#!/bin/bash
##############################################################################
# Logoscope 一键部署脚本
# 功能：部署、管理、监控所有 Logoscope 组件
# 作者：Claude Code AI Assistant
# 版本：v4.0
# 更新：2026-02-26
##############################################################################

set -e  # 遇到错误立即退出

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 项目路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
DEPLOY_DIR="$PROJECT_ROOT/deploy"

# K3s 配置
K3S_CONFIG_FILE="/etc/rancher/k3s/k3s.yaml"
NAMESPACE="islap"

##############################################################################
# 工具函数
##############################################################################

print_header() {
    echo -e "${BLUE}======================================================================${NC}"
    echo -e "${BLUE} $1${NC}"
    echo -e "${BLUE}======================================================================${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

# 检查 k3s 是否运行
check_k3s() {
    if ! systemctl is-active --quiet k3s; then
        print_error "K3s 服务未运行"
        print_info "启动 K3s: sudo systemctl start k3s"
        exit 1
    fi
    print_success "K3s 服务运行正常"
}

# 检查 kubectl 是否可用
check_kubectl() {
    if ! command -v kubectl &> /dev/null; then
        print_error "kubectl 未安装"
        print_info "安装 kubectl: curl -LO https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
        exit 1
    fi
    print_success "kubectl 可用"
}

# 设置 kubectl 配置
setup_kubectl() {
    export KUBECONFIG="$K3S_CONFIG_FILE"
}

# 等待 Pod 就绪
wait_for_pods() {
    local namespace=$1
    local label=$2
    local timeout=${3:-300}

    print_info "等待 Pod 就绪（标签: $label, 命名空间: $namespace）..."

    local start_time=$(date +%s)
    local elapsed=0

    while [ $elapsed -lt $timeout ]; do
        local ready=$(kubectl get pods -n "$namespace" -l "$label" -o jsonpath='{.items[*].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)

        if [[ "$ready" =~ "True" ]] && [[ ! "$ready" =~ "False" ]]; then
            print_success "所有 Pod 已就绪"
            return 0
        fi

        sleep 2
        elapsed=$(($(date +%s) - start_time))
    done

    print_error "等待 Pod 超时（${timeout}秒）"
    return 1
}

# 等待 Deployment 就绪
wait_for_deployment() {
    local namespace=$1
    local deployment=$2
    local timeout=${3:-300}

    print_info "等待 Deployment 就绪: $deployment"

    kubectl rollout status deployment/"$deployment" -n "$namespace" --timeout="${timeout}s"

    if [ $? -eq 0 ]; then
        print_success "Deployment $deployment 已就绪"
        return 0
    else
        print_error "Deployment $deployment 未能在 ${timeout}秒内就绪"
        return 1
    fi
}

# 等待 DaemonSet 就绪
wait_for_daemonset() {
    local namespace=$1
    local daemonset=$2
    local timeout=${3:-300}

    print_info "等待 DaemonSet 就绪: $daemonset"

    kubectl rollout status daemonset/"$daemonset" -n "$namespace" --timeout="${timeout}s"

    if [ $? -eq 0 ]; then
        print_success "DaemonSet $daemonset 已就绪"
        return 0
    else
        print_error "DaemonSet $daemonset 未能在 ${timeout}秒内就绪"
        return 1
    fi
}

# 检查服务健康状态
check_service_health() {
    local service=$1
    local namespace=${2:-$NAMESPACE}

    print_info "检查服务健康: $service"

    # 检查 Pod 状态
    local pods=$(kubectl get pods -n "$namespace" -l "app=$service" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)

    if [ -z "$pods" ]; then
        print_warning "未找到 $service 的 Pod"
        return 1
    fi

    local all_ready=true
    for pod in $pods; do
        local status=$(kubectl get pod "$pod" -n "$namespace" -o jsonpath='{.status.phase}')
        if [ "$status" != "Running" ]; then
            print_error "Pod $pod 状态: $status"
            all_ready=false
        else
            print_success "Pod $pod 运行正常"
        fi
    done

    if [ "$all_ready" = true ]; then
        return 0
    else
        return 1
    fi
}

##############################################################################
# 部署函数
##############################################################################

# 部署命名空间
deploy_namespace() {
    print_header "部署命名空间"

    if kubectl get namespace "$NAMESPACE" &> /dev/null; then
        print_info "命名空间 $NAMESPACE 已存在"
        return 0
    fi

    kubectl apply -f "$DEPLOY_DIR/namespace.yaml"
    print_success "命名空间 $NAMESPACE 创建完成"
}

# 部署 ClickHouse
deploy_clickhouse() {
    print_header "部署 ClickHouse"

    kubectl apply -f "$DEPLOY_DIR/clickhouse.yaml"

    print_info "等待 ClickHouse 就绪..."
    wait_for_deployment "$NAMESPACE" "clickhouse" 300

    print_success "ClickHouse 部署完成"
}

# 部署 Neo4j
deploy_neo4j() {
    print_header "部署 Neo4j"

    kubectl apply -f "$DEPLOY_DIR/neo4j.yaml"

    print_info "等待 Neo4j 就绪..."
    wait_for_deployment "$NAMESPACE" "neo4j" 300

    print_success "Neo4j 部署完成"
}

# 部署 Redis
deploy_redis() {
    print_header "部署 Redis"

    kubectl apply -f "$DEPLOY_DIR/redis.yaml"

    print_info "等待 Redis 就绪..."
    wait_for_deployment "$NAMESPACE" "redis" 180

    print_success "Redis 部署完成"
}

# 部署 Semantic Engine
deploy_semantic_engine() {
    print_header "部署 Semantic Engine API"

    kubectl apply -f "$DEPLOY_DIR/semantic-engine.yaml"

    print_info "等待 Semantic Engine 就绪..."
    wait_for_deployment "$NAMESPACE" "semantic-engine" 180

    print_success "Semantic Engine API 部署完成"
}

# 部署 AI Service
deploy_ai_service() {
    print_header "部署 AI Service"

    kubectl apply -f "$DEPLOY_DIR/ai-service.yaml"

    print_info "等待 AI Service 就绪..."
    wait_for_deployment "$NAMESPACE" "ai-service" 180

    print_success "AI Service 部署完成"
}

# 部署 Worker
deploy_worker() {
    print_header "部署 Semantic Engine Worker"

    kubectl apply -f "$DEPLOY_DIR/semantic-engine-worker.yaml"

    print_info "等待 Worker 就绪..."
    wait_for_deployment "$NAMESPACE" "semantic-engine-worker" 180

    print_success "Semantic Engine Worker 部署完成"
}

# 部署 Fluent Bit
deploy_fluentbit() {
    print_header "部署 Fluent Bit"

    kubectl apply -f "$DEPLOY_DIR/fluent-bit.yaml"

    print_info "等待 Fluent Bit 就绪..."
    wait_for_daemonset "$NAMESPACE" "fluent-bit" 180

    print_success "Fluent Bit 部署完成"
}

# 部署 OTEL Collector
deploy_otel_collector() {
    print_header "部署 OTEL Collector"

    kubectl apply -f "$DEPLOY_DIR/otel-collector.yaml"

    print_info "等待 OTEL Collector 就绪..."
    wait_for_daemonset "$NAMESPACE" "otel-collector" 180

    print_success "OTEL Collector 部署完成"
}

# 部署 Ingest Service
deploy_ingest_service() {
    print_header "部署 Ingest Service"

    kubectl apply -f "$DEPLOY_DIR/ingest-service.yaml"

    print_info "等待 Ingest Service 就绪..."
    wait_for_deployment "$NAMESPACE" "ingest-service" 180

    print_success "Ingest Service 部署完成"
}

# 部署 OTEL Gateway
deploy_otel_gateway() {
    print_header "部署 OTEL Gateway"

    kubectl apply -f "$DEPLOY_DIR/otel-gateway.yaml"

    print_info "等待 OTEL Gateway 就绪..."
    wait_for_deployment "$NAMESPACE" "otel-gateway" 180

    print_success "OTEL Gateway 部署完成"
}

# 部署 Query Service
deploy_query_service() {
    print_header "部署 Query Service"

    kubectl apply -f "$DEPLOY_DIR/query-service.yaml"

    print_info "等待 Query Service 就绪..."
    wait_for_deployment "$NAMESPACE" "query-service" 180

    print_success "Query Service 部署完成"
}

# 部署 Topology Service
deploy_topology_service() {
    print_header "部署 Topology Service"

    kubectl apply -f "$DEPLOY_DIR/topology-service.yaml"

    print_info "等待 Topology Service 就绪..."
    wait_for_deployment "$NAMESPACE" "topology-service" 180

    print_success "Topology Service 部署完成"
}

# 部署 Frontend
deploy_frontend() {
    print_header "部署 Frontend"

    kubectl apply -f "$DEPLOY_DIR/frontend.yaml"

    print_info "等待 Frontend 就绪..."
    wait_for_deployment "$NAMESPACE" "frontend" 180

    print_success "Frontend 部署完成"
}

# 部署 Value KPI 周期任务
deploy_value_kpi_cronjob() {
    print_header "部署 Value KPI 周期任务"

    kubectl apply -f "$DEPLOY_DIR/value-kpi-cronjob.yaml"

    print_success "Value KPI CronJob 部署完成"
}

# 部署所有组件
deploy_all() {
    print_header "Logoscope 一键部署"
    echo ""

    # 按依赖顺序部署
    print_info "部署顺序：命名空间 → 基础设施 → 核心服务 → 采集组件 → 查询服务 → 前端"
    echo ""

    # 1. 命名空间
    deploy_namespace
    echo ""

    # 2. 基础设施（数据库）
    print_info "部署基础设施（数据库）..."
    deploy_clickhouse
    echo ""

    deploy_neo4j
    echo ""

    deploy_redis
    echo ""

    # 3. 核心服务
    print_info "部署核心服务..."
    deploy_ingest_service
    echo ""

    deploy_semantic_engine
    echo ""

    deploy_ai_service
    echo ""

    deploy_worker
    echo ""

    deploy_query_service
    echo ""

    deploy_topology_service
    echo ""

    # 4. 采集组件
    print_info "部署数据采集组件..."
    deploy_fluentbit
    echo ""

    deploy_otel_collector
    echo ""

    deploy_otel_gateway
    echo ""

    # 5. 前端
    print_info "部署前端..."
    deploy_frontend
    echo ""

    # 6. 运营自动化
    print_info "部署运维自动化任务..."
    deploy_value_kpi_cronjob
    echo ""

    print_header "部署完成"
    print_success "所有组件部署完成！"
    print_info "使用 './deploy.sh status' 查看状态"
    print_info "使用 './deploy.sh health' 进行健康检查"
    print_info "使用 './deploy.sh init-db' 初始化数据库表"
}

##############################################################################
# 管理函数
##############################################################################

# 查看状态
show_status() {
    print_header "Logoscope 组件状态"

    echo -e "${BLUE}命名空间: $NAMESPACE${NC}"
    echo ""

    echo -e "${BLUE}【Pod 状态】${NC}"
    kubectl get pods -n "$NAMESPACE"
    echo ""

    echo -e "${BLUE}【服务状态】${NC}"
    kubectl get svc -n "$NAMESPACE"
    echo ""

    echo -e "${BLUE}【Deployment 状态】${NC}"
    kubectl get deployments -n "$NAMESPACE"
    echo ""

    echo -e "${BLUE}【DaemonSet 状态】${NC}"
    kubectl get daemonsets -n "$NAMESPACE"
    echo ""

    echo -e "${BLUE}【StatefulSet 状态】${NC}"
    kubectl get statefulsets -n "$NAMESPACE"
    echo ""

    echo -e "${BLUE}【CronJob 状态】${NC}"
    kubectl get cronjobs -n "$NAMESPACE"
}

# 健康检查
health_check() {
    print_header "Logoscope 健康检查"

    local services=(
        "clickhouse"
        "neo4j"
        "redis"
        "ingest-service"
        "semantic-engine"
        "ai-service"
        "semantic-engine-worker"
        "query-service"
        "topology-service"
        "frontend"
    )

    local all_healthy=true

    for service in "${services[@]}"; do
        if check_service_health "$service"; then
            print_success "$service 健康"
        else
            print_error "$service 不健康"
            all_healthy=false
        fi
    done

    echo ""

    if [ "$all_healthy" = true ]; then
        print_success "所有服务健康检查通过"
        return 0
    else
        print_error "部分服务健康检查失败"
        return 1
    fi
}

# 初始化数据库
init_database() {
    print_header "初始化数据库表"

    print_info "检查 ClickHouse 表..."
    local logs_ttl_days="${LOGS_TTL_DAYS:-30}"
    local traces_ttl_days="${TRACES_TTL_DAYS:-30}"
    local events_ttl_days="${EVENTS_TTL_DAYS:-30}"
    local metrics_ttl_days="${METRICS_TTL_DAYS:-7}"

    # 检查 ClickHouse 是否就绪
    local clickhouse_pod=$(kubectl get pods -n "$NAMESPACE" -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

    if [ -z "$clickhouse_pod" ]; then
        print_error "未找到 ClickHouse Pod"
        return 1
    fi

    print_info "ClickHouse Pod: $clickhouse_pod"

    # 创建 logs 数据库
    print_info "创建 logs 数据库..."
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "CREATE DATABASE IF NOT EXISTS logs"

    # 创建 logs 表（带物化列、跳数索引、projection）
    print_info "创建 logs.logs 表..."
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.logs (
            id String,
            timestamp DateTime64(9, 'UTC'),
            observed_timestamp DateTime64(9, 'UTC'),
            trace_id String,
            span_id String,
            trace_flags UInt8,
            service_name LowCardinality(String),
            pod_name String,
            namespace LowCardinality(String),
            node_name String,
            host_name String,
            pod_id String,
            container_name String,
            container_id String,
            container_image String,
            level String,
            level_norm LowCardinality(String) MATERIALIZED multiIf(
                length(trim(BOTH ' ' FROM ifNull(level, ''))) = 0, 'OTHER',
                upperUTF8(trim(BOTH ' ' FROM level)) = 'WARNING', 'WARN',
                upperUTF8(trim(BOTH ' ' FROM level))
            ),
            severity_number UInt8,
            flags UInt8,
            message String,
            labels String,
            attributes_json String,
            trace_id_source LowCardinality(String) MATERIALIZED lowerUTF8(ifNull(JSONExtractString(attributes_json, 'trace_id_source'), '')),
            host_ip String,
            cpu_limit String,
            cpu_request String,
            memory_limit String,
            memory_request String,
            INDEX idx_logs_id id TYPE bloom_filter(0.01) GRANULARITY 4,
            INDEX idx_logs_trace_id trace_id TYPE bloom_filter(0.01) GRANULARITY 4,
            INDEX idx_logs_span_id span_id TYPE bloom_filter(0.01) GRANULARITY 4,
            INDEX idx_logs_level_norm level_norm TYPE set(128) GRANULARITY 1,
            INDEX idx_logs_service_name service_name TYPE set(256) GRANULARITY 1,
            INDEX idx_logs_namespace namespace TYPE set(256) GRANULARITY 1,
            INDEX idx_logs_pod_name pod_name TYPE bloom_filter(0.01) GRANULARITY 4,
            INDEX idx_logs_trace_id_source trace_id_source TYPE set(128) GRANULARITY 1,
            INDEX idx_logs_message_token message TYPE tokenbf_v1(32768, 3, 0) GRANULARITY 1,
            PROJECTION proj_logs_trace_lookup
            (
                SELECT
                    id,
                    timestamp,
                    service_name,
                    level_norm,
                    message,
                    trace_id,
                    span_id,
                    pod_name,
                    namespace
                ORDER BY (trace_id, timestamp, id)
            ),
            PROJECTION proj_logs_service_level_time
            (
                SELECT
                    timestamp,
                    service_name,
                    level_norm,
                    trace_id,
                    span_id,
                    id
                ORDER BY (service_name, level_norm, timestamp, id)
            )
        ) ENGINE = MergeTree()
        PARTITION BY toDate(timestamp)
        ORDER BY (timestamp, service_name, trace_id, span_id, id)
        TTL toDateTime(timestamp) + INTERVAL ${logs_ttl_days} DAY DELETE
        SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1
    "
    # 历史集群增量补齐 logs 过滤索引（覆盖 service_name/namespace 查询）
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.logs
        ADD INDEX IF NOT EXISTS idx_logs_service_name service_name TYPE set(256) GRANULARITY 1
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.logs
        ADD INDEX IF NOT EXISTS idx_logs_namespace namespace TYPE set(256) GRANULARITY 1
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.logs
        ADD INDEX IF NOT EXISTS idx_logs_id id TYPE bloom_filter(0.01) GRANULARITY 4
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.logs
        ADD INDEX IF NOT EXISTS idx_logs_pod_name pod_name TYPE bloom_filter(0.01) GRANULARITY 4
    "

    # 创建 events 表
    print_info "创建 logs.events 表..."
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.events (
            id String,
            timestamp DateTime64(9, 'UTC'),
            entity_type String,
            entity_name String,
            event_type String,
            level String,
            content String,
            trace_id String,
            span_id String,
            labels String,
            host_ip String
        ) ENGINE = MergeTree()
        PARTITION BY toDate(timestamp)
        ORDER BY (timestamp, entity_name, event_type)
        TTL toDateTime(timestamp) + INTERVAL ${events_ttl_days} DAY DELETE
        SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1
    "

    # 创建 traces 表（带跳数索引、projection）
    print_info "创建 logs.traces 表..."
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.traces (
            timestamp DateTime64(9, 'UTC'),
            trace_id String,
            span_id String,
            parent_span_id String,
            service_name LowCardinality(String),
            operation_name String,
            span_kind String,
            status String,
            duration_ms Float64 DEFAULT 0.0,
            attributes_json String,
            traces_namespace LowCardinality(String) MATERIALIZED multiIf(
                length(JSONExtractString(attributes_json, 'k8s.namespace.name')) > 0,
                JSONExtractString(attributes_json, 'k8s.namespace.name'),
                length(JSONExtractString(attributes_json, 'service_namespace')) > 0,
                JSONExtractString(attributes_json, 'service_namespace'),
                JSONExtractString(attributes_json, 'namespace')
            ),
            events_json String,
            links_json String,
            INDEX idx_traces_trace_id trace_id TYPE bloom_filter(0.01) GRANULARITY 4,
            INDEX idx_traces_span_id span_id TYPE bloom_filter(0.01) GRANULARITY 4,
            INDEX idx_traces_parent_span_id parent_span_id TYPE bloom_filter(0.01) GRANULARITY 4,
            INDEX idx_traces_service_name service_name TYPE set(256) GRANULARITY 1,
            INDEX idx_traces_namespace traces_namespace TYPE set(256) GRANULARITY 1,
            PROJECTION proj_traces_trace_lookup
            (
                SELECT
                    trace_id,
                    span_id,
                    parent_span_id,
                    timestamp,
                    service_name,
                    operation_name,
                    status,
                    duration_ms
                ORDER BY (trace_id, timestamp, span_id)
            )
        ) ENGINE = MergeTree()
        PARTITION BY toDate(timestamp)
        ORDER BY (timestamp, trace_id, span_id)
        TTL toDateTime(timestamp) + INTERVAL ${traces_ttl_days} DAY DELETE
        SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1
    "
    # 历史集群增量补齐 traces_namespace（避免 namespace 查询回退到 JSONExtract）
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.traces
        ADD COLUMN IF NOT EXISTS traces_namespace LowCardinality(String)
        MATERIALIZED multiIf(
            length(JSONExtractString(attributes_json, 'k8s.namespace.name')) > 0,
            JSONExtractString(attributes_json, 'k8s.namespace.name'),
            length(JSONExtractString(attributes_json, 'service_namespace')) > 0,
            JSONExtractString(attributes_json, 'service_namespace'),
            JSONExtractString(attributes_json, 'namespace')
        )
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.traces
        ADD INDEX IF NOT EXISTS idx_traces_namespace traces_namespace TYPE set(256) GRANULARITY 1
    "

    # 创建 metrics 表
    print_info "创建 logs.metrics 表..."
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.metrics (
            timestamp DateTime64(9, 'UTC'),
            service_name LowCardinality(String),
            metric_name String,
            metric_type String,
            value_float64 Float64,
            value_int64 Int64,
            attributes_json String,
            metrics_namespace LowCardinality(String) MATERIALIZED if(
                length(JSONExtractString(attributes_json, 'service_namespace')) > 0,
                JSONExtractString(attributes_json, 'service_namespace'),
                JSONExtractString(attributes_json, 'namespace')
            ),
            data_point_count UInt32,
            INDEX idx_metrics_metric_name metric_name TYPE bloom_filter(0.01) GRANULARITY 4,
            INDEX idx_metrics_namespace metrics_namespace TYPE set(256) GRANULARITY 1
        ) ENGINE = MergeTree()
        PARTITION BY toDate(timestamp)
        ORDER BY (timestamp, service_name, metric_name)
        TTL toDateTime(timestamp) + INTERVAL ${metrics_ttl_days} DAY DELETE
        SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1
    "
    # 历史集群增量补齐 metrics_namespace（避免旧表缺列导致查询回退到 JSONExtract 全扫）
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.metrics
        ADD COLUMN IF NOT EXISTS metrics_namespace LowCardinality(String)
        MATERIALIZED if(
            length(JSONExtractString(attributes_json, 'service_namespace')) > 0,
            JSONExtractString(attributes_json, 'service_namespace'),
            JSONExtractString(attributes_json, 'namespace')
        )
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.metrics
        ADD INDEX IF NOT EXISTS idx_metrics_namespace metrics_namespace TYPE set(256) GRANULARITY 1
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.metrics
        ADD INDEX IF NOT EXISTS idx_metrics_metric_name metric_name TYPE bloom_filter(0.01) GRANULARITY 4
    "

    # 创建 observability 预聚合表 v2（标准化两表模型）
    print_info "创建 logs observability 预聚合表 v2..."
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.obs_counts_1m (
            ts_minute DateTime('UTC'),
            signal Enum8('log' = 1, 'metric' = 2),
            service_name LowCardinality(String),
            dim_name LowCardinality(String),
            dim_value String,
            count UInt64,
            error_count UInt64
        ) ENGINE = SummingMergeTree()
        PARTITION BY toDate(ts_minute)
        ORDER BY (signal, ts_minute, service_name, dim_name, dim_value)
        TTL ts_minute + INTERVAL 30 DAY DELETE
        SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.obs_traces_1m (
            ts_minute DateTime('UTC'),
            service_name LowCardinality(String),
            operation_name String,
            span_count_state AggregateFunction(sum, UInt64),
            error_span_count_state AggregateFunction(sum, UInt64),
            trace_id_state AggregateFunction(uniqCombined64, String),
            error_trace_id_state AggregateFunction(uniqCombined64, String)
        ) ENGINE = AggregatingMergeTree()
        PARTITION BY toDate(ts_minute)
        ORDER BY (ts_minute, service_name, operation_name)
        TTL ts_minute + INTERVAL 30 DAY DELETE
        SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE MATERIALIZED VIEW IF NOT EXISTS logs.mv_obs_counts_1m_from_logs
        TO logs.obs_counts_1m
        AS
        SELECT
            toStartOfMinute(timestamp) AS ts_minute,
            CAST('log', 'Enum8(\\'log\\' = 1, \\'metric\\' = 2)') AS signal,
            service_name,
            'level' AS dim_name,
            level_norm AS dim_value,
            count() AS count,
            countIf(level_norm IN ('ERROR', 'FATAL')) AS error_count
        FROM logs.logs
        GROUP BY ts_minute, service_name, dim_value
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE MATERIALIZED VIEW IF NOT EXISTS logs.mv_obs_counts_1m_from_metrics
        TO logs.obs_counts_1m
        AS
        SELECT
            toStartOfMinute(timestamp) AS ts_minute,
            CAST('metric', 'Enum8(\\'log\\' = 1, \\'metric\\' = 2)') AS signal,
            service_name,
            'metric_name' AS dim_name,
            metric_name AS dim_value,
            count() AS count,
            toUInt64(0) AS error_count
        FROM logs.metrics
        GROUP BY ts_minute, service_name, dim_value
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE MATERIALIZED VIEW IF NOT EXISTS logs.mv_obs_traces_1m_from_traces
        TO logs.obs_traces_1m
        AS
        SELECT
            toStartOfMinute(timestamp) AS ts_minute,
            service_name,
            operation_name,
            sumState(toUInt64(1)) AS span_count_state,
            sumState(toUInt64(
                if(toString(status) IN ('2', 'STATUS_CODE_ERROR', 'ERROR'), 1, 0)
            )) AS error_span_count_state,
            uniqCombined64State(trace_id) AS trace_id_state,
            uniqCombined64StateIf(
                trace_id,
                toString(status) IN ('2', 'STATUS_CODE_ERROR', 'ERROR')
            ) AS error_trace_id_state
        FROM logs.traces
        GROUP BY ts_minute, service_name, operation_name
    "

    # 创建 AI 会话与消息表
    print_info "创建 logs.ai_analysis_sessions / logs.ai_analysis_messages 表..."
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.ai_analysis_sessions (
            session_id String,
            analysis_type String,
            title String,
            service_name String,
            input_text String,
            trace_id String,
            summary_text String,
            context_json String,
            result_json String,
            analysis_method String,
            llm_model String,
            llm_provider String,
            source String,
            status String,
            created_at DateTime64(3, 'UTC'),
            updated_at DateTime64(3, 'UTC'),
            is_pinned UInt8 DEFAULT 0,
            is_archived UInt8 DEFAULT 0,
            is_deleted UInt8 DEFAULT 0
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (session_id)
        SETTINGS index_granularity = 8192
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.ai_analysis_messages (
            session_id String,
            message_id String,
            msg_index UInt32,
            role String,
            content String,
            metadata_json String,
            created_at DateTime64(3, 'UTC')
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (session_id, msg_index, created_at, message_id)
        SETTINGS index_granularity = 8192
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.ai_analysis_sessions
            ADD COLUMN IF NOT EXISTS title String DEFAULT '',
            ADD COLUMN IF NOT EXISTS summary_text String DEFAULT '',
            ADD COLUMN IF NOT EXISTS is_pinned UInt8 DEFAULT 0,
            ADD COLUMN IF NOT EXISTS is_archived UInt8 DEFAULT 0
    "

    # 创建 AI 知识库表
    print_info "创建 logs.ai_cases / logs.ai_case_change_history 表..."
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.ai_cases (
            case_id String,
            problem_type String,
            severity String,
            summary String,
            log_content String,
            service_name String,
            root_causes_json String,
            solutions_json String,
            context_json String,
            tags_json String,
            similarity_features_json String,
            resolved UInt8,
            resolution String,
            created_at DateTime64(3, 'UTC'),
            updated_at DateTime64(3, 'UTC'),
            resolved_at Nullable(DateTime64(3, 'UTC')),
            llm_provider String,
            llm_model String,
            llm_metadata_json String,
            source String,
            is_deleted UInt8 DEFAULT 0
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (case_id)
        SETTINGS index_granularity = 8192
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.ai_case_change_history (
            event_id String,
            case_id String,
            event_type String,
            version UInt32,
            editor String,
            changed_fields_json String,
            changes_json String,
            requested_fields_json String,
            unchanged_requested_fields_json String,
            no_effective_change_reason String,
            effective_save_mode String,
            sync_status String,
            sync_error_code String,
            note String,
            source String,
            created_at DateTime64(3, 'UTC')
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (case_id, created_at, event_id)
        SETTINGS index_granularity = 8192
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.ai_case_change_history
            ADD COLUMN IF NOT EXISTS requested_fields_json String DEFAULT '',
            ADD COLUMN IF NOT EXISTS unchanged_requested_fields_json String DEFAULT '',
            ADD COLUMN IF NOT EXISTS no_effective_change_reason String DEFAULT ''
    "

    # 创建 Value-KPI 与发布门禁表
    print_info "创建 logs.value_kpi_snapshots / logs.release_gate_reports 表..."
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.value_kpi_snapshots (
            snapshot_id String,
            source String,
            time_window String,
            window_start DateTime64(3, 'UTC'),
            window_end DateTime64(3, 'UTC'),
            mttd_minutes Float64,
            mttr_minutes Float64,
            trace_log_correlation_rate Float64,
            topology_coverage_rate Float64,
            release_regression_pass_rate Float64,
            incident_count UInt32,
            release_gate_total UInt32,
            release_gate_passed UInt32,
            release_gate_failed UInt32,
            release_gate_bypassed UInt32,
            created_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
        )
        ENGINE = MergeTree()
        ORDER BY (created_at, snapshot_id)
        SETTINGS index_granularity = 8192
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        CREATE TABLE IF NOT EXISTS logs.release_gate_reports (
            gate_id String,
            candidate String,
            tag String,
            target String,
            started_at DateTime64(3, 'UTC'),
            finished_at DateTime64(3, 'UTC'),
            duration_ms UInt64,
            status String,
            trace_id String,
            smoke_exit_code Int32,
            trace_smoke_exit_code Int32 DEFAULT smoke_exit_code,
            ai_contract_exit_code Int32 DEFAULT 0,
            query_contract_exit_code Int32 DEFAULT 0,
            sql_safety_exit_code Int32 DEFAULT 0,
            data_retention_exit_code Int32 DEFAULT 0,
            backend_pytest_exit_code Int32 DEFAULT 0,
            p0p1_regression_exit_code Int32 DEFAULT 0,
            perf_baseline_exit_code Int32 DEFAULT 0,
            perf_trend_exit_code Int32 DEFAULT 0,
            report_path String,
            summary String,
            created_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
        )
        ENGINE = MergeTree()
        ORDER BY (started_at, gate_id)
        SETTINGS index_granularity = 8192
    "
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        ALTER TABLE logs.release_gate_reports
            ADD COLUMN IF NOT EXISTS trace_smoke_exit_code Int32 DEFAULT smoke_exit_code,
            ADD COLUMN IF NOT EXISTS ai_contract_exit_code Int32 DEFAULT 0,
            ADD COLUMN IF NOT EXISTS query_contract_exit_code Int32 DEFAULT 0,
            ADD COLUMN IF NOT EXISTS sql_safety_exit_code Int32 DEFAULT 0,
            ADD COLUMN IF NOT EXISTS data_retention_exit_code Int32 DEFAULT 0,
            ADD COLUMN IF NOT EXISTS backend_pytest_exit_code Int32 DEFAULT 0,
            ADD COLUMN IF NOT EXISTS p0p1_regression_exit_code Int32 DEFAULT 0,
            ADD COLUMN IF NOT EXISTS perf_baseline_exit_code Int32 DEFAULT 0,
            ADD COLUMN IF NOT EXISTS perf_trend_exit_code Int32 DEFAULT 0
    "

    print_success "数据库表初始化完成"

    # 显示表统计
    echo ""
    print_info "表统计："
    kubectl exec "$clickhouse_pod" -n "$NAMESPACE" -- clickhouse-client --query "
        SELECT
            name,
            formatReadableSize(total_bytes) as size,
            formatReadableQuantity(ifNull(total_rows, 0)) as rows
        FROM system.tables
        WHERE database = 'logs'
        ORDER BY name
    "
}

# 清理组件数据
clean_component() {
    local component=$1

    if [ -z "$component" ]; then
        print_error "请指定要清理的组件"
        print_info "用法: $0 clean <component>"
        return 1
    fi

    print_header "清理组件数据: $component"

    case "$component" in
        clickhouse)
            print_warning "清理 ClickHouse 数据"
            kubectl exec -it "$(kubectl get pods -n "$NAMESPACE" -l app=clickhouse -o jsonpath='{.items[0].metadata.name}')" -n "$NAMESPACE" -- clickhouse-client --query "TRUNCATE TABLE IF EXISTS logs.logs"
            kubectl exec -it "$(kubectl get pods -n "$NAMESPACE" -l app=clickhouse -o jsonpath='{.items[0].metadata.name}')" -n "$NAMESPACE" -- clickhouse-client --query "TRUNCATE TABLE IF EXISTS logs.events"
            print_success "ClickHouse 数据已清理"
            ;;
        neo4j)
            print_warning "清理 Neo4j 数据"
            kubectl exec -it "$(kubectl get pods -n "$NAMESPACE" -l app=neo4j -o jsonpath='{.items[0].metadata.name}')" -n "$NAMESPACE" -- cypher-shell -u neo4j -p password "MATCH (n) DETACH DELETE n"
            print_success "Neo4j 数据已清理"
            ;;
        redis)
            print_warning "清理 Redis 数据"
            kubectl exec -it "$(kubectl get pods -n "$NAMESPACE" -l app=redis -o jsonpath='{.items[0].metadata.name}')" -n "$NAMESPACE" -- redis-cli FLUSHALL
            print_success "Redis 数据已清理"
            ;;
        *)
            print_error "未知组件: $component"
            print_info "支持的组件: clickhouse, neo4j, redis"
            return 1
            ;;
    esac
}

# 重启组件
restart_component() {
    local component=$1

    if [ -z "$component" ]; then
        print_error "请指定要重启的组件"
        print_info "用法: $0 restart <component>"
        return 1
    fi

    print_header "重启组件: $component"

    case "$component" in
        clickhouse|neo4j|redis|ingest-service|semantic-engine|ai-service|semantic-engine-worker|query-service|topology-service|otel-gateway)
            kubectl rollout restart deployment/"$component" -n "$NAMESPACE"
            print_success "组件 $component 已重启"
            print_info "使用 '$0 status' 查看状态"
            ;;
        frontend)
            kubectl rollout restart deployment/frontend -n "$NAMESPACE"
            print_success "组件 $component 已重启"
            print_info "使用 '$0 status' 查看状态"
            ;;
        fluent-bit|fluentbit|otel-collector)
            local daemonset_name="$component"
            if [ "$component" = "fluentbit" ]; then
                daemonset_name="fluent-bit"
            fi
            kubectl rollout restart daemonset/"$daemonset_name" -n "$NAMESPACE"
            print_success "组件 $component 已重启"
            print_info "使用 '$0 status' 查看状态"
            ;;
        *)
            print_error "未知组件: $component"
            print_info "支持的组件: clickhouse, neo4j, redis, ingest-service, semantic-engine, ai-service, semantic-engine-worker, query-service, topology-service, otel-gateway, frontend, fluent-bit, otel-collector"
            return 1
            ;;
    esac
}

