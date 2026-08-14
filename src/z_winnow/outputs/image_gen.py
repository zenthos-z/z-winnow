"""日报配图独立生图工具链 (#9.2) —— 复刻 winnow 技能验证方案。

三解耦部件（来自验证过的 winnow Code Skill + quick-img）：
  1. 提炼方法论 ``templates/image_gen/日报配图提炼.md`` —— LLM 按「认知差过滤→脱水」
     把 daily.json 精炼成 4 段结构（【主题】【核心议题与观点】【关键数据与概念】【活跃成员】）。
  2. 生图模板 ``templates/image_gen/生图模板.txt`` —— 包装提炼内容 + 内嵌基础信息图风格。
  3. 日报风格 ``templates/image_gen/日报生图风格.md`` —— 日报专属增量风格，追加到 prompt 末尾。

最终 prompt = ``生图模板(提炼内容)`` + ``\\n\\n`` + ``日报生图风格.md``。

DMX API 契约（原生 Gemini generateContent 形态，**非 OpenAI**；quick-img 验证过）：
  ``POST {base_url}/v1beta/models/{model}:generateContent``
  Headers ``x-goog-api-key: <DMX_API_KEY>`` (Gemini 原生鉴权，非 Bearer)
  Body ``{"contents":[{"parts":[{"text":<prompt>}]}],
         "generationConfig":{"responseModalities":["IMAGE"],"imageConfig":{"aspectRatio":..,"imageSize":..}}}``
  Response ``candidates[0].content.parts[].inlineData.data`` (base64)

设计约束（用户明确）：
  - 提炼**只用 LLM**，无规则兜底；LLM 未配置/失败 → 抛 ``ImageGenError``。
  - **无 mock 模式兜底**；测试通过 ``_transport`` 注入 + monkeypatch 守机制，不作功能验证依据。
  - DMX 调用按验证方案实装；出图效果/prompt 质量迭代不在本模块范围。
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 模板目录：src/z_winnow/templates/image_gen/
_IMAGE_GEN_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "image_gen"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ImageGenError(RuntimeError):
    """生图过程中的运行时错误（LLM 调用失败、DMX 响应异常等）。"""


class ImageGenConfigError(ImageGenError):
    """生图配置错误（如 DMX API key 未配置）。"""


# ---------------------------------------------------------------------------
# 模板加载（纯文本读取，不走 Jinja2 —— 模板里的 {{content}} 是 mustache 占位）
# ---------------------------------------------------------------------------


def _load_template(name: str) -> str:
    """读取 ``templates/image_gen/{name}`` 全文（verbatim，不改措辞）。"""
    path = _IMAGE_GEN_TEMPLATES_DIR / name
    if not path.is_file():
        raise ImageGenConfigError(f"生图模板缺失: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 提炼（纯 LLM）
# ---------------------------------------------------------------------------


def _daily_to_text(daily_data: dict[str, Any]) -> str:
    """把结构化 daily.json 渲染成可读文本，喂给提炼 LLM。"""
    parts: list[str] = []

    overview = daily_data.get("overview") or ""
    if overview:
        parts.append(f"# 日报概览\n{overview}")

    topics = daily_data.get("topics") or daily_data.get("topic_sections") or []
    topic_list = [t for t in topics if isinstance(t, dict)]
    if topic_list:
        # 按权重降序，让 LLM 优先看到高价值议题
        ordered = sorted(topic_list, key=lambda t: float(t.get("weight") or 0.0), reverse=True)
        lines = ["# 议题"]
        for t in ordered:
            name = t.get("topic_name") or t.get("name") or ""
            conclusion = t.get("conclusion") or ""
            lifecycle = t.get("lifecycle") or ""
            tag = f"[{lifecycle}] " if lifecycle else ""
            line = f"- {tag}{name}"
            if conclusion:
                line += f"：{conclusion}"
            lines.append(line)
        parts.append("\n".join(lines))

    highlights = daily_data.get("highlights") or []
    if highlights:
        parts.append("# 亮点/金句\n" + "\n".join(f"- {h}" for h in highlights))

    trend_summary = daily_data.get("trend_summary") or ""
    if trend_summary:
        parts.append(f"# 趋势\n{trend_summary}")
    else:
        trend_analysis = daily_data.get("trend_analysis")
        if isinstance(trend_analysis, str) and trend_analysis:
            parts.append(f"# 趋势\n{trend_analysis}")

    # 活跃成员：从议题 participants 聚合（保留出现顺序、去重）
    seen: set[str] = set()
    members: list[str] = []
    for t in topic_list:
        for p in t.get("participants") or []:
            if isinstance(p, str) and p and p not in seen:
                seen.add(p)
                members.append(p)
    if members:
        parts.append("# 活跃成员\n" + "、".join(members))

    return "\n\n".join(parts) if parts else json.dumps(daily_data, ensure_ascii=False)


async def distill_daily_content(daily_data: dict[str, Any]) -> str:
    """用 LLM 按「日报配图提炼.md」方法论精炼日报 → 4 段结构文本。

    无规则兜底、无 mock 兜底：LLM 未配置或调用失败 → 抛 ``ImageGenError``。
    复用 ``create_model_for_subagent("unified-reporter")``（已配置的文本 LLM）。
    """
    from z_winnow.config.models import create_model_for_subagent

    try:
        model = create_model_for_subagent("unified-reporter", temperature=0.3, max_tokens=1024)
    except Exception as exc:  # 未配置 key / provider 不可用
        raise ImageGenError(f"提炼模型初始化失败（检查 LLM 配置）: {exc}") from exc

    template = _load_template("日报配图提炼.md")
    daily_text = _daily_to_text(daily_data)
    prompt = template.replace("{{content}}", daily_text)

    try:
        result = await model.ainvoke(prompt)
    except Exception as exc:
        raise ImageGenError(f"提炼 LLM 调用失败: {exc}") from exc

    content = getattr(result, "content", None)
    if not content:
        content = str(result)
    if not isinstance(content, str) or not content.strip():
        raise ImageGenError("提炼 LLM 返回空内容")
    return content.strip()


# ---------------------------------------------------------------------------
# Prompt 组装
# ---------------------------------------------------------------------------


def build_image_prompt(content: str) -> str:
    """组装最终生图 prompt = 生图模板(提炼内容) + '\\n\\n' + 日报生图风格.md。

    ``生图模板.txt`` 含 ``{{content}}`` 占位（mustache 风格，用 replace，不走 Jinja2）。
    """
    wrapper = _load_template("生图模板.txt")
    base = wrapper.replace("{{content}}", content)
    style = _load_template("日报生图风格.md")
    return f"{base}\n\n{style}"


# ---------------------------------------------------------------------------
# DMX 调用（原生 Gemini generateContent，httpx async）
# ---------------------------------------------------------------------------


async def call_dmx_api(
    prompt: str,
    *,
    ratio: str = "4:5",
    size: str = "2K",
    _transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """调 DMX Gemini 生图，返回解码后的图片 bytes。

    原生 Gemini generateContent 形态（quick-img 验证过）：``x-goog-api-key`` 鉴权，
    响应取 ``candidates[0].content.parts[].inlineData.data`` (base64)。

    ``_transport`` 仅测试注入（MockTransport），正常路径为 None 走真实 httpx。
    """
    from z_winnow.config.settings import get_settings

    settings = get_settings()
    api_key = settings.quick_img_api_key
    if not api_key:
        raise ImageGenConfigError(
            "QUICK_IMG_API_KEY 未配置。设置 WINNOW_QUICK_IMG_API_KEY / DMX_API_KEY 后重试。"
        )

    base_url = settings.quick_img_base_url.rstrip("/")
    model = settings.quick_img_model
    url = f"{base_url}/v1beta/models/{model}:generateContent"

    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    image_config: dict[str, Any] = {"aspectRatio": ratio}
    if size != "1K":
        image_config["imageSize"] = size
    body: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": image_config,
        },
    }

    client_kwargs: dict[str, Any] = {"timeout": settings.image_gen_timeout}
    if _transport is not None:
        client_kwargs["transport"] = _transport

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise ImageGenError(f"DMX 网络请求失败: {exc}") from exc

    if resp.status_code != 200:
        raise ImageGenError(f"DMX API 返回 {resp.status_code}: {resp.text[:300]}")

    try:
        result = resp.json()
        parts = result["candidates"][0]["content"]["parts"]
        for part in parts:
            if isinstance(part, dict) and "inlineData" in part:
                return base64.b64decode(part["inlineData"]["data"])
    except (KeyError, IndexError, ValueError) as exc:
        raise ImageGenError(f"DMX 响应解析失败: {exc}; body={str(result)[:300]}") from exc

    raise ImageGenError(f"DMX 响应未含图片数据; body={str(result)[:300]}")


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件，失败抛 FileNotFoundError / ValueError（不静默吞错）。"""
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise ValueError(f"JSON 文件为空: {path}")
    data: dict[str, Any] = json.loads(raw)
    return data


