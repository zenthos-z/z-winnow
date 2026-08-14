"""T-V2-1: AppMsg XML 解析器 — 解析微信 appmsg (type 49) 分享消息.

将 appmsg XML 解析为结构化 AppMsgInfo，支持 7 种子类型 (article / file /
sticker / miniprogram / music / transfer / redpacket_cover) 和多种边缘情况。

设计原则 (P007 分层解析+回退):
  1. 尝试直接 XML 解析
  2. JSON 转义解码后重试
  3. 所有异常捕获 → 返回 None 走回退逻辑

技术约束:
  - xml.etree.ElementTree 标准库 (零依赖)
  - CDATA 由 ElementTree 自动剥离
  - Malformed XML → ET.ParseError → 返回 None
  - 空标题 → 从 URL 推断占位符
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# A007: Use structlog (logger), not print with emoji

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

APPMsg_CODE: int = 49

APPMsg_TYPE_MAP: dict[int, str] = {
    5: "article",
    6: "file",
    8: "sticker",
    33: "miniprogram",
    36: "music",
    2000: "transfer",
    2001: "redpacket_cover",
}

# 原始 XML 截断字符数 (防止超长 XML 占用过多内存/日志)
RAW_XML_MAX_CHARS: int = 4096

# 标题占位符模板 (空标题时使用)
FALLBACK_TITLE_PREFIX: str = "[链接分享]"

# ============================================================
# AppMsgInfo dataclass
# ============================================================


@dataclass
class AppMsgInfo:
    """结构化 appmsg 解析结果.

    Fields:
        msg_type: 子类型 — "article" | "miniprogram" | "music" | "file" |
                  "sticker" | "transfer" | "redpacket_cover" | "unknown"
        title: 消息标题 (可能为空)
        description: 消息摘要/描述
        url: 分享链接
        source: 来源名称 (公众号/小程序名称)
        raw_xml: 原始 XML 保留用于调试 (截断至 4096 字符)
    """

    msg_type: str = "unknown"
    title: str = ""
    description: str = ""
    url: str = ""
    source: str = ""
    raw_xml: str = ""
    file_size: int = 0  # bytes, from <appattach><totallen> for file-type appmsg


# ============================================================
# Formatting helpers
# ============================================================

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def _format_file_size(size_bytes: int) -> str:
    """Format byte count to human-readable size string.

    Reuses the same logic as xml_parsers._format_file_size to avoid cross-module import.
    """
    if size_bytes <= 0:
        return "0B"
    size: float = float(size_bytes)
    unit_idx: int = 0
    while size >= 1024 and unit_idx < len(_SIZE_UNITS) - 1:
        size /= 1024
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(size)}{_SIZE_UNITS[unit_idx]}"
    return f"{size:.1f}{_SIZE_UNITS[unit_idx]}"


# ============================================================
# XML parsing helpers
# ============================================================


def _resolve_subtype(
    type_text: str | None,
    fallback_code: int | None = None,
) -> str:
    """将 XML 中的 type 元素文本映射为子类型名称.

    Args:
        type_text: <type> 元素的文本内容 (如 "5", "33")
        fallback_code: 备用的 msg_type_code (当前未使用，为扩展保留)

    Returns:
        子类型名称 (如 "article"), 未知则为 "unknown"
    """
    if type_text is None:
        # 尝试从 fallback_code 获取
        code = fallback_code if fallback_code is not None else 0
        return APPMsg_TYPE_MAP.get(code, "unknown")

    try:
        code = int(type_text.strip())
    except (ValueError, TypeError):
        return "unknown"

    return APPMsg_TYPE_MAP.get(code, "unknown")


def _extract_text(element: ET.Element | None, tag: str) -> str:
    """从 Element 中提取指定标签的文本，安全处理缺失.

    ElementTree 自动剥离 CDATA 标记，findtext() 返回纯文本.

    Args:
        element: 父 XML 元素
        tag: 子标签名称

    Returns:
        标签文本或空字符串 (元素/标签缺失时)
    """
    if element is None:
        return ""
    child = element.find(tag)
    if child is None:
        return ""
    text = child.text
    return text.strip() if text else ""


def _infer_title_from_url(url: str) -> str:
    """从 URL 推断标题占位符.

    Args:
        url: 分享链接 URL

    Returns:
        从 URL 中提取的简化标题, 或通用占位符
    """
    if not url:
        return FALLBACK_TITLE_PREFIX

    # 尝试从 URL 路径提取最后一段作为标题线索
    try:
        # 移除协议和查询参数
        clean = re.sub(r"^https?://", "", url)
        clean = re.sub(r"\?.*$", "", clean)
        clean = re.sub(r"#.*$", "", clean)
        # 取路径最后一段 (排除域名本身)
        parts = [p for p in clean.split("/") if p]
        # 如果只有一个部分 (即纯域名), 无法推断标题
        if len(parts) <= 1:
            return FALLBACK_TITLE_PREFIX
        # parts[0] 是域名, parts[-1] 是最后路径段
        last = parts[-1]
        # 如果最后一段看起来像文件名，去掉扩展名
        last = re.sub(r"\.[^.]+$", "", last)
        if len(last) > 2:
            return f"{FALLBACK_TITLE_PREFIX}: {last[:80]}"
    except Exception:
        pass

    return FALLBACK_TITLE_PREFIX


def _decode_json_escaped(content: str) -> str | None:
    """尝试解码 JSON 转义的内容.

    微信 SDK 可能将 XML 字符串 JSON-转义后存储.
    例如: "\\<msg>\\<appmsg>..." → "<msg><appmsg>..."

    Args:
        content: 疑似被 JSON 转义的原始内容

    Returns:
        解码后的字符串, 或 None (解码失败时)
    """
    if not content:
        return None

    # 检查是否包含 JSON 转义特征 (\\u, \\n, \\", 或 \\<)
    if any(pattern in content for pattern in ('\\"', "\\n", "\\u", "\\<")):
        # 尝试作为 JSON 字符串解码
        try:
            decoded = json.loads(f'"{content}"')
            if isinstance(decoded, str) and len(decoded) > 0:
                logger.debug("Successfully decoded JSON-escaped content")
                return decoded
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 也尝试直接 json.loads (无需包装引号)
            try:
                decoded = json.loads(content)
                if isinstance(decoded, str) and len(decoded) > 0:
                    logger.debug("Successfully decoded JSON-escaped content (direct)")
                    return decoded
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

    # 最后尝试: 如果内容被完整包装成 JSON 字符串 (整个以 "\"" 开头)
    if content.startswith('"') and content.endswith('"'):
        try:
            decoded = json.loads(content)
            if isinstance(decoded, str) and len(decoded) > 0:
                return decoded
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return None


# ============================================================
# Main parsing functions
# ============================================================


def try_parse_appmsg(
    content: str,
    msg_type_code: int = APPMsg_CODE,
) -> AppMsgInfo | None:
    """解析 appmsg XML content 为结构化 AppMsgInfo.

    核心解析逻辑: 使用 xml.etree.ElementTree 解析 XML,
    提取 title/des/url/source 字段.

    XML 结构示例:
        <msg>
          <appmsg appid="" sdkver="0">
            <title><![CDATA[标题文本]]></title>
            <des><![CDATA[描述文本]]></des>
            <url><![CDATA[https://...]]></url>
            <type>5</type>
            <source>
              <name>来源名称</name>
            </source>
          </appmsg>
          ...
        </msg>

    Args:
        content: 原始 XML 字符串
        msg_type_code: 消息类型码 (默认 49=appmsg)

    Returns:
        AppMsgInfo 或 None (如果内容为空/非 XML/解析失败)
    """
    if not content or not isinstance(content, str):
        logger.debug("try_parse_appmsg: empty or non-string content")
        return None

    content = content.strip()
    if not content:
        return None

    # 快速检查: 内容是否以 XML 标签开头
    # CipherTalk rawContent 常带 sender 前缀 (如 "wxid_xxx:\n<msg>..."),
    # 去掉前缀后用 XML 主体部分继续解析.
    if not content.startswith("<"):
        # 尝试在换行或冒号后定位第一个 '<' — sender 前缀形如
        #   wxid_xxx:\n<msg>...
        #   25984983148809361@openim:\n<msg>...
        _lt = content.find("<")
        if _lt > 0:
            content = content[_lt:]
        else:
            logger.debug("try_parse_appmsg: content does not start with '<'")
            return None

    # Truncate raw_xml for storage (avoid oversized debug data)
    raw_xml = content[:RAW_XML_MAX_CHARS] if len(content) > RAW_XML_MAX_CHARS else content

    # P007: Primary parsing path — direct XML parse
    root: ET.Element | None = None
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.debug("XML parse error: %s", exc)
        return None

    if root is None:
        return None

    # Navigate to <appmsg> element (may be nested inside <msg>)
    appmsg = root.find("appmsg")
    if appmsg is None:
        # Try: root itself might be <appmsg>
        if root.tag == "appmsg":
            appmsg = root
        else:
            logger.debug("try_parse_appmsg: no <appmsg> element found")
            return None

    # Extract fields — ElementTree auto-strips CDATA markers
    title = _extract_text(appmsg, "title")
    description = _extract_text(appmsg, "des")
    url = _extract_text(appmsg, "url")

    # Extract source from <source><name>...</name></source>
    source = ""
    source_el = appmsg.find("source")
    if source_el is not None:
        source = _extract_text(source_el, "name")

    # Determine subtype from <type> element
    type_text = _extract_text(appmsg, "type")
    subtype = _resolve_subtype(type_text if type_text else None)

    # Extract file size + download URL from <appattach> (for file-type appmsg, type=6)
    file_size = 0
    appattach = appmsg.find("appattach")
    if appattach is not None:
        totallen_text = _extract_text(appattach, "totallen")
        if totallen_text:
            try:
                file_size = int(totallen_text)
            except (ValueError, TypeError):
                file_size = 0
        # WeWork file messages (type=6) put the download URL in <appattach><tpurl>
        # rather than the top-level <url> element.  Prefer tpurl for file subtype.
        tpurl = _extract_text(appattach, "tpurl")
        if tpurl and subtype == "file" and not url:
            url = tpurl

    # Edge case: empty title → infer from URL
    if not title:
        title = _infer_title_from_url(url)

    return AppMsgInfo(
        msg_type=subtype,
        title=title,
        description=description,
        url=url,
        source=source,
        raw_xml=raw_xml,
        file_size=file_size,
    )


def try_parse_appmsg_safe(
    content: str,
    msg_type_code: int = APPMsg_CODE,
) -> AppMsgInfo | None:
    """安全解析 appmsg content, 含所有边缘情况处理.

    分层策略 (P007):
      1. 直接 XML 解析 (try_parse_appmsg)
      2. JSON 转义解码 → XML 解析
      3. 所有异常捕获 → None

    此函数保证 NEVER 抛出异常, 所有失败情况返回 None.

    Args:
        content: 原始消息内容 (可能是 XML / JSON 转义 / 纯文本)
        msg_type_code: 消息类型码 (默认 49=appmsg)

    Returns:
        AppMsgInfo 或 None (解析失败时)
    """
    # A008: Initialize result to None before extraction chain
    result: AppMsgInfo | None = None

    # Early return for None / non-string content
    if not content or not isinstance(content, str):
        return None

    # Layer 1: direct XML parse
    try:
        result = try_parse_appmsg(content, msg_type_code)
        if result is not None:
            return result
    except Exception:
        # try_parse_appmsg should catch ParseError internally, but guard anyway
        logger.debug("Layer 1 (direct XML) failed, attempting JSON decode layer")

    # Layer 2: JSON-escaped content → decode → retry
    try:
        decoded = _decode_json_escaped(content)
        if decoded is not None:
            result = try_parse_appmsg(decoded, msg_type_code)
            if result is not None:
                logger.debug("Layer 2 (JSON-escaped) succeeded")
                return result
    except Exception as exc:
        logger.debug("Layer 2 (JSON decode) failed: %s", exc)

    # Layer 3: exhausted strategies → None
    logger.debug("All parsing layers exhausted for appmsg content[:200]=%s", content[:200])
    return None


def format_appmsg(info: AppMsgInfo) -> str:
    """将 AppMsgInfo 格式化为人类可读文本.

    输出格式: [分享{类型}] 标题：{title} | 摘要：{desc} | 来源：{source} | 链接：{url}

    对齐验收标准 #6: appmsg 消息在子 agent 上下文中显示为
    `[分享文章] 标题：xxx | 摘要：xxx | 来源：xxx | 链接：xxx`

    Args:
        info: 解析后的 AppMsgInfo

    Returns:
        格式化的可读文本
    """
    type_labels: dict[str, str] = {
        "article": "文章",
        "file": "文件",
        "sticker": "表情",
        "miniprogram": "小程序",
        "music": "音乐",
        "transfer": "转账",
        "redpacket_cover": "红包封面",
    }

    label = type_labels.get(info.msg_type, "分享")

    parts: list[str] = [f"[分享{label}]"]

    if info.title:
        parts.append(f"标题：{info.title}")

    if info.description:
        parts.append(f"摘要：{info.description}")

    # File size for file-type appmsg (from <appattach><totallen>)
    if info.file_size > 0 and info.msg_type == "file":
        parts.append(f"大小：{_format_file_size(info.file_size)}")

    if info.source:
        parts.append(f"来源：{info.source}")

    if info.url:
        parts.append(f"链接：{info.url}")

    return " | ".join(parts)
