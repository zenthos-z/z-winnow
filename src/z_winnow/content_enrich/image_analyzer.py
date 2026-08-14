"""T-V1-1: Vision API + MCP dual-mode image analysis module.

Produces structured ImageDescription from chat images for content enrichment.
Supports three modes:
  - Vision API: Uses VISION_MODEL env var + LangChain init_chat_model
  - MCP: Uses MCP_IMAGE_ANALYSIS=true + MCP analyze_image tool
  - Disabled: Neither configured, returns placeholder

Integration points:
  - T-V4 content_enrich node: calls analyze_images_batch() for image messages
  - T-V1-3 context.py: format_message() injects returned descriptions into context
  - sandbox.py: contains_sanitized_pattern() checks OCR text for prompt injection
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from z_winnow.pipeline.sandbox import contains_sanitized_pattern

# P007: json_mode primary -> invoke+parse fallback
# A007: Use structlog (logger), not print with emoji — base64 avoids file encoding issues
# A008: Initialize data = None before JSON parse chain

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

VALID_IMAGE_TYPES = frozenset(
    {
        "screenshot",
        "diagram",
        "photo",
        "meme",
        "article",
        "environment",
        "other",
    }
)

SUPPORTED_FORMATS_DEFAULT: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

SUPPORTED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)

DEFAULT_MAX_FILE_SIZE_MB = 20
BYTES_PER_MB = 1024 * 1024

# Image analysis prompt — 7-category classification aligned with original system
IMAGE_ANALYSIS_PROMPT = """你是一个群聊图片分析师。请分析这张图片并输出结构化JSON描述。

## 分析策略 (按图片类型，共 7 种，与原系统 ImageAnalyzer 对齐)

**纯文本截图** (聊天记录、代码、文档、错误信息等):
- 逐字还原所有可见文字
- 如有代码则保留代码格式
- 标注截图来源 (如 "VS Code 终端截图"、"微信聊天记录")

**操作界面截图** (App 界面、Web 页面、设置面板、工具界面等):
- 描述界面布局和主要元素
- 还原界面中的所有文字内容（按钮、菜单、输入框等）

**知识卡片/信息图** (图文混排的说明图、流程图、思维导图、知识总结等):
- 还原所有文字内容
- 说明图片的逻辑结构（如"从左到右分为3个阶段"、"上方是标题，下方是分步骤说明"）

**海报/公告** (活动海报、课程通知、宣传图等):
- 还原所有文字内容（标题、时间、地点、联系方式等）
- 简述视觉布局

**环境照片** (会议室、活动现场、白板、屏幕拍摄等):
- 简述环境场景和关键元素
- 重点提取照片中可见的文字信息（如白板内容、投影屏幕文字）

**表情包/梗图**:
- 简述图片内容和传达的情绪/含义
- 如有文字则还原（包括梗图中的中文/英文文案）

**其他类型**:
- 先说明图片性质，再还原关键内容和文字信息

## 输出要求
- summary: 一句话概括 (20 字内)
- description: 3~5 句详细描述
- ocr_text: 图片中的所有可见文字（如无则为空字符串）
- image_type: screenshot|diagram|photo|meme|article|environment|other (共 7 种，与 ImageDescription schema 对齐)

只输出纯 JSON 对象，不要 markdown code fence，不要任何解释文字。
"""

# GIF multi-frame prompt — extends the standard prompt with a prefix noting
# that the following images are frames from an animated GIF, not separate images.
GIF_MULTI_FRAME_PROMPT = """你是一个群聊图片分析师。以下图片是**同一张动图（GIF）的多个关键帧**，按时间顺序排列。请综合所有帧的视觉信息，理解完整的动画内容（动作变化、情绪递进、文字变化等），然后输出结构化JSON描述。

## 分析策略 (按图片类型，共 7 种，与原系统 ImageAnalyzer 对齐)

**纯文本截图** (聊天记录、代码、文档、错误信息等):
- 逐字还原所有可见文字
- 如有代码则保留代码格式
- 标注截图来源 (如 "VS Code 终端截图"、"微信聊天记录")

