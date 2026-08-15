"""MillenAI full-surface smoke test — the Fable-worthiness gate.

Runs against a locally spawned instance with a key (so every gate is
exercised) and reports a scorecard. Engine tests run REAL models.
"""
import json
import re
import sys
import time
import urllib.request
import urllib.error
from collections import Counter

BASE = "http://127.0.0.1:9894"
KEY = "smoketestkey123"
K = "millen_key=" + KEY

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("  — " + detail if detail and not ok else ""))


def req(path, method="GET", data=None, headers=None, cookie=None, timeout=30):
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    if data is not None and not isinstance(data, bytes):
        data = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


print("== access control ==")
# the key door is retired (1.20): local goes straight to the app, remote
# strangers land on the account screen
s, h, b = req("/")
check("local bare URL -> app", s == 200 and b"id=\"skyline\"" in b)
s, h, b = req("/?key=oldlink")
check("legacy key links still land", s == 200 and b"id=\"skyline\"" in b)
s, h, b = req("/", headers={"X-Forwarded-For": "1.2.3.4"})
check("remote stranger -> account screen", b"continue as guest" in b.lower()
      and b"pinform" in b)

print("== identities ==")
s, h, b = req("/", cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
check("remote no-identity -> sign-in", b"continue as guest" in b.lower())
s, h, b = req("/api/guest", "POST", {}, cookie=K,
              headers={"X-Forwarded-For": "1.2.3.4"})
mg = re.search(r"millen_user=([0-9a-f]{20})", str(h))
check("guest tap mints an identity", s == 200 and mg)
s, h, b = req("/api/welcome", "POST", {"name": "smoke", "pin": "1234"},
              cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
check("short PIN rejected", b"8-12 digit" in b)
s, h, b = req("/api/welcome", "POST", {"name": "smoke", "pin": "88881111"},
              cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
m = re.search(r"millen_user=([0-9a-f]{20})", str(h))
check("8-digit PIN -> identity cookie", s == 200 and m)
smoke_uid = m.group(1) if m else ""
s, h, b = req("/api/chats", cookie=K + "; millen_user=" + smoke_uid,
              headers={"X-Forwarded-For": "1.2.3.4"})
check("fresh profile sees empty chats", b == b'{"chats": []}')
s, h, b = req("/api/chats", cookie=K)
check("local owner sees real chats", b"title" in b)
own_pin = open("/Users/patrickmiller/Library/Application Support/MillenAI/owner_pin").read().strip()
s, h, b = req("/api/welcome", "POST", {"name": "anyname", "pin": own_pin},
              cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
m2 = re.search(r"millen_user=([0-9a-f]{20})", str(h))
s, h, b = req("/api/chats", cookie=K + "; millen_user=" + (m2.group(1) if m2 else ""),
              headers={"X-Forwarded-For": "1.2.3.4"})
check("owner PIN opens real chats remotely", b"title" in b)

print("== admin lockdown ==")
for p in ("/api/speak", "/api/model/download", "/api/open-logs",
          "/api/update/install", "/api/voice/prepare"):
    s, h, b = req(p, "POST", {}, cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
    check("remote blocked: " + p, b"owner only" in b)
s, h, b = req("/api/speak", "POST", {"stop": True}, cookie=K)
check("local speak allowed", b'"ok": true' in b)

print("== backdrop system ==")
s, h, b = req("/api/sky/cached", cookie=K)
cached = json.loads(b).get("cached", [])
check("cached list non-empty", len(cached) >= 1, str(cached))
if cached:
    i = cached[0]
    s, h, b = req(f"/api/sky/status?i={i}", cookie=K)
    check("cached clip reports ready", b'"ready"' in b)
    s, h, _ = req(f"/sky/{i}.mov", cookie=K, headers={"Range": "bytes=0-1023"})
    check("range serving 206", s == 206)
    s, h, _ = req(f"/sky/{i}.mov", cookie=K, headers={"Range": "bytes=-1024"})
    check("suffix range 206", s == 206)
s, h, b = req("/", cookie=K)
page = b.decode("utf-8", "replace")
check("SKY_N injected", re.search(r'parseInt\("\d+",10\)', page))
check("dark list injected", "darkSet=new Set(JSON.parse('[0, 3, 4" in page)

print("== page integrity ==")
leftovers = [t for t in re.findall(r"__[A-Z_]{3,}__", page)
             if t not in ("__MAIN__",)]
check("no unreplaced template tokens", not leftovers, str(leftovers[:5]))
check("no raw NUL bytes", b"\x00" not in b)
# 6.0b2: no in-app hero branding — greeting IS the hero (Claude-style);
# the only wordmark is the frame-wide sidebar header
# NB: ".h1row" survives as a dead CSS selector + haloTick query —
# assert the MARKUP is gone, not the substring
check("hero is greeting-only", '<p class="greet"' in page
      and 'class="h1row"' not in page)
check("frame-wide sidebar wordmark", "--sbw,384px)*.105" in page
      and 'class="vsub"' in page)
check("mobile drawer present", 'id="mburger"' in page)
check("tier dropdown js present", "tierRows.classList" in page)
check("arena removed", "arena" not in page.lower())
check("blend progress bar css", ".blendprog" in page)
check("serene entrance css", "heroIn 2.6s" in page and "shockOut" not in page)
# 5.2 surface
check("three-tab selector, glide in thirds",
      'data-m="code"' in page and "translateX(200%)" in page)
check("code tab carries Coding + Workspace",
      'data-agent="Coding"' in page and 'data-agent="Workspace"' in page)
check("query pinwheel css", ".wtspin" in page and "wtspin 1.5s" in page)
# NB: the JS boolean `lfgWashed` legitimately survives — only the old
# keyframes must be gone
check("centered LFG drop replaces the drive-by",
      "lfgOut" in page and "lfgBloom" in page
      and "keyframes lfgWash{" not in page)
check("backdrop pantry js", "millen.skynext" in page
      and "fillPantry" in page and "PANTRY=5" in page)
# 5.3.2 surface: lane-aware sidebar + iconed tabs, AI renamed Chat
check("lane-aware sidebar js", "laneOK" in page and ".cempty" in page
      and "switchLane" in page)
check("tabs are iconed and AI reads Chat",
      page.count("#mode-tabs .ltab svg") >= 1 and ">Chat</span>" in page
      and ">AI</span>" not in page)
# 5.3.3: reveal masks must be dropped once the flourish lands, or a
# stalled transition leaves a permanent seam ("weird edge thing")
check("post-flourish mask teardown css+js",
      "paintdone" in page and "mask-image:none!important" in page)
# 5.3.5: the halo is CANVAS pixels now — live CSS blur on it raster-
# clipped in Blink and misrendered in WebKit (the seam, three ways)
check("canvas halo replaces the filtered one",
      "haloTick" in page and "halo-cv" in page
      and ".halo{display:none}" in page)
check("pantry rotates a fresh clip per session",
      "THE SHELF ROTATES" in page)
# 5.3.6: a stocked pantry overrides the first-run dark-set preference —
# private-mode WKWebView wiped localStorage every launch until now
check("veteran pantry overrides first-run dark set",
      "stocked pantry is proof" in page)
# 6.0: the brand is Concorde on every user-facing surface; the old name
# survives only in internals (paths, bundle id, repo) which never
# reach the page
check("Concorde brand, no stray MillenAI",
      "Concorde" in page and "MillenAI" not in page)

print("== resolvers ==")
s, h, b = req("/api/tiers", cookie=K)
tiers = json.loads(b)
# 5.3: no skip list — Pro (all-models) must resolve like everything else
check("every tier resolves", all(t.get("models") for t in tiers.values()),
      str({n: t.get("models") for n, t in tiers.items()}))
check("Best and Power tiers are gone",
      "Best" not in tiers and "Power" not in tiers, str(list(tiers)))
s, h, b = req("/api/stats", cookie=K)
st = json.loads(b)
check("stats has users + memory", "users_total" in st and "mem_total_gb" in st)

print("== engines (live generations) ==")


def chat(payload, timeout=600):
    s, h, b = req("/api/chat", "POST", payload, cookie=K, timeout=timeout)
    text = b.decode("utf-8", "replace")
    text = re.sub("\x00STATUS:.*?\x00", "", text)
    text = re.sub("\x00DRAFT:.*?\x00", "", text)
    cut = text.rfind("\x00RESET\x00")
    if cut >= 0:
        text = text[cut + 7:]
    return text.strip()


def healthy(text):
    words = re.findall(r"[a-z']+", text.lower())
    grams = Counter(tuple(words[i:i + 3]) for i in range(max(0, len(words) - 2)))
    rep = max(grams.values()) if grams else 0
    return len(text) > 300 and rep <= 8 and "⚠️" not in text, \
        f"{len(text)} chars, 3gram x{rep}"


# Fast and Smart merged in 1.20: Fast now runs the strongest fitting
# model, so it earns the strict health bar
t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": False,
          "messages": [{"role": "user", "content": "tell me about central park"}]})
ok, d = healthy(t)
check("Fast tier answer healthy", ok, d)

t = chat({"model": "", "models": [], "tier": "Smart", "auto_web": False,
          "messages": [{"role": "user", "content": "give me a great one-day brooklyn itinerary"}]})
ok, d = healthy(t)
check("legacy Smart alias still answers", ok, d)

t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": True,
          "messages": [{"role": "user", "content": "whats the weather in 11221"}]})
check("weather answer carries real data", ("°F" in t or "degrees" in t or " mph" in t)
      and "⚠️" not in t and len(t) > 60, t[:120])

# a place no index knows must NOT get a bare "couldn't find any info"
# shrug (3.3) — the answer says so plainly AND asks a pin-down question
t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": True,
          "messages": [{"role": "user", "content": "is qzxvbn cafe in bushwick open tonight"}]})
