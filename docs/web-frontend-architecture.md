# 群日报 Web 前端架构与对接说明

> 本文档是「OD 重设计 → 前端 HTML 对接后端」任务的**初始上下文**。下次执行对接前先读本文档，再按 §7 执行计划操作；需要细节时按 §8 索引查阅参考文档。

---

## 1. 一句话定位

把 Open Design（OD）重设计产出的**静态 HTML 页面**，对接到已有的 FastAPI 纯 API 后端，形成一个**前后端分离、可独立并行迭代**的群日报管理后台。

---

## 2. 背景环境

### 2.1 要解决的问题
管理员需要一个 Web 后台：鸟瞰所有群当日报告状态、生成今日日报、预览/打分报告、管理群组与反馈。后端 API（FastAPI，`/api/v1`，54 端点）已完整实现并验证可用，**但前端缺失**（`web/static/` 目前只有 `.gitkeep`）。

### 2.2 已完成的设计资产（OD 侧）
Open Design 项目 **winnow**（id `d585d435-5279-4642-b37f-489ea35236e1`，daemon :7456，设计系统 `lovable`）中已生成全套页面：
- `index.html` — 仪表盘（鸟瞰全局）
- `reports.html` — 报告浏览 + 详情 + 评分反馈
- `groups.html` / `groups-config.html` — 群组配置
- `data.html` / `settings.html` — 数据探查 / 设置
- （memos / runs / feedback 页待生成）

这些是**单文件静态原型**：数据写死、vanilla JS 交互、Tailwind + 内联 CSS。

OD 已按需求做了定点裁剪：
- **index** 去掉「整体评分★ / 待评分筛选 / 待反馈计数」——index 只负责鸟瞰全局，评分反馈这种交互放到 reports。
- **reports** 保留「星级评分 + 评分反馈抽屉」（后端 feedback 接口可用），删掉「列表每行统计列」和「议题追踪」这两类后端无对应数据的部分。

### 2.3 为什么走 HTML 直连（而非 React）
本项目原计划是 React+Vite+Tailwind 脚手架（见 worktree-web-ui 分支与历史 plan）。但 OD 已产出高质量 HTML 设计，直接对接能：
- 省掉一轮「HTML → React 组件」移植；
- 立即用上 OD 的视觉设计；
- 后端 API 零改动。

代价：前端是 vanilla JS 多页（无组件化、无构建步骤）。**但这不破坏前后端分离**（见 §3），且未来若升级到 React，后端 API 完全不用动——这正是分离的价值所在。

### 2.4 运行环境（执行对接时的事实）
- OD daemon：`http://127.0.0.1:7456`（启动方式见 memory `od-daemon`）。
- 后端：`python -m poetry run winnow web` → `http://127.0.0.1:8100`。
- `/ui` 挂载：`StaticFiles(directory=web/static, html=True)` —— `/ui/` 自动 serve `index.html`，`/ui/reports.html` 等直接可访问，**同源无 CORS**。
- 鉴权：GET/OPTIONS 免 key；写操作经 `X-API-Key`，dev 未配 `web_api_key` 时自动放行（最小闭环可直接跑通）。
- `web/static/` **不被 gitignore**（实测 `.gitignore` 无相关规则），HTML/CSS/JS 直接被 git 跟踪，**无需 `-f`**。已纳入 `index.html` / `reports.html`。
- **远程开发**：远程环境**无需 OD**。`git clone` → `winnow web` → `http://127.0.0.1:8100/ui/` 即看到前端（静态 HTML，对接前数据写死可预览设计）。OD 仅本地用于设计迭代，定稿后拷入 `web/static/`。OD 工作流自身的跨机器同步（项目源 + memory）见 memory `od-daemon`（独立私有云盘，与本项目 git 解耦）。

---

## 3. 架构：前后端分离

```
┌─────────────────────────────┐         fetch /api/v1/* (同源 JSON)
│  前端  静态 HTML/JS/CSS      │ ◄──────────────────────────────────► ┌──────────────────────────┐
│  web/static/  (/ui serve)   │                                       │  后端  FastAPI 纯 API     │
│  index.html  reports.html … │                                       │  /api/v1  (54 端点)       │
│  + api.js (fetch 封装)      │                                       │  数据/LLM/SQLite/MemOS    │
└─────────────────────────────┘                                       └──────────────────────────┘
        ▲                                                                       ▲
        │ get_artifact 覆盖                                                     │ 独立开发
   ┌────┴──────────────┐                                                ┌───────┴────────┐
   │  Open Design      │                                                │  Python 后端   │
   │  项目 winnow     │                                                │  src/...       │
   └───────────────────┘                                                └────────────────┘
```

- **后端**：FastAPI，纯 API，`/api/v1/*`，不渲染任何 HTML。负责数据抓取、LLM 报告生成、SQLite 三层存储、MemOS 记忆。
- **前端**：静态 HTML/JS/CSS，放 `web/static/`，由 FastAPI `StaticFiles` 在 `/ui/` 同源 serve，仅通过 `fetch` 调 API。
- **通信**：同源 HTTP + JSON，dev/prod 一个端口（:8100）搞定，无 CORS、无代理配置。
- **设计源**：OD 项目是视觉设计的「源头」，重设计后用 `get_artifact` 拉最新 HTML 覆盖 `web/static/`。

