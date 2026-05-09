#!/bin/bash
# 添加用户脚本 (Linux / macOS)

PROJECT_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$PROJECT_ROOT"

source "$PROJECT_ROOT/selfskill/scripts/_paths.sh"
clawcross_init_paths

# 激活虚拟环境（如果存在）
if [ -f "$CLAWCROSS_VENV_DIR/bin/activate" ]; then
    source "$CLAWCROSS_VENV_DIR/bin/activate"
fi

python tools/gen_password.py
