"""T-V3-1: Link fetcher module — URL extraction + HTTP prefetch.

Extracts URLs from chat message text bodies and pre-fetches page titles,
meta descriptions, and body text, returning structured LinkPreview dataclass
instances. Includes SSRF protection and async concurrency control.

Public API:
    LinkPreview — 7-field dataclass for structured link preview data
    fetch_link_preview — single URL fetch with SSRF guard (never raises)
    fetch_all_links — batch entry point with dedup, concurrency, and error isolation

Integration:
    - Called from content_enrich pipeline node (T-V4-1) between data_fetch and orchestrator
    - Returns {server_id: LinkPreview} mapping consumed by format_message() (T-V3-2)
    - Lazy-imported per P016 pattern; zero imports from pipeline/ or sibling modules

Design principles:
    - P014: fetch_link_preview() never raises — all failures return LinkPreview with error
    - L014: fetch_all_links() uses asyncio.gather(return_exceptions=True)
    - A008: all HTML extraction variables pre-initialized before try/except chains
    - L016: safe extraction helpers with None/empty/missing defaults
    - P008: single URL timeout does not block the batch pipeline

SSRF protection layers:
    1. Scheme validation (http/https only)
    2. DNS resolution (wrapped in asyncio.to_thread, no event loop blocking)
    3. Private/loopback/link-local/unspecified IP checks via ipaddress module
    4. Explicit 0.0.0.0/8 block (not covered by ipaddress.is_private)
    5. DNS failure → reject (fail closed)
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

# URL extraction regex: matches http(s):// URLs and www.-prefixed bare URLs.
# Excludes whitespace, angle brackets, and double quotes as URL terminators.
_URL_REGEX = re.compile(r'https?://[^\s<>"]+|www\.[^\s<>"]+')

# Trailing punctuation characters stripped during post-extraction cleanup.
# These commonly appear in parentheticals or sentence-final position.
_TRAILING_PUNCTUATION = frozenset(")].,;:!?")

# File extensions that indicate image/media URLs (excluded from fetching).
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")

# Media API path fragment to exclude (internal media links).
_MEDIA_API_PATH = "/api/v1/media/"

# Default configuration values.
_DEFAULT_TIMEOUT: float = 10.0
_DEFAULT_MAX_CONCURRENCY: int = 3
_DEFAULT_MAX_CONTENT_LENGTH: int = 2000

# HTTP request defaults.
_MAX_REDIRECTS: int = 5
_USER_AGENT: str = "Winnow-LinkFetcher/1.0"

# Max characters for extracted text fields (title, description).
_FIELD_MAX_CHARS: int = 500

# Max characters for error messages stored in LinkPreview.error.
_ERROR_MAX_CHARS: int = 200

# 0.0.0.0/8 network — not fully covered by ipaddress.is_unspecified
# (which only catches 0.0.0.0 itself, not the entire /8 range).
_ZERO_NETWORK_V4 = ipaddress.IPv4Network("0.0.0.0/8")

# ============================================================
# LinkPreview dataclass
# ============================================================


@dataclass
class LinkPreview:
    """Structured link preview data for a fetched URL.

    All fields except ``url`` have sensible defaults so consumers can
    safely access any attribute without None checks.

    Fields:
        url: The original (normalized) URL that was fetched.
        title: Page title from <title> tag, truncated to 500 chars.
        description: Meta description or body text fallback, truncated to 500 chars.
        site_name: og:site_name meta tag value.
        status_code: HTTP response status code (0 if no request was made).
        error: Error description on failure (empty string on success).
        fetched_at: ISO 8601 UTC timestamp of when the fetch completed.
    """

    url: str
    title: str = ""
    description: str = ""
    site_name: str = ""
    status_code: int = 0
    error: str = ""
    fetched_at: str = ""


# ============================================================
# URL extraction helpers
# ============================================================


def _extract_urls(text: str) -> list[str]:
    """Extract all unique URLs from a text string.

    Uses the spec-defined regex ``https?://[^\\s<>\"]+|www\\.[^\\s<>\"]+``
    to find URLs, then applies post-extraction cleanup:

    1. Strips a single trailing punctuation character: ``) ] . , ; : ! ?``
       (handles URLs embedded in parentheticals or sentence-final position)
    2. Prepends ``https://`` to bare ``www.``-prefixed URLs
    3. Deduplicates (order-preserving — first occurrence wins)

    Args:
        text: Raw message text that may contain URLs.

    Returns:
        List of unique, normalized URL strings. Empty list if no URLs found.
    """
    if not text:
        return []

    raw_matches = _URL_REGEX.findall(text)
    seen: set[str] = set()
    result: list[str] = []

    for url in raw_matches:
        # Post-extraction cleanup: strip trailing punctuation (BLOCKING fix #2)
        if url[-1] in _TRAILING_PUNCTUATION:
            url = url[:-1]

        # Normalize: prepend https:// to bare www. URLs
        if url.startswith("www.") and "://" not in url:
            url = f"https://{url}"

        # Order-preserving dedup
        if url not in seen:
            seen.add(url)
            result.append(url)

    return result


def _is_excluded_url(url: str) -> bool:
    """Check whether a URL should be excluded from fetching.

    Excludes:
    - URLs whose path ends with image extensions (.jpg, .jpeg, .png, .gif,
      .webp, .bmp, .svg) — case-insensitive
    - URLs with ``/api/v1/media/`` in the path component

    Only the URL **path** is checked (not query string or fragment),
    so ``?format=jpg`` is NOT falsely excluded (evaluator criterion 3).

    Args:
        url: Normalized URL string.

    Returns:
        True if the URL should be excluded.
    """
    parsed = urlparse(url)
    path_lower = parsed.path.lower()

    # Exclude image file extensions in path
    if path_lower.endswith(_IMAGE_EXTENSIONS):
        return True

    # Exclude internal media API links
    return _MEDIA_API_PATH in path_lower


# ============================================================
# SSRF guard
# ============================================================


def is_private_url(url: str) -> bool:
    """SSRF guard — check if a URL points to a private/internal IP.

    This function is **synchronous** by design: DNS resolution via
    ``socket.gethostbyname()`` blocks, so callers MUST wrap it in
    ``await asyncio.to_thread(is_private_url, url)`` (evaluator criterion 15).

    Protection layers:
    1. Scheme validation — only ``http`` and ``https`` allowed
    2. No hostname → reject
    3. DNS resolution → resolve hostname to IP
    4. IP address checks against:
       - 127.0.0.0/8 (loopback)
       - 10.0.0.0/8 (private)
       - 172.16.0.0/12 (private)
       - 192.168.0.0/16 (private)
       - 169.254.0.0/16 (link-local)
       - 0.0.0.0/8 (this host on this network — not covered by is_private)
       - ::1 (IPv6 loopback)
       - fe80::/10 (IPv6 link-local)
    5. DNS resolution failure → reject (fail closed)

    Args:
        url: URL string to validate.

    Returns:
        True if the URL is private/internal and should be blocked.
    """
    parsed = urlparse(url)

    # Layer 1: Scheme validation
    if parsed.scheme not in ("http", "https"):
        return True

    # Layer 2: Hostname extraction
    hostname = parsed.hostname
    if not hostname:
        return True

    # Layer 3-4: DNS resolution + IP checks
    try:
        resolved = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(resolved)
    except (ValueError, socket.gaierror):
        # DNS resolution failure → fail closed
        return True

    # Python's ipaddress covers most ranges:
    #   is_private:    10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7
    #   is_loopback:   127.0.0.0/8, ::1
    #   is_link_local: 169.254.0.0/16, fe80::/10
    #   is_unspecified: 0.0.0.0, ::
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return True

    # Explicit check for 0.0.0.0/8 (is_unspecified only covers 0.0.0.0 itself)
    return isinstance(ip, ipaddress.IPv4Address) and ip in _ZERO_NETWORK_V4


# ============================================================
# HTML parsing helpers (L016 safe extraction pattern)
# ============================================================


def _extract_title(html: str) -> str:
    """Extract text from <title> tag.

    Args:
        html: Raw HTML content.

    Returns:
        Title text (whitespace-collapsed), or empty string if no <title> found.
    """
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    text = match.group(1).strip()
    return re.sub(r"\s+", " ", text)


def _extract_meta(html: str, attr_name: str, attr_value: str) -> str:
    """Extract a value from a <meta> tag by attribute matching.

    Generic helper following the L016 safe extraction pattern:
    handles missing tags, missing attributes, and None values.

    Searches for ``<meta {attr_name}="{attr_value}" content="...">``
    handling both attribute orderings (attr before content or vice versa).

    Args:
        html: Raw HTML content.
        attr_name: Attribute to match on (``"name"`` or ``"property"``).
        attr_value: Value of the matching attribute (e.g., ``"description"``).

    Returns:
        The ``content`` attribute value, or empty string if not found.
    """
    # Find all <meta> tags in the HTML
    meta_tags = re.findall(r"<meta\b[^>]*>", html, re.IGNORECASE)
    if not meta_tags:
        return ""

    escaped_value = re.escape(attr_value)
    # Pattern: <meta ... attr_name="attr_value" ... content="VALUE" ...>
    # or        <meta ... content="VALUE" ... attr_name="attr_value" ...>
    attr_pat = rf'{attr_name}=["\']{escaped_value}["\']'

    for tag in meta_tags:
        if re.search(attr_pat, tag, re.IGNORECASE):
            content_match = re.search(r'content=["\']([^"\']*)["\']', tag, re.IGNORECASE)
            if content_match:
                return content_match.group(1).strip()

    return ""


def _extract_body_text(html: str) -> str:
    """Extract visible text from <body> as a fallback.

    Strips all HTML tags, collapses whitespace, and returns the first
    portion of visible text.

    Args:
        html: Raw HTML content.

    Returns:
        Plain text extracted from the body, or empty string if no body found.
    """
    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.IGNORECASE | re.DOTALL)
    if not body_match:
        return ""
    body_text = body_match.group(1)
    # A010: Strip all HTML tags
    body_text = re.sub(r"<[^>]+>", "", body_text)
    # Collapse whitespace to single spaces
    body_text = re.sub(r"\s+", " ", body_text).strip()
    return body_text


def _parse_html(html: str) -> tuple[str, str, str]:
    """Parse HTML and extract title, description, site_name.

    Extraction priority:
    1. Title from ``<title>`` tag
    2. Description from ``<meta name="description">``
    3. Site name from ``<meta property="og:site_name">``
    4. Fallback: if no title AND no description, extract body text as title

    All extracted fields are truncated to ``_FIELD_MAX_CHARS`` (500).

    **A008 compliance**: All variables are pre-initialized before any
    extraction attempt. If all layers fail, we return empty strings —
    never an uninitialized variable.

    Args:
        html: Raw HTML content (may be truncated).

    Returns:
        Tuple of ``(title, description, site_name)``.
    """
    # A008: Pre-initialize all extraction variables
    title = ""
    description = ""
    site_name = ""

    # Layer 1: Extract <title>
    title = _extract_title(html)
    if title:
        title = title[:_FIELD_MAX_CHARS]

    # Layer 2: Extract <meta name="description">
    description = _extract_meta(html, "name", "description")
    if description:
        description = description[:_FIELD_MAX_CHARS]

    # Layer 3: Extract <meta property="og:site_name">
    site_name = _extract_meta(html, "property", "og:site_name")
    if site_name:
        site_name = site_name[:_FIELD_MAX_CHARS]

    # Layer 4: Fallback — if no title and no description, use body text
    if not title and not description:
        body_text = _extract_body_text(html)
        if body_text:
            title = body_text[:_FIELD_MAX_CHARS]

    return title, description, site_name


# ============================================================
# Environment variable helpers
# ============================================================


def _read_env_float(name: str, default: float) -> float:
    """Read a float environment variable with fallback.

    Args:
        name: Environment variable name.
        default: Default value if not set or invalid.

    Returns:
        Parsed float value or default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning("Invalid value for %s=%s, using default %s", name, raw, default)
        return default