# 查看日志
show_logs() {
    local component=$1
    local lines=${2:-100}

    if [ -z "$component" ]; then
        print_error "请指定要查看日志的组件"
        print_info "用法: $0 logs <component> [lines]"
        return 1
    fi

    print_header "查看日志: $component (最近 $lines 行)"

    case "$component" in
        clickhouse)
            kubectl logs -l app=clickhouse -n "$NAMESPACE" --tail="$lines"
            ;;
        neo4j)
            kubectl logs -l app=neo4j -n "$NAMESPACE" --tail="$lines"
            ;;
        redis)
            kubectl logs -l app=redis -n "$NAMESPACE" --tail="$lines"
            ;;
        ingest-service)
            kubectl logs -l app=ingest-service -n "$NAMESPACE" --tail="$lines"
            ;;
        semantic-engine)
            kubectl logs -l app=semantic-engine -n "$NAMESPACE" --tail="$lines"
            ;;
        ai-service)
            kubectl logs -l app=ai-service -n "$NAMESPACE" --tail="$lines"
            ;;
        worker|semantic-engine-worker)
            kubectl logs -l app=semantic-engine-worker -n "$NAMESPACE" --tail="$lines"
            ;;
        query-service)
            kubectl logs -l app=query-service -n "$NAMESPACE" --tail="$lines"
            ;;
        topology-service)
            kubectl logs -l app=topology-service -n "$NAMESPACE" --tail="$lines"
            ;;
        frontend)
            kubectl logs -l app=frontend -n "$NAMESPACE" --tail="$lines"
            ;;
        fluent-bit|fluentbit)
            kubectl logs -l app=fluent-bit -n "$NAMESPACE" --tail="$lines"
            ;;
        otel-collector)
            kubectl logs -l app=otel-collector -n "$NAMESPACE" --tail="$lines"
            ;;
        otel-gateway)
            kubectl logs -l app=otel-gateway -n "$NAMESPACE" --tail="$lines"
            ;;
        *)
            print_error "未知组件: $component"
            print_info "支持的组件: clickhouse, neo4j, redis, ingest-service, semantic-engine, ai-service, worker, query-service, topology-service, frontend, fluent-bit, otel-collector, otel-gateway"
            return 1
            ;;
    esac
}

