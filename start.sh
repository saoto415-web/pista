#!/bin/bash
# PISTA 起動スクリプト
# 使い方: bash start.sh [ngrokドメイン]
# 例:    bash start.sh myname.ngrok-free.app

DOMAIN=${1:-""}
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚴 PISTA 競輪AI 起動中..."

# 既存プロセス停止
pkill -f "streamlit run dashboard.py" 2>/dev/null
pkill -f "ngrok http" 2>/dev/null
sleep 1

# Streamlit 起動
cd "$DIR"
nohup streamlit run dashboard.py --server.port 8504 --server.headless true \
  > /tmp/pista_streamlit.log 2>&1 &
echo "✅ Streamlit 起動 (http://localhost:8504)"

# ngrok 起動
if [ -n "$DOMAIN" ]; then
  nohup ngrok http --domain="$DOMAIN" 8504 \
    > /tmp/pista_ngrok.log 2>&1 &
  sleep 3
  echo "✅ ngrok 起動"
  echo "📱 スマホURL: https://$DOMAIN"
else
  nohup ngrok http 8504 > /tmp/pista_ngrok.log 2>&1 &
  sleep 3
  URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c \
    "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])" 2>/dev/null)
  echo "📱 スマホURL: $URL"
fi
