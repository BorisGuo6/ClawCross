#!/bin/bash
# Cloudflare Tunnel 公网部署（独立使用）
# 用法: bash scripts/tunnel.sh

PROJECT_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$PROJECT_ROOT"

source "$PROJECT_ROOT/selfskill/scripts/_paths.sh"
clawcross_init_paths

# 激活虚拟环境
if [ -f "$CLAWCROSS_VENV_DIR/bin/activate" ]; then
    source "$CLAWCROSS_VENV_DIR/bin/activate"
fi

python scripts/tunnel.py
