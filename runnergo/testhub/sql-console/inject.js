/**
 * RunnerGo 新建SQL - DBeaver 风格多条 SQL 控制台（注入脚本）
 *
 * 由 web-ui/index.html 引入（宿主机挂载文件），运行在 legacy API 测试页面内。
 * 功能：
 *  - 拦截 SQL 节点的 执行 请求（/target/send_sql），改走 /api/sql-console/execute
 *  - 一次执行编辑器中的多条 SQL（分号切分，忽略字符串/注释中的分号）
 *  - 页面按语句返回多结果卡片（列名/数据/影响行数/耗时/错误）
 *  - 数据库连接来自当前环境的配置（切换顶部环境自动加载）
 *  - 隐藏顶部原生"断言/关联提取"tab 和底部"手动连接"按钮
 */
(function () {
  'use strict';
  if (window.__RG_SQL_CONSOLE__) return;
  window.__RG_SQL_CONSOLE__ = true;

  var VERSION = '38';

  /* ------------------------------ 工具函数 ------------------------------ */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function lsGet(k, d) { try { var v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } }
  function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { } }
  function fmtMs(ms) { return ms >= 1000 ? (ms / 1000).toFixed(2) + 's' : ms + 'ms'; }

  function toast(msg, type) {
    var t = document.createElement('div');
    t.className = 'rgsql-toast rgsql-toast-' + (type || 'info');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () { t.classList.add('rgsql-toast-show'); }, 10);
    setTimeout(function () {
      t.classList.remove('rgsql-toast-show');
      setTimeout(function () { t.remove(); }, 300);
    }, 2600);
  }

  /* --------------------------- Django API 调用 --------------------------- */

  function djangoToken() {
    try {
      return (document.cookie.match(/(?:^|;\s*)token=([^;]+)/) || [])[1] || '';
    } catch (e) { return ''; }
  }
  function teamId() { try { return sessionStorage.getItem('team_id') || '0'; } catch (e) { return '0'; } }

  // RunnerGo Docker 模式下 manage API 的 base URL。
  // 返回空字符串让请求走同域 nginx 代理（location ^~ /management/），
  // 避免直连 58889 端口时因 manage CORS 配置不兼容（Allow-Credentials:true + Allow-Origin:*）
  // 被浏览器拦截的问题。
  function manageBaseUrl() {
    return '';
  }

  function parseJsonSafe(text) {
    var i = 0, len = text.length;
    function skipWs() { while (i < len && (text[i] === ' ' || text[i] === '\n' || text[i] === '\r' || text[i] === '\t')) i++; }
    function parseValue() {
      skipWs();
      var ch = text[i];
      if (ch === '"') return parseString();
      if (ch === '{') return parseObject();
      if (ch === '[') return parseArray();
      if (ch === 't') { i += 4; return true; }
      if (ch === 'f') { i += 5; return false; }
      if (ch === 'n') { i += 4; return null; }
      return parseNumber();
    }
    function parseString() {
      var start = ++i, out = '';
      while (i < len && text[i] !== '"') {
        if (text[i] === '\\') {
          var esc = text[i + 1];
          if (esc === 'n') out += '\n';
          else if (esc === 't') out += '\t';
          else if (esc === 'r') out += '\r';
          else if (esc === '\\') out += '\\';
          else if (esc === '"') out += '"';
          else if (esc === '/') out += '/';
          else if (esc === 'u') { out += String.fromCharCode(parseInt(text.substr(i + 2, 4), 16)); i += 4; }
          else out += esc;
          i += 2;
        } else { out += text[i]; i++; }
      }
      i++;
      return out;
    }
    function parseNumber() {
      var start = i;
      if (text[i] === '-') i++;
      while (i < len && text[i] >= '0' && text[i] <= '9') i++;
      var isFloat = false;
      if (text[i] === '.') { isFloat = true; i++; while (i < len && text[i] >= '0' && text[i] <= '9') i++; }
      if (text[i] === 'e' || text[i] === 'E') { isFloat = true; i++; if (text[i] === '+' || text[i] === '-') i++; while (i < len && text[i] >= '0' && text[i] <= '9') i++; }
      var s = text.slice(start, i);
      if (!isFloat && s.replace('-', '').length >= 16) return s;
      return Number(s);
    }
    function parseObject() {
      var obj = {};
      i++; skipWs();
      if (text[i] === '}') { i++; return obj; }
      while (true) {
        skipWs();
        var key = parseString();
        skipWs(); i++;
        obj[key] = parseValue();
        skipWs();
        if (text[i] === ',') { i++; continue; }
        if (text[i] === '}') { i++; break; }
      }
      return obj;
    }
    function parseArray() {
      var arr = [];
      i++; skipWs();
      if (text[i] === ']') { i++; return arr; }
      while (true) {
        arr.push(parseValue());
        skipWs();
        if (text[i] === ',') { i++; continue; }
        if (text[i] === ']') { i++; break; }
      }
      return arr;
    }
    try { return parseValue(); } catch (e) { return JSON.parse(text); }
  }

  function callApi(path, body) {
    var headers = { 'Content-Type': 'application/json' };
    var t = djangoToken();
    if (t) headers['Authorization'] = 'Bearer ' + t;
    return fetch('/api/sql-console/' + path, {
      method: 'POST', headers: headers, credentials: 'include', body: JSON.stringify(body)
    }).then(function (r) {
      return r.text().then(function (txt) { return { http: r.status, json: parseJsonSafe(txt) }; });
    }).then(function (res) {
      if (res.http === 401 || res.http === 403) {
        // 第二认证路径：转发 manage 会话 Cookie 给后端校验
        headers['X-RG-Manage-Auth'] = '1';
        headers['X-RG-Team-Id'] = teamId();
        return fetch('/api/sql-console/' + path, {
          method: 'POST', headers: headers, credentials: 'include', body: JSON.stringify(body)
        }).then(function (r) { return r.text(); }).then(function (txt) { return { json: parseJsonSafe(txt) }; });
      }
      return { json: res.json };
    }).then(function (res) {
      var j = res.json || {};
      if (j.code !== 0) throw new Error(j.detail || '请求失败');
      return j.data || {};
    });
  }

  /* ------------------------------ SQL 切分 ------------------------------ */

  /** 按分号切分 SQL；忽略 '...' "..." `...`、-- 换行、# 换行、块注释 内的分号。 */
  function splitSql(text) {
    var out = [], buf = [], i = 0, n = text.length;
    var state = 'normal'; // normal | line | block | sq | dq | bt
    while (i < n) {
      var c = text[i], c2 = text.substr(i, 2);
      if (state === 'normal') {
        if (c2 === '--') { state = 'line'; buf.push(c2); i += 2; continue; }
        if (c2 === '/*') { state = 'block'; buf.push(c2); i += 2; continue; }
        if (c === "'") { state = 'sq'; buf.push(c); i++; continue; }
        if (c === '"') { state = 'dq'; buf.push(c); i++; continue; }
        if (c === '`') { state = 'bt'; buf.push(c); i++; continue; }
        if (c === ';') {
          var s = buf.join('').trim();
          if (s) out.push(s);
          buf = []; i++; continue;
        }
        buf.push(c); i++; continue;
      }
      if (state === 'line') {
        if (c === '\n') state = 'normal';
        buf.push(c); i++; continue;
      }
      if (state === 'block') {
        if (c2 === '*/') { state = 'normal'; buf.push(c2); i += 2; continue; }
        buf.push(c); i++; continue;
      }
      // 字符串状态
      if (state === 'sq' && c === '\\' && i + 1 < n) { buf.push(c2); i += 2; continue; }
      if (state === 'sq' && c === "'") {
        if (text[i + 1] === "'") { buf.push("''"); i += 2; continue; } // 转义 ''
        state = 'normal'; buf.push(c); i++; continue;
      }
      if (state === 'dq' && c === '\\' && i + 1 < n) { buf.push(c2); i += 2; continue; }
      if (state === 'dq' && c === '"') {
        if (text[i + 1] === '"') { buf.push('""'); i += 2; continue; }
        state = 'normal'; buf.push(c); i++; continue;
      }
      if (state === 'bt' && c === '`') { state = 'normal'; buf.push(c); i++; continue; }
      buf.push(c); i++;
    }
    var tail = buf.join('').trim();
    if (tail) out.push(tail);
    return out;
  }

  function preview(s, max) {
    s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    max = max || 60;
    return s.length > max ? s.slice(0, max) + '…' : s;
  }

  /* -------------------------------- 状态 -------------------------------- */

  var state = {
    dbs: [],               // get_sql_database_list 返回的连接列表（mysql）
    envId: null,           // 最近一次应用请求携带的 env_id
    targetId: null,        // 当前 SQL 节点 id
    targetName: '',
    targetInfo: null,      // 最近一次 target/detail 缓存
    rowLimit: lsGet('rgsql.rowlimit', 200),
    stopOnError: lsGet('rgsql.stopOnError', true),
    selectedDb: '',        // 按 targetId 持久化
    results: null,         // 最近一次执行结果
    running: false,
    // 表结构浏览
    schema: { tables: [], loading: false, selectedTable: '', expanded: {} },
    // 断言 & 关联提取 规则（按 targetId 持久化）
    assertions: lsGet('rgsql.assertions', {}),   // {"0": [{type,op,value,name,row,col}]}
    extractions: lsGet('rgsql.extractions', {}), // {"0": [{name,type,row,col}]}
    activeTab: lsGet('rgsql.activeTab', 'results') // results | assertions | extractions
  };

  /* ------------------------------ 样式注入 ------------------------------ */

  var CSS = [
    '.rgsql-console{color:#d6d9de;font-size:13px;background:#17181b;border:1px solid #2c2f34;border-radius:8px;',
    'margin:8px 12px 12px;display:flex;flex-direction:column;max-height:52%;min-height:220px;box-shadow:0 2px 10px rgba(0,0,0,.35)}',
    '.rgsql-head{display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid #2c2f34;flex-wrap:wrap;background:#1d1f23;border-radius:8px 8px 0 0}',
    '.rgsql-head .rgsql-title{font-weight:600;color:#9fd0ff;margin-right:2px;white-space:nowrap}',
    '.rgsql-target{color:#8b909a;font-size:12px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.rgsql-select,.rgsql-input{background:#24262b;border:1px solid #3a3e45;color:#d6d9de;border-radius:6px;padding:4px 8px;font-size:12px;outline:none;max-width:190px}',
    '.rgsql-select:focus,.rgsql-input:focus{border-color:#5a8dd6}',
    '.rgsql-btn{border:1px solid #3a3e45;background:#26282e;color:#d6d9de;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px;white-space:nowrap}',
    '.rgsql-btn:hover{border-color:#5a8dd6;color:#fff}',
    '.rgsql-btn-primary{background:#2563eb;border-color:#2563eb;color:#fff}',
    '.rgsql-btn-primary:hover{background:#1d4ed8;border-color:#1d4ed8}',
    '.rgsql-btn:disabled{opacity:.5;cursor:not-allowed}',
    '.rgsql-switch{display:inline-flex;align-items:center;gap:5px;cursor:pointer;font-size:12px;color:#a6abb5;user-select:none}',
    '.rgsql-switch .dot{width:26px;height:14px;border-radius:8px;background:#4a4e56;position:relative;transition:background .2s}',
    '.rgsql-switch .dot::after{content:"";position:absolute;top:2px;left:2px;width:10px;height:10px;border-radius:50%;background:#cfd3da;transition:left .2s}',
    '.rgsql-switch.on .dot{background:#2563eb}.rgsql-switch.on .dot::after{left:14px;background:#fff}',
    '.rgsql-body{flex:1;overflow:auto;padding:10px}',
    '.rgsql-empty{color:#7d828c;padding:18px 8px;text-align:center;line-height:1.8}',
    '.rgsql-empty kbd{background:#26282e;border:1px solid #3a3e45;border-radius:4px;padding:0 5px;font-size:11px}',
    '.rgsql-card{border:1px solid #2c2f34;border-radius:8px;margin-bottom:10px;background:#1d1f23}',
    '.rgsql-card-h{display:flex;align-items:center;gap:8px;padding:6px 10px;border-bottom:1px solid #2c2f34;flex-wrap:wrap}',
    '.rgsql-idx{background:#2b3340;color:#9fd0ff;border-radius:4px;padding:1px 7px;font-size:12px;font-weight:600}',
    '.rgsql-pill{border-radius:10px;padding:1px 9px;font-size:11px}',
    '.rgsql-pill.ok{background:rgba(34,197,94,.15);color:#4ade80}',
    '.rgsql-pill.err{background:rgba(239,68,68,.15);color:#f87171}',
    '.rgsql-pill.skip{background:rgba(148,163,184,.15);color:#94a3b8}',
    '.rgsql-pill.run{background:rgba(59,130,246,.15);color:#60a5fa}',
    '.rgsql-meta{color:#8b909a;font-size:12px}',
    '.rgsql-card-h .rgsql-space{flex:1}',
    '.rgsql-sql{font-family:Menlo,Consolas,monospace;font-size:12px;color:#b7bcc4;background:#17181b;border-radius:6px;',
    'margin:6px 10px 2px;padding:6px 9px;white-space:pre-wrap;word-break:break-all;max-height:96px;overflow:auto}',
    '.rgsql-sql .rgsql-sub{color:#7cc4ff}',
    '.rgsql-grid-wrap{overflow:auto;margin:6px 10px;border:1px solid #2c2f34;border-radius:6px;max-height:320px}',
    '.rgsql-grid{border-collapse:separate;border-spacing:0;width:100%;font-size:12px}',
    '.rgsql-grid th{position:sticky;top:0;background:#26282e;color:#cfd3da;text-align:left;padding:5px 9px;border-bottom:1px solid #3a3e45;white-space:nowrap;z-index:1}',
    '.rgsql-grid td{padding:4px 9px;border-bottom:1px solid #26282e;font-family:Menlo,Consolas,monospace;white-space:nowrap;max-width:420px;overflow:hidden;text-overflow:ellipsis}',
    '.rgsql-grid tr:hover td{background:#22242a}',
    '.rgsql-grid td.null{color:#5a6070;font-style:italic}',
    '.rgsql-err{margin:6px 10px;padding:7px 10px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.35);color:#f87171;border-radius:6px;font-family:Menlo,Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-all}',
    '.rgsql-info{margin:6px 10px;color:#8b909a;font-size:12px}',
    '.rgsql-footer{color:#7d828c;font-size:11px;padding:4px 10px 8px}',
    '.rgsql-loading{display:inline-block;width:14px;height:14px;border:2px solid #3a3e45;border-top-color:#60a5fa;border-radius:50%;animation:rgsqlspin .8s linear infinite;vertical-align:-2px}',
    '@keyframes rgsqlspin{to{transform:rotate(360deg)}}',
    '.rgsql-toast{position:fixed;top:18px;left:50%;transform:translateX(-50%) translateY(-8px);z-index:100000;',
    'background:#26282e;color:#e7e9ec;border:1px solid #3a3e45;border-radius:8px;padding:8px 16px;font-size:13px;opacity:0;transition:all .25s;box-shadow:0 4px 16px rgba(0,0,0,.4)}',
    '.rgsql-toast-show{opacity:1;transform:translateX(-50%) translateY(0)}',
    '.rgsql-toast-error{border-color:rgba(239,68,68,.5);color:#fca5a5}',
    '.rgsql-toast-success{border-color:rgba(34,197,94,.5);color:#86efac}',
    /* ===== v35 极简布局：砍掉所有试验性选择器，只保留 6 条核心规则 ===== */
    /* 1. 隐藏 request 里的断言/关联提取/数据库 tab（只保留 编辑器SQL） */
    '.rgsql-root-multi .sql-manage-detail-request .arco-tabs-header > .arco-tabs-header-title:nth-child(2),.rgsql-root-multi .sql-manage-detail-request .arco-tabs-header > .arco-tabs-header-title:nth-child(3),.rgsql-root-multi .sql-manage-detail-request .arco-tabs-header > .arco-tabs-header-title:nth-child(4){display:none !important}',
    /* 2-3. 隐藏 class 含 response 的元素 */
    '.rgsql-root-multi [class*="response"]{display:none !important;height:0 !important;min-height:0 !important;flex:none !important;overflow:hidden !important}',
    '.rgsql-root-multi [class*="Response"]{display:none !important;height:0 !important;min-height:0 !important;flex:none !important;overflow:hidden !important}',
    /* 4. 我们的控制台充满 flex 空间 */
    '.rgsql-root-multi .rgsql-console{flex:1 1 auto !important;min-height:200px !important;margin:0 !important;overflow:hidden !important;max-height:none !important}',
    /* 5. body 内部布局 */
    '.rgsql-root-multi .rgsql-body{flex:1 1 auto !important;display:flex !important;flex-direction:column !important;min-height:0 !important}',
    '.rgsql-root-multi .rgsql-tab-panel{flex:1 1 auto;overflow:auto}',
    '.rgsql-splitbar{width:5px;background:transparent;cursor:col-resize}',
    '.rgsql-splitbar:hover{background:rgba(96,165,250,.4)}',
    '.rgsql-splitbar.dragging{background:rgba(96,165,250,.4);user-select:none !important;cursor:col-resize !important}',
    '.rgsql-root-multi .rgsql-console{margin:0 !important;overflow:auto !important}',
    '.rgsql-root-multi .rgsql-body{flex:1 1 auto !important;display:flex !important;flex-direction:column !important;min-height:0 !important}',
    '.rgsql-tab-panel{flex:1 1 auto;overflow:auto}',
    '.rgsql-schema-head{padding:8px 10px;border-bottom:1px solid #2c2f34;display:flex;align-items:center;gap:6px}',
    '.rgsql-schema-title{font-size:12px;color:#9fd0ff;font-weight:600}',
    '.rgsql-schema-reload{background:none;border:none;color:#8b909a;cursor:pointer;font-size:14px;padding:0 4px}',
    '.rgsql-schema-reload:hover{color:#fff}',
    '.rgsql-schema-list{flex:1;overflow:auto;padding:6px 0;font-size:12px}',
    '.rgsql-schema-empty{color:#5a6070;padding:16px 10px;text-align:center;font-size:11px;line-height:1.6}',
    '.rgsql-schema-empty button{margin-top:6px;background:#26282e;border:1px solid #3a3e45;color:#d6d9de;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:11px}',
    '.rgsql-schema-loading{color:#60a5fa;padding:12px;text-align:center;font-size:11px}',
    '.rgsql-schema-group{color:#8b909a;font-size:11px;padding:4px 10px;cursor:pointer;user-select:none;display:flex;align-items:center;gap:4px}',
    '.rgsql-schema-group:hover{background:rgba(255,255,255,.05)}',
    '.rgsql-schema-group .caret{font-size:8px;width:10px;text-align:center;transition:transform .15s}',
    '.rgsql-schema-group.collapsed .caret{transform:rotate(-90deg)}',
    '.rgsql-schema-group.collapsed + .rgsql-schema-items{display:none}',
    '.rgsql-schema-item{padding:3px 10px 3px 22px;cursor:pointer;color:#cfd3da;display:flex;align-items:center;gap:5px;user-select:none}',
    '.rgsql-schema-item:hover{background:rgba(255,255,255,.06);color:#fff}',
    '.rgsql-schema-item.table::before{content:"🗄";font-size:10px}',
    '.rgsql-schema-item.col{padding-left:36px;font-size:11px;color:#8b909a}',
    '.rgsql-schema-item.col::before{content:"📋";font-size:9px}',
    '.rgsql-schema-item.col.pk{color:#fbbf24}',
    '.rgsql-schema-item.col.pk::before{content:"🔑"}',
    '.rgsql-schema-item.col.type{font-size:10px;color:#5a6070;margin-left:auto}',
    '.rgsql-schema-item.active{background:rgba(96,165,250,.15);color:#60a5fa}',
    '.rgsql-schema-item.active.table::before{content:"📂"}',
    /* ===== body tabs (结果/断言/关联提取) ===== */
    '.rgsql-body-tabs{display:flex;gap:0;border-bottom:1px solid #2c2f34;padding:0 10px;background:#1a1b1e}',
    '.rgsql-body-tab{padding:6px 14px;font-size:12px;color:#8b909a;cursor:pointer;border-bottom:2px solid transparent;user-select:none}',
    '.rgsql-body-tab:hover{color:#cfd3da}',
    '.rgsql-body-tab.active{color:#60a5fa;border-bottom-color:#60a5fa}',
    '.rgsql-tab-panel{display:none;flex:1;overflow:auto}',
    '.rgsql-tab-panel.active{display:block}',
    /* ===== 断言 & 提取 表格编辑 ===== */
    '.rgsql-rule-toolbar{display:flex;gap:6px;align-items:center;padding:8px 10px;border-bottom:1px solid #2c2f34;background:#1a1b1e}',
    '.rgsql-rule-toolbar .rgsql-btn{padding:3px 10px;font-size:11px}',
    '.rgsql-rule-table{width:100%;border-collapse:separate;border-spacing:0;font-size:12px}',
    '.rgsql-rule-table th{background:#26282e;color:#cfd3da;padding:5px 8px;text-align:left;font-weight:500;border-bottom:1px solid #3a3e45;white-space:nowrap}',
    '.rgsql-rule-table td{padding:4px 6px;border-bottom:1px solid #26282e;vertical-align:middle}',
    '.rgsql-rule-table td input,.rgsql-rule-table td select{background:#1a1b1e;border:1px solid #3a3e45;color:#d6d9de;border-radius:4px;padding:3px 6px;font-size:11px;width:100%;outline:none}',
    '.rgsql-rule-table td input:focus,.rgsql-rule-table td select:focus{border-color:#5a8dd6}',
    '.rgsql-rule-table td .rgsql-del{color:#f87171;cursor:pointer;padding:2px 6px;font-size:11px;border:1px solid rgba(248,113,113,.3);background:rgba(248,113,113,.1);border-radius:4px}',
    '.rgsql-rule-table td .rgsql-del:hover{background:rgba(248,113,113,.2)}',
    '.rgsql-rule-empty{color:#5a6070;padding:16px;text-align:center;font-size:11px}',
    '.rgsql-rule-row-passed td{background:rgba(34,197,94,.06)}',
    '.rgsql-rule-row-failed td{background:rgba(239,68,68,.08)}',
    '.rgsql-rule-pass{color:#4ade80;font-size:11px}',
    '.rgsql-rule-fail{color:#f87171;font-size:11px}',
    /* 结果卡片里的断言/提取展示 */
    '.rgsql-card-assertions{margin:4px 10px}',
    '.rgsql-card-assertion{display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px;border-bottom:1px dashed #2c2f34}',
    '.rgsql-card-assertion:last-child{border-bottom:none}',
    '.rgsql-card-assertion .rgsql-pass-ic{width:14px;height:14px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px}',
    '.rgsql-card-assertion .rgsql-pass-ic.ok{background:rgba(34,197,94,.2);color:#4ade80}',
    '.rgsql-card-assertion .rgsql-pass-ic.err{background:rgba(239,68,68,.2);color:#f87171}',
    '.rgsql-card-extractions{margin:4px 10px;display:flex;flex-wrap:wrap;gap:4px}',
    '.rgsql-card-ext{background:rgba(96,165,250,.15);border:1px solid rgba(96,165,250,.3);color:#9fd0ff;border-radius:4px;padding:2px 7px;font-size:11px;font-family:Menlo,Consolas,monospace}',
    /* ===== 语句勾选面板 ===== */
    '.rgsql-picker{padding:8px 10px}',
    '.rgsql-picker-h{font-size:13px;color:#9fd0ff;font-weight:600;margin-bottom:8px}',
    '.rgsql-picker-list{display:flex;flex-direction:column;gap:6px;margin-bottom:10px}',
    '.rgsql-picker-item{display:flex;align-items:flex-start;gap:8px;padding:8px 10px;background:#1d1f23;border:1px solid #2c2f34;border-radius:6px;cursor:pointer;user-select:none}',
    '.rgsql-picker-item:hover{border-color:#3a3e45;background:#22242a}',
    '.rgsql-picker-item input[type=checkbox]{margin-top:2px;accent-color:#2563eb;cursor:pointer}',
    '.rgsql-pick-idx{background:#2b3340;color:#9fd0ff;border-radius:4px;padding:1px 7px;font-size:12px;font-weight:600;flex-shrink:0}',
    '.rgsql-pick-sql{font-family:Menlo,Consolas,monospace;font-size:12px;color:#b7bcc4;white-space:pre-wrap;word-break:break-all;flex:1;max-height:96px;overflow:auto}',
    '.rgsql-picker-bar{display:flex;align-items:center;gap:8px;padding-top:6px;border-top:1px solid #2c2f34}',
    /* ===== 可编辑结果表格 ===== */
    '.rgsql-grid-toolbar{display:flex;align-items:center;gap:6px;padding:6px 8px;margin-bottom:4px;background:#1a1b1e;border:1px solid #2c2f34;border-radius:6px;flex-wrap:wrap}',
    '.rgsql-grid-tbl{font-size:11px;color:#8b909a}',
    '.rgsql-grid-tbl b{color:#9fd0ff}',
    '.rgsql-grid-toolbar .rgsql-btn{padding:3px 10px;font-size:11px}',
    '.rgsql-grid-edit th.rgsql-grid-act,.rgsql-grid-edit td.rgsql-grid-act{width:28px;min-width:28px;text-align:center;padding:4px 0;position:sticky;left:0;z-index:2;background:#1d1f23}',
    '.rgsql-grid-edit th.pk{color:#fbbf24}',
    '.rgsql-row-del{color:#f87171;cursor:pointer;font-size:12px;padding:2px 4px;border-radius:3px}',
    '.rgsql-row-del:hover{background:rgba(248,113,113,.2)}',
    '.rgsql-row-added td{background:rgba(34,197,94,.08) !important}',
    '.rgsql-row-modified td{background:rgba(251,191,36,.08) !important}',
    '.rgsql-grid-edit td[data-row][data-col]{cursor:text}',
    '.rgsql-grid-edit td[data-row][data-col]:hover{background:rgba(96,165,250,.1)}',
    '.rgsql-cell-input{width:100%;min-width:60px;background:#26282e;border:1px solid #5a8dd6;color:#fff;border-radius:3px;padding:2px 6px;font-size:12px;font-family:Menlo,Consolas,monospace;outline:none;box-sizing:border-box}',
  ].join('\n');

  function injectStyle() {
    var st = document.createElement('style');
    st.id = 'rgsql-console-style';
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  /* ---------------------------- Monaco 读取 ---------------------------- */

  var knownEditors = []; // onDidCreateEditor 捕获的编辑器实例
  function watchMonaco() {
    try {
      var mon = window.monaco;
      if (mon && mon.editor && mon.editor.onDidCreateEditor) {
        mon.editor.onDidCreateEditor(function (ed) { knownEditors.push(ed); });
      }
    } catch (e) { }
  }

  function pickEditorInSqlRoot() {
    var mon = window.monaco;
    if (!mon || !mon.editor) return null;
    var root = sqlRoot();
    var candidates = knownEditors.slice();
    try {
      var all = mon.editor.getEditors ? mon.editor.getEditors() : [];
      for (var j = 0; j < all.length; j++) {
        if (candidates.indexOf(all[j]) < 0) candidates.push(all[j]);
      }
    } catch (e) { }
    for (var i = candidates.length - 1; i >= 0; i--) {
      var ed = candidates[i];
      var dead = false;
      try { dead = !ed || !ed.getModel || !ed.getModel(); } catch (e) { dead = true; }
      if (dead) {
        var ki = knownEditors.indexOf(ed);
        if (ki >= 0) knownEditors.splice(ki, 1);
        continue;
      }
      var dom = null;
      try { dom = ed.getDomNode(); } catch (e) { }
      if (dom && dom.offsetParent !== null && (!root || root.contains(dom))) return ed;
    }
    return null;
  }

  function pickAttachedModel() {
    try {
      var models = window.monaco.editor.getModels() || [];
      var attached = models.filter(function (m) {
        return m.isAttachedToEditor && m.isAttachedToEditor();
      });
      if (!attached.length) return null;
      if (attached.length === 1) return attached[0];
      // 多个挂载模型时优先 SQL 样式内容
      var sqlLike = attached.filter(function (m) {
        return /\b(select|insert|update|delete|create|drop|alter|show|use|truncate)\b/i.test(m.getValue() || '');
      });
      return sqlLike[0] || attached[0];
    } catch (e) { return null; }
  }

  function currentSqlText() {
    var ed = pickEditorInSqlRoot();
    if (ed) {
      try { return { text: ed.getValue(), fromEditor: true, ed: ed }; } catch (e) { }
    }
    var model = pickAttachedModel();
    if (model) {
      try { return { text: model.getValue(), fromEditor: true, ed: null }; } catch (e) { }
    }
    var saved = state.targetInfo && state.targetInfo.sql_detail && state.targetInfo.sql_detail.sql_string;
    return { text: saved || '', fromEditor: false, ed: null };
  }

  /* ------------------------------ XHR 钩子 ------------------------------ */

  function fakeXhrResponse(xhr, text) {
    try {
      Object.defineProperty(xhr, 'readyState', { value: 4, configurable: true });
      Object.defineProperty(xhr, 'status', { value: 200, configurable: true });
      Object.defineProperty(xhr, 'statusText', { value: 'OK', configurable: true });
      Object.defineProperty(xhr, 'responseText', { value: text, configurable: true });
      Object.defineProperty(xhr, 'response', { value: text, configurable: true });
      Object.defineProperty(xhr, 'responseURL', { value: xhr.__rgUrl || '', configurable: true });
      Object.defineProperty(xhr, 'getAllResponseHeaders', {
        value: function () { return 'content-type: application/json\r\n'; }, configurable: true
      });
      Object.defineProperty(xhr, 'getResponseHeader', {
        value: function (name) { return /^content-type$/i.test(String(name)) ? 'application/json' : null; },
        configurable: true
      });
    } catch (e) { }
    setTimeout(function () {
      try { xhr.dispatchEvent(new Event('readystatechange')); } catch (e) { }
      try { xhr.dispatchEvent(new ProgressEvent('load')); } catch (e) { }
      try { xhr.dispatchEvent(new ProgressEvent('loadend')); } catch (e) { }
    }, 0);
  }

  function parseBody(body) {
    if (body == null) return {};
    if (typeof body === 'string') { try { return JSON.parse(body); } catch (e) { return {}; } }
    return {};
  }

  function hookXhr() {
    var XHR = XMLHttpRequest.prototype;
    var origOpen = XHR.open, origSend = XHR.send;

    XHR.open = function (method, url) {
      this.__rgMethod = (method || '').toUpperCase();
      this.__rgUrl = url || '';
      return origOpen.apply(this, arguments);
    };

    XHR.send = function (body) {
      var xhr = this;
      var url = xhr.__rgUrl || '';
      var method = xhr.__rgMethod || 'GET';

      // 缓存应用自身请求的数据库列表（含连接信息），供控制台下拉框使用
      if (/target\/get_sql_database_list/.test(url)) {
        var reqBody = parseBody(body);
        if (reqBody.env_id != null) state.envId = reqBody.env_id;
        xhr.addEventListener('load', function () {
          try {
            var j = JSON.parse(xhr.responseText);
            if (j && j.code === 0 && Array.isArray(j.data)) {
              state.dbs = j.data.filter(function (d) { return (d.type || 'mysql') === 'mysql'; });
              // 确保面板存在再渲染
              ensurePanel();
              renderHead();
            } else if (window.RGSqlConsole && window.RGSqlConsole._debug) {
              console.log('[RGSQL] get_sql_database_list response:', j);
            }
          } catch (e) { }
        });
      }

      // 缓存 target/detail（sql_detail、env_info、target 名称）
      if (/target\/detail/.test(url) && method === 'GET') {
        xhr.addEventListener('load', function () {
          try {
            var j = JSON.parse(xhr.responseText);
            if (j && j.code === 0 && j.data) {
              state.targetInfo = j.data;
              state.targetId = j.data.target_id || state.targetId;
              state.targetName = j.data.name || state.targetName;
              renderHead();
            }
          } catch (e) { }
        });
      }

      // 拦截 执行：走 SQL 控制台，伪造成功响应阻止原生单条执行
      if (/target\/send_sql/.test(url) && method === 'POST' && sqlRoot()) {
        var req = parseBody(body);
        var tid = req.target_id;
        if (tid) { state.targetId = tid; }
        fakeXhrResponse(xhr, JSON.stringify({ code: 0, data: {} }));
        setTimeout(function () { runExecute(false); }, 30);
        return; // 请求不发给服务器
      }

      return origSend.apply(this, arguments);
    };
  }

  /* --------------------------- 彻底干掉 RunnerGo 原生 response --------------------------- */

  function _hideRunnerGoResponse(midDiv) {
    if (!midDiv) return;

    // ===== 0. 找到 midDiv 的 3 个关键子元素（debug 证明真实结构）=====
    var editorWrap = midDiv.querySelector(':scope > .apipost-scale.vertical');
    var consoleEl = midDiv.querySelector(':scope > .rgsql-console');
    // 还可能有其他未知 wrapper（response-wrapper 等）

    // ===== 1. 把 midDiv 变成 flex column 容器（inline style 优先级最高）=====
    midDiv.style.setProperty('display', 'flex', 'important');
    midDiv.style.setProperty('flex-direction', 'column', 'important');
    midDiv.style.setProperty('flex', '1 1 auto', 'important');
    midDiv.style.setProperty('min-height', '0', 'important');
    midDiv.style.setProperty('height', 'auto', 'important');
    midDiv.style.setProperty('max-height', 'none', 'important');

    // ===== 2. 编辑器 wrapper (.apipost-scale.vertical) → flex-shrink:0 保持高度 =====
    if (editorWrap) {
      editorWrap.style.setProperty('flex', '0 1 260px', 'important');
      editorWrap.style.setProperty('min-height', '160px', 'important');
      editorWrap.style.setProperty('height', 'auto', 'important');
      editorWrap.style.setProperty('overflow', 'hidden', 'important');
    }

    // ===== 3. 我们的控制台 → flex:1 1 auto 填满剩余 =====
    if (consoleEl) {
      consoleEl.style.setProperty('flex', '1 1 auto', 'important');
      consoleEl.style.setProperty('min-height', '200px', 'important');
      consoleEl.style.setProperty('overflow', 'hidden', 'important');
    }

    // ===== 4. 遍历 midDiv 直接子元素，干掉所有"不该存在"的 =====
    // debug 证明：midDiv 应该只有 editorWrap + consoleEl 两个 child
    var allKids = Array.prototype.slice.call(midDiv.children);
    allKids.forEach(function (kid) {
      var cls = (kid.className || '') + '';
      // 保留编辑器 wrapper
      if (cls.indexOf('apipost-scale') >= 0) return;
      // 保留我们的控制台
      if (cls.indexOf('rgsql-console') >= 0) return;
      // 其他一律干掉（response-wrapper、空 class wrapper 等）
      _hideElement(kid);
    });

    // ===== 5. 隐藏 request 里的断言/关联提取/数据库 tab =====
    midDiv.querySelectorAll('.sql-manage-detail-request .arco-tabs-header > .arco-tabs-header-title').forEach(function (tab, i) {
      if (i >= 1) tab.style.setProperty('display', 'none', 'important');
    });
  }

  function _hideElement(el) {
    el.style.setProperty('display', 'none', 'important');
    el.style.setProperty('height', '0', 'important');
    el.style.setProperty('min-height', '0', 'important');
    el.style.setProperty('max-height', '0', 'important');
    el.style.setProperty('flex', 'none', 'important');
    el.style.setProperty('margin', '0', 'important');
    el.style.setProperty('padding', '0', 'important');
    el.style.setProperty('overflow', 'hidden', 'important');
    el.style.setProperty('visibility', 'hidden', 'important');
    el.style.setProperty('position', 'absolute', 'important');
    el.style.setProperty('width', '0', 'important');
  }

  /* ======= Debug 工具：查看真实 DOM 结构 =======
     在浏览器控制台执行：RGSqlConsole.debugDom()
     它会把 midDiv 的完整 DOM 结构打印出来，帮我们精准定位红框元素
  */
  function _debugDom() {
    var root = document.querySelector('.sql-manage-detail');
    if (!root) { console.log('[RGSQL] 没找到 .sql-manage-detail'); return; }
    console.log('========== root (.sql-manage-detail) children ==========');
    Array.prototype.slice.call(root.children).forEach(function (k, i) {
      var cls = k.className || '(no class)';
      var rect = k.getBoundingClientRect();
      var tag = k.tagName;
      console.log('root.child#' + i, tag + '.' + cls, 'size=' + Math.round(rect.width) + 'x' + Math.round(rect.height));
    });
    // 找到 midDiv（root 的空 class wrapper）
    var midDiv = null;
    Array.prototype.slice.call(root.children).forEach(function (k) {
      var cls = k.className || '';
      if (cls.indexOf('sql-manage-detail-header') < 0 && cls.indexOf('rgsql-') < 0 && !midDiv) {
        midDiv = k;
      }
    });
    if (midDiv) {
      console.log('========== midDiv (空class wrapper) children ==========');
      Array.prototype.slice.call(midDiv.children).forEach(function (k, i) {
        var cls = k.className || '(no class)';
        var rect = k.getBoundingClientRect();
        var tag = k.tagName;
        var cs = getComputedStyle(k);
        console.log('midDiv.child#' + i, tag + '.' + cls,
          'size=' + Math.round(rect.width) + 'x' + Math.round(rect.height),
          'display=' + cs.display, 'visibility=' + cs.visibility);
      });
      // 找 request
      var req = midDiv.querySelector(':scope > .sql-manage-detail-request');
      if (req) {
        console.log('========== request.nextElementSibling 链 ==========');
        var sib = req.nextElementSibling;
        var si = 0;
        while (sib && si < 10) {
          var scls = sib.className || '(no class)';
          var srect = sib.getBoundingClientRect();
          var scs = getComputedStyle(sib);
          console.log('  sib#' + si, sib.tagName + '.' + scls,
            'size=' + Math.round(srect.width) + 'x' + Math.round(srect.height),
            'display=' + scs.display, 'visibility=' + scs.visibility,
            'hasRgsqlConsole=' + (sib.className || '').indexOf('rgsql-console'));
          sib = sib.nextElementSibling;
          si++;
        }
      }
    }
    console.log('========== 所有带 response 的元素 ==========');
    var allResp = root.querySelectorAll('[class*="response"], [class*="Response"]');
    allResp.forEach(function (el) {
      var r = el.getBoundingClientRect();
      var cs = getComputedStyle(el);
      console.log(el.className, 'size=' + Math.round(r.width) + 'x' + Math.round(r.height),
        'display=' + cs.display, 'visibility=' + cs.visibility);
    });
  }

  /* --------------------------- 页面挂载/定位 --------------------------- */

  function sqlRoot() {
    var roots = $all('.sql-manage-detail');
    for (var i = 0; i < roots.length; i++) {
      if (roots[i].offsetParent !== null) return roots[i];
    }
    return null;
  }

  function ensurePanel() {
    var root = sqlRoot();
    if (!root) return null;
    root.classList.add('rgsql-root-multi');

    // ====== 版本变化才彻底清理 v10-v19 的垃圾（有 rightCol 的版本会把原生 DOM 搬走）======
    if (root.dataset.rgsqlVersion !== VERSION) {
      // 1. 如果存在 rightCol 或 bottom-row，把里面的原生元素还回 root
      var rightCol = root.querySelector(':scope > .rgsql-right-col');
      var bottomRow = root.querySelector(':scope > .rgsql-bottom-row');
      [rightCol, bottomRow].forEach(function (w) {
        if (!w) return;
        Array.prototype.slice.call(w.children).forEach(function (c) {
          var cls = c.className || '';
          if (cls.indexOf('rgsql-') < 0) root.appendChild(c);
        });
        w.remove();
      });
      // 2. 移除所有 rgsql- 注入元素（包括 midDiv 里的 panel）
      root.querySelectorAll('.rgsql-schema-bar, .rgsql-splitbar, .rgsql-col-wrap, .rgsql-console').forEach(function (e) { e.remove(); });
      // 3. 重置 root 和 midDiv 的 inline style
      root.style.removeProperty('display');
      root.style.removeProperty('flex-direction');
      root.style.removeProperty('min-height');
      root.style.removeProperty('min-width');
      root.style.removeProperty('gap');
      root.style.removeProperty('position');
      // 找到 midDiv 并清理它的 inline style
      var tmpMid = null;
      Array.prototype.slice.call(root.children).forEach(function (k) {
        var cls = k.className || '';
        if (cls.indexOf('sql-manage-detail-header') < 0 && cls.indexOf('rgsql-') < 0 && !tmpMid) {
          tmpMid = k;
        }
      });
      if (tmpMid) {
        tmpMid.style.removeProperty('display');
        tmpMid.style.removeProperty('flex-direction');
        tmpMid.style.removeProperty('min-height');
        delete tmpMid.dataset.rgsqlFlexForced;
      }
      root.dataset.rgsqlVersion = VERSION;
    }

    // ====== 极简构建：只追加，不移动任何原生元素 ======

    // 找到 RunnerGo 中间那个裸 DIV（包含 request 编辑器 + response 响应面板）
    var midDiv = null;
    var rootKids = Array.prototype.slice.call(root.children);
    for (var i = 0; i < rootKids.length; i++) {
      var k = rootKids[i];
      var cls = k.className || '';
      if (cls.indexOf('sql-manage-detail-header') < 0 && cls.indexOf('rgsql-') < 0) {
        midDiv = k;
        break;
      }
    }

    // 关键：给 midDiv 强制 flex column 布局（用 JS inline !important，CSS 选不中这个无 class 的裸 DIV）
    if (midDiv && !midDiv.dataset.rgsqlFlexForced) {
      midDiv.style.setProperty('display', 'flex', 'important');
      midDiv.style.setProperty('flex-direction', 'column', 'important');
      midDiv.style.setProperty('min-height', '0', 'important');
      midDiv.dataset.rgsqlFlexForced = '1';
    }

    // 关键：彻底干掉 RunnerGo 原生 response 面板（执行结果/断言结果/提取结果 tab header）
    _hideRunnerGoResponse(midDiv);
    // 持续监听 Vue 可能重新渲染的 response 元素
    if (midDiv && !midDiv.dataset.rgsqlObs) {
      midDiv.dataset.rgsqlObs = '1';
      var obs = new MutationObserver(function () {
        _hideRunnerGoResponse(midDiv);
      });
      obs.observe(midDiv, { childList: true, subtree: true });
    }

    // schema-bar / splitbar 仍然在 root 上（absolute 覆盖）
    var schemaBar = root.querySelector(':scope > .rgsql-schema-bar');
    var splitbar = root.querySelector(':scope > .rgsql-splitbar');

    // panel 放到 midDiv 里（取代被隐藏的 .sql-manage-detail-response）
    var panel = midDiv ? midDiv.querySelector(':scope > .rgsql-console') : null;
    // 清理 v20 遗留：panel 可能还在 root 上
    var oldPanelAtRoot = root.querySelector(':scope > .rgsql-console');
    if (oldPanelAtRoot && midDiv) {
      oldPanelAtRoot.remove();
      panel = null;
    }

    if (!panel && midDiv) {
      panel = document.createElement('div');
      panel.className = 'rgsql-console';
      panel.innerHTML =
        '<div class="rgsql-head"></div>' +
        '<div class="rgsql-body-tabs">' +
          '<div class="rgsql-body-tab" data-tab="results">结果</div>' +
          '<div class="rgsql-body-tab" data-tab="assertions">断言</div>' +
          '<div class="rgsql-body-tab" data-tab="extractions">关联提取</div>' +
        '</div>' +
        '<div class="rgsql-body">' +
          '<div class="rgsql-tab-panel active" data-panel="results"></div>' +
          '<div class="rgsql-tab-panel" data-panel="assertions"></div>' +
          '<div class="rgsql-tab-panel" data-panel="extractions"></div>' +
        '</div>' +
        '<div class="rgsql-footer"></div>';
      // 插入到 .sql-manage-detail-request 之后
      var req = midDiv.querySelector(':scope > .sql-manage-detail-request');
      if (req && req.nextSibling) {
        midDiv.insertBefore(panel, req.nextSibling);
      } else {
        midDiv.appendChild(panel);
      }
      bindPanelEvents(panel);
      // 初始化 active tab
      var at = state.activeTab || 'results';
      panel.querySelectorAll('.rgsql-body-tab').forEach(function (x) {
        x.classList.toggle('active', x.dataset.tab === at);
      });
      panel.querySelectorAll('.rgsql-tab-panel').forEach(function (x) {
        x.classList.toggle('active', x.dataset.panel === at);
      });
      if (at === 'assertions') renderAssertionsTab();
      else if (at === 'extractions') renderExtractionsTab();
      renderHead();
      renderResults();
      if (state.envId != null && state.envId !== 0 && state.dbs.length === 0) {
        setTimeout(refreshDbList, 100);
      }
    }

    if (!schemaBar) {
      schemaBar = document.createElement('div');
      schemaBar.className = 'rgsql-schema-bar';
      schemaBar.innerHTML = renderSchemaBar();
      schemaBar.style.setProperty('position', 'absolute', 'important');
      schemaBar.style.setProperty('left', '0', 'important');
      schemaBar.style.setProperty('top', '0', 'important');
      schemaBar.style.setProperty('bottom', '0', 'important');
      schemaBar.style.setProperty('width', '220px', 'important');
      schemaBar.style.setProperty('background', '#1d1f23', 'important');
      schemaBar.style.setProperty('border-right', '1px solid #2c2f34', 'important');
      schemaBar.style.setProperty('overflow', 'auto', 'important');
      schemaBar.style.setProperty('z-index', '10', 'important');
      root.appendChild(schemaBar);

      // 确保 root 有 position:relative 让 absolute 生效
      root.style.setProperty('position', 'relative', 'important');

      // 给 RunnerGo 原生内容加 margin-left 避让 schema-bar（首次 + 监听拖拽变化）
      applySchemaMargin(root, 220);
    }

    if (!splitbar) {
      splitbar = document.createElement('div');
      splitbar.className = 'rgsql-splitbar';
      splitbar.style.setProperty('position', 'absolute', 'important');
      splitbar.style.setProperty('top', '0', 'important');
      splitbar.style.setProperty('bottom', '0', 'important');
      splitbar.style.setProperty('width', '5px', 'important');
      splitbar.style.setProperty('cursor', 'col-resize', 'important');
      splitbar.style.setProperty('background', 'transparent', 'important');
      splitbar.style.setProperty('z-index', '11', 'important');
      splitbar.style.setProperty('left', '215px', 'important');
      root.appendChild(splitbar);
    }

    // 事件绑定（单次）
    if (!schemaBar.dataset.bound) {
      schemaBar.dataset.bound = '1';
      bindSchemaEvents(schemaBar);
      renderSchemaBar();
    }
    if (!splitbar.dataset.bound) {
      splitbar.dataset.bound = '1';
      bindSplitbar2(splitbar, schemaBar, root);
    }

    // 选中数据库后自动加载表结构
    if (state.selectedDb && state.dbs.length && state.schema.tables.length === 0 && !state.schema.loading) {
      setTimeout(loadSchema, 200);
    }

    // 恢复存的宽度
    var savedW = parseInt(localStorage.getItem('rgsql.schema.width'), 10);
    if (savedW > 0 && schemaBar.offsetWidth !== savedW) {
      schemaBar.style.width = savedW + 'px';
      splitbar.style.left = (savedW - 5) + 'px';
      applySchemaMargin(root, savedW);
    }

    return panel;
  }

  /* ============ 给 RunnerGo 原生内容加 margin-left ============ */
  function applySchemaMargin(root, width) {
    var kids = Array.prototype.slice.call(root.children);
    kids.forEach(function (k) {
      var cls = k.className || '';
      if (cls.indexOf('rgsql-') < 0) {
        // 原生元素：header、midDiv — 加 padding-left 避让
        k.style.setProperty('margin-left', width + 'px', 'important');
        k.style.setProperty('transition', 'none', 'important');
      }
    });
  }

  /* ============ 新版 splitter 拖拽 ============ */
  function bindSplitbar2(bar, target, root) {
    if (bar.dataset.bound) return;
    bar.dataset.bound = '1';
    var startX, startW, dragging = false;
    bar.addEventListener('mousedown', function (e) {
      dragging = true;
      bar.classList.add('dragging');
      startX = e.clientX;
      startW = target.offsetWidth;
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    document.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      var dx = e.clientX - startX;
      var w = Math.max(160, Math.min(window.innerWidth * 0.5, startW + dx));
      target.style.width = w + 'px';
      bar.style.left = (w - 5) + 'px';
      applySchemaMargin(root, w);
    });
    document.addEventListener('mouseup', function () {
      if (!dragging) return;
      dragging = false;
      bar.classList.remove('dragging');
      document.body.style.userSelect = '';
      localStorage.setItem('rgsql.schema.width', target.offsetWidth);
    });
  }

  function currentConnection() {
    var conf = null;
    $all('.rgsql-db-item').some(function (d) {
      if (d.dataset.server === state.selectedDb) { conf = JSON.parse(d.dataset.conf); return true; }
      return false;
    });
    if (conf) return conf;
    // 兜底：使用 target 上已保存的连接
    var info = state.targetInfo && state.targetInfo.sql_detail;
    if (info && info.sql_database_info && info.sql_database_info.host) {
      var c = info.sql_database_info;
      return { type: 'mysql', host: c.host, port: c.port, user: c.user, password: c.password, db_name: c.db_name, charset: c.charset || 'utf8mb4' };
    }
    return null;
  }

  /* --------------------------- 表结构浏览 --------------------------- */

  function renderSchemaBar() {
    if (state.schema.loading) {
      return '<div class="rgsql-schema-head"><span class="rgsql-schema-title">🗂 表结构</span></div>' +
        '<div class="rgsql-schema-list"><div class="rgsql-schema-loading">正在加载表…</div></div>';
    }
    var tables = state.schema.tables;
    var sb = state.schema.selectedTable;
    var expanded = state.schema.expanded[sb];
    var html = '<div class="rgsql-schema-head">' +
      '<span class="rgsql-schema-title">🗂 表结构</span>' +
      '<button class="rgsql-schema-reload" title="刷新表结构">🔄</button>' +
      '</div><div class="rgsql-schema-list">';
    if (!currentConnection()) {
      html += '<div class="rgsql-schema-empty">请先从上方选择<br>数据库连接</div>';
    } else if (!tables.length) {
      html += '<div class="rgsql-schema-empty">暂无表<br><button>重新加载</button></div>';
    } else {
      // tables 按字母排序
      var sorted = tables.slice().sort(function (a, b) { return a.name.localeCompare(b.name); });
      for (var i = 0; i < sorted.length; i++) {
        var t = sorted[i];
        var isSel = sb === t.name;
        var isExp = expanded;
        html += '<div class="rgsql-schema-item table' + (isSel ? ' active' : '') + '" ' +
          'data-table="' + esc(t.name) + '" title="双击插入 SELECT">' + esc(t.name) + '</div>';
        if (isSel && t.columns && t.columns.length) {
          for (var j = 0; j < t.columns.length; j++) {
            var c = t.columns[j];
            html += '<div class="rgsql-schema-item col' + (c.key === 'PRI' ? ' pk' : '') + '" ' +
              'data-table="' + esc(t.name) + '" data-col="' + esc(c.name) + '" ' +
              'title="双击插入列名">' + esc(c.name) +
              '<span class="type">' + esc(c.type || '') + '</span></div>';
          }
        }
      }
    }
    html += '</div>';
    return html;
  }

  function renderSchemaBarUpdate() {
    var bar = $('.rgsql-schema-bar');
    if (bar) bar.innerHTML = renderSchemaBar();
  }

  function bindSchemaEvents(bar) {
    bar.addEventListener('click', function (e) {
      var t = e.target;
      // 刷新按钮
      if (t.closest('.rgsql-schema-reload')) {
        if (!currentConnection()) { toast('请先选择数据库', 'error'); return; }
        state.schema.tables = [];
        state.schema.selectedTable = '';
        state.schema.expanded = {};
        renderSchemaBarUpdate();
        loadSchema();
        return;
      }
      // 空状态按钮
      if (t.tagName === 'BUTTON') {
        if (!currentConnection()) { toast('请先选择数据库', 'error'); return; }
        loadSchema();
        return;
      }
      // 点击表名
      var item = t.closest('.rgsql-schema-item');
      if (item) {
        var tableName = item.dataset.table;
        var colName = item.dataset.col;
        // 左键选中表，展开/折叠
        if (colName) {
          // 点击列名：选中所属表 + 高亮列
          state.schema.selectedTable = tableName;
          if (!state.schema.tables.find(function (t) { return t.name === tableName; }).columns) {
            loadTableColumns(tableName);
          }
        } else {
          // 点击表名：切换展开
          if (state.schema.selectedTable === tableName) {
            state.schema.selectedTable = '';
          } else {
            state.schema.selectedTable = tableName;
            var tbl = state.schema.tables.find(function (t) { return t.name === tableName; });
            if (tbl && !tbl.columns) loadTableColumns(tableName);
          }
        }
        renderSchemaBarUpdate();
        return;
      }
    });

    bar.addEventListener('dblclick', function (e) {
      var item = e.target.closest('.rgsql-schema-item');
      if (!item) return;
      var tableName = item.dataset.table;
      var colName = item.dataset.col;
      if (colName) {
        // 双击列名 → 插入到编辑器
        insertIntoEditor('`' + colName + '`');
      } else {
        // 双击表名 → 插入 SELECT * FROM table
        var conn = currentConnection();
        var db = conn && conn.db_name ? '`' + conn.db_name + '`.' : '';
        insertIntoEditor('SELECT * FROM ' + db + '`' + tableName + '`\n');
      }
    });
  }

  function insertIntoEditor(text) {
    var cur = currentSqlText();
    if (!cur.fromEditor) return;
    try {
      var ed = cur.ed;
      var model = ed.getModel();
      var sel = ed.getSelection();
      // 如果有选中就替换，没有就插入
      if (sel && model.getValueInRange(sel)) {
        ed.executeEdits('rgsql', [{ range: sel, text: text, forceMoveMarkers: true }]);
      } else {
        ed.executeEdits('rgsql', [{ range: sel, text: text }]);
      }
      ed.focus();
    } catch (e) { toast('插入失败: ' + e.message, 'error'); }
  }

  function loadSchema() {
    var conn = currentConnection();
    if (!conn) return;
    state.schema.loading = true;
    renderSchemaBarUpdate();
    callApi('execute', {
      connection: conn,
      statements: ['SHOW TABLES'],
      variables: {}, assertions: {}, extractions: {},
      row_limit: 5000, stop_on_error: true, timeout_ms: 15000
    }).then(function (data) {
      state.schema.loading = false;
      var tables = [];
      var r = (data.results || [])[0];
      if (r && r.columns && r.rows) {
        var nameCol = r.columns[0]; // SHOW TABLES 第一列就是表名
        r.rows.forEach(function (row) {
          tables.push({ name: row[0], columns: null });
        });
      }
      state.schema.tables = tables;
      if (state.schema.selectedTable && !tables.find(function (t) { return t.name === state.schema.selectedTable; })) {
        state.schema.selectedTable = '';
      }
      renderSchemaBarUpdate();
      toast('已加载 ' + tables.length + ' 张表', 'success');
    }).catch(function (err) {
      state.schema.loading = false;
      renderSchemaBarUpdate();
      toast('加载表列表失败: ' + err.message, 'error');
    });
  }

  function loadTableColumns(tableName) {
    var conn = currentConnection();
    if (!conn) return;
    var shortName = String(tableName).split('.').pop();
    callApi('execute', {
      connection: conn,
      statements: ['DESCRIBE ' + tableName],
      variables: {}, assertions: {}, extractions: {},
      row_limit: 200, stop_on_error: true, timeout_ms: 10000
    }).then(function (data) {
      var r = (data.results || [])[0];
      if (r && r.rows) {
        var cols = r.rows.map(function (row) {
          return { name: row[0], type: row[1], key: row[3], nullable: row[2] };
        });
        var tbl = state.schema.tables.find(function (t) { return t.name === shortName || t.name === tableName; });
        if (!tbl) { tbl = { name: shortName, columns: null }; state.schema.tables.push(tbl); }
        tbl.columns = cols;
        if (state._loadingTables) state._loadingTables[tableName] = false;
        renderSchemaBarUpdate();
        renderResults();
      }
    }).catch(function () { if (state._loadingTables) state._loadingTables[tableName] = false; });
  }

  /* ------------------------------ 执行逻辑 ------------------------------ */

  function runExecute(selectedOnly) {
    var root = sqlRoot();
    if (!root) { toast('请先打开一个 SQL 节点', 'error'); return; }
    if (state.running) { toast('正在执行中…', 'error'); return; }

    var cur = currentSqlText();
    var text = cur.text || '';
    if (!text.trim()) { toast('编辑器中没有 SQL', 'error'); return; }

    state._lastQuerySql = null;
    if (selectedOnly) { showStatementPicker(text); return; }
    execStatements(splitSql(text));
  }

  function execStatements(statements) {
    if (!statements || !statements.length) { toast('没有可执行的 SQL', 'error'); return; }
    var conn = currentConnection();
    if (!conn) { toast('请先选择数据库（切换顶部环境后从下拉框选择）', 'error'); return; }
    state.gridEdits = {};

    state.running = true;
    state.results = 'loading';
    renderResults(statements);
    renderHead();

    callApi('execute', {
      connection: conn,
      statements: statements,
      variables: {},
      assertions: state.assertions,
      extractions: state.extractions,
      row_limit: state.rowLimit,
      stop_on_error: state.stopOnError,
      timeout_ms: 30000
    }).then(function (data) {
      state.running = false;
      state.results = data;
      renderResults();
      renderHead();
      var fails = (data.results || []).filter(function (r) { return r.status === 'error'; }).length;
      if (fails) toast('执行完成，' + fails + ' 条语句失败', 'error');
      else toast('全部执行成功（' + fmtMs(data.total_ms || 0) + '）', 'success');
    }).catch(function (err) {
      state.running = false;
      state.results = { error: err.message || String(err), results: [] };
      renderResults();
      renderHead();
      toast(err.message || '执行失败', 'error');
    });
  }

  function showStatementPicker(text) {
    var statements = splitSql(text);
    if (!statements.length) { toast('没有可执行的 SQL', 'error'); return; }
    var panel = $('.rgsql-console'); if (!panel) return;
    panel.querySelectorAll('.rgsql-body-tab').forEach(function (x) {
      x.classList.toggle('active', x.dataset.tab === 'results');
    });
    panel.querySelectorAll('.rgsql-tab-panel').forEach(function (x) {
      x.classList.toggle('active', x.dataset.panel === 'results');
    });
    var body = $('[data-panel="results"]', panel);
    body.innerHTML = '<div class="rgsql-picker">' +
      '<div class="rgsql-picker-h">勾选要执行的 SQL 语句（共 ' + statements.length + ' 条）</div>' +
      '<div class="rgsql-picker-list">' +
        statements.map(function (s, i) {
          return '<label class="rgsql-picker-item">' +
            '<input type="checkbox" class="rgsql-pick-cb" data-idx="' + i + '" checked>' +
            '<span class="rgsql-pick-idx">' + (i + 1) + '</span>' +
            '<span class="rgsql-pick-sql">' + esc(s) + '</span>' +
          '</label>';
        }).join('') +
      '</div>' +
      '<div class="rgsql-picker-bar">' +
        '<button class="rgsql-btn rgsql-pick-all">全选</button>' +
        '<button class="rgsql-btn rgsql-pick-none">全不选</button>' +
        '<span style="flex:1"></span>' +
        '<button class="rgsql-btn rgsql-btn-primary rgsql-pick-run">执行勾选</button>' +
      '</div>' +
    '</div>';
    state._pickedStatements = statements;
  }

  /* ------------------------------ 断言 & 关联提取 规则 ------------------------------ */

  var _ASSERT_TYPES = [
    { v: 'status', l: '执行状态' },
    { v: 'row_count', l: '行数/影响行' },
    { v: 'cell', l: '单元格值' },
  ];
  var _OPS_COMMON = [
    { v: 'eq', l: '等于' }, { v: 'ne', l: '不等于' },
    { v: 'gt', l: '>' }, { v: 'gte', l: '>=' },
    { v: 'lt', l: '<' }, { v: 'lte', l: '<=' },
    { v: 'contains', l: '包含' }, { v: 'not_contains', l: '不包含' },
    { v: 'regex', l: '正则匹配' },
    { v: 'is_null', l: '为null' }, { v: 'not_null', l: '不为null' },
    { v: 'empty', l: '为空' }, { v: 'not_empty', l: '不为空' },
  ];
  var _EXTR_TYPES = [
    { v: 'cell', l: '单元格' },
    { v: 'row_count', l: '行数' },
    { v: 'affected_rows', l: '影响行数' },
    { v: 'insert_id', l: '插入ID' },
  ];

  function addAssertion() {
    var sIdx = prompt('作用于第几条语句？（0=第1条，默认0）', '0');
    if (sIdx === null) return;
    sIdx = parseInt(sIdx, 10) || 0;
    if (!state.assertions[sIdx]) state.assertions[sIdx] = [];
    state.assertions[sIdx].push({
      name: '', type: 'status', op: 'eq', value: 'ok', row: 0, col: ''
    });
    lsSet('rgsql.assertions', state.assertions);
  }
  function addExtraction() {
    var sIdx = prompt('作用于第几条语句？（0=第1条，默认0）', '0');
    if (sIdx === null) return;
    sIdx = parseInt(sIdx, 10) || 0;
    var name = prompt('变量名（如 user_id）：');
    if (!name) return;
    if (!state.extractions[sIdx]) state.extractions[sIdx] = [];
    state.extractions[sIdx].push({
      name: name, type: 'cell', row: 0, col: ''
    });
    lsSet('rgsql.extractions', state.extractions);
  }
  function removeRule(group, sIdx, rIdx) {
    var list = state[group] && state[group][sIdx];
    if (!list) return;
    list.splice(rIdx, 1);
    if (!list.length) delete state[group][sIdx];
    lsSet('rgsql.' + group, state[group]);
  }
  function _selHtml(opts, val) {
    return opts.map(function (o) {
      return '<option value="' + o.v + '"' + (o.v === val ? ' selected' : '') + '>' + o.l + '</option>';
    }).join('');
  }
  function renderAssertionsTab() {
    var panel = $('.rgsql-console [data-panel="assertions"]'); if (!panel) return;
    var sIdxKeys = Object.keys(state.assertions).sort(function (a, b) { return a - b; });
    if (!sIdxKeys.length) {
      panel.innerHTML = '<div class="rgsql-rule-toolbar">' +
        '<button class="rgsql-btn rgsql-add-assert">＋ 添加断言规则</button>' +
        '<span style="flex:1"></span><span style="color:#5a6070;font-size:11px">断言按语句索引分组，执行时自动应用</span>' +
        '</div><div class="rgsql-rule-empty">暂无断言规则，点击上方按钮添加</div>';
      return;
    }
    var html = '<div class="rgsql-rule-toolbar">' +
      '<button class="rgsql-btn rgsql-add-assert">＋ 添加断言规则</button>' +
      '<span style="flex:1"></span><span style="color:#5a6070;font-size:11px">断言按语句索引分组，执行时自动应用</span></div>';
    html += sIdxKeys.map(function (sIdx) {
      var rules = state.assertions[sIdx];
      return '<div style="padding:4px 10px;background:#1a1b1e;border-top:1px solid #2c2f34">' +
        '<span style="font-size:12px;color:#9fd0ff;font-weight:600">作用于 第 ' + (parseInt(sIdx, 10) + 1) + ' 条语句</span></div>' +
        '<table class="rgsql-rule-table"><thead><tr>' +
        '<th style="width:100px">名称</th>' +
        '<th style="width:100px">类型</th>' +
        '<th style="width:110px">操作符</th>' +
        '<th style="width:110px">期望值</th>' +
        '<th style="width:70px">行号</th>' +
        '<th style="width:120px">列名/列号</th>' +
        '<th style="width:50px"></th>' +
        '</tr></thead><tbody>' +
        rules.map(function (r, ri) {
          return '<tr data-rule-group="assertions" data-idx="' + sIdx + '" data-ridx="' + ri + '">' +
            '<td><input type="text" data-field="name" value="' + esc(r.name || '') + '" placeholder="自定义名称"></td>' +
            '<td><select data-field="type">' + _selHtml(_ASSERT_TYPES, r.type) + '</select></td>' +
            '<td><select data-field="op">' + _selHtml(_OPS_COMMON, r.op) + '</select></td>' +
            '<td><input type="text" data-field="value" value="' + esc(r.value == null ? '' : String(r.value)) + '" placeholder="期望值"></td>' +
            '<td><input type="number" data-field="row" value="' + esc(r.row == null ? '0' : String(r.row)) + '" min="0"></td>' +
            '<td><input type="text" data-field="col" value="' + esc(r.col || '') + '" placeholder="列名或列号"></td>' +
            '<td><button class="rgsql-del rgsql-del-assert" data-idx="' + sIdx + '" data-ridx="' + ri + '">删</button></td>' +
            '</tr>';
        }).join('') + '</tbody></table>';
    }).join('');
    panel.innerHTML = html;
  }
  function renderExtractionsTab() {
    var panel = $('.rgsql-console [data-panel="extractions"]'); if (!panel) return;
    var sIdxKeys = Object.keys(state.extractions).sort(function (a, b) { return a - b; });
    if (!sIdxKeys.length) {
      panel.innerHTML = '<div class="rgsql-rule-toolbar">' +
        '<button class="rgsql-btn rgsql-add-extr">＋ 添加提取规则</button>' +
        '<span style="flex:1"></span><span style="color:#5a6070;font-size:11px">提取变量可在后续 SQL 中用 ${var} 引用</span>' +
        '</div><div class="rgsql-rule-empty">暂无提取规则，点击上方按钮添加（变量可在后续 SQL 中用 ${var} 引用）</div>';
      return;
    }
    var html = '<div class="rgsql-rule-toolbar">' +
      '<button class="rgsql-btn rgsql-add-extr">＋ 添加提取规则</button>' +
      '<span style="flex:1"></span><span style="color:#5a6070;font-size:11px">提取变量可在后续 SQL 中用 ${var} 引用</span></div>';
    html += sIdxKeys.map(function (sIdx) {
      var rules = state.extractions[sIdx];
      return '<div style="padding:4px 10px;background:#1a1b1e;border-top:1px solid #2c2f34">' +
        '<span style="font-size:12px;color:#9fd0ff;font-weight:600">作用于 第 ' + (parseInt(sIdx, 10) + 1) + ' 条语句</span></div>' +
        '<table class="rgsql-rule-table"><thead><tr>' +
        '<th style="width:130px">变量名</th>' +
        '<th style="width:110px">提取类型</th>' +
        '<th style="width:70px">行号</th>' +
        '<th style="width:150px">列名/列号</th>' +
        '<th style="width:50px"></th>' +
        '</tr></thead><tbody>' +
        rules.map(function (r, ri) {
          return '<tr data-rule-group="extractions" data-idx="' + sIdx + '" data-ridx="' + ri + '">' +
            '<td><input type="text" data-field="name" value="' + esc(r.name || '') + '" placeholder="var_name"></td>' +
            '<td><select data-field="type">' + _selHtml(_EXTR_TYPES, r.type) + '</select></td>' +
            '<td><input type="number" data-field="row" value="' + esc(r.row == null ? '0' : String(r.row)) + '" min="0"></td>' +
            '<td><input type="text" data-field="col" value="' + esc(r.col || '') + '" placeholder="列名或列号"></td>' +
            '<td><button class="rgsql-del rgsql-del-extr" data-idx="' + sIdx + '" data-ridx="' + ri + '">删</button></td>' +
            '</tr>';
        }).join('') + '</tbody></table>';
    }).join('');
    panel.innerHTML = html;
  }

  /* ------------------------------ 渲染：头部 ------------------------------ */

  function dbOptions() {
    var opts = state.dbs.map(function (d) {
      var label = d.server_name + (d.db_name ? ' (' + d.db_name + ')' : '');
      return '<option class="rgsql-db-item" value="' + esc(d.server_name) + '" data-server="' + esc(d.server_name) + '" data-conf=\'' +
        esc(JSON.stringify({ type: 'mysql', host: d.host, port: d.port, user: d.user, password: d.password, db_name: d.db_name, charset: d.charset || 'utf8mb4' })) + '\'' +
        (state.selectedDb === d.server_name ? ' selected' : '') + '>' + esc(label) + '</option>';
    }).join('');
    return opts;
  }

  function currentConnLabel() {
    var conn = currentConnection();
    return conn ? (conn.host + ':' + conn.port + '/' + (conn.db_name || '…')) : '未选择';
  }

  function renderHead() {
    var panel = $('.rgsql-console'); if (!panel) return;
    var head = $('.rgsql-head', panel);
    var running = state.running;
    var connLabel = esc(currentConnLabel());
    head.innerHTML = [
      '<span class="rgsql-title">SQL 控制台</span>',
      '<span class="rgsql-target" title="当前 SQL 节点">' + esc(state.targetName || '新建SQL') + '</span>',
      '<select class="rgsql-select rgsql-db" title="选择数据库连接（来自当前环境配置）">' +
        (state.dbs.length ? dbOptions() : '<option value="">（切换环境后自动加载）</option>') + '</select>',
      '<button class="rgsql-btn rgsql-refresh" title="重新加载数据库列表">刷新</button>',
      '<span class="rgsql-meta">' + connLabel + '</span>',
      '<span style="flex:1"></span>',
      '<label class="rgsql-meta">行数 <select class="rgsql-select rgsql-limit">',
        [100, 200, 500, 1000].map(function (n) {
          return '<option value="' + n + '"' + (state.rowLimit === n ? ' selected' : '') + '>' + n + '</option>';
        }).join('') + '</select></label>',
      '<label class="rgsql-switch ' + (state.stopOnError ? 'on' : '') + ' rgsql-stop" title="某条失败后停止执行后续语句"><span class="dot"></span>失败即停</label>',
      '<button class="rgsql-btn rgsql-run-sel" ' + (running ? 'disabled' : '') + '>执行选中</button>',
      '<button class="rgsql-btn rgsql-btn-primary rgsql-run-all" ' + (running ? 'disabled' : '') + '>' +
        (running ? '<span class="rgsql-loading"></span> 执行中…' : '执行全部') + '</button>'
    ].join('');

    // 恢复选中的数据库
    var dbSel = $('.rgsql-db', head);
    if (state.selectedDb) dbSel.value = state.selectedDb;
    if (dbSel.selectedIndex < 0 && dbSel.options.length) dbSel.selectedIndex = 0;
    var sel = dbSel.options[dbSel.selectedIndex];
    if (sel && sel.classList.contains('rgsql-db-item')) {
      state.selectedDb = sel.dataset.server;
      lsSet('rgsql.db.' + String(state.targetId || '_default'), state.selectedDb);
    }

    $('.rgsql-footer', panel).textContent = '提示：Ctrl+Enter 执行全部；多条 SQL 以分号分隔；切换顶部环境可自动加载对应的数据库连接';
  }

  /* ------------------------------ 渲染：结果 ------------------------------ */

  function statusPill(r) {
    if (r.status === 'error') return '<span class="rgsql-pill err">失败</span>';
    if (r.status === 'skipped') return '<span class="rgsql-pill skip">已跳过</span>';
    return '<span class="rgsql-pill ok">成功</span>';
  }

  function parseTableName(sql) {
    var m = /\bfrom\s+([\w.]+)/i.exec(sql || '');
    if (!m) return null;
    return m[1];
  }

  function getTablePk(tableName) {
    var shortName = String(tableName).split('.').pop();
    var tbl = state.schema.tables.find(function (t) { return t.name === shortName || t.name === tableName; });
    if (!tbl || !tbl.columns) return [];
    return tbl.columns.filter(function (c) { return c.key === 'PRI'; }).map(function (c) { return c.name; });
  }

  function sqlValue(v) {
    if (v === null || v === undefined || v === '') return 'NULL';
    var s = String(v);
    if (/^-?\d+(\.\d+)?$/.test(s)) return s;
    return "'" + s.replace(/'/g, "''") + "'";
  }

  function getGridEdit(cardIdx) {
    if (state.gridEdits && state.gridEdits[cardIdx]) {
      var existing = state.gridEdits[cardIdx];
      if (existing.table && !existing.pkCols.length) {
        var newPk = getTablePk(existing.table);
        if (newPk.length) existing.pkCols = newPk;
      }
      return existing;
    }
    var results = (state.results && state.results.results) || [];
    var r = results[cardIdx];
    if (!r || !r.columns) return null;
    var tableName = parseTableName(r.sql);
    var pkCols = getTablePk(tableName);
    if (tableName && !pkCols.length) {
      if (!state._loadingTables) state._loadingTables = {};
      if (!state._loadingTables[tableName]) {
        state._loadingTables[tableName] = true;
        loadTableColumns(tableName);
      }
    }
    var rows = (r.rows || []).map(function (row) { return row.slice(); });
    var edit = {
      table: tableName, columns: r.columns.slice(), pkCols: pkCols, sql: r.sql,
      rows: rows,
      originals: (r.rows || []).map(function (row) { return row.slice(); }),
      rowStatus: rows.map(function () { return 'orig'; }),
      deleted: [], dirty: false
    };
    if (!state.gridEdits) state.gridEdits = {};
    state.gridEdits[cardIdx] = edit;
    return edit;
  }

  function genUpdateSql(table, pkCols, cols, origRow, newRow) {
    if (!pkCols || !pkCols.length) return null;
    var sets = [];
    for (var i = 0; i < cols.length; i++) {
      if (String(origRow[i]) !== String(newRow[i])) sets.push(cols[i] + '=' + sqlValue(newRow[i]));
    }
    if (!sets.length) return null;
    var where = pkCols.map(function (pk) { var idx = cols.indexOf(pk); return pk + '=' + sqlValue(origRow[idx]); });
    return 'UPDATE ' + table + ' SET ' + sets.join(', ') + ' WHERE ' + where.join(' AND ') + ';';
  }

  function genInsertSql(table, cols, row) {
    var vals = row.map(function (v) { return sqlValue(v); });
    return 'INSERT INTO ' + table + ' (' + cols.join(', ') + ') VALUES (' + vals.join(', ') + ');';
  }

  function genDeleteSql(table, pkCols, cols, row) {
    if (!pkCols || !pkCols.length) return null;
    var where = pkCols.map(function (pk) { var idx = cols.indexOf(pk); return pk + '=' + sqlValue(row[idx]); });
    return 'DELETE FROM ' + table + ' WHERE ' + where.join(' AND ') + ';';
  }

  function downloadFile(filename, content, mime) {
    var blob = new Blob([content], { type: mime || 'text/plain' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 200);
  }

  function exportCsv(columns, rows) {
    var lines = [columns.map(function (c) { return /[",\n]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c; }).join(',')];
    rows.forEach(function (row) {
      lines.push(row.map(function (v) {
        if (v === null || v === undefined) return '';
        var s = String(v);
        if (/[",\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
        return s;
      }).join(','));
    });
    return lines.join('\n');
  }

  function renderGrid(r) {
    if (r.error) return '<div class="rgsql-err">' + esc(r.error) + '</div>';
    if (r.columns && r.columns.length) {
      var edit = getGridEdit(r.index);
      var tableName = edit ? edit.table : null;
      var pkCols = edit ? edit.pkCols : [];
      var canEdit = tableName && pkCols.length > 0;
      var cardIdx = r.index;
      var displayRows = edit ? edit.rows : (r.rows || []);
      var thead = '<th class="rgsql-grid-act"></th>' + r.columns.map(function (c) {
        var isPk = pkCols.indexOf(c) >= 0;
        return '<th' + (isPk ? ' class="pk"' : '') + '>' + esc(c) + (isPk ? ' 🔑' : '') + '</th>';
      }).join('');
      var rows = displayRows.map(function (row, ri) {
        var status = edit ? edit.rowStatus[ri] : 'orig';
        var rowCls = status === 'added' ? ' class="rgsql-row-added"' : (status === 'modified' ? ' class="rgsql-row-modified"' : '');
        var tds = '<td class="rgsql-grid-act"><span class="rgsql-row-del" data-card="' + cardIdx + '" data-row="' + ri + '" title="删除此行">✕</span></td>' +
          row.map(function (v, ci) {
            var cls = v === null ? 'null' : '';
            return '<td class="' + cls + '" data-card="' + cardIdx + '" data-row="' + ri + '" data-col="' + ci + '">' + (v === null ? 'NULL' : esc(v)) + '</td>';
          }).join('');
        return '<tr' + rowCls + '>' + tds + '</tr>';
      }).join('');
      var note = r.truncated ? '<div class="rgsql-info">结果超过上限仅显示前 ' + r.row_count + ' 行</div>' : '';
      var toolbar = '<div class="rgsql-grid-toolbar">' +
        '<span class="rgsql-grid-tbl">' + (canEdit ? '表 <b>' + esc(tableName) + '</b> · 主键 ' + esc(pkCols.join(', ')) : (tableName ? '表 ' + esc(tableName) + ' 无主键（只读）' : '只读模式')) + '</span>' +
        '<span style="flex:1"></span>' +
        '<button class="rgsql-btn rgsql-grid-refresh" data-card="' + cardIdx + '" title="重新查询刷新结果">🔄 刷新</button>' +
        (canEdit ? '<button class="rgsql-btn rgsql-grid-addrow" data-card="' + cardIdx + '">＋ 增加行</button>' +
          '<button class="rgsql-btn rgsql-grid-undo" data-card="' + cardIdx + '">↶ 撤销</button>' +
          '<button class="rgsql-btn rgsql-btn-primary rgsql-grid-commit" data-card="' + cardIdx + '">✓ 提交</button>' : '') +
        '<button class="rgsql-btn rgsql-grid-export" data-card="' + cardIdx + '">⬇ 导出</button>' +
      '</div>';
      return note + toolbar + '<div class="rgsql-grid-wrap"><table class="rgsql-grid rgsql-grid-edit"><thead><tr>' + thead +
        '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
        (r.row_count ? '' : '<div class="rgsql-info">查询结果为空</div>');
    }
    var bits = [];
    if (r.affected_rows > 0 || r.status === 'ok') bits.push('影响行数 ' + r.affected_rows);
    if (r.insert_id) bits.push('insert_id ' + r.insert_id);
    var requeryBtn = state._lastQuerySql ? '<button class="rgsql-btn rgsql-grid-requery" title="重新执行原查询，查看更新后数据">🔄 刷新原查询</button>' : '';
    return '<div class="rgsql-info" style="display:flex;align-items:center;gap:12px">' +
      '<span>' + (bits.join('，') || '执行成功（无结果集）') + '</span>' + requeryBtn + '</div>';
  }

  function renderResults(pendingStatements) {
    var panel = $('.rgsql-console'); if (!panel) return;
    var body = $('[data-panel="results"]', panel);

    if (state.results === 'loading') {
      body.innerHTML = '<div class="rgsql-empty"><span class="rgsql-loading"></span> 正在执行 ' +
        (pendingStatements ? pendingStatements.length : '') + ' 条语句…</div>';
      return;
    }
    if (state.results && state.results.error) {
      body.innerHTML = '<div class="rgsql-err">' + esc(state.results.error) + '</div>';
      return;
    }
    if (!state.results || !state.results.results || !state.results.results.length) {
      body.innerHTML = '<div class="rgsql-empty">编辑 SQL 后点击 <b>执行全部</b> 或按 <kbd>Ctrl</kbd>+<kbd>Enter</kbd><br>' +
        '支持一次执行多条 SQL（分号分隔），每条语句独立展示结果、耗时与影响行数<br>' +
        '切换顶部环境可从下拉框选择对应数据库连接</div>';
      return;
    }

    body.innerHTML = state.results.results.map(function (r) {
      var sqlHtml = esc(r.sql);
      if (r.executed_sql !== r.sql) {
        sqlHtml += '\n<span class="rgsql-sub">↳ 替换变量后: ' + esc(preview(r.executed_sql, 300)) + '</span>';
      }
      // 断言展示
      var assertHtml = '';
      if (r.assertions && r.assertions.length) {
        assertHtml = '<div class="rgsql-card-assertions">' + r.assertions.map(function (a) {
          var icon = a.passed ? '<span class="rgsql-pass-ic ok">✓</span>' : '<span class="rgsql-pass-ic err">✗</span>';
          var colorCls = a.passed ? 'rgsql-rule-pass' : 'rgsql-rule-fail';
          var actualStr = a.actual == null ? 'null' : String(a.actual);
          var expectStr = a.expected == null ? 'null' : String(a.expected);
          var msg = a.message ? ' <span style="color:#8b909a;font-size:10px">(' + esc(a.message) + ')</span>' : '';
          return '<div class="rgsql-card-assertion">' + icon +
            '<span style="color:#cfd3da">' + esc(a.name || (a.type + ' ' + a.op)) + '</span>' +
            '<span style="color:#8b909a">期望:</span><span>' + esc(expectStr) + '</span>' +
            '<span style="color:#8b909a">实际:</span><span class="' + colorCls + '">' + esc(actualStr) + '</span>' +
            msg + '</div>';
        }).join('') + '</div>';
      }
      // 提取展示
      var extrHtml = '';
      if (r.extractions && r.extractions.length) {
        extrHtml = '<div class="rgsql-card-extractions">' + r.extractions.map(function (x) {
          if (x.error) {
            return '<span class="rgsql-card-ext" style="border-color:rgba(248,113,113,.4);color:#f87171">' + esc(x.name) + ' ❌ ' + esc(x.error) + '</span>';
          }
          return '<span class="rgsql-card-ext">${' + esc(x.name) + '} = ' + esc(x.value == null ? 'null' : String(x.value)) + '</span>';
        }).join('') + '</div>';
      }
      return '<div class="rgsql-card" data-idx="' + r.index + '">' +
        '<div class="rgsql-card-h">' +
          '<span class="rgsql-idx">语句 ' + (r.index + 1) + '</span>' + statusPill(r) +
          '<span class="rgsql-meta">' + fmtMs(r.duration_ms) + '</span>' +
          (r.columns && r.columns.length ? '<span class="rgsql-meta">' + r.row_count + ' 行' + (r.truncated ? '(截断)' : '') + '</span>'
            : (r.status === 'ok' ? '<span class="rgsql-meta">影响 ' + r.affected_rows + ' 行</span>' : '')) +
          assertHtml + extrHtml +
        '</div>' +
        '<div class="rgsql-sql">' + sqlHtml + '</div>' +
        renderGrid(r) +
        '</div>';
    }).join('');
  }

  /* ------------------------------ 面板事件 ------------------------------ */

  function bindPanelEvents(panel) {
    panel.addEventListener('click', function (e) {
      var t = e.target;
      // ====== tabs 切换 ======
      var tabEl = t.closest('.rgsql-body-tab');
      if (tabEl) {
        var tab = tabEl.dataset.tab;
        state.activeTab = tab;
        lsSet('rgsql.activeTab', tab);
        panel.querySelectorAll('.rgsql-body-tab').forEach(function (x) {
          x.classList.toggle('active', x.dataset.tab === tab);
        });
        panel.querySelectorAll('.rgsql-tab-panel').forEach(function (x) {
          x.classList.toggle('active', x.dataset.panel === tab);
        });
        // 切换后渲染对应的规则面板
        if (tab === 'assertions') renderAssertionsTab();
        else if (tab === 'extractions') renderExtractionsTab();
        return;
      }
      if (t.closest('.rgsql-run-all')) { runExecute(false); return; }
      if (t.closest('.rgsql-run-sel')) { runExecute(true); return; }
      if (t.closest('.rgsql-pick-run')) {
        var cbs = panel.querySelectorAll('.rgsql-pick-cb:checked');
        var picked = [];
        cbs.forEach(function (cb) {
          var idx = parseInt(cb.dataset.idx, 10);
          if (state._pickedStatements && state._pickedStatements[idx]) picked.push(state._pickedStatements[idx]);
        });
        if (!picked.length) { toast('请至少勾选一条 SQL 语句', 'error'); return; }
        execStatements(picked);
        return;
      }
      if (t.closest('.rgsql-pick-all')) {
        panel.querySelectorAll('.rgsql-pick-cb').forEach(function (cb) { cb.checked = true; });
        return;
      }
      if (t.closest('.rgsql-pick-none')) {
        panel.querySelectorAll('.rgsql-pick-cb').forEach(function (cb) { cb.checked = false; });
        return;
      }
      // ====== 结果表格编辑工具栏 ======
      if (t.closest('.rgsql-row-del')) {
        var delEl = t.closest('.rgsql-row-del');
        var dCi = parseInt(delEl.dataset.card, 10);
        var dRi = parseInt(delEl.dataset.row, 10);
        var dEd = getGridEdit(dCi);
        if (dEd) {
          if (dEd.originals[dRi]) dEd.deleted.push(dEd.originals[dRi]);
          dEd.rows.splice(dRi, 1); dEd.originals.splice(dRi, 1); dEd.rowStatus.splice(dRi, 1);
          dEd.dirty = true; renderResults();
        }
        return;
      }
      if (t.closest('.rgsql-grid-refresh')) {
        var rfEl = t.closest('.rgsql-grid-refresh');
        var rfCi = parseInt(rfEl.dataset.card, 10);
        var rfEd = getGridEdit(rfCi);
        if (rfEd) { delete state.gridEdits[rfCi]; execStatements([rfEd.sql]); }
        return;
      }
      if (t.closest('.rgsql-grid-requery')) {
        var rqSql = state._lastQuerySql;
        state._lastQuerySql = null;
        if (rqSql) { execStatements([rqSql]); }
        return;
      }
      if (t.closest('.rgsql-grid-addrow')) {
        var arEl = t.closest('.rgsql-grid-addrow');
        var arCi = parseInt(arEl.dataset.card, 10);
        var arEd = getGridEdit(arCi);
        if (arEd) {
          arEd.rows.push(arEd.columns.map(function () { return null; }));
          arEd.originals.push(null);
          arEd.rowStatus.push('added');
          arEd.dirty = true; renderResults();
        }
        return;
      }
      if (t.closest('.rgsql-grid-undo')) {
        var unEl = t.closest('.rgsql-grid-undo');
        var unCi = parseInt(unEl.dataset.card, 10);
        delete state.gridEdits[unCi]; renderResults();
        return;
      }
      if (t.closest('.rgsql-grid-commit')) {
        var cmEl = t.closest('.rgsql-grid-commit');
        var cmCi = parseInt(cmEl.dataset.card, 10);
        var cmEd = getGridEdit(cmCi);
        if (!cmEd) return;
        var sqls = [];
        cmEd.deleted.forEach(function (dr) { sqls.push(genDeleteSql(cmEd.table, cmEd.pkCols, cmEd.columns, dr)); });
        for (var ci2 = 0; ci2 < cmEd.rows.length; ci2++) {
          if (cmEd.rowStatus[ci2] === 'modified') {
            var u = genUpdateSql(cmEd.table, cmEd.pkCols, cmEd.columns, cmEd.originals[ci2], cmEd.rows[ci2]);
            if (u) sqls.push(u);
          } else if (cmEd.rowStatus[ci2] === 'added') {
            sqls.push(genInsertSql(cmEd.table, cmEd.columns, cmEd.rows[ci2]));
          }
        }
        if (!sqls.length) { toast('没有需要提交的修改', 'error'); return; }
        if (!confirm('将执行 ' + sqls.length + ' 条修改语句，确认提交？')) return;
        state._lastQuerySql = cmEd.sql;
        delete state.gridEdits[cmCi];
        execStatements(sqls);
        return;
      }
      if (t.closest('.rgsql-grid-export')) {
        var exEl = t.closest('.rgsql-grid-export');
        var exCi = parseInt(exEl.dataset.card, 10);
        var exEd = getGridEdit(exCi);
        if (!exEd) return;
        var fmt = prompt('导出格式：输入 csv 或 json', 'csv');
        if (!fmt) return;
        fmt = fmt.toLowerCase();
        if (fmt === 'csv') {
          downloadFile((exEd.table || 'result') + '.csv', exportCsv(exEd.columns, exEd.rows), 'text/csv');
        } else if (fmt === 'json') {
          var jsonData = exEd.rows.map(function (row) {
            var obj = {}; exEd.columns.forEach(function (c, i) { obj[c] = row[i]; }); return obj;
          });
          downloadFile((exEd.table || 'result') + '.json', JSON.stringify(jsonData, null, 2), 'application/json');
        } else { toast('不支持格式: ' + fmt, 'error'); }
        return;
      }
      if (t.closest('.rgsql-refresh')) { refreshDbList(); return; }
      if (t.closest('.rgsql-stop')) {
        state.stopOnError = !state.stopOnError;
        lsSet('rgsql.stopOnError', state.stopOnError);
        renderHead();
        return;
      }
      // ====== 规则增删 ======
      if (t.closest('.rgsql-add-assert')) { addAssertion(); renderAssertionsTab(); return; }
      if (t.closest('.rgsql-add-extr')) { addExtraction(); renderExtractionsTab(); return; }
      var delAssert = t.closest('.rgsql-del-assert');
      if (delAssert) { removeRule('assertions', delAssert.dataset.idx, delAssert.dataset.ridx); renderAssertionsTab(); return; }
      var delExtr = t.closest('.rgsql-del-extr');
      if (delExtr) { removeRule('extractions', delExtr.dataset.idx, delExtr.dataset.ridx); renderExtractionsTab(); return; }
    });

    panel.addEventListener('dblclick', function (e) {
      var td = e.target.closest('td[data-card][data-row][data-col]');
      if (!td) return;
      var ci = parseInt(td.dataset.card, 10);
      var ri = parseInt(td.dataset.row, 10);
      var col = parseInt(td.dataset.col, 10);
      var ed = getGridEdit(ci);
      if (!ed) return;
      if (td.querySelector('input')) return;
      var oldVal = ed.rows[ri][col];
      var input = document.createElement('input');
      input.type = 'text';
      input.value = oldVal === null ? '' : String(oldVal);
      input.className = 'rgsql-cell-input';
      td.textContent = '';
      td.appendChild(input);
      input.focus(); input.select();
      input.addEventListener('blur', function () {
        var nv = input.value;
        if (nv.toUpperCase() === 'NULL') nv = null;
        ed.rows[ri][col] = nv;
        if (ed.rowStatus[ri] === 'orig') ed.rowStatus[ri] = 'modified';
        ed.dirty = true;
        renderResults();
      });
      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
        if (ev.key === 'Escape') { input.value = oldVal === null ? '' : String(oldVal); input.blur(); }
      });
    });

    panel.addEventListener('change', function (e) {
      var t = e.target;
      if (t.classList.contains('rgsql-db')) {
        var opt = t.options[t.selectedIndex];
        if (opt && opt.classList.contains('rgsql-db-item')) {
          state.selectedDb = opt.dataset.server;
          lsSet('rgsql.db.' + String(state.targetId || '_default'), state.selectedDb);
          renderHead();
          state.schema.tables = [];
          state.schema.selectedTable = '';
          state.schema.expanded = {};
          renderSchemaBarUpdate();
          setTimeout(loadSchema, 150);
        }
        return;
      }
      if (t.classList.contains('rgsql-limit')) {
        state.rowLimit = parseInt(t.value, 10) || 200;
        lsSet('rgsql.rowlimit', state.rowLimit);
        return;
      }
      // ====== 规则编辑 ======
      var ruleEd = t.closest('[data-rule-group]');
      if (ruleEd) {
        var group = ruleEd.dataset.ruleGroup; // assertions | extractions
        var sIdx = parseInt(ruleEd.dataset.idx, 10);
        var rIdx = parseInt(ruleEd.dataset.ridx, 10);
        var field = t.dataset.field;
        var val = t.tagName === 'SELECT' ? t.value : t.value;
        var rules = state[group] || {};
        if (!rules[sIdx]) rules[sIdx] = [];
        rules[sIdx][rIdx][field] = val;
        state[group] = rules;
        lsSet('rgsql.' + group, rules);
      }
    });
  }

  function refreshDbList() {
    var envId = state.envId != null ? state.envId
      : (state.targetInfo && state.targetInfo.env_info ? state.targetInfo.env_info.env_id : 0);
    var base = manageBaseUrl();
    var url = base + '/management/api/v1/target/get_sql_database_list';
    var body = JSON.stringify({ team_id: teamId(), env_id: envId });
    var xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', 'application/json');
    // 和 RunnerGo 原生 XHR 完全一致的 headers
    var t = djangoToken();
    if (t) xhr.setRequestHeader('Authorization', t);  // 注意：无 "Bearer " 前缀！
    xhr.setRequestHeader('CurrentTeamID', teamId());
    xhr.onload = function () {
      try {
        var j = JSON.parse(xhr.responseText);
        if (j && j.code === 0 && Array.isArray(j.data)) {
          state.dbs = j.data.filter(function (d) { return (d.type || 'mysql') === 'mysql'; });
          renderHead();
          toast('已加载 ' + state.dbs.length + ' 个数据库连接', 'success');
        } else {
          toast('加载数据库列表失败：' + (j && (j.message || j.em) ? (j.message || j.em) : '未知错误') +
            ' (code:' + (j && j.code) + ', env:' + envId + ')', 'error');
        }
      } catch (e) { toast('加载数据库列表失败（解析错误）: ' + e.message, 'error'); }
    };
    xhr.onerror = function () { toast('加载数据库列表失败（网络错误）', 'error'); };
    xhr.send(body);
  }

  /* -------------------------- 执行按钮 / 快捷键 -------------------------- */

  function hookExecuteButton() {
    document.addEventListener('click', function (e) {
      var root = sqlRoot();
      if (!root || !root.contains(e.target)) return;
      var btn = e.target.closest('button');
      if (!btn || btn.closest('.rgsql-console')) return;
      var text = (btn.textContent || '').trim();
      if (text === '执行') {
        e.preventDefault(); e.stopImmediatePropagation(); e.stopPropagation();
        runExecute(false);
      }
    }, true);
  }

  function hookShortcut() {
    document.addEventListener('keydown', function (e) {
      if (!(e.ctrlKey || e.metaKey) || e.key !== 'Enter') return;
      var root = sqlRoot();
      if (!root) return;
      var ed = pickEditorInSqlRoot();
      if (!ed) return;
      e.preventDefault(); e.stopImmediatePropagation(); e.stopPropagation();
      runExecute(false);
    }, true);
  }

  /* -------------------------------- 启动 -------------------------------- */

  function boot() {
    injectStyle();
    watchMonaco();
    hookXhr();
    hookExecuteButton();
    hookShortcut();

    // 调试/测试钩子
    try {
      window.RGSqlConsole = {
        version: VERSION,
        state: state,
        run: runExecute,
        runSelected: function () { runExecute(true); },
        refresh: refreshDbList,
        split: splitSql,
        currentSql: currentSqlText,
        connection: currentConnection,
        debugDom: _debugDom,
        hideResponse: function () { _hideRunnerGoResponse(sqlRoot()); }
      };
    } catch (e) { }

    // 恢复该节点上次选择的数据库
    var savedDb = lsGet('rgsql.db.' + String(state.targetId || '_default'), '');
    if (savedDb && !state.selectedDb) state.selectedDb = savedDb;

    // SQL 节点页出现/切换时挂载面板
    setInterval(function () {
      if (sqlRoot()) {
        ensurePanel();
      }
    }, 500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
