"""T-W7-2: RawContent XML 解析器扩展 — parse_quote / parse_file / parse_link.

基于 rawContent 字段，实现专用 XML 解析器处理不同消息类型的 XML 内容：
引用消息 (parse_quote)、文件消息 (parse_file)、链接分享 (parse_link)、
表情消息 (parse_emoji)、小程序 (parse_weapp)、位置消息 (parse_location)。

设计原则 (P014 安全解析器架构):
  1. 内层核心解析 (parse_quote/parse_file/parse_link/parse_emoji/parse_weapp/parse_location)
  2. 外层分发包装 (parse_raw_content)
  3. NEVER 抛异常 — 所有 malformed XML 返回原始 raw_content

技术约束:
  - xml.etree.ElementTree 标准库 (零依赖)
  - CDATA 由 ElementTree 自动剥离 (L016)
  - ET.ParseError 精确捕获 (不用泛化 Exception)
  - A008: 所有 XML 元素提取前预初始化为 None/空字符串
"""

from __future__ import annotations

import contextlib
import logging
import re
import xml.etree.ElementTree as ET

# A007: Use structlog (logger), not print with emoji
logger = logging.getLogger(__name__)

# ============================================================
# 常量
# ============================================================

# 消息类型码 → 解析器路由 (P016: 类型映射先行)
QUOTE_MSG_TYPE: int = 25  # reply/引用消息
FILE_MSG_TYPE: int = 4  # file/文件消息
LINK_MSG_TYPE: int = 7  # link/链接分享

# refermsg <type> → 可读标签 (非文本类型用占位符代替原始 content)
_REFERMSG_TYPE_LABELS: dict[int, str] = {
    3: "[图片]",
    34: "[语音]",
    43: "[视频]",
    47: "[表情]",
    49: "[应用消息]",
}

# 文件大小单位
SIZE_UNITS: list[str] = ["B", "KB", "MB", "GB"]


# ============================================================
# 内部辅助函数
# ============================================================


def _extract_text(element: ET.Element | None, tag: str) -> str:
    """从 Element 中提取指定标签的文本，安全处理缺失。

    ElementTree 自动剥离 CDATA 标记, findtext() 返回纯文本 (L016).

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


def _format_file_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读的文件大小。

    Args:
        size_bytes: 文件大小 (字节)

    Returns:
        格式化字符串，如 "1024.0KB", "1.5MB"
    """
    if size_bytes <= 0:
        return "0B"

    size: float = float(size_bytes)
    unit_idx: int = 0
    while size >= 1024 and unit_idx < len(SIZE_UNITS) - 1:
        size /= 1024
        unit_idx += 1

    if unit_idx == 0:
        return f"{int(size)}{SIZE_UNITS[unit_idx]}"
    else:
        return f"{size:.1f}{SIZE_UNITS[unit_idx]}"


# ============================================================
# 核心解析器 (P014: 内层)
# ============================================================


def sanitize_reply_content(content: str) -> str:
    """清理 reply 消息 content 字段中的 XML 元数据，仅提取用户回复文本。

    Reply 消息的 content 格式（两种变体）:
      原始 XML:   "用户文本 view 57 0 ... <?xml><msg>...</msg> 尾部字段"
      HTML 转义:  "用户文本 view 57 0 ... &lt;msgsource&gt; ... 尾部字段"

    提取策略:
      按 " view <N> " 分割，取用户文本部分。
      不再提取 <title> 作为"被引用摘要" — <title> 是回复者文本，不是被引用内容。
      被引用内容由 parse_quote() 从 rawContent XML 的 <refermsg> 中提取。
    """
    if not content:
        return content

    # 提取用户文本（metadata/XML 之前）
    # reply content 格式变体:
    #   "用户文本 view 57 0 0 0 0 49 ..."  — 带 view 前缀
    #   "用户文本 57 0 0 0 0 ..."            — 不带 view 前缀
    user_text = content
    parts = re.split(r"\s+(?:view\s+)?\d+\s+0\s+0\s+0\s+", content, maxsplit=1)
    if len(parts) > 1 and parts[0].strip():
        user_text = parts[0].strip()
    else:
        # Fallback: split on any XML-like marker (raw or HTML-escaped)
        for marker in ("<?xml", "<msg>", "<msgsource>", "&lt;msg", "&lt;?xml"):
            if marker in content:
                text = content.split(marker)[0].strip()
                if text:
                    user_text = text
                    break

    return user_text


