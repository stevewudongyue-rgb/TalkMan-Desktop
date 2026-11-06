#!/bin/bash
# 在 Apple Silicon Mac 中双击；也可在终端执行 bash 后拖入本文件。
set -e
cd -- "$(dirname -- "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "请先安装 python.org 的 macOS Python 3.12 或 3.13，然后重新运行。"
  exit 1
fi
python3 "TalkMan-构建Mac.py" "$@"