**操作界面截图** (App 界面、Web 页面、设置面板、工具界面等):
- 描述界面布局和主要元素
- 还原界面中的所有文字内容（按钮、菜单、输入框等）

**知识卡片/信息图** (图文混排的说明图、流程图、思维导图、知识总结等):
- 还原所有文字内容
- 说明图片的逻辑结构（如"从左到右分为3个阶段"、"上方是标题，下方是分步骤说明"）

**海报/公告** (活动海报、课程通知、宣传图等):
- 还原所有文字内容（标题、时间、地点、联系方式等）
- 简述视觉布局

**环境照片** (会议室、活动现场、白板、屏幕拍摄等):
- 简述环境场景和关键元素
- 重点提取照片中可见的文字信息（如白板内容、投影屏幕文字）

**表情包/梗图**:
- 简述图片内容和传达的情绪/含义
- 如有文字则还原（包括梗图中的中文/英文文案）
- 注意：结合多帧变化，描述动画的关键动作和情绪递进

**其他类型**:
- 先说明图片性质，再还原关键内容和文字信息

## 输出要求
- summary: 一句话概括 (20 字内)
- description: 3~5 句详细描述（结合多帧动画变化）
- ocr_text: 图片中的所有可见文字（如无则为空字符串）
- image_type: screenshot|diagram|photo|meme|article|environment|other (共 7 种，与 ImageDescription schema 对齐)