async def generate_cover(
    group_id: str,
    date: str,
    *,
    count: int | None = None,
    ratio: str | None = None,
    size: str | None = None,
    dry_run: bool = False,
    _transport: httpx.AsyncBaseTransport | None = None,
) -> list[Path]:
    """生成日报配图并落盘。

    流程：读 L3 ``daily.json`` → LLM 提炼 → 组装 prompt → 写 ``cover.prompt.txt``
    →（``dry_run`` 则到此为止）→ 调 DMX 生图 → 写 ``cover.png``（多张则 ``cover_01.png``...）。

    落盘目录：``{layer3_output_dir}/{group_id}/{date}/``（与 ``report.md``/``attachments/`` 同级）。

    Args:
        group_id: 群组 ID。
        date: 日期 YYYYMMDD。
        count: 生成张数（None 取 ``settings.image_gen_count``）。
        ratio: 宽高比（None 取 ``settings.image_gen_ratio``）。
        size: 分辨率（None 取 ``settings.image_gen_size``）。
        dry_run: True 只组装 prompt 落 ``.prompt.txt``，不调 DMX（诊断用）。
        _transport: 测试注入。

    Returns:
        落盘路径列表（dry_run 返回 ``[cover.prompt.txt]``；否则返回图片路径）。

    Raises:
        FileNotFoundError: L3 daily.json 不存在。
        ImageGenError / ImageGenConfigError: 提炼或 DMX 失败。
    """
    from z_winnow.config.settings import get_settings

    settings = get_settings()
    ratio = ratio or settings.image_gen_ratio
    size = size or settings.image_gen_size
    count = count or settings.image_gen_count
    count = max(1, int(count))

    from z_winnow.pipeline.l3_paths import resolve_l3_dir

    l3_dir = resolve_l3_dir(settings.layer3_output_dir, group_id, date)
    daily_path = l3_dir / "daily.json"
    if not daily_path.is_file():
        raise FileNotFoundError(
            f"L3 daily.json 不存在: {daily_path}（先跑流水线生成当日 L3，或检查 group/date）"
        )
    daily_data = _read_json(daily_path)
    l3_dir.mkdir(parents=True, exist_ok=True)

    # 1. 提炼（真 LLM）
    logger.info("image_gen: 提炼日报内容 (group=%s date=%s) ...", group_id, date)
    content = await distill_daily_content(daily_data)

    # 2. 组装 prompt
    prompt = build_image_prompt(content)
    prompt_path = l3_dir / "cover.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    logger.info("image_gen: prompt 落盘 %s (%d 字符)", prompt_path, len(prompt))

    if dry_run:
        logger.info("image_gen: dry-run，跳过 DMX 调用")
        return [prompt_path]

    # 3. 调 DMX 生图（真 API）
    logger.info("image_gen: 调 DMX 生图 (ratio=%s size=%s count=%d) ...", ratio, size, count)
    paths: list[Path] = []
    for i in range(1, count + 1):
        image_bytes = await call_dmx_api(prompt, ratio=ratio, size=size, _transport=_transport)
        fname = "cover.png" if count == 1 else f"cover_{i:02d}.png"
        out_path = l3_dir / fname
        out_path.write_bytes(image_bytes)
        paths.append(out_path)
        logger.info("image_gen: 配图落盘 %s (%d bytes)", out_path, len(image_bytes))
    return paths


__all__ = [
    "ImageGenConfigError",
    "ImageGenError",
    "build_image_prompt",
    "call_dmx_api",
    "distill_daily_content",
    "generate_cover",
]