def _has_reply_xml_noise(text: str) -> bool:
    """检测 content 中是否包含原始或 HTML 转义的 XML 元数据。"""
    if not text:
        return False
    return bool(
        "<?xml" in text
        or "<msg>" in text
        or "<msgsource>" in text
        or "<appmsg" in text
        or "<sec_msg_node>" in text
        or "&lt;msg" in text
        or "&lt;msgsource" in text
        or "&lt;appmsg" in text
        or "&lt;sec_msg_node" in text
    )


def _clean_refermsg_content(content: str) -> str:
    """Clean <refermsg><content> to extract readable text.

    Refermsg content can be:
      1. Plain text (reply to simple text message)
      2. HTML-escaped XML (reply to appmsg/link message)
      3. Nested XML with its own <refermsg>
      4. Non-text metadata (base64 hash, trailing numbers — image/video replies)

    This helper extracts the human-readable portion.
    """
    if not content:
        return ""

    # Strip trailing numeric metadata: "0 0 0 0 0" or "1 2 3 4 5" at end
    content = re.sub(r"(?:\s+\d+){3,}$", "", content).strip()

    # Detect base64-encoded hash data (e.g. {"phash":...,"pdqHash":...})
    # base64 of JSON objects starts with "eyJ" (base64 of '{"')
    if re.match(r"^eyJ[A-Za-z0-9+/]+=*$", content):
        return "[非文本内容]"

    # Handle HTML-escaped XML (common for replies to appmsg messages)
    if "&lt;" in content:
        import html as _html

        content = _html.unescape(content)

    # Try to extract <title> from XML content (e.g., quoted appmsg title)
    if content.strip().startswith("<"):
        title_match = re.search(
            r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
            content,
        )
        if title_match and title_match.group(1).strip():
            return title_match.group(1).strip()
        # No <title> — strip all XML tags
        content = _RE_XML_TAG.sub("", content).strip()

    # Handle nested <refermsg> — only expand one level
    if "<refermsg>" in content:
        content = content.split("<refermsg>")[0].strip()

    # Truncate overly long content
    if len(content) > 200:
        content = content[:200] + "..."

    return content


def parse_quote(raw_content: str) -> str:
    """解析 <refermsg> 引用消息，返回格式化文本。

    支持两种 XML 结构:
      1. 简单引用 (type=25):
         <msg><refermsg><displayname>...</><content>...</></></>
      2. appmsg 嵌套引用 (type=57):
         <msg><appmsg><title>用户文本</><type>57</>
           <refermsg><displayname>...</><svrid>...</><content>...</></></></></>

    输出格式: [引用 {displayname}(svrid:{svrid}): {quoted_content}]
    嵌套引用只展开一层 (避免递归爆炸)。

    Args:
        raw_content: 原始 XML 字符串 (rawContent 字段)

    Returns:
        格式化引用文本，或原始 raw_content (解析失败时)
    """
    # A008: 预初始化所有提取变量
    displayname: str = ""
    content: str = ""
    svrid: str = ""

    if not raw_content or not isinstance(raw_content, str):
        return raw_content if raw_content else ""

    raw_content = raw_content.strip()
    if not raw_content or not raw_content.startswith("<"):
        return raw_content

    root: ET.Element | None = None
    try:
        root = ET.fromstring(raw_content)
    except ET.ParseError as exc:
        logger.debug("parse_quote: XML parse error: %s", exc)
        return raw_content

    if root is None:
        return raw_content

    # 定位 <refermsg> — 支持多层嵌套:
    #   1. 直接子元素: <msg><refermsg>
    #   2. appmsg 内: <msg><appmsg><refermsg>
    #   3. 任意深度 (兜底)
    #   4. 根元素本身是 <refermsg>
    refermsg: ET.Element | None = root.find("refermsg")
    if refermsg is None:
        refermsg = root.find("appmsg/refermsg")
    if refermsg is None:
        refermsg = root.find(".//refermsg")
    if refermsg is None and root.tag == "refermsg":
        refermsg = root

    if refermsg is None:
        logger.debug("parse_quote: no <refermsg> element found")
        return raw_content

    # 提取字段 (CDATA 由 ElementTree 自动剥离 — L016)
    displayname = _extract_text(refermsg, "displayname")
    content = _extract_text(refermsg, "content")
    svrid = _extract_text(refermsg, "svrid")
    ref_type_raw = _extract_text(refermsg, "type")

    if not displayname and not content:
        return raw_content

    # If refermsg type indicates a non-text message, use type label as content
    ref_type_label: str | None = None
    if ref_type_raw:
        with contextlib.suppress(ValueError, TypeError):
            ref_type_label = _REFERMSG_TYPE_LABELS.get(int(ref_type_raw))

    content = ref_type_label if ref_type_label else _clean_refermsg_content(content)

    ref_label = displayname
    if svrid:
        ref_label = f"{displayname}(svrid:{svrid})" if displayname else f"svrid:{svrid}"

    return f"[引用 {ref_label}: {content}]"


