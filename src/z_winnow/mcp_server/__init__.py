"""MCP interface layer — read L3 + write feedback Inbox.

See ``docs/mcp-platform-checkpoint.md`` §4.1. 不暴露 MemOS; 模糊检索 (场景 A) 用 LIKE
over ``topic_summaries`` (FTS5 内置 tokenizer 对中文 2 字词不可用, 详见 checkpoint §10)。
"""

from z_winnow.mcp_server.server import get_db, mcp, run

__all__ = ["get_db", "mcp", "run"]
