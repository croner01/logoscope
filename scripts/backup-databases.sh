#!/bin/bash
# Logoscope 数据备份脚本
# 定期备份 ClickHouse 和 Neo4j 数据

set -e

BACKUP_DIR="/data/backups/logoscope"
DATE=$(date +%Y%m%d_%H%M%S)
CLICKHOUSE_HOST="10.43.71.7"
CLICKHOUSE_PORT="8123"
NEO4J_HOST="10.43.184.132"
NEO4J_PORT="7687"

# 创建备份目录
mkdir -p "${BACKUP_DIR}/${DATE}"

echo "=== 开始备份: ${DATE} ==="

# 1. 备份 ClickHouse 数据结构
echo "备份 ClickHouse DDL..."
mkdir -p "${BACKUP_DIR}/${DATE}"
curl -s "http://${CLICKHOUSE_HOST}:${CLICKHOUSE_PORT}/?database=logs&query=SHOW+CREATE+TABLE+logs.logs&format=TSV" > "${BACKUP_DIR}/${DATE}/logs_schema.sql"

curl -s "http://${CLICKHOUSE_HOST}:${CLICKHOUSE_PORT}/?database=logs&query=SHOW+CREATE+TABLE+logs.traces&format=TSV" > "${BACKUP_DIR}/${DATE}/traces_schema.sql"

curl -s "http://${CLICKHOUSE_HOST}:${CLICKHOUSE_PORT}/?database=logs&query=SHOW+CREATE+TABLE+logs.events&format=TSV" > "${BACKUP_DIR}/${DATE}/events_schema.sql"

curl -s "http://${CLICKHOUSE_HOST}:${CLICKHOUSE_PORT}/?database=logs&query=SHOW+CREATE+TABLE+logs.metrics&format=TSV" > "${BACKUP_DIR}/${DATE}/metrics_schema.sql"

# 2. 导出关键数据（最近7天的日志，样本数据）
echo "导出 ClickHouse 数据样本..."
clickhouse-client --host "${CLICKHOUSE_HOST}" --port "${CLICKHOUSE_PORT}" --database logs --query "SELECT * FROM logs.logs WHERE timestamp >= now() - INTERVAL 7 DAY FORMAT CSVWithNames" > "${BACKUP_DIR}/${DATE}/logs_sample.csv" || echo "ClickHouse client不可用，使用HTTP接口导出"

# 3. 备份 Neo4j 数据
echo "备份 Neo4j 数据..."
cypher-shell -a "${NEO4J_HOST}" -p "${NEO4J_PORT}" -u neo4j -p "${NEO4J_PASSWORD}" << 'CYPHER' > "${BACKUP_DIR}/${DATE}/neo4j_backup.cyp"
CALL apoc.export.csv.all("*", "${BACKUP_DIR}/${DATE}/nodes.csv", "nodes")
CALL apoc.export.csv.all("*", "${BACKUP_DIR}/${DATE}/relationships.csv", "relationships")
CYPHER

# 4. 压缩备份
echo "压缩备份..."
tar -czf "${BACKUP_DIR}/${DATE}.tar.gz" -C "${BACKUP_DIR}" "${DATE}/"
rm -rf "${BACKUP_DIR}/${DATE}/"

# 5. 清理旧备份（保留最近30天）
echo "清理旧备份..."
find "${BACKUP_DIR}" -name "*.tar.gz" -mtime +30 -delete

echo "=== 备份完成: ${BACKUP_DIR}/${DATE}.tar.gz ==="
du -h "${BACKUP_DIR}/${DATE}.tar.gz"
