#!/usr/bin/env sh
# 在 Linux / macOS 上启动星枢。参数会原样传给 run.py，例如：
#   ./start-linux.sh --demo
#   ./start-linux.sh --host 0.0.0.0 --password 你的密码
cd "$(dirname "$0")" || exit 1
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    exec "$py" run.py "$@"
  fi
done
echo "没有找到 Python 3.8 或更新版本，请先安装（例如 Ubuntu / Debian：sudo apt install python3）" >&2
exit 1
