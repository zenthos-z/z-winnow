"""Chat context markdown builder for LLM consumption.

Generates a temporary markdown file from L2 parsed messages, replacing the
former 4-layer formatting chain (enrich_message_for_llm → _enrich_messages →
wrap_messages_xml).  The output is consumed by orchestrator and unified_reporter.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _format_voice_duration(duration_ms: int) -> str:
    """Format voice duration in milliseconds to human-readable string."""
    if duration_ms <= 0:
        return "[语音]"
    seconds = duration_ms / 1000 if duration_ms > 1000 else duration_ms
    if seconds >= 60:
        return f"[语音: {int(seconds // 60)}分{int(seconds % 60)}秒]"
    return f"[语音: {int(seconds)}秒]"


class ChatContextBuilder:
    """Build a markdown chat context string from L2 parsed messages.

    Each message is rendered as a ``###`` heading with timestamp, sender, and
    server ID, followed by the formatted content.  Reply messages include a
    blockquote showing the referenced message.
    """

    def __init__(
        self,
        messages: list[dict],
        image_descriptions: dict[str, str] | None = None,
        link_previews: dict[str, dict[str, str]] | None = None,
        *,
        group_id: str = "",
        date: str = "",
        group_name: str = "",
        member_map: dict[str, str] | None = None,
    ) -> None:
        self.messages = messages
        self.image_descriptions = image_descriptions or {}
        self.link_previews = link_previews or {}
        self.group_id = group_id
        self.date = date
        self.group_name = group_name
        self.member_map = member_map or {}

        # Build server_id → message lookup for reply resolution (O(n))
        self._msg_by_id: dict[str, dict] = {}
        for m in messages:
            sid = m.get("server_id", "")
            if sid:
                self._msg_by_id[sid] = m

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> str:
        """Build the full markdown context and write to ``data/tmp/``."""
        parts: list[str] = []

        # Header
        display_date = self._format_date(self.date)
        header = f"# 群聊记录 — {self.group_name} — {display_date}"
        parts.append(header)
        parts.append("")

        # CT.3d: Ensure chronological order before rendering
        self.messages.sort(key=lambda m: m.get("timestamp", 0))

        for msg in self.messages:
            formatted = self._format_message(msg)
            if formatted:
                parts.append(formatted)
                parts.append("")

        # Footer
        parts.append(f"--- 共 {len(self.messages)} 条消息 ---")

        markdown = "\n".join(parts)

        # Write to temp file
        if self.group_id and self.date:
            self._write_tmp_file(markdown)

        return markdown

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------

    def _format_message(self, msg: dict) -> str:
        """Format a single message as a markdown section."""
        server_id = str(msg.get("server_id", ""))
        timestamp = msg.get("timestamp", "")
        sender = self._get_sender(msg)
        time_str = self._format_timestamp(timestamp)

        heading = f"### {time_str} | {sender} | svrid:{server_id}"
        content = self._format_content(msg)

        # Reply blockquote
        blockquote = self._format_reply_blockquote(msg)

        sections = [heading, ""]
        if blockquote:
            sections.append(blockquote)
            sections.append("")
        sections.append(content)

        return "\n".join(sections)

    def _format_content(self, msg: dict) -> str:
        """Format message content based on msg_type.

        Replaces the 11-branch routing from enrich_message_for_llm().
        """
        msg_type = msg.get("msg_type", "text")
        content = str(msg.get("content", ""))
        server_id = str(msg.get("server_id", ""))

        if msg_type == "text":
            # [图片] 占位符：优先用 Vision API 描述替换
            if content.strip() == "[图片]":
                desc = self.image_descriptions.get(server_id, "")
                if desc:
                    return desc
                return "[图片]"
            return content

        if msg_type == "image":
            desc = self.image_descriptions.get(server_id, "")
            if desc:
                return desc
            return "[图片]"

        if msg_type == "reply":
            # Reply content is already cleaned by sanitize_reply_content()
            return content

        if msg_type in ("link", "appmsg"):
            # Already parsed by xml_parsers / card_parser
            # Append link preview metadata if available (title, description, site_name)
            lp = self.link_previews.get(server_id, {})
            if lp:
                parts: list[str] = [content]
                title = lp.get("title", "")
                desc = lp.get("description", "")
                site = lp.get("site_name", "")
                if title:
                    parts.append(f"[链接预览: {title}]")
                if desc:
                    parts.append(f"[链接描述: {desc[:200]}]")
                if site:
                    parts.append(f"[来源: {site}]")
                return "\n".join(parts)
            return content

        if msg_type == "file":
            media_url = msg.get("media_url", "")
            media_local = msg.get("media_local_path", "")
            location = ""
            if media_local:
                location = f" [本地: {media_local}]"
            elif media_url:
                location = f" [下载: {media_url}]"
            return f"[文件: {content}]{location}"

        if msg_type == "voice":
            return _format_voice_duration(msg.get("voice_duration_ms", 0))

        if msg_type == "video":
            return "[视频]"

        if msg_type == "emoji":
            # Image stickers analyzed by Vision API
            desc = self.image_descriptions.get(server_id, "")
            if desc:
                return f"[表情包: {desc}]"
            return content if content else "[表情]"

        if msg_type == "weapp":
            if content:
                return f"[小程序: {content}]"
            return "[小程序]"

        if msg_type == "location":
            return f"[位置: {content}]" if content else "[位置]"

        # Fallback for 'other', 'redpacket', 'transfer', 'contact', etc.
        return content

    def _format_reply_blockquote(self, msg: dict) -> str | None:
        """Build a blockquote for reply messages referencing another message."""
        msg_type = msg.get("msg_type", "")
        if msg_type != "reply":
            return None

        reply_to = str(msg.get("reply_to", ""))
        if not reply_to:
            return None

        ref_msg = self._msg_by_id.get(reply_to)
        if not ref_msg:
            return None

        ref_sender = self._get_sender(ref_msg)
        ref_content = self._format_content(ref_msg)
        # Truncate long referenced content for readability
        if len(ref_content) > 100:
            ref_content = ref_content[:100] + "..."

        return f"> 💬 引用 {ref_sender}: {ref_content}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_sender(self, msg: dict) -> str:
        """Get display name for a message sender, with member_map fallback."""
        name: str = str(msg.get("account_name", "") or "")
        if name and not name.startswith("wxid_") and not name.endswith("@openim"):
            return name
        # Fallback to member_map lookup by sender wxid
        sender: str = str(msg.get("sender", "") or "")
        if sender in self.member_map:
            return self.member_map[sender]
        if sender and not sender.startswith("wxid_") and not sender.endswith("@openim"):
            return sender
        return name or sender or "未知"

    @staticmethod
    def _format_timestamp(timestamp: str | int) -> str:
        """Convert timestamp to HH:MM:SS format."""
        if not timestamp:
            return "00:00:00"
        try:
            ts = int(timestamp)
            # timestamp could be seconds or milliseconds
            if ts > 1e12:
                ts = ts // 1000
            import datetime

            dt = datetime.datetime.fromtimestamp(ts)
            return dt.strftime("%H:%M:%S")
        except (ValueError, TypeError, OSError):
            # Fallback: try extracting HH:MM from string
            s = str(timestamp)
            if len(s) >= 4:
                return f"{s[:2]}:{s[2:4]}:00"
            return "00:00:00"

    @staticmethod
    def _format_date(date: str) -> str:
        """Format YYYYMMDD to human-readable date."""
        if not date or len(date) != 8:
            return date
        return f"{date[:4]}-{date[4:6]}-{date[6:8]}"

    def _write_tmp_file(self, markdown: str) -> None:
        """Write markdown to data/tmp/chat_context_{group_id}_{date}.md."""
        try:
            tmp_dir = Path("data/tmp")
            tmp_dir.mkdir(parents=True, exist_ok=True)
            filename = f"chat_context_{self.group_id}_{self.date}.md"
            filepath = tmp_dir / filename
            filepath.write_text(markdown, encoding="utf-8")
            logger.info("Wrote chat context to %s (%d chars)", filepath, len(markdown))
        except OSError:
            logger.warning("Failed to write chat context tmp file", exc_info=True)
