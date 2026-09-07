#!/usr/bin/env bash
# 勘察官方 RunnerGo 压测报告的真实存储位置（Mongo / MySQL / Redis）。
# 用法: bash locate-report-storage.sh
set -euo pipefail

mongo_c=$(docker ps --format '{{.Names}}' | grep -iE 'mongo' | head -1)
mysql_c=$(docker ps --format '{{.Names}}' | grep -iE 'mysql' | head -1)
redis_c=$(docker ps --format '{{.Names}}' | grep -iE 'redis' | head -1)

echo "########## MongoDB (${mongo_c:-未找到}) ##########"
if [ -n "${mongo_c}" ]; then
  docker exec "${mongo_c}" mongo -u runnergo -p hello123456 --authenticationDatabase runnergo --quiet --eval '
db.getSiblingDB("runnergo").getCollectionNames().forEach(function(cn){
  if (cn.indexOf("system.")===0) return;
  var c = db.getSiblingDB("runnergo").getCollection(cn);
  var n = c.count();
  if (n===0) return;
  var sample = c.findOne();
  print("  " + cn + "  count=" + n + "  keys=[" + Object.keys(sample||{}).join(",") + "]");
});
' 2>&1 | grep -vE '^(W|I|E)[0-9]' || true
fi

echo
echo "########## MySQL (${mysql_c:-未找到}) ##########"
if [ -n "${mysql_c}" ]; then
  echo ">> 所有表:"
  docker exec "${mysql_c}" mysql -uroot -p123456 runnergo -N -e "
SELECT table_name FROM information_schema.tables WHERE table_schema='runnergo' ORDER BY table_name;" 2>/dev/null | sed 's/^/  /'
  echo ">> 含 report/perform/stress/scene 的表结构 + 最近一条:"
  for t in $(docker exec "${mysql_c}" mysql -uroot -p123456 runnergo -N -e "
SELECT table_name FROM information_schema.tables WHERE table_schema='runnergo'
  AND (table_name LIKE '%report%' OR table_name LIKE '%perform%' OR table_name LIKE '%stress%' OR table_name LIKE '%scene%')
ORDER BY table_name;" 2>/dev/null); do
    echo "  ---- 表 ${t} ----"
    docker exec "${mysql_c}" mysql -uroot -p123456 runnergo -e "DESCRIBE ${t};" 2>/dev/null | sed 's/^/    /'
    echo "    最近 1 条:"
    docker exec "${mysql_c}" mysql -uroot -p123456 runnergo -e "SELECT * FROM ${t} ORDER BY 1 DESC LIMIT 1\G" 2>/dev/null | sed 's/^/    /' | head -40
  done
fi

echo
echo "########## Redis (${redis_c:-未找到}) ##########"
if [ -n "${redis_c}" ]; then
  echo ">> keyspace:"
  docker exec "${redis_c}" redis-cli -a mypassword --no-auth-warning INFO keyspace 2>/dev/null | sed 's/^/  /'
  echo ">> 与报告相关的 key（最多 30 个）:"
  docker exec "${redis_c}" redis-cli -a mypassword --no-auth-warning --scan --pattern '*report*' 2>/dev/null | head -30 | sed 's/^/  /'
  docker exec "${redis_c}" redis-cli -a mypassword --no-auth-warning --scan --pattern '*perf*' 2>/dev/null | head -10 | sed 's/^/  /'
  docker exec "${redis_c}" redis-cli -a mypassword --no-auth-warning --scan --pattern '*stress*' 2>/dev/null | head -10 | sed 's/^/  /'
fi

echo
echo ">> 勘察完成。把以上输出贴回，据此定位报告存储并写订正逻辑。"