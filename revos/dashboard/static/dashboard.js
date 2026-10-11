// Refreshes the page body every few seconds, runs the veto countdowns and the confirm prompts.
// No inline script anywhere (the page's content security policy forbids it).
(function () {
  var meta = document.querySelector('meta[name="server-now"]');
  var offset = meta && meta.content ? Date.parse(meta.content) - Date.now() : 0;

  function tick() {
    document.querySelectorAll('.countdown').forEach(function (el) {
      var left = Math.round((Date.parse(el.dataset.until) - (Date.now() + offset)) / 1000);
      if (left <= 0) { el.textContent = 'sending…'; el.classList.add('done'); return; }
      var m = Math.floor(left / 60), s = left % 60;
      el.textContent = (m ? m + 'm ' : '') + s + 's';
    });
  }

  document.addEventListener('submit', function (event) {
    var ask = event.target.getAttribute('data-confirm');
    if (ask && !window.confirm(ask)) { event.preventDefault(); }
  });

  var every = parseInt(document.body.dataset.refresh || '0', 10);
  function busy() {
    var a = document.activeElement;
    return a && (a.tagName === 'INPUT' || a.tagName === 'SELECT' || a.tagName === 'TEXTAREA');
  }
  function refresh() {
    if (document.hidden || busy() || document.querySelector('details[open]')) { return; }
    fetch(location.href, { credentials: 'same-origin', headers: { 'X-Requested-With': 'refresh' } })
      .then(function (r) { return r.ok && !r.redirected ? r.text() : null; })
      .then(function (html) {
        if (!html) { return; }
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var fresh = doc.getElementById('content');
        if (fresh) {
          var current = document.getElementById('content');
          if (current.innerHTML !== fresh.innerHTML) { current.innerHTML = fresh.innerHTML; }
          var stamp = document.getElementById('refreshed');
          if (stamp) { stamp.textContent = 'Updated ' + new Date().toLocaleTimeString(); }
        }
        var bell = doc.querySelector('nav');
        if (bell) { document.querySelector('nav').innerHTML = bell.innerHTML; }
      })
      .catch(function () {});
  }
  setInterval(tick, 1000);
  tick();
  if (every > 0) { setInterval(refresh, every * 1000); }
})();
