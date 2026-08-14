"""File content hash suffix dedup — inject SHA256-based suffix into file names.

WeChat SMB storage may contain multiple files with the same name in the same
month.  This module locates the correct file for each message via ``<totallen>``
(file size from XML) disambiguation, computes the file's content SHA256, and
injects an 8-hex-char suffix into the message ``content`` field so the LLM
sees unique filenames (e.g. ``报告_a1b2c3d4.pdf`` instead of ``报告.pdf``).

Architecture:
  - ``apply_file_content_hash_suffix`` — ASYNC, called from node_content_enrich
    after parse_raw_messages.  Scans SMB, hashes files, mutates messages in-place.
  - Helper functions are pure (no DB, no I/O) for testability.
"""

from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

# ── public entry point ────────────────────────────────────────────────────


async def apply_file_content_hash_suffix(
    messages: list[dict],
    smb_root: str,
) -> int:
    """Scan SMB, hash file content, inject hash suffix into message ``content``.

    For each file or file-type appmsg message with a parseable ``<title>`` and
    ``<totallen>`` (or ``<length>``), locates the matching file in the SMB
    ``YYYY-MM/`` directory, computes its SHA256, and replaces the plain filename
    in the message's ``content`` field with ``{name}_{sha256[:8]}.{ext}``.

    Returns the number of messages that were successfully corrected.
    Best-effort: failures are logged individually and do not block the pipeline.
    """
    if not messages or not smb_root:
        return 0

    smb_path = Path(smb_root)
    if not smb_path.is_dir():
        logger.warning("file_dedup: SMB root not accessible: %s", smb_root)
        return 0

    eligible = _collect_eligible(messages)
    if not eligible:
        return 0

    # Cache SMB directory listings per month to avoid repeated filesystem scans
    _smb_cache: dict[str, list[tuple[str, Path, int]]] = {}  # month_key → [(basename, path, size)]

    corrected = 0
    for idx, filename, file_size, ts_ms in eligible:
        month_key = _ts_to_month_key(ts_ms)
        if not month_key:
            continue

        # Load / cache SMB listing for this month
        if month_key not in _smb_cache:
            _smb_cache[month_key] = _scan_smb_month(smb_path, month_key)
        candidates = _smb_cache[month_key]

        # Disambiguate: match by filename + file_size
        smb_file = _match_smb_file(candidates, filename, file_size)
        if smb_file is None:
            logger.debug(
                "file_dedup: no SMB match for %s (%d bytes) in %s",
                filename,
                file_size,
                month_key,
            )
            continue

        # Compute content hash
        suffix = _compute_hash_suffix(smb_file)
        if not suffix:
            continue

        # Inject hash suffix into message content
        old_content = str(messages[idx].get("content", ""))
        new_content = _inject_hash_suffix(old_content, filename, suffix)
        if new_content != old_content:
            messages[idx]["content"] = new_content
            messages[idx]["content_hash_suffix"] = suffix
            corrected += 1
            logger.debug(
                "file_dedup: %s → %s",
                Path(smb_file).name,
                new_content[:120],
            )

    if corrected:
        logger.info("file_dedup: corrected %d file/appmsg messages", corrected)
    return corrected


# ── eligibility collection ─────────────────────────────────────────────────


def _collect_eligible(
    messages: list[dict],
) -> list[tuple[int, str, int, int]]:
    """Return [(index, filename, file_size_bytes, timestamp_ms), ...] for eligible messages.

    Eligible messages are those with:
      - msg_type "file", or msg_type "appmsg" with <type>6</type>
      - Parseable <title> in raw_content XML
      - Parseable file size (<totallen> for appmsg, <length> for file)
      - Non-zero timestamp
    """
    result: list[tuple[int, str, int, int]] = []
    for i, msg in enumerate(messages):
        msg_type = str(msg.get("msg_type", ""))
        raw_content = str(msg.get("raw_content", ""))
        if not raw_content or msg_type not in ("file", "appmsg"):
            continue

        # Extract <title>
        title = _extract_xml_text(raw_content, "title")
        if not title:
            continue

        # Extract file size
        if msg_type == "appmsg":
            # Only type=6 (file) appmsg
            appmsg_type = _extract_xml_text(raw_content, "type")
            if appmsg_type and appmsg_type.strip() not in ("6", "15"):
                # type 15 is also file-like in some WeChat versions
                if not _re_six.search(appmsg_type):
                    continue
            file_size = _extract_appmsg_file_size(raw_content)
        else:
            # Direct file message: <fileupload><length>
            file_size = _extract_fileupload_length(raw_content)

        if file_size <= 0:
            continue

        ts = int(msg.get("timestamp", 0))
        if not ts:
            continue

        result.append((i, title, file_size, ts))
    return result