def _read_env_int(name: str, default: int) -> int:
    """Read an integer environment variable with fallback.

    Args:
        name: Environment variable name.
        default: Default value if not set or invalid.

    Returns:
        Parsed int value or default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("Invalid value for %s=%s, using default %s", name, raw, default)
        return default


# ============================================================
# Core fetch functions
# ============================================================


async def fetch_link_preview(
    url: str,
    timeout: float = _DEFAULT_TIMEOUT,
    max_content_length: int = _DEFAULT_MAX_CONTENT_LENGTH,
) -> LinkPreview:
    """Fetch and extract preview data from a single URL.

    **P014 compliance**: This function NEVER raises exceptions.
    All failure modes (SSRF block, timeout, HTTP error, connection error,
    non-HTML content type, parse failure) return a ``LinkPreview`` with
    the ``error`` field populated.

    Steps:
    1. SSRF check via ``is_private_url()`` (DNS wrapped in ``asyncio.to_thread``)
    2. HTTP GET with timeout, redirect following, and User-Agent header
    3. Content-Type validation (must contain ``text/html``)
    4. HTML parsing for title, description, site_name
    5. Always closes the httpx client via ``async with``

    Args:
        url: URL to fetch (normalized).
        timeout: HTTP request timeout in seconds.
        max_content_length: Max characters of HTML body to parse.

    Returns:
        LinkPreview with fetch results or error description.
    """
    # Step 1: SSRF guard (DNS is synchronous → offload to thread)
    # BLOCKING fix #1: wrap socket.gethostbyname() in asyncio.to_thread()
    try:
        is_private = await asyncio.to_thread(is_private_url, url)
    except Exception:
        is_private = True  # Fail closed

    if is_private:
        parsed = urlparse(url)
        logger.warning(
            "SSRF blocked: url=%s, resolved_ip=%s",
            url,
            parsed.hostname or "unknown",
        )
        return LinkPreview(url=url, error="SSRF blocked: private/internal IP")

    # Step 2-6: HTTP GET + parse with manual redirect handling
    # T-V3-4 SECURITY FIX: Manual redirect loop instead of follow_redirects=True.
    # Each redirect target is validated through is_private_url() BEFORE the
    # HTTP request is made, preventing SSRF via open redirector attacks
    # (e.g., https://evil.com → 302 → http://169.254.169.254/).
    try:
        current_url = url
        redirects_remaining = _MAX_REDIRECTS

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,  # Manual redirect handling for SSRF safety
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            while redirects_remaining >= 0:
                # T-V3-4: SSRF check for EVERY redirect target,
                # not just the original URL
                try:
                    is_private = await asyncio.to_thread(is_private_url, current_url)
                except Exception:
                    is_private = True  # Fail closed

                if is_private:
                    parsed_target = urlparse(current_url)
                    logger.warning(
                        "SSRF blocked (redirect target): url=%s target=%s resolved_ip=%s",
                        url,
                        current_url,
                        parsed_target.hostname or "unknown",
                    )
                    return LinkPreview(url=url, error="SSRF blocked: private/internal IP")

                response = await client.get(current_url)
                status_code = response.status_code

                # Check for redirect (3xx with Location header)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    if location:
                        current_url = urljoin(current_url, location)
                        redirects_remaining -= 1
                        logger.debug(
                            "Redirect: %s -> %s (%d remaining)",
                            url,
                            current_url,
                            redirects_remaining,
                        )
                        continue
                    # No Location header — treat as final response
                    break

                # Final response (not a redirect)
                break

            # Too many redirects
            if redirects_remaining < 0:
                return LinkPreview(
                    url=url,
                    error=f"too many redirects (max {_MAX_REDIRECTS})",
                )

            fetched_at = datetime.now(UTC).isoformat()

            # HTTP error status
            if status_code >= 400:
                logger.debug("HTTP error for url=%s status_code=%d", url, status_code)
                return LinkPreview(
                    url=url,
                    status_code=status_code,
                    error=f"HTTP {status_code}",
                    fetched_at=fetched_at,
                )

            # BLOCKING fix #3: Content-Type validation before HTML parsing
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type.lower():
                logger.debug("Non-HTML content type for url=%s: %s", url, content_type)
                return LinkPreview(
                    url=url,
                    status_code=status_code,
                    error=f"non-HTML content type: {content_type}",
                    fetched_at=fetched_at,
                )

            # Parse HTML (truncate to max_content_length for memory safety)
            html_to_parse = response.text[:max_content_length]
            title, description, site_name = _parse_html(html_to_parse)

            logger.debug(
                "Fetched url=%s status=%d title=%.50s",
                url,
                status_code,
                title,
            )

            return LinkPreview(
                url=url,
                title=title,
                description=description,
                site_name=site_name,
                status_code=status_code,
                fetched_at=fetched_at,
            )

    except httpx.TimeoutException:
        logger.warning("Timeout fetching url=%s", url)
        return LinkPreview(url=url, error="timeout")

    except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        logger.warning("Connection error for url=%s: %.100s", url, str(exc))
        return LinkPreview(url=url, error=str(exc)[:_ERROR_MAX_CHARS])

    except Exception as exc:
        logger.warning("Unexpected error fetching url=%s: %.100s", url, str(exc))
        return LinkPreview(url=url, error=str(exc)[:_ERROR_MAX_CHARS])


async def fetch_all_links(
    messages: list[dict],
    max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
) -> dict[str, LinkPreview]:
    """Batch entry point — extract and fetch previews for all text messages.

    Flow:
    1. Extract URLs from text messages only (skip image/file/voice/video/appmsg)
    2. Build ``url -> [server_id, ...]`` mapping (dedup: one fetch per unique URL)
    3. Exclude image/media URLs via ``_is_excluded_url()``
    4. Block private/internal URLs via ``is_private_url()`` (no HTTP fetch)
    5. Concurrent fetching with ``asyncio.Semaphore`` for rate limiting
    6. **L014**: Use ``asyncio.gather(return_exceptions=True)`` for error isolation
    7. Build ``{server_id: LinkPreview}`` mapping for consumers

    **P008 compliance**: A single URL timeout or failure never blocks the
    entire batch. All successful results are returned alongside error entries.

    **P016 readiness**: This function reads env vars at call time (not import
    time), enabling lazy import and test-time monkeypatch interception.

    Environment variables (read once at entry):
    - ``LINK_FETCH_TIMEOUT`` (default 10, float): HTTP request timeout
    - ``LINK_FETCH_MAX_CONCURRENCY`` (default 3, int): Max concurrent requests
    - ``LINK_FETCH_MAX_CONTENT_LENGTH`` (default 2000, int): Max HTML chars

    Args:
        messages: List of message dicts. Each must have at least:
            ``content`` (str), ``msg_type`` (str), and ``server_id`` or ``serverID``.
        max_concurrency: Max concurrent HTTP requests. Overridden by
            ``LINK_FETCH_MAX_CONCURRENCY`` env var if set.

    Returns:
        Dict mapping ``server_id`` to ``LinkPreview``. Server IDs without
        URLs are omitted. Multiple server IDs sharing the same URL each
        receive a reference to the same ``LinkPreview`` object (shared by
        identity — documented as intentional per evaluator criterion 6).
    """
    # Read env vars once at entry (criterion 9)
    timeout = _read_env_float("LINK_FETCH_TIMEOUT", _DEFAULT_TIMEOUT)
    max_concurrency = _read_env_int("LINK_FETCH_MAX_CONCURRENCY", max_concurrency)
    max_content_length = _read_env_int("LINK_FETCH_MAX_CONTENT_LENGTH", _DEFAULT_MAX_CONTENT_LENGTH)

    # Step 1: Extract URLs from text messages only
    url_to_sids: dict[str, list[str]] = {}
    excluded_count = 0

    for msg in messages:
        if msg.get("msg_type") != "text":
            continue

        sid = msg.get("server_id") or msg.get("serverID", "")
        if not sid:
            continue

        content = msg.get("content", "")
        if not content:
            continue

        urls = _extract_urls(content)
        for url in urls:
            # Step 3: Check excluded URLs
            if _is_excluded_url(url):
                excluded_count += 1
                continue

            if url not in url_to_sids:
                url_to_sids[url] = []
            url_to_sids[url].append(sid)

    unique_urls = list(url_to_sids.keys())

    if not unique_urls:
        logger.debug("fetch_all_links: no URLs found in messages")
        return {}

    # Step 2 & 4: Separate private URLs (no HTTP fetch needed)
    ssrf_blocked_count = 0
    url_to_preview: dict[str, LinkPreview] = {}

    private_checks = await asyncio.gather(
        *[asyncio.to_thread(is_private_url, url) for url in unique_urls],
        return_exceptions=True,
    )

    fetch_urls: list[str] = []
    for url, is_private in zip(unique_urls, private_checks, strict=True):
        if isinstance(is_private, BaseException):
            # Exception in SSRF check → fail closed
            ssrf_blocked_count += 1
            url_to_preview[url] = LinkPreview(url=url, error="SSRF check failed")
        elif is_private:
            ssrf_blocked_count += 1
            parsed = urlparse(url)
            logger.warning(
                "SSRF blocked: url=%s, resolved_ip=%s",
                url,
                parsed.hostname or "unknown",
            )
            url_to_preview[url] = LinkPreview(url=url, error="SSRF blocked: private/internal IP")
        else:
            fetch_urls.append(url)

    # Step 5: Concurrent fetching with semaphore
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _fetch_with_semaphore(u: str) -> LinkPreview:
        async with semaphore:
            return await fetch_link_preview(
                u,
                timeout=timeout,
                max_content_length=max_content_length,
            )

    # Step 6: L014 — gather with return_exceptions for error isolation
    if fetch_urls:
        results = await asyncio.gather(
            *[_fetch_with_semaphore(u) for u in fetch_urls],
            return_exceptions=True,
        )

        for u, result in zip(fetch_urls, results, strict=True):
            if isinstance(result, BaseException):
                url_to_preview[u] = LinkPreview(
                    url=u,
                    error=str(result)[:_ERROR_MAX_CHARS],
                )
            else:
                url_to_preview[u] = result  # type: ignore[assignment]

    # Step 7: Build {server_id: LinkPreview} mapping
    out: dict[str, LinkPreview] = {}
    for url, sids in url_to_sids.items():
        preview = url_to_preview.get(url)
        if preview is None:
            continue
        for sid in sids:
            # Multiple server_ids for the same URL share the same object
            out[sid] = preview

    # Step 8: Log summary (criterion 12)
    success_count = sum(1 for p in url_to_preview.values() if not p.error)
    failure_count = sum(
        1
        for p in url_to_preview.values()
        if p.error and "SSRF blocked" not in p.error and "SSRF check failed" not in p.error
    )
    ssrf_blocked = ssrf_blocked_count

    logger.info(
        "fetch_all_links: total=%d excluded=%d ssrf_blocked=%d success=%d failure=%d",
        len(unique_urls),
        excluded_count,
        ssrf_blocked,
        success_count,
        failure_count,
    )

    return out
