"""T-A8: Pipeline 集成 CLI 入口.

提供命令行工具:
- winnow ingest --date 2026-04-28 [--group NAME]  → 单天入库
- winnow trace --server-id <ID>                     → 溯源查询
- winnow export --start 20260401 --end 20260428    → JSONL 导出
- winnow rl export --start YYYY-MM-DD --end YYYY-MM-DD  → RL 数据集导出
- winnow rl validate --input PATH                       → RL 数据集验证
- winnow web [--port PORT] [--host HOST]                 → 启动 Web 控制面板 (T-W7-6)

Usage:
    poetry run winnow ingest --date 2026-04-28
    poetry run winnow trace --server-id 1234567890
    poetry run winnow export --start 20260401 --end 20260428
    poetry run winnow rl export --start 2026-04-20 --end 2026-04-28
    poetry run winnow rl validate --input data/rl_sample_v1.jsonl
    poetry run winnow web
    poetry run winnow memos status
    poetry run winnow memos rebuild --group X --from sqlite
    poetry run winnow memos vacuum --group X
    poetry run winnow memos export --group X --out path/
    poetry run winnow memos search --group X --query "..."
    poetry run winnow memos flush
    poetry run winnow group add --chatroom-id xxx@chatroom [--display-name NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import sys
from pathlib import Path

from z_winnow.pipeline.context import assemble_daily_context
from z_winnow.pipeline.database import init_database
from z_winnow.pipeline.ingest import ingest_day
from z_winnow.pipeline.provenance import (
    export_jsonl,
    get_provenance_chain,
    trace_message_to_topics,
)
from z_winnow.rl.exporter import append_signal, export_rl_dataset
from z_winnow.rl.validator import validate_dataset

logger = logging.getLogger(__name__)


async def _resolve_group_identifier(group_input: str, db_path: str = "data/winnow.db") -> str:
    """Resolve a group identifier (group_id, chatroom_id, or display name) to group_id.

    If the input already looks like a group_id (g_xxx), return as-is.
    Otherwise try resolving via resolve_group_id (handles chatroom_id and display names).
    """
    if group_input.startswith("g_") and len(group_input) > 2:
        return group_input
    try:
        from z_winnow.pipeline.group_config import resolve_group_id

        resolved = await resolve_group_id(group_input, db_path=db_path)
        if resolved:
            return resolved
    except (ValueError, FileNotFoundError):
        pass
    return group_input


def _setup_logging(level: str = "INFO") -> None:
    """配置控制台日志输出。

    Args:
        level: 日志级别 (DEBUG, INFO, WARNING, ERROR)
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _get_env(key: str, default: str = "") -> str:
    """获取环境变量，优先从 .env 加载。

    Args:
        key: 环境变量名
        default: 默认值

    Returns:
        环境变量值或默认值
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    return os.getenv(key, default)


# ============================================================
# CLI 命令处理
# ============================================================


async def _cmd_ingest(args: argparse.Namespace) -> int:
    """执行 ingest 命令: 单天消息入库。

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    date = args.date.replace("-", "")  # 2026-04-28 → 20260428
    group_name = args.group or _get_env("WINNOW_GROUP_NAME", "")
    if not group_name:
        print("错误: 请用 --group 指定群聊名称或设置 WINNOW_GROUP_NAME 环境变量", file=sys.stderr)
        return 1

    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")

    from z_winnow.config.settings import get_settings

    settings = get_settings()
    base_url = args.base_url or settings.effective_data_base_url
    token = args.token or settings.effective_data_token

    # Resolve group_name → group_id for chatroom_id lookup
    group_id = ""
    try:
        from z_winnow.pipeline.group_config import resolve_group_id

        group_id = await resolve_group_id(group_name, db_path=db_path)
    except (ValueError, FileNotFoundError):
        pass

    print(f"正在从数据源获取 {group_name} 的 {date} 消息...")

    try:
        stats = await ingest_day(
            date=date,
            group_name=group_name,
            db_path=db_path,
            base_url=base_url,
            token=token,
            group_id=group_id,
        )
    except Exception as e:
        print(f"入库失败: {e}", file=sys.stderr)
        logger.exception("Ingest failed")
        return 1

    print(
        f"入库完成: 共 {stats['total']} 条, 新增 {stats['new']} 条, "
        f"更新 {stats['updated']} 条, 标记 {stats['sanitized']} 条, "
        f"跳过 {stats['skipped']} 条"
    )

    # 如果入库成功且有新消息，自动执行上下文组装
    if stats["total"] > 0:
        print(f"正在组装 {date} 的上下文...")
        try:
            contexts = await assemble_daily_context(date=date, db_path=db_path)
            print(f"上下文组装完成: {len(contexts)} 个上下文块")
        except Exception as e:
            print(f"上下文组装失败 (非致命): {e}", file=sys.stderr)
            logger.exception("Context assembly failed")

    return 0


async def _cmd_trace(args: argparse.Namespace) -> int:
    """执行 trace 命令: serverID 全链路溯源。

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    server_id = args.server_id
    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")

    # 初始化数据库（如果不存在）
    await init_database(db_path)

    # 正向溯源
    chain = await get_provenance_chain(server_id=server_id, db_path=db_path)

    if chain["message"] is None:
        print(f"未找到 serverID: {server_id}")
        return 1

    # 反向溯源
    reverse = await trace_message_to_topics(server_id=server_id, db_path=db_path)

    # 输出结果
    print(f"\n=== serverID: {server_id} ===\n")

    print("--- Layer 1: 原始消息 ---")
    msg = chain["message"]
    if msg:
        print(f"  日期: {msg.get('date', 'N/A')}")
        print(f"  发送者: {msg.get('sender', 'N/A')}")
        print(f"  内容: {msg.get('content', 'N/A')[:200]}")
        if msg.get("sanitized"):
            print("  ⚠️  已标记为潜在注入 (sanitized=1)")
    else:
        print("  (无)")

    print(f"\n--- Layer 2: 上下文块 ({len(chain['contexts'])} 个) ---")
    for ctx in chain["contexts"]:
        print(f"  context_id: {ctx.get('context_id', 'N/A')}")
        print(f"  token_count: {ctx.get('token_count', 'N/A')}")

    print(f"\n--- Layer 3: 议题总结 ({len(chain['topic_summaries'])} 个) ---")
    for ts in chain["topic_summaries"]:
        print(f"  topic: {ts.get('topic_name', 'N/A')}")
        print(f"  date: {ts.get('date', 'N/A')}")
        print(f"  confidence: {ts.get('confidence', 'N/A')}")

    print(f"\n--- 反向溯源: 被以下议题引用 ({len(reverse['referenced_by_topics'])} 个) ---")
    for topic in reverse["referenced_by_topics"]:
        print(f"  - {topic.get('topic_name', 'N/A')} (date: {topic.get('date', 'N/A')})")

    return 0


async def _cmd_export(args: argparse.Namespace) -> int:
    """执行 export 命令: JSONL 导出。

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    start_date = args.start.replace("-", "")
    end_date = args.end.replace("-", "")
    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")
    output_path = args.output or "data/rl_export.jsonl"

    print(f"正在导出 {start_date} ~ {end_date} 的议题数据...")

    try:
        count = await export_jsonl(
            start_date=start_date,
            end_date=end_date,
            output_path=output_path,
            db_path=db_path,
        )
    except Exception as e:
        print(f"导出失败: {e}", file=sys.stderr)
        logger.exception("Export failed")
        return 1

    print(f"导出完成: {count} 条记录 → {output_path}")
    return 0


# ============================================================
# RL 子命令处理 (T-I3)
# ============================================================


def _cmd_rl_export(args: argparse.Namespace) -> int:
    """执行 rl export 命令: RL 数据集 JSONL 导出。

    组合 extract_records + compute_reward + 写入 JSONL,
    输出数据集文件和统计信息。

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    start_date = args.start
    end_date = args.end
    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")
    output_path = args.output or "data/rl_dataset.jsonl"
    scenario_filter = args.scenario or "all"
    skip_reward = args.no_reward

    print(f"正在提取 RL 训练数据: {start_date} ~ {end_date}")
    print(f"  数据库: {db_path}")
    print(f"  输出: {output_path}")

    try:
        out_path, record_count, report = export_rl_dataset(
            start_date=start_date,
            end_date=end_date,
            db_path=db_path,
            output_path=output_path,
            scenario_filter=scenario_filter,
            skip_reward=skip_reward,
        )
    except Exception as e:
        print(f"RL 导出失败: {e}", file=sys.stderr)
        logger.exception("RL export failed")
        return 1

    print("\n导出完成:")
    print(f"  总记录数: {record_count}")
    print(f"  日期范围: {report.get('date_range', 'N/A')}")
    print("  场景分布:")
    for scenario, info in report.get("scenario_distribution", {}).items():
        count = info["count"] if isinstance(info, dict) else info
        pct = info.get("percentage", 0.0) if isinstance(info, dict) else 0.0
        print(f"    {scenario}: {count} ({pct:.1f}%)")
    reward_stats = report.get("reward_stats", {})
    if reward_stats.get("computed"):
        print(f"  Reward 计算: 已启用 (mean={reward_stats.get('mean', 0):.3f})")
    else:
        print("  Reward 计算: 已跳过 (--no-reward)")
    print(f"  输出文件: {out_path}")
    quality_score = report.get("quality_score", 0)
    print(f"  质量评分: {quality_score}")

    return 0


def _cmd_rl_validate(args: argparse.Namespace) -> int:
    """执行 rl validate 命令: 数据集质量验证。

    逐行解析 JSONL，进行 Pydantic schema 验证、分布检查、
    缺失值/异常值检查，输出验证报告。

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code (0=PASSED, 1=FAILED)
    """
    input_path = args.input

    print(f"正在验证 RL 数据集: {input_path}")

    try:
        report = validate_dataset(input_path)
    except FileNotFoundError as e:
        print(f"文件未找到: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"验证失败: {e}", file=sys.stderr)
        logger.exception("Validate dataset failed")
        return 1

    # 打印验证报告
    print(f"\nDataset: {report['file_path']}")
    print(f"Total records: {report['total_records']}")
    if report.get("date_range"):
        print(f"Date range: {report['date_range'][0]} ~ {report['date_range'][1]}")
    print("Scenario distribution:")
    for scenario, info in report.get("scenario_distribution", {}).items():
        print(f"  {scenario}: {info['count']} ({info['pct']:.1f}%)")

    if report.get("reward_stats"):
        rs = report["reward_stats"]
        print("Reward stats:")
        print(f"  mean: {rs.get('mean', 0):.3f}, std: {rs.get('std', 0):.3f}")
        if rs.get("dimensions"):
            for dim, d_stats in rs["dimensions"].items():
                print(
                    f"  {dim}: mean={d_stats.get('mean', 0):.3f}, std={d_stats.get('std', 0):.3f}"
                )

    issues = report.get("issues", [])
    if issues:
        print(f"\nIssues ({len(issues)}):")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("\nIssues: 无")

    status = report.get("status", "UNKNOWN")
    print(f"\nStatus: {status}")

    return 0 if status == "PASSED" else 1


# ============================================================
# web 子命令 (T-W7-6): 启动 Web 控制面板
# ============================================================


def _cmd_web(args: argparse.Namespace) -> int:
    """执行 web 命令: 启动 FastAPI Web 控制面板。

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    import uvicorn

    from z_winnow.config.settings import get_settings
    from z_winnow.web import runtime
    from z_winnow.web.app import app

    settings = get_settings()

    # P009: default-None, only override non-None values
    # Explicit None check instead of `or` to avoid 0-port falsy pitfall
    port = args.port if args.port is not None else settings.web_port
    host = args.host if args.host is not None else settings.web_host
    db_path = args.db or settings.sqlite_db_path

    os.environ.setdefault("SQLITE_DB_PATH", db_path)
    os.environ.setdefault("WEB_PORT", str(port))

    print(f"启动 Web 控制面板: http://{host}:{port}")
    print(f"数据库: {db_path}")
    print("按 Ctrl+C 停止")

    # 用 uvicorn.Server（保留引用）替代 uvicorn.run，以支持「保存并重启」：
    # 设置页 PUT /system/config → runtime.request_restart() + BackgroundTask 翻 should_exit
    # → server.run() 返回（uvicorn 已释放监听端口）→ 若 take_restart_flag() 则 os.execv 重启。
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    runtime.register_server(server)
    server.run()  # 阻塞至 should_exit；返回前 uvicorn 已关闭监听 socket + 跑完 lifespan shutdown
    if runtime.take_restart_flag():
        print("应用配置变更，重启中…", flush=True)
        os.execv(sys.executable, [sys.executable, *sys.argv])
        print("重启失败：os.execv 未生效，请手动重启", file=sys.stderr)
        return 1
    return 0