# ── XML extraction helpers ──────────────────────────────────────────────────


def _extract_xml_text(xml_str: str, tag: str) -> str:
    """Extract text content of *tag* from raw XML string.

    Handles CDATA-wrapped content automatically (ElementTree strips CDATA).
    Returns empty string on parse failure or missing tag.
    """
    try:
        # Strip sender prefix (e.g. "wxid_xxx:\n")
        lt = xml_str.find("<")
        if lt > 0:
            xml_str = xml_str[lt:]
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return ""
    el = root.find(f".//{tag}")
    if el is None:
        return ""
    return (el.text or "").strip()


def _extract_appmsg_file_size(raw_content: str) -> int:
    """Extract <appattach><totallen> from appmsg XML. Returns 0 on failure."""
    try:
        lt = raw_content.find("<")
        if lt > 0:
            raw_content = raw_content[lt:]
        root = ET.fromstring(raw_content)
    except ET.ParseError:
        return 0
    el = root.find(".//appattach/totallen")
    if el is None:
        # Fallback: try direct <appattach> → <totallen> via regex
        m = re.search(r"<totallen>(\d+)</totallen>", raw_content)
        return int(m.group(1)) if m else 0
    try:
        return int((el.text or "").strip())
    except (ValueError, TypeError):
        return 0


def _extract_fileupload_length(raw_content: str) -> int:
    """Extract <fileupload><length> from direct file message XML."""
    try:
        lt = raw_content.find("<")
        if lt > 0:
            raw_content = raw_content[lt:]
        root = ET.fromstring(raw_content)
    except ET.ParseError:
        return 0
    el = root.find(".//fileupload/length")
    if el is None:
        m = re.search(r"<length>(\d+)</length>", raw_content)
        return int(m.group(1)) if m else 0
    try:
        return int((el.text or "").strip())
    except (ValueError, TypeError):
        return 0


_re_six = re.compile(r"\b(6|15)\b")


# ── timestamp → month key ───────────────────────────────────────────────────


def _ts_to_month_key(ts_ms: int) -> str:
    """Convert millisecond timestamp to YYYY-MM string. Returns '' on error."""
    import datetime

    try:
        dt = datetime.datetime.fromtimestamp(ts_ms / 1000)
        return dt.strftime("%Y-%m")
    except (ValueError, OSError):
        return ""


# ── SMB scanning ────────────────────────────────────────────────────────────


def _scan_smb_month(
    smb_root: Path,
    month_key: str,
) -> list[tuple[str, Path, int]]:
    """Scan ``{smb_root}/{month_key}/`` and return [(basename, full_path, size_bytes), ...].

    Returns empty list if the directory does not exist or is unreadable.
    """
    month_dir = smb_root / month_key
    if not month_dir.is_dir():
        return []
    entries: list[tuple[str, Path, int]] = []
    try:
        for f in month_dir.iterdir():
            if not f.is_file():
                continue
            try:
                fsize = f.stat().st_size
            except OSError:
                fsize = 0
            entries.append((f.name, f, fsize))
    except OSError:
        return []
    return entries


# ── SMB file matching ───────────────────────────────────────────────────────


