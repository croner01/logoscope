#!/bin/bash
# Pod 重启监控脚本

echo "=== Logoscope Pods 监控 ==="
echo "开始时间: $(date)"
echo ""

while true; do
    clear
    echo "=== Logoscope Pods 状态 ($(date +%H:%M:%S)) ==="
    echo ""
    
    echo "Semantic Engine:"
    kubectl get pods -n islap -l app=semantic-engine --no-headers | \
        awk '{printf "  %s\t状态: %s\t重启: %s\t运行时长: %s\n", $1, $3, $4, $5}'
    
    echo ""
    echo "OTel Collector:"
    kubectl get pods -n islap -o wide | grep collector | \
        awk '{printf "  %s\t状态: %s\t重启: %s\t运行时长: %s\n", $1, $3, $4, $5}'
    
    echo ""
    echo "OTel Gateway:"
    kubectl get pods -n islap -l app=otel-gateway --no-headers | \
        awk '{printf "  %s\t状态: %s\t重启: %s\t运行时长: %s\n", $1, $3, $4, $5}'
    
    echo ""
    echo "按 Ctrl+C 退出"
    sleep 60
done