# ============================================================
# memos 子命令组 (T-W10-E-g): MemOS 运维和监控界面
# ============================================================


async def _cmd_memos_status(args: argparse.Namespace) -> int:
    """执行 memos status 命令: 显示 MemOS 服务健康状态。

    P016: 通过 create_memos_adapter() 获取 adapter.
    A008: adapter = None 在 try 前显式初始化.
    B4: MemOS 不可用时显示红色警告而非 crash.

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    # P016: 惰性导入 + 单点插入
    from z_winnow.memory.factory import create_memos_adapter

    # A008: 在 try 前显式初始化 adapter = None
    adapter = None
    try:
        adapter = create_memos_adapter()
    except Exception as e:
        print(f"MemOS adapter 创建失败: {e}", file=sys.stderr)
        return 1

    # --- Health Check ---
    # A008: health = None 在 try 前
    health = None
    try:
        health = await adapter.health_check()
    except Exception as e:
        health = {"status": "error", "error": str(e)}

    status_val = health.get("status", "unknown")
    latency_ms = health.get("latency_ms", "N/A")

    # Status 灯: green/yellow/red
    if status_val in ("healthy", "ok", "mock"):
        status_display = f"GREEN ({status_val})"
    elif status_val in ("disabled",):
        status_display = f"RED ({status_val}) — MemOS 不可用"
    else:
        status_display = f"YELLOW ({status_val})"

    print(f"MemOS 服务状态: {status_display}")
    print(f"  延迟: {latency_ms}ms")
    print()

    # --- 各 cube 节点数 ---
    group_name = getattr(args, "group", None)
    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")
    if group_name:
        group_name = await _resolve_group_identifier(group_name, db_path)

    # P016: 惰性导入 aiosqlite
    import aiosqlite

    cube_stats: list[dict] = []
    try:
        async with aiosqlite.connect(db_path) as db:
            # 查询所有 group 的 cube 信息
            if group_name:
                rows = await db.execute_fetchall(
                    "SELECT DISTINCT cube_id FROM memos_sync_queue WHERE cube_id LIKE ?",
                    (f"%{group_name}%",),
                )
            else:
                rows = await db.execute_fetchall("SELECT DISTINCT cube_id FROM memos_sync_queue")

            for row in rows:
                cube_id = row[0]
                # Extract group_id from cube_id: "winnow:{group}:..." → group
                parts = cube_id.split(":")
                cube_group = parts[1] if len(parts) >= 2 else "winnow"
                # P024: 独立 try/except 每个 cube 查询
                try:
                    memories = await adapter.get_all_memories(cube_id=cube_id, group_id=cube_group)
                    text_count = len(memories.get("text_mem", []))
                    act_count = len(memories.get("act_mem", []))
                    para_count = len(memories.get("para_mem", []))
                    total = text_count + act_count + para_count
                    cube_stats.append(
                        {
                            "cube_id": cube_id,
                            "text_nodes": text_count,
                            "act_nodes": act_count,
                            "para_nodes": para_count,
                            "total_nodes": total,
                        }
                    )
                except Exception as e:
                    cube_stats.append(
                        {
                            "cube_id": cube_id,
                            "text_nodes": "?",
                            "act_nodes": "?",
                            "para_nodes": "?",
                            "total_nodes": "?",
                            "error": str(e),
                        }
                    )
    except Exception as e:
        print(f"  数据库连接失败: {e}", file=sys.stderr)

    if cube_stats:
        print("Cube 节点统计:")
        for cs in cube_stats:
            error_info = f" [错误: {cs['error']}]" if cs.get("error") else ""
            print(
                f"  {cs['cube_id']}: "
                f"text={cs['text_nodes']}, act={cs['act_nodes']}, "
                f"para={cs['para_nodes']}, total={cs['total_nodes']}{error_info}"
            )
    else:
        print("  (无 cube 数据)")
    print()

    # --- Sync queue 积压 ---
    try:
        from z_winnow.pipeline.database import get_sync_queue_stats

        async with aiosqlite.connect(db_path) as db:
            stats = await get_sync_queue_stats(db)
        print("Sync Queue 状态:")
        print(f"  pending: {stats.get('pending', 0)}")
        print(f"  processing: {stats.get('processing', 0)}")
        print(f"  done: {stats.get('done', 0)}")
        print(f"  failed: {stats.get('failed', 0)}")
        print(f"  total: {stats.get('total', 0)}")
        if stats.get("pending", 0) > 1000:
            print("  警告: 积压任务超过 1000，请考虑执行 memos flush")
    except Exception as e:
        print(f"  Sync queue 查询失败: {e}")

    return 0


async def _cmd_memos_rebuild(args: argparse.Namespace) -> int:
    """执行 memos rebuild 命令: 从 SQLite 全量重建 cube.

    P016: 通过 create_memos_adapter() 获取 adapter.
    A008: adapter = None 在 try 前显式初始化.
    B2: 插入 N 条记录 → rebuild → 验证 cube 节点数 = N.

    从 SQLite 读取 topic_summaries / parsed_contexts / report_versions,
    转换为 StructuredMemoryItem, 批量写入 MemOS cube.

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    import aiosqlite

    from z_winnow.memory.factory import create_memos_adapter
    from z_winnow.memory.types import StructuredMemoryItem, TextualMemoryMetadata

    group_name = args.group
    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")
    group_name = await _resolve_group_identifier(group_name, db_path)

    # A008: adapter = None 在 try 前
    adapter = None
    try:
        adapter = create_memos_adapter()
    except Exception as e:
        print(f"MemOS adapter 创建失败: {e}", file=sys.stderr)
        return 1

    # 获取或创建 cube
    cube_id = await adapter.get_or_create_cube(f"{group_name}:topics")
    print(f"重建目标 cube: {cube_id} (scope={group_name}:topics)")

    items: list[StructuredMemoryItem] = []
    sqlite_record_count = 0

    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            # 读取 core_topics (表含 group_id, 是群组相关的核心议题)
            cursor = await db.execute(
                "SELECT * FROM core_topics WHERE group_id = ? ORDER BY last_matched_date",
                (group_name,),
            )
            topic_rows = await cursor.fetchall()
            for row in topic_rows:
                row_dict = dict(row)
                name = row_dict.get("name", "")
                desc = row_dict.get("description", "") or ""
                items.append(
                    StructuredMemoryItem(
                        memory=desc or name,
                        metadata=TextualMemoryMetadata(
                            type="topic",
                            source=f"sqlite:core_topics:{row_dict.get('last_matched_date', '')}",
                            confidence=80.0,
                            entities=[],
                            tags=[group_name, name, "core_topic"],
                            visibility="private",
                            memory_time=row_dict.get("last_matched_date", ""),
                            updated_at=datetime.datetime.now().isoformat(),
                        ),
                    )
                )
                sqlite_record_count += 1

            # 读取 report_versions
            cursor = await db.execute(
                "SELECT * FROM report_versions WHERE group_id = ? ORDER BY date",
                (group_name,),
            )
            rv_rows = await cursor.fetchall()
            for row in rv_rows:
                row_dict = dict(row)
                content = row_dict.get("content", "") or ""
                items.append(
                    StructuredMemoryItem(
                        memory=content[:2000],
                        metadata=TextualMemoryMetadata(
                            type="topic",
                            source=f"sqlite:report_versions:{row_dict.get('date', '')}",
                            confidence=80.0,
                            entities=[],
                            tags=[group_name, "report"],
                            visibility="private",
                            memory_time=row_dict.get("date", ""),
                            updated_at=datetime.datetime.now().isoformat(),
                        ),
                    )
                )
                sqlite_record_count += 1

    except Exception as e:
        print(f"SQLite 读取失败: {e}", file=sys.stderr)
        logger.exception("Rebuild read failed")
        return 1

    print(f"从 SQLite 读入 {sqlite_record_count} 条记录，准备写入 MemOS...")

    # 批量写入 MemOS (分批 50 条)
    batch_size = 50
    total_written = 0
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        try:
            result = await adapter.add_structured_memory(
                cube_id=cube_id,
                group_id=group_name,
                items=batch,
            )
            stored = result.get("data", {}).get("stored", len(batch))
            total_written += stored
            print(f"  批次 {i // batch_size + 1}: 写入 {stored} 条")
        except Exception as e:
            print(f"  批次 {i // batch_size + 1} 写入失败: {e}", file=sys.stderr)

    print(f"重建完成: SQLite={sqlite_record_count}, MemOS written={total_written}")
    return 0


