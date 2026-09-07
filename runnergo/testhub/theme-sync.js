/* RunnerGo theme sync — unified day/night theme across all testhub modules.
 * Source of truth: cookie `theme` written by the web-ui (API测试) module.
 * Fallback: localStorage.theme, then OS prefers-color-scheme.
 * Pages poll the cookie so theme switches in the web-ui propagate live
 * to the shell topbar and all testhub module iframes. */
(function () {
  var ATTR = 'data-theme';

  function resolveTheme() {
    var m = document.cookie.match(/(?:^|;\s*)theme=(dark|light)\b/);
    if (m) return m[1];
    var stored = localStorage.getItem('theme');
    if (stored === 'dark' || stored === 'light') return stored;
    try {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    } catch (e) {
      return 'light';
    }
  }

  function applyTheme() {
    var t = resolveTheme();
    if (document.documentElement.getAttribute(ATTR) !== t) {
      document.documentElement.setAttribute(ATTR, t);
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute('content', t === 'dark' ? '#171923' : '#5267ff');
    }
  }

  // Apply before first paint when loaded in <head>.
  applyTheme();

  // Poll for theme changes made inside the web-ui (cookie has no change event).
  window.setInterval(applyTheme, 800);
  // Re-check when the tab regains focus (cheap and covers most switch flows).
  window.addEventListener('focus', applyTheme);

  window.RunnerGoTheme = { resolve: resolveTheme, apply: applyTheme };
})();
