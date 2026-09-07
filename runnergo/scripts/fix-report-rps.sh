#!/usr/bin/env bash
# 一次性订正官方性能测试报告的 RPS/SRPS 口径错误。
#
# 背景：官方 engine 上报的 RPS 是瞬时峰值吞吐（与吞吐量曲线峰值一致），
# 而非 平均请求数/实际时长。校验依据：
#   1) Little's Law 上限：并发5 ÷ Avg 0.1148s ≈ 43.6/s < 引擎上报的 49.5
#   2) 与 tps 自相矛盾：单接口场景 RPS 应≈tps(34.14)
# 正确口径：RPS = 总请求数 ÷ 实际执行时长。
#
# 用法：
#   bash fix-report-rps.sh            # dry-run：只探测并打印目标文档结构
#   bash fix-report-rps.sh --apply    # 确认无误后写库订正
#
# 可用环境变量覆盖：
#   TOTAL_REQUESTS=3435 DURATION_SECONDS=99 CONTAINER=<mongo容器名>

set -euo pipefail

TOTAL_REQUESTS="${TOTAL_REQUESTS:-3435}"
DURATION_SECONDS="${DURATION_SECONDS:-99}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

EXPECTED_RPS=$(python3 -c "print(round(${TOTAL_REQUESTS}/${DURATION_SECONDS}, 2))")
echo ">> 目标口径: RPS = SRPS = ${TOTAL_REQUESTS} / ${DURATION_SECONDS}s = ${EXPECTED_RPS}"
if [ "${APPLY}" = "1" ]; then
  echo ">> 模式: APPLY（写库订正，原值备份到同文档 rps_raw_peak 字段）"
else
  echo ">> 模式: DRY-RUN（只探测，不写库；确认输出后追加 --apply 执行）"
fi

# 自动探测 mongo 容器名
CONTAINER="${CONTAINER:-}"
if [ -z "${CONTAINER}" ]; then
  CONTAINER=$(docker ps --format '{{.Names}}' | grep -i mongo | head -1)
fi
[ -n "${CONTAINER}" ] || { echo "!! 未找到 mongo 容器"; exit 1; }
echo ">> mongo 容器: ${CONTAINER}"

# mongo 4.4 用 mongo shell；兼容 mongosh
SHELL_BIN="mongo"
docker exec "${CONTAINER}" sh -c 'command -v mongosh >/dev/null 2>&1' && SHELL_BIN="mongosh"

# 在 mongo shell 内执行的探测/订正 JS：
#   1) 遍历业务库所有集合，找含 rps/qps/throughput 数值键的文档
#      （官方 collector 字段名未必是 rps，总数 3435 也可能由 manage 聚合时才算）
#   2) 打印 _id / 名称类字段 / 时间类字段 / 全部候选字段路径与值，用于人工定位
#   3) 含旧值 49.5 或总数 3435 的文档打 ★ 高亮
#   4) APPLY 模式下递归订正候选字段 → NEW_RPS，原值备份到 rps_raw_peak
read -r -d '' PROBE_JS <<EOF || true
var TOTAL=${TOTAL_REQUESTS}, NEW_RPS=${EXPECTED_RPS}, APPLY=(${APPLY}===1);
function isRateKey(lk){
  return lk.indexOf('rps')>=0 || lk.indexOf('qps')>=0 || lk.indexOf('throughput')>=0 || lk.indexOf('reqs')>=0;
}
function walk(obj, path, hits){
  if (obj === null || typeof obj !== 'object') return;
  if (Array.isArray(obj)) { obj.forEach(function(v,i){ walk(v, path+'['+i+']', hits); }); return; }
  Object.keys(obj).forEach(function(k){
    var v = obj[k], p = path+'.'+k, lk = k.toLowerCase();
    if (typeof v === 'number' && isRateKey(lk)) hits.push({parent:obj, key:k, path:p, value:v});
    walk(v, p, hits);
  });
}
function pickMeta(doc){
  var meta=[];
  Object.keys(doc).slice(0,40).forEach(function(k){
    var v=doc[k], lk=k.toLowerCase();
    if (typeof v==='string' && (lk.indexOf('name')>=0 || lk.indexOf('time')>=0 || lk.indexOf('date')>=0 || String(v).indexOf('性能')>=0)) meta.push(k+'='+String(v).substring(0,60));
    if (typeof v==='number' && (lk.indexOf('time')>=0 || lk.indexOf('date')>=0)) meta.push(k+'='+v);
  });
  return meta.join(' | ');
}
var totalHits=0, shown=0;
db.getMongo().getDBNames().forEach(function(dn){
  if (['admin','config','local'].indexOf(dn)>=0) return;
  var dbi = db.getSiblingDB(dn);
  dbi.getCollectionNames().forEach(function(cn){
    if (cn.indexOf('system.')===0) return;
    var collHits=0;
    dbi.getCollection(cn).find().forEach(function(doc){
      var hits=[]; walk(doc,'',hits);
      if (!hits.length) return;
      collHits++; totalHits++;
      if (shown>=20) return;
      shown++;
      var s=JSON.stringify(doc);
      var stars=[];
      if (s.indexOf(String(TOTAL))>=0) stars.push('含总数'+TOTAL);
      if (s.indexOf('49.5')>=0) stars.push('含旧值49.5');
      var star = stars.length ? '  ★ '+stars.join(' · ') : '';
      print('==== '+dn+'.'+cn+'  _id='+doc._id+star);
      var meta=pickMeta(doc);
      if (meta) print('  元信息: '+meta);
      hits.slice(0,25).forEach(function(h){ print('  字段 '+h.path+' = '+h.value); });
      if (hits.length>25) print('  ...共 '+hits.length+' 个候选字段');
    });
    if (collHits) print('---- 集合 '+dn+'.'+cn+' 共 '+collHits+' 个含速率字段文档');
  });
});
print('>> 总命中 '+totalHits+' 个文档');
if (!totalHits) print('!! 未找到任何含 rps/qps/throughput 字段的文档；可能速率字段名更冷僻，或报告根本不入 mongo（manage 直接从 kafka 内存聚合）');
if (APPLY && totalHits){
  print('!! 此版本仅探测结构。请把 dry-run 输出贴回，确认目标文档与字段路径后，再单独执行 --apply');
}
EOF

docker exec "${CONTAINER}" ${SHELL_BIN} --quiet --eval "${PROBE_JS}"