/* Vítr – přehled a nastavení anemometrů. Stavba a chování podle webu Retenční nádrže. */
(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const ico = (name, cls) => `<svg class="ico${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
    const num = (v, d = 1, unit = '') => (v == null || isNaN(v)) ? '–'
        : Number(v).toLocaleString('cs-CZ', { minimumFractionDigits: d, maximumFractionDigits: d }) + unit;
    const kmh = (v) => num(v, 1, ' km/h');
    const ms = (v) => (v == null ? '–' : num(v / 3.6, 1, ' m/s'));

    const BFT = [[1, 'bezvětří'], [6, 'vánek'], [12, 'větřík'], [20, 'slabý vítr'], [29, 'mírný vítr'],
        [39, 'čerstvý vítr'], [50, 'silný vítr'], [62, 'prudký vítr'], [75, 'bouřlivý vítr'], [89, 'vichřice'],
        [103, 'silná vichřice'], [118, 'mohutná vichřice'], [Infinity, 'orkán']];
    const bft = (v) => { for (let i = 0; i < BFT.length; i++) if (v < BFT[i][0]) return [i, BFT[i][1]]; return [12, 'orkán']; };
    const STATUS = { online: 'online', stale: 'zpožděná data', offline: 'offline' };

    let STATE = null, ADMIN = false, CURRENT = null, VIEW_ALL = false;
    const ME = { username: '', role: 'user', csrf: '', version: '' };
    try { VIEW_ALL = sessionStorage.getItem('viewAll') === '1'; } catch (e) { /* ignore */ }
    try { CURRENT = Number(localStorage.getItem('node')) || null; } catch (e) { /* ignore */ }

    function toast(msg, isError) {
        const t = $('toast');
        t.textContent = msg;
        t.className = 'toast show' + (isError ? ' error' : '');
        clearTimeout(toast._t);
        toast._t = setTimeout(() => { t.className = 'toast'; }, 3500);
    }

    const nm = (x) => `<span translate="no">${esc(x)}</span>`;
    // síla signálu v % lineárně mezi −110 dBm (mez citlivosti CC1101) a −50 dBm (velmi silný)
    function sigPct(dbm) {
        if (dbm === null || dbm === undefined || Number.isNaN(dbm)) return null;
        return Math.round(Math.min(100, Math.max(0, (dbm + 110) / 60 * 100)));
    }
    function sigWord(dbm) {
        if (dbm == null || Number.isNaN(dbm)) return '';
        if (dbm >= -75) return 'silný';
        if (dbm >= -90) return 'dobrý';
        if (dbm >= -100) return 'slabší';
        return 'slabý';
    }
    function sigTxt(dbm) { return sigPct(dbm) == null ? '–' : `${sigWord(dbm)} · ${sigPct(dbm)} % (${num(dbm, 0)} dBm)`; }

    async function action(fn, okMsg) {
        try {
            const res = await fn();
            if (okMsg) toast(typeof okMsg === 'function' ? okMsg(res) : okMsg);
            return res;
        } catch (e) {
            toast(e.message || 'Chyba spojení se serverem', true);
            return null;
        }
    }

    // ------------------------------- dialogy: potvrzení i formuláře (#cfmModal), jako u retenčky
    // openDialog({title, body: [text | Node], fields, validate, submit, confirm, cancel, danger})
    //   fields:   [{name, label, type, value, hint, placeholder, autocomplete, maxLength, trim}]
    //   validate: (hodnoty) → text chyby | {field, message} | null     (při chybě dialog zůstane otevřený)
    //   submit:   async (hodnoty) → text chyby (dialog zůstane otevřený) | nic (zavře se)
    //   stay:     (hodnoty) → po úspěšném submit dialog nezavírat (zavře ho až tlačítko cancel)
    //   after:    [Node] pod poli · wide: širší dialog · onOpen({inputs, submit, hideErr}) po otevření
    // → Promise<false | true | {hodnoty polí}>. Enter potvrdí, Esc / klik mimo dialog zruší.
    function openDialog(o) {
        return new Promise((resolve) => {
            const m = $('cfmModal'), body = $('cfmBody'), foot = $('cfmFoot');
            const opener = document.activeElement;
            const fields = o.fields || [];
            const inputs = {};
            let busy = false, stayed = null;
            $('cfmTitle').textContent = o.title;
            m.querySelector('.modal').classList.toggle('modal-md', !!o.wide);
            body.textContent = '';
            const put = (x) => {
                if (x == null) return;
                if (typeof x === 'string') { const pp = document.createElement('p'); pp.textContent = x; body.appendChild(pp); } else body.appendChild(x);
            };
            (Array.isArray(o.body) ? o.body : [o.body]).forEach(put);
            fields.forEach((f, i) => {
                const id = 'cfmF' + i;
                const g = document.createElement('div');
                g.className = 'form-group dlg-field';
                const lab = document.createElement('label');
                lab.htmlFor = id; lab.textContent = f.label;
                const input = document.createElement('input');
                input.type = f.type || 'text'; input.id = id;
                input.value = f.value == null ? '' : f.value;
                if (f.placeholder) input.placeholder = f.placeholder;
                input.autocomplete = f.autocomplete || 'off';
                if (f.maxLength) input.maxLength = f.maxLength;
                input.classList.add('unified-input');
                g.append(lab, input);
                if (f.hint) { const h = document.createElement('p'); h.className = 'field-hint'; h.textContent = f.hint; g.appendChild(h); }
                inputs[f.name] = input;
                body.appendChild(g);
            });
            const err = document.createElement('p');
            err.className = 'dlg-error hidden';
            err.id = 'cfmErr';
            err.setAttribute('role', 'alert');
            body.appendChild(err);
            (o.after || []).forEach(put);
            const hideErr = () => {
                err.classList.add('hidden');
                Object.values(inputs).forEach((i) => { i.removeAttribute('aria-invalid'); i.removeAttribute('aria-describedby'); });
            };
            const fail = (e) => {
                const msg = typeof e === 'string' ? e : e.message;
                const inp = (typeof e === 'object' && inputs[e.field]) || Object.values(inputs)[0];
                err.textContent = msg;
                err.classList.remove('hidden');
                if (inp) { inp.setAttribute('aria-invalid', 'true'); inp.setAttribute('aria-describedby', 'cfmErr'); inp.focus(); }
            };
            body.addEventListener('input', hideErr);
            const collect = () => {
                const v = {};
                fields.forEach((f) => { const i = inputs[f.name]; v[f.name] = f.trim ? i.value.trim() : i.value; });
                return v;
            };
            foot.textContent = '';
            const done = (v) => {
                m.classList.add('hidden');
                document.body.classList.remove('modal-open');
                m.removeEventListener('keydown', onKey, true);
                body.removeEventListener('input', hideErr);
                if (opener && opener.focus && opener.offsetParent !== null) opener.focus();
                resolve(v);
            };
            const cancel = document.createElement('button');
            cancel.type = 'button'; cancel.className = 'btn btn-outline'; cancel.textContent = o.cancel || 'Zrušit';
            cancel.addEventListener('click', () => { if (!busy) done(stayed || false); });
            const ok = document.createElement('button');
            ok.type = 'button'; ok.className = 'btn ' + (o.danger ? 'btn-danger-solid' : 'btn-primary'); ok.textContent = o.confirm || 'Potvrdit';
            const submit = async () => {
                if (busy) return;
                hideErr();
                const values = collect();
                if (o.validate) { const e = o.validate(values); if (e) { fail(e); return; } }
                if (o.submit) {
                    busy = true; ok.disabled = true; cancel.disabled = true;
                    let e = null;
                    try { e = await o.submit(values); } catch (x) { e = x.message || 'Chyba spojení se serverem'; }
                    busy = false; ok.disabled = false; cancel.disabled = false;
                    if (e) { fail(e); return; }
                }
                if (o.stay) { stayed = values; o.stay(values); return; }
                done(fields.length ? values : true);
            };
            ok.addEventListener('click', submit);
            foot.append(cancel, ok);
            const onKey = (e) => {
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); if (!busy) done(stayed || false); return; }
                if (e.key === 'Enter' && e.target && e.target.tagName === 'INPUT') { e.preventDefault(); submit(); return; }
                if (e.key !== 'Tab') return;
                const f = Array.from(m.querySelectorAll('button, [href], input, select')).filter((x) => !x.disabled && x.offsetParent !== null);
                if (!f.length) return;
                const i = f.indexOf(document.activeElement);
                if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); } else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
            };
            m.addEventListener('keydown', onKey, true);
            $('cfmClose').onclick = () => { if (!busy) done(stayed || false); };
            m.onmousedown = (e) => { if (e.target === m && !busy) done(stayed || false); };
            m.classList.remove('hidden');
            document.body.classList.add('modal-open');
            const empty = fields.find((f) => !f.value);
            const first = empty ? inputs[empty.name] : fields.length ? inputs[fields[0].name] : cancel;   // formulář: první prázdné pole; potvrzení: bezpečná volba
            first.focus();
            if (first.select) first.select();
            if (o.onOpen) o.onOpen({ inputs, submit, hideErr });
        });
    }
    const confirmDialog = (o) => openDialog(o).then((r) => !!r);
    const formDialog = (o) => openDialog(o).then((r) => (r && typeof r === 'object' ? r : null));

    async function api(path, body) {
        const opt = { credentials: 'same-origin' };
        if (body !== undefined) {
            opt.method = 'POST';
            opt.headers = { 'Content-Type': 'application/json', 'X-CSRF-Token': ME.csrf };
            opt.body = JSON.stringify(body);
        }
        const r = await fetch(path, opt);
        const j = await r.json().catch(() => ({}));
        if (r.status === 401) { window.location.replace('/login'); throw new Error('Přihlaste se'); }
        if (!r.ok) throw new Error(j.error || r.statusText);
        return j;
    }

    function fmtAgo(s) {
        if (s == null) return '';
        if (s < 90) return `před ${Math.round(s)} s`;
        if (s < 5400) return `před ${Math.round(s / 60)} min`;
        if (s < 172800) return `před ${Math.round(s / 3600)} h`;
        return `před ${Math.round(s / 86400)} dny`;
    }
    const fmtDateTime = (ms) => new Date(ms).toLocaleString('cs-CZ', { day: 'numeric', month: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' });
    const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString('cs-CZ', { hour: '2-digit', minute: '2-digit' });
    const age = (n) => (n.last_seen ? STATE.now - n.last_seen : null);
    const nodeById = (id) => (STATE ? STATE.nodes.find((n) => n.id === id) : null);

    // ------------------------------------------------------------------ motiv
    function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
    const T = {};
    function readTheme() {
        Object.assign(T, {
            ink: cssVar('--ink'), ink2: cssVar('--ink-2'), muted: cssVar('--muted'), grid: cssVar('--grid'),
            surface: cssVar('--surface'), lineStrong: cssVar('--line-strong'),
            s1: cssVar('--series-1'), s2: cssVar('--series-2'), s3: cssVar('--series-3'),
            s4: cssVar('--series-4'), s5: cssVar('--series-5'), s6: cssVar('--series-6'), amber: cssVar('--amber')
        });
        if (typeof Chart === 'undefined') return;
        const d = Chart.defaults;
        d.color = T.muted;
        d.borderColor = T.grid;
        d.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
        d.font.size = 12;
        d.scale.grid.color = T.grid;
        d.scale.grid.tickLength = 0;
        d.scale.border.display = false;
        d.scale.ticks.color = T.ink2;
        d.scale.ticks.padding = 6;
        d.elements.line.borderWidth = 2.25;
        d.elements.line.tension = 0.25;
        d.elements.point.radius = 0;
        d.elements.point.hoverRadius = 3;
        d.elements.point.hitRadius = 6;
        const lg = d.plugins.legend;
        lg.align = 'start';
        lg.labels.color = T.ink2;
        lg.labels.usePointStyle = true;
        lg.labels.pointStyle = 'rect';
        lg.labels.boxWidth = 8;
        lg.labels.boxHeight = 8;
        lg.labels.padding = 14;
        const tt = d.plugins.tooltip;
        tt.backgroundColor = T.surface;
        tt.titleColor = T.ink;
        tt.bodyColor = T.ink2;
        tt.borderColor = T.lineStrong;
        tt.borderWidth = 1;
        tt.cornerRadius = 6;
        tt.padding = 10;
        tt.boxPadding = 4;
        tt.usePointStyle = true;
        Chart.defaults.animation.duration = 150;
    }
    function applyTheme(t) {
        if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
        else document.documentElement.removeAttribute('data-theme');
        try { localStorage.setItem('theme', t); } catch (e) { /* ignore */ }
        document.querySelectorAll('#themeSeg button').forEach((b) => b.setAttribute('aria-pressed', b.dataset.themePick === t ? 'true' : 'false'));
        readTheme();
        reloadCharts();
    }

    // ------------------------------------------------------------------ grafy
    // Stejně jako retenčka (1.23+): uprostřed grafu pevná svislá čára, tažením se data posouvají
    // pod ní, bublina nad grafem ukazuje hodnoty pod čarou; graf se otevře s posledním měřením
    // pod čarou. Starší data se dotahují po oknech délky zvoleného rozsahu.
    const PRESETS = [['24h', '24 h', 86400], ['7d', '7 dní', 7 * 86400], ['30d', '30 dní', 30 * 86400], ['1y', 'rok', 365 * 86400]];
    const ranges = {};
    const charts = {};

    function buildRangePicker(el) {
        const key = el.dataset.chart;
        let saved = null;
        try { saved = localStorage.getItem('range:' + key); } catch (e) { /* ignore */ }
        const initial = PRESETS.some((p) => p[0] === saved) ? saved : (el.dataset.default || '24h');
        ranges[key] = initial;
        const seg = document.createElement('div');
        seg.className = 'seg';
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', 'Rozsah');
        PRESETS.forEach(([v, label]) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.textContent = label;
            b.setAttribute('aria-pressed', v === initial ? 'true' : 'false');
            b.addEventListener('click', () => {
                seg.querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', x === b ? 'true' : 'false'));
                ranges[key] = v;
                try { localStorage.setItem('range:' + key, v); } catch (e) { /* ignore */ }
                resetScrub(key);
                reloadChart(key);
            });
            seg.appendChild(b);
        });
        el.appendChild(seg);
    }
    function rangeOf(key) {
        const p = PRESETS.find((x) => x[0] === ranges[key]);
        const to = Math.floor(Date.now() / 1000);
        return { from: to - (p ? p[2] : 86400), to, live: true };
    }

    const p2 = (n) => String(n).padStart(2, '0');
    const fmtStamp = (ms) => { const d = new Date(ms); return `${d.getDate()}.${d.getMonth() + 1}.${d.getFullYear()} ${p2(d.getHours())}:${p2(d.getMinutes())}`; };

    // historie z hubu (čas v ms); uzavřená okna se drží déle v mezipaměti
    const histCache = new Map();
    async function getHistory(r, node) {
        const k = node + '|' + r.from + '|' + r.to;
        const hit = histCache.get(k);
        const past = r.to * 1000 < Date.now() - 3600e3;
        if (hit && Date.now() - hit.at < (past ? 600e3 : 60e3)) return hit.data;
        const data = await api(`/api/history?id=${node}&from=${r.from}&to=${r.to}`);
        data.t = (data.t || []).map((x) => x * 1000);
        histCache.delete(k);
        histCache.set(k, { at: Date.now(), data, to: r.to });
        if (histCache.size > 120) histCache.delete(histCache.keys().next().value);
        return data;
    }
    function dropLiveHistory() {
        const edge = Date.now() / 1000 - 3600;
        histCache.forEach((v, k) => { if (!v.to || v.to >= edge) histCache.delete(k); });
    }

    const SCRUB = {};      // key -> { offset (s, + = do minulosti), chunks, exhausted, first, last (ms), loading }
    const SCRUB_RANGE = {};
    function scrubState(key) { return SCRUB[key] || (SCRUB[key] = { offset: null, home: true, chunks: 1, exhausted: false, first: null, last: null, loading: false }); }
    function homeOffset(key, r) {
        if (!r || !r.live) return 0;
        const span = r.to - r.from, s = SCRUB[key];
        if (!s || s.last == null) return -span / 2;
        return Math.max(-span / 2, Math.min(s.chunks * span, r.from + span / 2 - s.last / 1000));
    }
    function resetScrub(key) { delete SCRUB[key]; }
    function viewRange(key, r) {
        const s = scrubState(key);
        if (s.offset == null || s.home) s.offset = homeOffset(key, r);
        return { from: r.from - s.offset, to: r.to - s.offset };
    }
    function mergeHist(a, b) {
        const at = a.t || [], bt = b.t || [];
        const n = at.length;
        const isSeries = (k) => Array.isArray(a[k]) && a[k].length === n && Array.isArray(b[k]) && b[k].length === bt.length;
        const firstB = bt.find((x) => x != null);
        let keep = n;
        if (firstB != null) while (keep > 0 && (at[keep - 1] == null || at[keep - 1] >= firstB)) keep--;
        const lastA = keep ? at[keep - 1] : null;
        const bucket = (b.bucket_s || a.bucket_s || 300) * 1000;
        const gap = lastA != null && firstB != null && firstB - lastA > 3 * bucket + 600e3;
        const out = Object.assign({}, b);
        Object.keys(b).forEach((k) => {
            if (!Array.isArray(a[k]) || !Array.isArray(b[k])) return;
            if (k === 't' || isSeries(k)) {
                const head = a[k].slice(0, keep);
                if (gap) head.push(k === 't' ? lastA + bucket : null);   // přerušení čáry na švu oken
                out[k] = head.concat(b[k]);
            } else out[k] = a[k].concat(b[k]);                           // seznamy událostí (boots)
        });
        return out;
    }
    async function histChunks(key, r, node, single) {
        const s = scrubState(key), span = r.to - r.from;
        const anchor = r.live ? Math.floor(r.from / 3600) * 3600 : r.from;
        const wins = [];
        for (let i = s.chunks; i >= 1; i--) wins.push({ from: anchor - i * span, to: anchor - (i - 1) * span });
        wins.push({ from: anchor, to: r.to });
        const parts = await Promise.all(wins.map((w) => getHistory(w, node)));
        const d = parts.reduce(mergeHist);
        d.loadedFrom = (anchor - s.chunks * span) * 1000;
        d.first_ts = parts[parts.length - 1].first_ts;
        if (single) setScrubStart(key, [d]);
        return d;
    }
    function setScrubStart(key, list) {
        const s = scrubState(key);
        const firsts = list.map((d) => d.first_ts).filter((x) => x != null);
        s.first = firsts.length ? Math.min(...firsts) : null;
        s.exhausted = s.first == null || list.every((d) => d.loadedFrom <= s.first);
        const lasts = list.map((d) => { const t = d.t || []; let i = t.length - 1; while (i >= 0 && t[i] == null) i--; return i < 0 ? null : t[i]; })
            .filter((x) => x != null);
        s.last = lasts.length ? Math.max(...lasts) : null;
    }
    function nearestIdx(t, ms, maxGap, values) {
        let lo = 0, hi = (t || []).length - 1, best = -1, bestD = Infinity;
        if (hi < 0) return -1;
        while (lo <= hi) {
            const mid = (lo + hi) >> 1;
            if (t[mid] < ms) lo = mid + 1; else hi = mid - 1;
        }
        [lo - 1, lo, lo - 2, lo + 1].forEach((i) => {
            if (i < 0 || i >= t.length || (values && values[i] == null)) return;
            const dd = Math.abs(t[i] - ms);
            if (dd < bestD) { bestD = dd; best = i; }
        });
        return bestD <= maxGap ? best : -1;
    }
    const scrubPlugin = {
        id: 'scrub',
        afterDatasetsDraw(chart, args, opts) {
            const { top, bottom, left, right } = chart.chartArea;
            const cx = Math.round((left + right) / 2) + 0.5;
            const ms = chart.scales.x.getValueForPixel(cx);
            const ctx = chart.ctx;
            ctx.save();
            ctx.strokeStyle = T.ink;
            ctx.globalAlpha = 0.55;
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(cx, top); ctx.lineTo(cx, bottom); ctx.stroke();
            ctx.globalAlpha = 1;
            chart.data.datasets.forEach((ds, di) => {
                if (!chart.isDatasetVisible(di)) return;
                const pts = ds.data || [];
                const i = nearestIdx(pts.map((p) => p.x), ms, opts.gapMs || 900e3, pts.map((p) => p.y));
                if (i < 0) return;
                const y = chart.scales[ds.yAxisID || 'y'].getPixelForValue(pts[i].y);
                ctx.beginPath(); ctx.arc(cx, y, 4, 0, Math.PI * 2);
                ctx.fillStyle = ds.borderColor; ctx.strokeStyle = T.surface; ctx.lineWidth = 2;
                ctx.fill(); ctx.stroke();
            });
            ctx.restore();
            const box = chart.canvas.parentNode;
            let bub = box.previousElementSibling;
            if (!bub || !bub.classList.contains('scrub-bubble')) {
                bub = document.createElement('div');
                bub.className = 'scrub-bubble';
                box.parentNode.insertBefore(bub, box);
            }
            const lines = opts.text ? opts.text(ms) : [];
            const key = opts.key;
            const away = key && SCRUB_RANGE[key] && !scrubState(key).home;
            bub.innerHTML = `<b>${esc(fmtStamp(ms))}</b>` +
                (lines.length ? lines.map((l) => `<span>${esc(l)}</span>`).join('') : '<span class="muted">bez dat</span>') +
                (away ? '<button type="button" class="scrub-now">Aktuální →</button>' : '');
            const now = bub.querySelector('.scrub-now');
            if (now) now.addEventListener('click', () => scrubTo(key, homeOffset(key, SCRUB_RANGE[key])));
        }
    };
    function scrubOpts(key, r, d, text) {
        SCRUB_RANGE[key] = r;
        return { key, gapMs: Math.max(d.bucket_s || 300, 300) * 1500, text };
    }
    function scrubLimits(key) {
        const r = SCRUB_RANGE[key], s = scrubState(key), span = r.to - r.from;
        const min = r.live ? -span / 2 : 0;
        let max = s.chunks * span;
        if (s.first != null) max = Math.min(max, Math.max(0, r.from + span / 2 - s.first / 1000));
        else if (s.exhausted) max = 0;
        return { min, max: Math.max(min, max), span };
    }
    function scrubTo(key, offset) {
        const ch = charts[key], r = SCRUB_RANGE[key];
        if (!ch || !r) return;
        const s = scrubState(key), lim = scrubLimits(key);
        s.offset = Math.max(lim.min, Math.min(lim.max, offset));
        s.home = Math.abs(s.offset - homeOffset(key, r)) < 1;
        const v = viewRange(key, r);
        ch.options.scales.x.min = v.from * 1000;
        ch.options.scales.x.max = v.to * 1000;
        ch.update('none');
        if (!s.exhausted && !s.loading && s.offset > s.chunks * lim.span - lim.span / 2) {
            s.loading = true;
            s.chunks += 1;
            Promise.resolve(LOADERS[key]()).catch((e) => console.error(key, e)).finally(() => { s.loading = false; });
        }
    }
    function scrubbable(key, canvasId) {
        const canvas = $(canvasId), box = canvas.parentNode;
        box.classList.add('scrub');
        canvas.tabIndex = 0;
        canvas.setAttribute('aria-label', 'Graf – šipkami vlevo a vpravo posunete čas, Home = aktuální data');
        let drag = null;
        box.addEventListener('pointerdown', (e) => {
            if (e.button !== 0 || e.target.closest('button') || !charts[key] || !SCRUB_RANGE[key]) return;
            const a = charts[key].chartArea;
            drag = { id: e.pointerId, x: e.clientX, start: scrubState(key).offset, w: Math.max(1, a.right - a.left) };
            try { box.setPointerCapture(e.pointerId); } catch (err) { /* syntetická událost */ }
            box.classList.add('dragging');
        });
        box.addEventListener('pointermove', (e) => {
            if (!drag || e.pointerId !== drag.id) return;
            const span = SCRUB_RANGE[key].to - SCRUB_RANGE[key].from;
            scrubTo(key, drag.start + (e.clientX - drag.x) / drag.w * span);
        });
        const up = (e) => {
            if (!drag || e.pointerId !== drag.id) return;
            drag = null;
            box.classList.remove('dragging');
        };
        box.addEventListener('pointerup', up);
        box.addEventListener('pointercancel', up);
        box.addEventListener('dblclick', () => scrubTo(key, homeOffset(key, SCRUB_RANGE[key])));
        canvas.addEventListener('keydown', (e) => {
            const r = SCRUB_RANGE[key];
            if (!r) return;
            const step = (r.to - r.from) / 10, o = scrubState(key).offset;
            if (e.key === 'ArrowLeft') scrubTo(key, o + step);
            else if (e.key === 'ArrowRight') scrubTo(key, o - step);
            else if (e.key === 'Home') scrubTo(key, homeOffset(key, r));
            else return;
            e.preventDefault();
        });
    }

    // dílky časové osy na celé hodiny / půlnoc / začátek měsíce (místní čas)
    const MONTHS = ['led', 'úno', 'bře', 'dub', 'kvě', 'čvn', 'čvc', 'srp', 'zář', 'říj', 'lis', 'pro'];
    function timeTicks(fromMs, toMs, maxN) {
        const H = 3600e3, D = 86400e3, span = toMs - fromMs, out = [];
        const hours = [1, 2, 3, 6, 12].find((h) => span / (h * H) <= maxN);
        const days = [1, 2, 3, 7, 14].find((n) => span / (n * D) <= maxN);
        if (hours) {
            const d = new Date(fromMs);
            d.setMinutes(0, 0, 0);
            while (d.getTime() < fromMs || d.getHours() % hours) d.setHours(d.getHours() + 1);
            for (; d.getTime() <= toMs; d.setHours(d.getHours() + hours)) out.push({ value: d.getTime(), unit: 'h' });
        } else if (days) {
            const d = new Date(fromMs);
            d.setHours(0, 0, 0, 0);
            if (d.getTime() < fromMs) d.setDate(d.getDate() + 1);
            for (; d.getTime() <= toMs; d.setDate(d.getDate() + days)) out.push({ value: d.getTime(), unit: 'd' });
        } else {
            const months = [1, 2, 3, 6, 12].find((m) => span / (m * 30.4 * D) <= maxN) || 12;
            const d = new Date(fromMs);
            d.setHours(0, 0, 0, 0);
            d.setDate(1);
            if (d.getTime() < fromMs) d.setMonth(d.getMonth() + 1);
            while (d.getMonth() % months) d.setMonth(d.getMonth() + 1);
            for (; d.getTime() <= toMs; d.setMonth(d.getMonth() + months)) out.push({ value: d.getTime(), unit: 'm' });
        }
        return out;
    }
    function tickLabel(v, unit) {
        const d = new Date(v);
        if (unit === 'm') return d.getMonth() === 0 ? `${MONTHS[0]} ${d.getFullYear()}` : MONTHS[d.getMonth()];
        if (unit === 'h' && (d.getHours() || d.getMinutes())) return `${d.getHours()}:${p2(d.getMinutes())}`;
        return `${d.getDate()}. ${d.getMonth() + 1}.`;
    }
    function timeScale(r) {
        let unitOf = new Map();
        return {
            type: 'linear', min: r.from * 1000, max: r.to * 1000,
            afterBuildTicks: (axis) => {
                const ticks = timeTicks(axis.min, axis.max, window.innerWidth < 640 ? 4 : 8);
                unitOf = new Map(ticks.map((t) => [t.value, t.unit]));
                axis.ticks = ticks.map((t) => ({ value: t.value }));
            },
            ticks: { autoSkip: true, maxRotation: 0, callback: (v) => tickLabel(v, unitOf.get(v)) }
        };
    }
    function series(t, values) {
        const out = new Array(t.length);
        for (let i = 0; i < t.length; i++) out[i] = { x: t[i], y: values[i] };
        return out;
    }
    function lastDot(values, color) {
        let last = -1;
        for (let i = (values || []).length - 1; i >= 0; i--) { if (values[i] != null) { last = i; break; } }
        return {
            pointRadius: (c) => (c.dataIndex === last ? 4 : 0),
            pointBorderWidth: (c) => (c.dataIndex === last ? 2 : 1),
            pointBorderColor: (c) => (c.dataIndex === last ? T.surface : color),
            pointBackgroundColor: color
        };
    }
    const line = (color, extra) => Object.assign({ borderColor: color, backgroundColor: color, pointBackgroundColor: color, pointBorderColor: color }, extra || {});
    function alpha(color, a) {
        const m = /^#([0-9a-f]{6})$/i.exec(color);
        if (!m) return color;
        const n = parseInt(m[1], 16);
        return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
    }
    function upsert(key, canvas, config) {
        if (charts[key]) charts[key].destroy();
        charts[key] = new Chart($(canvas).getContext('2d'), config);
    }
    // vložení baterie (plný kroužek) a restart čidla (prázdný) – svislá čárka v grafu baterie
    const bootMarks = {
        id: 'bootMarks',
        afterDatasetsDraw(chart, args, opts) {
            const ev = opts.boots || [];
            if (!ev.length) return;
            const x = chart.scales.x, { top, bottom, left, right } = chart.chartArea;
            const ctx = chart.ctx;
            ctx.save();
            ctx.lineWidth = 1;
            ev.forEach((e) => {
                const px = Math.round(x.getPixelForValue(e.t)) + 0.5;
                if (px < left || px > right) return;
                ctx.strokeStyle = alpha(T.amber, 0.35);
                ctx.beginPath(); ctx.moveTo(px, top + 8); ctx.lineTo(px, bottom); ctx.stroke();
                ctx.beginPath(); ctx.arc(px, top + 4, 3.5, 0, Math.PI * 2);
                ctx.strokeStyle = T.amber;
                ctx.fillStyle = e.wdt ? T.surface : T.amber;
                ctx.fill(); ctx.stroke();
            });
            ctx.restore();
        }
    };
    // popisek osy česky a bez binárního šumu
    const axisNum = (v, unit) => num(v, Math.abs(v - Math.round(v)) < 1e-6 ? 0 : 1, unit);
    const pctAxis = () => ({ min: 0, max: 100, ticks: { callback: (v) => axisNum(v, ' %'), maxTicksLimit: 6 } });
    const msAxis = () => ({ beginAtZero: true, ticks: { callback: (v) => axisNum(v, ' m/s'), maxTicksLimit: 6 } });
    const baseOpts = () => ({ responsive: true, maintainAspectRatio: false, parsing: false, interaction: { mode: 'nearest', axis: 'x', intersect: false } });
    const toMs = (v) => (v == null ? null : Math.round(v / 3.6 * 10) / 10);
    const gapOf = (d) => Math.max(d.bucket_s || 300, 300) * 1500;

    async function loadWindChart() {
        if (CURRENT == null || VIEW_ALL) return;
        const r = rangeOf('wind');
        const d = await histChunks('wind', r, CURRENT, true);
        const sp = d.speed.map(toMs), gu = d.gust.map(toMs), gap = gapOf(d);
        upsert('wind', 'windChart', {
            type: 'line',
            plugins: [scrubPlugin],
            data: { datasets: [
                line(T.s1, Object.assign({ label: d.bucket ? 'Průměrná rychlost' : 'Rychlost', data: series(d.t, sp), backgroundColor: alpha(T.s1, 0.05), fill: 'origin', borderWidth: 2.5, spanGaps: false }, lastDot(sp, T.s1))),
                line(T.s2, { label: d.bucket ? 'Nejsilnější náraz' : 'Náraz', data: series(d.t, gu), borderWidth: 1.5, spanGaps: false })
            ] },
            options: Object.assign(baseOpts(), {
                plugins: {
                    tooltip: { enabled: false },
                    scrub: scrubOpts('wind', r, d, (ms) => {
                        const out = [];
                        const i = nearestIdx(d.t, ms, gap, sp);
                        if (i >= 0) out.push(`${d.bucket ? 'Průměr' : 'Rychlost'} ${num(sp[i], 1, ' m/s')} · ${kmh(d.speed[i])}`);
                        const j = nearestIdx(d.t, ms, gap, gu);
                        if (j >= 0) out.push(`Náraz ${num(gu[j], 1, ' m/s')}`);
                        return out;
                    })
                },
                scales: { x: timeScale(viewRange('wind', r)), y: msAxis() }
            })
        });
    }

    async function loadBatteryChart() {
        if (CURRENT == null || VIEW_ALL) return;
        const r = rangeOf('battery');
        const d = await histChunks('battery', r, CURRENT, true);
        const gap = gapOf(d);
        upsert('battery', 'batteryChart', {
            type: 'line',
            plugins: [bootMarks, scrubPlugin],
            data: { datasets: [
                line(T.s3, { label: 'Baterie (%)', data: series(d.t, d.bat_pct || []), spanGaps: true, yAxisID: 'y' }),
                line(T.s4, { label: 'Signál (%)', data: series(d.t, (d.rssi || []).map(sigPct)), borderWidth: 1.5, yAxisID: 'y' })
            ] },
            options: Object.assign(baseOpts(), {
                plugins: {
                    bootMarks: { boots: d.boots },
                    tooltip: { enabled: false },
                    scrub: scrubOpts('battery', r, d, (ms) => {
                        const out = [];
                        const i = nearestIdx(d.t, ms, gap, d.bat_pct || []);
                        if (i >= 0) out.push(`Baterie ${num(d.bat_pct[i], 0, ' %')} · ${num(d.vbat[i], 3, ' V')}`);
                        const j = nearestIdx(d.t, ms, gap, d.rssi || []);
                        if (j >= 0) out.push('Signál ' + sigTxt(d.rssi[j]));
                        const half = Math.max(d.bucket_s || 300, 900) * 1000;
                        return out.concat((d.boots || []).filter((b) => Math.abs(b.t - ms) <= half)
                            .map((b) => (b.wdt ? 'Restart čidla' : 'Vložení baterie')));
                    })
                },
                scales: { x: timeScale(viewRange('battery', r)), y: pctAxis() }
            })
        });
    }

    async function loadAllChart() {
        if (!VIEW_ALL || !STATE) return;
        const r = rangeOf('all');
        const res = (await Promise.all(STATE.nodes.map((n) =>
            histChunks('all', r, n.id).then((d) => ({ n, d, sp: d.speed.map(toMs) })).catch(() => null)))).filter(Boolean);
        setScrubStart('all', res.map(({ d }) => d));
        const pal = [T.s1, T.s2, T.s3, T.s4, T.s5, T.s6];
        const d0 = res.length ? res[0].d : {};
        const gap = gapOf(d0);
        upsert('all', 'allChart', {
            type: 'line',
            plugins: [scrubPlugin],
            data: { datasets: res.map(({ n, d, sp }, i) => line(pal[i % pal.length], { label: n.name, data: series(d.t, sp), spanGaps: false, ...lastDot(sp, pal[i % pal.length]) })) },
            options: Object.assign(baseOpts(), {
                plugins: {
                    tooltip: { enabled: false },
                    scrub: scrubOpts('all', r, d0, (ms) => res.map(({ n, d, sp }) => {
                        const i = nearestIdx(d.t, ms, gap, sp);
                        return i < 0 ? null : `${n.name}: ${num(sp[i], 1, ' m/s')}`;
                    }).filter(Boolean))
                },
                scales: { x: timeScale(viewRange('all', r)), y: msAxis() }
            })
        });
    }

    const LOADERS = { wind: loadWindChart, battery: loadBatteryChart, all: loadAllChart };
    scrubbable('wind', 'windChart');
    scrubbable('battery', 'batteryChart');
    scrubbable('all', 'allChart');
    function reloadChart(key) {
        if (typeof Chart === 'undefined' || !STATE || !STATE.nodes.length) return;
        if (LOADERS[key]) LOADERS[key]().catch(() => { /* hub se restartuje – příště */ });
    }
    function reloadCharts(live) {
        if (live) dropLiveHistory();
        if (VIEW_ALL) reloadChart('all');
        else { reloadChart('wind'); reloadChart('battery'); }
    }

    // ------------------------------------------------------------ přehled
    function setStatus(el, n) {
        el.className = 'tank-status ' + n.status;
        el.innerHTML = `<span class="dot"></span><span>${n.unpair ? 'čeká na odpárování' : 'Čidlo ' + esc(STATUS[n.status])}${n.last_seen ? ' · ' + esc(fmtAgo(age(n))) : ''}</span>`
            + `<span class="tank-time">${n.last_seen ? esc(fmtDateTime(n.last_seen * 1000)) : ''}</span>`;
    }

    function renderDetail(n) {
        $('txtName').textContent = n.name || 'Anemometr ' + n.hex;
        setStatus($('txtStatus'), n);
        $('windCard').classList.toggle('is-stale', n.status !== 'online');
        const sp = n.speed_kmh;
        const pair = (msId, kmhId, v) => { $(msId).textContent = ms(v); $(kmhId).textContent = kmh(v); };
        pair('txtMs', 'txtKmh', sp);
        pair('txtGust', 'txtGustKmh', n.gust_kmh);
        pair('txtAvg', 'txtAvgKmh', n.avg10_kmh);
        pair('txtAvg1h', 'txtAvg1hKmh', n.avg1h_kmh);
        $('txtPeak').textContent = ms(n.gust_today_kmh);
        $('txtPeakTime').textContent = n.gust_today_ts ? `${kmh(n.gust_today_kmh)} v ${fmtTime(n.gust_today_ts)}` : '–';
        if (sp == null) { $('txtBft').textContent = '–'; $('txtBftSub').textContent = 'zatím bez dat'; }
        else {
            const [b, name] = bft(sp);
            $('txtBft').textContent = b + ' Bft';
            $('txtBftSub').textContent = n.stuck && !sp ? name + ' · kontakt sepnutý, rotor stojí' : name;
        }
        // jako retenčka: „dobrý · 57 % (−76 dBm)“, baterie v % podle typu a korekce, napětí na mV
        $('txtRssi').textContent = sigTxt(n.rssi);
        $('txtLoss').textContent = n.loss_pct == null ? '' : `ztráty 24 h: ${num(n.loss_pct, 1, ' %')}`;
        $('txtBat').textContent = n.battery_pct == null ? '–' : n.battery_pct + ' %' + (n.battery_low ? ' – vyměnit' : '');
        $('txtBat').classList.toggle('bad-text', !!n.battery_low);
        $('txtBatV').textContent = n.battery_v == null ? '' : num(n.battery_v, 3, ' V') + ' · ' + (n.battery_type === 'lifes2' ? 'lithiové' : 'alkalické');
    }

    function renderOverview() {
        const g = $('overviewGrid');
        g.textContent = '';
        STATE.nodes.forEach((n) => {
            const c = document.createElement('button');
            c.type = 'button';
            c.className = 'card ov-card wind-ov' + (n.status !== 'online' ? ' is-stale' : '');
            const st = STATUS[n.status] || n.status;
            c.innerHTML = `<div class="ov-body"><h4>${esc(n.name)}</h4>
                <div class="big">${n.speed_kmh == null ? '–' : num(n.speed_kmh / 3.6, 1)} <small>m/s</small></div>
                <div class="ov-litres">${kmh(n.speed_kmh)} · náraz ${ms(n.gust_kmh)}</div>
                <div class="meta"><span><span class="dot ${esc(n.status)}"></span><span class="${n.status === 'online' ? '' : n.status === 'stale' ? 'warn' : 'bad'}">${esc(st)}</span>${n.last_seen ? `<span>${esc(fmtAgo(age(n)))}</span>` : ''}</span>
                <span>${ico('antenna')} ${n.link_pct == null ? '–' : n.link_pct + ' %'}</span><span>${ico('battery')} <span class="${n.battery_low ? 'bad' : ''}">${n.battery_pct == null ? '–' : n.battery_pct + ' %'}</span></span></div></div>`;
            c.addEventListener('click', () => selectNode(n.id, false));
            g.appendChild(c);
        });
    }

    function renderNodeBar() {
        const bar = $('nodeBar');
        const onDash = $('dashboard').classList.contains('active');
        const nodes = STATE.nodes;
        bar.classList.toggle('hidden', nodes.length < 2 || !onDash);
        bar.textContent = '';
        if (nodes.length < 2) return;
        const all = document.createElement('button');
        all.type = 'button';
        all.className = 'node-pill all' + (VIEW_ALL ? ' active' : '');
        all.setAttribute('role', 'tab');
        const online = nodes.filter((n) => n.status === 'online').length;
        all.innerHTML = `Všechny anemometry <span class="pct">${online}/${nodes.length} online</span>`;
        all.addEventListener('click', () => { setViewAll(true); render(); reloadCharts(); });
        bar.appendChild(all);
        nodes.forEach((n) => {
            const b = document.createElement('button');
            b.type = 'button';
            const active = n.id === CURRENT && !VIEW_ALL;
            b.className = 'node-pill' + (active ? ' active' : '');
            b.setAttribute('role', 'tab');
            b.setAttribute('aria-selected', active ? 'true' : 'false');
            b.innerHTML = `<span class="dot ${esc(n.status)}"></span>${esc(n.name)} <span class="pct">${n.speed_kmh == null ? '–' : num(n.speed_kmh / 3.6, 1) + ' m/s'}</span>`;
            b.addEventListener('click', () => selectNode(n.id, false));
            bar.appendChild(b);
        });
    }

    function setViewAll(v) {
        VIEW_ALL = v;
        try { sessionStorage.setItem('viewAll', v ? '1' : ''); } catch (e) { /* ignore */ }
    }
    function selectNode(id, keepAll) {
        if (id !== CURRENT) { resetScrub('wind'); resetScrub('battery'); }
        CURRENT = id;
        try { localStorage.setItem('node', String(id)); } catch (e) { /* ignore */ }
        if (!keepAll) setViewAll(false);
        render();
        reloadCharts();
    }

    function render() {
        if (!STATE) return;
        const nodes = STATE.nodes;
        if (!nodes.some((n) => n.id === CURRENT)) CURRENT = nodes.length ? nodes[0].id : null;
        if (nodes.length < 2) setViewAll(false);
        const online = nodes.filter((n) => n.status === 'online').length;
        const pill = $('statusPill');
        pill.textContent = nodes.length ? `Čidla online: ${online}/${nodes.length}` : 'Žádné čidlo';
        pill.className = 'status-pill ' + (!nodes.length ? '' : online === nodes.length ? 'online' : online ? 'stale' : 'offline');
        $('emptyState').classList.toggle('hidden', nodes.length > 0);
        $('overview').classList.toggle('hidden', !VIEW_ALL || !nodes.length);
        $('nodeDash').classList.toggle('hidden', VIEW_ALL || !nodes.length);
        renderNodeBar();
        if (VIEW_ALL) renderOverview();
        else if (CURRENT != null) renderDetail(nodeById(CURRENT));
        renderSettings();
        $('menuVersion').textContent = 'Vítr ' + STATE.version;
    }

    // ----------------------------------------------------------- nastavení
    function applyMe() {
        ADMIN = ME.role === 'admin';
        const initial = (ME.username || '?').trim().charAt(0).toUpperCase() || '?';
        $('avatarInitial').textContent = $('menuInitial').textContent = initial;
        $('userBadge').textContent = ME.username;
        $('userRole').textContent = ADMIN ? 'Administrátor' : 'Uživatel (jen čtení)';
        $('accountBtn').title = `${ME.username} (${ADMIN ? 'administrátor' : 'uživatel'})`;
        $('aboutLine').textContent = `Vítr ${ME.version}`;
        document.querySelectorAll('.admin-only').forEach((el) => el.classList.toggle('hidden', !ADMIN));
        document.querySelectorAll('.user-only').forEach((el) => el.classList.toggle('hidden', ADMIN));
        if (ADMIN) { loadMqtt(); renderSettings(); buildSetNav(); if (!applyMe.upd) { applyMe.upd = true; bindUpdates(); } }
    }

    function editing(el) { return el.contains(document.activeElement) && document.activeElement.tagName === 'INPUT'; }

    function renderSettings() {
        if (!ADMIN || !STATE) return;
        const wrap = $('nodeCards');
        if (!editing(wrap)) {
            const ids = STATE.nodes.map((n) => n.id + ':' + n.unpair).join();
            if (wrap.dataset.ids !== ids) {
                wrap.dataset.ids = ids;
                wrap.textContent = '';
                STATE.nodes.forEach((n) => wrap.appendChild(nodeCard(n)));
                buildSetNav();
            }
            STATE.nodes.forEach((n) => fillLive(n));
        }
        const r = STATE.radio;
        const kv = [
            ['Verze', 'Vítr ' + STATE.version],
            ['Rádio (přes retenčku)', r.bridge_ok ? 'data chodí' : 'přes 30 min nic – běží retenčka s mostem?'],
            ['Přijaté rámce', r.frames], ['Data', r.data], ['Neověřené', r.auth_fail], ['Opakované / staré', r.replay],
            ['Žádosti o párování', r.pair_req], ['Home Assistant', STATE.mqtt]
        ];
        $('diagKv').innerHTML = kv.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
        $('mqStatus').textContent = 'Stav: ' + STATE.mqtt;
        $('sysInfo').innerHTML = `<dt>Verze</dt><dd>Vítr ${esc(STATE.version)}</dd>`;
    }

    function nodeCard(n) {
        const c = document.createElement('section');
        c.className = 'card node-card';
        c.id = 'node-' + n.id;
        c.dataset.nav = n.name;
        c.dataset.group = 'Anemometry';
        c.innerHTML = `
            <div class="card-title">
                <span class="card-ico" aria-hidden="true">${ico('wind')}</span>
                <div><h2>Anemometr – ${esc(n.name)}</h2><p class="card-desc">Název, kalibrace a aktuální hodnoty z čidla.</p></div>
            </div>
            <dl class="live-grid">
                <div><dt>Rychlost teď</dt><dd data-live="speed">–</dd></div>
                <div><dt>Otáčky / s</dt><dd data-live="hz">–</dd></div>
                <div><dt>Hodiny čidla</dt><dd data-live="clock">–</dd></div>
                <div><dt>Firmware</dt><dd data-live="fw">–</dd></div>
            </dl>
            <form class="mt-20">
                <div class="form-row">
                    <div class="form-group"><label>Název</label><input type="text" name="name" maxlength="40" class="unified-input" required></div>
                    <div class="form-group"><label>ID čidla</label><input type="text" class="unified-input" value="${esc(n.hex)}" disabled></div>
                </div>
                <div class="form-group">
                    <label>Kalibrace</label>
                    <div class="calib-row">
                        <span class="calib-eq">1 otáčka za sekundu =</span>
                        <input type="number" name="rev" class="unified-input" step="0.001" min="0.01" required aria-label="Rychlost při 1 otáčce za sekundu">
                        <select name="unit" class="unified-select" aria-label="Jednotka"><option value="ms">m/s</option><option value="kmh">km/h</option></select>
                    </div>
                    <p class="field-hint">Údaj z popisu anemometru. Běžné miskové anemometry: 1 otáčka/s = 2,4 km/h (0,667 m/s).</p>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>Pulzů na otáčku</label>
                        <input type="number" name="ppr" class="unified-input" min="1" max="16" step="1" required>
                        <p class="field-hint">Kolikrát jazýček sepne za jednu otáčku (většinou 1, některé anemometry 2).</p>
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>Baterie v čidle (3× AA)</label>
                        <select name="battery_type" class="unified-select">
                            <option value="lifes2">Lithiové Li-FeS2 (Energizer Ultimate Lithium)</option>
                            <option value="alkaline">Alkalické</option>
                        </select>
                        <p class="field-hint">Podle typu baterií se z napětí počítá zbývající kapacita. Lithiové drží napětí téměř až do vybití, alkalické klesá postupně.</p>
                    </div>
                </div>
                <div class="inline-actions">
                    <button type="submit" class="btn btn-primary">Uložit</button>
                </div>
            </form>
            <div class="sub adv">
                <h3>Kalibrace měření baterie</h3>
                <p class="field-hint">Změřte napětí baterií voltmetrem a zadejte ho. Hub z toho spočítá korekci (násobek), aby údaj souhlasil. Korekce 1,0000 = bez úpravy.</p>
                <div class="inline-actions">
                    <div class="input-unit">
                        <input type="text" inputmode="decimal" autocomplete="off" data-bat="actual" placeholder="Změřené napětí" aria-label="Změřené napětí (V)">
                        <span class="unit">V</span>
                    </div>
                    <button class="btn btn-outline" type="button" data-bat="cal">Opravit měření</button>
                </div>
                <p class="muted small mt-15">Korekce: <strong data-bat="trim">–</strong> · hlášené napětí <span data-live="vbat">–</span></p>
            </div>`;
        const batIn = c.querySelector('[data-bat="actual"]');
        c.querySelector('[data-bat="trim"]').textContent = num(n.battery_trim, 4);
        c.querySelector('[data-bat="cal"]').addEventListener('click', async () => {
            const r = await action(() => api('/api/node/battery', { id: n.id, actual_v: batIn.value }), 'Baterie zkalibrována.');
            if (r) {
                c.querySelector('[data-bat="trim"]').textContent = num(r.battery_trim, 4);
                batIn.value = '';
                await refresh();
            }
        });
        const f = c.querySelector('form');
        f.name.value = n.name;
        f.rev.value = Math.round(n.rev_ms * 1000) / 1000;
        f.unit.value = 'ms';
        f.ppr.value = n.ppr;
        f.battery_type.value = n.battery_type || 'alkaline';
        f.unit.addEventListener('change', () => {          // přepočet čísla při změně jednotky
            const v = Number(f.rev.value);
            if (v) f.rev.value = Math.round((f.unit.value === 'kmh' ? v * 3.6 : v / 3.6) * 1000) / 1000;
        });
        f.addEventListener('submit', async (e) => {
            e.preventDefault();
            try {
                await api('/api/node/update', { id: n.id, name: f.name.value, rev_value: f.rev.value, rev_unit: f.unit.value, ppr: f.ppr.value, battery_type: f.battery_type.value });
                toast('Uloženo');
                document.activeElement.blur();
                $('nodeCards').dataset.ids = '';
                await refresh();
            } catch (err) { toast(err.message, true); }
        });
        return c;
    }

    function fillLive(n) {
        const c = $('node-' + n.id);
        if (!c) return;
        const set = (k, v) => { c.querySelector(`[data-live="${k}"]`).textContent = v; };
        set('speed', ms(n.speed_kmh));
        set('hz', n.speed_kmh == null ? '–' : num(n.speed_kmh / 3.6 / n.rev_ms, 2));
        set('clock', (n.clock >= 1 ? '+' : '') + num((n.clock - 1) * 100, 1, ' %'));
        set('fw', n.fw ? num(n.fw / 10, 1) : '–');
        // hlášené napětí před korekcí (z něj se korekce počítá)
        set('vbat', n.battery_v == null ? '–' : num(n.battery_v / (n.battery_trim || 1), 3, ' V'));
    }

    // ------------------------------------------- čidla: seznam, párování, čidla v dosahu (jako retenčka)
    async function startPairing(id, name, msg) {
        const r = await action(() => api('/api/pairing/start', { node_id: id, name: name || '' }), msg || 'Čekám na čidlo…');
        if (r) await refreshNodes();
        return !!r;
    }

    let NODES = null;
    async function refreshNodes() {
        if (!ADMIN) return;
        try {
            NODES = await api('/api/nodes');
            renderNodes(NODES);
        } catch (e) { /* ignore */ }
    }

    // Přidání čidla – celé v dialogu: kroky, ID ze štítku + povinný název, nová čidla v dosahu.
    // Dialog zůstává otevřený a kroky / tabulka se obnovují s refreshNodes (každé 4 s).
    let pairFill = null;            // otevřený dialog: „Spárovat“ v tabulce doplní ID
    async function pairDialog(knownId) {
        const panel = $('pairPanel');
        panel.classList.remove('hidden');
        await formDialog({
            title: 'Přidat čidlo', wide: true, cancel: 'Zavřít', confirm: 'Čekat na čidlo',
            body: ['Vložte do anemometru baterii – objeví se v tabulce dole, klikněte u něj „Spárovat“ a doplňte název. ID ze štítku můžete zadat i předem a vložit baterii až po potvrzení.',
                $('pairSteps')],
            fields: [{ name: 'id', label: 'ID ze štítku', value: knownId || '', maxLength: 6, trim: true, placeholder: '4 znaky, např. 3F2A', hint: 'Čtyři znaky 0–9 / A–F vytištěné na štítku čidla.' },
                { name: 'name', label: 'Název', maxLength: 40, trim: true, hint: 'Povinný – pod tímto jménem se anemometr ukáže v přehledu.' }],
            after: [panel],
            validate: (v) => (!/^(0x)?[0-9a-f]{1,4}$/i.test(v.id) ? { field: 'id', message: 'ID čidla jsou 4 znaky 0–9 / A–F (např. 3F2A).' }
                : !v.name ? { field: 'name', message: 'Zadejte název čidla.' } : null),
            submit: async (v) => {
                try { await api('/api/pairing/start', { node_id: v.id, name: v.name }); } catch (e) { return e.message; }
                return null;
            },
            stay: () => { toast('Čekám na čidlo – vložte do něj baterii.'); refreshNodes(); },
            onOpen: ({ inputs, submit, hideErr }) => {
                pairFill = (id) => {
                    hideErr();
                    inputs.id.value = id;
                    if (inputs.name.value.trim()) submit(); else inputs.name.focus();
                };
            }
        });
        pairFill = null;
        panel.classList.add('hidden');
        panel.prepend($('pairSteps'));
        $('pairHint').parentNode.after(panel);
        refresh();
    }

    function renderNodes(d) {
        const r = d.radio || {};
        $('radioInfo').innerHTML = `
            <dt>Přijímač</dt><dd>${r.bridge_ok ? 'rádio retenčky – data chodí' : 'rádio retenčky – přes 30 min nic'}</dd>
            <dt>Přijaté rámce / data</dt><dd>${esc(r.frames)} / ${esc(r.data)}</dd>
            <dt>Neověřené podpisy / opakované pakety</dt><dd>${esc(r.auth_fail)} / ${esc(r.replay)}</dd>
            <dt>Žádosti o párování</dt><dd>${esc(r.pair_req)}</dd>`;

        const body = $('nodesBody');
        body.textContent = '';
        if (!d.nodes.length) body.innerHTML = '<tr><td colspan="6" class="muted">Zatím žádné čidlo</td></tr>';
        d.nodes.forEach((n) => {
            const tr = document.createElement('tr');
            const st = n.unpair ? 'odpárování – vyjměte a vložte baterii' : (STATUS[n.status] || n.status);
            const ago = n.last_seen && STATE ? STATE.now - n.last_seen : null;
            tr.innerHTML = `<td><strong>${nm(n.name)}</strong>${n.paired ? `<span title="Spárováno">${ico('lock', 'ico-sm')}</span>` : ' <span class="muted small">(nespárováno)</span>'}</td><td><code>${esc(n.hex)}</code></td>
                <td>${esc(st)}${!n.unpair && ago != null ? ' · ' + fmtAgo(ago) : ''}</td><td>${sigTxt(n.rssi)}</td>
                <td class="${n.battery_low ? 'bad-text' : ''}">${n.battery_pct == null ? '–' : n.battery_pct + ' %'}</td><td class="td-actions"></td>`;
            const btn = (label, cls, fn) => {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-sm ' + cls; b.textContent = label;
                b.addEventListener('click', fn);
                tr.lastElementChild.appendChild(b);
            };
            const done = () => { refreshNodes(); refresh(); };
            if (n.unpair) {
                btn('Zrušit', 'btn-outline', () => action(() => api('/api/nodes/unpair/cancel', { node_id: n.hex }), 'Odpárování zrušeno.').then(done));
                btn('Odebrat bez čidla', 'btn-danger', async () => {
                    if (!await confirmDialog({
                        title: `Odebrat čidlo ${n.hex} bez potvrzení?`,
                        body: ['Jen pro ztracené nebo zničené čidlo.',
                            'Hub si jeho klíč uschová – když se čidlo najde, stačí u něj v „Čidla v dosahu“ kliknout „Spárovat znovu“ a vyjmout a vložit baterii.'],
                        confirm: 'Odebrat čidlo', danger: true
                    })) return;
                    action(() => api('/api/nodes/remove', { node_id: n.hex, force: true }), 'Čidlo odebráno.').then(done);
                });
            } else {
                btn('Nastavit', 'btn-outline', () => { const c = $('node-' + n.id); if (c) c.scrollIntoView({ behavior: 'smooth', block: 'start' }); });
                btn('Odpárovat', 'btn-danger', async () => {
                    if (!await confirmDialog({
                        title: `Odpárovat čidlo ${n.name} (${n.hex})?`,
                        body: ['Dokončí se po vyjmutí a vložení baterie do čidla. Naměřená data zůstanou.'],
                        confirm: 'Odpárovat', danger: true
                    })) return;
                    action(() => api('/api/nodes/remove', { node_id: n.hex }), (x) => x.removed ? 'Čidlo odebráno.' : 'Vyjměte a vložte do čidla baterii.').then(done);
                });
            }
            body.appendChild(tr);
        });

        renderPairSteps(d);
        const pb = $('pairBody');
        pb.textContent = '';
        const pr = (d.pairing && d.pairing.requests) || [];
        const targets = (d.pairing && d.pairing.targets) || [];
        if (!pr.length && !targets.length) pb.innerHTML = '<tr><td colspan="5" class="muted">Žádné nové čidlo</td></tr>';
        pr.forEach((q) => {
            const tr = document.createElement('tr');
            // paired, but not to this hub: another hub's sensor (cannot be taken over), or one
            // removed here whose key the hub still keeps (archive) – that one can be re-paired
            const foreign = q.node_paired && !q.known && !q.archived;
            const state = q.answered ? 'čekám na data'
                : q.target ? 'páruji…'
                : foreign ? 'patří jinému hubu'
                : (q.node_paired && !q.known) ? 'odebrané čidlo (klíč v archivu)'
                : q.known ? (q.node_paired ? 'výměna baterie' : 'čidlo ztratilo klíč')
                : 'nové';
            const multiKey = q.pub_count > 1 ? ` <span class="bad-text" title="Zaznamenáno více různých veřejných klíčů pro toto ID">· pozor: ${esc(q.pub_count)} klíče!</span>` : '';
            tr.innerHTML = `<td><code>${esc(q.id)}</code></td><td>${sigTxt(q.rssi)}</td><td>${fmtAgo(q.age_s)}</td><td>${esc(state)}${multiKey}</td><td class="td-actions"></td>`;
            if (!q.target && !foreign) {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-primary btn-sm';
                b.textContent = q.known ? 'Znovu spárovat' : (q.node_paired ? 'Spárovat znovu' : 'Spárovat');
                // the table lives in the Add-sensor dialog: no nested confirmation, the button says it
                b.title = !q.known && q.node_paired ? 'Hub ho uvolní uschovaným klíčem a dá mu nový.'
                    : q.known ? 'Starý klíč se zruší a vygeneruje se nový.' : 'Doplní ID; zadejte název a potvrďte.';
                b.addEventListener('click', () => {
                    if (!q.known && !q.node_paired) {        // nové čidlo: název + ověření ID podle štítku
                        if (pairFill) pairFill(q.id); else pairDialog(q.id);
                        return;
                    }
                    startPairing(q.id, '');
                });
                tr.lastElementChild.appendChild(b);
            }
            pb.appendChild(tr);
        });
        ((d.pairing && d.pairing.pending) || []).filter((x) => !pr.some((q) => q.id === x.id)).forEach((x) => {
            // PAIR_ACK sent, the first data did not arrive yet: accepted for up to 24 h
            const tr = document.createElement('tr');
            const multiKey = x.pub_count > 1 ? ` <span class="bad-text">· pozor: ${esc(x.pub_count)} klíče!</span>` : '';
            tr.innerHTML = `<td><code>${esc(x.id)}</code> ${nm(x.name || '')}</td><td>${sigTxt(x.rssi)}</td><td>–</td>
                <td>čekám na první data (${Math.max(1, Math.ceil(x.expires_in / 3600))} h)${multiKey}</td><td class="td-actions"></td>`;
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'btn btn-outline btn-sm'; b.textContent = 'Zrušit';
            b.addEventListener('click', () => action(() => api('/api/pairing/cancel', { node_id: x.id })).then(refreshNodes));
            tr.lastElementChild.appendChild(b);
            pb.appendChild(tr);
        });
        targets.filter((t) => !pr.some((q) => q.id === t.id)).forEach((t) => {
            const tr = document.createElement('tr');
            const multiKey = t.pub_count > 1 ? ` <span class="bad-text">· pozor: ${esc(t.pub_count)} klíče!</span>` : '';
            tr.innerHTML = `<td><code>${esc(t.id)}</code> ${nm(t.name || '')}</td><td>${sigTxt(t.rssi)}</td><td>–</td>
                <td>čekám (${Math.ceil(t.expires_in / 60)} min)${multiKey}</td><td class="td-actions"></td>`;
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'btn btn-outline btn-sm'; b.textContent = 'Zrušit';
            b.addEventListener('click', () => action(() => api('/api/pairing/cancel', { node_id: t.id })).then(refreshNodes));
            tr.lastElementChild.appendChild(b);
            pb.appendChild(tr);
        });

        const hb = $('heardBody');
        hb.textContent = '';
        if (!d.heard.length) hb.innerHTML = '<tr><td colspan="6" class="muted">Zatím nic nepřijato.</td></tr>';
        const targetIds = new Set(targets.map((t) => t.id));
        d.heard.forEach((h) => {
            const tr = document.createElement('tr');
            const foreign = h.kind !== 'pair' && !h.added;
            tr.innerHTML = `<td><code>${esc(h.id)}</code>${h.added ? ico('check', 'ico-sm') : ''}</td><td>${sigTxt(h.rssi)}</td>
                <td>${fmtAgo(h.age_s)}</td><td>${esc(h.count)}</td><td class="muted small">${h.kind === 'pair' ? 'nabízí párování' : (h.added ? 'data' : 'cizí čidlo')}</td><td class="td-actions"></td>`;
            const cell = tr.lastElementChild;
            if (foreign && targetIds.has(h.id)) {
                cell.innerHTML = '<span class="muted small">Vyjměte a vložte do čidla baterii.</span>';
            } else if (foreign && h.archived) {
                // removed here (or its first data were lost): the hub still holds its key
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-primary btn-sm'; b.textContent = 'Spárovat znovu';
                b.title = h.key_match ? 'Čidlo vysílá s klíčem uschovaným v hubu' : 'Hub má uschovaný starší klíč tohoto ID';
                b.addEventListener('click', async () => {
                    if (!await confirmDialog({
                        title: `Spárovat znovu čidlo ${h.id}${h.archived_name ? ' (' + h.archived_name + ')' : ''}?`,
                        body: ['Pak do čidla vyjměte a vložte baterii – hub ho uvolní uschovaným klíčem a spáruje s novým.'],
                        confirm: 'Spárovat znovu'
                    })) return;
                    startPairing(h.id, '', 'Vyjměte a vložte do čidla baterii.');
                });
                cell.appendChild(b);
            } else if (foreign) {
                cell.innerHTML = '<span class="muted small">Čidlo patří jinému hubu nebo bylo spárované před přeinstalací – smažte párování v čidle (4-smazat-parovani.cmd).</span>';
            }
            hb.appendChild(tr);
        });
    }

    function renderPairSteps(d) {
        const pr = (d.pairing && d.pairing.requests) || [];
        const targets = (d.pairing && d.pairing.targets) || [];
        const offering = pr.some((q) => !q.node_paired && !q.target);
        const answered = targets.some((t) => t.answered) || pr.some((q) => q.answered);
        const waiting = targets.length > 0;
        let step = 1;
        if (offering || waiting) step = 2;
        if (answered) step = 3;
        if (!offering && !waiting && pairDone > Date.now()) step = 4;
        if (answered) pairDone = Date.now() + 60000;
        // čidlo, na které se čekalo, už je v seznamu (spárovalo se mezi dvěma obnoveními)
        if (pairWait.some((id) => !targets.some((t) => t.id === id) && d.nodes.some((n) => n.hex === id))) {
            pairDone = Date.now() + 60000;
            if (!offering && !waiting) step = 4;
        }
        pairWait = targets.map((t) => t.id);
        const hint = $('pairHint');
        hint.dataset.idle = hint.dataset.idle || hint.textContent;
        hint.textContent = offering ? 'Nové čidlo nabízí párování – otevřete „Přidat čidlo“.'
            : waiting ? `Čekám na čidlo ${targets.map((t) => t.id).join(', ')}.` : hint.dataset.idle;
        hint.classList.toggle('muted', !offering);
        document.querySelectorAll('#pairSteps li').forEach((li) => {
            const n = +li.dataset.step;
            li.classList.toggle('done', n < step || (step === 4 && n === 4));
            li.classList.toggle('active', n === step && step !== 4);
        });
    }
    let pairDone = 0, pairWait = [];

    function buildSetNav() {
        const nav = $('setNav');
        nav.textContent = '';
        let group = null;
        document.querySelectorAll('#setStack [data-nav]').forEach((c) => {
            if (c.dataset.group !== group) {
                group = c.dataset.group;
                const g = document.createElement('div');
                g.className = 'set-nav-group';
                g.textContent = group;
                nav.appendChild(g);
            }
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'set-chip';
            b.textContent = c.dataset.nav;
            b.addEventListener('click', () => c.scrollIntoView({ behavior: 'smooth', block: 'start' }));
            nav.appendChild(b);
        });
    }

    async function loadMqtt() {
        try {
            const m = await api('/api/mqtt');
            $('mqEnabled').checked = m.enabled;
            $('mqHost').value = m.host;
            $('mqPort').value = m.port;
            $('mqUser').value = m.user;
            $('mqPass').value = '';
            $('mqPass').placeholder = m.password_set ? 'beze změny' : 'bez hesla';
        } catch (e) { /* ignore */ }
    }

    // --------------------------------------------------------------- obsluha
    const ADMIN_TABS = ['nastaveni', 'logovani', 'uzivatele'];
    function showTab(tab) {
        if (ADMIN_TABS.includes(tab) && !ADMIN) tab = 'dashboard';
        document.querySelectorAll('.nav-tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
        document.querySelectorAll('.tab-content').forEach((c) => c.classList.toggle('active', c.id === tab));
        try { sessionStorage.setItem('tab', tab); } catch (e) { /* ignore */ }
        if (STATE) render();
        if (tab === 'dashboard') reloadCharts();
        if (tab === 'uzivatele') loadUsers();
        if (tab === 'logovani') loadAuditLog(1);
        if (tab === 'nastaveni') refreshNodes();
    }
    document.querySelectorAll('[data-tab]').forEach((b) => b.addEventListener('click', () => {
        $('accountMenu').classList.add('hidden');
        showTab(b.dataset.tab);
    }));

    // ---------------------------------------------------------------- uživatelé
    const pad = (n) => String(n).padStart(2, '0');
    let PW_FOR = null;

    async function loadUsers() {
        const body = $('usersBody');
        let users;
        try { users = await api('/api/users'); } catch (e) { toast(e.message, true); return; }
        body.textContent = '';
        users.forEach((u) => {
            const self = u.username.toLowerCase() === ME.username.toLowerCase();
            const tr = document.createElement('tr');
            tr.innerHTML = `<td><strong>${esc(u.username)}</strong>${self ? ' (vy)' : ''}</td>
                <td><select class="table-select" aria-label="Role"${self ? ' disabled' : ''}>
                    <option value="user">Uživatel</option><option value="admin">Administrátor</option></select></td>
                <td>${u.last_login ? fmtDateTime(u.last_login * 1000) : '–'}</td><td class="td-actions"></td>`;
            const sel = tr.querySelector('select');
            sel.value = u.role;
            sel.addEventListener('change', async () => {
                const to = sel.value;
                const msg = to === 'admin' ? 'Administrátor může měnit nastavení, párovat čidla a spravovat uživatele.'
                    : 'Uživatel uvidí data, ale nastavení měnit nemůže.';
                if (!await confirmDialog({ title: `Změnit roli uživatele ${u.username}?`,
                    body: [`Nová role: ${to === 'admin' ? 'Administrátor' : 'Uživatel (jen čtení)'}.`, msg], confirm: 'Změnit roli' })) {
                    sel.value = u.role; return;
                }
                try { await api('/api/users/role', { username: u.username, role: to }); toast('Role změněna.'); } catch (e) { toast(e.message, true); }
                loadUsers();
            });
            if (!self) {
                const cell = tr.querySelector('.td-actions');
                const pw = document.createElement('button');
                pw.type = 'button'; pw.className = 'btn btn-outline btn-sm'; pw.textContent = 'Nové heslo';
                pw.addEventListener('click', () => {
                    PW_FOR = u.username;
                    $('userPwTitle').textContent = `Nové heslo pro ${u.username}`;
                    $('userPwForm').classList.remove('hidden');
                    $('userPw').focus();
                });
                const del = document.createElement('button');
                del.type = 'button'; del.className = 'btn btn-danger btn-sm'; del.textContent = 'Smazat';
                del.addEventListener('click', async () => {
                    if (!await confirmDialog({ title: `Smazat uživatele ${u.username}?`,
                        body: ['Bude odhlášen ze všech zařízení. Nejde to vzít zpět.'], confirm: 'Smazat', danger: true })) return;
                    try { await api('/api/users/delete', { username: u.username }); toast('Uživatel smazán.'); } catch (e) { toast(e.message, true); }
                    loadUsers();
                });
                cell.appendChild(pw); cell.appendChild(del);
            }
            body.appendChild(tr);
        });
    }

    $('userPwCancel').addEventListener('click', () => { $('userPwForm').reset(); $('userPwForm').classList.add('hidden'); });
    $('userPwForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        if ($('userPw').value !== $('userPw2').value) { toast('Hesla se neshodují!', true); return; }
        try {
            await api('/api/users/password', { username: PW_FOR, password: $('userPw').value });
            e.target.reset(); e.target.classList.add('hidden');
            toast('Heslo nastaveno, uživatel byl odhlášen.');
        } catch (err) { toast(err.message, true); }
    });
    $('addUserForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const p = $('newPassword').value;
        if (p !== $('newPasswordConfirm').value) { toast('Hesla se neshodují!', true); return; }
        try {
            await api('/api/users/add', { username: $('newUsername').value.trim(), password: p, role: $('newRole').value });
            e.target.reset();
            toast('Uživatel přidán.');
            loadUsers();
        } catch (err) { toast(err.message, true); }
    });

    // ------------------------------------------------------------ updates
    // Dialog nad stránkou: kontrola → výběr → instalace s průběhem. Stav píše root updater
    // do update-status.json, web ho čte přes /api/update/status (viz server/vitr/updates.py).
    const UPD_STEPS = [['downloading', 'Stahuji'], ['verifying', 'Ověřuji podpis'], ['installing', 'Instaluji'], ['restarting', 'Restartuji']];
    const UPD = {
        open: false, mode: null, since: 0, target: null, timer: null, failSince: null,
        last: null, opener: null, pendingSince: null
    };

    function fmtIsoDate(iso) {
        const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
        return m ? `${+m[3]}. ${+m[2]}. ${m[1]}` : '';
    }

    // Bezpečné vykreslení podmnožiny Markdownu (nadpisy, odrážky, odstavce, **tučně**, `kód`,
    // [odkaz](https://…)) – jen createElement + textContent, nikdy innerHTML z cizího textu.
    function mdInline(parent, text) {
        const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\))/g;
        let last = 0, m;
        while ((m = re.exec(text))) {
            if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
            const t = m[0];
            if (t.startsWith('**')) {
                const b = document.createElement('strong'); b.textContent = t.slice(2, -2); parent.appendChild(b);
            } else if (t.startsWith('`')) {
                const c = document.createElement('code'); c.textContent = t.slice(1, -1); parent.appendChild(c);
            } else {
                const mm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(t);
                if (mm && /^https:\/\//i.test(mm[2])) {
                    const a = document.createElement('a');
                    a.textContent = mm[1]; a.href = mm[2]; a.target = '_blank'; a.rel = 'noopener noreferrer';
                    parent.appendChild(a);
                } else parent.appendChild(document.createTextNode(mm ? mm[1] : t));
            }
            last = m.index + t.length;
        }
        if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
    }
    function renderMarkdown(md) {
        const root = document.createElement('div');
        root.className = 'md';
        let list = null, para = null;
        String(md || '').split(/\r?\n/).forEach((raw) => {
            const line = raw.trimEnd();
            const h = /^(#{1,6})\s+(.*)$/.exec(line);
            const li = /^\s*[-*+]\s+(.*)$/.exec(line);
            if (!line.trim()) { list = null; para = null; return; }
            if (h) {
                list = null; para = null;
                const el = document.createElement('h4');
                mdInline(el, h[2]);
                root.appendChild(el);
            } else if (li) {
                para = null;
                if (!list) { list = document.createElement('ul'); root.appendChild(list); }
                const el = document.createElement('li');
                mdInline(el, li[1]);
                list.appendChild(el);
            } else if (list && /^\s{2,}/.test(raw)) {
                list.lastChild.appendChild(document.createTextNode(' '));
                mdInline(list.lastChild, line.trim());
            } else {
                list = null;
                if (!para) { para = document.createElement('p'); root.appendChild(para); } else para.appendChild(document.createTextNode(' '));
                mdInline(para, line.trim());
            }
        });
        return root;
    }

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text != null) e.textContent = text;
        return e;
    }
    // Dialog se při čekání každou chvíli překreslí a nové kolečko by se roztočilo od nuly
    // (trhaný skok). Všechna kolečka mají animaci ukotvenou na začátek časové osy dokumentu,
    // takže nové pokračuje přesně v úhlu starého.
    function spinnerEl() {
        const sp = el('span', 'spinner');
        queueMicrotask(() => {                       // to už je kolečko vložené do stránky
            if (sp.getAnimations) sp.getAnimations().forEach((a) => { a.startTime = 0; });
        });
        return sp;
    }

    function updButton(label, cls, fn) {
        const b = el('button', 'btn ' + cls, label);
        b.type = 'button';
        b.addEventListener('click', fn);
        return b;
    }

    function updView(kind, sum) {
        const body = $('updBody'), foot = $('updFoot');
        body.textContent = '';
        foot.textContent = '';
        const st = (sum && sum.status) || {};
        const running = (sum && sum.running) || ME.version;
        const close = () => updButton('Zavřít', 'btn-outline', closeUpdateDialog);
        $('updModal').classList.toggle('busy', kind === 'install' || kind === 'restart');
        if (kind === 'checking') {
            const row = el('div', 'upd-wait');
            row.append(spinnerEl(), el('span', '', 'Hledám aktualizace…'));
            body.appendChild(row);
            foot.appendChild(close());
        } else if (kind === 'up_to_date') {
            body.appendChild(el('p', 'upd-big', `Máte nejnovější verzi ${running}.`));
            if (st.updated_at) body.appendChild(el('p', 'muted small', 'Zkontrolováno ' + fmtDateTime(st.updated_at * 1000)));
            foot.appendChild(close());
        } else if (kind === 'available') {
            const head = el('div', 'upd-avail');
            head.appendChild(el('p', 'upd-big', `K dispozici je verze ${st.latest}`));
            const meta = [`nainstalovaná ${running}`];
            if (st.published_at) meta.push('vydáno ' + fmtIsoDate(st.published_at));
            head.appendChild(el('p', 'muted small', meta.join(' · ')));
            body.appendChild(head);
            if (st.failed_before) body.appendChild(el('div', 'alert-warning', 'Tuto verzi se dříve nepodařilo spustit (vitr se vrátil k předchozí). Instalaci lze zkusit znovu.'));
            if (st.message && /už není nejnovější/.test(st.message)) body.appendChild(el('div', 'alert-info', st.message));
            const notes = Array.isArray(st.notes) ? st.notes : [];
            if (notes.length) {
                const box = el('div', 'upd-notes');
                notes.forEach((n) => {
                    if (!n || typeof n !== 'object') return;
                    box.appendChild(el('h3', '', `Verze ${n.version}` + (n.date ? ' · ' + fmtIsoDate(n.date) : '')));
                    box.appendChild(renderMarkdown(n.markdown));
                });
                body.appendChild(box);
            } else body.appendChild(el('p', 'muted small', 'Poznámky k vydání nejsou k dispozici.'));
            foot.append(updButton('Později', 'btn-outline', closeUpdateDialog),
                updButton(`Aktualizovat na ${st.latest}`, 'btn-primary', () => startInstall(st.latest)));
        } else if (kind === 'error') {
            body.appendChild(el('div', 'alert-danger', st.message || 'Kontrola aktualizací selhala.'));
            foot.append(close(), updButton('Zkusit znovu', 'btn-primary', () => openUpdateDialog('check')));
        } else if (kind === 'install' || kind === 'restart') {
            body.appendChild(el('p', 'upd-big', `Aktualizace na verzi ${UPD.target}`));
            const ol = el('ol', 'upd-steps');
            const idx = kind === 'restart' ? 3 : Math.max(0, UPD_STEPS.findIndex(([k]) => k === st.state));
            UPD_STEPS.forEach(([, label], i) => {
                const li = el('li', i < idx ? 'done' : i === idx ? 'active' : '');
                li.appendChild(el('span', 'step-dot'));
                li.appendChild(el('span', '', label));
                if (i === idx) li.setAttribute('aria-current', 'step');
                ol.appendChild(li);
            });
            body.appendChild(ol);
            const msg = el('div', 'upd-wait');
            msg.append(spinnerEl(), el('span', '', kind === 'restart' ? 'Čekám na restart…'
                : (UPD_STEPS.some(([k]) => k === st.state) && st.message) || 'Připravuji instalaci…'));
            body.appendChild(msg);
            body.appendChild(el('p', 'muted small', 'Nezavírejte prosím stránku, vitr se během aktualizace krátce restartuje.'));
        } else if (kind === 'done') {
            body.appendChild(el('p', 'upd-big ok', `Aktualizováno na ${running}`));
            body.appendChild(el('p', 'muted small', 'Načtěte stránku znovu, ať se použije nová verze webu.'));
            foot.appendChild(updButton('Načíst znovu', 'btn-primary', () => window.location.reload()));
        } else if (kind === 'failed') {
            body.appendChild(el('div', 'alert-danger', st.message || 'Aktualizace se nezdařila.'));
            body.appendChild(el('p', 'muted small', `Dál běží předchozí verze ${running}, data ani nastavení se nezměnila.`));
            foot.append(close(), updButton('Zkusit znovu', 'btn-primary', () => openUpdateDialog('check')));
        } else if (kind === 'timeout') {
            body.appendChild(el('div', 'alert-danger', UPD.timeoutMsg || 'Služba aktualizací neodpovídá.'));
            foot.append(close(), updButton('Zkusit znovu', 'btn-primary', () => openUpdateDialog('check')));
        }
        // po překreslení držet fokus v dialogu (po otevření na hlavní akci)
        if (UPD.open && (UPD.needFocus || !$('updModal').contains(document.activeElement))) {
            UPD.needFocus = false;
            focusFirst();
        }
    }

    function focusables() {
        return Array.from($('updModal').querySelectorAll('button, [href], input, select, [tabindex]:not([tabindex="-1"])'))
            .filter((e) => !e.disabled && e.offsetParent !== null);
    }
    function focusFirst() {
        const f = $('updFoot').querySelector('.btn-primary') || $('updFoot').querySelector('button') || $('updClose');
        (f || $('updModal').querySelector('.modal')).focus();
    }

    function viewFor(sum) {
        const st = (sum && sum.status) || {};
        const s = st.state;
        if (UPD.mode === 'install') {
            if (s === 'done' && sum.running === UPD.target) return 'done';
            if (s === 'rolled_back' || (s === 'error' && (st.updated_at || 0) >= UPD.since - 1)) return 'failed';
            if (s === 'restarting' || s === 'done') return 'restart';      // čeká se na novou verzi
            return 'install';
        }
        if (UPD.mode === 'check' && ((st.updated_at || 0) < UPD.since - 1 || s === 'checking' || sum.pending)) return 'checking';
        if (s === 'available') return 'available';
        if (s === 'up_to_date') return 'up_to_date';
        if (s === 'error' || s === 'rolled_back') return 'error';
        if (s === 'done') return sum.running === st.current ? 'up_to_date' : 'available';
        if (['downloading', 'verifying', 'installing', 'restarting'].includes(s) && sum.busy) {
            UPD.mode = 'install'; UPD.target = st.target || st.latest; UPD.since = st.started_at || 0;
            return 'install';
        }
        return s ? 'checking' : 'up_to_date';
    }

    async function updPoll() {
        clearTimeout(UPD.timer);
        if (!UPD.open) return;
        let sum = null;
        try {
            const r = await fetch('/api/update/status', { credentials: 'same-origin' });
            if (r.status === 401) { window.location.replace('/login'); return; }
            if (!r.ok) throw new Error('HTTP ' + r.status);
            sum = await r.json();
        } catch (e) {
            sum = null;
        }
        if (!UPD.open) return;
        if (!sum) {                                   // server se restartuje
            if (UPD.mode === 'install') {
                UPD.failSince = UPD.failSince || Date.now();
                if (Date.now() - UPD.failSince > 180000) {
                    UPD.timeoutMsg = 'Vitr se po restartu neozval do 3 minut. Zkuste stránku načíst znovu za chvíli.';
                    updView('timeout');
                    return;
                }
                updView('restart', UPD.last);
            }
            UPD.timer = setTimeout(updPoll, 1500);
            return;
        }
        UPD.failSince = null;
        UPD.last = sum;
        setUpdateBadge(sum);
        const kind = viewFor(sum);
        if (kind === 'checking' && sum.pending) {
            UPD.pendingSince = UPD.pendingSince || Date.now();
            if (Date.now() - UPD.pendingSince > 45000) {        // žádost nikdo nevyzvedl
                UPD.timeoutMsg = 'Služba aktualizací na žádost nereaguje (vitr-update.path). Zkuste to později nebo zkontrolujte journalctl -u vitr-update.';
                updView('timeout', sum);
                return;
            }
        } else UPD.pendingSince = null;
        updView(kind, sum);
        if (['checking', 'install', 'restart'].includes(kind)) UPD.timer = setTimeout(updPoll, 1500);
    }

    async function openUpdateDialog(mode) {
        const m = $('updModal');
        if (!UPD.open) {
            UPD.opener = document.activeElement;
            m.classList.remove('hidden');
            document.body.classList.add('modal-open');
            UPD.open = true;
            UPD.needFocus = true;
        }
        UPD.mode = mode;
        UPD.failSince = null;
        UPD.pendingSince = null;
        if (mode === 'check') {
            updView('checking');
            UPD.since = Math.floor(Date.now() / 1000);
            const r = await action(() => api('/api/update/check', {}));
            if (!r) {                                  // 409 = právě probíhá → jen ukázat stav
                UPD.mode = 'show';
            }
        }
        updPoll();
    }

    async function startInstall(ver) {
        UPD.target = ver;
        UPD.mode = 'install';
        UPD.since = Math.floor(Date.now() / 1000);
        updView('install', { status: { state: 'downloading', message: 'Odesílám žádost…' } });
        const r = await action(() => api('/api/update/install', { version: ver }));
        if (!r) { UPD.mode = 'show'; }
        updPoll();
    }

    function closeUpdateDialog() {
        if (!UPD.open || $('updModal').classList.contains('busy')) return;
        UPD.open = false;
        clearTimeout(UPD.timer);
        $('updModal').classList.add('hidden');
        document.body.classList.remove('modal-open');
        // řádek v kartě Systém se po zavření překreslí → fokus na stálé tlačítko
        const back = UPD.opener && UPD.opener.isConnected && !$('sysUpdate').contains(UPD.opener) ? UPD.opener : $('updateBtn');
        if (back && back.offsetParent !== null) back.focus(); else if (document.activeElement) document.activeElement.blur();
        refreshUpdateStatus();
    }

    function setUpdateBadge(sum) {
        const st = (sum && sum.status) || {};
        const avail = st.state === 'available' && st.latest && st.latest !== (sum.running || ME.version);
        $('menuUpdDot').classList.toggle('hidden', !avail);
        renderUpdateLine(sum);
        applyUpdatePrefs(sum && sum.prefs);
    }

    function renderUpdateLine(sum) {
        const box = $('sysUpdate');
        if (!box) return;
        box.textContent = '';
        const st = (sum && sum.status) || {};
        const when = st.updated_at || st.checked_at;
        if (st.state === 'available' && st.latest) {
            const row = el('div', 'upd-avail-line');
            row.appendChild(el('span', 'badge-dot'));
            row.appendChild(el('strong', '', `K dispozici verze ${st.latest}`));
            row.appendChild(updButton('Zobrazit', 'btn-primary btn-sm', () => openUpdateDialog('show')));
            box.appendChild(row);
        }
        const last = !when ? 'Zatím nekontrolováno.'
            : `Poslední kontrola ${fmtDateTime(when * 1000)}: ${st.message || st.state || ''}`;
        box.appendChild(el('p', 'muted small', last));
    }

    function applyUpdatePrefs(p) {
        if (!p) return;
        document.querySelectorAll('.upd-auto').forEach((c) => { c.checked = !!p.auto; });
        document.querySelectorAll('.upd-channel-sel').forEach((s) => { s.value = p.channel; });
    }

    async function refreshUpdateStatus() {
        if (ME.role !== 'admin') return;
        try { setUpdateBadge(await api('/api/update/status')); } catch (e) { /* ignore */ }
    }

    function bindUpdates() {
        $('updateBtn').addEventListener('click', () => openUpdateDialog('check'));
        $('updClose').addEventListener('click', closeUpdateDialog);
        $('updModal').addEventListener('mousedown', (e) => { if (e.target === $('updModal')) closeUpdateDialog(); });
        $('updModal').addEventListener('keydown', (e) => {
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeUpdateDialog(); return; }
            if (e.key !== 'Tab') return;
            const f = focusables();
            if (!f.length) return;
            const i = f.indexOf(document.activeElement);
            if (e.shiftKey && (i <= 0)) { e.preventDefault(); f[f.length - 1].focus(); }
            else if (!e.shiftKey && (i === -1 || i === f.length - 1)) { e.preventDefault(); f[0].focus(); }
        });
        const save = (src) => {
            const box = src.closest('.upd-prefs');
            const auto = box.querySelector('.upd-auto').checked;
            const channel = box.querySelector('.upd-channel-sel').value;
            action(() => api('/api/update/prefs', { auto, channel }),
                auto ? 'Aktualizace se budou instalovat automaticky v noci.' : 'Aktualizace jen ručně.')
                .then((r) => { if (r) applyUpdatePrefs(r.prefs); else refreshUpdateStatus(); });
        };
        document.querySelectorAll('.upd-auto, .upd-channel-sel').forEach((c) => c.addEventListener('change', () => save(c)));
        refreshUpdateStatus();
        setInterval(() => { if (!document.hidden && !UPD.open) refreshUpdateStatus(); }, 60000);
    }

    // ---------------------------------------------------------- historie událostí
    // [ikona, text, závažnost]  závažnost: '' | ok | warn | bad | info
    const ACTIONS = {
        LOGIN_SUCCESS: ['key', 'Přihlášení', 'ok'], LOGIN_FAILED: ['block', 'Neúspěšné přihlášení', 'bad'],
        LOGOUT: ['logout', 'Odhlášení', ''], SETUP_LOGIN: ['key', 'První přihlášení', 'warn'],
        SETUP_FAILED: ['block', 'Neplatné výchozí přihlášení', 'bad'], PASSWORD_CHANGED: ['key', 'Změna hesla', ''],
        USER_ADDED: ['user', 'Nový uživatel', 'ok'], USER_DELETED: ['user', 'Smazán uživatel', 'warn'],
        USER_ROLE_CHANGED: ['user', 'Změna role', 'warn'],
        SETTINGS_CHANGED: ['sliders', 'Změna nastavení', ''], CALIBRATION: ['ruler', 'Kalibrace', ''],
        NODE_PAIRED: ['antenna', 'Čidlo spárováno', 'ok'], NODE_PAIRING: ['antenna', 'Čekání na párování', ''],
        NODE_PAIRING_CANCELLED: ['antenna', 'Párování zrušeno', ''],
        NODE_REMOVED: ['antenna', 'Čidlo odebráno', 'warn'], NODE_UNPAIR_REQUESTED: ['antenna', 'Odpárování zahájeno', 'warn'],
        NODE_UNPAIRED: ['antenna', 'Čidlo odpárováno', 'warn'], NODE_UNPAIR_CANCELLED: ['antenna', 'Odpárování zrušeno', ''],
        UPDATE_REQUESTED: ['arrow-up', 'Aktualizace', ''], UPDATE_AVAILABLE: ['arrow-up', 'Dostupná aktualizace', 'info'],
        UPDATE_INSTALLED: ['check', 'Aktualizace nainstalována', 'ok'], UPDATE_ROLLED_BACK: ['restart', 'Aktualizace vrácena', 'bad'],
        UPDATE_ERROR: ['alert', 'Chyba aktualizace', 'bad'], UPDATE_UP_TO_DATE: ['check', 'Bez aktualizace', '']
    };
    const LOG = { page: 1, cat: '', lastDay: null };
    const DAYS = ['neděle', 'pondělí', 'úterý', 'středa', 'čtvrtek', 'pátek', 'sobota'];

    function dayTitle(t) {
        const d = new Date(t), today = new Date(), y = new Date(Date.now() - 86400e3);
        const same = (a, b) => a.toDateString() === b.toDateString();
        if (same(d, today)) return 'Dnes';
        if (same(d, y)) return 'Včera';
        return `${DAYS[d.getDay()]} ${d.getDate()}. ${d.getMonth() + 1}. ${d.getFullYear()}`;
    }

    function auditQuery(page) {
        const p = new URLSearchParams({ page: String(page) });
        if (LOG.cat) p.set('category', LOG.cat);
        if ($('logWho').value) p.set('who', $('logWho').value);
        const q = $('logSearch').value.trim();
        if (q) p.set('q', q);
        if ($('logFrom').value) p.set('from', String(Math.floor(new Date($('logFrom').value + 'T00:00:00').getTime() / 1000)));
        if ($('logTo').value) p.set('to', String(Math.floor(new Date($('logTo').value + 'T00:00:00').getTime() / 1000) + 86400));
        return '/api/audit?' + p.toString();
    }

    async function loadAuditLog(page) {
        const list = $('auditList');
        if (page === 1) { list.innerHTML = '<div class="muted">Načítám…</div>'; LOG.lastDay = null; }
        try {
            const d = await api(auditQuery(page));
            LOG.page = page;
            if (page === 1) list.textContent = '';
            const who = $('logWho');
            const known = Array.from(who.options).map((o) => o.value);
            (d.users || []).forEach((u) => {
                if (!known.includes(u)) {
                    const o = document.createElement('option');
                    o.value = u; o.textContent = u;
                    who.appendChild(o);
                }
            });
            if (!d.logs.length && page === 1) list.innerHTML = '<div class="muted log-empty">Žádné záznamy</div>';
            d.logs.forEach((l) => {
                const t = l.ts * 1000;
                const day = dayTitle(t);
                if (day !== LOG.lastDay) {
                    const h = document.createElement('div');
                    h.className = 'log-day';
                    h.textContent = day;
                    list.appendChild(h);
                    LOG.lastDay = day;
                }
                const [icon, text, sev] = ACTIONS[l.action] || ['dot', l.action, ''];
                const sys = !l.username || l.username.startsWith('system');
                const row = document.createElement('div');
                row.className = 'log-row ' + (sev ? 'sev-' + sev : '');
                const dt = new Date(t);
                row.innerHTML = `<span class="log-time">${pad(dt.getHours())}:${pad(dt.getMinutes())}</span>
                    <span class="log-what"><span class="log-icon">${ico(icon)}</span><span class="log-title">${esc(text)}</span></span>
                    <span class="log-detail">${esc(l.detail || '')}</span>
                    <span class="log-who"><span class="log-user ${sys ? 'sys' : ''}">${sys ? 'automatika' : esc(l.username)}</span>
                    ${l.ip && !sys ? `<span class="log-ip">${esc(l.ip)}</span>` : ''}</span>`;
                list.appendChild(row);
            });
            $('logSummary').textContent = `${d.total} záznamů`;
            const pag = $('auditPagination');
            pag.textContent = '';
            if (page < d.total_pages) {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-outline'; b.textContent = 'Načíst starší';
                b.addEventListener('click', () => loadAuditLog(page + 1));
                pag.appendChild(b);
            }
        } catch (e) {
            list.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
        }
    }

    document.querySelectorAll('#logChips .chip').forEach((c) => c.addEventListener('click', () => {
        document.querySelectorAll('#logChips .chip').forEach((x) => x.classList.toggle('active', x === c));
        LOG.cat = c.dataset.cat;
        loadAuditLog(1);
    }));
    ['logWho', 'logFrom', 'logTo'].forEach((id) => $(id).addEventListener('change', () => loadAuditLog(1)));
    let searchT = null;
    $('logSearch').addEventListener('input', () => { clearTimeout(searchT); searchT = setTimeout(() => loadAuditLog(1), 350); });
    $('auditRefresh').addEventListener('click', () => loadAuditLog(1));

    const menu = $('accountMenu');
    $('accountBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        const open = menu.classList.toggle('hidden') === false;
        $('accountBtn').setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    document.addEventListener('click', (e) => {
        if (!menu.contains(e.target)) { menu.classList.add('hidden'); $('accountBtn').setAttribute('aria-expanded', 'false'); }
    });
    document.querySelectorAll('#themeSeg button').forEach((b) => b.addEventListener('click', () => applyTheme(b.dataset.themePick)));
    $('logoutBtn').addEventListener('click', async () => {
        menu.classList.add('hidden');
        await api('/api/auth/logout', {}).catch(() => {});
        window.location.replace('/login');
    });
    $('addNodeBtn').addEventListener('click', () => pairDialog(null));
    function setAdvanced(on) {
        document.body.classList.toggle('show-adv', !!on);
        $('advToggle').checked = !!on;
        try { localStorage.setItem('advSettings', on ? '1' : ''); } catch (e) { /* ignore */ }
    }
    let adv = false;
    try { adv = localStorage.getItem('advSettings') === '1'; } catch (e) { /* ignore */ }
    setAdvanced(adv);
    $('advToggle').addEventListener('change', (e) => setAdvanced(e.target.checked));
    $('mqttForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        try {
            await api('/api/mqtt', { enabled: $('mqEnabled').checked, host: $('mqHost').value, port: Number($('mqPort').value),
                user: $('mqUser').value, password: $('mqPass').value });
            toast('Uloženo');
            loadMqtt();
            setTimeout(refresh, 1500);
        } catch (err) { toast(err.message, true); }
    });
    $('pwForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        try {
            if ($('pwNew').value !== $('pwNew2').value) { toast('Hesla se neshodují!', true); return; }
            await api('/api/me/password', { old_password: $('pwOld').value, new_password: $('pwNew').value });
            e.target.reset();
            toast('Heslo bylo změněno, ostatní zařízení byla odhlášena.');
        } catch (err) { toast(err.message, true); }
    });
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { readTheme(); reloadCharts(); });

    async function refresh() {
        try {
            const first = !STATE;
            STATE = await api('/api/state');
            render();
            if (first) reloadCharts();
        } catch (e) { $('statusPill').textContent = 'Hub nedostupný'; $('statusPill').className = 'status-pill offline'; }
    }

    let theme = 'auto';
    try { theme = localStorage.getItem('theme') || 'auto'; } catch (e) { /* ignore */ }
    document.querySelectorAll('#themeSeg button').forEach((b) => b.setAttribute('aria-pressed', b.dataset.themePick === theme ? 'true' : 'false'));
    readTheme();
    document.querySelectorAll('.range-picker').forEach(buildRangePicker);
    (async () => {
        const me = await api('/api/me').catch(() => null);
        if (!me) return;
        Object.assign(ME, me);
        applyMe();
        await refresh();
        let tab = 'dashboard';
        try { tab = sessionStorage.getItem('tab') || 'dashboard'; } catch (e) { /* ignore */ }
        if (tab !== 'dashboard') showTab(tab);
        setInterval(refresh, 15000);
        // čidla a párování: každé 4 s, dokud je otevřené Nastavení (jako u retenčky)
        setInterval(() => { if (ADMIN && $('nastaveni').classList.contains('active') && !document.hidden) refreshNodes(); }, 4000);
        setInterval(() => reloadCharts(true), 120000);
    })();
})();
