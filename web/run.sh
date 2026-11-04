#!/usr/bin/env bash
# LLM Wiki web 服务托管脚本：退出后 3 秒自动重启
# 用法: setsid bash /home/hunt/work/llm_wiki/web/run.sh &
# 停止: pkill -f "server[.]py --port 8899" 且 pkill -f "run[.]sh"
while :; do
  python3 "$(dirname "$0")/server.py" --port 8899 >> /tmp/llmwiki-web.log 2>&1
  echo "$(date +%T) web exited code=$? restart in 3s" >> /tmp/llmwiki-web.log
  sleep 3
done
