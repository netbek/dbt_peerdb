#!/usr/bin/env bash
# Install ClickHouse config for tests
# Usage: install-clickhousectl.sh (no args)
set -euo pipefail

mkdir -p ~/.clickhouse/configs
cat > ~/.clickhouse/configs/dbt_peerdb.yaml <<'EOF'
query_log:
    database: system
    table: query_log
EOF
