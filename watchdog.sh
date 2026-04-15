#!/bin/bash
# DevAlert + FinAPI 健康检查与自动重启
# 每2分钟由cron调用

check_health() {
    local port=$1
    response=$(curl -s -m 5 "http://localhost:${port}/health" 2>/dev/null)
    if echo "$response" | grep -q '"status":"ok"'; then
        return 0
    fi
    return 1
}

restart_service() {
    local port=$1
    local workdir=$2
    local log=$3
    pkill -f "uvicorn main:app --host 0.0.0.0 --port ${port}" 2>/dev/null
    sleep 1
    cd "$workdir"
    nohup python3 -m uvicorn main:app --host 0.0.0.0 --port ${port} > "$log" 2>&1 &
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Restarted service on :${port}" >> /tmp/services_watchdog.log
}

# DevAlert (:8001)
if ! check_health 8001; then
    restart_service 8001 /workspace/devalert /tmp/devalert.log
fi

# FinAPI (:8000)
if ! check_health 8000; then
    restart_service 8000 /workspace/finapi /tmp/finapi.log
fi