只输出纯 JSON 对象，不要 markdown code fence，不要任何解释文字。
"""


# ============================================================
# Configuration helpers
# ============================================================


def _get_vision_model() -> str | None:
    """Get VISION_MODEL from Settings, or None if not configured.

    T-W12-5: S7 配置单源 — reads from Settings instead of os.getenv().
    A013: Called at function level, not module level.
    """
    from z_winnow.config.settings import get_settings

    val = get_settings().vision_model.strip()
    return val if val else None


def _get_vision_base_url() -> str | None:
    """Get VISION_BASE_URL for OpenAI-compatible Vision API proxy.

    T-W12-5: S7 配置单源.
    """
    from z_winnow.config.settings import get_settings

    val = get_settings().vision_base_url.strip()
    return val if val else None


def _get_vision_api_key() -> str | None:
    """Get VISION_API_KEY for OpenAI-compatible Vision API proxy.

    T-W12-5: S7 配置单源.
    """
    from z_winnow.config.settings import get_settings

    val = get_settings().vision_api_key.strip()
    return val if val else None


def _get_mcp_mode() -> bool:
    """Check if MCP image analysis mode is enabled.

    T-W12-5: S7 配置单源.
    """
    from z_winnow.config.settings import get_settings

    return get_settings().mcp_image_analysis


def _get_max_concurrency() -> int:
    """Get IMAGE_MAX_CONCURRENCY from Settings, default 5.

    T-W12-5: S7 配置单源.
    """
    from z_winnow.config.settings import get_settings

    return get_settings().image_max_concurrency


def _get_max_file_size_mb() -> int:
    """Get IMAGE_MAX_FILE_SIZE_MB from Settings, default 20.

    T-W12-5: S7 配置单源.
    """
    from z_winnow.config.settings import get_settings

    return get_settings().image_max_file_size_mb


def _get_supported_formats() -> frozenset[str]:
    """Get SUPPORTED_IMAGE_FORMATS from Settings, comma-separated.

    T-W12-5: S7 配置单源.
    """
    from z_winnow.config.settings import get_settings

    raw = get_settings().supported_image_formats.strip()
    if raw:
        return frozenset(f.strip().lower() for f in raw.split(",") if f.strip())
    return SUPPORTED_FORMATS_DEFAULT


def _get_mcp_endpoint() -> str:
    """Get MCP image analysis endpoint URL.

    T-W12-5: S7 配置单源.
    """
    from z_winnow.config.settings import get_settings

    return get_settings().mcp_image_endpoint


# ============================================================
# ImageDescription model
# ============================================================


class ImageDescription(BaseModel):
    """Structured description of an analyzed image.

    Produced by Vision API or MCP analysis, consumed by content enrichment
    pipeline to inject AI-generated descriptions into chat messages.

    Fields:
        summary: One-line summary of image content
        description: Detailed description, 3-5 sentences
        ocr_text: Text extracted from image via OCR (empty if none)
        image_type: One of screenshot|diagram|photo|meme|article|environment|other
    """

    summary: str = Field(
        default="",
        description="图片内容的一句话概括",
    )
    description: str = Field(
        default="",
        description="详细描述，3~5句",
    )
    ocr_text: str = Field(
        default="",
        description="图片中提取的文字内容（OCR），如无文字则为空字符串",
    )
    image_type: str = Field(
        default="other",
        description="图片类型: screenshot|diagram|photo|meme|article|environment|other",
    )

    @field_validator("image_type", mode="before")
    @classmethod
    def _validate_image_type(cls, v: str) -> str:
        """Validate and normalize image_type to known categories.

        Unknown types are logged and defaulted to 'other'.
        """
        if not isinstance(v, str):
            return "other"
        v_lower = v.lower().strip()
        if v_lower in VALID_IMAGE_TYPES:
            return v_lower
        logger.warning("Unknown image_type '%s', defaulting to 'other'", v_lower)
        return "other"

    @property
    def is_sanitized(self) -> bool:
        """Check if OCR text contains prompt injection patterns."""
        return contains_sanitized_pattern(self.ocr_text)

    def format_description(self) -> str:
        """Format this description as a text string for message injection.

        Applies sanitization prefix if OCR text matches injection patterns.
        """
        parts: list[str] = [f"[图片描述: {self.summary}]"]
        if self.description:
            parts.append(self.description)
        if self.ocr_text:
            ocr = self.ocr_text
            if self.is_sanitized:
                ocr = f"[已过滤] {ocr}"
            parts.append(f"[OCR: {ocr}]")
        return " ".join(parts)


# ============================================================
# Mode detection
# ============================================================


def _detect_mode() -> str:
    """Detect active analysis mode.

    Priority: VISION_MODEL > MCP_IMAGE_ANALYSIS > disabled

    Returns:
        'vision'   — VISION_MODEL is configured
        'mcp'      — MCP_IMAGE_ANALYSIS=true and VISION_MODEL unset
        'disabled' — neither backend configured
    """
    if _get_vision_model():
        return "vision"
    if _get_mcp_mode():
        return "mcp"
    return "disabled"


# ============================================================
# File validators
# ============================================================


def _validate_image_file(
    image_path: str,
    max_size_mb: int,
    supported_formats: frozenset[str],
) -> tuple[bool, str]:
    """Validate an image file against size and format constraints.

    Args:
        image_path: Path to the image file
        max_size_mb: Maximum file size in MB
        supported_formats: Set of allowed file extensions (lowercase)

    Returns:
        (is_valid, error_message) — is_valid=True if all checks pass
    """
    path = Path(image_path)

    # Existence check
    if not path.exists():
        return False, f"File not found: {image_path}"

    # Format gate
    suffix = path.suffix.lstrip(".").lower()
    # Normalize jpeg -> jpg
    if suffix == "jpeg":
        suffix = "jpg"
    if suffix not in supported_formats:
        return False, (
            f"Unsupported image format: .{suffix} "
            f"(supported: {', '.join(sorted(supported_formats))})"
        )

    # File size gate
    file_size = path.stat().st_size
    max_bytes = max_size_mb * BYTES_PER_MB
    if file_size > max_bytes:
        return False, (
            f"Image exceeds size limit: {file_size / BYTES_PER_MB:.1f}MB > {max_size_mb}MB"
        )

    return True, ""


# ============================================================
# GIF frame extraction (Pillow-based, graceful degradation)
# ============================================================

# Max frames to sample from an animated GIF when sending to the vision model.
# Chat stickers are typically 10–30 frames; 8 frames evenly sampled covers
# start, end, and key intermediate actions without blowing up token usage.
_MAX_GIF_FRAMES: int = 8


def _extract_gif_frames(image_path: str, max_frames: int = _MAX_GIF_FRAMES) -> list[str]:
    """Extract frames from an animated GIF as base64 PNG data-URL strings.

    Used by ``_analyze_via_vision_api`` to send GIF animations as multi-frame
    input instead of a static first-frame-only image.

    Behavior:
      - **Single-frame / non-animated GIF**: returns ``[]`` — caller should
        fall back to the existing static-image path.
      - **Animated GIF with ≤ max_frames**: every frame is returned.
      - **Animated GIF with > max_frames**: frames are *evenly sampled*
        (first and last are always included, rest are spaced evenly).
      - **Any error** (Pillow not installed, corrupt file, etc.): returns
        ``[]`` — caller falls back to static-image path.

    Each returned string is a self-contained data URL:
    ``data:image/png;base64,...`` — PNG encoding avoids GIF palette issues
    that can confuse some vision APIs.

    Args:
        image_path: Absolute path to the GIF file.
        max_frames: Maximum number of frames to return (default 8).

    Returns:
        List of data-URL strings, or empty list if extraction is unnecessary
        or impossible.
    """
    try:
        from PIL import Image  # lazy import, Pillow optional
    except ImportError:
        logger.debug("Pillow not installed — GIF multi-frame disabled")
        return []

    try:
        gif = Image.open(image_path)
    except Exception:
        logger.debug("Cannot open %s as image — skipping GIF extraction", image_path)
        return []

    try:
        is_animated = getattr(gif, "is_animated", False)
        n_frames = getattr(gif, "n_frames", 1)
    except Exception:
        is_animated = False
        n_frames = 1

    if not is_animated or n_frames <= 1:
        return []  # static GIF — no multi-frame needed

    # Build the list of frame indices to sample
    if n_frames <= max_frames:
        indices = list(range(n_frames))
    else:
        # Evenly sample: first + last + (max_frames - 2) interior frames
        step = (n_frames - 1) / (max_frames - 1)
        indices = [round(i * step) for i in range(max_frames)]
        # ensure first=0 and last=n_frames-1 (rounding can slip)
        indices[0] = 0
        indices[-1] = n_frames - 1
        # deduplicate adjacent collisions
        seen: set[int] = set()
        deduped: list[int] = []
        for idx in indices:
            if idx not in seen:
                seen.add(idx)
                deduped.append(idx)
        indices = deduped

    frames: list[str] = []
    for idx in indices:
        try:
            gif.seek(idx)
            # Convert to RGB (handle palette/RGBA modes) → PNG → base64
            converted = gif.convert("RGBA")
            import io

            buf = io.BytesIO()
            converted.save(buf, format="PNG")
            buf.seek(0)
            b64 = base64.b64encode(buf.read()).decode("ascii")
            frames.append(f"data:image/png;base64,{b64}")
        except Exception:
            logger.debug("GIF frame %d extraction failed for %s", idx, image_path)
            # omit this frame, continue with others
            continue

    if len(frames) <= 1:
        return []  # only got 1 frame out — treat as static

    logger.info(
        "GIF multi-frame: %d/%d frames extracted from %s",
        len(frames),
        n_frames,
        image_path,
    )
    return frames


# ============================================================
# Single image analysis
# ============================================================


async def analyze_single_image(image_path: str) -> ImageDescription:
    """Analyze a single image and return structured description.

    Mode selection (priority order):
      1. VISION_MODEL configured → Vision API mode (LangChain multimodal)
      2. MCP_IMAGE_ANALYSIS=true → MCP mode (HTTP to MCP server)
      3. Neither → Disabled mode (placeholder description)

    Args:
        image_path: Absolute or relative path to the image file

    Returns:
        ImageDescription with summary, description, ocr_text, image_type

    Raises:
        ValueError: If image_path is empty
        FileNotFoundError: If file does not exist (after validation)
    """
    if not image_path:
        raise ValueError("image_path must not be empty")

    max_size_mb = _get_max_file_size_mb()
    supported_formats = _get_supported_formats()

    # Validate file before analysis
    is_valid, error_msg = _validate_image_file(image_path, max_size_mb, supported_formats)
    if not is_valid:
        logger.warning("Image validation failed: %s", error_msg)
        return ImageDescription(
            summary=f"[跳过: {error_msg}]",
            description=error_msg,
            image_type="other",
        )

    mode = _detect_mode()

    if mode == "vision":
        return await _analyze_via_vision_api(image_path)
    elif mode == "mcp":
        return await _analyze_via_mcp(image_path)
    else:
        return _create_placeholder_description(image_path)


async def _analyze_via_vision_api(image_path: str) -> ImageDescription:
    """Analyze image using LangChain Vision API (multimodal model).

    Uses plain invoke + manual 3-strategy JSON extraction (direct → code fence
    → regex).  Neither with_structured_output nor bind(response_format=...) is
    used — langchain-openai ChatOpenAI treats response_format as OpenAI native
    structured output (reading .parsed instead of .content), which breaks on
    all OpenAI-compatible proxies (dmxapi, LiteLLM, OpenRouter, etc.).
    """
    from langchain.chat_models import init_chat_model
    from langchain.messages import HumanMessage

    model_name = _get_vision_model()
    if not model_name:
        return _create_placeholder_description(image_path)

    # Determine MIME type — base64 encoding avoids GBK emoji issues (A007)
    mime_type = mimetypes.guess_type(image_path)[0] or "image/png"
    if mime_type not in SUPPORTED_MIME_TYPES:
        logger.warning(
            "Unsupported MIME type '%s' for %s, defaulting to image/png",
            mime_type,
            image_path,
        )
        mime_type = "image/png"

    # --- GIF multi-frame path: extract frames and send as sequential images ---
    # When the file is an animated GIF, we extract up to _MAX_GIF_FRAMES evenly
    # sampled frames and send them as separate image_url blocks so the vision
    # model can perceive the animation rather than just the first frame.
    if mime_type == "image/gif":
        gif_frames = _extract_gif_frames(image_path)
        if gif_frames:
            content_blocks: list[dict[str, Any]] = [
                {"type": "text", "text": GIF_MULTI_FRAME_PROMPT}
            ]
            for _i, _fb64 in enumerate(gif_frames):
                content_blocks.append(
                    {"type": "image_url", "image_url": {"url": _fb64}}
                )
            msg = HumanMessage(content=content_blocks)
            # skip the static-image block below — jump directly to model invoke
            _gif_mode = True
        else:
            _gif_mode = False
    else:
        _gif_mode = False

    if not _gif_mode:
        # --- Static image path (original behaviour) ---
        # Read and encode image
        try:
            with open(image_path, "rb") as f:
                image_data = f.read()
            b64_data = base64.b64encode(image_data).decode("ascii")
        except OSError as exc:
            logger.error("Failed to read image %s: %s", image_path, exc)
            return ImageDescription(
                summary="[图片读取失败]",
                description=f"Failed to read image file: {exc}",
                image_type="other",
            )

        # Build multimodal message (single image)
        msg = HumanMessage(
            content=[
                {"type": "text", "text": IMAGE_ANALYSIS_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64_data}"},
                },
            ]
        )

    # Plain invoke + manual JSON extraction (3-strategy: direct → code fence →
    # regex).  NOT using with_structured_output or bind(response_format=...)
    # because langchain-openai's ChatOpenAI treats response_format as OpenAI
    # native structured output (reading .parsed instead of .content), which
    # breaks on all OpenAI-compatible proxies (dmxapi, LiteLLM, OpenRouter, etc.).
    try:
        from z_winnow.config.settings import get_settings as _gs

        init_kwargs: dict[str, Any] = {
            "temperature": 0.1,
            "max_tokens": _gs().vision_max_tokens,
        }
        vision_base_url = _get_vision_base_url()
        vision_api_key = _get_vision_api_key()
        if vision_base_url:
            init_kwargs["model_provider"] = "openai"
            init_kwargs["base_url"] = vision_base_url
            init_kwargs["api_key"] = vision_api_key or "not-set"
        vision_model = init_chat_model(
            model_name,
            **init_kwargs,
        )
        raw = await vision_model.ainvoke([msg])
        raw_text = str(raw.content) if hasattr(raw, "content") else str(raw)

        # A008: Initialize data before extraction chain
        data: Any = None
        try:
            data = json.loads(raw_text.strip())
        except (json.JSONDecodeError, TypeError):
            # Layer 2: fenced code block
            m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw_text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1).strip())
                except (json.JSONDecodeError, TypeError):
                    data = None
            # Layer 3: first {..} in text
            if data is None:
                m2 = re.search(r"\{.*\}", raw_text, re.DOTALL)
                if m2:
                    try:
                        data = json.loads(m2.group(0))
                    except (json.JSONDecodeError, TypeError):
                        data = None

        if data is not None and isinstance(data, dict):
            validated: ImageDescription = ImageDescription.model_validate(data)
            return validated

        logger.warning(
            "Failed to parse Vision API response for %s: %s",
            image_path,
            raw_text[:200],
        )
        return ImageDescription(
            summary="[解析失败]",
            description=f"Failed to parse model response: {raw_text[:200]}",
            image_type="other",
        )
    except Exception as exc:
        logger.error("Vision API analysis failed for %s: %s", image_path, exc)
        return ImageDescription(
            summary="[分析失败]",
            description=f"Vision API analysis error: {exc}",
            image_type="other",
        )


async def _analyze_via_mcp(image_path: str) -> ImageDescription:
    """Analyze image using MCP analyze_image tool via HTTP endpoint.

    Sends the image path and prompt to the MCP server endpoint,
    parses the response into ImageDescription.

    If MCP endpoint is unreachable or returns an error, returns a placeholder
    with the error information.
    """
    endpoint = _get_mcp_endpoint()

    # Read and encode image
    try:
        with open(image_path, "rb") as f:
            image_data = f.read()
        b64_data = base64.b64encode(image_data).decode("ascii")
    except OSError as exc:
        logger.error("Failed to read image %s for MCP: %s", image_path, exc)
        return ImageDescription(
            summary="[图片读取失败]",
            description=f"Failed to read image file: {exc}",
            image_type="other",
        )

    mime_type = mimetypes.guess_type(image_path)[0] or "image/png"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                endpoint,
                json={
                    "image_source": f"data:{mime_type};base64,{b64_data}",
                    "prompt": IMAGE_ANALYSIS_PROMPT,
                },
            )
            response.raise_for_status()
            result_data = response.json()

            # A008: Initialize data before extraction
            data: Any = None
            if isinstance(result_data, dict):
                # Try direct field extraction
                if "summary" in result_data:
                    data = result_data
                elif "content" in result_data:
                    # Response might wrap result in 'content' field
                    inner = result_data["content"]
                    if isinstance(inner, dict):
                        data = inner
                    elif isinstance(inner, str):
                        try:
                            data = json.loads(inner)
                        except (json.JSONDecodeError, TypeError):
                            data = None

            if data is not None and isinstance(data, dict):
                return ImageDescription.model_validate(data)

            logger.warning(
                "MCP response missing expected fields: %s",
                str(result_data)[:300],
            )
            return ImageDescription(
                summary="[MCP 响应格式错误]",
                description=f"Unexpected MCP response format: {str(result_data)[:200]}",
                image_type="other",
            )

    except httpx.HTTPError as exc:
        logger.warning("MCP HTTP request failed for %s: %s", image_path, exc)
        return ImageDescription(
            summary="[MCP 请求失败]",
            description=f"MCP server unreachable: {exc}",
            image_type="other",
        )
    except Exception as exc:
        logger.error("MCP analysis failed for %s: %s", image_path, exc)
        return ImageDescription(
            summary="[MCP 分析失败]",
            description=f"MCP analysis error: {exc}",
            image_type="other",
        )


def _create_placeholder_description(image_path: str) -> ImageDescription:
    """Create a placeholder description when no analysis backend is configured.

    This preserves backward compatibility — the image URL is still available
    for display, but no AI-generated description is produced.
    """
    path = Path(image_path)
    return ImageDescription(
        summary=f"[图片: {path.name}]",
        description="(图片分析未启用 — 未配置 VISION_MODEL 或 MCP_IMAGE_ANALYSIS)",
        image_type="other",
    )


# ============================================================
# Batch image analysis
# ============================================================


async def analyze_images_batch(
    messages: list[dict],
    max_concurrency: int | None = None,
) -> dict[str, str]:
    """Batch analyze images from chat messages with concurrency control.

    Filters only image-type messages that have a media_local_path, then
    analyzes each image concurrently using asyncio.Semaphore for rate limiting.

    Each image is analyzed independently — a single failure does not crash
    the batch. Failed images get an error description string.

    Args:
        messages: List of message dicts, each containing:
                  - server_id: unique message identifier
                  - msg_type: message type (must be "image" for analysis)
                  - media_local_path: local file path to the image
        max_concurrency: Maximum concurrent Vision API/MCP calls.
                         None (default) resolves to Settings.image_max_concurrency
                         (env IMAGE_MAX_CONCURRENCY / WINNOW_IMAGE_MAX_CONCURRENCY).
                         An explicit int is used verbatim and env is not consulted
                         (P009: explicit argument precedence).

    Returns:
        {server_id: formatted_description_text} mapping.
        Empty dict if no image messages are found.
    """
    # Filter: messages with a local file path that are either:
    #   - explicitly msg_type="image", or
    #   - msg_type="text" with content="[图片]" (CipherTalk API marks images as text), or
    #   - msg_type="emoji" with media file (sticker images need Vision API for emotion)
    image_messages = [
        m
        for m in messages
        if m.get("media_local_path")
        and (
            m.get("msg_type") == "image"
            or (m.get("msg_type") == "text" and m.get("content", "").strip() == "[图片]")
            or m.get("msg_type") == "emoji"
        )
    ]

    if not image_messages:
        logger.debug("No image messages with media_local_path found in batch")
        return {}

    # Resolve concurrency from Settings only when the caller omits it (None sentinel).
    # An explicit int argument takes precedence over env (P009).
    if max_concurrency is None:
        max_concurrency = _get_max_concurrency()

    logger.info(
        "Starting batch image analysis: %d images, max_concurrency=%d",
        len(image_messages),
        max_concurrency,
    )

    semaphore = asyncio.Semaphore(max_concurrency)

    async def analyze_one(msg: dict) -> tuple[str, str]:
        """Analyze a single image message with concurrency control.

        Exception isolation: any failure returns an error description
        rather than propagating the exception.
        """
        async with semaphore:
            server_id = msg.get("server_id", "")
            image_path = msg.get("media_local_path", "")

            try:
                desc = await analyze_single_image(image_path)
                text = desc.format_description()
                logger.debug("Image analyzed: %s -> %s", server_id, desc.summary)
                return (server_id, text)
            except Exception as exc:
                logger.warning(
                    "Image analysis failed for server_id=%s, path=%s: %s",
                    server_id,
                    image_path,
                    exc,
                )
                return (server_id, f"[图片分析失败: {str(exc)[:100]}]")

    # Launch all analysis tasks concurrently, controlled by semaphore
    tasks = [analyze_one(msg) for msg in image_messages]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Build result dict, handling any unexpected gather-level exceptions
    output: dict[str, str] = {}
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            sid = image_messages[i].get("server_id", "")
            logger.error(
                "Unexpected gather exception for server_id=%s: %s",
                sid,
                result,
            )
            output[sid] = f"[图片分析异常: {str(result)[:100]}]"
        elif isinstance(result, tuple) and len(result) == 2:
            sid, text = result
            output[sid] = text
        else:
            sid = image_messages[i].get("server_id", "")
            output[sid] = "[图片分析失败: 未知错误]"

    logger.info(
        "Batch image analysis complete: %d/%d succeeded",
        len([v for v in output.values() if "失败" not in v and "异常" not in v]),
        len(image_messages),
    )

    return output
