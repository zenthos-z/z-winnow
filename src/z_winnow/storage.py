"""Storage layer — SQLite three-layer data model with serverID provenance.

Provides a unified async API for:
- raw_messages (Layer 1): CipherTalk original messages, serverID as primary key
- parsed_contexts (Layer 2): Context blocks assembled from messages, server_ids JSON FK
- topic_summaries (Layer 3): Topic-level structured summaries, context_ids + source_server_ids FKs

Provenance chain:
    raw_messages.serverID ← topic_summaries.source_server_ids (JSON LIKE)
    raw_messages.serverID ← parsed_contexts.server_ids (JSON LIKE)
    topic_summaries.context_ids → parsed_contexts.context_id

Usage:
    async with Storage("data/winnow.db") as store:
        await store.insert_raw_messages(messages, date="20260428")
        chain = await store.trace_message_to_topics("msg_12345")
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from z_winnow.config import get_settings
from z_winnow.pipeline.database import (
    init_database_in_conn,
)

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

CST = timezone(datetime.now().astimezone().utcoffset() or __import__("datetime").timedelta(hours=8))

# ============================================================
# W9: Validation constants for groups + members CRUD
# ============================================================

# P009: allowed-whitelist pattern for UPDATE operations
VALID_MEMBER_ROLES: frozenset[str] = frozenset(
    {
        "admin",
        "manager",
        "monitor",
        "tech_lead",
        "observer",
        "owner",
        "viewer",
        "member",
    }
)

_ALLOWED_GROUP_FIELDS: frozenset[str] = frozenset(
    {
        "display_name",
        "chatroom_id",
        "output_dir",
        "feishu_enabled",
        "feishu_base_token",
        "feishu_table_summary",
        "feishu_table_topics",
        "feishu_table_resources",
        "feishu_table_engineering",
        "feishu_framework_initialized",
        "feishu_engineering_enabled",
        "engineering_enabled",
        "custom_prompt_hints",
        "is_active",
        "daily_report_enabled",
        "daily_schedule_cron",
    }
)

_ALLOWED_MEMBER_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "wxid",
        "role",
        "weight",
        "note",
        "is_active",
    }
)

# ============================================================
# Storage class — async context manager with full CRUD + provenance
# ============================================================


class Storage:
    """Async SQLite storage manager for winnow three-layer data.

    Provides CRUD operations for all three layers and full serverID
    provenance chain queries (forward and reverse).

    Usage:
        async with Storage("data/winnow.db") as store:
            await store.insert_raw_messages(messages, date="20260428")
            chain = await store.trace_message_to_topics("msg_12345")
    """

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize Storage with a database path.

        A013: None-sentinel pattern — the default path is resolved from the
        Settings singleton in the function body, NOT baked into the default
        parameter. A module-level default parameter would resolve the path once
        at import time, freezing the value and silently ignoring later env /
        WINNOW_DB_PATH overrides (mirrors pipeline/database.py:110-122).

        Args:
            db_path: Path to SQLite database file. If None, resolves to the
                configured Settings.db_path at construction time.
        """
        self.db_path: str = db_path or get_settings().db_path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> Storage:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open database connection and ensure schema exists."""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await init_database_in_conn(self._conn)
        logger.debug("Storage connected to %s", self.db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.debug("Storage connection closed.")

    @property
    def conn(self) -> aiosqlite.Connection:
        """Get the current connection, raising if not connected."""
        if self._conn is None:
            raise RuntimeError(
                "Storage not connected. Use 'async with Storage()' or call connect()."
            )
        return self._conn

    # ================================================================
    # Layer 1: raw_messages CRUD
    # ================================================================

    async def insert_raw_message(
        self,
        server_id: str,
        date: str,
        sender: str,
        content: str,
        msg_type: str = "text",
        media_url: str = "",
        sanitized: bool = False,
        reply_to: str = "",
        raw_json: dict[str, Any] | None = None,
        group_id: str = "",
    ) -> None:
        """Insert or replace a single raw message by serverID.

        Args:
            server_id: WeChat server message ID (primary key).
            date: Date string YYYYMMDD.
            sender: Sender identifier.
            content: Message text content.
            msg_type: Message type (text/image/voice/video/file/link/...).
            media_url: Media resource URL if applicable.
            sanitized: Whether the message contains prompt injection patterns.
            reply_to: Reply target server ID (empty if none).
            raw_json: Complete original message dict (serialized to JSON).
            group_id: 群组标识符（groups 表 PK）.
        """
        db = self.conn
        raw_data = raw_json or {
            "server_id": server_id,
            "date": date,
            "sender": sender,
            "content": content,
            "msg_type": msg_type,
            "media_url": media_url,
            "sanitized": sanitized,
            "reply_to": reply_to,
            "group_name": "",
        }
        if isinstance(raw_data, str):
            raw_json_str = raw_data
        else:
            raw_json_str = raw_data.get("raw_json") if isinstance(raw_data, dict) else None
            if not raw_json_str:
                raw_json_str = json.dumps(raw_data, ensure_ascii=False)
        await db.execute(
            """INSERT OR REPLACE INTO raw_messages
               (serverID, date, sender, content, msg_type, image_path, sanitized, raw_json, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                server_id,
                date,
                sender,
                content,
                msg_type,
                media_url,
                1 if sanitized else 0,
                raw_json_str,
                group_id,
            ),
        )
        await db.commit()

    async def insert_raw_messages(
        self,
        messages: list[dict[str, Any]],
        date: str,
        group_id: str = "",
    ) -> int:
        """Batch insert/replace raw messages for a date.

        Uses INSERT OR REPLACE to handle duplicate serverIDs safely.
        Each message dict should contain: server_id, sender, content, msg_type,
        and optionally: media_url, sanitized, reply_to, group_name, account_name,
        group_nickname, timestamp.

        Args:
            messages: List of message dicts.
            date: Date string YYYYMMDD.
            group_id: 群组标识符（groups 表 PK）.

        Returns:
            Number of messages inserted.
        """
        db = self.conn
        count = 0
        for msg in messages:
            server_id = msg.get("server_id", "")
            if not server_id:
                continue

            await db.execute(
                """INSERT OR REPLACE INTO raw_messages
                   (serverID, date, sender, content, msg_type, image_path, sanitized, raw_json, group_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    server_id,
                    date,
                    msg.get("sender", ""),
                    msg.get("content", ""),
                    msg.get("msg_type", "text"),
                    msg.get("media_url", ""),
                    1 if msg.get("sanitized") else 0,
                    msg.get("raw_json") or json.dumps(msg, ensure_ascii=False),
                    group_id,
                ),
            )
            count += 1

        await db.commit()
        logger.info("Inserted %d raw messages for date %s", count, date)
        return count

    async def get_raw_message(self, server_id: str) -> dict[str, Any] | None:
        """Get a single raw message by serverID.

        Args:
            server_id: WeChat server message ID.

        Returns:
            Message dict or None if not found.
        """
        db = self.conn
        cursor = await db.execute(
            "SELECT * FROM raw_messages WHERE serverID = ?",
            (server_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_raw_messages_by_date(
        self, date: str, *, group_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get all raw messages for a given date, ordered by serverID.

        Args:
            date: Date string YYYYMMDD.
            group_id: Optional group identifier for filtering.

        Returns:
            List of message dicts.
        """
        db = self.conn
        if group_id:
            cursor = await db.execute(
                "SELECT * FROM raw_messages WHERE date = ? AND group_id = ? ORDER BY serverID",
                (date, group_id),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM raw_messages WHERE date = ? ORDER BY serverID",
                (date,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_raw_message_count(
        self, date: str | None = None, *, group_id: str | None = None
    ) -> int:
        """Count raw messages, optionally filtered by date and group.

        Args:
            date: Optional date filter YYYYMMDD.
            group_id: Optional group identifier for filtering.

        Returns:
            Message count.
        """
        db = self.conn
        if date and group_id:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE date = ? AND group_id = ?",
                (date, group_id),
            )
        elif date:
            cursor = await db.execute("SELECT COUNT(*) FROM raw_messages WHERE date = ?", (date,))
        elif group_id:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE group_id = ?", (group_id,)
            )
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM raw_messages")
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ================================================================
    # Layer 2: parsed_contexts CRUD
    # ================================================================

    async def insert_context(
        self,
        date: str,
        server_ids: list[str],
        context_text: str,
        token_count: int = 0,
        source_subagent: str = "",
        group_id: str = "",
    ) -> str:
        """Insert a parsed context block linking to raw messages via server_ids.

        Args:
            date: Date string YYYYMMDD.
            server_ids: List of serverIDs this context references.
            context_text: The assembled context text.
            token_count: Estimated token count.
            source_subagent: Which subagent created this context.
            group_id: 群组标识符（groups 表 PK）.

        Returns:
            The generated context_id string.
        """
        db = self.conn
        context_id = f"ctx_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO parsed_contexts
               (context_id, date, server_ids, context_text, token_count, source_subagent, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                context_id,
                date,
                json.dumps(server_ids, ensure_ascii=False),
                context_text,
                token_count,
                source_subagent,
                group_id,
            ),
        )
        await db.commit()
        logger.debug("Inserted context %s with %d server_ids", context_id, len(server_ids))
        return context_id

    async def get_context(self, context_id: str) -> dict[str, Any] | None:
        """Get a single context block by ID.

        Args:
            context_id: Context block ID.

        Returns:
            Context dict or None.
        """
        db = self.conn
        cursor = await db.execute(
            "SELECT * FROM parsed_contexts WHERE context_id = ?",
            (context_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_contexts_by_date(
        self, date: str, *, group_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get all context blocks for a date.

        Args:
            date: Date string YYYYMMDD.
            group_id: Optional group identifier for filtering.

        Returns:
            List of context dicts.
        """
        db = self.conn
        if group_id:
            cursor = await db.execute(
                "SELECT * FROM parsed_contexts WHERE date = ? AND group_id = ? ORDER BY context_id",
                (date, group_id),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM parsed_contexts WHERE date = ? ORDER BY context_id",
                (date,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_contexts_by_server_id(self, server_id: str) -> list[dict[str, Any]]:
        """Find all context blocks that reference a given serverID.

        Uses SQLite LIKE to search within the server_ids JSON array.

        Args:
            server_id: The serverID to search for.

        Returns:
            List of matching context dicts.
        """
        db = self.conn
        cursor = await db.execute(
            "SELECT * FROM parsed_contexts WHERE server_ids LIKE ? ORDER BY date",
            (f"%{server_id}%",),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ================================================================
    # Layer 3: topic_summaries CRUD
    # ================================================================

    async def get_topic_summary(self, summary_id: str) -> dict[str, Any] | None:
        """Get a single topic summary by ID.

        Args:
            summary_id: Summary record ID.

        Returns:
            Summary dict or None.
        """
        db = self.conn
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE summary_id = ?",
            (summary_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_topics_by_date(
        self, date: str, *, group_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get all topic summaries for a date.

        Args:
            date: Date string YYYYMMDD.
            group_id: Optional group identifier for filtering.

        Returns:
            List of topic summary dicts.
        """
        db = self.conn
        if group_id:
            cursor = await db.execute(
                "SELECT * FROM topic_summaries WHERE date = ? AND group_id = ? ORDER BY summary_id",
                (date, group_id),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM topic_summaries WHERE date = ? ORDER BY summary_id",
                (date,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_topics_by_name(self, topic_name: str) -> list[dict[str, Any]]:
        """Get all topic summaries matching a topic name.

        Args:
            topic_name: Topic name to search for.

        Returns:
            List of matching topic summary dicts, ordered by date desc.
        """
        db = self.conn
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE topic_name = ? ORDER BY date DESC",
            (topic_name,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_topic_count(self, date: str | None = None, *, group_id: str | None = None) -> int:
        """Count topic summaries, optionally filtered by date and group.

        Args:
            date: Optional date filter YYYYMMDD.
            group_id: Optional group identifier for filtering.

        Returns:
            Topic count.
        """
        db = self.conn
        if date and group_id:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM topic_summaries WHERE date = ? AND group_id = ?",
                (date, group_id),
            )
        elif date:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM topic_summaries WHERE date = ?", (date,)
            )
        elif group_id:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM topic_summaries WHERE group_id = ?", (group_id,)
            )
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM topic_summaries")
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ================================================================
    # Provenance Chain Queries — Full serverID traceability
    # ================================================================

    async def trace_message_to_topics(self, server_id: str) -> dict[str, Any]:
        """Reverse provenance: from a single serverID, find all linked topics.

        Query chain:
            1. raw_messages.serverID → get the message
            2. topic_summaries WHERE source_server_ids LIKE '%server_id%'
            3. parsed_contexts WHERE server_ids LIKE '%server_id%'

        Args:
            server_id: The WeChat server message ID.

        Returns:
            {
                "server_id": str,
                "message": dict or None,
                "contexts": list[dict],    # contexts referencing this message
                "referenced_by_topics": list[dict],  # topics referencing this message
            }
        """
        db = self.conn

        # Step 1: Get the raw message
        message = await self.get_raw_message(server_id)

        # Step 2: Find contexts that reference this server_id
        cursor = await db.execute(
            "SELECT * FROM parsed_contexts WHERE server_ids LIKE ? ORDER BY date",
            (f"%{server_id}%",),
        )
        contexts = [dict(row) for row in await cursor.fetchall()]

        # Step 3: Find topic summaries that reference this server_id
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE source_server_ids LIKE ? ORDER BY date DESC",
            (f"%{server_id}%",),
        )
        topics = [dict(row) for row in await cursor.fetchall()]

        return {
            "server_id": server_id,
            "message": message,
            "contexts": contexts,
            "referenced_by_topics": topics,
        }

    async def trace_topic_to_messages(self, topic_name: str) -> dict[str, Any]:
        """Forward provenance: from a topic name, find all linked messages.

        Query chain:
            1. topic_summaries WHERE topic_name = ? → get summaries
            2. Parse source_server_ids JSON from each summary
            3. raw_messages WHERE serverID IN (...) → get messages
            4. parsed_contexts WHERE context_id IN (...) → get contexts

        This is the full three-layer JOIN: topic → context → raw message.

        Args:
            topic_name: The topic name to trace.

        Returns:
            {
                "topic_name": str,
                "summaries": list[dict],       # matching topic_summaries records
                "contexts": list[dict],         # linked parsed_contexts
                "raw_messages": list[dict],     # linked raw_messages
            }
        """
        db = self.conn

        # Step 1: Find all topic summaries for this topic
        summaries = await self.get_topics_by_name(topic_name)
        if not summaries:
            return {
                "topic_name": topic_name,
                "summaries": [],
                "contexts": [],
                "raw_messages": [],
            }

        # Step 2: Collect all source_server_ids and context_ids
        all_server_ids: set[str] = set()
        all_context_ids: set[str] = set()
        for s in summaries:
            try:
                server_ids = json.loads(s.get("source_server_ids", "[]"))
                all_server_ids.update(server_ids)
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                ctx_ids = json.loads(s.get("context_ids", "[]"))
                all_context_ids.update(ctx_ids)
            except (json.JSONDecodeError, TypeError):
                pass

        # Step 3: Fetch all referenced raw messages
        raw_messages: list[dict[str, Any]] = []
        for sid in all_server_ids:
            cursor = await db.execute("SELECT * FROM raw_messages WHERE serverID = ?", (sid,))
            row = await cursor.fetchone()
            if row:
                raw_messages.append(dict(row))

        # Step 4: Fetch all linked contexts
        contexts: list[dict[str, Any]] = []
        for cid in all_context_ids:
            cursor = await db.execute("SELECT * FROM parsed_contexts WHERE context_id = ?", (cid,))
            row = await cursor.fetchone()
            if row:
                contexts.append(dict(row))

        return {
            "topic_name": topic_name,
            "summaries": summaries,
            "contexts": contexts,
            "raw_messages": raw_messages,
        }

    async def get_provenance_chain(self, server_id: str) -> dict[str, Any]:
        """Full provenance chain for a serverID: message → contexts → topics.

        Same as trace_message_to_topics but includes all intermediate contexts.

        Args:
            server_id: The WeChat server message ID.

        Returns:
            Dictionary with the complete provenance chain.
        """
        return await self.trace_message_to_topics(server_id)

    # ================================================================
    # Export — JSONL for RL training data
    # ================================================================

    async def export_jsonl(
        self,
        start_date: str,
        end_date: str,
        output_path: str = "data/rl_export.jsonl",
    ) -> int:
        """Export topic summaries as JSONL for RL training.

        Each line includes: topic_name, date, summary, confidence, model_used,
        source_server_ids, and placeholder human_signals for future reward labeling.

        Args:
            start_date: Start date YYYYMMDD (inclusive).
            end_date: End date YYYYMMDD (inclusive).
            output_path: Path for the output JSONL file.

        Returns:
            Number of records exported.
        """
        db = self.conn
        cursor = await db.execute(
            """SELECT * FROM topic_summaries
               WHERE date >= ? AND date <= ?
               ORDER BY date""",
            (start_date, end_date),
        )
        rows = await cursor.fetchall()

        output_dir = Path(output_path).parent
        output_dir.mkdir(parents=True, exist_ok=True)

        count = 0
        with open(output_path, "w", encoding="utf-8") as f:
            for row in rows:
                record = dict(row)
                try:
                    server_ids = json.loads(record.get("source_server_ids", "[]"))
                except (json.JSONDecodeError, TypeError):
                    server_ids = []

                f.write(
                    json.dumps(
                        {
                            "topic_name": record.get("topic_name", ""),
                            "date": record.get("date", ""),
                            "summary": record.get("summary_text", ""),
                            "confidence": record.get("confidence", 0.0),
                            "model_used": record.get("model_used", ""),
                            "source_server_ids": server_ids,
                            "human_signals": [],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                count += 1

        logger.info("Exported %d records to %s", count, output_path)
        return count

    # ================================================================
    # Utility — Stats
    # ================================================================

    async def get_stats(self) -> dict[str, int]:
        """Get overall database statistics.

        Returns:
            Dict with counts for raw_messages, parsed_contexts, topic_summaries.
        """
        db = self.conn
        stats: dict[str, int] = {}

        for table in ("raw_messages", "parsed_contexts", "topic_summaries"):
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")  # nosec B608 — table names are hardcoded
            row = await cursor.fetchone()
            stats[table] = row[0] if row else 0

        return stats

    # ================================================================
    # W9: Groups + Members CRUD (T-W9-B-d + T-W9-E-faa)
    # ================================================================

    async def get_group(self, group_id: str) -> dict[str, Any] | None:
        """Get a single group by ID.

        Args:
            group_id: Group identifier.

        Returns:
            Group dict or None if not found.
        """
        db = self.conn
        cursor = await db.execute(
            "SELECT * FROM groups WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def create_group(
        self, group_id: str, display_name: str, chatroom_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Create a new group. P009: optional kwargs via whitelist.

        Args:
            group_id: Group identifier (required, non-empty).
            display_name: Human-readable group name (required, non-empty).
            chatroom_id: WeChat chatroom ID, format xxx@chatroom (required, non-empty).
            **kwargs: Optional fields (output_dir, feishu_enabled,
                custom_prompt_hints, is_active, daily_report_enabled,
                daily_schedule_cron, created_by).

        Returns:
            Dict of the created group record.

        Raises:
            ValueError: If required fields are empty.
        """
        if not group_id or not group_id.strip():
            raise ValueError("group_id must be non-empty")
        if not display_name or not display_name.strip():
            raise ValueError("display_name must be non-empty")
        if not chatroom_id or not chatroom_id.strip():
            raise ValueError("chatroom_id must be non-empty")

        db = self.conn
        now = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S")
        gid = group_id.strip()

        await db.execute(
            """INSERT INTO groups
               (group_id, display_name, chatroom_id, output_dir,
                feishu_enabled, custom_prompt_hints,
                is_active, daily_report_enabled,
                daily_schedule_cron, created_at, updated_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                gid,
                display_name.strip(),
                chatroom_id.strip(),
                kwargs.get("output_dir"),
                kwargs.get("feishu_enabled", 1),
                kwargs.get("custom_prompt_hints"),
                kwargs.get("is_active", 1),
                kwargs.get("daily_report_enabled", 1),
                kwargs.get("daily_schedule_cron"),
                now,
                now,
                kwargs.get("created_by"),
            ),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM groups WHERE group_id = ?",
            (gid,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    async def list_groups(self, is_active: bool = True) -> list[dict[str, Any]]:
        """List all groups, optionally filtered by active status. P022: pure storage, dict return.

        Args:
            is_active: If True, return only active groups. If False, return all.

        Returns:
            List of group dicts ordered by group_id.
        """
        db = self.conn
        if is_active:
            cursor = await db.execute(
                "SELECT * FROM groups WHERE is_active = 1 ORDER BY group_id",
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM groups ORDER BY group_id",
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_group(self, group_id: str, **kwargs: Any) -> dict[str, Any]:
        """Update a group. P009: only modifies explicitly passed kwargs via whitelist.

        Args:
            group_id: Group identifier (required, non-empty).
            **kwargs: Fields to update (only keys in _ALLOWED_GROUP_FIELDS).

        Returns:
            Updated group dict.

        Raises:
            ValueError: If group not found or group_id empty.
        """
        if not group_id or not group_id.strip():
            raise ValueError("group_id must be non-empty")

        db = self.conn
        gid = group_id.strip()
        cursor = await db.execute(
            "SELECT * FROM groups WHERE group_id = ?",
            (gid,),
        )
        existing = await cursor.fetchone()
        if existing is None:
            raise ValueError(f"Group not found: {gid}")

        # P009: whitelist-based field update
        sets: list[str] = []
        values: list[Any] = []
        for key, value in kwargs.items():
            if key in _ALLOWED_GROUP_FIELDS:
                sets.append(f"{key} = ?")
                values.append(value)

        if not sets:
            return dict(existing)

        now = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S")
        sets.append("updated_at = ?")
        values.append(now)
        values.append(gid)

        await db.execute(
            f"UPDATE groups SET {', '.join(sets)} WHERE group_id = ?",
            tuple(values),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM groups WHERE group_id = ?",
            (gid,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    async def delete_group(self, group_id: str) -> None:
        """Soft-delete a group (sets is_active=0). G4: no physical deletion.

        P053: SQLite ON DELETE CASCADE does not apply to UPDATE,
        child group_members rows are unaffected at the application layer.

        Args:
            group_id: Group identifier.
        """
        db = self.conn
        now = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S")
        await db.execute(
            "UPDATE groups SET is_active = 0, updated_at = ? WHERE group_id = ?",
            (now, group_id),
        )
        await db.commit()

    # ——— group_members CRUD —————————————————————

    async def _next_member_id(self, group_id: str) -> str:
        """Generate the next member_id in 'gm-{group_id}-{seq}' format."""
        db = self.conn
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM group_members WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0
        return f"gm-{group_id}-{count + 1}"

    async def add_member(
        self,
        group_id: str,
        name: str,
        role: str,
        wxid: str | None = None,
        weight: float = 1.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Add a member to a group. P009: default-None pattern for optional params.

        Args:
            group_id: Group identifier (required, non-empty).
            name: Member display name (required, non-empty).
            role: Member role, must be in VALID_MEMBER_ROLES.
            wxid: WeChat ID (optional).
            weight: Member weight >= 0 (default 1.0).
            **kwargs: Optional fields (note).

        Returns:
            Dict of the created member record.

        Raises:
            ValueError: If group_id empty, name empty, role invalid, weight < 0,
                        or group not found.
        """
        if not group_id or not group_id.strip():
            raise ValueError("group_id must be non-empty")
        if not name or not name.strip():
            raise ValueError("name must be non-empty")

        # P009: default-None role validation
        if not role or role not in VALID_MEMBER_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_MEMBER_ROLES)}, got: {role!r}")
        if weight < 0:
            raise ValueError("weight must be >= 0")

        gid = group_id.strip()

        # Verify group exists before adding member
        existing_group = await self.get_group(gid)
        if existing_group is None:
            raise ValueError(f"Group not found: {gid}")

        db = self.conn
        member_id = await self._next_member_id(gid)
        now = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S")
        note = kwargs.get("note")

        await db.execute(
            """INSERT INTO group_members
               (member_id, group_id, name, wxid, role, weight, note, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                member_id,
                gid,
                name.strip(),
                wxid,
                role,
                weight,
                note,
                now,
            ),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM group_members WHERE member_id = ?",
            (member_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    async def list_members(self, group_id: str, is_active: bool = True) -> list[dict[str, Any]]:
        """List members for a group. P022: pure storage, dict return.

        Args:
            group_id: Group identifier.
            is_active: If True, return only active members. If False, return all.

        Returns:
            List of member dicts ordered by member_id.
        """
        db = self.conn
        if is_active:
            cursor = await db.execute(
                "SELECT * FROM group_members WHERE group_id = ? AND is_active = 1 ORDER BY member_id",
                (group_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM group_members WHERE group_id = ? ORDER BY member_id",
                (group_id,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_member(self, member_id: str, **kwargs: Any) -> dict[str, Any]:
        """Update a member. P009: whitelist-based field update.

        Args:
            member_id: Member identifier (required, non-empty).
            **kwargs: Fields to update (only keys in _ALLOWED_MEMBER_FIELDS).

        Returns:
            Updated member dict.

        Raises:
            ValueError: If member not found, member_id empty,
                        role invalid, or weight < 0.
        """
        if not member_id or not member_id.strip():
            raise ValueError("member_id must be non-empty")

        # P009: validate values before UPDATE
        if "role" in kwargs and kwargs["role"] not in VALID_MEMBER_ROLES:
            raise ValueError(
                f"role must be one of {sorted(VALID_MEMBER_ROLES)}, got: {kwargs['role']!r}"
            )
        if "weight" in kwargs and kwargs["weight"] < 0:
            raise ValueError("weight must be >= 0")

        db = self.conn
        mid = member_id.strip()
        cursor = await db.execute(
            "SELECT * FROM group_members WHERE member_id = ?",
            (mid,),
        )
        existing = await cursor.fetchone()
        if existing is None:
            raise ValueError(f"Member not found: {mid}")

        # P009: whitelist-based field update
        sets: list[str] = []
        values: list[Any] = []
        for key, value in kwargs.items():
            if key in _ALLOWED_MEMBER_FIELDS:
                sets.append(f"{key} = ?")
                values.append(value)

        if not sets:
            return dict(existing)

        values.append(mid)

        await db.execute(
            f"UPDATE group_members SET {', '.join(sets)} WHERE member_id = ?",
            tuple(values),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM group_members WHERE member_id = ?",
            (mid,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    async def remove_member(self, member_id: str) -> None:
        """Soft-delete a member (sets is_active=0). P053: no physical DELETE.

        Args:
            member_id: Member identifier.
        """
        db = self.conn
        await db.execute(
            "UPDATE group_members SET is_active = 0 WHERE member_id = ?",
            (member_id,),
        )
        await db.commit()

    # ================================================================
    # W9: Core Topics CRUD (T-W9-B-d + T-W9-E-ga)
    # ================================================================

    async def _next_core_topic_id(self, group_id: str) -> str:
        """Generate the next core_topic_id in 'core-{group_id}-{seq}' format."""
        db = self.conn
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM core_topics WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0
        return f"core-{group_id}-{count + 1}"

    async def create_core_topic(
        self,
        group_id: str,
        name: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a new core topic for a group.

        Args:
            group_id: Group identifier (required, non-empty).
            name: Topic name.
            **kwargs: Optional fields (description, keywords, priority).

        Returns:
            Dict of the created core topic record.

        Raises:
            ValueError: If group_id empty or priority not 1 or 2.
        """
        if not group_id or not group_id.strip():
            raise ValueError("group_id must be non-empty")

        priority = int(kwargs.get("priority", 1))
        if priority not in (1, 2):
            raise ValueError("priority must be 1 or 2")

        description = kwargs.get("description")
        keywords = kwargs.get("keywords")

        db = self.conn
        core_topic_id = await self._next_core_topic_id(group_id.strip())
        now = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S")

        await db.execute(
            """INSERT INTO core_topics
               (core_topic_id, group_id, name, description, keywords, priority,
                is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                core_topic_id,
                group_id.strip(),
                name,
                description,
                keywords,
                priority,
                now,
                now,
            ),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM core_topics WHERE core_topic_id = ?",
            (core_topic_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    async def get_core_topic(self, core_topic_id: str) -> dict[str, Any] | None:
        """Get a single core topic by ID.

        Args:
            core_topic_id: Core topic identifier.

        Returns:
            Core topic dict or None.
        """
        db = self.conn
        cursor = await db.execute(
            "SELECT * FROM core_topics WHERE core_topic_id = ?",
            (core_topic_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_core_topics(self, group_id: str, is_active: bool = True) -> list[dict[str, Any]]:
        """List core topics for a group.

        Args:
            group_id: Group identifier.
            is_active: If True, return only active topics. If False, return all.

        Returns:
            List of core topic dicts.
        """
        db = self.conn
        if is_active:
            cursor = await db.execute(
                "SELECT * FROM core_topics WHERE group_id = ? AND is_active = 1 ORDER BY priority, created_at",
                (group_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM core_topics WHERE group_id = ? ORDER BY priority, created_at",
                (group_id,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_core_topic(self, core_topic_id: str, **kwargs: Any) -> dict[str, Any]:
        """Update a core topic. P009: only modifies explicitly passed kwargs.

        Args:
            core_topic_id: Core topic identifier.
            **kwargs: Fields to update (name, description, keywords, priority, is_active).

        Returns:
            Updated core topic dict.

        Raises:
            ValueError: If core topic not found or priority invalid.
        """
        db = self.conn
        cursor = await db.execute(
            "SELECT * FROM core_topics WHERE core_topic_id = ?",
            (core_topic_id,),
        )
        existing_row = await cursor.fetchone()
        if existing_row is None:
            raise ValueError(f"Core topic not found: {core_topic_id}")

        if "priority" in kwargs and kwargs["priority"] not in (1, 2):
            raise ValueError("priority must be 1 or 2")

        allowed = {"name", "description", "keywords", "priority", "is_active"}
        sets: list[str] = []
        values: list[Any] = []
        for key, value in kwargs.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                values.append(value)

        if not sets:
            return dict(existing_row)

        now = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S")
        sets.append("updated_at = ?")
        values.append(now)
        values.append(core_topic_id)

        await db.execute(
            f"UPDATE core_topics SET {', '.join(sets)} WHERE core_topic_id = ?",
            tuple(values),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM core_topics WHERE core_topic_id = ?",
            (core_topic_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    async def delete_core_topic(self, core_topic_id: str) -> None:
        """Soft-delete a core topic (sets is_active=0).

        P053: No child tables exist for core_topics — no cascade needed.

        Args:
            core_topic_id: Core topic identifier.
        """
        db = self.conn
        now = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S")
        await db.execute(
            "UPDATE core_topics SET is_active = 0, updated_at = ? WHERE core_topic_id = ?",
            (now, core_topic_id),
        )
        await db.commit()
