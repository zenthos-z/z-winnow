"""lark-cli subprocess wrapper for Feishu Bitable operations.

Wraps the official ``@larksuite/cli`` (``lark-cli``) binary. Every Bitable
operation — creating a Base, probing fields, writing records — goes through
here as a subprocess call with JSON parsing and structured error handling.

Identity policy
---------------
All calls use ``--as user`` (the default). The Feishu app configured via
``lark-cli config init`` + ``auth login`` acts on behalf of the logged-in
user, so it can access every Base that user owns — no per-Base collaborator
dance, which matters because one deployment serves many groups' Bases.

Mock mode
---------
``LARK_CLI_MOCK=1`` (or passing ``mock=True``) short-circuits the subprocess
and returns canned JSON, so tests and dev run without ``lark-cli`` installed
or authenticated. The mock dispatcher mimics the real command surface enough
to exercise the uploader's framework-init and record-mapping logic.

This module is the single live integration point with lark-cli. (The legacy
direct-HTTP ``outputs/feishu.py`` adapter was removed.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from typing import Any, cast

logger = logging.getLogger(__name__)

# ============================================================
# Errors
# ============================================================


class LarkCliError(RuntimeError):
    """Raised when lark-cli exits non-zero or returns ``ok: false``."""

    def __init__(self, message: str, *, returncode: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.returncode = returncode
        self.payload = payload


# ============================================================
# Config: binary path + mock toggle
# ============================================================


def lark_bin() -> str:
    """Resolve the lark-cli binary path.

    Override with ``LARK_CLI_BIN`` env; defaults to ``lark-cli`` on PATH.
    """
    return os.environ.get("LARK_CLI_BIN", "lark-cli").strip() or "lark-cli"


def mock_mode(mock: bool | None = None) -> bool:
    """Whether to short-circuit subprocess calls with canned responses.

    Explicit ``mock`` arg wins; else ``LARK_CLI_MOCK`` env (1/true/yes/on).
    """
    if mock is not None:
        return mock
    return os.environ.get("LARK_CLI_MOCK", "").lower() in ("1", "true", "yes", "on")


# ============================================================
# Core runner
# ============================================================


async def run(
    args: list[str],
    *,
    timeout: float = 90.0,
    mock: bool | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Run a ``lark-cli base +<cmd>`` invocation and return parsed JSON.

    ``args`` is the full argv tail after ``lark-cli`` (e.g.
    ``["base", "+field-list", "--base-token", X, "--table-id", Y]``).
    ``--as user`` is injected unless the caller already passed ``--as``.

    Output is JSON by default (lark-cli's default format). We parse stdout;
    a top-level ``{"ok": false, "error": ...}`` is converted to
    :class:`LarkCliError`.

    Args:
        args: argv tail.
        timeout: subprocess timeout in seconds.
        mock: force mock mode on/off (else env-driven).

    Returns:
        Parsed JSON dict from lark-cli stdout.

    Raises:
        LarkCliError: non-zero exit, timeout, or ``ok: false`` payload.
    """
    # Inject --as user unless caller specified an identity.
    if not any(a == "--as" for a in args):
        args = [*args, "--as", "user"]

    if mock_mode(mock):
        logger.debug("lark-cli MOCK: %s", " ".join(shlex.quote(a) for a in args))
        return _mock_dispatch(args)

    cmd = [lark_bin(), *args]
    logger.debug("lark-cli exec (cwd=%s): %s", cwd, " ".join(shlex.quote(a) for a in cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise LarkCliError(
            f"lark-cli binary not found ({lark_bin()}). Install it or set LARK_CLI_BIN. "
            f"Set LARK_CLI_MOCK=1 for tests."
        ) from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise LarkCliError(f"lark-cli timed out after {timeout}s: {args}") from exc

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        raise LarkCliError(
            f"lark-cli exited {proc.returncode}: {stderr or stdout[:300]}",
            returncode=proc.returncode,
        )

    data = _parse_json(stdout)
    if isinstance(data, dict) and data.get("ok") is False:
        err = data.get("error") or {}
        raise LarkCliError(
            f"lark-cli error: {err.get('message', 'unknown')} "
            f"(type={err.get('type')} subtype={err.get('subtype')})",
            returncode=proc.returncode,
            payload=data,
        )
    return data if isinstance(data, dict) else {"ok": True, "data": data}


def _parse_json(stdout: str) -> Any:
    """Parse lark-cli stdout as JSON; tolerate leading/trailing noise."""
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # lark-cli occasionally prefixes logs; find the first '{' … last '}'.
        first = stdout.find("{")
        last = stdout.rfind("}")
        if first != -1 and last != -1 and last > first:
            try:
                return json.loads(stdout[first : last + 1])
            except json.JSONDecodeError:
                pass
        logger.warning("lark-cli stdout was not JSON (head=%r)", stdout[:200])
        return {"ok": True, "raw": stdout}


# ============================================================
# High-level Bitable operations
# ============================================================


async def url_resolve(url: str, *, mock: bool | None = None) -> dict[str, Any]:
    """Resolve a Base share URL into base_token / table_id / view_id."""
    return await run(
        ["base", "+url-resolve", "--url", url],
        mock=mock,
    )


async def base_create(
    name: str,
    table_name: str,
    fields: list[dict[str, Any]],
    *,
    folder_token: str | None = None,
    time_zone: str = "Asia/Shanghai",
    mock: bool | None = None,
) -> dict[str, Any]:
    """Create a new Base with one initial table + field schema.

    Returns lark-cli JSON (contains the new app_token and initial table_id).
    Use :func:`table_create` to add further tables to the same Base.
    """
    args = [
        "base",
        "+base-create",
        "--name",
        name,
        "--table-name",
        table_name,
        "--fields",
        json.dumps(fields, ensure_ascii=False),
        "--time-zone",
        time_zone,
    ]
    if folder_token:
        args += ["--folder-token", folder_token]
    return await run(args, mock=mock)


async def table_create(
    base_token: str,
    name: str,
    fields: list[dict[str, Any]],
    *,
    mock: bool | None = None,
) -> dict[str, Any]:
    """Add a data table (with field schema) to an existing Base."""
    return await run(
        [
            "base",
            "+table-create",
            "--base-token",
            base_token,
            "--name",
            name,
            "--fields",
            json.dumps(fields, ensure_ascii=False),
        ],
        mock=mock,
    )


async def field_list(
    base_token: str,
    table_id: str,
    *,
    mock: bool | None = None,
) -> list[dict[str, Any]]:
    """List fields of a table. Empty/short list ⇒ table not yet initialized."""
    res = await run(
        ["base", "+field-list", "--base-token", base_token, "--table-id", table_id],
        mock=mock,
    )
    return _extract_items(res)


async def record_batch_create(
    base_token: str,
    table_id: str,
    columns: list[str],
    rows: list[list[Any]],
    *,
    mock: bool | None = None,
) -> dict[str, Any]:
    """Batch-create records (column-oriented, max 200 rows/call).

    ``columns`` are writable field names in order; ``rows`` are parallel value
    arrays. Auto-chunked into ≤200-row batches. Returns created ``record_ids``
    (from the response ``record_id_list``) so callers can post-process records
    (e.g. upload attachments to specific rows).
    """
    table_id_resolved = table_id
    all_results: list[dict[str, Any]] = []
    record_ids: list[str] = []
    for i in range(0, max(len(rows), 1), 200):
        chunk = rows[i : i + 200]
        if not chunk and i > 0:
            break
        payload = {"fields": columns, "rows": chunk}
        res = await run(
            [
                "base",
                "+record-batch-create",
                "--base-token",
                base_token,
                "--table-id",
                table_id_resolved,
                "--json",
                json.dumps(payload, ensure_ascii=False),
            ],
            mock=mock,
        )
        all_results.append(res)
        record_ids.extend(_extract_record_ids(res))
    return {
        "ok": True,
        "batches": all_results,
        "rows_written": len(rows),
        "record_ids": record_ids,
    }


async def record_upload_attachment(
    base_token: str,
    table_id: str,
    record_id: str,
    field: str,
    file_name: str,
    *,
    cwd: str | None = None,
    mock: bool | None = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Upload a local file as an attachment into a record's attachment field.

    Attachment fields can't be written via ``record-batch-create``; this is the
    separate two-phase step. ``field`` is the attachment field name (or field ID).

    lark-cli refuses absolute ``--file`` paths (security: must be relative to
    cwd), so pass ``file_name`` as a bare filename and set ``cwd`` to the
    directory holding the file.

    ``timeout`` overrides ``run()``'s default 90s subprocess timeout — large
    files (>20MB) use lark-cli's multipart upload and can take many minutes, so
    callers should pass a size-aware value (see ``uploader._attachment_upload_timeout``).
    """
    return await run(
        [
            "base",
            "+record-upload-attachment",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--record-id",
            record_id,
            "--field-id",
            field,
            "--file",
            file_name,
        ],
        mock=mock,
        cwd=cwd,
        timeout=timeout,
    )


async def record_list(
    base_token: str,
    table_id: str,
    *,
    filter_json: str | None = None,
    field_id: list[str] | None = None,
    limit: int = 200,
    mock: bool | None = None,
) -> dict[str, Any]:
    """List records in a table, with optional filter.

    Uses ``+record-list`` under the hood. Returns full lark-cli response
    (items are under ``data.items`` or ``items``).
    """
    args = [
        "base",
        "+record-list",
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--limit",
        str(limit),
        "--format",
        "json",
    ]
    if filter_json:
        args += ["--filter-json", filter_json]
    if field_id:
        for f in field_id:
            args += ["--field-id", f]
    return await run(args, mock=mock)


async def record_delete(
    base_token: str,
    table_id: str,
    record_ids: list[str],
    *,
    mock: bool | None = None,
) -> dict[str, Any]:
    """Delete one or more records by ID.

    Uses ``+record-delete``. This is a high-risk-write operation; lark-cli
    prompts for confirmation unless ``--yes`` is passed.
    """
    args = [
        "base",
        "+record-delete",
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--yes",
    ]
    for rid in record_ids:
        args += ["--record-id", rid]
    return await run(args, mock=mock)


# ============================================================
# Helpers
# ============================================================


def _extract_items(res: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort extract a list of field/record items from lark-cli JSON.

    lark-cli normalizes Feishu API responses; items may live under
    ``data.items``, ``items``, or the response root.
    """
    if not isinstance(res, dict):
        return []
    for key in ("items",):
        if isinstance(res.get(key), list):
            return cast(list[dict[str, Any]], res[key])
    data = res.get("data")
    if isinstance(data, dict):
        for key in ("items", "fields"):
            if isinstance(data.get(key), list):
                return cast(list[dict[str, Any]], data[key])
    # Some responses nest under the API envelope.
    for key in ("fields",):
        if isinstance(res.get(key), list):
            return cast(list[dict[str, Any]], res[key])
    return []


def _extract_record_ids(res: dict[str, Any]) -> list[str]:
    """Extract record_id_list from a record-batch-create response."""
    for loc in (res, res.get("data") or {}):
        if isinstance(loc, dict):
            ids = loc.get("record_id_list")
            if isinstance(ids, list):
                return [str(x) for x in ids if x]
    return []


# ============================================================
# Mock dispatcher (tests / dev without lark-cli)
# ============================================================

_MOCK_STATE: dict[str, Any] = {}


def _mock_dispatch(args: list[str]) -> dict[str, Any]:
    """Return canned JSON shaped like lark-cli output for the given args."""
    joined = " ".join(args)
    cmd = args[1] if len(args) > 1 and args[0] == "base" else ""

    if cmd == "+base-create":
        # Stable pseudo-IDs derived from the base name for test determinism.
        name = _flag(args, "--name") or "MockBase"
        app_token = f"mockApp_{abs(hash(name)) % 100000:05d}"
        tbl = f"tblMock_{abs(hash(name + 't')) % 100000:05d}"
        _MOCK_STATE[app_token] = {tbl: []}
        # Mirror the REAL lark-cli base-create envelope: data.base.base_token + data.table.id
        return {
            "ok": True,
            "data": {
                "base": {
                    "base_token": app_token,
                    "name": name,
                    "url": f"https://example.feishu.cn/base/{app_token}",
                },
                "table": {"id": tbl, "name": _flag(args, "--table-name") or "Table1"},
                "created": True,
            },
        }
    if cmd == "+table-create":
        base = _flag(args, "--base-token") or "mockApp"
        tname = _flag(args, "--name") or "Table"
        tbl = f"tblMock_{abs(hash(base + tname)) % 100000:05d}"
        _MOCK_STATE.setdefault(base, {})[tbl] = []
        # Mirror the REAL lark-cli table-create envelope: data.table.id
        return {"ok": True, "data": {"table": {"id": tbl, "name": tname}}}
    if cmd == "+field-list":
        base = _flag(args, "--base-token") or "mockApp"
        tbl = _flag(args, "--table-id") or "tblMock"
        fields = _MOCK_STATE.get(base, {}).get(tbl, [])
        return {"ok": True, "items": fields, "data": {"items": fields}}
    if cmd == "+record-batch-create":
        n = len(_rows_from_args(args))
        # Mirror real lark-cli JSON envelope: data.record_id_list
        return {
            "ok": True,
            "data": {
                "record_id_list": [f"recMock_{i:05d}" for i in range(n)],
                "records_written": n,
            },
        }
    if cmd == "+record-upload-attachment":
        return {
            "ok": True,
            "data": {"file_token": "mockFile_" + (_flag(args, "--file") or "")[-8:]},
        }
    if cmd == "+url-resolve":
        url = _flag(args, "--url") or ""
        token = url.split("/base/")[-1].split("?")[0] if "/base/" in url else "mockApp"
        return {
            "ok": True,
            "data": {"base_token": token, "table_id": "tblFromUrl", "view_id": ""},
        }
    if cmd == "+record-list":
        # Mirror real lark-cli JSON envelope: data.record_id_list + data.data + data.fields
        return {
            "ok": True,
            "data": {
                "data": [],
                "fields": [],
                "field_id_list": [],
                "record_id_list": [],
                "has_more": False,
                "total": 0,
            },
        }
    if cmd == "+record-delete":
        # Mirror real lark-cli JSON envelope
        return {"ok": True, "data": {"record_id_list": ["recMock_deleted"]}}
    logger.debug("lark-cli MOCK unhandled cmd: %s", joined)
    return {"ok": True, "data": {}}


def _flag(args: list[str], name: str) -> str | None:
    try:
        i = args.index(name)
        return args[i + 1]
    except (ValueError, IndexError):
        return None


def _rows_from_args(args: list[str]) -> list[list[Any]]:
    raw = _flag(args, "--json")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rows = parsed.get("rows", []) if isinstance(parsed, dict) else []
    return cast(list[list[Any]], rows) if isinstance(rows, list) else []


def _mock_set_fields(base: str, table: str, fields: list[dict[str, Any]]) -> None:
    """Test helper: pre-seed a table's field list in mock state."""
    _MOCK_STATE.setdefault(base, {})[table] = fields


__all__ = [
    "LarkCliError",
    "base_create",
    "field_list",
    "lark_bin",
    "mock_mode",
    "record_batch_create",
    "record_delete",
    "record_list",
    "record_upload_attachment",
    "run",
    "table_create",
    "url_resolve",
]