def parse_file(raw_content: str) -> str:
    """解析 <fileupload> 文件消息，返回格式化文本（含文件存储地址）。

    XML 结构 (type=4 file 消息的 rawContent):
        <msg>
          <fileupload>
            <title><![CDATA[filename.ext]]></title>
            <length>1048576</length>
            <cdnattachurl><![CDATA[...]]></cdnattachurl>
            <filetype><![CDATA[pdf]]></filetype>
          </fileupload>
        </msg>

    输出格式: [文件: filename.ext (1.0MB) | 存储: cdn_url]
              如果无大小信息: [文件: filename.ext | 存储: cdn_url]
              如果无 CDN 地址: [文件: filename.ext (1.0MB)]

    Args:
        raw_content: 原始 XML 字符串 (rawContent 字段)

    Returns:
        格式化文件信息文本，或原始 raw_content (解析失败时)
    """
    # A008: 预初始化所有提取变量
    title: str = ""
    length_str: str = ""
    length_val: int = 0
    cdn_url: str = ""

    if not raw_content or not isinstance(raw_content, str):
        return raw_content if raw_content else ""

    raw_content = raw_content.strip()
    if not raw_content or not raw_content.startswith("<"):
        return raw_content

    root: ET.Element | None = None
    try:
        root = ET.fromstring(raw_content)
    except ET.ParseError as exc:
        logger.debug("parse_file: XML parse error: %s", exc)
        return raw_content

    if root is None:
        return raw_content

    # 定位 <fileupload> 元素
    fileupload: ET.Element | None = root.find("fileupload")
    if fileupload is None and root.tag == "fileupload":
        fileupload = root

    if fileupload is None:
        logger.debug("parse_file: no <fileupload> element found")
        return raw_content

    # 提取字段 (L016: CDATA 自动剥离)
    title = _extract_text(fileupload, "title")
    length_str = _extract_text(fileupload, "length")
    cdn_url = _extract_text(fileupload, "cdnattachurl")

    # 解析文件大小
    if length_str:
        try:
            length_val = int(length_str)
        except (ValueError, TypeError):
            length_val = 0

    if not title:
        return raw_content

    size_str: str = _format_file_size(length_val) if length_val > 0 else ""

    # 构建输出: 包含文件名 + 大小 + 存储地址
    parts: list[str] = [f"[文件: {title}"]
    if size_str:
        parts.append(f" ({size_str})")
    if cdn_url:
        parts.append(f" | 存储: {cdn_url}")
    parts.append("]")
    return "".join(parts)