### 为什么能「独立并行迭代」
- 后端加/改端点 → 前端只在对应 HTML 加一处 fetch，后端不依赖前端。
- 前端改视觉/交互 → 只动 HTML/CSS/JS，后端零改动。
- OD 重新设计页面 → 重新拉 HTML 覆盖，API 契约与对接代码保持不变。
- 三条线（后端 API / 前端页面 / OD 设计）可并行推进，**前提是 API 字段契约不变**。

---

## 4. 实现效果（重点描述效果）

### 4.1 管理员使用效果
打开 `http://127.0.0.1:8100/ui/`：
- **仪表盘**实时显示所有群当日状态（成功 / 失败 / 未生成），一键「生成全部今日日报」；点任一群内联预览当日报告（概要、重要通知、议题、资源、工程、趋势、亮点、活跃成员），一键跳转完整报告。
- **报告页**按日倒序浏览历史日报，展开看完整内容（议题时间段/参与者/结论、趋势、亮点、资源、工程问题），给议题/资源/整体**打分反馈**，查看**版本历史与 diff**，导出 Markdown、推送飞书、LLM 评估、重新生成。
- 所有数字与内容都来自**真实后端**，不再是写死样例；操作（生成/评分/导出）真实写库、真实触发任务。

### 4.2 开发迭代效果
- 改一个页面 = 改一个 HTML 文件，浏览器刷新即生效，**无构建步骤、无热重载配置**。
- API 字段名 == 前端访问的字段名（契约对齐，见 §5），「去硬编码、接 API」是**机械的字段路径替换**，低风险。
- int-bool / envelope / 分页等数据形态坑集中在 `api.js` 一处，全站统一受用。

### 4.3 设计迭代效果
- OD 重新生成或微调页面 → `get_artifact` 拉最新 HTML 覆盖 `web/static/` → 视觉立即更新，而对接代码（数据替换部分）保持不变。
- 视觉设计与数据对接解耦：设计师在 OD 里自由迭代视觉，不碰 fetch 逻辑；开发者改 fetch，不碰视觉。

---

## 5. 数据契约（API ↔ 前端字段对齐）

OD 样例数据用的字段名 == 后端 schema 字段名，故对接是机械替换。需注意的形态差异（集中在 `api.js` 处理）：

- **int-bool**：`GroupOut.is_active/feishu_enabled/daily_report_enabled`、`CoreTopicOut.is_active` 是 `0/1` → 用 `x==1` 判断；`OverviewGroupItem`、`KeyPeopleOut.is_active` 是真 `boolean`，直接判真。
- **envelope**：`GET /reports/{id}/content` 返回 `{report_type, group_id, date, data}` → 页面字段取自 `.data`（非裸 passthrough）。
- **分页**：`PaginatedResponse{total, page, page_size, items}` → 取 `.items`；MemOS cube 列表是 `.cubes`（非 items）。
- **camelCase**：`L1MessageOut.serverID`（其余字段 snake_case）。

---

## 6. 本次对接任务范围（已与用户确认）

- **范围**：`index.html` + `reports.html` **同时**对接（两个核心页）。
- **路线**：HTML 直连后端（放弃 React 移植）。
- **serve**：FastAPI `/ui` 同源 serve，页间 `<a>` 跳转。
- **其余页**（groups / data / settings / memos / runs）本次不做，后续按需。

---

## 7. 执行计划（下次执行的初始上下文）

**前提**：后端 `winnow web` 可起（:8100）；`web/static/` 已含 `index.html` / `reports.html`（OD 导出，git 跟踪）。OD daemon :7456 仅在需要重新设计/导出时启动。

1. **拉文件** ✅ 已完成：`index.html` / `reports.html` 已从 OD 拷入 `web/static/`。OD 重设计后重跑此步（从 OD 项目目录 `cp` 或 `get_artifact`）覆盖更新。
2. **写 `api.js`**：`web/static/api.js` —— `api(path, opts)` 封装（同源 fetch、写操作按需带 `X-API-Key`、非 2xx 抛错）+ 形态助手（`toBool` / 取 `.data` / 取 `.items`）。两页 `<script src="api.js">` 引入。
3. **index.html**：删内联 `groups/status/reports` →
   - `GET /overview` 填状态行（成功/失败计数）+ 群组列表；
   - 群块展开 → `GET /reports/{report_version_id}/content`（取 `.data`）填今日日报块（懒加载）；
   - 新建群 → `POST /groups`，成功后重拉 overview；
   - 操作行 → `POST /reports/{id}/regenerate`、`GET /reports/{id}/export`、`POST /reports/{id}/feishu`、`POST /judge`；
   - `[查看完整报告]` → `<a href="/ui/reports.html?group_id=…">`。
