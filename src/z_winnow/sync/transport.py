"""SSH/rsync 传输封装（阶段 2.1 sync push/pull/status 共用）。

ECS 连接配置从 :class:`Settings` 读（``ecs_ssh_host/user/key/data_dir/container``）。
所有命令异步执行（``asyncio.create_subprocess_exec``），stdout/stderr 合并捕获。

设计要点：
- rsync 的 ``-e`` 接一个 ssh 命令串作为**单个 argv 元素**（rsync 内部 shell 解析），
  subprocess list 形式每元素独立 argv，绕开 shell 分词问题。
- ssh 选项内联成 list（``StrictHostKeyChecking=accept-new`` 首次自动接受 host key，
  ``IdentitiesOnly=yes`` 防 ssh-agent 注入其他 key 干扰）。
- 边界集中在 :func:`run_argv` / :func:`run_rsync` / :func:`run_ssh`，便于测试 mock。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from z_winnow.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class SyncConfigError(RuntimeError):
    """ECS sync 必填配置缺失。"""


def check_config(settings: Settings | None = None) -> Settings:
    """校验 ECS sync 必填项（host + key），返回校验过的 settings。"""
    settings = settings or get_settings()
    missing = [
        name
        for name, val in (
            ("ecs_ssh_host", settings.ecs_ssh_host),
            ("ecs_ssh_key", settings.ecs_ssh_key),
        )
        if not val
    ]
    if missing:
        raise SyncConfigError(
            f"ECS sync 未配置：{missing}。请在 .env 设置 WINNOW_ECS_SSH_HOST / "
            "WINNOW_ECS_SSH_KEY（见 docs/mcp-platform-checkpoint.md §4.3）。"
        )
    return settings


def ssh_target(settings: Settings) -> str:
    """``user@host`` 形式的远程目标。"""
    return f"{settings.ecs_ssh_user}@{settings.ecs_ssh_host}"


def ssh_base_args(settings: Settings) -> list[str]:
    """ssh 基础 argv（密钥 + 安全选项 + 目标），调用方再 append 远程命令。"""
    return [
        "ssh",
        "-i",
        settings.ecs_ssh_key,
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ServerAliveInterval=30",
        ssh_target(settings),
    ]


def rsync_e_arg(settings: Settings) -> str:
    """rsync ``-e`` 接受的 ssh 命令串（单 argv 元素，rsync 内部 shell 解析）。"""
    return (
        f"ssh -i {settings.ecs_ssh_key} -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
    )


@dataclass
class CmdResult:
    """命令执行结果。

    ``output`` 是 stdout（解析用，如 status 的行数输出）；``stderr`` 单独存
    （SSH/rsync 的 WARNING/进度不污染 stdout 解析）。错误诊断用 ``combined``。
    """

    returncode: int
    output: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def combined(self) -> str:
        """stdout + stderr（错误诊断 / 文本特征匹配用）。"""
        return f"{self.output}\n{self.stderr}" if self.stderr else self.output


async def run_argv(argv: list[str]) -> CmdResult:
    """执行命令，stdout / stderr 分离捕获（stderr 噪音不污染 stdout 解析）。"""
    logger.debug("run argv: %s", argv)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_bytes, err_bytes = await proc.communicate()
    output = out_bytes.decode("utf-8", errors="replace") if out_bytes else ""
    stderr = err_bytes.decode("utf-8", errors="replace") if err_bytes else ""
    rc = proc.returncode if proc.returncode is not None else -1
    if rc != 0:
        logger.warning("cmd failed (rc=%d): %s\n%s", rc, argv[0], output + stderr)
    return CmdResult(returncode=rc, output=output, stderr=stderr)


async def run_ssh(settings: Settings, remote_cmd: str) -> CmdResult:
    """在 ECS 执行远程 shell 命令。"""
    check_config(settings)
    return await run_argv([*ssh_base_args(settings), remote_cmd])


async def run_rsync(args: list[str]) -> CmdResult:
    """执行 rsync（调用方组装完整 args，本函数只 prepend ``rsync``）。"""
    return await run_argv(["rsync", *args])
