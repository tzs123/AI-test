(function () {
  var modules = [
    ['API测试', '/testhub/legacy-api.html'],
    ['UI自动化', '/testhub/legacy-ui.html'],
    ['APP自动化', '/testhub/app-automation/dashboard'],
    ['AI用例生成', '/testhub/ai-generation/requirement-analysis'],

    ['性能看板', '/testhub/performance/dashboard'],
  ];

  function mount() {
    var logo = document.querySelector('.logo');
    if (!logo || document.querySelector('.restored-module-nav')) return;
    var nav = document.createElement('nav');
    nav.className = 'restored-module-nav';
    nav.setAttribute('aria-label', '测试模块');
    modules.forEach(function (item) {
      var link = document.createElement('a');
      link.href = item[1];
      link.textContent = item[0];
      link.className = 'restored-module-link';
      nav.appendChild(link);
    });
    var header = logo.closest('.el-header') || logo.parentNode;
    header.parentNode.insertBefore(nav, header.nextSibling);
  }

  var style = document.createElement('style');
  style.textContent = [
    '.restored-module-nav{position:fixed;z-index:1000;top:0;left:50%;transform:translateX(-50%);height:60px;display:flex;align-items:center;gap:6px;padding:0 16px;background:transparent}',
    '.restored-module-link{display:inline-flex;align-items:center;padding:7px 13px;border-radius:4px;color:#606266;font-size:14px;line-height:20px;text-decoration:none;white-space:nowrap}',
    '.restored-module-link:hover{color:#5267ff;background:#f0f2ff}',
    '.restored-module-link[href="'+window.location.pathname+'"]{color:#5267ff;background:#eef0ff;font-weight:600}',
    '.el-aside.is-collapsed .restored-module-nav{display:flex}',
    '@media(max-width:900px){.restored-module-nav{left:220px;transform:none;max-width:calc(100% - 220px);overflow-x:auto}.restored-module-link{font-size:12px;padding:6px 8px}}',
  ].join('');
  document.head.appendChild(style);

  new MutationObserver(mount).observe(document.documentElement, { childList: true, subtree: true });
  mount();
})();
