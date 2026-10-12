// Draws the two live maps (system map, data map) as inline SVG from read-only JSON the dashboard serves.
// Our own file, no libraries, no inline script, nothing loaded from outside (the content security policy
// forbids it). Every label is set with textContent, never as HTML.
(function () {
  'use strict';
  var SVG = 'http://www.w3.org/2000/svg';
  var host = document.getElementById('map');
  if (!host) { return; }

  function el(tag, attrs, parent, text) {
    var node = document.createElementNS(SVG, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) { node.textContent = text; }
    if (parent) { parent.appendChild(node); }
    return node;
  }
  function label(parent, x, y, text, cls, anchor) {
    var node = el('text', { x: x, y: y, 'class': cls || 'm-text' }, parent, text);
    if (anchor) { node.setAttribute('text-anchor', anchor); }
    return node;
  }
  function box(parent, x, y, w, h, cls) { return el('rect', { x: x, y: y, width: w, height: h, rx: 12, 'class': cls || 'm-box' }, parent); }
  function fmt(n) { return Number(n).toLocaleString('en-US'); }
  function ago(iso, nowIso) {
    if (!iso) { return 'never'; }
    var s = Math.max(0, Math.round((Date.parse(nowIso) - Date.parse(iso)) / 1000));
    if (s < 90) { return s + 's ago'; }
    if (s < 5400) { return Math.round(s / 60) + 'm ago'; }
    if (s < 129600) { return Math.round(s / 3600) + 'h ago'; }
    return Math.round(s / 86400) + 'd ago';
  }
  function every(seconds) {
    if (seconds % 86400 === 0) { return 'every ' + (seconds / 86400) + 'd'; }
    if (seconds % 3600 === 0) { return 'every ' + (seconds / 3600) + 'h'; }
    if (seconds % 60 === 0) { return 'every ' + (seconds / 60) + 'm'; }
    return 'every ' + seconds + 's';
  }
  function words(name) { return String(name).replace(/_/g, ' '); }
  function clip(text, n) { text = String(text || ''); return text.length > n ? text.slice(0, n - 1) + '…' : text; }
  function defs(svg) {
    var d = el('defs', {}, svg);
    var m = el('marker', { id: 'arrow', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, d);
    el('path', { d: 'M0 0L10 5L0 10z', 'class': 'm-arrow-head' }, m);
  }
  function arrow(svg, d, text, tx, ty) {
    el('path', { d: d, 'class': 'm-arrow', 'marker-end': 'url(#arrow)', fill: 'none' }, svg);
    if (text) { label(svg, tx, ty, text, 'm-count', 'middle'); }
  }

  // --- the system map ---------------------------------------------------------------------------------------
  function drawArchitecture(model) {
    var W = 1200;
    var gateRows = model.gate.domains.length;
    var colBottom = Math.max(44 + model.integrations.length * 60, 44 + model.agents.length * 52, 190 + 150 + gateRows * 18 + 16 + 70);
    var back = colBottom + 26, sy = back + 40, H = sy + 62 + 12;
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, 'class': 'map-svg', role: 'presentation' });
    defs(svg);
    var X = { svc: 10, run: 270, agent: 530, pol: 890 }, WID = { svc: 200, run: 200, agent: 290, pol: 300 };
    [['svc', 'SERVICES'], ['run', 'RUNTIME (POSTGRES)'], ['agent', 'AGENTS PROPOSE'], ['pol', 'CODE DECIDES']].forEach(function (c) {
      label(svg, X[c[0]], 28, c[1], 'm-head');
    });

    // services
    model.integrations.forEach(function (s, i) {
      var y = 44 + i * 60, g = el('g', {}, svg);
      box(g, X.svc, y, WID.svc, 52, 'm-box tone-' + (s.tone || 'muted'));
      label(g, X.svc + 12, y + 20, s.service, 'm-title');
      label(g, X.svc + WID.svc - 12, y + 20, s.state, 'm-badge tone-' + (s.tone || 'muted'), 'end');
      var note = s.polled ? 'polled ' + ago(s.polled, model.as_of) : clip(s.note || s.why, 34);
      label(g, X.svc + 12, y + 39, note, 'm-sub');
      el('title', {}, g, s.service + ': ' + s.state + (s.why ? ' (' + s.why + ')' : ''));
    });

    // runtime
    var rt = model.runtime, jobs = rt.jobs || {};
    var g = el('g', {}, svg);
    box(g, X.run, 44, WID.run, 76); label(g, X.run + 12, 66, 'Events', 'm-title');
    label(g, X.run + 12, 85, 'unique key: a duplicate is a no-op', 'm-sub');
    label(g, X.run + 12, 106, fmt(model.hour.events) + ' in the last hour', 'm-num');
    g = el('g', {}, svg);
    box(g, X.run, 134, WID.run, 150); label(g, X.run + 12, 156, 'Queue', 'm-title');
    label(g, X.run + 12, 174, 'SKIP LOCKED, retries, dead letters', 'm-sub');
    ['queued', 'running', 'done', 'dead'].forEach(function (k, i) {
      label(g, X.run + 12, 198 + i * 18, k, 'm-text');
      label(g, X.run + WID.run - 12, 198 + i * 18, fmt(jobs[k] || 0), 'm-num', 'end');
    });
    label(g, X.run + 12, 274, 'dead letters open: ' + fmt(rt.dead_open), rt.dead_open ? 'm-warn' : 'm-sub');
    g = el('g', {}, svg);
    box(g, X.run, 298, WID.run, 40 + rt.schedules.length * 20);
    label(g, X.run + 12, 320, 'Scheduler', 'm-title');
    rt.schedules.forEach(function (s, i) {
      label(g, X.run + 12, 342 + i * 20, s.event, 'm-text');
      label(g, X.run + WID.run - 12, 342 + i * 20, every(s.every_seconds), 'm-sub', 'end');
    });

    // agents
    model.agents.forEach(function (a, i) {
      var y = 44 + i * 52, gg = el('g', {}, svg);
      box(gg, X.agent, y, WID.agent, 44, 'm-box agent-' + a.kind);
      label(gg, X.agent + 12, y + 18, a.name, 'm-title');
      label(gg, X.agent + WID.agent - 12, y + 18, a.kind === 'judgement' ? 'model-judged' : 'rules', 'm-badge agent-tag-' + a.kind, 'end');
      label(gg, X.agent + 12, y + 35, a.last_run ? 'last run ' + ago(a.last_run, model.as_of) + ' (' + a.runner + ')' : 'no runs yet', 'm-sub');
      label(gg, X.agent + WID.agent - 12, y + 35, fmt(a.runs_1h) + '/h', 'm-count', 'end');
      el('title', {}, gg, a.does);
    });

    // policy, send gate, outbox
    var p = model.policy, gate = model.gate;
    g = el('g', {}, svg);
    box(g, X.pol, 44, WID.pol, 130); label(g, X.pol + 12, 66, 'Policy engine', 'm-title');
    label(g, X.pol + 12, 84, 'allowlist: an unknown action is denied', 'm-sub');
    label(g, X.pol + 12, 108, 'autonomy', 'm-text'); label(g, X.pol + WID.pol - 12, 108, p.autonomy_mode, 'm-num', 'end');
    label(g, X.pol + 12, 128, 'kill switch', 'm-text');
    label(g, X.pol + WID.pol - 12, 128, p.kill_switch ? 'ON: nothing is sent' : 'off', p.kill_switch ? 'm-warn' : 'm-num', 'end');
    label(g, X.pol + 12, 148, 'veto window', 'm-text'); label(g, X.pol + WID.pol - 12, 148, p.veto_seconds + ' s', 'm-num', 'end');
    g = el('g', {}, svg);
    var gateH = 150 + gate.domains.length * 18;
    box(g, X.pol, 190, WID.pol, gateH); label(g, X.pol + 12, 212, 'Send gate', 'm-title');
    label(g, X.pol + 12, 230, 're-reads approval from the database', 'm-sub');
    var daily = gate.caps.map(function (c) { return c.daily; });
    var perDomain = gate.caps.map(function (c) { return c.per_domain; });
    var same = function (a) { return a.every(function (v) { return v === a[0]; }); };
    label(g, X.pol + 12, 254, 'daily cap, ' + gate.day, 'm-text');
    label(g, X.pol + WID.pol - 12, 254, fmt(gate.daily_used) + ' / ' + (daily.length && same(daily) ? daily[0] : daily.join(' or ')), 'm-num', 'end');
    gate.domains.forEach(function (d, i) {
      label(g, X.pol + 12, 274 + i * 18, 'domain ' + clip(d.domain, 22), 'm-text');
      label(g, X.pol + WID.pol - 12, 274 + i * 18, fmt(d.used) + ' / ' + (perDomain.length && same(perDomain) ? perDomain[0] : perDomain.join(' or ')), 'm-num', 'end');
    });
    var by = 274 + gate.domains.length * 18;
    label(g, X.pol + 12, by + 6, 'send-window bypass rows', 'm-text'); label(g, X.pol + WID.pol - 12, by + 6, fmt(gate.bypass_send_window), 'm-num', 'end');
    label(g, X.pol + 12, by + 26, 'domain-cap bypass rows', 'm-text'); label(g, X.pol + WID.pol - 12, by + 26, fmt(gate.bypass_domain_cap), 'm-num', 'end');
    label(g, X.pol + 12, by + 46, 'demo only, each one audited', 'm-sub');
    var oy = 190 + gateH + 16;
    g = el('g', {}, svg);
    box(g, X.pol, oy, WID.pol, 70); label(g, X.pol + 12, oy + 22, 'Outbox', 'm-title');
    label(g, X.pol + 12, oy + 40, 'every side effect, checked with the provider first', 'm-sub');
    label(g, X.pol + 12, oy + 58, fmt(model.hour.sent) + ' sent in the last hour', 'm-num');

    // arrows with the last-hour counts
    arrow(svg, 'M' + (X.svc + WID.svc + 2) + ' 82 L' + (X.run - 4) + ' 82', fmt(model.hour.events) + '/h', (X.svc + WID.svc + X.run) / 2, 74);
    arrow(svg, 'M' + (X.run + WID.run + 2) + ' 82 L' + (X.agent - 4) + ' 82', fmt(model.hour.runs) + ' runs/h', (X.run + WID.run + X.agent) / 2, 74);
    arrow(svg, 'M' + (X.agent + WID.agent + 2) + ' 82 L' + (X.pol - 4) + ' 82', fmt(model.hour.decisions) + '/h', (X.agent + WID.agent + X.pol) / 2, 74);
    var outY = oy + 70;
    arrow(svg, 'M' + (X.pol + WID.pol / 2) + ' ' + outY + ' L' + (X.pol + WID.pol / 2) + ' ' + back + ' L' + (X.svc + WID.svc / 2) + ' ' + back + ' L' + (X.svc + WID.svc / 2) + ' ' + (44 + model.integrations.length * 60 + 2),
          'side effects sent, last hour: ' + fmt(model.hour.sent), (X.pol + X.svc) / 2, back - 8);

    // stages
    label(svg, 10, sy - 12, 'DEAL STAGES, LIVE COUNT (a deal changes stage only through transition_deal)', 'm-head');
    var sw = 92, gap = 7;
    model.stages.forEach(function (s, i) {
      var x = 10 + i * (sw + gap), gs = el('g', {}, svg);
      box(gs, x, sy, sw, 62, 'm-box stage' + (s.n ? ' has' : ''));
      var parts = words(s.stage).split(' ');
      label(gs, x + 8, sy + 16, parts[0], 'm-sub');
      if (parts[1]) { label(gs, x + 8, sy + 28, parts.slice(1).join(' '), 'm-sub'); }
      label(gs, x + sw - 8, sy + 54, fmt(s.n), 'm-big', 'end');
      var need = s.needs.map(function (n) { return (n.pack ? n.pack + ': ' : '') + n.fields.join(', '); }).join(' | ');
      el('title', {}, gs, words(s.stage) + ': ' + s.n + ' deal(s)' + (need ? '. To enter: ' + need : '. No entry fields required.'));
    });
    return svg;
  }

  // --- the data map -----------------------------------------------------------------------------------------
  var CARD_W = 232, COL_GAP = 44, ROW_H = 15, HEAD_H = 28, MAX_ROWS = 8;
  var TYPES = { 'timestamp with time zone': 'timestamptz', 'character varying': 'varchar', integer: 'int', boolean: 'bool', 'double precision': 'float', ARRAY: 'array', 'USER-DEFINED': 'enum' };
  var selected = null;

  function rowsFor(t) {
    var cols = t.columns.slice(), pk = t.pk || [];
    cols.sort(function (a, b) {
      var rank = function (c) { return pk.indexOf(c.name) >= 0 ? 0 : c.fk ? 1 : 2; };
      return rank(a) - rank(b);
    });
    var shown = cols.slice(0, MAX_ROWS);
    return { shown: shown, more: cols.length - shown.length };
  }
  function footer(t) {
    var parts = [];
    if (t.triggers.length) { parts.push('trigger: ' + t.triggers.join(', ')); }
    if (t.checks) { parts.push(t.checks + ' CHECK' + (t.checks > 1 ? 's' : '')); }
    return parts.join(' · ');
  }
  function drawDataModel(model) {
    var cols = model.areas.map(function (a) { return model.tables.filter(function (t) { return t.area === a; }); });
    var pos = {}, colH = 0;
    cols.forEach(function (tables, ci) {
      var y = 44;
      tables.forEach(function (t) {
        var r = rowsFor(t), h = HEAD_H + r.shown.length * ROW_H + (r.more ? ROW_H : 0) + (footer(t) ? 20 : 8);
        pos[t.name] = { x: 10 + ci * (CARD_W + COL_GAP), y: y, h: h, r: r, col: ci };
        y += h + 14;
      });
      colH = Math.max(colH, y);
    });
    var W = 20 + cols.length * (CARD_W + COL_GAP), H = colH + 10;
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, 'class': 'map-svg', role: 'presentation' });
    defs(svg);
    model.areas.forEach(function (a, ci) { label(svg, 10 + ci * (CARD_W + COL_GAP), 26, a.toUpperCase(), 'm-head'); });

    // foreign-key lines first, so the cards sit on top of them
    var linkNodes = [];
    model.links.forEach(function (l) {
      var a = pos[l.from], b = pos[l.to];
      if (!a || !b) { return; }
      var from = model.tables.filter(function (t) { return t.name === l.from; })[0];
      var idx = a.r.shown.findIndex(function (c) { return l.cols.indexOf(c.name) >= 0; });
      var y1 = a.y + HEAD_H + (idx >= 0 ? idx * ROW_H + ROW_H / 2 : -HEAD_H / 2 + 2);
      var y2 = b.y + HEAD_H / 2, x1, x2, d;
      if (a.col === b.col) {
        x1 = a.x + CARD_W; x2 = b.x + CARD_W;
        d = 'M' + x1 + ' ' + y1 + ' C' + (x1 + 34) + ' ' + y1 + ' ' + (x2 + 34) + ' ' + y2 + ' ' + x2 + ' ' + y2;
      } else if (b.col > a.col) {
        x1 = a.x + CARD_W; x2 = b.x;
        d = 'M' + x1 + ' ' + y1 + ' C' + (x1 + 30) + ' ' + y1 + ' ' + (x2 - 30) + ' ' + y2 + ' ' + x2 + ' ' + y2;
      } else {
        x1 = a.x; x2 = b.x + CARD_W;
        d = 'M' + x1 + ' ' + y1 + ' C' + (x1 - 30) + ' ' + y1 + ' ' + (x2 + 30) + ' ' + y2 + ' ' + x2 + ' ' + y2;
      }
      var p = el('path', { d: d, 'class': 'dm-link', fill: 'none', 'marker-end': 'url(#arrow)' }, svg);
      el('title', {}, p, l.from + '.' + l.cols.join(', ') + ' references ' + l.to);
      linkNodes.push({ node: p, from: l.from, to: l.to });
    });

    var cards = {};
    model.tables.forEach(function (t) {
      var p = pos[t.name], g = el('g', { 'class': 'dm-card', tabindex: 0, role: 'button', 'aria-label': t.name + ', ' + t.rows + ' rows' }, svg);
      cards[t.name] = g;
      el('rect', { x: p.x, y: p.y, width: CARD_W, height: p.h, rx: 10, 'class': 'm-box' + (t.triggers.length ? ' guarded' : '') }, g);
      label(g, p.x + 10, p.y + 19, t.name, 'm-title');
      label(g, p.x + CARD_W - 10, p.y + 19, fmt(t.rows) + (t.rows === 1 ? ' row' : ' rows'), 'm-num', 'end');
      el('line', { x1: p.x, x2: p.x + CARD_W, y1: p.y + HEAD_H - 3, y2: p.y + HEAD_H - 3, 'class': 'm-rule' }, g);
      p.r.shown.forEach(function (c, i) {
        var y = p.y + HEAD_H + i * ROW_H + 11, isPk = (t.pk || []).indexOf(c.name) >= 0;
        label(g, p.x + 10, y, (isPk ? 'PK ' : c.fk ? 'FK ' : '') + c.name, 'm-col' + (isPk ? ' pk' : c.fk ? ' fk' : ''));
        label(g, p.x + CARD_W - 10, y, (TYPES[c.type] || c.type) + (c.null ? '' : ' *'), 'm-sub', 'end');
      });
      if (p.r.more) { label(g, p.x + 10, p.y + HEAD_H + p.r.shown.length * ROW_H + 11, '+ ' + p.r.more + ' more columns', 'm-sub'); }
      var foot = footer(t);
      if (foot) { label(g, p.x + 10, p.y + p.h - 7, clip(foot, 44), 'm-guard'); }
      var activate = function () { selected = selected === t.name ? null : t.name; applySelection(); };
      g.addEventListener('click', activate);
      g.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); } });
      el('title', {}, g, t.name + ': ' + t.rows + ' rows, ' + t.columns.length + ' columns. Click to highlight its links.');
    });

    function applySelection() {
      var related = {};
      if (selected) {
        related[selected] = true;
        linkNodes.forEach(function (l) { if (l.from === selected || l.to === selected) { related[l.from] = related[l.to] = true; } });
      }
      Object.keys(cards).forEach(function (name) {
        cards[name].setAttribute('class', 'dm-card' + (selected && !related[name] ? ' dim' : '') + (name === selected ? ' sel' : ''));
      });
      linkNodes.forEach(function (l) {
        var hot = selected && (l.from === selected || l.to === selected);
        l.node.setAttribute('class', 'dm-link' + (hot ? ' hot' : selected ? ' dim' : ''));
      });
    }
    applySelection();
    return svg;
  }

  // --- load, draw, refresh ---------------------------------------------------------------------------------
  var draw = host.dataset.kind === 'datamodel' ? drawDataModel : drawArchitecture;
  var last = '';
  function load() {
    fetch(host.dataset.src, { credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { window.location.href = '/dashboard/login'; return null; }
        return r.ok ? r.text() : null;
      })
      .then(function (text) {
        if (!text) { return; }
        var stamp = document.getElementById('map-stamp');
        if (stamp) { stamp.textContent = 'Updated ' + new Date().toLocaleTimeString('en-GB'); }
        if (text === last) { return; }
        last = text;
        host.replaceChildren(draw(JSON.parse(text)));
      })
      .catch(function () {
        var stamp = document.getElementById('map-stamp');
        if (stamp) { stamp.textContent = 'Could not load'; }
      });
  }
  load();
  setInterval(function () { if (!document.hidden) { load(); } }, 5000);
})();
