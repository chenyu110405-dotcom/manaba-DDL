#!/bin/bash
cd "$(dirname "$0")"
if [ ! -f ".venv/bin/python" ]; then
    echo "请先双击运行 setup_mac.command 安装依赖环境。"
    read -p "按回车键退出..."
    exit 1
fi
(sleep 1 && open "http://127.0.0.1:8765/") &
./.venv/bin/python -m manaba_deadline serve
