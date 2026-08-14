/* api.js — winnow 前端 fetch 封装 + 数据形态助手
 *
 * 同源 fetch（前端在 /ui/，后端 /api/v1，同端口 :8100，无 CORS）。
 * 写操作（POST/PUT/DELETE）按需带 X-API-Key（dev 未配 web_api_key 时后端放行）。
 *
 * 形态坑（docs/web-frontend-architecture.md §5）集中在此处理：
 * - toBool:   GroupOut.is_active / CoreTopicOut.is_active 是 int 0/1，需 ==1 判断
 *             （OverviewGroupItem / KeyPeopleOut.is_active 是真 bool，直通也成立）
 * - envelope: GET /reports/{id}/content 返回 {report_type, group_id, date, data}，业务字段取 .data
 * - itemsOf:  PaginatedResponse 取 .items；memos cube 列表取 .cubes；裸数组直通
 */

async function api(path, opts = {}) {
  const res = await fetch('/api/v1' + path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${path} → HTTP ${res.status} ${text.slice(0, 160)}`);
  }
  if (res.status === 204) return null; /* No Content（DELETE 软删/硬删）—— 空体，跳过 res.json() */
  return res.json();
}

/* text/plain|markdown 端点（如 GET /reports/{id}/export 返回裸 text/markdown）。
 * 不走 api() 的 res.json() —— 对 text/markdown 必抛 SyntaxError（B4 前端根因：
 * JSON 解析失败先于兜底执行，导出 Markdown 100% 失败）。改走 res.text() 拿裸 string。
 * B4 全局裁定（board r1-W16-A4-0 resolved）：导出端点锁定裸 markdown string，
 * 后端 reports.py 保持 Response(media_type="text/markdown") 现状，根因纯在前端解析方式。 */
async function apiText(path, opts = {}) {
  const res = await fetch('/api/v1' + path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${path} → HTTP ${res.status} ${text.slice(0, 160)}`);
  }
  return res.text();
}

/* int 0/1 → bool（GroupOut/CoreTopicOut 字段）；真 bool 直通 */
const toBool = x => x === true || x === 1 || x === '1' || x === 'true';

/* /reports/{id}/content envelope：业务字段在 .data；非 envelope 直通 */
const envelope = r => (r && typeof r === 'object' && 'data' in r) ? r.data : r;

/* 分页/列表：取 .items（PaginatedResponse）；memos 取 .cubes；裸数组直通 */
const itemsOf = r => (Array.isArray(r) ? r : (r.items || r.cubes || r.groups || []));
