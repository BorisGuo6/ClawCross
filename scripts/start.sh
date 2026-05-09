#!/bin/bash
# WeBot 启动脚本（直接启动服务，跳过环境配置）
# 实际启动逻辑统一由 launcher.py 管理

PROJECT_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$PROJECT_ROOT"

source "$PROJECT_ROOT/selfskill/scripts/_paths.sh"
clawcross_init_paths

# 激活虚拟环境（如果存在）
if [ -f "$CLAWCROSS_VENV_DIR/bin/activate" ]; then
    source "$CLAWCROSS_VENV_DIR/bin/activate"
fi

# 调用 Python 启动器
exec python scripts/launcher.py