check("unknown place gets helpful no-match answer",
      len(t) > 100 and "?" in t and "⚠️" not in t
      # the shape is taught by a Milano's/Ridgewood worked example —
      # its names leaking into the answer means the fence failed
      and "milano" not in t.lower() and "ridgewood" not in t.lower(),
      t[:160])


print("== attached files ==")
t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": True,
          "messages": [{"role": "user", "content": "what is the project codename mentioned in this file?"}],
          "docs": [{"name": "notes.txt",
                    "text": "quarterly planning notes\nthe project codename is ZEBRA-42\nlunch is at noon"}]})
# models emit fancy hyphens (ZEBRA‑42 with U+2011) — normalize first
flat = re.sub(r"[^a-z0-9]+", "", t.lower())
affirms = "zebra42" in flat and not re.search(
    r"not seeing|don't see|do not see|there is no|isn't a|can't help", t.lower())
check("doc content reaches the model", affirms, t[:160])

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
       "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
t = chat({"model": "", "models": [], "tier": "", "auto_web": False,
          "messages": [{"role": "user", "content": "what color is this image?"}],
          "images": ["data:image/png;base64," + PNG]})
check("vision answers about the pixels", "red" in t.lower() and "⚠️" not in t,
      t[:100])

print()
passed = sum(1 for _n, o, _d in RESULTS if o)
print(f"SCORECARD: {passed}/{len(RESULTS)} passed")
for n, o, d in RESULTS:
    if not o:
        print("  FAILED:", n, "—", d)
sys.exit(0 if passed == len(RESULTS) else 1)
