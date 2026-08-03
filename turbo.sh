#!/bin/bash
# MillenAI Turbo — point the app at a free cloud GPU in one step.
#
#   ./turbo.sh            interactive: paste your key, it does the rest
#   ./turbo.sh off        back to fully local
#
# Your key is read by THIS script on YOUR machine, written to a 0600 file,
# and never printed, logged, or sent anywhere except the provider you pick.
set -euo pipefail
CFG="$HOME/Library/Application Support/MillenAI/cloud.json"
PORT="${MILLENAI_PORT:-8889}"

arm() {  # arm|disarm the switch in the running app (no-op if it's closed)
  curl -s -m 3 -X POST "http://127.0.0.1:$PORT/api/prefs" \
    -H "Content-Type: application/json" \
    -d "{\"turbo\":$1}" >/dev/null 2>&1 || true
}

if [[ "${1:-}" == "off" ]]; then
  arm false
  echo "Turbo off — every answer is local again."
  echo "(the config stays at $CFG; run ./turbo.sh to re-arm)"
  exit 0
fi

cat <<'INTRO'

  MillenAI Turbo
  ──────────────
  Answers come from a free cloud GPU instead of your Mac. Much faster,
  but your prompts leave this computer while it is on.

  1  Groq          fastest, biggest free tier   console.groq.com/keys
  2  OpenRouter    many models, free tier       openrouter.ai/keys
  3  Cloudflare    workers ai                   dash.cloudflare.com

INTRO
read -r -p "  provider [1-3, default 1]: " PICK
PICK="${PICK:-1}"
case "$PICK" in
  2) NAME="OpenRouter"; BASE="https://openrouter.ai/api/v1"
     MODEL="meta-llama/llama-3.3-70b-instruct:free" ;;
  3) NAME="Cloudflare Workers AI"
     read -r -p "  cloudflare account id: " CFACC
     BASE="https://api.cloudflare.com/client/v4/accounts/$CFACC/ai/v1"
     MODEL="@cf/meta/llama-3.3-70b-instruct-fp8-fast" ;;
  *) NAME="Groq"; BASE="https://api.groq.com/openai/v1"
     MODEL="llama-3.3-70b-versatile" ;;
esac

echo "  open the page above, create a key, then paste it here."
read -r -s -p "  api key (hidden): " KEY; echo
[[ -n "$KEY" ]] || { echo "  no key given — nothing changed."; exit 1; }

echo "  testing $NAME…"
RESP=$(curl -s -m 25 "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}]}" \
  || true)

if ! grep -q '"content"' <<<"$RESP"; then
  echo "  that did not work. the provider said:"
  sed -e 's/{"error"/\n  /' <<<"$RESP" | head -4
  echo "  nothing was saved."
  exit 1
fi

mkdir -p "$(dirname "$CFG")"
umask 177
python3 - "$CFG" "$NAME" "$BASE" "$MODEL" <<'PY' <<<"$KEY"
import json, sys
cfg, name, base, model = sys.argv[1:5]
key = sys.stdin.readline().strip()
json.dump({"name": name, "base": base, "key": key, "model": model},
          open(cfg, "w"))
PY
chmod 600 "$CFG"
arm true

echo
echo "  ✓ $NAME is live — $MODEL"
echo "    Turbo is ON. Settings ▸ Turbo turns it off any time,"
echo "    or run ./turbo.sh off"
echo
