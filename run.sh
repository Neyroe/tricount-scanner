#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create .env from example if missing
if [ ! -f .env ]; then
  cp .env.example .env
  echo "⚠️  Fichier .env créé — renseigne ta clé ANTHROPIC_API_KEY dans .env avant de relancer."
  exit 1
fi

# Get local IP for mobile access
LOCAL_IP=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || hostname -I | awk '{print $1}')

echo ""
echo "  🧾  Tricount Scanner"
echo "  ─────────────────────────────────────"
echo "  Local   : http://localhost:8000"
echo "  Mobile  : http://${LOCAL_IP}:8000"
echo "  ─────────────────────────────────────"
echo "  (Les deux appareils doivent être sur le même Wi-Fi)"
echo ""

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