async def _cmd_memos_vacuum(args: argparse.Namespace) -> int:
    """执行 memos vacuum 命令: 触发生命周期状态机.

    P016: 通过 create_memos_adapter() 获取 adapter.
    A008: adapter = None 在 try 前显式初始化.

    扫描所有 memories, 应用生命周期规则:
    - confidence < 20 且 status="activated" → archive
    - status="archived" 超过 30 天 → delete

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """

    # A008: adapter = None 在 try 前
    print("此功能尚未实现 — 等待 MemOS handler 集成", file=sys.stderr)
    return 0


async def _cmd_memos_export(args: argparse.Namespace) -> int:
    """执行 memos export 命令: dump cube 到文件.

    P016: 通过 create_memos_adapter() 获取 adapter.
    A008: adapter = None 在 try 前显式初始化.

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    import json
    from pathlib import Path

    from z_winnow.memory.factory import create_memos_adapter

    group_name = await _resolve_group_identifier(args.group)
    out_path = Path(args.out)
    out_path.mkdir(parents=True, exist_ok=True)

    adapter = None
    try:
        adapter = create_memos_adapter()
    except Exception as e:
        print(f"MemOS adapter 创建失败: {e}", file=sys.stderr)
        return 1

    total_exported = 0

    for suffix in [":topics", ":feedback"]:
        cube_id = await adapter.get_or_create_cube(f"{group_name}{suffix}")
        try:
            data = await adapter.get_all_memories(cube_id=cube_id, group_id=group_name)
        except Exception as e:
            print(f"查询 cube {cube_id} 失败: {e}", file=sys.stderr)
            continue

        text_count = len(data.get("text_mem", []))
        act_count = len(data.get("act_mem", []))
        para_count = len(data.get("para_mem", []))
        count = text_count + act_count + para_count

        if count == 0:
            print(f"  cube {cube_id}: 空，跳过")
            continue

        safe_suffix = suffix.lstrip(":")
        out_file = out_path / f"{group_name}_{safe_suffix}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        total_exported += count
        print(f"  cube {cube_id}: {count} 条记忆 → {out_file}")

    if total_exported == 0:
        print("未找到任何记忆数据")
    else:
        print(f"\n导出完成: 共 {total_exported} 条记忆 → {out_path}")

    return 0


async def _cmd_memos_delete_cube(args: argparse.Namespace) -> int:
    """删除指定 cube 的所有记忆.

    流程: 预览节点数 → 确认 → delete_memory(memory_ids=all_ids).
    安全设计: 默认要求交互式确认, --yes 跳过.

    Args:
        args: 解析后的命令行参数 (--group, --yes)

    Returns:
        exit code
    """
    import sys

    from z_winnow.memory.factory import create_memos_adapter

    group_name = await _resolve_group_identifier(args.group)

    adapter = None
    try:
        adapter = create_memos_adapter()
    except Exception as e:
        print(f"MemOS adapter 创建失败: {e}", file=sys.stderr)
        return 1

    # Delete both :topics and :feedback cubes
    cube_ids: list[str] = []
    for suffix in [":topics", ":feedback"]:
        cube_ids.append(await adapter.get_or_create_cube(f"{group_name}{suffix}"))

    total_all = 0
    all_ids_all: list[str] = []

    for cube_id in cube_ids:
        try:
            all_data = await adapter.get_all_memories(cube_id=cube_id, group_id=group_name)
        except Exception as e:
            print(f"查询 cube {cube_id} 失败: {e}", file=sys.stderr)
            continue

        text_count = len(all_data.get("text_mem", []))
        act_count = len(all_data.get("act_mem", []))
        para_count = len(all_data.get("para_mem", []))
        total = text_count + act_count + para_count

        print(f"cube: {cube_id}")
        print(f"  text_mem: {text_count} 条")
        print(f"  act_mem:  {act_count} 条")
        print(f"  para_mem: {para_count} 条")
        print(f"  总计: {total} 条")
        print()

        total_all += total
        for mem_type in ["text_mem", "act_mem", "para_mem"]:
            items = all_data.get(mem_type, [])
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    all_ids_all.append(item["id"])

    if total_all == 0:
        print("所有 cube 均为空，无需删除")
        return 0

    if not args.yes:
        try:
            confirm = input(f"确认删除 {total_all} 条记忆? (yes/no): ").strip().lower()
        except EOFError:
            confirm = "no"
        if confirm != "yes":
            print("已取消")
            return 0

    # Delete from all cubes
    for cube_id in cube_ids:
        ok = await adapter.delete_memory(
            cube_id=cube_id,
            group_id=group_name,
            memory_ids=all_ids_all,
        )
        if ok:
            print(f"已从 {cube_id} 删除记忆")
        else:
            print(f"从 {cube_id} 删除失败（delete_memory 返回 False）", file=sys.stderr)


async def _cmd_memos_wipe_all(args: argparse.Namespace) -> int:
    """全量清空所有群的 MemOS 记忆（开发调试用，不可逆）.

    流程: 列出所有已注册群 → 预览 → 确认 → wipe_all_memories.
    --include-local: 同时清空本地数据（保留 groups 注册行）.
    --yes: 跳过确认.

    Args:
        args: --yes, --include-local, --db

    Returns:
        exit code
    """
    import sys

    import aiosqlite

    from z_winnow.memory.factory import create_memos_adapter

    db_path = getattr(args, "db", None) or _get_env("SQLITE_DB_PATH", "data/winnow.db")

    # 1. list all registered group_ids
    group_ids: list[str] = []
    try:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute("SELECT group_id FROM groups ORDER BY group_id")
            rows = await cur.fetchall()
            group_ids = [r[0] for r in rows if r and r[0]]
    except Exception as e:
        print(f"读取群组列表失败 ({db_path}): {e}", file=sys.stderr)
        return 1

    if not group_ids:
        print("无已注册群组 — 无 MemOS 记忆可清。")
        return 0

    print(f"将对以下 {len(group_ids)} 个群清空 MemOS 记忆：")
    for gid in group_ids:
        print(f"  - {gid}")
    if args.include_local:
        print("[--include-local] 同时清空本地数据（保留 groups 注册行）")

    if not args.yes:
        try:
            confirm = input("\n确认全量清空？此操作不可逆 (yes/no): ").strip().lower()
        except EOFError:
            confirm = "no"
        if confirm != "yes":
            print("已取消")
            return 0

    # 2. create adapter
    try:
        adapter = create_memos_adapter()
    except Exception as e:
        print(f"MemOS adapter 创建失败: {e}", file=sys.stderr)
        return 1

    # 3. wipe MemOS memories across all groups
    from z_winnow.web.services.memos_service import wipe_all_memories

    summary = await wipe_all_memories(adapter, group_ids)
    print(
        f"\nMemOS 清空完成：群 {summary['groups']} | cube {len(summary['cubes'])} | "
        f"删除记忆 {summary['total_removed']} | all_ok={summary['all_ok']}"
    )

    # 4. optional local wipe (keeps groups rows)
    if args.include_local:
        from z_winnow.web.services.group_service import purge_group_local_data

        totals: dict[str, int] = {}
        async with aiosqlite.connect(db_path) as db:
            for gid in group_ids:
                counts = await purge_group_local_data(db, gid)
                for k, v in counts.items():
                    totals[k] = totals.get(k, 0) + (v if isinstance(v, int) else 0)
        print(f"本地数据清空：{totals}")

    return 0


async def _cmd_memos_purge_wxid(args: argparse.Namespace) -> int:
    """扫描并清理 MemOS 中含 wxid_ 的记忆节点.

    对每个群组的 :topics 和 :feedback cube 执行:
      1. get_all_memories() 获取全部节点
      2. 过滤 memory 文本中含 wxid_ 的节点
      3. --dry-run: 只报告统计，不删除
      4. 否则: delete_memory() 批量删除

    Args:
        args: --group (可选), --dry-run

    Returns:
        exit code
    """
    import sys

    from z_winnow.memory.factory import create_memos_adapter

    adapter = None
    try:
        adapter = create_memos_adapter()
    except Exception as e:
        print(f"MemOS adapter 创建失败: {e}", file=sys.stderr)
        return 1

    # Resolve target groups
    if args.group:
        group_name = await _resolve_group_identifier(args.group)
        groups = [group_name]
    else:
        # Scan all registered groups from database
        try:
            from z_winnow.config.settings import get_settings
            from z_winnow.pipeline.database import get_all_groups

            settings = get_settings()
            import aiosqlite

            async with aiosqlite.connect(settings.sqlite_db_path) as conn:
                conn.row_factory = aiosqlite.Row
                rows = await get_all_groups(conn)
                groups = [row["group_id"] for row in rows]
        except Exception as e:
            print(f"获取群组列表失败: {e}", file=sys.stderr)
            return 1

    if not groups:
        print("没有找到已注册的群组")
        return 0

    total_scanned = 0
    total_contaminated = 0
    total_deleted = 0

    for group_name in groups:
        for suffix in [":topics", ":feedback"]:
            cube_id = await adapter.get_or_create_cube(f"{group_name}{suffix}")
            try:
                all_data = await adapter.get_all_memories(cube_id=cube_id, group_id=group_name)
            except Exception as e:
                print(f"  跳过 cube {cube_id}: {e}", file=sys.stderr)
                continue

            all_items = all_data.get("text_mem", [])
            contaminated = [
                item
                for item in all_items
                if isinstance(item, dict) and "wxid_" in item.get("memory", "")
            ]

            total_scanned += len(all_items)
            total_contaminated += len(contaminated)

            if contaminated:
                print(f"\ncube: {cube_id}")
                print(f"  扫描: {len(all_items)} 条")
                print(f"  含 wxid_: {len(contaminated)} 条")
                for item in contaminated[:5]:
                    mem_preview = item.get("memory", "")[:80]
                    print(f"    - [{item.get('id', '?')[:12]}] {mem_preview}...")
                if len(contaminated) > 5:
                    print(f"    ... 还有 {len(contaminated) - 5} 条")

                if not args.dry_run:
                    ids_to_delete = [item["id"] for item in contaminated if item.get("id")]
                    if ids_to_delete:
                        ok = await adapter.delete_memory(
                            cube_id=cube_id,
                            group_id=group_name,
                            memory_ids=ids_to_delete,
                        )
                        if ok:
                            total_deleted += len(ids_to_delete)
                            print(f"  已删除: {len(ids_to_delete)} 条")
                        else:
                            print("  删除失败!", file=sys.stderr)
            else:
                print(f"cube {cube_id}: 干净 ({len(all_items)} 条)")

    print("\n--- 清理统计 ---")
    print(f"扫描节点: {total_scanned}")
    print(f"含 wxid_: {total_contaminated}")
    print(f"干净节点: {total_scanned - total_contaminated}")
    if args.dry_run:
        print("[DRY RUN] 未实际删除")
    else:
        print(f"已删除: {total_deleted}")

    return 0


async def _cmd_memos_search(args: argparse.Namespace) -> int:
    """执行 memos search 命令: 命令行查询调试.

    P016: 通过 create_memos_adapter() 获取 adapter.
    A008: adapter = None 在 try 前显式初始化.

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    from z_winnow.memory.factory import create_memos_adapter

    group_name = await _resolve_group_identifier(args.group)
    query = args.query
    top_k = getattr(args, "top_k", 20) or 20

    # A008: adapter = None 在 try 前
    adapter = None
    try:
        adapter = create_memos_adapter()
    except Exception as e:
        print(f"MemOS adapter 创建失败: {e}", file=sys.stderr)
        return 1

    topics_cube_id = await adapter.get_or_create_cube(f"{group_name}:topics")
    fb_cube_id = await adapter.get_or_create_cube(f"{group_name}:feedback")
    print(f"在 cube {topics_cube_id} 中搜索: {query}")

    results: list = []
    try:
        results = await adapter.search_memories(
            query=query,
            group_id=group_name,
            readable_cube_ids=[topics_cube_id],
            top_k=top_k,
        )
    except Exception as e:
        print(f"topics cube 搜索失败: {e}", file=sys.stderr)

    try:
        fb_results = await adapter.search_memories(
            query=query,
            group_id=group_name,
            readable_cube_ids=[fb_cube_id],
            top_k=top_k,
        )
        results.extend(fb_results)
    except Exception as e:
        print(f"feedback cube 搜索失败: {e}", file=sys.stderr)

    if not results:
        print("  (无结果)")
        return 0

    print(f"找到 {len(results)} 条结果:\n")
    for i, r in enumerate(results, 1):
        memory_text = r.memory[:120].replace("\n", " ")
        print(f"  [{i}] score={r.score:.3f} id={r.id}")
        print(f"      {memory_text}...")
        if r.metadata:
            tags = r.metadata.get("tags", [])
            mem_type = r.metadata.get("type", "")
            if tags or mem_type:
                print(f"      type={mem_type}, tags={tags}")
        print()

    return 0


