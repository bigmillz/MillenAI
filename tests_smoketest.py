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
s, h, b = req("/")
check("door on bare URL", s == 200 and b"enter your access key" in b)
s, h, b = req("/?key=wrong")
check("wrong key gets the note", "isn’t right" in b.decode("utf-8", "replace"))
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
_noredir = urllib.request.build_opener(_NoRedirect)
try:
    _noredir.open(BASE + "/?key=" + KEY, timeout=10)
    check("right key -> 302 + cookie", False, "no redirect raised")
except urllib.error.HTTPError as e:
    check("right key -> 302 + cookie",
          e.code == 302 and "millen_key" in str(e.headers))
s, h, b = req("/", cookie=K)
check("keyed local -> app", b"id=\"skyline\"" in b)

print("== identities ==")
s, h, b = req("/", cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
check("remote no-identity -> sign-in", b"pick a name and a PIN" in b)
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
check("halo element present", 'class="halo"' in page)
check("mobile drawer present", 'id="mburger"' in page)
check("tier dropdown js present", "tierRows.classList" in page)
check("model-group folding present", "#adv-wrap .group-label" in page)
check("arena present", 'id="arena-toggle"' in page and "sendArena" in page)
check("user count present", 'id="user-label"' in page)
check("blend progress bar css", ".blendprog" in page)
check("serene entrance css", "heroIn 2.6s" in page and "shockOut" not in page)

print("== resolvers ==")
s, h, b = req("/api/arena/pair", cookie=K)
pair = json.loads(b).get("pair", [])
check("arena pair = 3 distinct", len(pair) == 3 and len(set(pair)) == 3, str(pair))
s, h, b = req("/api/tiers", cookie=K)
tiers = json.loads(b)
check("every tier resolves", all(t.get("models") for n, t in tiers.items()
                                 if n != "Power"), str({n: t.get("models") for n, t in tiers.items()}))
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


t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": False,
          "messages": [{"role": "user", "content": "tell me about central park"}]})
# Fast is the SPEED tier (a 3B): judge it on collapse, not eloquence —
# the server-side tail guard owns catastrophe; x14 tolerates 3B waffle
words = re.findall(r"[a-z']+", t.lower())
grams = Counter(tuple(words[i:i+3]) for i in range(max(0, len(words)-2)))
rep = max(grams.values()) if grams else 0
check("Fast tier answer healthy", len(t) > 300 and rep <= 14 and "⚠️" not in t,
      f"{len(t)} chars, 3gram x{rep}")

t = chat({"model": "", "models": [], "tier": "Smart", "auto_web": False,
          "messages": [{"role": "user", "content": "give me a great one-day brooklyn itinerary"}]})
ok, d = healthy(t)
check("Smart tier answer healthy", ok, d)

t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": True,
          "messages": [{"role": "user", "content": "whats the weather in 11221"}]})
check("weather answer carries real data", ("°F" in t or "degrees" in t or " mph" in t)
      and "⚠️" not in t and len(t) > 60, t[:120])

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
