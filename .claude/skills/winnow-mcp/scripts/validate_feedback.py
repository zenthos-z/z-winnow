#!/usr/bin/env python3
"""winnow 反馈 payload 格式预检（纯标准库，零依赖）。

在调用 MCP ``submit_feedback`` **之前**本地预检反馈 payload，避免服务端因格式
不符拒绝（服务端会 schema 校验，非法请求不写库）。面向接公网 MCP 的外部
Agent / 脚本——无需安装 z-winnow 或 pydantic，裸 ``python3`` 即可跑。

⚠️ 单一真源是服务端 ``src/z_winnow/mcp_server/feedback_schema.py``；
本脚本手工镜像其规则（signal 5 值集 + target_type 基础集 + date 校验）。
两端 drift 由服务端 ``tests/test_mcp_feedback_schema.py`` 的 drift-guard 测试兜底。
改规则时务必两端同步。

用法:
  # ① 内联 JSON
  python3 validate_feedback.py --inline '{"group_id":"g_xxx","date":"20260720", ...}'
  # ② 文件
  python3 validate_feedback.py --file payload.json
  # ③ stdin 管道
  cat payload.json | python3 validate_feedback.py
  echo '{...}' | python3 validate_feedback.py

退出码: 0 = 通过；1 = 格式错误（详见输出）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime

# ============================================================
# 合法取值 —— 与服务端 feedback_schema.py 保持同步（drift-guard 测试兜底）
# ============================================================

#: 反馈意图 5 值集。correction/supplement 的 content 落 corrected_text；
#: approval/stale/quality 的 content 落 correction_note。
ALLOWED_SIGNALS = ("correction", "supplement", "approval", "stale", "quality")

#: target_type 基础集。自定义表 id（engineering / world_models / …）由平台注册表
#: 动态扩展，本脚本面向外部用户只校验基础集；若你确定在用某自定义表且被服务端接受，
#: 预检报 target_type 不在基础集属正常——以服务端响应为准。
ALLOWED_TARGET_TYPES = ("report", "trend", "highlights", "topic", "resource", "section")

#: 必填字段（非空字符串）
REQUIRED_FIELDS = ("group_id", "date", "target_type", "signal", "content")

_DATE_RE = re.compile(r"^\d{8}$|^\d{4}-\d{2}-\d{2}$")


# ============================================================
# 校验逻辑
# ============================================================


def _normalize_date(value: str) -> str | None:
    """校验 date 形态 + 真实日历日期；合法返回归一化的 YYYY-MM-DD，非法返回 None。"""
    s = (value or "").strip()
    if not _DATE_RE.match(s):
        return None
    fmt = "%Y%m%d" if len(s) == 8 else "%Y-%m-%d"
    try:
        return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
    except ValueError:
        return None


def validate(payload: dict) -> tuple[list[str], dict]:
    """校验 payload，返回 (错误列表, 归一化后的 payload)。

    错误列表为空表示通过。归一化 payload 仅在通过时有意义（date 归一为 YYYY-MM-DD）。
    """
    errors: list[str] = []
    normalized = dict(payload) if isinstance(payload, dict) else {}

    if not isinstance(payload, dict):
        return ["payload 必须是一个 JSON 对象（{...}）"], {}

    # 必填字段存在性 + 非空
    for field in REQUIRED_FIELDS:
        val = payload.get(field)
        if val is None:
            errors.append(f"缺少必填字段：{field}")
        elif not isinstance(val, str) or not val.strip():
            errors.append(f"字段 {field} 必须是非空字符串")

    # signal 取值
    signal = payload.get("signal")
    if isinstance(signal, str) and signal.strip() and signal not in ALLOWED_SIGNALS:
        errors.append(f"signal 非法（{signal!r}）；合法值：{list(ALLOWED_SIGNALS)}")

    # target_type 取值
    ttype = payload.get("target_type")
    if isinstance(ttype, str) and ttype.strip() and ttype not in ALLOWED_TARGET_TYPES:
        errors.append(
            f"target_type 非法（{ttype!r}）；合法基础值：{list(ALLOWED_TARGET_TYPES)}"
            "（自定义表 id 如 engineering/world_models 由平台扩展，以服务端响应为准）"
        )

    # date 格式 + 真实日期
    date_val = payload.get("date")
    if isinstance(date_val, str) and date_val.strip():
        norm = _normalize_date(date_val)
        if norm is None:
            errors.append(f"date 非法（{date_val!r}）；需 YYYYMMDD 或 YYYY-MM-DD 且为真实日历日期")
        else:
            normalized["date"] = norm

    return errors, normalized


# ============================================================
# 输入解析 + CLI
# ============================================================

_EXAMPLE = {
    "group_id": "g_xxx",
    "date": "20260720",
    "target_type": "topic",
    "signal": "correction",
    "content": "这里的结论应该是……",
    "target_topic_id": "summary_xxx",
    "target_version_id": "report_xxx-v3",
    "original_text": "原内容…",
}


def _read_payload(args: argparse.Namespace) -> dict:
    raw: str
    if args.inline is not None:
        raw = args.inline
    elif args.file is not None:
        raw = args.file.read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        raise SystemExit(
            "❌ 未提供 payload。用 --inline '{...}' / --file path.json / 或通过 stdin 传入。"
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"❌ JSON 解析失败：{e}") from e
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="winnow 反馈 payload 格式预检（提交 submit_feedback 前用）。",
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--inline", metavar="JSON", help="内联 JSON 字符串")
    src.add_argument("--file", metavar="PATH", type=argparse.FileType("r"), help="JSON 文件路径")
    args = parser.parse_args(argv)

    payload = _read_payload(args)
    errors, normalized = validate(payload)

    if errors:
        print("❌ 反馈格式校验失败（不会写入，请按下列修正后再提交）：")
        for e in errors:
            print(f"  • {e}")
        print()
        print(
            f"参考合法值 —— signal: {list(ALLOWED_SIGNALS)}；"
            f"target_type 基础集: {list(ALLOWED_TARGET_TYPES)}"
        )
        print()
        print("合法 payload 示例：")
        print(json.dumps(_EXAMPLE, ensure_ascii=False, indent=2))
        return 1

    print("✅ 反馈格式校验通过。归一化 payload：")
    print(json.dumps(normalized, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