def parse_link(raw_content: str) -> str:
    """解析链接分享 XML (type=7)，返回格式化文本。

    XML 结构 (type=7 link 消息的 rawContent):
        <msg>
          <appmsg>
            <title><![CDATA[链接标题]]></title>
            <des><![CDATA[链接摘要]]></des>
            <url><![CDATA[https://example.com]]></url>
            <type>5</type>
          </appmsg>
        </msg>

    输出格式: [链接: 标题](URL) - 摘要
    与已有的 try_parse_appmsg 互补: appmsg 覆盖 type=49 卡片, parse_link 覆盖 type=7 链接。

    Args:
        raw_content: 原始 XML 字符串 (rawContent 字段)

    Returns:
        格式化链接文本，或原始 raw_content (解析失败时)
    """
    # A008: 预初始化所有提取变量
    title: str = ""
    description: str = ""
    url: str = ""

    if not raw_content or not isinstance(raw_content, str):
        return raw_content if raw_content else ""

    raw_content = raw_content.strip()
    if not raw_content or not raw_content.startswith("<"):
        return raw_content

    root: ET.Element | None = None
    try:
        root = ET.fromstring(raw_content)
    except ET.ParseError as exc:
        logger.debug("parse_link: XML parse error: %s", exc)
        return raw_content

    if root is None:
        return raw_content

    # 尝试多种结构:
    # 1. <msg><appmsg><title>...</appmsg></msg>
    # 2. <appmsg><title>...</appmsg>
    # 3. <msg><title>... (根级别)

    # 尝试找 <appmsg> 容器
    appmsg: ET.Element | None = root.find("appmsg")
    if appmsg is None and root.tag == "appmsg":
        appmsg = root

    # 如果找到了 <appmsg>，从中提取字段
    if appmsg is not None:
        title = _extract_text(appmsg, "title")
        description = _extract_text(appmsg, "des")
        url = _extract_text(appmsg, "url")
    else:
        # 没有 <appmsg>，尝试从 <msg> 根直接提取
        title = _extract_text(root, "title")
        description = _extract_text(root, "des")
        url = _extract_text(root, "url")

    # 至少需要标题或 URL 才算有效解析
    if not title and not url:
        return raw_content

    # 构建输出
    parts: list[str] = []
    if title and url:
        parts.append(f"[链接: {title}]({url})")
    elif title:
        parts.append(f"[链接: {title}]")
    elif url:
        parts.append(f"[链接: {url}]")

    if description:
        parts.append(f" - {description}")

    return "".join(parts)


# ============================================================
# P3-1 扩展解析器: parse_emoji / parse_weapp / parse_location
# ============================================================


def parse_emoji(raw_content: str) -> str:
    """解析表情消息 rawContent，返回语义占位符。

    表情消息 (localType=47) 的 rawContent 可能包含 XML 协议数据
    (CDN 地址、二进制标识等)。T-W13-1 后 mediaUrl 可用，
    解析层仅需返回语义占位符 [表情]。

    Args:
        raw_content: 原始 rawContent 字符串

    Returns:
        "[表情]" 或原始 raw_content (解析失败时)
    """
    if not raw_content or not isinstance(raw_content, str):
        return raw_content if raw_content else ""

    raw_content = raw_content.strip()
    if not raw_content:
        return ""

    # XML 表情消息 — 返回语义占位符，丢弃所有协议数据
    if raw_content.startswith("<"):
        return "[表情]"

    # 非 XML 的纯文本内容 (如 "[动画表情]") — 直接使用
    return raw_content


def parse_weapp(raw_content: str) -> str:
    """解析小程序 rawContent XML，提取标题返回格式化文本。

    XML 结构 (小程序消息的 rawContent):
        <msg>
          <appmsg>
            <title><![CDATA[小程序标题]]></title>
            <type>33</type>  (或 36)
          </appmsg>
        </msg>

    输出格式: [小程序: 标题]

    Args:
        raw_content: 原始 XML 字符串

    Returns:
        格式化小程序信息，或原始 raw_content (解析失败时)
    """
    # A008: 预初始化所有提取变量
    title: str = ""

    if not raw_content or not isinstance(raw_content, str):
        return raw_content if raw_content else ""

    raw_content = raw_content.strip()
    if not raw_content or not raw_content.startswith("<"):
        return raw_content

    root: ET.Element | None = None
    try:
        root = ET.fromstring(raw_content)
    except ET.ParseError as exc:
        logger.debug("parse_weapp: XML parse error: %s", exc)
        return raw_content

    if root is None:
        return raw_content

    # 定位 <appmsg> 容器
    appmsg: ET.Element | None = root.find("appmsg")
    if appmsg is None and root.tag == "appmsg":
        appmsg = root

    title = _extract_text(appmsg, "title") if appmsg is not None else _extract_text(root, "title")

    if title:
        return f"[小程序: {title}]"

    return "[小程序]"


