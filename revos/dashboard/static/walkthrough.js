// The animated deal walkthrough: draws the system once, then lights the nodes and arrows of each step and shows
// what that deal's audit log recorded for it. Our own file, no libraries, labels set with textContent only.
(function () {
  'use strict';
  var SVG = 'http://www.w3.org/2000/svg';
  var mapHost = document.getElementById('wt-map');
  if (!mapHost) { return; }
  var $ = function (id) { return document.getElementById(id); };
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var model = null, index = 0, timer = null, nodeFocus = null, svg = null, nodeEls = {}, edgeEls = {}, dots = [];

  function el(tag, attrs, parent, text) {
    var node = document.createElementNS(SVG, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) { node.textContent = text; }
    if (parent) { parent.appendChild(node); }
    return node;
  }
  function h(tag, cls, text, parent) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined) { node.textContent = text; }
    if (parent) { parent.appendChild(node); }
    return node;
  }
  function clip(t, n) { t = String(t || ''); return t.length > n ? t.slice(0, n - 1) + '…' : t; }
  function words(s) { return String(s).replace(/_/g, ' '); }

  var DIR = { r: [1, 0], l: [-1, 0], t: [0, -1], b: [0, 1] };
  function anchor(n, side) {
    if (side === 'r') { return [n.x + n.w, n.y + n.h / 2]; }
    if (side === 'l') { return [n.x, n.y + n.h / 2]; }
    if (side === 't') { return [n.x + n.w / 2, n.y]; }
    return [n.x + n.w / 2, n.y + n.h];
  }
  function pathFor(a, b, sa, sb) {
    var p1 = anchor(a, sa), p2 = anchor(b, sb), d1 = DIR[sa], d2 = DIR[sb], k = 44;
    return 'M' + p1[0] + ' ' + p1[1] + ' C' + (p1[0] + d1[0] * k) + ' ' + (p1[1] + d1[1] * k) + ' ' +
           (p2[0] + d2[0] * k) + ' ' + (p2[1] + d2[1] * k) + ' ' + p2[0] + ' ' + p2[1];
  }

  function build() {
    var W = 1260, H = 600;
    svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, 'class': 'map-svg wt-svg', role: 'presentation' });
    var d = el('defs', {}, svg);
    var m = el('marker', { id: 'wt-arrow', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 9, markerHeight: 9, markerUnits: 'userSpaceOnUse', orient: 'auto-start-reverse' }, d);
    el('path', { d: 'M0 0L10 5L0 10z', 'class': 'wt-arrow-head' }, m);
    var byId = {};
    model.nodes.forEach(function (n) { byId[n.id] = n; });
    var cols = [['TRIGGERS', 10], ['CORE', 190], ['AGENTS', 395], ['POLICY', 615], ['ROUTES', 785], ['GATE', 965], ['WORLD', 1125]];
    cols.forEach(function (c) { el('text', { x: c[1], y: 30, 'class': 'm-head' }, svg, c[0]); });
    nodeEls = {}; edgeEls = {};
    model.edges.forEach(function (e) {
      var a = byId[e.from], b = byId[e.to];
      var p = el('path', { d: pathFor(a, b, e.fromSide, e.toSide), 'class': 'wt-edge', fill: 'none', 'marker-end': 'url(#wt-arrow)' }, svg);
      edgeEls[e.id] = { node: p, d: p.getAttribute('d') };
    });
    model.nodes.forEach(function (n) {
      var g = el('g', { 'class': 'wt-node' + (n.world ? ' world' : ''), tabindex: 0, role: 'button', 'aria-label': n.title }, svg);
      el('rect', { x: n.x, y: n.y, width: n.w, height: n.h, rx: 10, 'class': 'wt-box' }, g);
      el('text', { x: n.x + 10, y: n.y + 20, 'class': 'm-title' }, g, n.title);
      if (n.world && n.sub) { el('text', { x: n.x + 10, y: n.y + 38, 'class': 'wt-badge' }, g, n.sub); }
      else if (n.sub) { wrapInto(g, n.x + 10, n.y + 36, n.sub, Math.floor(n.w / 6.3)); }
      el('title', {}, g, n.title + ': ' + n.does);
      var pick = function () { nodeFocus = nodeFocus === n.id ? null : n.id; renderPanel(); };
      g.addEventListener('click', pick);
      g.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); pick(); } });
      nodeEls[n.id] = g;
    });
    model.edges.forEach(function (e) {
      if (!e.label) { return; }
      var p2 = anchor(byId[e.to], e.toSide);
      el('text', { x: p2[0] - 20, y: p2[1] - 6, 'class': 'wt-edge-label', 'text-anchor': 'middle' }, svg, e.label);
    });
    mapHost.replaceChildren(svg);
  }
  function wrapInto(g, x, y, text, max) {
    var lines = [], line = '';
    String(text).split(' ').forEach(function (w) {
      if ((line + ' ' + w).trim().length > max) { lines.push(line); line = w; } else { line = (line + ' ' + w).trim(); }
    });
    if (line) { lines.push(line); }
    lines.slice(0, 3).forEach(function (l, i) { el('text', { x: x, y: y + i * 12, 'class': 'm-sub' }, g, l); });
  }

  function fmtTime(iso) {
    var d = new Date(iso);
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' + d.toLocaleTimeString('en-GB');
  }
  function renderPanel() {
    var step = model.steps[index], box = $('wt-step');
    box.replaceChildren();
    var left = h('div', 'wt-left', undefined, box), right = h('div', 'wt-right', undefined, box);
    h('p', 'eyebrow', 'Step ' + (index + 1) + ' of ' + model.steps.length, left);
    h('h3', 'wt-title', step.title, left);
    h('p', 'wt-text', step.text, right);
    var rec = h('div', 'wt-rec', undefined, right);
    h('p', 'wt-rec-head', model.deal ? 'Recorded for ' + model.deal.company : 'Recorded', rec);
    if (!step.recorded.length) { h('p', 'muted small', 'No matching record for this deal at this step.', rec); }
    step.recorded.forEach(function (r) {
      var line = h('p', 'wt-rec-line', undefined, rec);
      h('span', 'wt-rec-time', fmtTime(r.at), line);
      h('span', 'wt-rec-event', r.event, line);
      if (r.note) { h('span', 'wt-rec-note', r.note, line); }
    });
    if (nodeFocus) {
      var n = model.nodes.filter(function (x) { return x.id === nodeFocus; })[0];
      var card = h('div', 'wt-node-card', undefined, right);
      h('strong', '', n.title + ': ', card);
      card.appendChild(document.createTextNode(n.does));
    }
  }
  function render() {
    var step = model.steps[index], lit = {}, litEdges = {}, seen = {};
    step.nodes.forEach(function (id) { lit[id] = true; });
    step.edges.forEach(function (id) { litEdges[id] = true; });
    for (var i = 0; i < index; i++) { model.steps[i].nodes.forEach(function (id) { seen[id] = true; }); }
    Object.keys(nodeEls).forEach(function (id) {
      nodeEls[id].setAttribute('class', 'wt-node' + (nodeEls[id].classList.contains('world') ? ' world' : '') +
        (lit[id] ? ' on' : seen[id] ? ' seen' : ' off'));
    });
    dots.forEach(function (d) { d.remove(); }); dots = [];
    Object.keys(edgeEls).forEach(function (id) {
      edgeEls[id].node.setAttribute('class', 'wt-edge' + (litEdges[id] ? ' on' : ''));
      if (litEdges[id] && !reduced) {
        var dot = el('circle', { r: 5, 'class': 'wt-dot', fill: '#e0552a', stroke: '#ffffff', 'stroke-width': 1.5 }, svg);
        var motion = el('animateMotion', { dur: '1.4s', repeatCount: 'indefinite', path: edgeEls[id].d }, dot);
        motion.setAttribute('rotate', 'auto');
        dots.push(dot);
      }
    });
    var chips = $('wt-stages').children, order = model.stages, here = order.indexOf(step.stage);
    for (var j = 0; j < chips.length; j++) { chips[j].className = 'wt-stage' + (j < here ? ' done' : j === here ? ' on' : ''); }
    $('wt-back').disabled = index === 0;
    $('wt-next').disabled = index === model.steps.length - 1;
    renderPanel();
  }
  function stop() { if (timer) { clearTimeout(timer); timer = null; } $('wt-play').textContent = 'Play'; }
  function go(n) { index = Math.max(0, Math.min(model.steps.length - 1, n)); render(); }
  function schedule() {
    timer = setTimeout(function () {
      if (index >= model.steps.length - 1) { stop(); return; }
      go(index + 1); schedule();
    }, parseInt($('wt-speed').value, 10));
  }
  function play() {
    if (timer) { stop(); return; }
    if (index >= model.steps.length - 1) { go(0); }
    $('wt-play').textContent = 'Pause';
    schedule();
  }

  function fill(data) {
    model = data; index = 0; nodeFocus = null; stop();
    $('wt-chip').textContent = data.deal ? data.deal.company + ' (' + words(data.deal.stage) + ')' : 'No deals yet';
    var pick = $('wt-deal');
    pick.replaceChildren();
    data.deals.forEach(function (d) {
      var o = document.createElement('option'); o.value = d.id; o.textContent = d.company + ' (' + words(d.stage) + ')';
      if (data.deal && d.id === data.deal.id) { o.selected = true; }
      pick.appendChild(o);
    });
    var chips = $('wt-stages'); chips.replaceChildren();
    data.stages.forEach(function (s) { h('li', 'wt-stage', words(s), chips); });
    build(); render();
  }
  function load(deal) {
    fetch('/dashboard/api/walkthrough' + (deal ? '?deal=' + encodeURIComponent(deal) : ''), { credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { window.location.href = '/dashboard/login'; return null; }
        return r.ok ? r.json() : null;
      })
      .then(function (data) { if (data) { fill(data); } })
      .catch(function () { $('wt-chip').textContent = 'Could not load'; });
  }

  $('wt-play').addEventListener('click', function () { if (model) { play(); } });
  $('wt-next').addEventListener('click', function () { if (model) { stop(); go(index + 1); } });
  $('wt-back').addEventListener('click', function () { if (model) { stop(); go(index - 1); } });
  $('wt-restart').addEventListener('click', function () { if (model) { stop(); go(0); } });
  $('wt-deal').addEventListener('change', function (e) { stop(); load(e.target.value); });
  document.addEventListener('keydown', function (e) {
    if (!model || /^(INPUT|SELECT|TEXTAREA)$/.test((document.activeElement || {}).tagName || '')) { return; }
    if (e.key === 'ArrowRight') { stop(); go(index + 1); }
    if (e.key === 'ArrowLeft') { stop(); go(index - 1); }
  });
  load('');
})();