4. **reports.html**：删内联 `report_list/detail/feedback_list/judge_result` →
   - 读 URL `?group_id=` → `GET /reports?group_id=`（取 `.items`）填时间线；
   - 展开 → `GET /reports/{id}/content`（取 `.data`）；
   - 评分 → `POST /feedback`；评分显示 → `GET /feedback?group_id=&date=`；
   - 版本历史 → `GET /reports/{id}/versions` + `GET /reports/{id}/diff`；
   - consume/rollback → `POST /feedback/{id}/consume|rollback`；
   - 操作行 → regenerate/export/feishu/judge；
   - `[返回]` → `<a href="/ui/">`。
5. **验证**：起后端 `winnow web`（:8100），开 `http://127.0.0.1:8100/ui/`，DevTools Network 确认 `/api/v1/*` 200、真实数据渲染、无 console error；逐功能过（index 群列表/日报块/新建群/操作行；reports 时间线/展开/评分/版本历史）。

---

## 8. 索引（参考文档）—— 含时效性与局限

> **可信度总原则**：
> - **OD 项目 winnow 里的 HTML 文件 = 前端设计的唯一可信当前来源**。任何「页面现在长什么样」的判断都以它为准，**不要信线框图或提示词文档**。
> - 代码文件（schema / app.py / routes）2026-06-16 核实过、较新，但**行号会随提交漂移**——定位以 grep 为准，字段以实际 API 响应为终判。
> - wireframes / od-prompts 是**历史草稿与输入**，记录「当初怎么想 / 怎么发指令」，不代表当前页面或后端状态。
> - 页面生成状态见 §9：本次只对接 index + reports，其余页（已生成或未生成）暂不动。

| 主题 | 路径 | 时效 / 局限 |
|------|------|------------|
| 设计线框图（8 页布局 + 组件清单） | `docs/web-redesign-wireframes.md` | ⚠️ **过时草稿**。早期线框图，OD 实际产物已多处偏离（index 去掉评分/待评分/待反馈；reports 改了结构）。仅作「最初布局意图」参考，**不代表当前页面**。 |
| OD 提示词与对接思路 | `docs/od-prompts/`（README、INTEGRATION、各页 .md） | ⚠️ 历史输入。部分页（memos/runs/feedback）提示词写了但 OD **无产物**。INTEGRATION.md 讲的是「HTML→React 移植」，与本次「HTML 直连」路线冲突；其中数据形态 / 字段契约部分仍有效。 |
| `/ui` 挂载 / 中间件 | `web/app.py`、`web/middleware/auth.py` | ✅ 较新（2026-06-16 核实）。行号会漂移，用 grep 定位。 |
| 后端 schema（字段契约） | `web/schemas/{overview,reports,feedback,…}.py` | ✅ 当前有效，字段契约源头。新增字段会变，对接前对照实际 API 响应。 |
| report content envelope | `web/services/report_service.py` | ✅ 有效。行号会漂移。 |
| feedback 端点实现 | `web/routes/feedback.py` + `services/feedback_service.py` | ✅ 有效（已核实 5 端点全实现）。 |
| reports 端点 | `web/routes/reports.py` | ✅ 有效。 |
| OD daemon 启动 | memory `od-daemon` | ✅ 有效。 |
| 已废弃 React 脚手架 | worktree-web-ui 分支 | ⚠️ 本次不走、保留备升级。其内 React 组件 / `types/api.ts` 可能过时，引用需复核。 |

---

## 9. 当前状态（2026-06-28）

**OD 页面生成状态**（OD 项目 winnow 内的实际文件）：

| 页面 | OD 产物 | 裁剪 | 对接状态 |
|------|---------|------|----------|
| index.html（仪表盘） | ✅ 已生成 | ✅ 已裁剪（去评分 / 待评分 / 待反馈） | ✅ 已对接（/overview、/reports、/judge） |
| reports.html（报告+评分） | ✅ 已生成 | ✅ 已裁剪（去列表统计 / 议题追踪，保留评分反馈） | ✅ 已对接（reports / feedback / versions） |
| groups-config.html（单群配置） | ✅ 已生成 | ✅ 已裁剪 + 接 API（2026-06-28） | ✅ 已对接（/groups CRUD + /key-people + /core-topics + /runs） |
| groups.html（群组列表） | ✅ 已生成 | ❌ 未裁剪 | ⏸ 暂不做（index 已有群列表表） |
| data.html / settings.html | ✅ 已生成 | ❌ 未裁剪 | ⏸ 暂不做 |
| memos / runs / feedback 页 | ❌ 未生成 | — | ⏸ 不准备生成（暂） |

- **前端 HTML**（`web/static/`，git 跟踪）：`index.html` / `reports.html` / `groups-config.html` 已入并接 API；`api.js` 为统一 fetch 封装 + 形态助手（含 204 守卫，供 DELETE 空体端点用）。
- **后端**：完整可用（groups / key-people / core-topics / feedback / judge / reports / versions / diff / runs 均已实现）。
- **已知 UX 点（groups-config）**：① 保留 OD 草稿的飞书校验——群 `feishu_enabled=1` 但无 `feishu_table_id` 时，配置页保存会被前端拦下，须先填表 ID 或关飞书开关。② 后端 `update_group` 用 `exclude_none`，前端清空文本字段须发 `''`（非 null）才能真正清除。
