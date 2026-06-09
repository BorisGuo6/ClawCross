#!/usr/bin/env bash
# ClawCross WeChat wrapper installer.
# Creates ~/.clawcross/bin/clawcross_wechat so ClawCross never exposes legacy
# channel naming in user-facing config.
set -euo pipefail

bin_dir="${CLAWCROSS_WECHAT_BIN_DIR:-$HOME/.clawcross/bin}"
wrapper="${CLAWCROSS_WECHAT_BIN:-$bin_dir/clawcross_wechat}"
wrapper="$(python3 -c 'import os,sys; print(os.path.expanduser(sys.argv[1]))' "$wrapper")"

if [[ -x "$wrapper" ]]; then
    echo "ClawCross WeChat wrapper already exists: $wrapper"
    "$wrapper" version 2>/dev/null || true
    exit 0
fi

upstream="${CLAWCROSS_WECHAT_UPSTREAM_BIN:-}"
if [[ -z "$upstream" ]]; then
    cat >&2 <<'EOF'
CLAWCROSS_WECHAT_UPSTREAM_BIN is not set.

Install a compatible local WeChat bridge executable first, then run:
  CLAWCROSS_WECHAT_UPSTREAM_BIN=/absolute/path/to/bridge bash scripts/clawcross_wechat_install.sh
EOF
    exit 1
fi

if command -v "$upstream" >/dev/null 2>&1; then
    upstream="$(command -v "$upstream")"
else
    upstream="$(python3 -c 'import os,sys; print(os.path.expanduser(sys.argv[1]))' "$upstream")"
fi

if [[ ! -x "$upstream" ]]; then
    echo "CLAWCROSS_WECHAT_UPSTREAM_BIN is not executable: $upstream" >&2
    exit 1
fi

mkdir -p "$(dirname "$wrapper")"
cat >"$wrapper" <<EOF
#!/usr/bin/env bash
exec "$upstream" "\$@"
EOF
chmod +x "$wrapper"

echo "Installed ClawCross WeChat wrapper: $wrapper"
echo "Set CLAWCROSS_WECHAT_BIN=$wrapper"