##############################################################################
# 帮助函数
##############################################################################

show_help() {
    cat << EOF
${GREEN}Logoscope 一键部署脚本 v4.0${NC}

${BLUE}用法:${NC}
    $0 <command> [options]

${BLUE}部署命令:${NC}
    all                    部署所有组件（推荐）
    namespace               部署命名空间
    clickhouse              部署 ClickHouse
    neo4j                   部署 Neo4j
    redis                   部署 Redis
    ingest-service          部署 Ingest Service（数据摄入）
    semantic-engine         部署 Semantic Engine API
    ai-service              部署 AI Service（LLM/会话/案例库/follow-up）
    worker                  部署 Semantic Engine Worker
    query-service           部署 Query Service（查询服务）
    topology-service        部署 Topology Service（拓扑服务）
    fluent-bit              部署 Fluent Bit
    otel-collector          部署 OTEL Collector
    otel-gateway            部署 OTEL Gateway
    frontend                部署 Frontend（前端界面）

${BLUE}管理命令:${NC}
    status                  查看所有组件状态
    health                  健康检查所有服务
    init-db                 初始化数据库表
    clean <component>       清理组件数据（clickhouse/neo4j/redis）
    restart <component>     重启组件
    logs <component> [n]    查看组件日志（默认100行）

${BLUE}示例:${NC}
    $0 all                         # 部署所有组件
    $0 status                      # 查看状态
    $0 health                      # 健康检查
    $0 init-db                     # 初始化数据库
    $0 logs semantic-engine 200    # 查看 Semantic Engine 日志（200行）
    $0 restart semantic-engine      # 重启 Semantic Engine
    $0 clean clickhouse            # 清理 ClickHouse 数据

${BLUE}组件部署顺序:${NC}
    1. namespace (islap)
    2. clickhouse (时序数据库)
    3. neo4j (图数据库)
    4. redis (缓存/队列)
    5. ingest-service (数据摄入服务)
    6. semantic-engine (语义分析 API)
    7. ai-service (AI API)
    8. worker (异步处理 Worker)
    9. query-service (查询服务)
    10. topology-service (拓扑服务)
    11. fluent-bit (日志采集)
    12. otel-collector (OTel 采集器)
    13. otel-gateway (OTel 网关)
    14. frontend (前端界面)
    15. value-kpi-cronjob (价值指标周任务)

${BLUE}服务地址:${NC}
    Ingest Service:  http://10.43.167.123:8080
    Semantic Engine:  http://10.43.231.27:8080
    AI Service:       http://ai-service.islap.svc:8090
    Query Service:    http://query-service.islap.svc:8080
    Topology Service: http://topology-service.islap.svc:8080
    Frontend:         http://frontend.islap.svc:80
    OTEL Gateway:     http://10.43.29.2:4318
    ClickHouse:       10.43.11.236:9000
    Neo4j:            10.43.125.222:7687
    Redis:            10.43.13.32:6379

EOF
}