def parse_location(raw_content: str) -> str:
    """解析位置消息 rawContent XML，提取标签返回格式化文本。

    XML 结构 (位置消息的 rawContent):
        <msg>
          <appmsg>
            <title><![CDATA[位置标签]]></title>
            <type>34</type>
            <url>...</url>
          </appmsg>
        </msg>
    或:
        <msg>
          <location ... label="位置标签" ... />
        </msg>

    输出格式: [位置: 标签]

    Args:
        raw_content: 原始 XML 字符串

    Returns:
        格式化位置信息，或原始 raw_content (解析失败时)
    """
    # A008: 预初始化
    label: str = ""

    if not raw_content or not isinstance(raw_content, str):
        return raw_content if raw_content else ""

    raw_content = raw_content.strip()
    if not raw_content or not raw_content.startswith("<"):
        return raw_content

    root: ET.Element | None = None
    try:
        root = ET.fromstring(raw_content)
    except ET.ParseError as exc:
        logger.debug("parse_location: XML parse error: %s", exc)
        return raw_content

    if root is None:
        return raw_content

    # 尝试多种结构:
    # 1. <msg><appmsg><title>...</appmsg></msg>
    # 2. <msg><location label="..." /></msg>
    # 3. <location label="..." />

    # Strategy 1: <appmsg> 容器中的 <title>
    appmsg = root.find("appmsg")
    if appmsg is None and root.tag == "appmsg":
        appmsg = root

    if appmsg is not None:
        label = _extract_text(appmsg, "title")
    else:
        # Strategy 2: <location> 元素的 label 属性
        loc = root.find("location")
        if loc is None and root.tag == "location":
            loc = root
        # Strategy 2+3: location label 属性 or root <title>
        label = str(loc.get("label", "")) if loc is not None else _extract_text(root, "title")

    if label:
        return f"[位置: {label}]"

    return "[位置]"


# ============================================================
# 通用清洗辅助
# ============================================================

# P3-1: 清除残留 XML 标签、CDN 标识、长 hex 字符串
_RE_XML_TAG = re.compile(r"<[^>]+>")
_RE_CDN_PREFIX = re.compile(r"@cdn_\w+")
_RE_LONG_HEX = re.compile(r"[\da-f]{64,}")


def clean_noise(text: str) -> str:
    """清除文本中的无语义噪声 (XML 残留标签、CDN 标识、长 hex 字符串)。

    P3-1 通用清洗兜底 — 在 parse_raw_content 末尾调用。
    仅对已解析的文本执行清洗，不修改原始 current_content 回退值。

    Args:
        text: 待清洗文本

    Returns:
        清洗后的文本
    """
    text = _RE_XML_TAG.sub("", text)
    text = _RE_CDN_PREFIX.sub("", text)
    text = _RE_LONG_HEX.sub("", text)
    # 清理多余空白
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


# ============================================================
# 分发入口 (P014: 外层包装)
# ============================================================


def parse_raw_content(
    raw_content: str,
    msg_type: str,
    current_content: str,
) -> str:
    """根据 msg_type 分发到对应解析器，解析失败返回 current_content。

    优雅降级 (P014/P016):
      - 解析成功 → 返回格式化文本
      - 解析失败/无 raw_content → 返回 current_content (不抛异常)
      - 未匹配的 msg_type → 返回 current_content (透传)

    Args:
        raw_content: rawContent XML 字符串
        msg_type: 消息类型字符串 (如 "reply", "file", "link")
        current_content: 当前 content 字段值 (用于降级回退)

    Returns:
        解析后的可读文本，或 current_content (降级时)
    """
    if not raw_content or not raw_content.strip():
        return current_content

    if not raw_content.strip().startswith("<"):
        return current_content

    # P021: Type-specialized parser dispatch — 每种消息类型专用解析器
    parsed: str | None = None
    try:
        if msg_type == "reply":
            parsed = parse_quote(raw_content)
        elif msg_type == "file":
            parsed = parse_file(raw_content)
        elif msg_type == "link":
            parsed = parse_link(raw_content)
        elif msg_type == "emoji":
            parsed = parse_emoji(raw_content)
        elif msg_type == "weapp":
            parsed = parse_weapp(raw_content)
        elif msg_type == "location":
            parsed = parse_location(raw_content)
    except Exception:
        # P014: NEVER 抛异常 — 最外层兜底
        logger.debug(
            "parse_raw_content: unexpected error for msg_type=%s, falling back",
            msg_type,
        )

    if parsed is not None and parsed != raw_content:
        # P3-1: 通用清洗兜底 — 清除残留 XML 标签、CDN 标识、长 hex 字符串
        return clean_noise(parsed)

    return current_content