def _match_smb_file(
    candidates: list[tuple[str, Path, int]],
    target_name: str,
    target_size: int,
) -> str | None:
    """Find the SMB file matching *target_name* and *target_size* (±5%).

    Disambiguation strategy:
      1. Collect all candidates whose basename starts with *target_name*
         (handles WeChat suffixes like ``报告(1).pdf`` for name ``报告.pdf``).
      2. If exactly one candidate — return it.
      3. If multiple — compare file sizes (±5% tolerance), return the closest match.
      4. If none match — return None.
    """
    target_name_lower = target_name.lower()
    stem = Path(target_name).stem

    # Find candidates where the SMB filename starts with the target stem
    # This catches: "报告.pdf", "报告(1).pdf", "报告.1.pdf" for target "报告.pdf"
    matching: list[tuple[str, Path, int]] = []
    for basename, full_path, fsize in candidates:
        bn_lower = basename.lower()
        bn_stem = Path(basename).stem.lower()
        # Match: SMB filename starts with our target stem (ignoring extension)
        if bn_stem == stem.lower():
            matching.append((basename, full_path, fsize))
        elif bn_stem.startswith(stem.lower() + "(") or bn_stem.startswith(stem.lower() + "."):
            # WeChat renamed: 报告(1).pdf or 报告.1.pdf
            matching.append((basename, full_path, fsize))
        elif bn_lower == target_name_lower:
            matching.append((basename, full_path, fsize))

    if not matching:
        return None
    if len(matching) == 1:
        return str(matching[0][1])

    # Multiple matches — disambiguate by file size (±5% tolerance)
    if target_size <= 0:
        # Can't disambiguate without size — pick the first exact name match
        for bn, fp, _sz in matching:
            if bn.lower() == target_name_lower:
                return str(fp)
        return str(matching[0][1])

    best: tuple[str, Path, int] | None = None
    best_diff = float("inf")
    for bn, fp, fsize in matching:
        diff = abs(fsize - target_size)
        if diff <= target_size * 0.05 or diff < 1024:  # ±5% or within 1KB
            if diff < best_diff:
                best = (bn, fp, fsize)
                best_diff = diff

    if best is not None:
        return str(best[1])

    # No size match — return the exact name match if exists
    for bn, fp, _sz in matching:
        if bn.lower() == target_name_lower:
            return str(fp)
    return None


# ── hash computation ────────────────────────────────────────────────────────


def _compute_hash_suffix(file_path: str, n_chars: int = 8) -> str:
    """Compute SHA256 of *file_path* and return first *n_chars* hex chars.

    Returns empty string on I/O error.
    """
    hasher = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()[:n_chars]
    except OSError:
        logger.debug("file_dedup: hash failed for %s", file_path, exc_info=True)
        return ""


# ── content injection ───────────────────────────────────────────────────────


def _inject_hash_suffix(content: str, base_filename: str, suffix: str) -> str:
    """Replace first occurrence of *base_filename* in *content* with hash-suffixed version.

    Example:
        "[文件: 报告.pdf (235.8KB)]" with base="报告.pdf", suffix="a1b2c3d4"
        → "[文件: 报告_a1b2c3d4.pdf (235.8KB)]"

        "[分享文件] | 标题：报告.pdf | 大小：235.8KB"
        → "[分享文件] | 标题：报告_a1b2c3d4.pdf | 大小：235.8KB"
    """
    stem, ext = _split_filename_ext(base_filename)
    hashed_name = f"{stem}_{suffix}{ext}"
    return content.replace(base_filename, hashed_name, 1)


def _split_filename_ext(filename: str) -> tuple[str, str]:
    """Split a filename into (stem, extension). Extension includes the dot."""
    dot = filename.rfind(".")
    if dot > 0:
        return filename[:dot], filename[dot:]
    return filename, ""


# ── hash suffix stripping (for downstream matching) ─────────────────────────


def strip_hash_suffix(filename: str) -> tuple[str, str | None]:
    """Strip ``_{8-hex}`` suffix from a filename, return (base_name, hash_or_None).

    Example:
        "报告_a1b2c3d4.pdf" → ("报告.pdf", "a1b2c3d4")
        "报告.pdf" → ("报告.pdf", None)
        "file_12345678.tar.gz" → ("file_12345678.tar.gz", None)  # only last segment
    """
    name, ext = _split_filename_ext(filename)
    parts = name.rsplit("_", 1)
    if len(parts) == 2 and len(parts[1]) == 8:
        if re.fullmatch(r"[0-9a-fA-F]{8}", parts[1]):
            return f"{parts[0]}{ext}", parts[1].lower()
    return filename, None