async def _cmd_memos_flush(args: argparse.Namespace) -> int:
    """执行 memos flush 命令: 强制处理所有 pending sync 任务.

    P016: 惰性导入 sync_ops, 通过 create_memos_adapter() 获取 adapter.
    A008: adapter = None 在 try 前显式初始化.

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    import aiosqlite

    from z_winnow.memory.factory import create_memos_adapter
    from z_winnow.pipeline.database import (
        fetch_pending_jobs,
        get_sync_queue_stats,
        mark_done,
        mark_failed,
        mark_processing,
    )

    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")

    # A008: adapter = None 在 try 前
    adapter = None
    try:
        adapter = create_memos_adapter()
    except Exception as e:
        print(f"MemOS adapter 创建失败: {e}", file=sys.stderr)
        return 1

    # P016: 惰性导入 dispatch_op
    from z_winnow.memory.sync_ops import dispatch_op

    try:
        async with aiosqlite.connect(db_path) as db:
            # 先显示当前积压
            stats = await get_sync_queue_stats(db)
            print(
                f"当前 sync queue: pending={stats.get('pending', 0)}, "
                f"processing={stats.get('processing', 0)}, "
                f"failed={stats.get('failed', 0)}"
            )

            if stats.get("pending", 0) == 0:
                print("无 pending 任务，跳过 flush")
                return 0

            # 批量处理 pending jobs
            total_processed = 0
            total_errors = 0

            while True:
                jobs = await fetch_pending_jobs(db, limit=50)
                if not jobs:
                    break

                for job in jobs:
                    queue_id = job.get("queue_id", 0)

                    try:
                        await mark_processing(db, queue_id)
                        await dispatch_op(adapter=adapter, row=job)
                        await mark_done(db, queue_id)
                        total_processed += 1
                    except Exception as e:
                        total_errors += 1
                        import contextlib

                        with contextlib.suppress(Exception):
                            retry_count = int(job.get("retry_count", 0))
                            await mark_failed(db, queue_id, str(e), retry_count)
                        if total_errors <= 5:
                            print(f"  任务 {queue_id} 失败: {e}", file=sys.stderr)

                # 批次间短暂等待
                import asyncio

                await asyncio.sleep(0.1)

            # 最终统计
            final_stats = await get_sync_queue_stats(db)
            print(f"Flush 完成: processed={total_processed}, errors={total_errors}")
            print(
                f"最终状态: pending={final_stats.get('pending', 0)}, "
                f"done={final_stats.get('done', 0)}, "
                f"failed={final_stats.get('failed', 0)}"
            )

    except Exception as e:
        print(f"Flush 失败: {e}", file=sys.stderr)
        logger.exception("Flush failed")
        return 1

    return 0


# ============================================================
# judge 子命令 (T-W10-B-b): LLM-as-judge 报告评估
# ============================================================


async def _cmd_judge(args: argparse.Namespace) -> int:
    """执行 judge 命令: 用 LLM-as-judge 评估报告质量.

    支持三种模式:
      - 单日: --date 2026-05-15 --group X
      - 日期范围: --from 2026-05-01 --to 2026-05-15 --group X
      - 最近 N 份: --group X --latest N

    输出: 4 维度评分表 (completeness/accuracy/conciseness/actionability) + overall.
    --output json 则输出 JSON 行.

    P009: --from/--to/--latest 均为 default=None 可选参数.
    P007: --output json 时 JSON 解析失败 fallback 到 regex 提取.

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code (0=成功, 1=失败)
    """
    import aiosqlite

    from z_winnow.pipeline.database import init_database_in_conn
    from z_winnow.pipeline.report_version import find_versions
    from z_winnow.rl.llm_judge import ReportVersion, judge_report
    from z_winnow.rl.schema import RLRewardSignal

    # P009: 提取可选参数, default=None
    date = args.date
    from_date = getattr(args, "from_date", None)  # --from → dest='from_date'
    to_date = getattr(args, "to_date", None)  # --to → dest='to_date'
    group_id = args.group
    latest = getattr(args, "latest", None)
    output_fmt = getattr(args, "output", "table")

    # Validate: at least one mode of report selection
    has_date = date is not None
    has_range = from_date is not None or to_date is not None
    has_latest = latest is not None

    if not (has_date or has_range or has_latest):
        print(
            "错误: 请指定 --date、--from/--to 或 --latest 中的至少一种方式",
            file=sys.stderr,
        )
        return 1

    # Normalize date format (YYYY-MM-DD → YYYYMMDD)
    def _norm_date(d: str) -> str:
        return d.replace("-", "")

    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")

    # Connect to database
    try:
        async with aiosqlite.connect(db_path) as db:
            await init_database_in_conn(db)  # ensure schema exists

            # Query report_versions
            kwargs: dict[str, object] = {}
            if group_id:
                kwargs["group_id"] = group_id
            if date:
                kwargs["date"] = _norm_date(date)
            elif has_range or has_latest:
                if from_date:
                    kwargs["from_date"] = _norm_date(from_date)
                if to_date:
                    kwargs["to_date"] = _norm_date(to_date)
            if latest is not None:
                kwargs["limit"] = latest

            versions = await find_versions(db, **kwargs)  # type: ignore[arg-type]

            # B3: 无匹配报告 → 非零退出码 + 有意义错误消息
            if not versions:
                locator_parts: list[str] = []
                if date:
                    locator_parts.append(f"date={date}")
                if from_date:
                    locator_parts.append(f"from={from_date}")
                if to_date:
                    locator_parts.append(f"to={to_date}")
                if group_id:
                    locator_parts.append(f"group={group_id}")
                locator = ", ".join(locator_parts)
                print(
                    f"未找到匹配的报告: {locator}。请检查日期/群组是否正确，"
                    f"或确认数据库中存在对应 report_versions 记录。",
                    file=sys.stderr,
                )
                return 1

            # Process each report
            results: list[dict] = []
            for idx, ver in enumerate(versions):
                report = ReportVersion(
                    content=ver.content or "",
                    version_id=ver.version_id,
                    group_id=ver.group_id,
                    date=ver.date,
                )

                # P007: progress to stderr so JSON stdout stays clean
                print(
                    f"[{idx + 1}/{len(versions)}] 评估中: {ver.version_id} "
                    f"(group={ver.group_id}, date={ver.date})",
                    file=sys.stderr,
                )

                try:
                    judge_result = await judge_report(report)
                except Exception as e:
                    print(f"  评估失败: {e}", file=sys.stderr)
                    continue

                results.append(
                    {
                        "version_id": ver.version_id,
                        "group_id": ver.group_id,
                        "date": ver.date,
                        "overall": judge_result.overall,
                        "completeness": judge_result.completeness.score,
                        "accuracy": judge_result.accuracy.score,
                        "conciseness": judge_result.conciseness.score,
                        "actionability": judge_result.actionability.score,
                        "completeness_evidence": judge_result.completeness.evidence,
                        "accuracy_evidence": judge_result.accuracy.evidence,
                        "conciseness_evidence": judge_result.conciseness.evidence,
                        "actionability_evidence": judge_result.actionability.evidence,
                        "error": judge_result.error,
                        "raw_response": judge_result.raw_response,
                    }
                )

                # 写入 RL 信号
                signal_output = (
                    getattr(args, "signal_output", None) or "data/rl/judge_signals.jsonl"
                )
                timestamp = judge_result.judge_at
                for dim, label in [
                    ("completeness", "completeness"),
                    ("accuracy", "accuracy"),
                    ("conciseness", "conciseness"),
                    ("actionability", "actionability"),
                ]:
                    dim_score = getattr(judge_result, dim)
                    signal = RLRewardSignal(
                        signal_type="auto_comparison",
                        source="llm_judge",
                        value=dim_score.score,
                        dimension=label,
                        evidence=dim_score.evidence,
                        timestamp=timestamp,
                    )
                    try:
                        append_signal(signal_output, ver.version_id, signal)
                    except Exception as e:
                        logger.warning("Failed to append signal for %s: %s", ver.version_id, e)

            # --- Output ---
            # A007: 不打印裸 emoji, 纯文本替代, 使用 utf-8 reconfigure 保底
            import contextlib

            with contextlib.suppress(Exception):
                sys.stdout.reconfigure(encoding="utf-8")  # A007

            if output_fmt == "json":
                # P007: json_mode 输出, 每行一个 JSON 对象
                for r in results:
                    print(json.dumps(r, ensure_ascii=False))  # A007: ensure_ascii=False
            else:
                # Table output
                _print_judge_table(results)

            # Summary to stderr (keep stdout clean for JSON output)
            print(f"\n评估完成: {len(results)} 份报告", file=sys.stderr)

            return 0

    except aiosqlite.OperationalError as e:
        print(f"数据库错误: {e}", file=sys.stderr)
        logger.exception("Database error in judge command")
        return 1
    except Exception as e:
        print(f"judge 命令执行失败: {e}", file=sys.stderr)
        logger.exception("Judge command failed")
        return 1


def _print_judge_table(results: list[dict]) -> None:
    """打印 4 维度评分表 (纯文本, 无 emoji).

    A007: 禁止裸 print emoji, 使用纯文本替代.
    """
    if not results:
        return

    # Header
    header = f"{'Version ID':<24} {'Comp':>6} {'Acc':>6} {'Conc':>6} {'Act':>6} {'Overall':>8}"
    sep = "-" * len(header)

    print("\n" + header)
    print(sep)

    for r in results:
        line = (
            f"{r['version_id']:<24} "
            f"{r['completeness']:>6.2f} "
            f"{r['accuracy']:>6.2f} "
            f"{r['conciseness']:>6.2f} "
            f"{r['actionability']:>6.2f} "
            f"{r['overall']:>8.2f}"
        )
        print(line)

    # Evidence summary
    print("\n--- 评分依据摘要 ---")
    for r in results:
        print(f"\n{r['version_id']} (date={r['date']}, group={r['group_id']})")
        for dim in ["completeness", "accuracy", "conciseness", "actionability"]:
            evidence = r.get(f"{dim}_evidence", "")
            score = r.get(dim, 0.0)
            if evidence:
                print(f"  {dim}: [{score:.2f}] {evidence}")
            else:
                print(f"  {dim}: [{score:.2f}] (无依据)")


# ============================================================
# group 子命令: group_id ↔ chatroom_id 转换
# ============================================================


async def _cmd_group_list(args: argparse.Namespace) -> int:
    """列出所有已注册群组。"""
    import aiosqlite

    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")

    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT group_id, display_name, chatroom_id FROM groups ORDER BY group_id"
            )
            rows = await cursor.fetchall()
    except Exception as e:
        print(f"查询失败: {e}", file=sys.stderr)
        return 1

    if not rows:
        print("无已注册群组")
        return 0

    print(f"{'group_id':<24} {'display_name':<30} {'chatroom_id'}")
    print("-" * 80)
    for row in rows:
        print(f"{row['group_id']:<24} {row['display_name']:<30} {row['chatroom_id']}")
    print(f"\n共 {len(rows)} 个群组")
    return 0


async def _cmd_group_resolve(args: argparse.Namespace) -> int:
    """group_id ↔ chatroom_id 双向解析。"""
    from z_winnow.pipeline.group_config import resolve_chatroom_id, resolve_group_id

    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")
    group_id_input = getattr(args, "group_id", None)
    room_id_input = getattr(args, "room_id", None)
    name_input = getattr(args, "name", None)

    if not group_id_input and not room_id_input and not name_input:
        print("错误: 请指定 --group-id、--room-id 或 --name 之一", file=sys.stderr)
        return 1

    try:
        if group_id_input:
            chatroom_id = await resolve_chatroom_id(group_id_input, db_path=db_path)
            print(f"group_id:    {group_id_input}")
            print(f"chatroom_id: {chatroom_id}")
        elif room_id_input:
            gid = await resolve_group_id(room_id_input, db_path=db_path)
            print(f"chatroom_id: {room_id_input}")
            print(f"group_id:  {gid}")
        elif name_input:
            gid = await resolve_group_id(name_input, db_path=db_path)
            chatroom_id = await resolve_chatroom_id(gid, db_path=db_path)
            print(f"input:       {name_input}")
            print(f"group_id:    {gid}")
            print(f"chatroom_id: {chatroom_id}")
    except ValueError as e:
        print(f"解析失败: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"数据库错误: {e}", file=sys.stderr)
        return 1

    return 0


async def _cmd_group_add(args: argparse.Namespace) -> int:
    """注册新群组到 groups 表。

    幂等：如果 chatroom_id 已存在，打印已有记录并以 exit 0 返回。

    Args:
        args: 解析后的命令行参数 (--chatroom-id, --display-name)

    Returns:
        exit code
    """
    import uuid

    import aiosqlite

    from z_winnow.storage import Storage

    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")
    chatroom_id = args.chatroom_id.strip()
    display_name = (args.display_name or "").strip()

    # Auto-resolve display_name from CipherTalk when not provided
    if not display_name:
        try:
            from z_winnow.pipeline.cipher_talk_client import create_data_client

            async with create_data_client() as client:
                session = await client.find_session_by_room_id(chatroom_id)
                if session and session.get("displayName"):
                    display_name = session["displayName"]
                    print(f"  CipherTalk 解析群名: {display_name}")
        except Exception as e:
            print(f"  CipherTalk 解析群名失败: {e}", file=sys.stderr)
        if not display_name:
            display_name = chatroom_id

    # Duplicate check: chatroom_id already registered?
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT group_id, display_name FROM groups WHERE chatroom_id = ?",
                (chatroom_id,),
            )
            existing = await cursor.fetchone()
    except Exception as e:
        print(f"数据库查询失败: {e}", file=sys.stderr)
        return 1

    if existing:
        existing_name = existing["display_name"]
        # Auto-fix display_name if still equals chatroom_id
        if existing_name == chatroom_id and display_name != chatroom_id:
            import datetime

            try:
                async with aiosqlite.connect(db_path) as db:
                    await db.execute(
                        "UPDATE groups SET display_name = ?, updated_at = ? WHERE group_id = ?",
                        (display_name, datetime.datetime.now().isoformat(), existing["group_id"]),
                    )
                    await db.commit()
                print(
                    f"群组已注册 (已更新群名): group_id={existing['group_id']}, "
                    f"display_name: {existing_name} → {display_name}"
                )
            except Exception:
                print(
                    f"群组已注册: group_id={existing['group_id']}, "
                    f"display_name={existing_name}, chatroom_id={chatroom_id}"
                )
        else:
            print(
                f"群组已注册: group_id={existing['group_id']}, "
                f"display_name={existing_name}, chatroom_id={chatroom_id}"
            )
        return 0

    # Generate group_id and create via Storage
    group_id = f"g_{uuid.uuid4().hex[:12]}"

    try:
        async with Storage(db_path) as store:
            record = await store.create_group(
                group_id=group_id,
                display_name=display_name,
                chatroom_id=chatroom_id,
            )
    except Exception as e:
        print(f"创建群组失败: {e}", file=sys.stderr)
        return 1

    print("已注册群组:")
    print(f"  group_id:     {record['group_id']}")
    print(f"  display_name: {record['display_name']}")
    print(f"  chatroom_id:  {record['chatroom_id']}")
    return 0


async def _cmd_group_dispatch(args: argparse.Namespace) -> int:
    """group 子命令分发器。"""
    group_action = getattr(args, "group_action", None)
    if group_action == "list":
        return await _cmd_group_list(args)
    elif group_action == "resolve":
        return await _cmd_group_resolve(args)
    elif group_action == "add":
        return await _cmd_group_add(args)
    else:
        parser = build_parser()
        for action in parser._subparsers._group_actions:  # type: ignore[attr-defined]
            if "group" in action.choices:
                action.choices["group"].print_help()
                return 1
        return 1


async def _upload_cover_to_feishu(
    group_id: str, record_id: str, image_path: Path, db_path: str
) -> int:
    """把生成的配图挂到飞书日报记录的「图片」字段（gen-image --upload 用）。

    base_token + 日报汇总表 table_id 从群组配置读；record_id 由用户显式提供
    （按日期查记录的响应格式未经验证，不做猜测——那属于「完善 API 接口」范畴）。
    """
    import aiosqlite

    from z_winnow.pipeline.feishu import lark_cli
    from z_winnow.web.services.group_service import get_group_detail

    async with aiosqlite.connect(db_path) as db:
        group = await get_group_detail(db, group_id)
    if not group:
        print(f"错误: 群组 {group_id} 未找到", file=sys.stderr)
        return 1
    base_token = group.feishu_base_token or ""
    table_id = group.feishu_table_summary or ""
    if not base_token or not table_id:
        print(
            "错误: 该群未配置飞书 base/日报汇总表，先在前端初始化飞书框架",
            file=sys.stderr,
        )
        return 1

    await lark_cli.record_upload_attachment(
        base_token,
        table_id,
        record_id,
        "图片",
        image_path.name,
        cwd=str(image_path.parent),
    )
    print(f"已挂载配图到飞书「图片」字段 (record={record_id})")
    return 0


async def _cmd_gen_image(args: argparse.Namespace) -> int:
    """gen-image: 生成日报配图（DMX Gemini，#9.2）。"""
    from z_winnow.outputs.image_gen import (
        ImageGenConfigError,
        ImageGenError,
        generate_cover,
    )

    date = args.date.replace("-", "")
    db_path = args.db or _get_env("SQLITE_DB_PATH", "data/winnow.db")
    group_id = await _resolve_group_identifier(args.group, db_path=db_path)

    if args.upload and args.dry_run:
        print("错误: --dry-run 不生图，无法 --upload", file=sys.stderr)
        return 1
    if args.upload and not args.record_id:
        print("错误: --upload 需配合 --record-id（要挂载的飞书日报记录）", file=sys.stderr)
        return 1

    try:
        paths = await generate_cover(
            group_id,
            date,
            count=args.count,
            ratio=args.ratio,
            size=args.size,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except ImageGenConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    except ImageGenError as exc:
        print(f"生图失败: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[dry-run] prompt 已落盘: {paths[0]}")
    else:
        for p in paths:
            print(f"配图已生成: {p} ({p.stat().st_size} bytes)")

    if args.upload:
        # paths[-1] 是最新生成的 cover.png（单张即 cover.png，多张取末张）
        rc = await _upload_cover_to_feishu(group_id, args.record_id, paths[-1], db_path)
        if rc != 0:
            return rc

    return 0


# ============================================================
# Argument Parser
# ============================================================


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。

    Returns:
        配置好的 ArgumentParser 实例
    """
    parser = argparse.ArgumentParser(
        prog="winnow",
        description="z-winnow 群日报数据管道 CLI",
    )

    # 全局选项
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite 数据库路径 (默认: data/winnow.db)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别 (默认: INFO)",
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # ingest 子命令
    ingest_parser = subparsers.add_parser("ingest", help="单天消息入库")
    ingest_parser.add_argument(
        "--date",
        required=True,
        help="目标日期 YYYY-MM-DD 或 YYYYMMDD",
    )
    ingest_parser.add_argument(
        "--group",
        default=None,
        help="群聊显示名称",
    )
    ingest_parser.add_argument(
        "--base-url",
        default=None,
        help="CipherTalk API 基础 URL",
    )
    ingest_parser.add_argument(
        "--token",
        default=None,
        help="CipherTalk API 认证 token",
    )

    # trace 子命令
    trace_parser = subparsers.add_parser("trace", help="按 serverID 溯源查询")
    trace_parser.add_argument(
        "--server-id",
        required=True,
        help="微信 serverId",
    )

    # export 子命令
    export_parser = subparsers.add_parser("export", help="导出 RL 训练数据 (JSONL)")
    export_parser.add_argument(
        "--start",
        required=True,
        help="开始日期 YYYY-MM-DD 或 YYYYMMDD",
    )
    export_parser.add_argument(
        "--end",
        required=True,
        help="结束日期 YYYY-MM-DD 或 YYYYMMDD",
    )
    export_parser.add_argument(
        "--output",
        default=None,
        help="输出文件路径 (默认: data/rl_export.jsonl)",
    )

    # web 子命令 (T-W7-6)
    web_parser = subparsers.add_parser("web", help="启动 Web 控制面板")
    web_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="监听端口 (默认: 8100, 或 WEB_PORT 环境变量)",
    )
    web_parser.add_argument(
        "--host",
        default=None,
        help="监听地址 (默认: 127.0.0.1, 或 WEB_HOST 环境变量)",
    )

    # group 子命令: group_id ↔ chatroom_id 转换
    group_parser = subparsers.add_parser("group", help="群组标识符转换与查询")
    group_sub = group_parser.add_subparsers(dest="group_action", help="group 子命令")

    group_sub.add_parser("list", help="列出所有已注册群组")

    g_resolve = group_sub.add_parser("resolve", help="group_id/chatroom_id/name 双向解析")
    g_resolve.add_argument("--group-id", default=None, help="按 group_id 查 chatroom_id")
    g_resolve.add_argument("--room-id", default=None, help="按 chatroom_id 查 group_id")
    g_resolve.add_argument("--name", default=None, help="按群名查 group_id + chatroom_id")

    g_add = group_sub.add_parser("add", help="注册新群组")
    g_add.add_argument("--chatroom-id", required=True, help="微信群聊 ID (xxx@chatroom)")
    g_add.add_argument("--display-name", default=None, help="群组显示名称 (默认=chatroom_id)")

    # scheduler 子命令组 (T-SCHED): 定时日报调度（cron 触发 + 补跑 + 环境预检）
    # NOTE: 复用根级 --db（line ~1768），与其它命令一致。
    sched_parser = subparsers.add_parser(
        "scheduler", help="定时日报调度（cron 触发 + 补跑 + 环境预检）"
    )
    sched_sub = sched_parser.add_subparsers(
        dest="scheduler_action", help="scheduler 子命令（无子命令=交互菜单）"
    )

    s_status = sched_sub.add_parser("status", help="查看各群调度状态看板")
    s_status.add_argument("--group", default=None, help="只看指定群 (group_id / display_name)")
    s_status.add_argument(
        "--window", type=int, default=None, help="缺失天数检查窗口 (默认=scheduler_lookback_days)"
    )
    s_status.add_argument("--watch", action="store_true", help="实时刷新（每 ~3s）")

    s_set = sched_sub.add_parser("set", help="修改群的 cron / 启用状态")
    s_set.add_argument("group", help="group_id 或 display_name")
    s_set.add_argument("--cron", default=None, help="cron 表达式，如 '0 2 * * *'")
    s_set.add_argument("--enable", dest="daily_enabled", action="store_true")
    s_set.add_argument("--disable", dest="daily_enabled", action="store_false")
    s_set.set_defaults(daily_enabled=None)

    sched_sub.add_parser("enable", help="启用某群的定时日报").add_argument(
        "group", help="group_id 或 display_name"
    )
    sched_sub.add_parser("disable", help="关闭某群的定时日报").add_argument(
        "group", help="group_id 或 display_name"
    )

    s_run = sched_sub.add_parser("run", help="常驻调度守护进程（或 --once 单次评估）")
    s_run.add_argument(
        "--once", action="store_true", help="只评估当前分钟一次后退出（system cron / launchd 用）"
    )
    s_run.add_argument(
        "--backfill-days",
        type=int,
        default=None,
        help="启动补跑窗口天数（默认=scheduler_backfill_days）",
    )
    s_run.add_argument("--no-backfill", action="store_true", help="跳过启动补跑")
    s_run.add_argument("--poll-interval", type=int, default=None, help="轮询间隔秒（默认=60）")
    s_run.add_argument(
        "--now", default=None, help="[调试] 注入当前时间 ISO8601（仅与 --once 配合）"
    )
    s_run.add_argument("--skip-preflight", action="store_true", help="跳过环境依赖预检")
    s_run.add_argument(
        "--fix-deps",
        action="store_true",
        help="预检失败时一键拉起依赖 (start_all.sh --no-web) 再复检",
    )

    s_doctor = sched_sub.add_parser(
        "doctor", help="环境依赖体检（Docker/容器/Qdrant/memos/数据源/LLM/DB）"
    )
    s_doctor.add_argument("--fix", action="store_true", help="检测到关键缺失时一键拉起依赖后复检")

    s_next = sched_sub.add_parser("next", help="显示各群未来触发时间")
    s_next.add_argument("--count", type=int, default=5, help="每个群显示几个未来触发点")
    s_next.add_argument("--group", default=None, help="只看指定群")

    # judge 子命令 (T-W10-B-b): LLM-as-judge 报告评估
    judge_parser = subparsers.add_parser("judge", help="LLM-as-judge 报告评估")
    judge_parser.add_argument(
        "--date",
        default=None,
        help="单日评估 YYYY-MM-DD 或 YYYYMMDD",
    )
    judge_parser.add_argument(
        "--from",
        dest="from_date",
        default=None,
        help="日期范围起始 YYYY-MM-DD 或 YYYYMMDD (搭配 --to)",
    )
    judge_parser.add_argument(
        "--to",
        dest="to_date",
        default=None,
        help="日期范围结束 YYYY-MM-DD 或 YYYYMMDD (搭配 --from)",
    )
    judge_parser.add_argument(
        "--group",
        default=None,
        help="群组 ID (可选, 不指定则查询全部群组)",
    )
    judge_parser.add_argument(
        "--latest",
        type=int,
        default=None,
        help="评估最近 N 份报告 (按 date DESC)",
    )
    judge_parser.add_argument(
        "--output",
        default="table",
        choices=["table", "json"],
        help="输出格式: table (表格) 或 json (JSON 行). 默认 table.",
    )
    judge_parser.add_argument(
        "--signal-output",
        default=None,
        help="信号输出 JSONL 文件路径 (默认: data/rl/judge_signals.jsonl)",
    )

    # gen-image 子命令 (#9.2): 日报配图独立生图（DMX Gemini）
    gi_parser = subparsers.add_parser("gen-image", help="生成日报配图（DMX Gemini，#9.2）")
    gi_parser.add_argument("--group", required=True, help="群名/group_id/chatroom_id")
    gi_parser.add_argument("--date", required=True, help="目标日期 YYYYMMDD 或 YYYY-MM-DD")
    gi_parser.add_argument("--count", type=int, default=None, help="生成张数（默认取配置）")
    gi_parser.add_argument("--ratio", default=None, help="宽高比 如 4:5（默认取配置）")
    gi_parser.add_argument("--size", default=None, help="分辨率 如 2K（默认取配置）")
    gi_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只组装 prompt 落 cover.prompt.txt，不调 DMX（诊断用）",
    )
    gi_parser.add_argument(
        "--upload",
        action="store_true",
        help="生成后挂载到飞书日报「图片」字段（需配合 --record-id）",
    )
    gi_parser.add_argument(
        "--record-id",
        default=None,
        help="飞书日报记录 ID（--upload 时必填；不做按日期查记录的猜测）",
    )

    # memos 子命令组 (T-W10-E-g): MemOS 运维和监控
    memos_parser = subparsers.add_parser("memos", help="MemOS 运维和监控")
    memos_sub = memos_parser.add_subparsers(dest="memos_action", help="MemOS 子命令")

    # memos status
    m_status = memos_sub.add_parser("status", help="显示 MemOS 健康状态和 cube 节点数")
    m_status.add_argument("--group", default=None, help="限定群组")

    # memos rebuild
    m_rebuild = memos_sub.add_parser("rebuild", help="从 SQLite 全量重建 cube")
    m_rebuild.add_argument("--group", required=True, help="群组名称")
    m_rebuild.add_argument(
        "--from",
        dest="from_source",
        default="sqlite",
        help="数据源 (默认: sqlite)",
    )

    # memos vacuum
    m_vacuum = memos_sub.add_parser("vacuum", help="触发生命周期状态机")
    m_vacuum.add_argument("--group", default=None, help="限定群组")

    # memos export
    m_export = memos_sub.add_parser("export", help="dump cube 到文件")
    m_export.add_argument("--group", required=True, help="群组名称")
    m_export.add_argument("--out", required=True, help="输出文件路径")

    # memos search
    m_search = memos_sub.add_parser("search", help="命令行查询调试")
    m_search.add_argument("--group", required=True, help="群组名称")
    m_search.add_argument("--query", required=True, help="搜索查询字符串")
    m_search.add_argument("--top-k", type=int, default=20, help="返回结果数 (默认: 20)")

    # memos flush
    memos_sub.add_parser("flush", help="强制处理所有 pending sync 任务")

    # memos delete-cube
    m_delete = memos_sub.add_parser("delete-cube", help="删除指定 cube 的所有记忆")
    m_delete.add_argument("--group", required=True, help="群组名称")
    m_delete.add_argument("--yes", "-y", action="store_true", help="跳过确认提示")

    # memos purge-wxid
    m_purge = memos_sub.add_parser("purge-wxid", help="扫描并清理含 wxid_ 的 MemOS 记忆节点")
    m_purge.add_argument("--group", default=None, help="限定群组（不指定则扫描所有已注册群组）")
    m_purge.add_argument("--dry-run", action="store_true", help="只预览，不实际删除")

    # memos wipe-all (dev/debug — nuke all groups' memories)
    m_wipe = memos_sub.add_parser(
        "wipe-all", help="全量清空所有群的 MemOS 记忆（开发调试，不可逆）"
    )
    m_wipe.add_argument("--yes", "-y", action="store_true", help="跳过确认提示")
    m_wipe.add_argument(
        "--include-local",
        action="store_true",
        help="同时清空本地数据（保留 groups 注册行）",
    )
    m_wipe.add_argument(
        "--db", default=None, help="SQLite 路径（默认 SQLITE_DB_PATH / data/winnow.db）"
    )

    # mcp 子命令: 启动 MCP server (L3 读取 + 反馈 Inbox)
    # docs/mcp-platform-checkpoint.md §4.1
    mcp_parser = subparsers.add_parser("mcp", help="启动 MCP server（L3 读取 + 反馈 Inbox）")
    mcp_parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "http"],
        help="传输方式：stdio（本地集成，默认）/ http（远程或 ECS 部署，streamable-http）",
    )
    mcp_parser.add_argument("--port", type=int, default=8000, help="http 传输监听端口（默认 8000）")
    mcp_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="http 传输绑定地址（默认 0.0.0.0 容器/远程可达；本地调试用 127.0.0.1）",
    )

    # sync 子命令组 (阶段 2.1/2.2): 本地 ↔ ECS 数据同步
    # docs/mcp-platform-checkpoint.md §3.3
    sync_parser = subparsers.add_parser(
        "sync", help="本地 ↔ ECS 数据同步（push L3 / pull 反馈 / status）"
    )
    sync_sub = sync_parser.add_subparsers(dest="sync_action", help="sync 子命令")

    s_push = sync_sub.add_parser("push", help="推 L3 快照 + processed JSON 到 ECS")
    s_push.add_argument("--dry-run", action="store_true", help="只生成本地快照并报告，不传输")
    s_push.add_argument(
        "--no-processed",
        action="store_true",
        help="只推 l3_snapshot.db，跳过 processed/ JSON（首次部署必传完整一次）",
    )

    s_pull = sync_sub.add_parser("pull", help="拉 ECS 反馈 inbox → merge 本地 → 清 inbox")
    s_pull.add_argument("--dry-run", action="store_true", help="只 merge 报告，不清 ECS inbox")

    sync_sub.add_parser("status", help="比对本地 vs ECS 行数 + 待 pull 计数")

    # r2 子命令组: Cloudflare R2 对象存储（附件上传/回填，MCP 公网下载）
    r2_parser = subparsers.add_parser(
        "r2", help="Cloudflare R2 对象存储（附件上传/回填，MCP 公网下载）"
    )
    r2_sub = r2_parser.add_subparsers(dest="r2_action", help="r2 子命令")

    r2_sub.add_parser("status", help="配置就绪状态 + resources.json 已传/待传统计")

    r2_upload = r2_sub.add_parser("upload", help="上传附件到 R2 + 回填 cloud_key")
    r2_upload.add_argument("--group", default=None, help="只处理指定 group_id")
    r2_upload.add_argument("--date", default=None, help="只处理指定日期 YYYYMMDD")
    r2_upload.add_argument("--dry-run", action="store_true", help="只报告待传数，不传不写")

    # mcp-key 子命令组: MCP API key 管理（key→成员/群组权限白名单）
    mk_parser = subparsers.add_parser("mcp-key", help="MCP API key 管理（成员/群组权限）")
    mk_sub = mk_parser.add_subparsers(dest="mcpkey_action", help="mcp-key 子命令")

    mk_sub.add_parser("list", help="列出所有注册 key（脱敏显示）")

    mk_add = mk_sub.add_parser("add", help="生成新 key 并绑定成员/群组权限")
    mk_add.add_argument(
        "--member", required=True, help="成员标识 member_id（写入 feedback reporter）"
    )
    mk_add.add_argument("--name", default=None, help="成员显示名（默认=member）")
    mk_add.add_argument("--groups", default=None, help="可访问群组 group_id 列表（逗号分隔）")
    mk_add.add_argument("--admin", action="store_true", help="管理员（全权，忽略 --groups）")

    mk_revoke = mk_sub.add_parser("revoke", help="撤销 key")
    mk_revoke.add_argument("--key", required=True, help="要撤销的完整 key")

    mk_allow = mk_sub.add_parser("allow", help="给 key 追加可访问群组")
    mk_allow.add_argument("--key", required=True, help="完整 key")
    mk_allow.add_argument("--groups", required=True, help="追加的 group_id 列表（逗号分隔）")

    return parser


async def _cmd_memos_dispatch(args: argparse.Namespace) -> int:
    """memos 子命令分发器.

    Args:
        args: 解析后的命令行参数

    Returns:
        exit code
    """
    memos_action = getattr(args, "memos_action", None)

    if memos_action == "status":
        return await _cmd_memos_status(args)
    elif memos_action == "rebuild":
        return await _cmd_memos_rebuild(args)
    elif memos_action == "vacuum":
        return await _cmd_memos_vacuum(args)
    elif memos_action == "export":
        return await _cmd_memos_export(args)
    elif memos_action == "search":
        return await _cmd_memos_search(args)
    elif memos_action == "flush":
        return await _cmd_memos_flush(args)
    elif memos_action == "delete-cube":
        return await _cmd_memos_delete_cube(args)
    elif memos_action == "purge-wxid":
        return await _cmd_memos_purge_wxid(args)
    elif memos_action == "wipe-all":
        return await _cmd_memos_wipe_all(args)
    else:
        parser = build_parser()
        # Find memos subparser to print its help
        for action in parser._subparsers._group_actions:
            if "memos" in action.choices:
                action.choices["memos"].print_help()
                return 1
        return 1


def _cmd_mcp(args: argparse.Namespace) -> int:
    """启动 MCP server (L3 读取 + 反馈 Inbox, docs/mcp-platform-checkpoint.md §4.1).

    同步阻塞 — ``mcp.run()`` 自管理事件循环, 仿 ``_cmd_web`` 模式, 不走 ``asyncio.run``.
    """
    from z_winnow.mcp_server import run as run_mcp

    transport = getattr(args, "transport", "stdio")
    host = getattr(args, "host", "0.0.0.0")
    port = getattr(args, "port", 8000)
    if transport == "stdio":
        # stdio 下 stdout 是 MCP 协议通道, 提示只能走 stderr
        print(
            "启动 MCP server (stdio) — 供 Claude Desktop / Cursor 等本地客户端调用",
            file=sys.stderr,
        )
    else:
        print(f"启动 MCP server (http, {host}:{port}) — 远程 / ECS 部署", file=sys.stderr)
    run_mcp(transport=transport, host=host, port=port)
    return 0


# ============================================================
# sync 子命令（阶段 2.1/2.2）：本地 ↔ ECS 数据同步
# docs/mcp-platform-checkpoint.md §3.3
# ============================================================


async def _cmd_sync_push(args: argparse.Namespace) -> int:
    """sync push: 推 L3 快照 + processed JSON 到 ECS。"""
    from z_winnow.sync import push as sync_push

    try:
        r = await sync_push(
            dry_run=getattr(args, "dry_run", False),
            include_processed=not getattr(args, "no_processed", False),
        )
    except Exception as e:
        print(f"❌ push 失败: {e}", file=sys.stderr)
        return 1

    kb = r["snapshot_bytes"] / 1024
    print(f"✅ L3 快照: {kb:.1f} KB → {r['remote_snapshot_path']}")
    if r["dry_run"]:
        print("   [dry-run] 未实际传输")
    else:
        print(f"   processed JSON 同步: {'是' if r['processed_synced'] else '否'}")
        print(f"   mcp_keys.yaml 同步: {'是' if r.get('keys_synced') else '否'}")
    return 0


async def _cmd_sync_pull(args: argparse.Namespace) -> int:
    """sync pull: 拉 ECS 反馈 inbox → merge 本地 → 清 inbox。"""
    from z_winnow.sync import pull as sync_pull

    try:
        r = await sync_pull(dry_run=getattr(args, "dry_run", False))
    except Exception as e:
        print(f"❌ pull 失败: {e}", file=sys.stderr)
        return 1

    if r["dry_run"]:
        print(f"✅ [dry-run] merge {r['pulled']} 条新反馈（未清 ECS inbox）")
    else:
        print(f"✅ 拉取并 merge {r['pulled']} 条反馈；ECS inbox 清空: {r['cleared']}")
    if r.get("note"):
        print(f"   ℹ️  {r['note']}")
    return 0


async def _cmd_sync_status(args: argparse.Namespace) -> int:
    """sync status: 本地 vs ECS 行数比对 + 待 pull 计数。"""
    from z_winnow.sync import status as sync_status

    try:
        r = await sync_status()
    except Exception as e:
        print(f"❌ status 失败: {e}", file=sys.stderr)
        return 1

    local = r["local"]
    ecs_l3 = r["ecs_l3"]
    ecs_inbox = r["ecs_inbox"]
    pending = r["inbox_pending_pull"]

    def _row(table: str) -> str:
        lv = local.get(table, "?")
        if isinstance(ecs_l3, dict):
            ev = ecs_l3.get(table, "?")
        elif ecs_l3 == "NOT_EXISTS":
            ev = "未 push"
        else:
            ev = "(查询失败)"
        return f"  {table:<18} 本地 {lv!s:<8} ECS {ev}"

    print("=== 本地 vs ECS l3_snapshot ===")
    for t in ("groups", "topic_summaries", "report_versions", "feedback_events"):
        print(_row(t))
    if ecs_l3 == "NOT_EXISTS":
        print("  ⚠️  ECS l3_snapshot 尚未 push（运行 `winnow sync push`）")
    print()
    print(f"=== ECS feedback inbox 待 pull: {pending} 条 ===")
    if isinstance(ecs_inbox, dict):
        for k, v in ecs_inbox.items():
            print(f"  {k}: {v}")
    elif ecs_inbox == "NOT_EXISTS":
        print("  (inbox 尚未创建 — ECS 未收到任何反馈)")
    if pending > 0:
        print("  → 运行 `winnow sync pull` 拉取")
    return 0


async def _cmd_sync_dispatch(args: argparse.Namespace) -> int:
    """sync 子命令分发器。"""
    action = getattr(args, "sync_action", None)
    if action == "push":
        return await _cmd_sync_push(args)
    elif action == "pull":
        return await _cmd_sync_pull(args)
    elif action == "status":
        return await _cmd_sync_status(args)
    else:
        parser = build_parser()
        for act in parser._subparsers._group_actions:  # type: ignore[attr-defined]
            if "sync" in act.choices:
                act.choices["sync"].print_help()
                return 1
        return 1


# ============================================================
# r2 子命令：Cloudflare R2 对象存储（附件上传/回填）
# ============================================================


def _r2_scan_resources_json(
    group_filter: str = "", date_filter: str = ""
) -> list[tuple[Path, str, str]]:
    """扫 data/processed/**/resources.json，返回 [(path, gid, date), ...]（按 group/date 过滤）。"""
    from z_winnow.config.settings import get_settings

    proc_dir = Path(get_settings().layer3_output_dir)
    if not proc_dir.exists():
        return []
    out: list[tuple[Path, str, str]] = []
    for rj in proc_dir.rglob("resources.json"):
        try:
            rel = rj.relative_to(proc_dir)
        except ValueError:
            continue
        parts = rel.parts  # (gid, date, [vN], resources.json)
        if len(parts) < 3:
            continue
        gid, d = parts[0], parts[1]
        if group_filter and gid != group_filter:
            continue
        if date_filter and d != date_filter:
            continue
        out.append((rj, gid, d))
    return out


async def _cmd_r2_status(args: argparse.Namespace) -> int:
    """r2 status：配置就绪状态 + resources.json 扫描统计（已传/待传）。"""
    from z_winnow.config.settings import get_settings
    from z_winnow.object_storage.r2 import is_r2_configured

    s = get_settings()
    print("=" * 60)
    print("Cloudflare R2 配置状态")
    print("=" * 60)
    print(f"  r2_upload_enabled   : {s.r2_upload_enabled}")
    print(f"  凭证齐全(is_r2_configured): {is_r2_configured(s)}")
    print(f"  endpoint            : {s.r2_endpoint or '(未配)'}")
    print(f"  bucket              : {s.r2_bucket or '(未配)'}")
    print(f"  access_key_id       : {'(已配)' if s.r2_access_key_id else '(未配)'}")
    print(f"  secret_access_key   : {'(已配)' if s.r2_secret_access_key else '(未配)'}")
    print(f"  presigned_expiry    : {s.r2_presigned_expiry}s")

    print()
    print("扫描 data/processed/**/resources.json")
    found = _r2_scan_resources_json()
    total_res = with_local = with_cloud = pending = 0
    for rj, _gid, _d in found:
        try:
            data = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for r in data.get("resources", []) or []:
            if not isinstance(r, dict):
                continue
            total_res += 1
            has_local = bool(r.get("local_path")) and Path(str(r["local_path"])).is_file()
            has_cloud = bool(r.get("cloud_key"))
            if has_local:
                with_local += 1
            if has_cloud:
                with_cloud += 1
            if has_local and not has_cloud:
                pending += 1
    print(f"  resources.json 文件数   : {len(found)}")
    print(f"  resource 总数           : {total_res}")
    print(f"  有本地文件(local_path)  : {with_local}")
    print(f"  已传 R2(cloud_key)      : {with_cloud}")
    print(f"  待传(有本地/无cloud_key): {pending}")
    if not s.r2_upload_enabled:
        print("\n⚠ r2_upload_enabled=false，pipeline 不自动上传；CLI `r2 upload` 仍可手动传。")
    elif not is_r2_configured(s):
        print("\n⚠ 凭证未配齐，无法上传；填 .env 的 R2_* 项。")
    return 0


async def _cmd_r2_upload(args: argparse.Namespace) -> int:
    """r2 upload：扫 resources.json，上传 valid local_path 文件到 R2 + 回填 cloud_key。"""
    from z_winnow.object_storage.r2 import is_r2_configured, upload_resources

    dry = bool(getattr(args, "dry_run", False))
    group_filter = getattr(args, "group", "") or ""
    date_filter = getattr(args, "date", "") or ""

    if not is_r2_configured():
        print("✗ R2 凭证未配齐（endpoint/key/secret/bucket），无法上传。先填 .env。")
        return 1

    found = _r2_scan_resources_json(group_filter, date_filter)
    if not found:
        print(
            f"未扫到匹配的 resources.json（group={group_filter or '*'}, date={date_filter or '*'}）"
        )
        return 0

    mode = "[dry-run] " if dry else ""
    print(f"{mode}扫描到 {len(found)} 个 resources.json，开始上传...")
    total = 0
    for rj, gid, d in found:
        n = await upload_resources(rj, gid, d, dry_run=dry)
        if n:
            print(f"  {mode}{gid}/{d}: {n} 个资源{'待传' if dry else '已传'}")
            total += n
    print(f"{mode}完成：共 {total} 个资源{'待传' if dry else '上传'}。")
    return 0


async def _cmd_r2_dispatch(args: argparse.Namespace) -> int:
    """r2 子命令分发器。"""
    action = getattr(args, "r2_action", None)
    if action == "status":
        return await _cmd_r2_status(args)
    elif action == "upload":
        return await _cmd_r2_upload(args)
    else:
        parser = build_parser()
        for act in parser._subparsers._group_actions:  # type: ignore[attr-defined]
            if "r2" in act.choices:
                act.choices["r2"].print_help()
                return 1
        return 1


# ============================================================
# mcp-key 子命令：MCP API key 管理（key→成员/群组权限）
# ============================================================


def _split_groups(raw: str | None) -> list[str]:
    """逗号分隔的 group_id 列表 → list（去空白/空项）。"""
    return [g.strip() for g in (raw or "").split(",") if g.strip()]


async def _cmd_mcp_key_list(args: argparse.Namespace) -> int:
    """列出所有注册 key（脱敏：仅显示 key 前缀 + member + 权限）。"""
    from z_winnow.config.settings import get_settings
    from z_winnow.mcp_server.mcp_keys import load_keys

    path = get_settings().mcp_keys_path
    keys = load_keys(path)
    if not keys:
        print(f"（{path} 无注册 key）")
        return 0
    print(f"=== MCP keys ({path}) — 共 {len(keys)} 个 ===")
    for k, v in keys.items():
        prefix = k[:12] + "..." if len(k) > 12 else k
        if v.get("is_admin"):
            scope = "管理员(全权)"
        else:
            groups = v.get("allowed_groups") or []
            scope = f"群组({len(groups)}): {','.join(groups)}" if groups else "(无群权限)"
        print(
            f"  {prefix}  member={v.get('member_id', '')}  name={v.get('display_name', '')}  [{scope}]"
        )
    return 0


async def _cmd_mcp_key_add(args: argparse.Namespace) -> int:
    """生成新 key（wn_<token>）并绑定成员/群组权限，写 YAML。"""
    import secrets

    from z_winnow.config.settings import get_settings
    from z_winnow.mcp_server.mcp_keys import load_keys, save_keys

    path = get_settings().mcp_keys_path
    keys = load_keys(path)
    new_key = "wn_" + secrets.token_urlsafe(24)
    is_admin = bool(args.admin)
    groups = [] if is_admin else _split_groups(args.groups)
    keys[new_key] = {
        "member_id": args.member,
        "display_name": args.name or args.member,
        "is_admin": is_admin,
        "allowed_groups": groups,
    }
    save_keys(path, keys)
    print(f"✅ 已添加 key（请安全发送给成员）:\n  {new_key}")
    scope = "管理员(全权)" if is_admin else f"groups={groups}"
    print(f"   member={args.member}  {scope}")
    return 0


async def _cmd_mcp_key_revoke(args: argparse.Namespace) -> int:
    """撤销 key（从 YAML 删）。"""
    from z_winnow.config.settings import get_settings
    from z_winnow.mcp_server.mcp_keys import load_keys, save_keys

    path = get_settings().mcp_keys_path
    keys = load_keys(path)
    if args.key not in keys:
        print(f"❌ key 未找到: {args.key}", file=sys.stderr)
        return 1
    entry = keys.pop(args.key)
    save_keys(path, keys)
    print(f"✅ 已撤销 key（member={entry.get('member_id')}）: {args.key[:12]}...")
    return 0


async def _cmd_mcp_key_allow(args: argparse.Namespace) -> int:
    """给 key 追加可访问群组（admin key 不变，已全权）。"""
    from z_winnow.config.settings import get_settings
    from z_winnow.mcp_server.mcp_keys import load_keys, save_keys

    path = get_settings().mcp_keys_path
    keys = load_keys(path)
    if args.key not in keys:
        print(f"❌ key 未找到: {args.key}", file=sys.stderr)
        return 1
    entry = keys[args.key]
    if entry.get("is_admin"):
        print("ℹ️  该 key 是管理员（全权），无需限定群组")
        return 0
    existing = set(entry.get("allowed_groups") or [])
    existing.update(_split_groups(args.groups))
    entry["allowed_groups"] = sorted(existing)
    save_keys(path, keys)
    print(f"✅ 已追加群权限: {entry['allowed_groups']}")
    return 0


async def _cmd_mcp_key_dispatch(args: argparse.Namespace) -> int:
    """mcp-key 子命令分发器。"""
    action = getattr(args, "mcpkey_action", None)
    if action == "list":
        return await _cmd_mcp_key_list(args)
    elif action == "add":
        return await _cmd_mcp_key_add(args)
    elif action == "revoke":
        return await _cmd_mcp_key_revoke(args)
    elif action == "allow":
        return await _cmd_mcp_key_allow(args)
    else:
        parser = build_parser()
        for act in parser._subparsers._group_actions:  # type: ignore[attr-defined]
            if "mcp-key" in act.choices:
                act.choices["mcp-key"].print_help()
                return 1
        return 1


# ============================================================
# 主入口
# ============================================================


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口函数。

    Args:
        argv: 命令行参数列表 (None = sys.argv[1:])

    Returns:
        exit code (0 = 成功)
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "ingest":
        return asyncio.run(_cmd_ingest(args))
    elif args.command == "trace":
        return asyncio.run(_cmd_trace(args))
    elif args.command == "export":
        return asyncio.run(_cmd_export(args))
    elif args.command == "web":
        return _cmd_web(args)
    elif args.command == "judge":
        return asyncio.run(_cmd_judge(args))
    elif args.command == "gen-image":
        return asyncio.run(_cmd_gen_image(args))
    elif args.command == "memos":
        return asyncio.run(_cmd_memos_dispatch(args))
    elif args.command == "mcp":
        return _cmd_mcp(args)
    elif args.command == "sync":
        return asyncio.run(_cmd_sync_dispatch(args))
    elif args.command == "r2":
        return asyncio.run(_cmd_r2_dispatch(args))
    elif args.command == "mcp-key":
        return asyncio.run(_cmd_mcp_key_dispatch(args))
    elif args.command == "group":
        return asyncio.run(_cmd_group_dispatch(args))
    elif args.command == "scheduler":
        from z_winnow.scheduler.cli_dispatch import _cmd_scheduler_dispatch

        return _cmd_scheduler_dispatch(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
