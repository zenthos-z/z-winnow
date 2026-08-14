/* batch-panel.js — 批量生成面板（模态框版）
 * 打开 → 选群 + 选日期 → 数据预检 → 确认生成 → SSE 进度
 */

var BATCH_PANEL = {
    groups: [],
    selections: {},
    batchId: null,
    es: null,
    _overlay: null,
    _activeMenuGid: null,

    /* ── 日期范围计算 ── */
    PRESETS: {
        'yesterday': { label: '昨天', days: 1 },
        '3d': { label: '最近 3 天', days: 3 },
        '7d': { label: '最近 7 天', days: 7 },
        '15d': { label: '最近 15 天', days: 15 },
        '30d': { label: '最近 30 天', days: 30 }
    },

    _presetLabel: function(preset) {
        return (this.PRESETS[preset] && this.PRESETS[preset].label) || '自定义';
    },

    _rangeLabel: function(dateFrom, dateTo) {
        if (!dateFrom || !dateTo) return '';
        var start = new Date(dateFrom + 'T00:00:00');
        var end = new Date(dateTo + 'T00:00:00');
        if (isNaN(start.getTime()) || isNaN(end.getTime())) return '';
        var days = Math.round((end - start) / 86400000) + 1;
        if (days === 1) {
            var yesterday = new Date(); yesterday.setDate(yesterday.getDate() - 1);
            if (start.getFullYear() === yesterday.getFullYear() &&
                start.getMonth() === yesterday.getMonth() &&
                start.getDate() === yesterday.getDate()) return '昨天';
            return (start.getMonth() + 1) + '月' + start.getDate() + '日';
        }
        return days + ' 天';
    },

    _fmtRange: function(dateFrom, dateTo) {
        if (!dateFrom || !dateTo) return '未选择';
        var sm = dateFrom.slice(5);
        var em = dateTo.slice(5);
        if (dateFrom === dateTo) return sm;
        return sm + ' ~ ' + em;
    },

    _calcRange: function(preset) {
        var end = new Date(); end.setDate(end.getDate() - 1);
        var start = new Date(end);
        var days = (this.PRESETS[preset] && this.PRESETS[preset].days) || 1;
        if (days > 1) start.setDate(end.getDate() - (days - 1));
        return { from: start.toISOString().slice(0, 10), to: end.toISOString().slice(0, 10) };
    },

    _countDays: function(fromStr, toStr) {
        if (!fromStr || !toStr) return 0;
        var from = new Date(fromStr), to = new Date(toStr);
        return Math.round((to - from) / 86400000) + 1;
    },

    _isValidDate: function(str) {
        return /^\d{4}-\d{2}-\d{2}$/.test(str) && !isNaN(new Date(str).getTime());
    },

    /* ── SVG 图标（与项目风格一致的 24×24 线条图标）── */
    ICONS: {
        sparkle: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v2M12 17v2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M3 12h2M19 12h2M5.6 18.4l1.4-1.4M17 7l1.4-1.4"/><circle cx="12" cy="12" r="4.5"/></svg>',
        close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
        chevron: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>',
        calendar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>',
        table: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/></svg>',
        dot: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="5"/></svg>',
        check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
    },

    /* ── 入口 ── */
    open: function() {
        if (!window.GROUPS || !window.GROUPS.length) { toast('群组列表尚未加载'); return; }
        this.groups = window.GROUPS.filter(function(g) { return toBool(g.daily_report_enabled); });
        if (!this.groups.length) { toast('没有启用日报生成的群'); return; }

        // 如果正在生成中，恢复到进度视图
        var btn = document.getElementById('btn-gen-all');
        if (btn && btn.classList.contains('is-gen') && this._activeBatch) {
            var ab = this._activeBatch;
            if (ab.groups && ab.groups.length) {
                this._showProgress(ab.groups, ab.batchId);
            } else {
                // groups 未缓存（刷新后从 active 端点恢复），异步拉详情补 groups 再渲染
                var self = this;
                api('/runs/batch/' + encodeURIComponent(ab.batchId)).then(function(detail) {
                    var groups = (detail && detail.groups)
                        ? detail.groups.map(function(g) { return { group_id: g.group_id }; })
                        : [];
                    self._showProgress(groups, ab.batchId);   // 内部自动 _startPolling 重连 SSE
                }).catch(function() { toast('恢复进度失败'); });
            }
            return;
        }

        // 新建设置视图，清除旧批次状态
        this._activeBatch = null;
        this.batchId = null;
        this._cleanupSSE();
        // 清除按钮 done 状态
        if (btn && btn.classList.contains('is-done')) {
            btn.classList.remove('is-done');
            var gt = document.getElementById('gen-text');
            if (gt) gt.textContent = '批量生成日报';
        }

        var yesterday = this._calcRange('yesterday');
        this.selections = {};
        var self = this;
        this.groups.forEach(function(g) {
            self.selections[g.group_id] = {
                checked: true,
                date_from: yesterday.from,
                date_to: yesterday.to,
                preset: 'yesterday'
            };
        });
        this._showSetup();
    },

    close: function() {
        // 生成中关闭：保留 _activeBatch 以便恢复
        var genBtn = document.getElementById('btn-gen-all');
        if (!genBtn || !genBtn.classList.contains('is-gen')) {
            this._activeBatch = null;
        }
        this._cleanupSSE();
        this._closeCalendar();
        this._hidePreviewOverlay();
        if (this._overlay) { this._overlay.remove(); this._overlay = null; }
        if (genBtn) genBtn.classList.remove('is-open');
    },

    /* ── 日历面板关闭 ── */
    _calClickGuard: false,
    _openCalIdx: -1,

    _closeCalendar: function() {
        if (this._openCalIdx < 0) return;
        var cal = document.getElementById('bp-cal-' + this._openCalIdx);
        if (cal) cal.classList.remove('is-open');
        var btn = document.getElementById('bp-range-btn-' + this._openCalIdx);
        if (btn) btn.classList.remove('is-open');
        var row = document.querySelector('.bp-group-row[data-idx="' + this._openCalIdx + '"]');
        if (row) row.classList.remove('has-calendar');
        this._openCalIdx = -1;
        this._activeMenuGid = null;
        this._calSelecting = null;
    },

    /* ── 构建遮罩 + 模态框 ── */
    _ensureOverlay: function() {
        if (this._overlay) this._overlay.remove();
        var overlay = document.createElement('div');
        overlay.className = 'bp-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-label', '批量生成日报');
        overlay.addEventListener('click', function(e) { if (e.target === overlay) BATCH_PANEL.close(); });
        document.body.appendChild(overlay);
        this._overlay = overlay;
        return overlay;
    },

    /* ── 设置视图：选群 + 选日期 ── */
    _showSetup: function() {
        var overlay = this._ensureOverlay();
        var self = this;
        var presets = ['yesterday', '3d', '7d', '15d', '30d'];
        var I = this.ICONS;

        var html = '';
        html += '<div class="bp-modal">';

        // 头部
        html += '<div class="bp-head">';
        html += '<div class="bp-head-left">';
        html += '<div class="bp-head-icon">' + I.sparkle + '</div>';
        html += '<div><div class="bp-title">批量生成日报</div><div class="bp-subtitle">选择群组与日期范围，一键生成多日日报</div></div>';
        html += '</div>';
        html += '<button type="button" class="bp-close" onclick="BATCH_PANEL.close()" aria-label="关闭">' + I.close + '</button>';
        html += '</div>';

        // 主体
        html += '<div class="bp-body">';

        // 日期范围
        html += '<div class="bp-section">';
        html += '<div class="bp-section-label">' + I.calendar + ' 日期范围</div>';
        html += '<div class="bp-preset-row" id="bp-global-presets">';
        for (var i = 0; i < presets.length; i++) {
            var p = presets[i];
            html += '<button type="button" class="bp-preset-chip' + (p === 'yesterday' ? ' is-active' : '') + '" data-preset="' + p + '" onclick="BATCH_PANEL.applyPresetToAll(\'' + p + '\')">' + self._presetLabel(p) + '</button>';
        }
        html += '</div>';
        html += '</div>';

        // 群组列表
        html += '<div class="bp-section">';
        var totalDays = 0;
        for (var gid2 in this.selections) {
            totalDays += this._countDays(this.selections[gid2].date_from, this.selections[gid2].date_to);
        }
        html += '<div class="bp-section-label" style="justify-content:space-between"><span>群组选择</span><span style="font-weight:400;font-size:var(--text-sm);color:var(--muted)" id="bp-count">已选 ' + this.groups.length + ' 群 · 共 ' + totalDays + ' 天</span></div>';
        html += '<div class="bp-group-list" id="bp-group-list">';
        for (var j = 0; j < this.groups.length; j++) {
            var g = this.groups[j], sel = this.selections[g.group_id];
            if (!sel) continue;
            var name = g.display_name || g.group_id || '未知群';
            var checked = sel.checked ? ' checked' : '';
            var dateFrom = sel.date_from || '';
            var dateTo = sel.date_to || '';
            var dateLabel = (dateFrom && dateTo) ? this._rangeLabel(dateFrom, dateTo) : '';
            var dateRange = this._fmtRange(dateFrom, dateTo);
            html += '<div>';
            html += '<div class="bp-group-row' + (sel.checked ? '' : ' is-unchecked') + '" data-gid="' + esc(g.group_id) + '" data-idx="' + j + '">';
            html += '<label class="bp-check-label"><input type="checkbox"' + checked + ' onchange="BATCH_PANEL._toggleGroup(\'' + esc(g.group_id) + '\', this.checked)"><span class="bp-check-box"></span></label>';
            html += '<span class="bp-group-name">' + esc(name) + '</span>';
            html += '<div class="bp-range-wrap">';
            html += '<button class="bp-range-btn" id="bp-range-btn-' + j + '" onclick="BATCH_PANEL._toggleCalendar(' + j + ', event)">';
            html += '<span class="bp-range-label">' + dateLabel + '</span>';
            html += '<span class="bp-range-date">' + dateRange + '</span>';
            html += '<svg class="bp-range-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>';
            html += '</button>';
            html += '</div>';
            html += '</div>';
            // 浮动日历面板（position:fixed，JS 定位）
            html += '<div class="bp-cal-inline" id="bp-cal-' + j + '"></div>';
            html += '</div>';
        }
        html += '</div>';
        html += '</div>'; // bp-section

        html += '</div>'; // bp-body

        // 底部
        html += '<div class="bp-foot">';
        html += '<span class="bp-foot-hint">将生成 <strong id="bp-foot-days">' + totalDays + '</strong> 份日报</span>';
        html += '<button type="button" class="btn btn-ghost btn-sm" onclick="BATCH_PANEL.close()">取消</button>';
        html += '<button type="button" class="bp-submit" id="bp-submit" onclick="BATCH_PANEL._showPreviewOverlay()">确认生成</button>';
        html += '</div>';

        html += '</div>'; // bp-modal

        overlay.innerHTML = html;

        var btn = document.getElementById('btn-gen-all');
        if (btn) btn.classList.add('is-open');

        this._bindGlobalListeners();
    },

    /* ── 全局事件 ── */
    _bindGlobalListeners: function() {
        if (this._listenersBound) return;
        this._listenersBound = true;
        var self = this;
        // 点击空白关闭日历
        document.addEventListener('click', function(e) {
            if (self._calClickGuard) return;
            if (self._openCalIdx < 0) return;
            var cal = document.getElementById('bp-cal-' + self._openCalIdx);
            var btn = document.getElementById('bp-range-btn-' + self._openCalIdx);
            if (cal && btn && !cal.contains(e.target) && !btn.contains(e.target)) {
                self._closeCalendar();
            }
        });
        // 滚动/调整窗口时重新定位日历
        window.addEventListener('scroll', function() {
            if (self._openCalIdx >= 0) self._positionCalendar(self._openCalIdx);
        }, { passive: true });
        window.addEventListener('resize', function() {
            if (self._openCalIdx >= 0) self._positionCalendar(self._openCalIdx);
        });
    },

    /* ── 群勾选 ── */
    _toggleGroup: function(gid, checked) {
        if (!this.selections[gid]) return;
        this.selections[gid].checked = checked;
        var row = this._findGroupRow(gid);
        if (row) row.classList.toggle('is-unchecked', !checked);
        this._updateCount();
    },

    _findGroupRow: function(gid) {
        var rows = document.querySelectorAll('.bp-group-row');
        for (var i = 0; i < rows.length; i++) {
            if (rows[i].getAttribute('data-gid') === gid) return rows[i];
        }
        return null;
    },

    /* ── 日期菜单（fixed 定位）── */
    /* ── 日历浮动面板定位 ── */
    _positionCalendar: function(idx) {
        if (idx < 0 || window.innerWidth <= 560) return;
        var btn = document.getElementById('bp-range-btn-' + idx);
        var cal = document.getElementById('bp-cal-' + idx);
        if (!btn || !cal || !cal.classList.contains('is-open')) return;
        var br = btn.getBoundingClientRect();
        var calH = cal.offsetHeight || 400;
        var spaceBelow = window.innerHeight - br.bottom;
        var spaceAbove = br.top;
        var top = spaceBelow >= calH + 8 ? br.bottom + 4 : br.top - calH - 4;
        cal.style.top = Math.max(4, top) + 'px';
        cal.style.right = (window.innerWidth - br.right) + 'px';
    },

    /* ── 日历开关 ── */
    _toggleCalendar: function(idx, evt) {
        evt.stopPropagation();
        if (this._openCalIdx === idx) { this._closeCalendar(); return; }
        this._closeCalendar();
        this._openCalIdx = idx;
        this._calSelecting = null;
        // 重置日历到当前选定月份
        var gid = this.groups[idx].group_id;
        delete this._calendarState[gid];
        var btn = document.getElementById('bp-range-btn-' + idx);
        if (btn) btn.classList.add('is-open');
        var row = document.querySelector('.bp-group-row[data-idx="' + idx + '"]');
        if (row) row.classList.add('has-calendar');
        var self = this;
        this._renderCalendarPanel(idx);
        setTimeout(function() { self._positionCalendar(idx); }, 0);
    },

    /* ── 预设 ── */
    _setPreset: function(gid, preset) {
        var range = this._calcRange(preset);
        if (!this.selections[gid]) return;
        this.selections[gid].date_from = range.from;
        this.selections[gid].date_to = range.to;
        this.selections[gid].preset = preset;
        delete this._calendarState[gid];
        this._updateRowDisplay(gid);
        this._closeCalendar();
        this._updateCount();
    },

    _setCustom: function(gid, which, value) {
        if (!this.selections[gid]) return;
        if (!this._isValidDate(value)) {
            toast('日期格式无效，请输入 YYYY-MM-DD（如 2026-07-15）');
            this._updateRowDisplay(gid);
            return;
        }
        var sel = this.selections[gid];
        if (which === 'from') sel.date_from = value;
        else sel.date_to = value;
        if (sel.date_from > sel.date_to) {
            toast('开始日期不能晚于结束日期');
            this._updateRowDisplay(gid);
            return;
        }
        sel.preset = 'custom';
        this._updateRowDisplay(gid);
        this._closeCalendar();
        this._updateCount();
    },

    /* ── 日历渲染（OD redesign）── */
    _calendarState: {},
    _calSelecting: null,

    _fmtLocal: function(d) {
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    },

    _getCalendarState: function(gid) {
        if (!this._calendarState[gid]) {
            var sel = this.selections[gid];
            var d = (sel && sel.date_from) ? new Date(sel.date_from + 'T00:00:00') : new Date();
            if (isNaN(d.getTime())) d = new Date();
            this._calendarState[gid] = { year: d.getFullYear(), month: d.getMonth() };
        }
        return this._calendarState[gid];
    },

    _renderCalendarPanel: function(idx) {
        var calEl = document.getElementById('bp-cal-' + idx);
        if (!calEl) return;
        var gid = this.groups[idx].group_id;
        var sel = this.selections[gid];

        var st = this._getCalendarState(gid);
        var y = st.year, m = st.month;
        var today = new Date(); today.setHours(0,0,0,0);
        var todayStr = this._fmtLocal(today);
        var WEEKDAYS = ['一','二','三','四','五','六','日'];

        var firstDay = new Date(y, m, 1);
        var startDow = firstDay.getDay(); startDow = startDow === 0 ? 6 : startDow - 1;
        var prevLast = new Date(y, m, 0);
        var totalDays = new Date(y, m + 1, 0).getDate();
        var cells = [];

        for (var pd = startDow - 1; pd >= 0; pd--) {
            var prevDate = new Date(y, m - 1, prevLast.getDate() - pd);
            cells.push({ date: prevDate, otherMonth: true, weekend: prevDate.getDay() === 0 || prevDate.getDay() === 6 });
        }
        for (var d = 1; d <= totalDays; d++) {
            var dt = new Date(y, m, d);
            cells.push({ date: dt, otherMonth: false, weekend: dt.getDay() === 0 || dt.getDay() === 6, today: this._fmtLocal(dt) === todayStr });
        }
        var rem = 7 - (cells.length % 7);
        if (rem < 7) {
            for (var nd = 1; nd <= rem; nd++) {
                var nextDate = new Date(y, m + 1, nd);
                cells.push({ date: nextDate, otherMonth: true, weekend: nextDate.getDay() === 0 || nextDate.getDay() === 6 });
            }
        }

        var dateFrom = (sel && sel.date_from) ? sel.date_from : '';
        var dateTo = (sel && sel.date_to) ? sel.date_to : '';

        var cellHtml = cells.map(function(c) {
            var ds = BATCH_PANEL._fmtLocal(c.date);
            var cls = 'bp-cal-cell';
            if (c.otherMonth) cls += ' other-month';
            if (c.weekend) cls += ' weekend';
            if (c.today) cls += ' today';
            if (dateFrom && dateTo) {
                if (ds === dateFrom && ds === dateTo) cls += ' range-start range-end';
                else if (ds === dateFrom) cls += ' range-start';
                else if (ds === dateTo) cls += ' range-end';
                else if (ds > dateFrom && ds < dateTo) cls += ' range-mid';
            }
            return '<button type="button" class="' + cls + '" data-date="' + ds + '" onclick="BATCH_PANEL._onCalDayClick(\'' + ds + '\',' + idx + ')">' + c.date.getDate() + '</button>';
        }).join('');

        var dayCount = (dateFrom && dateTo) ? Math.round((new Date(dateTo + 'T00:00:00') - new Date(dateFrom + 'T00:00:00')) / 86400000) + 1 : 0;
        var rangeDisplay = (dateFrom && dateTo) ? '已选 ' + dayCount + ' 天' : '';

        var yesterday = new Date(); yesterday.setDate(yesterday.getDate() - 1);
        var isYesterday = dateFrom && dateTo && dateFrom === dateTo && dateFrom === this._fmtLocal(yesterday);

        var miniPresets = [1,3,7,15,30].map(function(days) {
            var active = days === 1 ? isYesterday : (dateFrom && dateTo && dayCount === days);
            return '<button data-days="' + days + '" onclick="BATCH_PANEL._applyMiniPreset(' + days + ',' + idx + ')" class="' + (active ? 'is-active' : '') + '">' + (days === 1 ? '昨天' : days + '天') + '</button>';
        }).join('');

        var monthLabel = y + '年 ' + (m + 1) + '月';
        var html = '<div class="bp-cal-head">';
        html += '<div class="bp-cal-nav">';
        html += '<button class="bp-cal-nav-btn" onclick="BATCH_PANEL._calPrevMonth(' + idx + ')" aria-label="上个月"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 18-6-6 6-6"/></svg></button>';
        html += '<span class="bp-cal-month-label">' + monthLabel + '</span>';
        html += '<button class="bp-cal-nav-btn" onclick="BATCH_PANEL._calNextMonth(' + idx + ')" aria-label="下个月"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 18 6-6-6-6"/></svg></button>';
        html += '</div>';
        html += '<button class="bp-cal-today-btn" onclick="BATCH_PANEL._calGoToday(' + idx + ')">今天</button>';
        html += '</div>';
        html += '<div class="bp-cal-weekdays">';
        for (var w = 0; w < 7; w++) html += '<span>' + WEEKDAYS[w] + '</span>';
        html += '</div>';
        html += '<div class="bp-cal-grid">' + cellHtml + '</div>';
        html += '<div class="bp-cal-mini-presets">' + miniPresets + '</div>';
        html += '<div class="bp-cal-foot">';
        html += '<span class="bp-cal-range-display">' + rangeDisplay + '</span>';
        html += '<div class="bp-cal-actions">';
        html += '<button class="bp-cal-clear" onclick="BATCH_PANEL._calClear(' + idx + ')">清除</button>';
        html += '<button class="bp-cal-apply" onclick="BATCH_PANEL._calApply(' + idx + ')">应用</button>';
        html += '</div></div>';

        calEl.innerHTML = html;
        calEl.classList.add('is-open');
    },

    /* ── 日历交互 ── */
    _onCalDayClick: function(dateStr, idx) {
        var gid = this.groups[idx].group_id;
        if (!this.selections[gid]) return;
        var sel = this.selections[gid];
        var cs = this._calSelecting;
        var d = new Date(dateStr + 'T00:00:00');

        if (!cs || cs.idx !== idx) {
            // 首次选择或切换群
            this._calSelecting = { idx: idx, start: d, end: d };
        } else if (cs.start && cs.end && cs.start.getTime() === cs.end.getTime()) {
            // 当前是单日选择
            if (d.getTime() === cs.start.getTime()) {
                // 点击同一天 → 清空
                this._calSelecting = { idx: idx, start: null, end: null };
            } else if (d < cs.start) {
                // 点击更早日期 → 原日期变成 end
                this._calSelecting = { idx: idx, start: d, end: new Date(cs.start) };
            } else {
                // 点击更晚日期 → 扩展范围
                this._calSelecting.end = d;
            }
        } else {
            // 已有范围，重新开始
            this._calSelecting = { idx: idx, start: d, end: d };
        }

        cs = this._calSelecting;
        sel.date_from = cs.start ? this._fmtLocal(cs.start) : '';
        sel.date_to = cs.end ? this._fmtLocal(cs.end) : '';
        sel.preset = 'custom';

        // 自动跳月
        if (cs.start) {
            var calSt = this._getCalendarState(gid);
            if (cs.start.getFullYear() !== calSt.year || cs.start.getMonth() !== calSt.month) {
                delete this._calendarState[gid];
            }
        }

        this._refreshCalendarUI(idx);
    },

    _calPrevMonth: function(idx) {
        var gid = this.groups[idx].group_id;
        var st = this._getCalendarState(gid);
        st.month--; if (st.month < 0) { st.month = 11; st.year--; }
        this._refreshCalendarUI(idx);
    },
    _calNextMonth: function(idx) {
        var gid = this.groups[idx].group_id;
        var st = this._getCalendarState(gid);
        st.month++; if (st.month > 11) { st.month = 0; st.year++; }
        this._refreshCalendarUI(idx);
    },
    _calGoToday: function(idx) {
        var gid = this.groups[idx].group_id;
        var td = new Date();
        this._calendarState[gid] = { year: td.getFullYear(), month: td.getMonth() };
        this._refreshCalendarUI(idx);
    },

    _applyMiniPreset: function(days, idx) {
        var gid = this.groups[idx].group_id;
        if (!this.selections[gid]) return;
        var end = new Date(); end.setDate(end.getDate() - 1);
        var start = new Date(end); start.setDate(start.getDate() - days + 1);
        this.selections[gid].date_from = this._fmtLocal(start);
        this.selections[gid].date_to = this._fmtLocal(end);
        this.selections[gid].preset = days === 1 ? 'yesterday' : (days + 'd');
        this._calSelecting = null;
        delete this._calendarState[gid];
        this._closeCalendar();
        this._updateRowDisplay(gid);
        this._updateCount();
    },

    _calClear: function(idx) {
        var gid = this.groups[idx].group_id;
        if (!this.selections[gid]) return;
        this.selections[gid].date_from = '';
        this.selections[gid].date_to = '';
        this.selections[gid].preset = 'custom';
        this._calSelecting = { idx: idx, start: '', end: '' };
        this._refreshCalendarUI(idx);
    },

    _calApply: function(idx) {
        this._closeCalendar();
        var gid = this.groups[idx].group_id;
        this._updateRowDisplay(gid);
        this._updateCount();
    },

    /* ── 局部刷新日历（带 calClickGuard）── */
    _refreshCalendarUI: function(idx) {
        this._calClickGuard = true;
        var gid = this.groups[idx].group_id;
        var sel = this.selections[gid];
        // 更新行按钮文字
        var btn = document.getElementById('bp-range-btn-' + idx);
        if (btn && sel) {
            var dateFrom = sel.date_from || '';
            var dateTo = sel.date_to || '';
            btn.querySelector('.bp-range-label').textContent = (dateFrom && dateTo) ? this._rangeLabel(dateFrom, dateTo) : '';
            btn.querySelector('.bp-range-date').textContent = this._fmtRange(dateFrom, dateTo);
        }
        // 重建日历面板
        this._renderCalendarPanel(idx);
        this._positionCalendar(idx);
        this._updateCount();
        var self = this;
        setTimeout(function() { self._calClickGuard = false; }, 0);
    },

    _updateRowDisplay: function(gid) {
        var sel = this.selections[gid];
        if (!sel) return;
        var row = this._findGroupRow(gid);
        if (!row) return;
        var btn = row.querySelector('.bp-range-btn');
        if (btn) {
            var dateFrom = sel.date_from || '';
            var dateTo = sel.date_to || '';
            btn.querySelector('.bp-range-label').textContent = (dateFrom && dateTo) ? this._rangeLabel(dateFrom, dateTo) : '';
            btn.querySelector('.bp-range-date').textContent = this._fmtRange(dateFrom, dateTo);
        }
    },

    applyPresetToAll: function(preset) {
        var range = this._calcRange(preset);
        for (var gid in this.selections) {
            var sel = this.selections[gid];
            // 只覆盖已勾选的群，保护未勾选群的自定义日期
            if (!sel.checked) continue;
            sel.date_from = range.from;
            sel.date_to = range.to;
            sel.preset = preset;
            delete this._calendarState[gid];
            this._updateRowDisplay(gid);
        }
        var chips = document.querySelectorAll('#bp-global-presets .bp-preset-chip');
        for (var i = 0; i < chips.length; i++) {
            chips[i].classList.toggle('is-active', chips[i].getAttribute('data-preset') === preset);
        }
        this._updateCount();
    },

    _getChecked: function() {
        var ids = [];
        for (var gid in this.selections) {
            if (this.selections[gid].checked) ids.push(gid);
        }
        return ids;
    },

    _updateCount: function() {
        var checked = this._getChecked();
        var totalDays = 0;
        for (var i = 0; i < checked.length; i++) {
            var sel = this.selections[checked[i]];
            totalDays += this._countDays(sel.date_from, sel.date_to);
        }
        var countEl = document.getElementById('bp-count');
        if (countEl) countEl.textContent = '已选 ' + checked.length + ' 群 · 共 ' + totalDays + ' 天';
        var daysEl = document.getElementById('bp-foot-days');
        if (daysEl) daysEl.textContent = '' + totalDays;
        var submitBtn = document.getElementById('bp-submit');
        if (submitBtn) submitBtn.textContent = checked.length > 1 ? ('确认生成（' + checked.length + ' 群）') : '确认生成';
        if (submitBtn) submitBtn.disabled = checked.length === 0;
    },

    /* ── 预览覆盖层 ── */
    _showPreviewOverlay: async function() {
        var checked = this._getChecked();
        if (!checked.length) { toast('请至少勾选一个群'); return; }

        // 验证所有已勾选群都有有效日期
        var hasEmpty = false;
        for (var i = 0; i < checked.length; i++) {
            var sel = this.selections[checked[i]];
            if (!sel.date_from || !sel.date_to) { hasEmpty = true; break; }
        }
        if (hasEmpty) { toast('部分群组尚未选择日期范围'); return; }

        var I = this.ICONS;
        // 构建覆盖层
        var html = '<div class="bp-preview-overlay" id="bp-preview-overlay"><div class="bp-preview-popup" role="dialog" aria-modal="true">';
        html += '<div class="bp-head">';
        html += '<div class="bp-head-left"><div class="bp-head-icon">' + I.table + '</div><div><div class="bp-title">预览生成计划</div><div class="bp-subtitle" id="bp-pv-sub">确认后将开始生成日报</div></div></div>';
        html += '<button type="button" class="bp-close" onclick="BATCH_PANEL._hidePreviewOverlay()" aria-label="关闭"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>';
        html += '</div>';
        html += '<div class="bp-body" style="padding-bottom:var(--space-3)">';
        html += '<div style="text-align:center;padding:var(--space-8);color:var(--muted);font-size:var(--text-sm)"><span class="bp-spinner" style="width:18px;height:18px;border-radius:50%;border:2px solid rgba(28,28,28,.15);border-top-color:var(--fg);animation:bp-spin .7s linear infinite;display:inline-block;vertical-align:middle;margin-right:8px"></span> 正在查询数据源...</div>';
        html += '</div>';
        html += '<div class="bp-foot"><span class="bp-foot-hint">正在加载...</span><button type="button" class="btn btn-ghost btn-sm" onclick="BATCH_PANEL._hidePreviewOverlay()">返回修改</button><button type="button" class="bp-submit" disabled>确认生成</button></div>';
        html += '</div></div>';

        var overlay = document.createElement('div');
        overlay.innerHTML = html;
        document.body.appendChild(overlay.firstElementChild);

        // 绑定遮罩点击关闭
        var self = this;
        document.getElementById('bp-preview-overlay').addEventListener('click', function(e) { if (e.target === this) self._hidePreviewOverlay(); });

        // 加载数据
        try {
            var gidToName = {};
            for (var j = 0; j < this.groups.length; j++) {
                gidToName[this.groups[j].group_id] = this.groups[j].display_name;
            }

            var promises = checked.map(function(gid) {
                var sel2 = self.selections[gid];
                var url = '/data/source-check?group_ids=' + encodeURIComponent(gid) + '&date_from=' + sel2.date_from + '&date_to=' + sel2.date_to;
                return api(url).then(function(r) { return { gid: gid, items: r.items || [] }; }, function() { return { gid: gid, items: [] }; });
            });
            var results = await Promise.all(promises);
            this._renderPreviewOverlay(results, checked, gidToName);
        } catch (ex) {
            toast('预检失败：' + (ex && ex.message || ex));
            this._hidePreviewOverlay();
        }
    },

    _renderPreviewOverlay: function(results, checked, gidToName) {
        var totalDays = 0;
        var totalMsgs = 0;
        var self = this;
        var rows = '';

        for (var g = 0; g < results.length; g++) {
            var gid = results[g].gid;
            var dateItems = results[g].items || [];
            var name = gidToName[gid] || gid;
            var sel = this.selections[gid];
            var days = sel && sel.date_from && sel.date_to ? this._countDays(sel.date_from, sel.date_to) : 0;
            totalDays += days;

            // 按日期排序
            dateItems.sort(function(a, b) { return a.date < b.date ? -1 : 1; });

            for (var d = 0; d < dateItems.length; d++) {
                var it = dateItems[d];
                totalMsgs += it.message_count || 0;
                var rowspan = d === 0 ? ' rowspan="' + dateItems.length + '"' : '';
                var lastClass = d === dateItems.length - 1 ? ' bp-pv-last-in-group' : '';
                rows += '<tr class="' + lastClass + '">';
                if (d === 0) rows += '<td class="bp-pv-name"' + rowspan + '>' + esc(name) + '</td>';
                rows += '<td class="bp-pv-date">' + it.date + '</td>';
                rows += '<td class="bp-pv-status ' + (it.has_data ? 'has' : 'empty') + '">' + (it.has_data ? '有数据' : '无数据') + '</td>';
                rows += '<td class="bp-pv-count num">' + (it.has_data ? (it.message_count || 0).toLocaleString() : '—') + '</td>';
                rows += '</tr>';
            }
        }

        var popup = document.querySelector('.bp-preview-popup');
        if (!popup) return;
        var body = popup.querySelector('.bp-body');
        var foot = popup.querySelector('.bp-foot');
        var sub = document.getElementById('bp-pv-sub');
        if (sub) sub.textContent = results.length + ' 个群 · 共 ' + totalDays + ' 天 · ' + totalMsgs.toLocaleString() + ' 条消息';

        body.innerHTML = '<table class="bp-preview-table"><thead><tr><th>群</th><th>日期</th><th>状态</th><th style="text-align:right">消息数</th></tr></thead><tbody>' + rows + '</tbody></table>';
        body.innerHTML += '<div class="bp-preview-overlay-summary">' + results.map(function(r) {
            var gid2 = r.gid;
            var s2 = self.selections[gid2];
            var d2 = s2 && s2.date_from && s2.date_to ? self._countDays(s2.date_from, s2.date_to) : 0;
            var m2 = (r.items || []).reduce(function(s, it) { return s + (it.message_count || 0); }, 0);
            return '<span><strong>' + esc(gidToName[gid2] || gid2) + '</strong> ' + d2 + ' 天 · ' + m2.toLocaleString() + ' 条</span>';
        }).join('') + '</div>';

        foot.innerHTML = '<span class="bp-foot-hint">共 <strong>' + totalDays + '</strong> 份日报待生成</span><button type="button" class="btn btn-ghost btn-sm" onclick="BATCH_PANEL._hidePreviewOverlay()">返回修改</button><button type="button" class="bp-submit" onclick="BATCH_PANEL._confirmGenerate()">确认生成</button>';
    },

    _hidePreviewOverlay: function() {
        var overlay = document.getElementById('bp-preview-overlay');
        if (overlay) overlay.remove();
    },

    _confirmGenerate: async function() {
        var checked = this._getChecked();
        var groups = [];
        for (var gid in this.selections) {
            var sel = this.selections[gid];
            if (sel.checked && sel.date_from && sel.date_to) {
                groups.push({ group_id: gid, date_from: sel.date_from, date_to: sel.date_to });
            }
        }

        // 关闭预览覆盖层
        this._hidePreviewOverlay();

        // 禁用提交按钮
        var submitBtn = document.getElementById('bp-submit');
        if (submitBtn) submitBtn.disabled = true;

        try {
            var res = await api('/runs/batch-v2', {
                method: 'POST',
                body: JSON.stringify({ groups: groups })
            });
            this.batchId = res.batch_id;
            this._activeBatch = { batchId: res.batch_id, groups: groups };
            // 持久化到 sessionStorage，页面切换后恢复
            try { sessionStorage.setItem('bp_active_batch', JSON.stringify({ batchId: res.batch_id, groups: groups, ts: Date.now() })); } catch(e) {}
            toast('已提交 · 共 ' + (res.total_items || '?') + ' 天');
            this._showProgress(groups, res.batch_id);
            this._startPolling();
        } catch (ex) {
            toast('提交失败：' + (ex && ex.message || ex));
            if (submitBtn) submitBtn.disabled = false;
        }
    },

    /* ── 进度视图 ── */
    _showProgress: function(groups, batchId) {
        // 恢复进度时确保遮罩存在
        var overlay = this._overlay || this._ensureOverlay();
        this._overlay = overlay;

        var btn = document.getElementById('btn-gen-all');
        if (btn) { btn.classList.add('is-gen'); btn.classList.remove('is-open'); }

        var gidToName = {};
        for (var i = 0; i < this.groups.length; i++) {
            gidToName[this.groups[i].group_id] = this.groups[i].display_name;
        }
        var I = this.ICONS;

        var html = '';
        html += '<div class="bp-modal">';
        html += '<div class="bp-head">';
        html += '<div class="bp-head-left">';
        html += '<div class="bp-head-icon" style="background:var(--success)">' + I.sparkle + '</div>';
        html += '<div><div class="bp-title">正在生成</div><div class="bp-subtitle">可关闭此窗口稍后查看，再次点击生成按钮可恢复进度</div></div>';
        html += '</div>';
        html += '<button type="button" class="bp-close" onclick="BATCH_PANEL.close()" aria-label="关闭">' + I.close + '</button>';
        html += '</div>';

        html += '<div class="bp-body">';
        html += '<div class="bp-progress">';
        html += '<div class="bp-progress-head">';
        html += '<span class="bp-spinner"></span>';
        html += '<span class="bp-progress-head-text" id="bp-prog-text">正在连接...</span>';
        html += '<span class="bp-progress-head-stat" id="bp-prog-stat">' + groups.length + ' 群</span>';
        html += '</div>';
        html += '<div class="bp-progress-bar-wrap"><div class="bp-progress-bar-fill" id="bp-prog-bar" style="width:0%"></div></div>';
        html += '<div class="bp-progress-items" id="bp-prog-items">';
        for (var j = 0; j < groups.length; j++) {
            var g = groups[j];
            html += '<div class="bp-progress-item" data-gid="' + esc(g.group_id) + '">';
            html += '<span class="bp-group-name">' + esc(gidToName[g.group_id] || g.group_id) + '</span>';
            html += '<span class="bp-item-bar"><span class="bp-item-bar-fill" style="width:0%"></span></span>';
            html += '<span class="bp-item-status">...</span>';
            html += '</div>';
        }
        html += '</div>';
        html += '</div>';
        html += '</div>'; // bp-body
        html += '</div>'; // bp-modal

        overlay.innerHTML = html;
        overlay.hidden = false;

        // 如果是恢复已有批次，重新连接 SSE
        if (batchId) {
            this.batchId = batchId;
            this._startPolling();
        }
    },

    /* ── SSE 轮询 ── */
    _startPolling: function() {
        this._cleanupSSE();
        var self = this;
        try {
            this.es = new EventSource('/api/v1/runs/batch/' + this.batchId + '/stream');
            this.es.onmessage = function(ev) {
                var data;
                try { data = JSON.parse(ev.data); } catch (e) { return; }
                if (data.type === 'batch_update') self._onBatchUpdate(data);
                else if (data.type === 'item_update') self._onItemUpdate(data);
                else if (data.type === 'batch_complete') self._onComplete(data);
            };
            this.es.onerror = function() { /* 瞬断忽略 */ };
        } catch (e) {
            toast('SSE 连接失败');
        }
    },

    _onBatchUpdate: function(data) {
        var textEl = document.getElementById('bp-prog-text');
        var statEl = document.getElementById('bp-prog-stat');
        var barEl = document.getElementById('bp-prog-bar');
        if (textEl) textEl.textContent = '生成中...';
        if (statEl) statEl.textContent = (data.completed + data.failed + data.skipped_empty) + ' / ' + data.total + ' 完成';
        if (barEl) barEl.style.width = (data.progress_pct || 0) + '%';

        var genText = document.getElementById('gen-text');
        if (genText) {
            genText.textContent = '生成中 ' + (data.completed + data.failed + data.skipped_empty) + '/' + data.total;
        }
    },

    _onItemUpdate: function(data) {
        // 更新单个群的进度条和状态
        var item = document.querySelector('.bp-progress-item[data-gid="' + esc(data.group_id) + '"]');
        if (!item) return;
        var barFill = item.querySelector('.bp-item-bar-fill');
        var statusEl = item.querySelector('.bp-item-status');
        if (barFill) barFill.style.width = (data.progress_pct || 0) + '%';
        if (statusEl) {
            if (data.status === 'completed') {
                item.classList.add('is-ok');
                statusEl.textContent = '成功';
            } else if (data.status === 'failed') {
                item.classList.add('is-bad');
                statusEl.textContent = data.error_message ? '失败' : '失败';
            } else if (data.status === 'skipped_empty') {
                item.classList.add('is-ok');
                statusEl.textContent = '无数据';
            } else {
                statusEl.textContent = '生成中 ' + (data.progress_pct || 0) + '%';
            }
        }
    },

    _onComplete: function(data) {
        this._cleanupSSE();
        this._activeBatch = null; /* 允许下次打开时进入设置视图 */
        try { sessionStorage.removeItem('bp_active_batch'); } catch(e) {}

        var textEl = document.getElementById('bp-prog-text');
        var statEl = document.getElementById('bp-prog-stat');
        var barEl = document.getElementById('bp-prog-bar');
        if (textEl) textEl.textContent = '生成完成';
        if (statEl) statEl.textContent = (data.completed || '?') + ' 成功 / ' + (data.failed || 0) + ' 失败';
        if (barEl) { barEl.style.width = '100%'; barEl.parentElement.classList.add('bp-progress-done'); }

        var genText = document.getElementById('gen-text');
        var btn = document.getElementById('btn-gen-all');
        if (genText) genText.textContent = '生成完毕';
        if (btn) { btn.classList.remove('is-gen'); btn.classList.add('is-done'); }

        var self = this;
        setTimeout(function() {
            if (self._overlay && document.getElementById('bp-prog-text')) self.close();
        }, 2000);

        toast('批量生成完成');
        if (typeof refreshOverview === 'function') refreshOverview();
        /* 自动推送：若 ECS 同步面板开启了「生成完成后自动同步」 */
        if (typeof SYNC_PANEL !== 'undefined' && SYNC_PANEL.isAutoPushOn && SYNC_PANEL.isAutoPushOn()) {
            SYNC_PANEL._autoSync();
        }
    },

    _cleanupSSE: function() {
        if (this.es) { try { this.es.close(); } catch (e) {} this.es = null; }
    }
};

/* ESC 关闭 */
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        // 优先关闭预览覆盖层
        if (document.getElementById('bp-preview-overlay')) {
            BATCH_PANEL._hidePreviewOverlay();
            return;
        }
        // 关闭日历面板
        if (BATCH_PANEL._openCalIdx >= 0) {
            BATCH_PANEL._closeCalendar();
            return;
        }
        // 如果生成中，不关闭模态框
        var genBtn = document.getElementById('btn-gen-all');
        if (genBtn && genBtn.classList.contains('is-gen')) return;
        if (BATCH_PANEL._overlay) {
            BATCH_PANEL.close();
        }
    }
});
