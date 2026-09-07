/* cURL 导入接口 - 解析 cURL 命令并保存到 RunnerGo API 测试模块
 * 保存接口调用 manage 服务的 /management/api/v1/target/save_import_api，
 * 与 web-ui 内置「导入」功能使用同一后端接口与数据结构。 */
(function () {
  'use strict';

  /* ---------- cURL 词法解析（处理引号、转义、续行、$'...'） ---------- */

  function tokenize(input) {
    var tokens = [];
    var cur = '';
    var hasCur = false;
    var n = input.length;
    var i = 0;
    function push() { if (hasCur) { tokens.push(cur); cur = ''; hasCur = false; } }
    while (i < n) {
      var ch = input[i];
      if (ch === '\\' && (input[i + 1] === '\n')) { i += 2; continue; }
      if (ch === '\\' && input[i + 1] === '\r' && input[i + 2] === '\n') { i += 3; continue; }
      if (ch === '\n' || ch === '\r' || ch === ' ' || ch === '\t') { push(); i++; continue; }
      if (ch === "'") {
        hasCur = true; i++;
        while (i < n && input[i] !== "'") { cur += input[i]; i++; }
        i++; continue;
      }
      if (ch === '"') {
        hasCur = true; i++;
        while (i < n && input[i] !== '"') {
          var nc = input[i + 1];
          if (input[i] === '\\' && (nc === '"' || nc === '\\' || nc === '$' || nc === '`')) { cur += nc; i += 2; continue; }
          if (input[i] === '\\' && nc === '\n') { i += 2; continue; }
          cur += input[i]; i++;
        }
        i++; continue;
      }
      if (ch === '\\' && i + 1 < n) { hasCur = true; cur += input[i + 1]; i += 2; continue; }
      if (ch === '$' && input[i + 1] === "'") { // ANSI-C 引号
        hasCur = true; i += 2;
        var map = { n: '\n', t: '\t', r: '\r', '\\': '\\', "'": "'", '"': '"', a: '\x07', b: '\b', f: '\f', v: '\v' };
        while (i < n && input[i] !== "'") {
          if (input[i] === '\\' && (input[i + 1] in map)) { cur += map[input[i + 1]]; i += 2; continue; }
          cur += input[i]; i++;
        }
        i++; continue;
      }
      hasCur = true; cur += ch; i++;
    }
    push();
    return tokens;
  }

  /* 将整段文本按 curl 命令边界拆分为多组 token */
  function splitCommands(input) {
    var tokens = tokenize(input);
    var commands = [];
    var current = null;
    tokens.forEach(function (tk) {
      if (tk === 'curl' || /\/curl(\.exe)?$/.test(tk)) {
        if (current && current.length > 1) commands.push(current);
        current = [tk];
      } else if (current) {
        current.push(tk);
      }
    });
    if (current && current.length > 1) commands.push(current);
    return commands.filter(function (c) { return c.length > 1; });
  }

  /* ---------- cURL 命令解析为结构化请求 ---------- */

  var BOOL_FLAGS = ['-L', '--location', '-k', '--insecure', '-s', '--silent', '-S', '--show-error',
    '--compressed', '-i', '--include', '-v', '--verbose', '-#', '--progress-bar',
    '-4', '-6', '--http1.1', '--http2', '-g', '--globoff'];

  function parseCommand(tokens) {
    var req = {
      method: null, url: null, headers: [],
      dataParts: [], dataUrlEncode: [], formParts: [],
      user: null, cookie: null, head: false, getFlag: false
    };
    for (var k = 1; k < tokens.length; k++) {
      var t = tokens[k];
      var next = function () { return tokens[++k]; };
      if (t === '-X' || t === '--request') { req.method = (next() || '').toUpperCase(); }
      else if (t === '-H' || t === '--header') { req.headers.push(next() || ''); }
      else if (t === '-A' || t === '--user-agent') { req.headers.push('User-Agent: ' + (next() || '')); }
      else if (t === '-e' || t === '--referer') { req.headers.push('Referer: ' + (next() || '')); }
      else if (t === '-d' || t === '--data' || t === '--data-raw' || t === '--data-ascii' || t === '--data-binary') { req.dataParts.push(next() || ''); }
      else if (t === '--data-urlencode') { req.dataUrlEncode.push(next() || ''); }
      else if (t === '-F' || t === '--form' || t === '--form-string') { req.formParts.push(next() || ''); }
      else if (t === '-u' || t === '--user') { req.user = next() || ''; }
      else if (t === '-b' || t === '--cookie') { req.cookie = next() || ''; }
      else if (t === '-I' || t === '--head') { req.head = true; }
      else if (t === '-G' || t === '--get') { req.getFlag = true; }
      else if (t === '--url') { if (!req.url) req.url = next() || null; }
      else if (BOOL_FLAGS.indexOf(t) >= 0) { /* 忽略 */ }
      else if (/^-[A-Za-z]{2,}$/.test(t)) { // 组合短参数，如 -skL
        for (var j = 1; j < t.length; j++) { if (t[j] === 'I') req.head = true; if (t[j] === 'G') req.getFlag = true; }
      }
      else if (/^-/.test(t)) { if (tokens[k + 1] && !/^-/.test(tokens[k + 1])) k++; /* 跳过带值的未知参数 */ }
      else if (!req.url) { req.url = t; }
    }
    if (!req.url) return null;
    if (/^\/\//.test(req.url)) req.url = 'http:' + req.url;
    else if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(req.url)) req.url = 'http://' + req.url;
    var u;
    try { u = new URL(req.url); } catch (e) { return null; }
    if (!req.method && req.head) req.method = 'HEAD';
    var hasBody = (req.dataParts.length || req.formParts.length || req.dataUrlEncode.length) > 0;
    if (!req.method) req.method = hasBody && !req.getFlag ? 'POST' : 'GET';

    var headerList = [];
    var contentType = '';
    req.headers.forEach(function (h) {
      var m = /^([^:]+):(.*)$/.exec(h.trim());
      if (!m) return;
      var key = m[1].trim(), val = m[2].trim();
      if (!key) return;
      if (key.toLowerCase() === 'content-type' && !contentType) contentType = val;
      if (key.toLowerCase() === 'authorization' && req.user) return; // -u 优先走 basic 认证
      headerList.push({ key: key, value: val });
    });
    if (req.cookie) headerList.push({ key: 'Cookie', value: req.cookie });
    var query = [];
    u.searchParams.forEach(function (v, key) { query.push({ key: key, value: v }); });
    if (req.getFlag && (req.dataParts.length || req.dataUrlEncode.length)) {
      var merged = req.dataParts.join('&') + (req.dataParts.length && req.dataUrlEncode.length ? '&' : '') + req.dataUrlEncode.join('&');
      merged.split('&').forEach(function (pair) {
        if (!pair) return;
        var eq = pair.indexOf('=');
        var key = eq >= 0 ? pair.slice(0, eq) : pair;
        var val = eq >= 0 ? pair.slice(eq + 1) : '';
        try { key = decodeURIComponent(key); val = decodeURIComponent(val); } catch (e) { }
        query.push({ key: key, value: val });
      });
    }
    return {
      method: req.method,
      scheme: u.protocol.replace(':', ''),
      host: u.host,
      path: u.pathname || '/',
      url: req.url,
      query: query,
      headers: headerList,
      contentType: contentType,
      // -G 模式下 data 已并入 query，不再生成 body
      body: buildBody(Object.assign({}, req, req.getFlag ? { dataParts: [], dataUrlEncode: [], formParts: [] } : {}), contentType),
      auth: parseAuth(req)
    };
  }

  function splitKV(text, sep) {
    var out = [];
    text.split(sep).forEach(function (pair) {
      if (!pair) return;
      var eq = pair.indexOf('=');
      var key = eq >= 0 ? pair.slice(0, eq) : pair;
      var val = eq >= 0 ? pair.slice(eq + 1) : '';
      try { key = decodeURIComponent(key); } catch (e) { }
      try { val = decodeURIComponent(val); } catch (e) { }
      out.push({ key: key, value: val });
    });
    return out;
  }

  function buildBody(req, contentType) {
    var ct = (contentType || '').toLowerCase();
    if (req.formParts.length) {
      var params = req.formParts.map(function (p) {
        var eq = p.indexOf('=');
        var key = eq >= 0 ? p.slice(0, eq) : p;
        var val = eq >= 0 ? p.slice(eq + 1) : '';
        var isFile = val.charAt(0) === '@';
        return { is_checked: 1, type: isFile ? 'File' : 'Text', key: key, value: isFile ? val.slice(1) : val, not_null: 1, description: '', field_type: 'Text' };
      });
      return { mode: 'form-data', parameter: params, raw: '', raw_para: [] };
    }
    var data = req.dataParts.join('');
    if (req.dataUrlEncode.length) {
      data = (data ? data + '&' : '') + req.dataUrlEncode.join('&');
    }
    if (!data) return { mode: 'none', parameter: [], raw: '', raw_para: [] };
    // multipart 正文体（从 DevTools 复制 --boundary 块）按 form-data 处理
    if (/multipart\/form-data/.test(ct) || /^--.{8,}\r?\n/.test(data)) {
      var formParams = parseMultipart(data);
      if (formParams.length) return { mode: 'form-data', parameter: formParams, raw: '', raw_para: [] };
    }
    if (/x-www-form-urlencoded/.test(ct) || (req.dataUrlEncode.length && !ct) ||
      (!ct && /^[^=&\s]+=[^=]*(&[^=&\s]+=[^=]*)*$/.test(data) && data.indexOf('=') >= 0)) {
      return { mode: 'urlencoded', parameter: splitKV(data, '&').map(function (kv) {
        return { is_checked: 1, type: 'Text', key: kv.key, value: kv.value, not_null: 1, description: '', field_type: 'Text' };
      }), raw: '', raw_para: [] };
    }
    var trimmed = data.trim();
    if (/xml/.test(ct)) return { mode: 'xml', parameter: [], raw: data, raw_para: [] };
    if (/json/.test(ct) || /^\{[\s\S]*\}$/.test(trimmed) || /^\[[\s\S]*\]$/.test(trimmed)) {
      return { mode: 'json', parameter: [], raw: data, raw_para: [] };
    }
    // 平台对原始文本 body 统一用 json 模式承载（与 yApi 导入逻辑一致）
    return { mode: 'json', parameter: [], raw: data, raw_para: [] };
  }

  function parseMultipart(data) {
    var m = /^--([^\r\n]+)/.exec(data);
    if (!m) return [];
    var boundary = m[1].replace(/\s+$/, '');
    var params = [];
    data.split('--' + boundary).forEach(function (part) {
      var nameM = /name="([^"]*)"/.exec(part);
      if (!nameM) return;
      var isFile = /filename="/.test(part);
      var sepM = /\r?\n\r?\n/.exec(part);
      var val = '';
      if (sepM) {
        val = part.slice(sepM.index + sepM[0].length);
        val = val.replace(/\r\n$/, '').replace(/\n$/, ''); // 分隔符前的换行属于 boundary
      }
      params.push({ is_checked: 1, type: isFile ? 'File' : 'Text', key: nameM[1], value: val, not_null: 1, description: '', field_type: 'Text' });
    });
    return params;
  }

  function parseAuth(req) {
    if (req.user) {
      var eq = req.user.indexOf(':');
      return { type: 'basic', kv: { key: '', value: '' }, bearer: { key: '' },
        basic: { username: eq >= 0 ? req.user.slice(0, eq) : req.user, password: eq >= 0 ? req.user.slice(eq + 1) : '' } };
    }
    return { type: 'noauth', kv: { key: '', value: '' }, bearer: { key: '' }, basic: { username: '', password: '' } };
  }

  /* ---------- 构造 save_import_api 请求体 ---------- */

  function uuidV4() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0; var v = c === 'x' ? r : (r & 0x3 | 0x8); return v.toString(16);
    });
  }

  function deriveName(parsed) {
    var segs = parsed.path.split('/').filter(Boolean);
    var last = segs.length ? segs[segs.length - 1] : '';
    if (/^\{\d+\}$/.test(last) || /^\d+$/.test(last) || !last) last = segs.length >= 2 ? segs[segs.length - 2] : parsed.host;
    try { last = decodeURIComponent(last); } catch (e) { }
    return last || '新建接口';
  }

  function buildApiItem(parsed, name) {
    var now = Math.floor(Date.now() / 1000);
    var updateDay = Math.floor(new Date(new Date().toLocaleDateString()).getTime() / 1000);
    var targetId = uuidV4();
    var kvItem = function (kv) {
      return { is_checked: 1, type: 'Text', key: kv.key, value: kv.value, not_null: 1, description: '', field_type: 'Text' };
    };
    return {
      name: name || deriveName(parsed),
      target_type: 'api',
      method: parsed.method,
      url: parsed.url,
      request: {
        auth: parsed.auth,
        body: parsed.body,
        cookie: { parameter: [] },
        description: '',
        event: { pre_script: '', test: '' },
        header: { parameter: parsed.headers.map(kvItem) },
        query: { parameter: parsed.query.map(function (kv) {
          return { is_checked: 1, type: 'Text', key: kv.key, value: kv.value, not_null: 2, description: '', field_type: 'Text' };
        }) },
        resful: { parameter: [] },
        url: parsed.url
      },
      response: { success: { parameter: [], raw: '' }, error: { parameter: [], raw: '' } },
      mock: '{}',
      mock_url: '',
      assert: [],
      regex: [],
      is_changed: -1,
      mark: 'developing',
      parent_id: '0',
      project_id: '-1',
      sort: -1,
      target_id: targetId,
      old_target_id: targetId,
      old_parent_id: '0',
      type_sort: 1,
      version: 1,
      status: 1,
      update_day: updateDay,
      update_dtime: now,
      create_dtime: now
    };
  }

  /* ---------- 鉴权信息 ---------- */

  function getTokenCookie() {
    var m = /(?:^|;\s*)token=([^;]*)/.exec(document.cookie);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function manageHeaders(teamId) {
    return {
      'Content-Type': 'application/json',
      'Authorization': getTokenCookie(),
      'CurrentTeamID': teamId || '0'
    };
  }

  function fetchTeamId() {
    return fetch('/management/api/v1/setting/get', { method: 'GET', headers: manageHeaders('0'), credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res && res.code === 20003) throw new Error('需要登录：请先打开「API测试」页面登录，再使用本页导入。');
        if (!res || res.code !== 0) throw new Error((res && (res.et || res.em)) || '获取项目信息失败');
        var tid = res.data && res.data.settings && res.data.settings.current_team_id;
        if (!tid) throw new Error('未找到当前项目（team）信息，请先在「API测试」页面选择项目。');
        return tid;
      });
  }

  function saveApis(items, teamId) {
    return fetch('/management/api/v1/target/save_import_api', {
      method: 'POST',
      headers: manageHeaders(teamId),
      credentials: 'same-origin',
      body: JSON.stringify({ team_id: teamId, apis: items })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (!res || res.code !== 0) throw new Error((res && (res.et || res.em)) || '保存失败');
      return res;
    });
  }

  /* ---------- 页面交互 ---------- */

  var parsedApis = []; // {parsed, item}

  var el = function (id) { return document.getElementById(id); };
  var curlInput = el('curlInput'), parseBtn = el('parseBtn'), clearBtn = el('clearBtn');
  var parseStatus = el('parseStatus'), previewPanel = el('previewPanel'), previewList = el('previewList');
  var savePanel = el('savePanel'), saveBtn = el('saveBtn'), saveStatus = el('saveStatus'), saveResult = el('saveResult');

  function setStatus(node, text, cls) { node.textContent = text; node.className = 'status' + (cls ? ' ' + cls : ''); }

  parseBtn.addEventListener('click', function () {
    var raw = curlInput.value.trim();
    if (!raw) { setStatus(parseStatus, '请先粘贴 cURL 命令', 'err'); return; }
    var commands = splitCommands(raw);
    if (!commands.length) { setStatus(parseStatus, '未识别到 cURL 命令（命令需以 curl 开头）', 'err'); return; }
    parsedApis = [];
    var failed = 0;
    commands.forEach(function (tokens) {
      var parsed = parseCommand(tokens);
      if (!parsed) { failed++; return; }
      parsedApis.push({ parsed: parsed, item: buildApiItem(parsed) });
    });
    if (!parsedApis.length) { setStatus(parseStatus, 'cURL 解析失败：未找到有效的请求 URL', 'err'); return; }
    renderPreview();
    previewPanel.hidden = false;
    savePanel.hidden = false;
    saveResult.hidden = true;
    setStatus(parseStatus, '已解析 ' + parsedApis.length + ' 个接口' + (failed ? '，' + failed + ' 条命令无法解析' : ''), failed ? 'err' : 'ok');
  });

  clearBtn.addEventListener('click', function () {
    curlInput.value = '';
    parsedApis = [];
    previewPanel.hidden = true;
    savePanel.hidden = true;
    saveResult.hidden = true;
    setStatus(parseStatus, '', '');
  });

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  function renderPreview() {
    previewList.innerHTML = '';
    parsedApis.forEach(function (api, idx) {
      var p = api.parsed;
      var card = document.createElement('div');
      card.className = 'api-card';
      var bodyDesc = '无';
      if (p.body.mode !== 'none') {
        bodyDesc = p.body.mode;
        if (p.body.mode === 'json' || p.body.mode === 'xml') bodyDesc += ' · ' + p.body.raw.length + ' 字符';
        else bodyDesc += ' · ' + p.body.parameter.length + ' 个参数';
      }
      card.innerHTML =
        '<div class="api-card-head">' +
        '<span class="method-badge m-' + esc(p.method) + '">' + esc(p.method) + '</span>' +
        '<input class="api-name" type="text" value="' + esc(api.item.name) + '" title="接口名称">' +
        '<button class="api-card-remove" type="button" title="移除">&times;</button>' +
        '</div>' +
        '<p class="api-url">' + esc(p.url) + '</p>' +
        '<div class="api-meta"><span>Header ' + p.headers.length + '</span><span>Query ' + p.query.length + '</span><span>Body: ' + esc(bodyDesc) + '</span></div>' +
        '<div class="api-detail"></div>';
      var nameInput = card.querySelector('.api-name');
      nameInput.addEventListener('input', function () { api.item.name = nameInput.value; });
      card.querySelector('.api-card-remove').addEventListener('click', function () {
        parsedApis.splice(idx, 1);
        if (!parsedApis.length) {
          previewPanel.hidden = true;
          savePanel.hidden = true;
          setStatus(parseStatus, '', '');
        } else { renderPreview(); }
      });
      var metaRow = card.querySelector('.api-meta');
      var toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'api-detail-toggle';
      toggle.textContent = '展开详情';
      toggle.addEventListener('click', function () { card.classList.toggle('open'); });
      metaRow.appendChild(toggle);
      var detail = card.querySelector('.api-detail');
      var html = '';
      if (p.headers.length) {
        html += '<table class="kv-table"><tr><th>Header</th><th>Value</th></tr>';
        p.headers.forEach(function (h) { html += '<tr><td>' + esc(h.key) + '</td><td>' + esc(h.value) + '</td></tr>'; });
        html += '</table>';
      }
      if (p.query.length) {
        html += '<table class="kv-table"><tr><th>Query</th><th>Value</th></tr>';
        p.query.forEach(function (h) { html += '<tr><td>' + esc(h.key) + '</td><td>' + esc(h.value) + '</td></tr>'; });
        html += '</table>';
      }
      if (p.body.mode !== 'none') {
        html += '<div style="margin-top:6px;font-size:12px;color:#909399">Body（' + esc(p.body.mode) + '）</div>';
        if (p.body.mode === 'json' || p.body.mode === 'xml') {
          html += '<pre class="body-raw">' + esc(p.body.raw) + '</pre>';
        } else {
          html += '<table class="kv-table"><tr><th>Key</th><th>Value</th><th>类型</th></tr>';
          p.body.parameter.forEach(function (kv) { html += '<tr><td>' + esc(kv.key) + '</td><td>' + esc(kv.value) + '</td><td>' + esc(kv.type) + '</td></tr>'; });
          html += '</table>';
        }
      }
      if (p.auth.type === 'basic') {
        html += '<table class="kv-table"><tr><th>认证</th><th></th></tr><tr><td>Basic Auth</td><td>' + esc(p.auth.basic.username) + ' / ********</td></tr></table>';
      }
      detail.innerHTML = html || '<div style="font-size:12px;color:#909399">无 Header / Query / Body</div>';
      previewList.appendChild(card);
    });
  }

  saveBtn.addEventListener('click', function () {
    saveBtn.disabled = true;
    setStatus(saveStatus, '保存中…', '');
    fetchTeamId().then(function (teamId) {
      var items = parsedApis.map(function (api) { return api.item; });
      return saveApis(items, teamId);
    }).then(function () {
      setStatus(saveStatus, '导入成功', 'ok');
      saveResult.hidden = false;
      try { window.parent.postMessage({ type: 'curl-imported' }, '*'); } catch (e) { }
    }).catch(function (err) {
      setStatus(saveStatus, err && err.message ? err.message : '导入失败', 'err');
    }).finally(function () { saveBtn.disabled = false; });
  });

  el('gotoApiBtn').addEventListener('click', function (evt) {
    if (evt && evt.preventDefault) evt.preventDefault();
    try { window.parent.postMessage({ type: 'curl-import-close', route: '#/apis' }, '*'); } catch (e) { }
  });
})();
