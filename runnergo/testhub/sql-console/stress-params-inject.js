(function () {
  'use strict';

  const API_BASE = '/auto-test/api';
  let fetchIntercepted = false;

  function randomPhone() {
    const prefixes = ['133', '149', '150', '151', '152', '153', '155', '156', '157', '158', '159',
      '166', '170', '176', '177', '178', '180', '181', '182', '183', '184', '185', '186', '187', '188', '189'];
    const prefix = prefixes[Math.floor(Math.random() * prefixes.length)];
    let suffix = '';
    for (let i = 0; i < 8; i++) suffix += Math.floor(Math.random() * 10);
    return prefix + suffix;
  }

  function randomCode(len = 6) {
    let code = '';
    for (let i = 0; i < len; i++) code += Math.floor(Math.random() * 10);
    return code;
  }

  function randomIdCard() {
    const provinces = ['110101', '310101', '440101', '440301', '330101', '420101', '510101', '320101', '500101'];
    const prov = provinces[Math.floor(Math.random() * provinces.length)];
    const year = 1960 + Math.floor(Math.random() * 40);
    const month = String(1 + Math.floor(Math.random() * 12)).padStart(2, '0');
    const day = String(1 + Math.floor(Math.random() * 28)).padStart(2, '0');
    let seq = '';
    for (let i = 0; i < 3; i++) seq += Math.floor(Math.random() * 10);
    const base = prov + year + month + day + seq;
    const weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2];
    const checkMap = ['1', '0', 'X', '9', '8', '7', '6', '5', '4', '3', '2'];
    let sum = 0;
    for (let i = 0; i < 17; i++) sum += parseInt(base[i]) * weights[i];
    return base + checkMap[sum % 11];
  }

  function randomEmail() {
    const names = ['user', 'test', 'admin', 'dev', 'qa', 'ops', 'tmp', 'demo'];
    const domains = ['gmail.com', 'qq.com', '163.com', '126.com', 'outlook.com', 'foxmail.com'];
    return names[Math.floor(Math.random() * names.length)] + Date.now().toString(36) + '@' + domains[Math.floor(Math.random() * domains.length)];
  }

  function randomName() {
    const surnames = ['张', '王', '李', '赵', '刘', '陈', '杨', '黄', '周', '吴', '徐', '孙', '马', '朱', '胡', '林', '何', '高', '罗', '郑'];
    const given = ['伟', '芳', '娜', '敏', '静', '丽', '强', '磊', '洋', '勇', '军', '杰', '涛', '明', '超', '秀', '霞', '平', '刚', '桂'];
    return surnames[Math.floor(Math.random() * surnames.length)] + given[Math.floor(Math.random() * given.length)] + given[Math.floor(Math.random() * given.length)];
  }

  function randomPlate() {
    const provinces = ['京', '沪', '粤', '浙', '苏', '鲁', '川', '鄂', '湘', '闽', '陕', '豫', '冀', '皖', '赣', '桂', '晋', '蒙', '辽', '吉', '黑', '云', '贵', '渝', '津'];
    const letters = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
    let plate = provinces[Math.floor(Math.random() * provinces.length)] + letters[Math.floor(Math.random() * letters.length)];
    for (let i = 0; i < 5; i++) {
      const r = Math.random();
      if (r < 0.5) plate += Math.floor(Math.random() * 10);
      else plate += letters[Math.floor(Math.random() * letters.length)];
    }
    return plate;
  }

  function randomString(len = 8) {
    const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
    let s = '';
    for (let i = 0; i < len; i++) s += chars[Math.floor(Math.random() * chars.length)];
    return s;
  }

  function randomAmount() {
    return (Math.random() * 10000).toFixed(2);
  }

  function generateRandomValue(elementLabel, defaultValue, inputType) {
    const label = String(elementLabel || '').toLowerCase();
    const dv = String(defaultValue || '');
    const it = String(inputType || '').toLowerCase();

    if (label.includes('手机') || label.includes('phone') || label.includes('电话') || it === 'tel') return randomPhone();
    if (label.includes('验证码') || label.includes('code') || label.includes('captcha')) return randomCode(6);
    if (label.includes('身份证') || label.includes('idcard') || label.includes('id_card')) return randomIdCard();
    if (label.includes('邮箱') || label.includes('email') || label.includes('mail')) return randomEmail();
    if (label.includes('姓名') || label.includes('name') || label.includes('用户名')) return randomName();
    if (label.includes('车牌') || label.includes('plate') || label.includes('车牌号')) return randomPlate();
    if (label.includes('金额') || label.includes('amount') || label.includes('price') || label.includes('价格')) return randomAmount();
    if (label.includes('密码') || label.includes('password') || label.includes('pwd')) return randomString(12);
    if (it === 'number' || it === 'tel') return randomCode(6);
    return randomString(10);
  }

  function parseVarRef(value) {
    if (typeof value !== 'string') return null;
    const m = value.match(/^\$\{([^}]+)\}$/);
    if (!m) return null;
    const expr = m[1].trim();
    if (expr.startsWith('random_') || expr.startsWith('uuid') || expr.startsWith('timestamp')) return { builtin: true, expr };
    const parts = expr.split('.');
    return { path: parts, raw: expr };
  }

  function setNestedPath(obj, path, value) {
    let cur = obj;
    for (let i = 0; i < path.length - 1; i++) {
      if (!cur[path[i]] || typeof cur[path[i]] !== 'object') cur[path[i]] = {};
      cur = cur[path[i]];
    }
    cur[path[path.length - 1]] = value;
  }

  function getProjectId() {
    try {
      if (typeof window.currentProject === 'string' && window.currentProject) return window.currentProject;
    } catch (e) {}
    const sel = document.getElementById('globalProj');
    if (sel && sel.value) return sel.value;
    return 'default';
  }

  async function fetchInputFields(caseFile, projectId) {
    const resp = await fetch(`${API_BASE}/runtime-case/scan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId, module: 'ui', case_file: caseFile }),
    });
    if (!resp.ok) throw new Error(`扫描失败: ${resp.status} ${resp.statusText}`);
    return resp.json();
  }

  function getSelectedCases() {
    const checks = document.querySelectorAll('#loadTestCaseList input[data-scenario]:checked');
    return Array.from(checks).map(c => c.dataset.scenario);
  }

  function setupFetchInterceptor() {
    if (fetchIntercepted) return;
    const originalFetch = window.fetch;
    window.fetch = function (url, opts) {
      const urlStr = String(url || '');
      const method = String((opts && opts.method) || 'GET').toUpperCase();
      if (urlStr.includes('/load-tests/runs') && method === 'POST' && opts && opts.body) {
        try {
          const body = JSON.parse(opts.body);
          const cb = document.getElementById('stressAutoRandomize');
          if (cb && cb.checked) {
            body.auto_randomize = true;
            opts.body = JSON.stringify(body);
          }
        } catch (e) {}
      }
      return originalFetch.call(this, url, opts);
    };
    fetchIntercepted = true;
  }

  function buildParamsTable(fields, caseFile) {
    const existing = document.getElementById('stressParamsTable');
    if (existing) existing.remove();

    const container = document.createElement('div');
    container.id = 'stressParamsTable';
    container.className = 'mt-4 border rounded-lg overflow-hidden';

    const header = document.createElement('div');
    header.className = 'flex items-center justify-between p-3 bg-indigo-50 border-b';
    header.innerHTML = `
      <div class="flex items-center gap-2">
        <span class="text-sm font-semibold text-indigo-800">参数化输入框</span>
        <span class="badge bg-indigo-100 text-indigo-700">${fields.length} 个</span>
        <span class="text-xs text-slate-500 truncate max-w-xs">${caseFile}</span>
      </div>
      <div class="flex items-center gap-2">
        <button type="button" id="stressRegenBtn" class="bg-amber-500 text-white rounded px-2 py-1 text-xs hover:bg-amber-600">重新生成</button>
        <button type="button" id="stressApplyDataRowsBtn" class="bg-green-600 text-white rounded px-2 py-1 text-xs hover:bg-green-700">应用到数据行</button>
        <button type="button" id="stressApplyVarsBtn" class="bg-blue-600 text-white rounded px-2 py-1 text-xs hover:bg-blue-700">应用到运行变量</button>
        <button type="button" id="stressCloseTableBtn" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
      </div>`;
    container.appendChild(header);

    const tableWrap = document.createElement('div');
    tableWrap.className = 'max-h-80 overflow-y-auto';

    const table = document.createElement('table');
    table.className = 'w-full text-sm';

    const thead = document.createElement('thead');
    thead.className = 'bg-slate-50 border-b sticky top-0';
    thead.innerHTML = `
      <tr>
        <th class="p-2 text-left text-xs text-slate-500 w-8">#</th>
        <th class="p-2 text-left text-xs text-slate-500">元素</th>
        <th class="p-2 text-left text-xs text-slate-500">步骤</th>
        <th class="p-2 text-left text-xs text-slate-500">默认值</th>
        <th class="p-2 text-left text-xs text-slate-500">类型</th>
        <th class="p-2 text-left text-xs text-slate-500">运行值</th>
      </tr>`;
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    fields.forEach((field, idx) => {
      const tr = document.createElement('tr');
      tr.className = 'border-b hover:bg-slate-50';

      const varRef = parseVarRef(field.defaultValue);
      let typeLabel = '固定值';
      let typeClass = 'bg-slate-100 text-slate-600';
      if (varRef) {
        if (varRef.builtin) {
          typeLabel = '内置表达式';
          typeClass = 'bg-purple-100 text-purple-700';
        } else {
          typeLabel = '变量引用';
          typeClass = 'bg-blue-100 text-blue-700';
        }
      }

      const randomVal = generateRandomValue(field.element, field.defaultValue, '');

      tr.innerHTML = `
        <td class="p-2 text-xs text-slate-400">${idx + 1}</td>
        <td class="p-2 text-xs">${field.element || '-'}</td>
        <td class="p-2 text-xs text-slate-500 truncate max-w-xs" title="${field.step || ''}">${field.step || '-'}</td>
        <td class="p-2 text-xs font-mono text-slate-500 truncate max-w-xs" title="${field.defaultValue || ''}">${field.defaultValue || '-'}</td>
        <td class="p-2"><span class="badge ${typeClass}">${typeLabel}</span></td>
        <td class="p-2"><input type="text" data-step-id="${field.stepId}" data-step-path="${field.stepPath}" data-default="${encodeURIComponent(field.defaultValue || '')}" data-element="${encodeURIComponent(field.element || '')}" value="${randomVal}" class="w-full border rounded px-2 py-1 text-xs font-mono"></td>`;
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    container.appendChild(tableWrap);

    const hint = document.createElement('div');
    hint.id = 'stressParamsHint';
    hint.className = 'p-2 text-xs text-slate-400 border-t';
    hint.textContent = '勾选"自动生成随机参数"后，每次迭代会自动为固定值生成不同的随机值，无需手动填写数据行。';
    container.appendChild(hint);

    const slaParent = Array.from(document.querySelectorAll('details')).find(d => {
      const s = d.querySelector('summary');
      return s && s.textContent.includes('参数化与 SLA');
    });

    if (slaParent && slaParent.parentElement) {
      slaParent.parentElement.insertBefore(container, slaParent.nextSibling);
    } else {
      const main = document.getElementById('main');
      if (main) main.appendChild(container);
    }

    document.getElementById('stressRegenBtn').addEventListener('click', () => {
      const inputs = container.querySelectorAll('input[data-step-id]');
      inputs.forEach(inp => {
        const element = decodeURIComponent(inp.dataset.element || '');
        const dv = decodeURIComponent(inp.dataset.default || '');
        inp.value = generateRandomValue(element, dv, '');
      });
    });

    document.getElementById('stressApplyDataRowsBtn').addEventListener('click', () => applyToDataRows(fields, container));
    document.getElementById('stressApplyVarsBtn').addEventListener('click', () => applyToVariables(fields, container));
    document.getElementById('stressCloseTableBtn').addEventListener('click', () => container.remove());
  }

  function inferFieldKey(elementLabel) {
    const label = String(elementLabel || '').toLowerCase();
    if (label.includes('手机') || label.includes('phone') || label.includes('电话')) return 'phone';
    if (label.includes('验证码') || label.includes('code') || label.includes('captcha')) return 'code';
    if (label.includes('身份证') || label.includes('idcard')) return 'idcard';
    if (label.includes('邮箱') || label.includes('email') || label.includes('mail')) return 'email';
    if (label.includes('姓名') || label.includes('name')) return 'name';
    if (label.includes('地址') || label.includes('address')) return 'address';
    if (label.includes('金额') || label.includes('amount') || label.includes('price')) return 'amount';
    if (label.includes('密码') || label.includes('password') || label.includes('pwd')) return 'password';
    return String(elementLabel || '').replace(/[^a-zA-Z0-9_\u4e00-\u9fa5]/g, '').slice(0, 20) || 'field';
  }

  function applyToDataRows(fields, container) {
    const inputs = container.querySelectorAll('input[data-step-id]');
    const vus = parseInt(document.getElementById('loadTestVus')?.value || '1', 10);
    const dataRows = [];

    const varRefFields = [];
    inputs.forEach(inp => {
      const dv = decodeURIComponent(inp.dataset.default || '');
      const varRef = parseVarRef(dv);
      if (varRef && !varRef.builtin) varRefFields.push({ inp, varRef });
    });

    if (!varRefFields.length) {
      const fallbackFields = [];
      inputs.forEach(inp => {
        const element = decodeURIComponent(inp.dataset.element || '');
        const key = inferFieldKey(element);
        fallbackFields.push({ inp, key });
      });
      if (!fallbackFields.length) {
        alert('未找到任何输入框字段。');
        return;
      }
      for (let v = 0; v < vus; v++) {
        const row = {};
        fallbackFields.forEach(({ inp, key }) => {
          const val = generateRandomValue(
            decodeURIComponent(inp.dataset.element || ''),
            decodeURIComponent(inp.dataset.default || ''),
            ''
          );
          row[key] = val;
        });
        dataRows.push(row);
      }
      const textarea = document.getElementById('loadTestDataRows');
      if (textarea) {
        textarea.value = JSON.stringify(dataRows, null, 2);
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        const hint = document.getElementById('stressParamsHint');
        if (hint) {
          hint.textContent = `已为 ${vus} 个 VU 生成 ${dataRows.length} 行随机参数数据（按元素标签推断字段名）。`;
          hint.className = 'p-2 text-xs text-green-600 border-t';
        }
      }
      return;
    }

    for (let v = 0; v < vus; v++) {
      const row = {};
      varRefFields.forEach(({ inp, varRef }) => {
        const val = generateRandomValue(
          decodeURIComponent(inp.dataset.element || ''),
          decodeURIComponent(inp.dataset.default || ''),
          ''
        );
        const path = varRef.path;
        if (path[0] === 'data' || path[0] === 'dataAssets') {
          setNestedPath(row, path.slice(1), val);
        } else {
          setNestedPath(row, path, val);
        }
      });
      dataRows.push(row);
    }

    const textarea = document.getElementById('loadTestDataRows');
    if (textarea) {
      textarea.value = JSON.stringify(dataRows, null, 2);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      const hint = document.getElementById('stressParamsHint');
      if (hint) {
        hint.textContent = `已为 ${vus} 个 VU 生成 ${dataRows.length} 行随机参数数据。`;
        hint.className = 'p-2 text-xs text-green-600 border-t';
      }
    }
  }

  function applyToVariables(fields, container) {
    const inputs = container.querySelectorAll('input[data-step-id]');
    const variables = {};
    let count = 0;

    inputs.forEach(inp => {
      const dv = decodeURIComponent(inp.dataset.default || '');
      const varRef = parseVarRef(dv);
      if (!varRef || varRef.builtin) return;

      const path = varRef.path;
      if (path[0] === 'data' || path[0] === 'dataAssets') {
        setNestedPath(variables, path.slice(1), inp.value);
      } else {
        setNestedPath(variables, path, inp.value);
      }
      count++;
    });

    if (count === 0) {
      inputs.forEach(inp => {
        const element = decodeURIComponent(inp.dataset.element || '');
        const key = inferFieldKey(element);
        variables[key] = inp.value;
        count++;
      });
    }

    if (count === 0) {
      alert('未找到任何输入框字段。');
      return;
    }

    const textarea = document.getElementById('loadTestVariables');
    if (textarea) {
      textarea.value = JSON.stringify(variables, null, 2);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      const hint = document.getElementById('stressParamsHint');
      if (hint) {
        hint.textContent = `已将 ${count} 个参数应用到运行变量 JSON。`;
        hint.className = 'p-2 text-xs text-green-600 border-t';
      }
    }
  }

  async function handleExtractAndGenerate() {
    const cases = getSelectedCases();
    if (!cases.length) {
      alert('请先勾选一条或多条业务链路 YAML');
      return;
    }

    const projectId = getProjectId();
    const btn = document.getElementById('stressExtractBtn');
    if (btn) { btn.disabled = true; btn.textContent = '提取中...'; }

    try {
      const allFields = [];
      for (const caseFile of cases) {
        const result = await fetchInputFields(caseFile, projectId);
        if (result.fields && result.fields.length) {
          result.fields.forEach(f => f._caseFile = caseFile);
          allFields.push(...result.fields);
        }
      }

      if (!allFields.length) {
        alert('未找到任何输入框（fill 步骤）');
        return;
      }

      buildParamsTable(allFields, cases.join(', '));
    } catch (err) {
      alert('提取输入框失败: ' + err.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '提取输入框并生成随机参数'; }
    }
  }

  function injectButton() {
    if (document.getElementById('stressExtractBtn')) return;

    const slaDetails = Array.from(document.querySelectorAll('details')).find(d => {
      const s = d.querySelector('summary');
      return s && s.textContent.includes('参数化与 SLA');
    });

    if (!slaDetails) return;

    const btnContainer = document.createElement('div');
    btnContainer.className = 'mt-3 mb-2 space-y-2';
    btnContainer.innerHTML = `
      <label class="flex items-center gap-2 p-2 rounded border border-indigo-200 bg-indigo-50/50 cursor-pointer" title="每次迭代自动为 fill 步骤的固定值生成不同的随机参数（手机号、验证码等），无需修改 YAML 源文件">
        <input type="checkbox" id="stressAutoRandomize" class="rounded border-slate-300 text-indigo-600 focus:ring-indigo-500">
        <span class="text-sm font-medium text-indigo-800">自动生成随机参数（每次迭代不同）</span>
        <span class="text-xs text-slate-500">勾选后每次迭代自动为固定值生成不同随机值</span>
      </label>
      <button type="button" id="stressExtractBtn" class="w-full bg-indigo-600 text-white rounded px-3 py-2 text-sm hover:bg-indigo-700 flex items-center justify-center gap-2">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        提取输入框并生成随机参数
      </button>`;

    const mt3 = slaDetails.querySelector('.mt-3');
    if (mt3) mt3.prepend(btnContainer);
    else slaDetails.appendChild(btnContainer);

    document.getElementById('stressExtractBtn').addEventListener('click', handleExtractAndGenerate);
  }

  function checkAndInject() {
    const main = document.getElementById('main');
    if (!main) return;

    const loadTestTitle = main.querySelector('h2');
    if (!loadTestTitle || !loadTestTitle.textContent.includes('全链路压测')) return;

    setupFetchInterceptor();
    injectButton();
  }

  const observer = new MutationObserver(() => {
    checkAndInject();
  });

  function startObserving() {
    const main = document.getElementById('main');
    if (main) {
      observer.observe(main, { childList: true, subtree: true });
    } else {
      setTimeout(startObserving, 500);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startObserving);
  } else {
    startObserving();
  }

  checkAndInject();
})();
