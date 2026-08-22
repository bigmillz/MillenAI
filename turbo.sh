#!/bin/bash
# ConcordeAI Turbo — point the app at a free cloud GPU in one step.
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

  ConcordeAI Turbo
  ────────────────
  Answers come from a cloud GPU instead of your Mac. Much faster,
  but your prompts leave this computer while it is on.

  1  Groq          free, very fast, 120B       console.groq.com/keys
  2  Google Gemini free tier, frontier-class    aistudio.google.com/apikey
  3  Anthropic     Claude — PAID, best quality  console.anthropic.com
  4  xAI (Grok)    grok models, paid credits    console.x.ai
  5  OpenRouter    many models, free tier       openrouter.ai/keys
  6  Cloudflare    workers ai, free tier        dash.cloudflare.com

INTRO

# defaults first: a stray keystroke must never leave these unset
NAME="Groq 120B"; BASE="https://api.groq.com/openai/v1"
MODEL="openai/gpt-oss-120b"

PICK=""
read -r -p "  provider [1-6, default 1]: " PICK || true
case "${PICK:-1}" in
  2) NAME="Gemini";     BASE="https://generativelanguage.googleapis.com/v1beta/openai"
     MODEL="gemini-2.5-flash" ;;
  3) NAME="Claude";     BASE="https://api.anthropic.com/v1"
     MODEL="claude-sonnet-4-5" ;;
  4) NAME="xAI Grok";   BASE="https://api.x.ai/v1"
     MODEL="grok-4-fast" ;;
  5) NAME="OpenRouter"; BASE="https://openrouter.ai/api/v1"
     MODEL="meta-llama/llama-3.3-70b-instruct:free" ;;
  6) NAME="Cloudflare Workers AI"
     CFACC=""
     read -r -p "  cloudflare account id: " CFACC || true
     BASE="https://api.cloudflare.com/client/v4/accounts/${CFACC}/ai/v1"
     MODEL="@cf/meta/llama-3.3-70b-instruct-fp8-fast" ;;
  *) : ;;   # keep the Groq defaults
esac

echo "  open that provider's page, create a key, then paste it here."
KEY=""
read -r -s -p "  api key (hidden): " KEY || true
echo
[[ -n "$KEY" ]] || { echo "  no key given — nothing changed."; exit 1; }

echo "  testing $NAME ($MODEL)…"
if [[ "$BASE" == *anthropic.com* ]]; then
  RESP=$(curl -s -m 25 "$BASE/messages" \
    -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}]}" \
    || true)
else
  RESP=$(curl -s -m 25 "$BASE/chat/completions" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}]}" \
    || true)
fi

if ! grep -q '"content"' <<<"$RESP"; then
  echo "  that did not work. the provider said:"
  python3 -c 'import json,sys
try:
    d=json.load(sys.stdin); e=d.get("error",d)
    print("   ", e.get("message") or e.get("code") or str(e)[:160])
except Exception:
    print("   ", sys.stdin.read()[:160] or "(no response)")' <<<"$RESP" || true
  echo "  nothing was saved."
  exit 1
fi

mkdir -p "$(dirname "$CFG")"
# the key goes to python through the environment, never on a command line
# (argv is visible to every process on the machine via ps)
MILLEN_KEY="$KEY" python3 - "$CFG" "$NAME" "$BASE" "$MODEL" <<'PY'
import json, os, sys
cfg, name, base, model = sys.argv[1:5]
with open(cfg, "w") as f:
    json.dump({"name": name, "base": base,
               "key": os.environ["MILLEN_KEY"], "model": model}, f)
PY
chmod 600 "$CFG"
arm true

echo
echo "  ✓ $NAME is live — $MODEL"
echo "    Turbo is ON. Settings ▸ Turbo turns it off any time,"
echo "    or run ./turbo.sh off"
