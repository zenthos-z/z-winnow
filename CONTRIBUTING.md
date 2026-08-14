# 贡献指南

感谢你考虑为 winnow 贡献！

## 开发环境

```bash
git clone https://github.com/zenthos-z/z-winnow.git && cd z-winnow
poetry install --with dev
pre-commit install
```

## 提交前自检

```bash
poetry run ruff check . && poetry run ruff format --check .
poetry run mypy src/                 # 基础模式，非阻断但请尽量清零
poetry run pytest tests/ -v --tb=short
pre-commit run --all-files
```

CI（`.github/workflows/ci.yml`）会在 push / PR 时跑 lint → test → security 全套；本地全绿基本等于 CI 全绿（测试全 mock 模式，无需任何 API key）。

## 代码约定

- Python 3.12，Ruff 行宽 100（lint + format 配置在 `pyproject.toml`）
- 异步优先：存储层 / 数据源客户端均为 aiosqlite / httpx async
- 配置一律走 pydantic-settings（`WINNOW_*` 环境变量），不要在代码里读裸环境变量
- 新增 L3 产物类型优先走自定义表 registry（`custom_tables/`，YAML 加表），不要硬编码
- LangGraph 状态新增字段必须先声明进 `OverallState` TypedDict（未声明 key 会被静默丢弃）
- 测试自行构造数据（内联 fixture），不依赖任何真实数据源或网络

## PR 流程

1. Fork → 特性分支（`feat-xxx` / `fix-xxx`）
2. 改动 + 测试 + 文档同步更新（`README.md` / `docs/` 相应章节）
3. 本地自检全绿 → 提 PR，描述「改了什么 / 为什么 / 怎么验证」
4. CI 通过后 maintainer review

## 报告问题

提 Issue 请包含：版本 / Python 版本 / 复现步骤 / 相关日志。涉及数据隐私时请先脱敏。
