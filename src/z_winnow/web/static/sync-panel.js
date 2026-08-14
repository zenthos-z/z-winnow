/* sync-panel.js — ECS 服务器同步面板（模态框版）
 * 打开 → 显示上次同步时间 + [开始同步] → 一键推送 → 轮询 5 阶段进度
 * 复用 .bp-overlay/.bp-modal 骨架与全局 api()/toast()/esc()。
 */

/* 全局按钮点击入口（onclick="onSyncClick()"） */
function onSyncClick() {
    var btn = document.getElementById('btn-sync');
    if (!btn) return;
    if (btn.classList.contains('is-gen')) {
        SYNC_PANEL.open();            /* 同步中 → 展开进度 */
    } else if (btn.classList.contains('is-done')) {
        SYNC_PANEL._resetBtn();
        SYNC_PANEL.open();
    } else {
        SYNC_PANEL.open();
    }
}

var SYNC_PANEL = {
    _overlay: null,
    _pollTimer: null,
    _lastStage: null,

    /* sync push 的 5 个阶段（顺序固定，来自后端 progress_cb 的 stage_id） */
    STAGES: [
        { id: 'snapshot', label: '生成 L3 快照' },
        { id: 'connect', label: '连接 ECS' },
        { id: 'upload_snapshot', label: '上传 L3 快照' },
        { id: 'upload_processed', label: '同步 processed JSON' },
        { id: 'upload_keys', label: '同步鉴权配置' }
    ],

    ICONS: {
        cloud: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>',
        close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
        check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
    },

    /* ── 自动推送开关（localStorage 持久化；批量生成完成时读取） ── */
    _autoPushKey: 'bp_auto_push',
    isAutoPushOn: function () {
        try { return localStorage.getItem(this._autoPushKey) === '1'; } catch (e) { return false; }
    },
    _onAutoPushToggle: function (on) {
        try { localStorage.setItem(this._autoPushKey, on ? '1' : '0'); } catch (e) {}
        toast(on ? '已开启：生成完成后自动同步到 ECS' : '已关闭自动同步');
    },

    /* ── 入口 ── */
    open: function () {
        var self = this;
        api('/sync/progress').then(function (p) {
            if (p && p.state === 'syncing') {
                self._renderProgress(p);
                self._startPolling();
            } else {
                self._renderSetup(p);
            }
        }).catch(function () {
            /* 端点不可用也允许打开（只展示「开始同步」） */
            self._renderSetup({ state: 'idle', last_sync: null });
        });
    },

    close: function () {
        /* 同步中关闭：保留按钮态 + 继续轮询（只更新按钮），不清理 _pollTimer */
        if (this._overlay) { this._overlay.remove(); this._overlay = null; }
        var btn = document.getElementById('btn-sync');
        if (btn) btn.classList.remove('is-open');
    },

    _resetBtn: function () {
        var btn = document.getElementById('btn-sync');
        var text = document.getElementById('sync-text');
        if (btn) btn.classList.remove('is-gen', 'is-done');
        if (text) text.textContent = 'ECS 服务器同步';
    },

    /* ── 设置视图：上次同步摘要 + 开始同步 ── */
    _renderSetup: function (p) {
        var I = this.ICONS;
        var last = (p && p.last_sync) || null;
        var sub = last ? '上次同步 ' + this._fmtTime(last.finished_at) : '把本地 L3 数据推送到 ECS 公网服务';

        var html = '';
        html += '<div class="bp-overlay" id="sp-overlay">';
        html += '<div class="bp-modal" role="dialog" aria-modal="true" aria-label="ECS 服务器同步">';
        html += '<div class="bp-head"><div class="bp-head-left"><div class="bp-head-icon">' + I.cloud + '</div>';
        html += '<div><div class="bp-title">ECS 服务器同步</div><div class="bp-subtitle" id="sp-sub">' + esc(sub) + '</div></div></div>';
        html += '<button type="button" class="bp-close" onclick="SYNC_PANEL.close()" aria-label="关闭">' + I.close + '</button></div>';
        html += '<div class="bp-body" id="sp-body">';

        if (last) {
            html += '<div class="bp-section"><div class="bp-section-label">上次同步</div>';
            html += '<div class="sp-summary">';
            html += '<div>完成时间 · <strong>' + esc(this._fmtTime(last.finished_at)) + '</strong>' + (last.duration_ms != null ? ' · 耗时 ' + this._fmtDur(last.duration_ms) : '') + '</div>';
            html += '<div>L3 快照 · <strong>' + this._fmtBytes(last.snapshot_bytes) + '</strong></div>';
            html += '<div>processed JSON · <strong>' + (last.processed_synced ? '已同步' : '未同步') + '</strong>　 鉴权配置 · <strong>' + (last.keys_synced ? '已同步' : '未同步') + '</strong></div>';
            html += '<div style="font-family:var(--font-mono);font-size:var(--text-xs);color:var(--meta);word-break:break-all">' + esc(last.remote_snapshot_path || '') + '</div>';
            html += '</div></div>';
        } else {
            html += '<div class="sp-summary">尚未同步过。点击「开始同步」将推送：<strong>L3 快照</strong>（议题/资源/日报）+ <strong>processed JSON</strong> + <strong>鉴权配置</strong> 到 ECS。</div>';
        }

        html += '<div class="bp-section"><div class="bp-section-label">本地 vs ECS 数据</div><div class="sp-compare" id="sp-compare">正在查询…</div></div>';

        html += '<div class="bp-section"><label class="sp-toggle-row"><span class="sp-toggle-text"><span class="sp-toggle-title">生成完成后自动同步</span><span class="sp-toggle-desc">每次批量生成结束，自动把最新 L3 推送到 ECS</span></span>';
        html += '<span class="sp-switch"><input type="checkbox" id="sp-autopush"' + (this.isAutoPushOn() ? ' checked' : '') + ' onchange="SYNC_PANEL._onAutoPushToggle(this.checked)"><span class="sp-switch-track"></span></span></label></div>';

        html += '</div>'; /* bp-body */
        html += '<div class="bp-foot"><span class="bp-foot-hint">推送期间可关闭此窗口，再次点击按钮可恢复查看</span>';
        html += '<button type="button" class="btn btn-ghost btn-sm" onclick="SYNC_PANEL.close()">取消</button>';
        html += '<button type="button" class="bp-submit" id="sp-start" onclick="SYNC_PANEL._startSync()">开始同步</button>';
        html += '</div></div></div>';

        var root = document.createElement('div');
        root.innerHTML = html;
        var overlay = root.firstElementChild;
        overlay.addEventListener('click', function (e) { if (e.target === overlay) SYNC_PANEL.close(); });
        document.body.appendChild(overlay);
        this._overlay = overlay;
        var btn = document.getElementById('btn-sync');
        if (btn) btn.classList.add('is-open');

        /* 失败可忽略（ECS 未配置时 /sync/status 返 400） */
        api('/sync/status').then(this._renderCompare.bind(this)).catch(function () {
            var el = document.getElementById('sp-compare');
            if (el) el.textContent = '（无法连接 ECS 查询比对，可能未配置 SSH）';
        });
    },

    _renderCompare: function (r) {
        var el = document.getElementById('sp-compare');
        if (!el) return;
        var tables = ['groups', 'topic_summaries', 'report_versions', 'feedback_events'];
        var ecsL3 = r.ecs_l3;
        var lines = tables.map(function (t) {
            var lv = (r.local && r.local[t] != null) ? r.local[t] : '?';
            var ev;
            if (ecsL3 && typeof ecsL3 === 'object' && !Array.isArray(ecsL3)) ev = ecsL3[t] != null ? ecsL3[t] : '?';
            else if (ecsL3 === 'NOT_EXISTS') ev = '未 push';
            else ev = '(查询失败)';
            return esc(t) + '  本地 ' + lv + '  ECS ' + ev;
        });
        lines.push('inbox 待 pull · ' + (r.inbox_pending_pull || 0) + ' 条');
        el.innerHTML = lines.join('<br>');
    },

    /* ── 进度视图：进度条 + 阶段清单 ── */
    _renderProgress: function (p) {
        var I = this.ICONS;
        var overlay = this._overlay;
        if (!overlay) {
            overlay = this._ensureProgressOverlay();
        }
        var state = (p && p.state) || 'syncing';
        var headText = state === 'failed' ? '同步失败' : (state === 'done' ? '同步完成' : '同步中…');
        var stat = (p && p.pct != null) ? (p.pct + '%') : '';

        var html = '';
        html += '<div class="bp-modal" role="dialog" aria-modal="true" aria-label="ECS 服务器同步">';
        html += '<div class="bp-head"><div class="bp-head-left"><div class="bp-head-icon"' + (state === 'done' ? ' style="background:var(--success)"' : '') + '>' + I.cloud + '</div>';
        html += '<div><div class="bp-title">ECS 服务器同步</div><div class="bp-subtitle" id="sp-sub">' + esc(p && p.stage_label ? p.stage_label : headText) + '</div></div></div>';
        html += '<button type="button" class="bp-close" onclick="SYNC_PANEL.close()" aria-label="关闭">' + I.close + '</button></div>';
        html += '<div class="bp-body" id="sp-body">';
        html += '<div class="sp-progress-head">';
        html += '<span class="bp-spinner' + (state === 'done' ? '" style="display:none' : '') + '"></span>';
        html += '<span class="sp-progress-head-text" id="sp-prog-text">' + esc(headText) + '</span>';
        html += '<span class="sp-progress-head-stat" id="sp-prog-stat">' + esc(stat) + '</span>';
        html += '</div>';
        html += '<div class="bp-progress-bar-wrap"><div class="bp-progress-bar-fill" id="sp-prog-bar" style="width:' + (p && p.pct || 0) + '%"></div></div>';
        html += '<div class="sp-stages" id="sp-stages"></div>';
        if (state === 'failed' && p && p.error) {
            html += '<div class="sp-err">' + esc(p.error) + '</div>';
        }
        html += '</div>'; /* bp-body */
        html += '<div class="bp-foot" id="sp-foot"><span class="bp-foot-hint" id="sp-foot-hint">' + (state === 'done' ? '已完成' : '同步进行中…') + '</span>';
        html += '<button type="button" class="bp-submit" onclick="SYNC_PANEL.close()">关闭</button></div>';
        html += '</div>'; /* bp-modal */

        overlay.innerHTML = html;
        overlay.hidden = false;
        this._renderStages(p);
    },

    _ensureProgressOverlay: function () {
        var overlay = document.createElement('div');
        overlay.className = 'bp-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.addEventListener('click', function (e) { if (e.target === overlay) SYNC_PANEL.close(); });
        document.body.appendChild(overlay);
        this._overlay = overlay;
        return overlay;
    },

    _renderStages: function (p) {
        var el = document.getElementById('sp-stages');
        if (!el) return;
        var state = (p && p.state) || 'syncing';
        var cur = (p && p.stage) || 'init';
        if (cur === 'init' || !cur) cur = 'snapshot';
        var curIdx = -1;
        for (var i = 0; i < this.STAGES.length; i++) { if (this.STAGES[i].id === cur) { curIdx = i; break; } }

        var I = this.ICONS;
        var html = this.STAGES.map(function (s, i) {
            var cls = 'is-pending';
            if (state === 'done') cls = 'is-done';
            else if (curIdx < 0) cls = (i === 0 ? 'is-active' : 'is-pending');
            else if (i < curIdx) cls = 'is-done';
            else if (i === curIdx) cls = (state === 'failed' ? 'is-done' : 'is-active');
            var ico = cls === 'is-done' ? '<span class="sp-stage-ico">' + I.check + '</span>' : '<span class="sp-stage-ico"></span>';
            return '<div class="sp-stage ' + cls + '">' + ico + '<span class="sp-stage-label">' + esc(s.label) + '</span></div>';
        }).join('');
        el.innerHTML = html;
    },

    /* ── 触发同步 ── */
    _startSync: function () {
        /* 来自弹窗「开始同步」按钮 —— 打开进度视图 */
        this._postPush(true);
    },

    _autoSync: function () {
        /* 来自批量生成完成 —— 不弹窗，仅更新按钮 + toast；已在同步中则跳过 */
        if (this._pollTimer) return;
        this._postPush(false);
    },

    _postPush: function (openModal) {
        if (openModal) {
            var startBtn = document.getElementById('sp-start');
            if (startBtn) startBtn.disabled = true;
        }
        var self = this;
        api('/sync/push', { method: 'POST' }).then(function (res) {
            var btn = document.getElementById('btn-sync');
            if (btn) { btn.classList.add('is-gen'); btn.classList.remove('is-done'); }
            toast(openModal ? '已开始同步' : '生成完成，已自动开始 ECS 同步');
            if (openModal) {
                self._renderProgress({ state: 'syncing', stage: 'init', stage_label: '初始化', pct: 0 });
            }
            self._startPolling();
        }).catch(function (ex) {
            if (openModal) {
                var sb = document.getElementById('sp-start');
                if (sb) sb.disabled = false;
            }
            var msg = (ex && ex.message) || '';
            if (msg.indexOf('409') >= 0) toast('同步正在进行中');
            else if (msg.indexOf('400') >= 0) toast('ECS 未配置：请在 .env 设置 SSH host/key');
            else toast('启动同步失败：' + msg);
        });
    },

    /* ── 轮询 ── */
    _startPolling: function () {
        this._cleanupPolling();
        var self = this;
        this._pollTimer = setInterval(function () {
            api('/sync/progress').then(function (p) { self._applyProgress(p); }).catch(function () { /* 瞬断忽略 */ });
        }, 1200);
    },

    _applyProgress: function (p) {
        if (!p) return;
        var btn = document.getElementById('btn-sync');
        var syncText = document.getElementById('sync-text');
        var modalOpen = !!document.getElementById('sp-overlay');

        /* 按钮态（无论弹窗开关都更新） */
        if (btn && syncText) {
            if (p.state === 'syncing') {
                btn.classList.add('is-gen'); btn.classList.remove('is-done');
                syncText.textContent = '同步中 ' + (p.pct || 0) + '%';
            }
        }

        if (modalOpen) {
            /* 局部更新进度条 + 文案（避免整窗重绘抖动） */
            var bar = document.getElementById('sp-prog-bar');
            if (bar) bar.style.width = (p.pct || 0) + '%';
            var stat = document.getElementById('sp-prog-stat');
            if (stat) stat.textContent = (p.pct || 0) + '%';
            var sub = document.getElementById('sp-sub');
            if (sub && p.stage_label) sub.textContent = p.stage_label;
            if (p.stage !== this._lastStage) { this._renderStages(p); this._lastStage = p.stage; }
        }

        if (p.state === 'done') this._onDone(p, modalOpen);
        else if (p.state === 'failed') this._onFailed(p, modalOpen);
    },

    _onDone: function (p, modalOpen) {
        this._cleanupPolling();
        var btn = document.getElementById('btn-sync');
        var syncText = document.getElementById('sync-text');
        if (btn) { btn.classList.remove('is-gen'); btn.classList.add('is-done'); }
        if (syncText) syncText.textContent = '同步完成';

        if (modalOpen) {
            /* 收尾重绘到完成态（100% + 全阶段打勾） */
            this._renderProgress({ state: 'done', stage: 'done', stage_label: '同步完成', pct: 100 });
            var self = this;
            setTimeout(function () { if (self._overlay) self.close(); }, 2000);
        }
        toast('ECS 同步完成');
        if (typeof refreshOverview === 'function') refreshOverview();
    },

    _onFailed: function (p, modalOpen) {
        this._cleanupPolling();
        this._resetBtn();
        if (modalOpen) {
            this._renderProgress({ state: 'failed', stage: p.stage || '', stage_label: '同步失败', pct: p.pct || 0, error: p.error });
        }
        toast('同步失败：' + (p.error || '未知错误'));
    },

    _cleanupPolling: function () {
        if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
        this._lastStage = null;
    },

    /* ── 格式化助手 ── */
    _fmtTime: function (iso) {
        if (!iso) return '';
        var d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        var p = function (n) { return String(n).padStart(2, '0'); };
        return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
    },
    _fmtBytes: function (n) {
        n = n || 0;
        if (n < 1024) return n + ' B';
        if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
        return (n / 1048576).toFixed(1) + ' MB';
    },
    _fmtDur: function (ms) {
        if (ms == null) return '';
        if (ms < 1000) return ms + ' ms';
        var s = ms / 1000;
        return s < 60 ? (s.toFixed(1) + ' 秒') : (Math.floor(s / 60) + '分' + Math.round(s % 60) + '秒');
    }
};

/* ESC 关闭（同步中允许关闭、保留态） */
document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && SYNC_PANEL._overlay) SYNC_PANEL.close();
});