##############################################################################
# 主程序
##############################################################################

main() {
    # 检查环境
    check_k3s
    check_kubectl
    setup_kubectl

    # 执行命令
    local command=$1
    shift || true

    case "$command" in
        all)
            deploy_all
            ;;
        namespace)
            deploy_namespace
            ;;
        clickhouse)
            deploy_clickhouse
            ;;
        neo4j)
            deploy_neo4j
            ;;
        redis)
            deploy_redis
            ;;
        semantic-engine)
            deploy_semantic_engine
            ;;
        ai-service)
            deploy_ai_service
            ;;
        worker)
            deploy_worker
            ;;
        fluent-bit|fluentbit)
            deploy_fluentbit
            ;;
        otel-collector)
            deploy_otel_collector
            ;;
        otel-gateway)
            deploy_otel_gateway
            ;;
        ingest-service)
            deploy_ingest_service
            ;;
        query-service)
            deploy_query_service
            ;;
        topology-service)
            deploy_topology_service
            ;;
        frontend)
            deploy_frontend
            ;;
        value-kpi-cronjob|kpi-cronjob)
            deploy_value_kpi_cronjob
            ;;
        status)
            show_status
            ;;
        health)
            health_check
            ;;
        init-db|initdb|init_database)
            init_database
            ;;
        clean)
            clean_component "$@"
            ;;
        restart)
            restart_component "$@"
            ;;
        logs)
            show_logs "$@"
            ;;
        help|--help|-h|"")
            show_help
            ;;
        *)
            print_error "未知命令: $command"
            echo ""
            show_help
            exit 1
            ;;
    esac
}

# 运行主程序
main "$@"
