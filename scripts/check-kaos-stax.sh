#!/bin/bash
# KAOS Health Check — STAX_ASP Monitor
# Check: process status, .kaos logs, recent output files, compile errors

STAX_DIR="/home/ka/Repos/github.com/trongnghiango/STAX_ASP"
KAOS_DIR="/home/ka/Repos/github.com/trongnghiango/kaos"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

echo "=== KAOS Health Check — $TIMESTAMP ==="

# 1. Check KAOS process
KAOS_PID=$(pgrep -f "kaos.*auto.*STAX_ASP" 2>/dev/null | head -1)
if [ -n "$KAOS_PID" ]; then
    KAOS_RUNNING="YES (PID: $KAOS_PID)"
    KAOS_ELAPSED=$(ps -o etime= -p "$KAOS_PID" 2>/dev/null | xargs)
else
    KAOS_RUNNING="NO"
    KAOS_ELAPSED="N/A"
fi
echo "[PROCESS] kaos --auto running: $KAOS_RUNNING (elapsed: $KAOS_ELAPSED)"

# 2. Check .kaos workspace state
WORK_DIR="$STAX_DIR/.kaos"
if [ -d "$WORK_DIR" ]; then
    TMP_COUNT=$(find "$WORK_DIR/tmp" -maxdepth 1 -type d 2>/dev/null | wc -l)
    LATEST_TMP=$(ls -1t "$WORK_DIR/tmp/" 2>/dev/null | head -1)
    LATEST_DIR="$WORK_DIR/tmp/$LATEST_TMP"
    echo "[WORKDIR] .kaos/tmp sessions: $((TMP_COUNT - 1))"
    echo "[WORKDIR] Latest session: $LATEST_TMP"

    # 3. Check recent files inside latest session
    if [ -d "$LATEST_DIR" ]; then
        JSON_COUNT=$(find "$LATEST_DIR" -name "*.json" -type f 2>/dev/null | wc -l)
        echo "[SESSION] JSON files: $JSON_COUNT"
        ls -la "$LATEST_DIR/" 2>/dev/null | head -10

        # Check for act_out files (execution output from Goose)
        ACT_OUT=$(find "$LATEST_DIR" -name "act_out_*.json" -type f 2>/dev/null | wc -l)
        echo "[SESSION] act_out files: $ACT_OUT"

        # Check scout_spec_result (did spec parsing work?)
        if [ -f "$LATEST_DIR/scout_spec_result.json" ]; then
            SPEC_REQS=$(grep -o '"requirements"' "$LATEST_DIR/scout_spec_result.json" | wc -l)
            echo "[SCOUT] scout_spec_result.json: found (requirements field present)"
            head -c 200 "$LATEST_DIR/scout_spec_result.json"
            echo ""
        else
            echo "[SCOUT] scout_spec_result.json: NOT FOUND"
        fi
    fi
fi

# 4. Check logs
LOG_DIR="$WORK_DIR/logs"
if [ -d "$LOG_DIR" ]; then
    LOG_COUNT=$(find "$LOG_DIR" -name "*.log" -type f 2>/dev/null | wc -l)
    LATEST_LOG=$(ls -1t "$LOG_DIR/" 2>/dev/null | head -1)
    echo "[LOGS] Log files: $LOG_COUNT"
    if [ -n "$LATEST_LOG" ]; then
        LOG_SIZE=$(stat --printf="%s" "$LOG_DIR/$LATEST_LOG" 2>/dev/null)
        echo "[LOGS] Latest: $LATEST_LOG (${LOG_SIZE}b)"
        # Last 5 lines for summary
        echo "[LOGS] --- last 5 lines ---"
        tail -5 "$LOG_DIR/$LATEST_LOG" 2>/dev/null
        echo "[LOGS] ---"
    fi
fi

# 5. Check for compile errors in STAX_ASP
echo "[TYPECHECK] Running tsc --noEmit for baseline..."
cd "$STAX_DIR/backend" 2>/dev/null && npx tsc --noEmit 2>&1 | grep -c "error TS" || echo "0 type errors"
cd "$STAX_DIR/packages/contracts" 2>/dev/null && npx tsc --noEmit 2>&1 | tail -3
cd "$STAX_DIR/packages/db-schema" 2>/dev/null && npx tsc --noEmit 2>&1 | tail -3

echo "=== END CHECK ==="
