# 示例数据（完全虚构）

本目录提供一套**完全虚构**的演示数据：群名、成员、消息、议题、资源链接均为杜撰
（链接统一使用 `example.com` 保留域），与任何真实群聊、真实人物、真实项目无关。

主题设定是一个虚构的「AI 工具观察站」技术讨论群，覆盖 3 天（2026-08-12 ~ 08-14），
包含完整的数据层次，可用于零依赖体验整个系统：

| 层 | 内容 |
|----|------|
| L1 `raw_messages` | 33 条虚构消息（文本 / 链接 / 文件） |
| L2 `parsed_contexts` | 每天 2 个上下文块 |
| L3 `topic_summaries` + JSON | 8 条议题（含一个跨 3 天演化的 sustained 议题）、日报、资源 |

## 用法

```bash
# 仓库根目录执行
poetry run python examples/sample-data/seed.py     # 写入（幂等，可重复执行）
poetry run winnow web                              # 浏览 http://127.0.0.1:8100/ui/
poetry run python examples/sample-data/seed.py --clean   # 清除示例数据
```

写入后可以在 Web UI 里体验：报告列表与详情、议题演化（看「本地大模型部署方案选型」
从 emerging → sustained → concluded 的三天 trend 迭代）、数据三层浏览、版本管理界面。
