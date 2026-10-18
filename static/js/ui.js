/* UniAlert interface behaviour: account menu, mobile menu, theme switching,
   password field tools, copy buttons and the one-time stat count-up. */
(function () {
  'use strict';
  var root = document.documentElement;
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- Theme ---------- */
  function markActiveTheme() {
    var current = root.getAttribute('data-theme');
    document.querySelectorAll('[data-theme-choice]').forEach(function (b) {
      var on = b.getAttribute('data-theme-choice') === current;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-theme-choice]');
    if (!b) return;
    var theme = b.getAttribute('data-theme-choice');
    root.setAttribute('data-theme', theme);
    try { localStorage.setItem('ua-theme', theme); } catch (err) {}
    markActiveTheme();
    document.dispatchEvent(new CustomEvent('ua:themechange', { detail: theme }));
  });

  /* ---------- Account dropdown ---------- */
  function closeDropdowns(except) {
    document.querySelectorAll('[data-dropdown]').forEach(function (d) {
      if (d === except) return;
      var menu = d.querySelector('.dropdown');
      var btn = d.querySelector('[data-dropdown-toggle]');
      if (menu) menu.hidden = true;
      if (btn) btn.setAttribute('aria-expanded', 'false');
    });
  }
  document.addEventListener('click', function (e) {
    var toggle = e.target.closest('[data-dropdown-toggle]');
    if (toggle) {
      var wrap = toggle.closest('[data-dropdown]');
      var menu = wrap.querySelector('.dropdown');
      var open = menu.hidden;
      closeDropdowns(wrap);
      menu.hidden = !open;
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) { var first = menu.querySelector('a, button'); if (first) first.focus(); }
      return;
    }
    if (!e.target.closest('.dropdown')) closeDropdowns(null);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      closeDropdowns(null);
      document.body.classList.remove('menu-open');
    }
  });

  /* ---------- Mobile menu ---------- */
  document.addEventListener('click', function (e) {
    var m = e.target.closest('[data-menu-toggle]');
    if (m) {
      var open = !document.body.classList.contains('menu-open');
      document.body.classList.toggle('menu-open', open);
      m.setAttribute('aria-expanded', open ? 'true' : 'false');
    } else if (document.body.classList.contains('menu-open') && !e.target.closest('.sidebar')) {
      document.body.classList.remove('menu-open');
    }
  });

  /* ---------- Password fields ---------- */
  var LOWER = 'abcdefghijkmnopqrstuvwxyz', UPPER = 'ABCDEFGHJKLMNPQRSTUVWXYZ',
      DIGITS = '23456789', SYMBOLS = '!#$%&*?@+-=';
  function rand(n) {
    var a = new Uint32Array(1); window.crypto.getRandomValues(a); return a[0] % n;
  }
  function generate(len) {
    var groups = [LOWER, UPPER, DIGITS, SYMBOLS], all = groups.join(''), out = [];
    groups.forEach(function (g) { out.push(g[rand(g.length)]); });
    while (out.length < len) out.push(all[rand(all.length)]);
    for (var i = out.length - 1; i > 0; i--) { var j = rand(i + 1); var t = out[i]; out[i] = out[j]; out[j] = t; }
    return out.join('');
  }
  function score(pw) {
    if (!pw) return 0;
    var groups = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter(function (r) { return r.test(pw); }).length;
    if (pw.length < 10 || groups < 3) return 1;
    if (pw.length >= 14 && groups === 4) return 3;
    return 2;
  }
  var LABELS = ['', 'Too weak', 'Good', 'Strong'];
  document.querySelectorAll('[data-pw-field]').forEach(function (field) {
    var input = field.querySelector('[data-pw-input]');
    var bar = field.querySelector('[data-pw-bar]');
    var hint = field.querySelector('[data-pw-hint]');
    var original = hint ? hint.textContent : '';
    function update() {
      var s = score(input.value);
      field.setAttribute('data-strength', s);
      if (bar) bar.style.width = (s / 3 * 100) + '%';
      if (hint) hint.textContent = input.value ? LABELS[s] + '. ' + original : original;
    }
    input.addEventListener('input', update);
    var toggle = field.querySelector('[data-pw-toggle]');
    toggle && toggle.addEventListener('click', function () {
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      toggle.textContent = show ? 'Hide' : 'Show';
      toggle.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    });
    var gen = field.querySelector('[data-pw-generate]');
    gen && gen.addEventListener('click', function () {
      input.value = generate(14);
      input.type = 'text';
      var confirmBox = document.getElementById('confirm_password');
      if (confirmBox && input.id === 'new_password') { confirmBox.value = input.value; confirmBox.type = 'text'; }
      if (toggle) { toggle.textContent = 'Hide'; }
      update();
      input.focus(); input.select();
    });
    update();
  });

  /* ---------- Copy buttons ---------- */
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-copy]');
    if (!b) return;
    var text = b.getAttribute('data-copy');
    var done = function () { var old = b.textContent; b.textContent = 'Copied'; setTimeout(function () { b.textContent = old; }, 1600); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done);
    } else {
      var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta);
      ta.select(); try { document.execCommand('copy'); done(); } catch (err) {} ta.remove();
    }
  });

  /* ---------- Stat count-up: the dashboard's one orchestrated moment ---------- */
  function countUp() {
    if (reduceMotion) return;
    document.querySelectorAll('.stat .num').forEach(function (el, i) {
      var target = parseFloat(el.textContent);
      if (isNaN(target) || target <= 0 || /[^\d.]/.test(el.textContent.trim())) return;
      var decimals = (el.textContent.split('.')[1] || '').length;
      var start = null, dur = 900, delay = i * 70;
      el.textContent = (0).toFixed(decimals);
      function step(ts) {
        if (start === null) start = ts + delay;
        var p = Math.min(1, Math.max(0, (ts - start) / dur));
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = (target * eased).toFixed(decimals);
        if (p < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    markActiveTheme();
    countUp();
  });
})();

/* Auto-refresh (FR-12): dashboards check every minute whether anything they
   show has changed (a new alert, a recalculated risk score, a new note) and
   reload only then. Never while the person is typing in a form or the tab is
   hidden: then a small "New updates" bar offers a refresh instead. */
(function () {
  var body = document.body;
  if (!body || !body.dataset.autorefresh) return;
  var every = Math.max(20, parseInt(body.dataset.autorefresh, 10) || 60) * 1000;
  var token = body.dataset.refreshToken;
  var url = body.dataset.refreshUrl;
  var dirty = false;
  document.addEventListener('input', function (e) {
    if (e.target && e.target.closest && e.target.closest('form')) dirty = true;
  });

  function busy() {
    var a = document.activeElement;
    if (dirty) return true;
    return !!(a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  }

  function showBar() {
    if (document.getElementById('refresh-bar')) return;
    var bar = document.createElement('div');
    bar.id = 'refresh-bar';
    bar.className = 'refresh-bar';
    bar.setAttribute('role', 'status');
    bar.innerHTML = 'New updates are available. <button type="button" class="btn small">Refresh</button>';
    bar.querySelector('button').addEventListener('click', function () { location.reload(); });
    document.body.appendChild(bar);
  }

  function check() {
    if (document.visibilityState !== 'visible') return;
    fetch(url, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.token || data.token === token) return;
        if (busy()) { showBar(); } else { location.reload(); }
      })
      .catch(function () { /* offline or signed out: try again next time */ });
  }

  setInterval(check, every);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') check();
  });
})();
