(function () {
  'use strict';
  var VERSION = '4';

  var badge = document.createElement('div');
  badge.style.cssText = 'position:fixed;bottom:5px;left:5px;z-index:99999;padding:2px 8px;border-radius:3px;font-size:11px;color:#fff;background:#4A90E2;opacity:0.7;pointer-events:none';
  badge.textContent = 'RG-Export v' + VERSION;
  if (document.body) document.body.appendChild(badge);
  else document.addEventListener('DOMContentLoaded', function () { document.body.appendChild(badge); });

  function toast(msg, type) {
    var t = document.createElement('div');
    t.style.cssText = 'position:fixed;top:20px;right:20px;z-index:99999;padding:10px 20px;border-radius:6px;font-size:14px;color:#fff;background:' +
      (type === 'error' ? '#f56c6c' : type === 'success' ? '#67c23a' : '#409eff') +
      ';box-shadow:0 4px 12px rgba(0,0,0,.2);transition:opacity .3s;opacity:0';
    t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.style.opacity = '1'; });
    setTimeout(function () { t.style.opacity = '0'; setTimeout(function () { t.remove(); }, 300); }, 2600);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function djangoToken() {
    try {
      var c = (document.cookie.match(/(?:^|;\s*)token=([^;]+)/) || [])[1] || '';
      var t = localStorage.getItem('access_token') || localStorage.getItem('token') ||
              sessionStorage.getItem('access_token') || sessionStorage.getItem('token') || c;
      return t;
    } catch (e) { return ''; }
  }

  function findExportButton() {
    var btns = document.querySelectorAll('button, a, span');
    for (var i = 0; i < btns.length; i++) {
      var text = (btns[i].textContent || '').trim();
      if (text === '导出') return btns[i];
    }
    return null;
  }

  function getWorkflowName() {
    var inputs = document.querySelectorAll('input[class*="name"], input[class*="title"], h1, h2, h3');
    for (var i = 0; i < inputs.length; i++) {
      var val = (inputs[i].value || inputs[i].textContent || '').trim();
      if (val && val.length > 0 && val.length < 50) return val;
    }
    return 'workflow';
  }

  function getWorkflowId() {
    var m = window.location.pathname.match(/workflows\/([\w-]+)/);
    if (m) return m[1];
    var m2 = window.location.hash.match(/workflows\/([\w-]+)/);
    if (m2) return m2[1];
    var els = document.querySelectorAll('[class*="id"], [data-id]');
    for (var i = 0; i < els.length; i++) {
      var v = els[i].getAttribute('data-id') || els[i].value || '';
      if (/^\d+$/.test(v) && v.length > 5) return v;
    }
    return null;
  }

  function fetchWorkflowData() {
    var id = getWorkflowId();
    if (!id) return Promise.reject('no workflow id');

    var token = djangoToken();
    var headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = token.indexOf('Bearer ') === 0 ? token : 'Bearer ' + token;

    return fetch('/api/requirement-analysis/workflows/' + id + '/', {
      method: 'GET', headers: headers, credentials: 'include'
    }).then(function (r) {
      if (!r.ok) throw new Error('API ' + r.status);
      return r.json();
    });
  }

  function extractNodesFromDOM() {
    var selectors = [
      '.react-flow__node',
      '.vue-flow__node',
      '[class*="flow-node"]',
      '[class*="node-item"]',
    ];
    var nodeEls = [];
    for (var i = 0; i < selectors.length && nodeEls.length === 0; i++) {
      nodeEls = Array.prototype.slice.call(document.querySelectorAll(selectors[i]));
    }
    if (nodeEls.length === 0) return [];

    var viewport = document.querySelector('.react-flow__viewport, .vue-flow__viewport, [class*="viewport"]');
    var vpTransform = viewport ? (viewport.style.transform || viewport.getAttribute('transform') || '') : '';
    var vpMatch = vpTransform.match(/translate\(([-\d.]+)px,\s*([-\d.]+)px\)\s*scale\(([\d.]+)\)/);
    var vpX = vpMatch ? parseFloat(vpMatch[1]) : 0;
    var vpY = vpMatch ? parseFloat(vpMatch[2]) : 0;
    var vpScale = vpMatch ? parseFloat(vpMatch[3]) : 1;
    var vpRect = viewport ? viewport.getBoundingClientRect() : { left: 0, top: 0 };

    return nodeEls.map(function (el) {
      var rect = el.getBoundingClientRect();

      var label = '';
      var labelEls = el.querySelectorAll('[class*="label"], [class*="title"]');
      if (labelEls.length > 0) {
        label = (labelEls[labelEls.length - 1].textContent || '').trim();
      }
      if (!label) {
        var spans = el.querySelectorAll('span, p');
        for (var j = 0; j < spans.length; j++) {
          var s = spans[j];
          if (s.children.length === 0) {
            var t = (s.textContent || '').trim();
            if (t) { label = t; break; }
          }
        }
      }

      var type = 'task';
      var cls = el.className || '';
      if (typeof cls !== 'string') cls = cls.baseVal || '';
      if (/start/i.test(cls)) type = 'start';
      else if (/end/i.test(cls)) type = 'end';
      else if (/gateway/i.test(cls)) type = 'gateway';
      else if (/subprocess|sub-flow/i.test(cls)) type = 'subprocess';

      if (type === 'start') label = '开始';
      else if (type === 'end') label = '结束';

      var style = window.getComputedStyle(el);
      var w = parseFloat(style.width) || rect.width;
      var h = parseFloat(style.height) || rect.height;

      return {
        id: el.getAttribute('data-id') || el.getAttribute('data-nodeid') || el.id || ('node-' + Math.random().toString(36).substr(2, 6)),
        type: type,
        label: label,
        x: (rect.left - vpRect.left - vpX) / vpScale,
        y: (rect.top - vpRect.top - vpY) / vpScale,
        w: w / vpScale,
        h: h / vpScale,
      };
    }).filter(function (n) { return n.label || n.type === 'start' || n.type === 'end'; });
  }

  function extractEdgesFromDOM(nodes) {
    var nodeMap = {};
    nodes.forEach(function (n) { nodeMap[n.id] = n; });

    var edgeContainers = document.querySelectorAll(
      '.react-flow__edge, .vue-flow__edge, [class*="edge-wrapper"], [class*="edge-container"]'
    );

    var edges = [];
    edgeContainers.forEach(function (edgeEl) {
      var sourceId = edgeEl.getAttribute('data-source') || '';
      var targetId = edgeEl.getAttribute('data-target') || '';

      if (!sourceId || !targetId) {
        var idAttr = edgeEl.getAttribute('data-id') || '';
        var m = idAttr.match(/edge[_-]([\w-]+)[_>-]([\w-]+)/);
        if (m) { sourceId = m[1]; targetId = m[2]; }
      }

      var label = '';
      var labelEl = edgeEl.querySelector('[class*="edge-label"], [class*="label-wrapper"], text');
      if (labelEl) label = (labelEl.textContent || '').trim();

      if (sourceId && targetId) {
        edges.push({ source: sourceId, target: targetId, label: label });
      }
    });

    if (edges.length === 0) {
      var pathSelectors = [
        '.react-flow__edge-path',
        '.vue-flow__edge-path',
        '.react-flow__edge path',
        '.vue-flow__edge path',
        '[class*="edge"] path',
      ];
      var paths = [];
      for (var i = 0; i < pathSelectors.length && paths.length === 0; i++) {
        paths = Array.prototype.slice.call(document.querySelectorAll(pathSelectors[i]));
      }

      paths.forEach(function (p) {
        var edgeEl = p.closest('[class*="edge"]');
        var sourceId = edgeEl ? (edgeEl.getAttribute('data-source') || '') : '';
        var targetId = edgeEl ? (edgeEl.getAttribute('data-target') || '') : '';
        var label = '';
        if (edgeEl) {
          var labelEl = edgeEl.querySelector('[class*="label"], text, span');
          if (labelEl) label = (labelEl.textContent || '').trim();
        }

        if (!sourceId || !targetId) {
          var d = p.getAttribute('d') || '';
          var startMatch = d.match(/M\s*([\d.-]+)[,\s]+([\d.-]+)/);
          var endMatch = d.match(/([\d.-]+)[,\s]+([\d.-]+)\s*$/);
          var sx = startMatch ? parseFloat(startMatch[1]) : 0;
          var sy = startMatch ? parseFloat(startMatch[2]) : 0;
          var ex = endMatch ? parseFloat(endMatch[1]) : sx;
          var ey = endMatch ? parseFloat(endMatch[2]) : sy;

          var src = findNearestNode(nodes, sx, sy);
          var tgt = src ? findNearestNodeExcluding(nodes, ex, ey, src.id) : null;
          if (src && tgt) {
            sourceId = src.id;
            targetId = tgt.id;
          }
        }

        if (sourceId && targetId) {
          edges.push({ source: sourceId, target: targetId, label: label });
        }
      });
    }

    return dedupEdges(edges);
  }

  function dedupEdges(edges) {
    var seen = {};
    var result = [];
    edges.forEach(function (e) {
      var key = e.source + '->' + e.target;
      if (!seen[key]) {
        seen[key] = true;
        result.push(e);
      }
    });
    return result;
  }

  function findNearestNode(nodes, x, y) {
    var best = null, bestDist = Infinity;
    nodes.forEach(function (n) {
      var cx = n.x + n.w / 2, cy = n.y + n.h / 2;
      var dist = Math.sqrt((cx - x) * (cx - x) + (cy - y) * (cy - y));
      if (dist < bestDist) { bestDist = dist; best = n; }
    });
    return best;
  }

  function findNearestNodeExcluding(nodes, x, y, excludeId) {
    var best = null, bestDist = Infinity;
    nodes.forEach(function (n) {
      if (n.id === excludeId) return;
      var cx = n.x + n.w / 2, cy = n.y + n.h / 2;
      var dist = Math.sqrt((cx - x) * (cx - x) + (cy - y) * (cy - y));
      if (dist < bestDist) { bestDist = dist; best = n; }
    });
    return best;
  }

  function normalizeNodes(apiNodes) {
    var typeMap = {
      'start': 'start', 'startEvent': 'start',
      'end': 'end', 'endEvent': 'end',
      'task': 'task', 'userTask': 'task', 'serviceTask': 'serviceTask',
      'gateway': 'gateway', 'exclusiveGateway': 'gateway', 'parallelGateway': 'gateway', 'inclusiveGateway': 'gateway',
      'subprocess': 'subprocess', 'subProcess': 'subprocess',
    };
    return (apiNodes || []).map(function (n) {
      var pos = n.position || { x: n.x || 0, y: n.y || 0 };
      return {
        id: String(n.id),
        type: typeMap[n.type] || n.type || 'task',
        label: n.label || n.data && n.data.label || n.name || '',
        x: pos.x,
        y: pos.y,
        w: n.width || n.w || 180,
        h: n.height || n.h || 40,
      };
    });
  }

  function normalizeEdges(apiEdges) {
    return (apiEdges || []).map(function (e) {
      return {
        source: String(e.source),
        target: String(e.target),
        label: e.label || e.data && e.data.label || '',
      };
    });
  }

  function buildSVG(nodes, edges) {
    if (nodes.length === 0) return null;

    nodes.forEach(function (n) {
      n.label = (n.label || '').replace(/事件/g, '').replace(/用户任务/g, '').trim();
      if (!n.label && n.type === 'start') n.label = '开始';
      if (!n.label && n.type === 'end') n.label = '结束';
    });

    var nodeMap = {};
    nodes.forEach(function (n) { nodeMap[n.id] = n; });

    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach(function (n) {
      minX = Math.min(minX, n.x);
      minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x + n.w);
      maxY = Math.max(maxY, n.y + n.h);
    });

    var pad = 50;
    var width = (maxX - minX) + pad * 2;
    var height = (maxY - minY) + pad * 2;

    var offsetX = pad - minX;
    var offsetY = pad - minY;

    function nodeCenter(n) {
      return { x: n.x + offsetX + n.w / 2, y: n.y + offsetY + n.h / 2 };
    }
    function nodeBorder(n, tx, ty) {
      var cx = n.x + offsetX + n.w / 2;
      var cy = n.y + offsetY + n.h / 2;
      var dx = tx - cx, dy = ty - cy;
      if (Math.abs(dx) < 0.01 && Math.abs(dy) < 0.01) return { x: cx, y: cy };
      var hw = n.w / 2, hh = n.h / 2;
      var scale = Math.min(hw / Math.abs(dx), hh / Math.abs(dy));
      return { x: cx + dx * scale, y: cy + dy * scale };
    }

    var parts = [];
    parts.push('<svg xmlns="http://www.w3.org/2000/svg" width="' + width + '" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '">');
    parts.push('<rect width="100%" height="100%" fill="#ffffff"/>');
    parts.push('<defs><marker id="arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8 z" fill="#666"/></marker></defs>');

    edges.forEach(function (e) {
      var src = nodeMap[e.source];
      var tgt = nodeMap[e.target];
      if (!src || !tgt) return;


      var sc = nodeCenter(src), tc = nodeCenter(tgt);
      var sp = nodeBorder(src, tc.x, tc.y);
      var tp = nodeBorder(tgt, sc.x, sc.y);

      var dx = tp.x - sp.x, dy = tp.y - sp.y;
      var path;
      if (Math.abs(dx) > Math.abs(dy) * 2) {
        var mx = (sp.x + tp.x) / 2;
        path = 'M ' + sp.x + ' ' + sp.y + ' C ' + mx + ' ' + sp.y + ' ' + mx + ' ' + tp.y + ' ' + tp.x + ' ' + tp.y;
      } else {
        path = 'M ' + sp.x + ' ' + sp.y + ' L ' + tp.x + ' ' + tp.y;
      }

      parts.push('<path d="' + path + '" fill="none" stroke="#666" stroke-width="1.5" marker-end="url(#arrow)"/>');

      if (e.label) {
        var mx2 = (sp.x + tp.x) / 2;
        var my2 = (sp.y + tp.y) / 2;
        var labelW = esc(e.label).length * 7 + 12;
        parts.push('<rect x="' + (mx2 - labelW / 2) + '" y="' + (my2 - 10) + '" width="' + labelW + '" height="20" rx="3" fill="#fff" stroke="#ccc"/>');
        parts.push('<text x="' + mx2 + '" y="' + (my2 + 4) + '" text-anchor="middle" font-size="12" fill="#333">' + esc(e.label) + '</text>');
      }
    });

    nodes.forEach(function (n) {
      var x = n.x + offsetX, y = n.y + offsetY;
      var cx = x + n.w / 2, cy = y + n.h / 2;

      if (n.type === 'start' || n.type === 'end') {
        var r = Math.min(n.w, n.h) / 2;
        var fill = n.type === 'start' ? '#4CAF50' : '#f44336';
        parts.push('<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="' + fill + '" stroke="' + fill + '" stroke-width="2"/>');
        parts.push('<text x="' + cx + '" y="' + (cy + 5) + '" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">' + esc(n.label || (n.type === 'start' ? '开始' : '结束')) + '</text>');
      } else if (n.type === 'gateway') {
        var dw = n.w / 2, dh = n.h / 2;
        parts.push('<polygon points="' + cx + ',' + (cy - dh) + ' ' + (cx + dw) + ',' + cy + ' ' + cx + ',' + (cy + dh) + ' ' + (cx - dw) + ',' + cy + '" fill="#FF9800" stroke="#E65100" stroke-width="2"/>');
        parts.push('<text x="' + cx + '" y="' + (cy + 5) + '" text-anchor="middle" font-size="12" fill="#fff" font-weight="bold">' + esc(n.label) + '</text>');
      } else {
        var fill = n.type === 'subprocess' ? '#9C27B0' : '#2196F3';
        var stroke = n.type === 'subprocess' ? '#6A1B9A' : '#1565C0';
        parts.push('<rect x="' + x + '" y="' + y + '" width="' + n.w + '" height="' + n.h + '" rx="6" fill="' + fill + '" stroke="' + stroke + '" stroke-width="2"/>');
        parts.push('<text x="' + cx + '" y="' + (cy + 5) + '" text-anchor="middle" font-size="13" fill="#fff">' + esc(n.label) + '</text>');
      }
    });

    parts.push('</svg>');
    return parts.join('');
  }

  function svgToPng(svgStr, filename) {
    var blob = new Blob([svgStr], { type: 'image/svg+xml;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var img = new Image();
    img.onload = function () {
      var canvas = document.createElement('canvas');
      canvas.width = img.width * 2;
      canvas.height = img.height * 2;
      var ctx = canvas.getContext('2d');
      ctx.fillStyle = '#fff';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      canvas.toBlob(function (pngBlob) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(pngBlob);
        a.download = filename + '.png';
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 200);
        toast('导出成功: ' + filename + '.png', 'success');
      }, 'image/png');
    };
    img.onerror = function () {
      URL.revokeObjectURL(url);
      toast('图片生成失败', 'error');
    };
    img.src = url;
  }

  function exportWorkflowImage() {
    toast('正在提取流程图数据...', 'info');

    fetchWorkflowData().then(function (data) {
      var def = data.definition || data.data && data.data.definition || data;
      var nodes = normalizeNodes(def.nodes || []);
      if (nodes.length === 0) {
        fallbackToDOM();
        return;
      }
      nodes.sort(function (a, b) { return a.y - b.y; });
      var edges = [];
      for (var i = 0; i < nodes.length - 1; i++) {
        edges.push({ source: nodes[i].id, target: nodes[i + 1].id, label: '' });
      }
      doExport(nodes, edges);
    }).catch(function () {
      fallbackToDOM();
    });
  }

  function fallbackToDOM() {
    var nodes = extractNodesFromDOM();
    if (nodes.length === 0) {
      toast('未找到流程图节点，无法导出', 'error');
      return;
    }
    nodes.sort(function (a, b) { return a.y - b.y; });
    var edges = [];
    for (var i = 0; i < nodes.length - 1; i++) {
      edges.push({ source: nodes[i].id, target: nodes[i + 1].id, label: '' });
    }
    doExport(nodes, edges);
  }

  function doExport(nodes, edges) {
    var svg = buildSVG(nodes, edges);
    if (!svg) {
      toast('流程图数据为空', 'error');
      return;
    }
    var name = getWorkflowName();
    svgToPng(svg, name);
  }

  function isWorkflowPage() {
    return /workflows/.test(window.location.pathname + window.location.hash);
  }

  function installInterceptOnDoc(doc, label) {
    if (doc.__rgExportIntercepted) return;
    doc.__rgExportIntercepted = true;
    console.log('[RG-WorkflowExport] 拦截器已安装于', label);

    doc.addEventListener('click', function (e) {
      var target = e.target;
      var btn = target.closest ? target.closest('button, a, span, div') : null;
      if (!btn) return;
      var text = (btn.textContent || '').trim();
      if (!/导出/.test(text)) return;
      if (text.length > 10) return;
      console.log('[RG-WorkflowExport] 拦截到导出按钮:', text, btn.tagName, btn.className);
      e.stopPropagation();
      e.preventDefault();
      e.stopImmediatePropagation();
      exportWorkflowImage();
    }, true);
  }

  function installIntercept() {
    installInterceptOnDoc(document, 'main document');

    var iframes = document.querySelectorAll('iframe');
    iframes.forEach(function (f, i) {
      try { installInterceptOnDoc(f.contentDocument, 'iframe#' + i); } catch (e) {}
      f.addEventListener('load', function () {
        try { installInterceptOnDoc(f.contentDocument, 'iframe#' + i + ' (load)'); } catch (e) {}
      });
    });

    setInterval(function () {
      document.querySelectorAll('iframe').forEach(function (f, i) {
        try { installInterceptOnDoc(f.contentDocument, 'iframe#' + i + ' (poll)'); } catch (e) {}
      });
    }, 2000);
  }

  function init() {
    installIntercept();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
