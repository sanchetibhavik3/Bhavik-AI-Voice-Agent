#!/bin/bash
set -e
cd "$(dirname "$0")"
source .env 2>/dev/null || true

echo "🚀 Starting Outbound Mass Caller..."

echo "📋 Configuration:"
echo "   LiveKit: ${LIVEKIT_URL}"
echo "   Gemini: ${GEMINI_MODEL:-gemini-2.0-flash-live-001}"
echo "   Supabase: ${SUPABASE_URL}"

echo "🌐 Starting FastAPI server on port 8000..."
python3 -m uvicorn server:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

sleep 2

echo "🤖 Starting LiveKit agent worker..."
python3 agent.py start

kill $SERVER_PID 2>/dev/null || true
