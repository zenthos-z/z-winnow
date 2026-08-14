"""MemOS real adapter — async httpx client wrapping MemOS REST API.

Targets MemOS 2.0 (MemoryOS package). Endpoints used:
  POST /product/add       — add memories (messages or memory_content)
  POST /product/search    — semantic search (response: data.text_mem[].memories[])
  POST /product/get_all   — list memories (requires memory_type; data=[{cube_id,memories:[{nodes}]}])
  POST /product/delete_memory — delete memories
  POST /product/feedback  — (2.0) native MemFeedback: archive old + write new, version-traceable
  GET  /product/get_memory/{id} — (2.0) fetch single memory by id
  GET  /product/scheduler/status | POST /scheduler/wait — scheduler
  GET  /openapi.json      — health check

Design:
- Deterministic cube_id: "winnow:{scope}" — cubes are auto-created on first add
- Read methods propagate exceptions (MemOS is required service)
- Write methods fault-tolerant (graceful degradation)
- add_structured_memory captures memory_ids for caller linkage (feedback provenance)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, cast

import httpx

from z_winnow.memory.types import (
    MemoryResult,
    StructuredMemoryItem,
)

logger = logging.getLogger(__name__)

DEFAULT_MEMOS_BASE_URL = "http://127.0.0.1:8000"

DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=60.0,
    write=10.0,
    pool=5.0,
)

DEFAULT_POOL_LIMITS = httpx.Limits(
    max_keepalive_connections=10,
    max_connections=20,
    keepalive_expiry=30.0,
)


class MemOSAdapter:
    """Real MemOS adapter communicating with the MemOS REST API via httpx.

    Usage:
        adapter = MemOSAdapter(base_url="http://localhost:8000")
        results = await adapter.search_memories(
            query="...", group_id="user1", readable_cube_ids=["cube1"],
        )
        await adapter.close()
    """

    def __init__(
        self,
        base_url: str = DEFAULT_MEMOS_BASE_URL,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self.base_url: str = base_url.rstrip("/")
        self._timeout: httpx.Timeout = timeout or DEFAULT_TIMEOUT
        self._client: httpx.AsyncClient | None = None
        # T-W13-5: Per-group Lock for concurrent write protection
        # P072: Lock keyed by user_id (= group_id after this task)
        self._group_locks: dict[str, asyncio.Lock] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                limits=DEFAULT_POOL_LIMITS,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Per-group concurrency control — T-W13-5
    # ------------------------------------------------------------------

    def _get_group_lock(self, group_id: str) -> asyncio.Lock:
        """Return per-group asyncio.Lock, lazily initialized."""
        if group_id not in self._group_locks:
            self._group_locks[group_id] = asyncio.Lock()
        return self._group_locks[group_id]

    # ------------------------------------------------------------------
    # Cube management — deterministic naming, no API registration needed
    # ------------------------------------------------------------------

    async def get_or_create_cube(self, scope: str) -> str:
        """Return a deterministic cube_id for the given scope.

        MemOS auto-creates cubes on first add, so no registration call needed.
        Format: "winnow:{scope}"
        """
        cube_id = f"winnow:{scope}"
        logger.debug("Cube resolved: scope=%s -> cube_id=%s", scope, cube_id)
        return cube_id

    # ------------------------------------------------------------------
    # Read methods — exceptions propagate (required service)
    # ------------------------------------------------------------------

    async def search_memories(
        self,
        query: str,
        group_id: str,
        readable_cube_ids: list[str],
        top_k: int = 20,
        legacy_group_ids: list[str] | None = None,
        mode: str = "fine",
    ) -> list[MemoryResult]:
        """Search memories via POST /product/search.

        Args:
            query: Search query string.
            group_id: Group identifier (sent as MemOS user_id).
            readable_cube_ids: Cube IDs to search.
            top_k: Max results.
            legacy_group_ids: Optional legacy group_ids for backward compat.
            mode: MemOS search mode — "fine" (default; precise recall, leverages
                MemReader auto-extracted tags), "fast", or "mixture". Empirically
                fine yields the best recall; callers may pass "mixture" for
                stability since fine occasionally returns empty on some queries.
        """
        mem_cube_id = readable_cube_ids[0] if readable_cube_ids else ""

        payload: dict[str, Any] = {
            "query": query,
            "user_id": group_id,
            "mem_cube_id": mem_cube_id,
            "top_k": top_k,
            "mode": mode,
        }
        client = await self._get_client()
        resp = await client.post("/product/search", json=payload)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()

        # Parse nested response: data.text_mem[].memories[]
        results: list[MemoryResult] = []
        seen_ids: set[str] = set()
        text_mem_groups = body.get("data", {}).get("text_mem", [])
        for group in text_mem_groups:
            if not isinstance(group, dict):
                continue
            for item in group.get("memories", []):
                if not isinstance(item, dict):
                    continue
                mem_id = item.get("id", str(uuid.uuid4()))
                if mem_id in seen_ids:
                    continue
                seen_ids.add(mem_id)
                results.append(
                    MemoryResult(
                        id=mem_id,
                        memory=item.get("memory", ""),
                        metadata=item.get("metadata", {}),
                        score=float(item.get("score", 0.0)),
                    )
                )

        if legacy_group_ids:
            for legacy_gid in legacy_group_ids:
                if legacy_gid == group_id or not legacy_gid:
                    continue
                try:
                    payload_legacy = {
                        "query": query,
                        "user_id": legacy_gid,
                        "mem_cube_id": mem_cube_id,
                        "top_k": top_k,
                        "mode": mode,
                    }
                    resp_l = await client.post("/product/search", json=payload_legacy)
                    resp_l.raise_for_status()
                    body_l: dict[str, Any] = resp_l.json()
                    lg_groups = body_l.get("data", {}).get("text_mem", [])
                    for grp in lg_groups:
                        if not isinstance(grp, dict):
                            continue
                        for itm in grp.get("memories", []):
                            if not isinstance(itm, dict):
                                continue
                            lid = itm.get("id", str(uuid.uuid4()))
                            if lid in seen_ids:
                                continue
                            seen_ids.add(lid)
                            results.append(
                                MemoryResult(
                                    id=lid,
                                    memory=itm.get("memory", ""),
                                    metadata=itm.get("metadata", {}),
                                    score=float(itm.get("score", 0.0)),
                                )
                            )
                    logger.debug(
                        "search_memories legacy: group=%s -> %d total results",
                        legacy_gid,
                        len(results),
                    )
                except Exception:
                    # Legacy search is best-effort — don't block primary results
                    logger.debug(
                        "search_memories legacy search failed for group=%s, continuing",
                        legacy_gid,
                    )

        logger.debug(
            "search_memories OK: query=%s cube=%s -> %d results",
            query[:80],
            mem_cube_id,
            len(results),
        )
        return results

    async def get_all_memories(
        self,
        cube_id: str,
        group_id: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get all memories via POST /product/get_all.

        Parses MemOS response format:
            {code, data: [{cube_id, memories: [{nodes: [{id, memory, metadata}]}]}]}
        and flattens into {text_mem: [{id, memory, metadata}], ...} for CLI compatibility.
        """
        payload: dict[str, Any] = {
            "user_id": group_id,
            "memory_type": "text_mem",
            "mem_cube_ids": [cube_id],
        }
        if filters:
            payload.update(filters)

        try:
            client = await self._get_client()
            resp = await client.post(
                "/product/get_all",
                json=payload,
                timeout=10.0,
            )
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()

            result: dict[str, Any] = {"text_mem": [], "act_mem": [], "para_mem": []}
            data_section = body.get("data", [])

            if isinstance(data_section, list):
                # MemOS format: data is a list of {cube_id, memories: [{nodes: [...]}]}
                for cube_data in data_section:
                    if not isinstance(cube_data, dict):
                        continue
                    for mem_group in cube_data.get("memories", []):
                        if not isinstance(mem_group, dict):
                            continue
                        for node in mem_group.get("nodes", []):
                            if isinstance(node, dict) and node.get("id"):
                                result["text_mem"].append(
                                    {
                                        "id": node["id"],
                                        "memory": node.get("memory", ""),
                                        "metadata": node.get("metadata", {}),
                                    }
                                )
            elif isinstance(data_section, dict):
                # Fallback: flat format {text_mem: [...], ...}
                for key in ("text_mem", "act_mem", "para_mem"):
                    result[key] = data_section.get(key, [])

            return result
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError):
            raise
        except httpx.HTTPStatusError:
            raise
        except Exception:
            raise

    # ------------------------------------------------------------------
    # Write methods — fault-tolerant (graceful degradation)
    # ------------------------------------------------------------------

    async def add_memory(
        self,
        group_id: str,
        mem_cube_id: str,
        messages: list[dict[str, str]],
        async_mode: str = "sync",
    ) -> dict[str, Any]:
        """Store memory via POST /product/add with messages format."""
        lock = self._get_group_lock(group_id)
        async with lock:
            payload: dict[str, Any] = {
                "user_id": group_id,
                "mem_cube_id": mem_cube_id,
                "messages": messages,
                "async_mode": async_mode,
            }
            try:
                client = await self._get_client()
                resp = await client.post("/product/add", json=payload)
                resp.raise_for_status()
                result: dict[str, Any] = resp.json()
                logger.debug("add_memory OK: group=%s cube=%s", group_id, mem_cube_id)
                return result
            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                logger.warning("MemOS add_memory network error: %s", exc)
                return {}
            except httpx.HTTPStatusError as exc:
                logger.warning("MemOS add_memory HTTP %d", exc.response.status_code)
                return {}
            except Exception:
                logger.exception("MemOS add_memory unexpected error")
                return {}

    async def add_structured_memory(
        self,
        cube_id: str,
        group_id: str,
        items: list[StructuredMemoryItem],
        async_mode: str = "sync",
    ) -> dict[str, Any]:
        """Store structured memory items via POST /product/add."""
        if not items:
            return {}

        lock = self._get_group_lock(group_id)
        async with lock:
            results: list[dict[str, Any]] = []
            memory_ids: list[str] = []
            for item in items:
                payload: dict[str, Any] = {
                    "user_id": group_id,
                    "mem_cube_id": cube_id,
                    "memory_content": item.memory,
                    "async_mode": async_mode,
                }
                try:
                    client = await self._get_client()
                    resp = await client.post("/product/add", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    results.append(data)
                    # G4 fix: capture written node id for caller linkage
                    # (feedback provenance / dedup). Response: {data: [{memory_id}]}
                    item_data = data.get("data") or []
                    if isinstance(item_data, list) and item_data and isinstance(item_data[0], dict):
                        mid = item_data[0].get("memory_id")
                        if mid:
                            memory_ids.append(str(mid))
                    logger.debug(
                        "add_structured_memory OK: cube=%s memory=%s...",
                        cube_id,
                        item.memory[:50],
                    )
                except (
                    httpx.TimeoutException,
                    httpx.ConnectError,
                    httpx.RemoteProtocolError,
                ) as exc:
                    logger.warning("MemOS add_structured_memory network error: %s", exc)
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "MemOS add_structured_memory HTTP %d",
                        exc.response.status_code,
                    )
                except Exception:
                    logger.exception("MemOS add_structured_memory unexpected error")

            if not results:
                return {}
            return {
                "added": len(results),
                "total": len(items),
                "memory_ids": memory_ids,
            }

    # ------------------------------------------------------------------
    # MemFeedback — native correction (MemOS 2.0+)
    # ------------------------------------------------------------------

    async def feedback_memory(
        self,
        group_id: str,
        cube_ids: list[str],
        feedback_content: str,
        retrieved_memory_ids: list[str],
        history: list[dict[str, str]] | None = None,
        async_mode: str = "sync",
        corrected_answer: bool = False,
        session_id: str = "default_session",
    ) -> dict[str, Any]:
        """Correct memories via native POST /product/feedback (MemOS 2.0+).

        MemFeedback understands the natural-language correction, locates the
        conflicting memory (targeted by retrieved_memory_ids), archives the old
        node and writes a corrected one — preserving version history
        (new.working_binding / shared key ↔ archived_id). Vector cleanup is
        handled server-side (vector_sync=success, no stale-vector hack).

        Args:
            group_id: MemOS user_id (per-group isolation).
            cube_ids: writable cubes — where the corrected memory lands.
            feedback_content: natural-language correction ("不对,应为X").
            retrieved_memory_ids: memory ids from the prior RAG turn — the
                precise correction target. Strongly recommended; without it
                MemOS re-searches (slow, error-prone).
            history: chat history so the LLM can judge intent.
            async_mode: "sync" (block) or "async" (background).
            corrected_answer: also return a corrected natural-language answer.
            session_id: soft-filter scope.

        Returns:
            On success: ``{"added": [...], "updated": [...], "new_ids": [...],
            "archived_ids": [...], "raw": <server record>}`` where each
            ``updated`` entry is ``{id, text, archived_id, origin_memory}``.
            On failure: ``{"status": "error", "error": ...}``.
        """
        payload: dict[str, Any] = {
            "user_id": group_id,
            "writable_cube_ids": cube_ids,
            "feedback_content": feedback_content,
            "retrieved_memory_ids": retrieved_memory_ids,
            "history": history or [],
            "async_mode": async_mode,
            "corrected_answer": corrected_answer,
            "session_id": session_id,
        }
        lock = self._get_group_lock(group_id)
        async with lock:
            try:
                client = await self._get_client()
                resp = await client.post("/product/feedback", json=payload)
                resp.raise_for_status()
                body: dict[str, Any] = resp.json()
                # record lives in body["data"][0]["record"]
                data_list = body.get("data") or []
                record: dict[str, Any] = {}
                if isinstance(data_list, list) and data_list and isinstance(data_list[0], dict):
                    record = data_list[0].get("record", {}) or {}
                added = record.get("add", []) or []
                updated = record.get("update", []) or []
                new_ids = [u["id"] for u in updated if isinstance(u, dict) and u.get("id")]
                new_ids += [a["id"] for a in added if isinstance(a, dict) and a.get("id")]
                archived_ids = [
                    u["archived_id"]
                    for u in updated
                    if isinstance(u, dict) and u.get("archived_id")
                ]
                logger.info(
                    "feedback_memory OK: group=%s new=%d archived=%d",
                    group_id,
                    len(new_ids),
                    len(archived_ids),
                )
                return {
                    "added": added,
                    "updated": updated,
                    "new_ids": new_ids,
                    "archived_ids": archived_ids,
                    "raw": record,
                }
            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                logger.warning("MemOS feedback_memory network error: %s", exc)
                return {"status": "error", "error": str(exc)}
            except httpx.HTTPStatusError as exc:
                logger.warning("MemOS feedback_memory HTTP %d", exc.response.status_code)
                return {"status": "error", "error": f"HTTP {exc.response.status_code}"}
            except Exception:
                logger.exception("MemOS feedback_memory unexpected error")
                return {"status": "error", "error": "unexpected"}

    async def get_memory(self, memory_id: str, group_id: str | None = None) -> MemoryResult | None:
        """Fetch a single memory by id via GET /product/get_memory/{id} (2.0+).

        Pairs with the node_id captured by add_structured_memory /
        feedback_memory for precise provenance lookup.

        Returns:
            MemoryResult, or None if not found / unreachable.
        """
        try:
            client = await self._get_client()
            resp = await client.get(f"/product/get_memory/{memory_id}", timeout=15.0)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()
            data = body.get("data") or {}
            if not isinstance(data, dict) or not data.get("id"):
                return None
            return MemoryResult(
                id=data.get("id", memory_id),
                memory=data.get("memory", ""),
                metadata=data.get("metadata", {}) or {},
                score=float(data.get("score", 0.0)),
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.warning("MemOS get_memory network error: %s", exc)
            return None
        except Exception:
            logger.exception("MemOS get_memory unexpected error")
            return None

    # ------------------------------------------------------------------
    # MemoryHandler — delete
    # ------------------------------------------------------------------

    async def delete_memory(
        self,
        cube_id: str,
        group_id: str,
        memory_ids: list[str] | None = None,
        file_ids: list[str] | None = None,
        filter: dict[str, Any] | None = None,
    ) -> bool:
        """Delete memories via POST /product/delete_memory."""
        lock = self._get_group_lock(group_id)
        async with lock:
            payload: dict[str, Any] = {
                "writable_cube_ids": [cube_id],
                "user_id": group_id,
            }
            if memory_ids:
                payload["memory_ids"] = memory_ids
            if file_ids:
                payload["file_ids"] = file_ids
            if filter:
                payload["filter"] = filter
            try:
                client = await self._get_client()
                resp = await client.post("/product/delete_memory", json=payload)
                resp.raise_for_status()
                logger.info(
                    "delete_memory OK: cube=%s group=%s ids=%s",
                    cube_id,
                    group_id,
                    memory_ids,
                )
                return True
            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
            ) as exc:
                logger.warning("MemOS delete_memory network error: %s", exc)
                return False
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "MemOS delete_memory HTTP %d",
                    exc.response.status_code,
                )
                return False
            except Exception:
                logger.exception("MemOS delete_memory unexpected error")
                return False

    # ------------------------------------------------------------------
    # SchedulerHandler — status + wait
    # ------------------------------------------------------------------

    async def scheduler_status(self, user_name: str) -> dict[str, Any]:
        """Check scheduler status via GET /product/scheduler/status."""
        try:
            client = await self._get_client()
            resp = await client.get(
                "/product/scheduler/status",
                params={"user_name": user_name},
            )
            resp.raise_for_status()
            return cast(dict[str, Any], resp.json())
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            return {"status": "error", "error": str(exc)}
        except Exception:
            return {"status": "error", "error": "unexpected"}

    async def scheduler_wait(
        self,
        user_name: str,
        timeout_seconds: float = 120.0,
        poll_interval: float = 0.2,
    ) -> dict[str, Any]:
        """Wait for scheduler to complete via POST /product/scheduler/wait."""
        try:
            client = await self._get_client()
            resp = await client.post(
                "/product/scheduler/wait",
                params={
                    "user_name": user_name,
                    "timeout_seconds": timeout_seconds,
                    "poll_interval": poll_interval,
                },
            )
            resp.raise_for_status()
            return cast(dict[str, Any], resp.json())
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            return {"status": "error", "error": str(exc)}
        except httpx.HTTPStatusError as exc:
            return {"status": "error", "error": f"HTTP {exc.response.status_code}"}
        except Exception:
            return {"status": "error", "error": "unexpected"}

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> dict[str, Any]:
        """Check MemOS backend health — HTTP server + search pipeline validation.

        M.5.1: 增加搜索管线探测。当 embedding API key 为空时，scheduler 静默失败，
        导致 get_all 有数据但 search 返回空。此探测可提前发现该问题。
        """
        t0: float = time.monotonic()
        try:
            client = await self._get_client()
            resp = await client.get("/openapi.json", timeout=5.0)
            latency_ms: float = (time.monotonic() - t0) * 1000.0
            resp.raise_for_status()

            # M.5.1: 搜索管线验证 — 轻量探测
            search_ok, search_error = False, ""
            try:
                probe = await client.post(
                    "/product/search",
                    json={
                        "query": "__health_probe__",
                        "user_id": "__health__",
                        "mem_cube_id": "",
                        "top_k": 1,
                    },
                    timeout=5.0,
                )
                search_ok = probe.status_code == 200
                if not search_ok:
                    search_error = f"HTTP {probe.status_code}"
            except Exception as exc:
                search_error = str(exc)

            result: dict[str, Any] = {"status": "ok", "latency_ms": round(latency_ms, 1)}
            result["search_status"] = "ok" if search_ok else "degraded"
            if not search_ok:
                result["search_error"] = search_error
                logger.warning("MemOS health_check: 搜索管线降级 — %s", search_error)
            return result
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return {"status": "error", "latency_ms": round(latency_ms, 1), "error": str(exc)}
        except Exception:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return {"status": "error", "latency_ms": round(latency_ms, 1), "error": "unexpected"}
