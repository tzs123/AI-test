(function () {
  'use strict';
  var VERSION = '1';

  function djangoToken() {
    try {
      var c = (document.cookie.match(/(?:^|;\s*)token=([^;]+)/) || [])[1] || '';
      var t = localStorage.getItem('access_token') || localStorage.getItem('token') ||
              sessionStorage.getItem('access_token') || sessionStorage.getItem('token') || c;
      return t;
    } catch (e) { return ''; }
  }

  function toast(msg, type) {
    var t = document.createElement('div');
    t.style.cssText = 'position:fixed;top:20px;right:20px;z-index:99999;padding:10px 20px;border-radius:6px;font-size:14px;color:#fff;background:' +
      (type === 'error' ? '#f56c6c' : type === 'success' ? '#67c23a' : '#409eff') + ';box-shadow:0 4px 12px rgba(0,0,0,.2);transition:opacity .3s;opacity:0';
    t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.style.opacity = '1'; });
    setTimeout(function () { t.style.opacity = '0'; setTimeout(function () { t.remove(); }, 300); }, 2600);
  }

  function exportExcel(knowledgeFileName) {
    var url = '/api/requirement-analysis/business-rules/export_excel/';
    if (knowledgeFileName) url += '?knowledge_file_name=' + encodeURIComponent(knowledgeFileName);
    var token = djangoToken();
    var headers = {};
    if (token) headers['Authorization'] = token.indexOf('Bearer ') === 0 ? token : 'Bearer ' + token;
    toast('正在导出...', 'info');
    fetch(url, { method: 'GET', headers: headers, credentials: 'include' })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.text().then(function (txt) {
            toast('导出失败: ' + resp.status + (txt ? ' ' + txt.slice(0, 100) : ''), 'error');
          });
        }
        var disp = resp.headers.get('Content-Disposition') || '';
        var m = disp.match(/filename="([^"]+)"/);
        var filename = m ? m[1] : 'business_rules.xlsx';
        return resp.blob().then(function (blob) {
          var a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = filename;
          a.click();
          setTimeout(function () { URL.revokeObjectURL(a.href); }, 200);
          toast('导出成功', 'success');
        });
      })
      .catch(function (err) { toast('导出失败: ' + err.message, 'error'); });
  }

  function isBusinessRulesPage() {
    return /business-rules|requirement-analysis/.test(window.location.pathname + window.location.hash);
  }

  function injectButtons() {
    if (!isBusinessRulesPage()) return;

    var rows = document.querySelectorAll('table tbody tr');
    rows.forEach(function (tr) {
      if (tr.dataset.rgExportInjected) return;
      var cells = tr.querySelectorAll('td');
      if (cells.length < 5) return;
      var nameCell = cells[1];
      var name = (nameCell.textContent || '').trim();
      if (!name) return;
      var actionCell = cells[cells.length - 1];
      if (!actionCell) return;
      var existingBtn = actionCell.querySelector('.rg-export-btn');
      if (existingBtn) return;
      var btn = document.createElement('button');
      btn.className = 'rg-export-btn';
      btn.textContent = '导出';
      btn.style.cssText = 'margin-left:8px;padding:4px 12px;border:1px solid #4A90E2;border-radius:4px;background:#4A90E2;color:#fff;font-size:13px;cursor:pointer;white-space:nowrap';
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        e.preventDefault();
        exportExcel(name);
      });
      actionCell.appendChild(btn);
      tr.dataset.rgExportInjected = '1';
    });

    var toolbarBtns = document.querySelectorAll('button');
    var hasExportAll = false;
    toolbarBtns.forEach(function (b) {
      if (b.classList.contains('rg-export-all-btn')) hasExportAll = true;
    });
    if (!hasExportAll) {
      var addBtn = null;
      toolbarBtns.forEach(function (b) {
        if (!addBtn && /新增知识文件|新建/.test(b.textContent)) addBtn = b;
      });
      if (addBtn && addBtn.parentNode) {
        var exportAllBtn = document.createElement('button');
        exportAllBtn.className = 'rg-export-all-btn';
        exportAllBtn.textContent = '导出Excel';
        exportAllBtn.style.cssText = 'margin-left:10px;padding:6px 16px;border:1px solid #67c23a;border-radius:4px;background:#67c23a;color:#fff;font-size:14px;cursor:pointer;white-space:nowrap';
        exportAllBtn.addEventListener('click', function (e) {
          e.stopPropagation();
          e.preventDefault();
          exportExcel(null);
        });
        addBtn.parentNode.insertBefore(exportAllBtn, addBtn.nextSibling);
      }
    }
  }

  function init() {
    var observer = new MutationObserver(function () {
      clearTimeout(init._t);
      init._t = setTimeout(injectButtons, 300);
    });
    observer.observe(document.body, { childList: true, subtree: true });
    setInterval(injectButtons, 2000);
    injectButtons();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();