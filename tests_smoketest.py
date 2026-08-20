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
# 6.0b4: the wordmark went SMALL (gpt/gemini corner mark) — assert the
# compact form + the beta-updates opt-in
check("corner wordmark + version row", "font-size:12.5px" in page
      and 'class="vsub"' in page)
check("beta updates opt-in present", 'id="betaup"' in page
      and "beta_updates" in page)
# 6.0b7: engine dropdown at the chip, Hermes agent, 300px rail
check("engine dropdown js + meta", "openEngMenu" in page
      and '"Fast"' in page and "engrow" in page)
# 6b209: agents UI pulled until the logistics are sorted — two tabs
# only, no specialist list; the machinery stays dormant (AGENT_META
# still feeds the Code tab's popups, Hermes waits inside it)
check("agents tab pulled, machinery dormant",
      'data-m="agents"' not in page and 'id="agents-wrap"' not in page
      and "showAgentPop" in page and '"Hermes"' in page)
# b228: three tabs again (Chat | Code | Funnels) — thirds glide
check("three-tab glide in thirds", "width:calc(33.334% - 2px)" in page
      and "translateX(200%)" in page)
check("funnels tab present", 'data-m="funnel"' in page
      and 'id="fn-goal"' in page and 'id="fn-stages"' in page)
check("sidebar defaults to 300px", "width:300px;min-width:300px" in page)
# 6.0b206: rich answers — flow diagrams, code cards, highlighter
check("flow diagram renderer", "flowDiagram" in page and "wireFlow" in page
      and "fwires" in page)
check("code cards + mini highlighter", "codecard" in page
      and "hilite" in page and "hkw" in page)
# 6b244: every code card carries a copy button that is greyed (.wait,
# disabled) while its fence is still open and live once it closes
check("code-card copy button, greyed until the fence closes",
      'class="ccopy wait" disabled' in page and ".ccopy.wait" in page
      and "ccopy" in page and "Still generating" in page)
# 6b243: the burger was DEAD on phones — a 760px block set the sidebar
# display:none while the 700px drawer block only animated transform, so
# the ☰ toggled a class on an element that was never rendered. ONE
# breakpoint now; this guards the second one from creeping back.
check("mobile drawer present and openable",
      'id="mburger"' in page and "body.sbopen #sidebar" in page
      and page.count("max-width:760px") == 1
      and "max-width:700px" not in page
      and "#sidebar{display:none}" not in page)
# 6b242: ONE mode picker. The sidebar's copy of the tier list is gone —
# the composer's engine pill is the only place modes are chosen, so guard
# both halves: the picker is there, and the duplicate has not crept back.
check("composer engine picker is the only mode selector",
      "openEngMenu" in page and 'id="model-chip"' in page
      and 'id="tier-rows"' not in page and 'class="tier"' not in page)
# 6b242: voice chat parked. The button greys out, the click is inert, and
# a machine that had it ON must not keep talking after the update — so the
# stale localStorage flag has to be cleared at boot, not just ignored.
check("voice chat parked, and stale flag cleared",
      "VOICE_PARKED=true" in page and "parked" in page
      and 'localStorage.setItem("millen.voice","0")' in page)
check("arena removed", "arena" not in page.lower())
check("blend progress bar css", ".blendprog" in page)
check("serene entrance css", "heroIn 2.6s" in page and "shockOut" not in page)
# 5.2 surface (agents tab pulled again in 6b209 — two tabs is correct)
check("tab selector with Code lane", 'data-m="code"' in page
      and 'data-m="ai"' in page)
check("code tab carries Coding + Workspace",
      'data-agent="Coding"' in page and 'data-agent="Workspace"' in page)
check("query pinwheel css", ".wtspin" in page and "wtspin 1.5s" in page)
# 6b214: the LFG moment is fully retired — no element, no wash, no
# splash line, and the boot (cube wave + reveal) runs without it
check("LFG removed entirely",
      "lfg" not in page.lower() and "fucking" not in page.lower())
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
# 6b245: Kimi K3 is the 4th provider — dropdown option and board row.
# (The backend spec was proven live: a probe key reached api.moonshot.ai
# and came back with Moonshot's own "Invalid Authentication".)
check("Kimi K3 wired as a provider",
      'value="kimi">Kimi K3 (paid)' in page and '"kimi","Kimi K3"' in page)
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

# FLEET LOOPBACK (6b244): a real worker speaking the real protocol —
# register (auto-approve + token), long-poll, take the job, submit a
# sentinel — and the chat answer must BE that sentinel, delivered with
# the "GPU is on it" status. Proves dispatch end to end with zero
# engine loads. turbo is parked for the window (cloud outranks fleet
# in the single-model path) and restored no matter what.
import threading as _th

_FSENT = ("FLEET-GAUNTLET-7391: the pooled GPU answered this, and this "
          "sentence is long enough to clear the degenerate-output floor "
          "standing in for a real model's reply.")


# The hub hands a worker its token ONCE (register marks the claim
# "claimed"); a known wid arriving with no token is an imposter and
# parks in pending — correct security, but it made a fixed test wid
# work exactly once. Persist the (wid, token) PAIR across runs; if the
# cache is gone, a fresh random wid gets auto-approved and re-cached.
import os as _os
import secrets as _sec
import tempfile as _tf

_FCACHE = _os.path.join(_tf.gettempdir(), "millenai-gauntlet-fleet.json")


def _fleet_worker(stop):
    try:
        c = json.load(open(_FCACHE))
        wid, tok = c["wid"], c["token"]
    except Exception:
        wid, tok = "gauntlet" + _sec.token_hex(6), ""
    while not stop.is_set():
        try:
            s2, h2, b2 = req("/api/fleet/register", "POST",
                             {"id": wid, "token": tok, "name": "gauntlet-rig",
                              "models": [json.loads(
                                  req("/api/tiers", cookie=K)[2])
                                  ["Fast"]["models"][0]]}, cookie=K)
            out = json.loads(b2)
            if out.get("pending"):
                # claimed wid, lost token — start over as a new worker
                wid, tok = "gauntlet" + _sec.token_hex(6), ""
                continue
            if out.get("token"):
                tok = out["token"]
                json.dump({"wid": wid, "token": tok}, open(_FCACHE, "w"))
            if not tok:
                time.sleep(1)
                continue
            s2, h2, b2 = req("/api/fleet/poll", "POST",
                             {"id": wid, "token": tok}, cookie=K, timeout=40)
            job = json.loads(b2)
            if job.get("job"):
                req("/api/fleet/submit", "POST",
                    {"id": wid, "token": tok, "job": job["job"],
                     "text": _FSENT}, cookie=K)
                return
        except Exception:
            time.sleep(1)


_prefs0 = json.loads(req("/api/prefs", cookie=K)[2])
req("/api/prefs", "POST", {"turbo": False}, cookie=K)
_fstop = _th.Event()
_fth = _th.Thread(target=_fleet_worker, args=(_fstop,), daemon=True)
_fth.start()
time.sleep(2)
try:
    t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": False,
              "messages": [{"role": "user",
                            "content": "Say hello in one sentence."}]},
             timeout=60)
    check("fleet: worker's answer comes back through chat",
          "FLEET-GAUNTLET-7391" in t, t[:120])
finally:
    _fstop.set()
    req("/api/prefs", "POST", {"turbo": bool(_prefs0.get("turbo"))}, cookie=K)

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
