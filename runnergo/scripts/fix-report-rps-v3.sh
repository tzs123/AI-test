#!/usr/bin/env bash
# 订正官方性能测试报告 RPS/SRPS 口径错误（v3：定位到 mongo runnergo.report_data）。
#
# 根因：report_data.data 是 JSON 字符串字段，内嵌接口指标；
#       引擎把瞬时峰值吞吐写进了 rps/srps 列，正确口径=请求数÷时长。
#
# 用法:
#   bash fix-report-rps-v3.sh            # dry-run：打印 data 结构 + 所有 rps 命中点
#   bash fix-report-rps-v3.sh --apply    # 写库订正（原值备份到同层级 rps_raw_peak）
#
# 环境变量: TOTAL_REQUESTS=3435 DURATION_SECONDS=99
set -euo pipefail

TOTAL_REQUESTS="${TOTAL_REQUESTS:-3435}"
DURATION_SECONDS="${DURATION_SECONDS:-99}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1
EXPECTED_RPS=$(python3 -c "print(round(${TOTAL_REQUESTS}/${DURATION_SECONDS}, 2))")

CONTAINER=$(docker ps --format '{{.Names}}' | grep -iE 'mongo' | head -1)
[ -n "${CONTAINER}" ] || { echo "!! 未找到 mongo 容器"; exit 1; }
echo ">> 目标: ${CONTAINER} / runnergo.report_data  RPS=${TOTAL_REQUESTS}/${DURATION_SECONDS}s=${EXPECTED_RPS}  模式=$([ ${APPLY} = 1 ] && echo APPLY || echo DRY-RUN)"

read -r -d '' JS <<EOF || true
var NEW_RPS=${EXPECTED_RPS}, APPLY=(${APPLY}===1), DURATION=${DURATION_SECONDS};
function isRateKey(lk){ return lk==='rps'||lk==='srps'||lk.indexOf('rps')>=0||lk.indexOf('qps')>=0||lk.indexOf('throughput')>=0; }
function isCountKey(lk){ return lk==='requests'||lk==='requestcount'||lk==='totalrequestcount'||lk==='totalrequests'||lk==='count'||lk==='reqcount'; }
function walk(obj, path, hits){
  if (obj === null || typeof obj !== 'object') return;
  if (Array.isArray(obj)) { obj.forEach(function(v,i){ walk(v, path+'['+i+']', hits); }); return; }
  Object.keys(obj).forEach(function(k){
    var v = obj[k], p = path+'.'+k, lk = k.toLowerCase();
    if (typeof v === 'number' && isRateKey(lk)) hits.push({parent:obj, key:k, path:p, value:v});
    walk(v, p, hits);
  });
}
var coll = db.getSiblingDB('runnergo').getCollection('report_data');
var doc = coll.findOne();
if (!doc) { print('!! report_data 集合为空'); quit(); }
print('==== report_data._id=' + doc._id + '  report_id=' + doc.report_id + '  plan_id=' + doc.plan_id);

['data','analysis'].forEach(function(FIELD){
  var raw = doc[FIELD];
  if (raw === undefined || raw === null) { print('  [' + FIELD + '] 不存在'); return; }
  var parsed, isStr = (typeof raw === 'string');
  if (isStr) { try { parsed = JSON.parse(raw); } catch(e){ print('  [' + FIELD + '] 字符串但非 JSON，跳过'); return; } }
  else parsed = raw;
  print('  [' + FIELD + '] type=' + (isStr?'JSON字符串':'对象') + ' 顶层键=[' + Object.keys(parsed||{}).join(',') + ']');
  if (parsed && typeof parsed === 'object') {
    Object.keys(parsed).slice(0,8).forEach(function(k){
      var v = parsed[k];
      if (Array.isArray(v)) print('    ' + k + ': array[' + v.length + ']' + (v.length? ' 元素键=['+Object.keys(v[0]||{}).slice(0,15).join(',')+']':''));
      else if (v && typeof v === 'object') print('    ' + k + ': object  键=[' + Object.keys(v).slice(0,15).join(',') + ']');
      else print('    ' + k + ': ' + (typeof v) + '=' + String(v).substring(0,60));
    });
  }
  var hits = []; walk(parsed, FIELD, hits);
  if (!hits.length) { print('    无 rps/qps/throughput 命中'); return; }
  print('    命中 ' + hits.length + ' 个速率字段:');
  var changed = false;
  hits.forEach(function(h){
    print('      ' + h.path + ' = ' + h.value + '  ->  ' + NEW_RPS);
    if (APPLY && h.value !== NEW_RPS) {
      if (h.parent.rps_raw_peak === undefined) h.parent.rps_raw_peak = h.value;
      h.parent[h.key] = NEW_RPS; changed = true;
    }
  });
  if (APPLY && changed) {
    doc[FIELD] = isStr ? JSON.stringify(parsed) : parsed;
    print('    ✓ 已订正 ' + FIELD);
  }
});
if (APPLY) { coll.replaceOne({_id:doc._id}, doc); print('>> 已写库'); }
else print('>> dry-run 完成，确认后追加 --apply');
EOF

docker exec "${CONTAINER}" mongo -u runnergo -p hello123456 --authenticationDatabase runnergo --quiet --eval "${JS}" 2>&1 | grep -vE '^(W|I|E)[0-9]'