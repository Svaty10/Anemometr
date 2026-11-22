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
            s4: cssVar('--series-4'), s5: cssVar('--series-5'), s6: cssVar('--series-6')
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
                reloadChart(key);
            });
            seg.appendChild(b);
        });
        el.appendChild(seg);
    }
    function span(key) { const p = PRESETS.find((x) => x[0] === ranges[key]); return p ? p[2] : 86400; }

    function timeTicks(min, max, count) {
        const steps = [3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400, 61 * 86400];
        const range = (max - min) / 1000;
        const step = steps.find((s) => range / s <= count) || steps[steps.length - 1];
        const off = new Date().getTimezoneOffset() * 60;
        const out = [];
        let t = Math.ceil((min / 1000 - off) / step) * step + off;
        for (; t * 1000 <= max; t += step) out.push({ value: t * 1000, day: step >= 86400 });
        return out;
    }
    function timeScale(frm, to) {
        let unit = new Map();
        return {
            type: 'linear', min: frm * 1000, max: to * 1000,
            afterBuildTicks: (axis) => {
                const ticks = timeTicks(axis.min, axis.max, window.innerWidth < 640 ? 4 : 8);
                unit = new Map(ticks.map((t) => [t.value, t.day]));
                axis.ticks = ticks.map((t) => ({ value: t.value }));
            },
            ticks: {
                autoSkip: true, maxRotation: 0,
                callback: (v) => {
                    const d = new Date(v);
                    if (unit.get(v)) return `${d.getDate()}. ${d.getMonth() + 1}.`;
                    return d.toLocaleTimeString('cs-CZ', { hour: '2-digit', minute: '2-digit' });
                }
            }
        };
    }
    const series = (t, vals) => t.map((x, i) => ({ x: x * 1000, y: vals[i] }));
    function lastDot(values, color) {
        let last = -1;
        for (let i = values.length - 1; i >= 0; i--) { if (values[i] != null) { last = i; break; } }
        return {
            pointRadius: (c) => (c.dataIndex === last ? 4 : 0),
            pointBorderWidth: (c) => (c.dataIndex === last ? 2 : 1),
            pointBorderColor: (c) => (c.dataIndex === last ? T.surface : color),
            pointBackgroundColor: color
        };
    }
    const line = (color, extra) => Object.assign({ borderColor: color, backgroundColor: color, pointBackgroundColor: color, pointBorderColor: color }, extra || {});
    function upsert(key, canvas, config) {
        if (charts[key]) charts[key].destroy();
        charts[key] = new Chart($(canvas).getContext('2d'), config);
    }
    const tip = (unit, d = 1) => ({
        callbacks: {
            title: (ctx) => (ctx.length ? fmtDateTime(ctx[0].parsed.x) : ''),
            label: (c) => `${c.dataset.label}: ${num(c.parsed.y, d, ' ' + unit)}`
        }
    });
    const toMs = (h) => Object.assign(h, {
        speed: h.speed.map((v) => (v == null ? null : Math.round(v / 3.6 * 10) / 10)),
        gust: h.gust.map((v) => (v == null ? null : Math.round(v / 3.6 * 10) / 10))
    });
    const history = (id, frm, to) => api(`/api/history?id=${id}&from=${frm}&to=${to}`);

    async function reloadChart(key) {
        if (typeof Chart === 'undefined' || !STATE || !STATE.nodes.length) return;
        const to = Math.floor(Date.now() / 1000);
        const frm = to - span(key);
        try {
            if (key === 'wind' && CURRENT != null && !VIEW_ALL) {
                const h = toMs(await history(CURRENT, frm, to));
                upsert('wind', 'windChart', {
                    type: 'line',
                    data: { datasets: [
                        line(T.s1, { label: h.bucket ? 'Průměrná rychlost' : 'Rychlost', data: series(h.t, h.speed), fill: { target: 'origin', above: T.s1 + '22' }, ...lastDot(h.speed, T.s1) }),
                        line(T.s2, { label: h.bucket ? 'Nejsilnější náraz' : 'Náraz', data: series(h.t, h.gust), borderWidth: 1.5 })
                    ] },
                    options: {
                        responsive: true, maintainAspectRatio: false, parsing: false, interaction: { mode: 'index', intersect: false },
                        plugins: { tooltip: tip('m/s') },
                        scales: { x: timeScale(frm, to), y: { beginAtZero: true, ticks: { callback: (v) => v + ' m/s' } } }
                    }
                });
            } else if (key === 'battery' && CURRENT != null && !VIEW_ALL) {
                const h = await history(CURRENT, frm, to);
                upsert('battery', 'batteryChart', {
                    type: 'line',
                    data: { datasets: [
                        line(T.s3, { label: 'Baterie', data: series(h.t, h.battery), spanGaps: true, yAxisID: 'y' }),
                        line(T.s4, { label: 'Signál', data: series(h.t, h.rssi), borderWidth: 1.5, yAxisID: 'y1' })
                    ] },
                    options: {
                        responsive: true, maintainAspectRatio: false, parsing: false, interaction: { mode: 'index', intersect: false },
                        plugins: { tooltip: { callbacks: { title: (c) => (c.length ? fmtDateTime(c[0].parsed.x) : ''),
                            label: (c) => c.dataset.yAxisID === 'y' ? `Baterie: ${num(c.parsed.y, 2, ' V')}` : `Signál: ${num(c.parsed.y, 0, ' dBm')}` } } },
                        scales: {
                            x: timeScale(frm, to),
                            y: { position: 'left', ticks: { callback: (v) => num(v, 2, ' V') } },
                            y1: { position: 'right', grid: { display: false }, ticks: { callback: (v) => v + ' dBm' } }
                        }
                    }
                });
            } else if (key === 'all' && VIEW_ALL) {
                const colors = [T.s1, T.s2, T.s3, T.s4, T.s5, T.s6];
                const hs = (await Promise.all(STATE.nodes.map((n) => history(n.id, frm, to)))).map(toMs);
                upsert('all', 'allChart', {
                    type: 'line',
                    data: { datasets: STATE.nodes.map((n, i) => line(colors[i % 6], { label: n.name, data: series(hs[i].t, hs[i].speed), ...lastDot(hs[i].speed, colors[i % 6]) })) },
                    options: {
                        responsive: true, maintainAspectRatio: false, parsing: false, interaction: { mode: 'index', intersect: false },
                        plugins: { tooltip: tip('m/s') },
                        scales: { x: timeScale(frm, to), y: { beginAtZero: true, ticks: { callback: (v) => v + ' m/s' } } }
                    }
                });
            }
        } catch (e) { /* hub se restartuje – příště */ }
    }
    function reloadCharts() {
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
        const q = n.link_pct == null ? null : (n.link_pct >= 60 ? 'výborný' : n.link_pct >= 35 ? 'dobrý' : n.link_pct >= 15 ? 'slabý' : 'velmi slabý');
        $('txtRssi').textContent = n.rssi == null ? '–' : `${q} · ${n.link_pct} %`;
        $('txtLoss').textContent = n.rssi == null ? '–' : `${num(n.rssi, 0)} dBm` + (n.loss_pct == null ? '' : ` · ztráty 24 h ${num(n.loss_pct, 1, ' %')}`);
        $('txtTemp').textContent = n.temperature == null ? '–' : num(n.temperature, 0, ' °C');
        $('txtBat').textContent = n.battery_pct == null ? '–' : n.battery_pct + ' %' + (n.battery_low ? ' – vyměnit' : '');
        $('txtBat').classList.toggle('bad-text', !!n.battery_low);
        $('txtBatV').textContent = n.battery_v == null ? '–' : num(n.battery_v, 2, ' V');
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
                <span>${ico('battery')} <span class="${n.battery_low ? 'bad' : ''}">${n.battery_pct == null ? '–' : n.battery_pct + ' %'}</span></span></div></div>`;
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
        if (ADMIN) { loadMqtt(); renderSettings(); buildSetNav(); }
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
                <div class="inline-actions">
                    <button type="submit" class="btn btn-primary">Uložit</button>
                </div>
            </form>`;
        const f = c.querySelector('form');
        f.name.value = n.name;
        f.rev.value = Math.round(n.rev_ms * 1000) / 1000;
        f.unit.value = 'ms';
        f.ppr.value = n.ppr;
        f.unit.addEventListener('change', () => {          // přepočet čísla při změně jednotky
            const v = Number(f.rev.value);
            if (v) f.rev.value = Math.round((f.unit.value === 'kmh' ? v * 3.6 : v / 3.6) * 1000) / 1000;
        });
        f.addEventListener('submit', async (e) => {
            e.preventDefault();
            try {
                await api('/api/node/update', { id: n.id, name: f.name.value, rev_value: f.rev.value, rev_unit: f.unit.value, ppr: f.ppr.value });
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
        NODE_UNPAIRED: ['antenna', 'Čidlo odpárováno', 'warn'], NODE_UNPAIR_CANCELLED: ['antenna', 'Odpárování zrušeno', '']
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
        setInterval(reloadCharts, 120000);
    })();
})();
