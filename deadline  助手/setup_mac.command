#!/bin/bash
cd "$(dirname "$0")"
echo "正在安装「作业泡泡」运行环境..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
./.venv/bin/pip install -e .
./.venv/bin/playwright install chromium
echo "安装完成！请双击 run_mac.command 启动。"
read -p "按回车键退出..."
