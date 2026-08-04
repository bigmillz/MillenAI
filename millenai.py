#!/usr/bin/env python3
"""
MillenAI — single-file local LLM cockpit.

Run:  python3 millenai.py
Optional extras:
  pip install ddgs                (enables live web search)
  pip install psutil              (enables real RAM telemetry)

Backends (any subset is fine — missing ones just report offline):
  MLX / llama.cpp OpenAI-compatible servers on:
    127.0.0.1:8888  -> Llama 3.2 3B
    127.0.0.1:8890  -> Gemma 2 9B IT
    127.0.0.1:8892  -> Mistral Nemo 12B
  Ollama on 127.0.0.1:11434 for the heavy models.

If mlx-lm (and/or ollama) is installed, the app spawns any engine whose
port is free at launch and stops those children on exit — engines that are
already running (launchd agents, the Ollama menubar app) are left alone.
First use of an MLX engine downloads its weights from Hugging Face.
"""

import atexit
import calendar
import glob
import json
import os
import platform
import plistlib
import random
import re
import shutil
import signal
import socket
import base64
import hashlib
import secrets
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
import http.server
import socketserver
import urllib.request
import urllib.error
import urllib.parse
import webbrowser

# xet-backed HF downloads materialise files only on completion, which blinds
# the on-disk progress meter (and anonymous xet gets rate-limited harder) —
# force the classic CDN path for us and every engine we spawn.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# ---------------------------------------------------------------- optional deps
try:
    from ddgs import DDGS           # current package
    HAS_SEARCH = True
except ImportError:
    try:
        # legacy name — still importable, but its API returns nothing now,
        # so treat it as unavailable rather than silently searching blanks
        from duckduckgo_search import DDGS  # noqa: F401
        HAS_SEARCH = False
    except ImportError:
        HAS_SEARCH = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import webview  # pywebview -> native macOS window (WKWebView)
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False

APP_VERSION = "2.17.4"   # bump here — UI, window, DMG all follow


def short_version(v: str = None) -> str:
    """Display form, macOS-style: '2.0.0'->'2.0', '2.0.1' stays."""
    v = v or APP_VERSION
    return v[:-2] if v.count(".") == 2 and v.endswith(".0") else v
APP_BUILD = 154               # integer compared against the GitHub release tag
APP_BUILD_DATE = ""         # ISO date; blank falls back to this file's mtime

# Set to "youruser/yourrepo" once this is on GitHub. Publish each build as a
# Release whose tag ends in the build number (e.g. "v5") with the .dmg
# attached; the app then offers a one-click in-place update.
UPDATE_REPO = "bigmillz/MillenAI"

# MILLENAI_PORT: the go-live LaunchAgent runs a second, headless instance
# beside the desktop app — it must not fight the app for 8889
PORT = int(os.environ.get("MILLENAI_PORT", "8889"))
# Opt-in remote-access gate. The backend has no auth of its own — it was
# built to listen on 127.0.0.1 for a window on the same machine. Before
# exposing it through a tunnel (Tailscale Funnel, cloudflared, ...), set
# MILLENAI_KEY: every request must then carry the key once (?key=... sets a
# cookie) or be refused. Unset = exactly the old behaviour.
ACCESS_KEY = os.environ.get("MILLENAI_KEY", "").strip()

# delimiter for out-of-band progress lines in the chat stream — the UI
# strips these so they never appear inside an answer
NUL = chr(0)

SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "You are a warm, authentic, adaptive, and insightful AI collaborator. "
        "Avoid sounding like a rigid textbook, robot, or bullet-point generator. "
        "Speak naturally in clear, engaging prose as if talking to a smart peer "
        "— contractions, natural rhythm, a little dry humour when it fits. "
        "Lead with the answer itself: never open by restating the question "
        "or with filler like 'Great question'. NEVER respond with only "
        "clarifying questions — when a request is broad, make a reasonable "
        "assumption, name it in one clause, and deliver the substance "
        "anyway. "
        "CALIBRATE depth to the ask, the way the best assistants do: a "
        "simple factual question gets a tight, confident answer with the "
        "number or name up front and one line of useful context — not an "
        "essay. A meaty question gets the full treatment: developed "
        "paragraphs, concrete names, numbers and lived-in detail, the "
        "interesting angle explored, the obvious follow-up anticipated. "
        "Padding a small answer is as bad as starving a big one. "
        "Specifics are the voice of competence: prefer 'around $4-5 a "
        "taco, $12-15 for a plate of three' over 'prices vary'. End when "
        "the answer is done — no summary paragraphs, no 'In conclusion', "
        "no offering to help further. Use a list only for truly "
        "enumerable things. When you don't know, say so plainly. When "
        "stakes are real (health, money, code), be rigorous.\n\n"
        "Example of the register for a quick ask —\n"
        "Q: whats a good price for tacos in nyc\n"
        "A: Around $4-6 per taco at a good taqueria — Los Tacos No.1 "
        "runs about $4.50, sit-down spots more like $6-7. A three-taco "
        "plate with rice and beans lands at $14-18. Past $8 a taco "
        "you're paying for the room, not the taco.\n\n"
        "Example of the register for a meaty ask — a question about "
        "planning a first marathon gets structure: what to do this week, "
        "the training arc, the race itself, each with real numbers."
    ),
}

# MLX needs Apple silicon; on Intel Macs the starter models run on Ollama
# (CPU) instead, so the same app works everywhere.
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
# MLX is Apple-silicon only; everywhere else inference goes through Ollama
# (which uses CUDA automatically on an NVIDIA box).
IS_ARM = IS_MAC and platform.machine() == "arm64"


def app_dir() -> str:
    """Per-user data directory (venv, memory, downloaded engines)."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "MillenAI")
    return os.path.expanduser("~/Library/Application Support/MillenAI")


def log_dir() -> str:
    return (os.path.join(app_dir(), "logs") if IS_WIN
            else os.path.expanduser("~/Library/Logs/MillenAI"))


def reveal(path: str):
    """Show a folder in Finder / Explorer."""
    subprocess.Popen(["explorer", path] if IS_WIN else ["open", path])


# ---------------------------------------------------------------- catalog
# One row per model. `mlx` is an Apple-silicon-only 4-bit build; `ollama`
# works on any Mac (Intel included). A model with no ollama tag simply
# isn't available on Intel and is shown greyed out.
#   port    — fixed local port for the MLX server (None = ollama only)
#   mem/gb  — resident RAM once loaded, and on-disk download size
#   star    — offered on the first-run setup screen
# All repos/tags below were checked against the HF and Ollama registries.
CATALOG = [
    # label,               icon, size, group, mlx repo, ollama tag, port, mem_gb, gb, star
    ("Llama 3.2 1B",       "🪶", "1B",  "core", "mlx-community/Llama-3.2-1B-Instruct-4bit",        "llama3.2:1b",       8884,  1.2,  0.8, True),
    ("Llama 3.2 3B",       "⚡️", "3B",  "core", "mlx-community/Llama-3.2-3B-Instruct-4bit",        "llama3.2:3b",       8888,  2.5,  1.8, True),
    ("Gemma 2 9B IT",      "💎", "9B",  "core", "mlx-community/gemma-2-9b-it-4bit",                "gemma2:9b",         8890,  6.2,  5.2, True),
    ("Mistral Nemo 12B",   "🌪️", "12B", "core", "mlx-community/Mistral-Nemo-Instruct-2407-4bit",   "mistral-nemo:12b",  8892,  7.8,  6.9, True),
    ("Gemma 2 2B",         "🌱", "2B",  "core", "mlx-community/gemma-2-2b-it-4bit",                "gemma2:2b",         8886,  2.0,  1.6, False),
    ("Llama 3.1 8B",       "🦙", "8B",  "core", "mlx-community/Meta-Llama-3.1-8B-Instruct-4bit",   "llama3.1:8b",       8894,  5.5,  4.5, False),
    # tuned for tool use and structured output — the Research agent's first pick
    ("Hermes 3 8B",        "🪽", "8B",  "core", "mlx-community/Hermes-3-Llama-3.1-8B-4bit",        "hermes3:8b",        8912,  5.5,  4.6, False),
    ("Qwen 2.5 7B",        "🧭", "7B",  "core", "mlx-community/Qwen2.5-7B-Instruct-4bit",          "qwen2.5:7b",        8896,  5.0,  4.3, False),
    ("Qwen 2.5 Coder 7B",  "💻", "7B",  "code", "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",    "qwen2.5-coder:7b",  8898,  5.0,  4.3, False),
    ("Qwen 2.5 Coder 14B", "🛠️", "14B", "code", "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit",   "qwen2.5-coder:14b", 8900,  9.5,  8.1, False),
    ("Gemma 4 12B",        "💠", "12B", "core", "mlx-community/gemma-4-12B-it-4bit",               "gemma4:12b",        8908,  8.2,  6.8, True),
    ("Gemma 4 26B",        "🔷", "26B", "core", "mlx-community/gemma-4-26b-a4b-it-4bit",           "gemma4:26b",        8910, 17.0, 15.4, False),
    ("Phi-4 14B",          "🔬", "14B", "core", "mlx-community/phi-4-4bit",                        "phi4:14b",          8902,  9.5,  8.2, False),
    ("DeepSeek R1 7B",     "🧠", "7B",  "core", "mlx-community/DeepSeek-R1-Distill-Qwen-7B-4bit",  "deepseek-r1:7b",    8904,  5.0,  4.3, False),
    ("Mistral Small 24B",  "🧊", "24B", "big",  "mlx-community/Mistral-Small-24B-Instruct-2501-4bit", "mistral-small:24b", 8906, 15.0, 13.0, False),
    ("LLaVA Vision 7B",    "👁️", "7B",  "code", None,                                              "llava:7b",          None,  5.0,  4.7, False),
    ("DeepSeek R1",        "☁️", "R1",  "core", None,                                              "deepseek-r1",       None,  5.5,  4.7, False),
    # ---- the 2026 ladder: every repo/tag verified against HF + the Ollama
    # registry on 2026-08-01. Strongest model per hardware class; anything
    # that can't fit the machine is filtered out of the UI entirely.
    ("GPT-OSS 20B",        "🌀", "20B",  "core", "mlx-community/gpt-oss-20b-MXFP4-Q4",             "gpt-oss:20b",       8914, 13.0, 12.0, False),
    ("Qwen 3.6 27B",       "🐉", "27B",  "big",  "mlx-community/Qwen3.6-27B-4bit",                 None,                8916, 16.5, 15.0, False),
    ("Qwen 3.6 35B MoE",   "🚀", "35B",  "big",  "mlx-community/Qwen3.6-35B-A3B-4bit",             "qwen3.6:35b",       8918, 20.0, 18.5, True),
    ("Llama 3.3 70B",      "🐋", "70B",  "big",  "mlx-community/Llama-3.3-70B-Instruct-4bit",      "llama3.3:70b",      8920, 42.0, 40.0, False),
    ("Llama 4 Scout",      "🦅", "109B", "big",  "mlx-community/Llama-4-Scout-17B-16E-Instruct-4bit","llama4:scout",    8922, 58.0, 55.0, False),
    ("GPT-OSS 120B",       "🌌", "120B", "big",  "mlx-community/gpt-oss-120b-MXFP4-Q4",            "gpt-oss:120b",      8924, 64.0, 61.0, False),
    ("Qwen 3 235B MoE",    "🐲", "235B", "big",  "mlx-community/Qwen3-235B-A22B-4bit",             "qwen3:235b",        8926, 125.0, 118.0, False),
    ("GLM-5.2",            "👑", "744B", "big",  "mlx-community/GLM-5.2-4bit",                     None,                8928, 375.0, 360.0, False),
    ("DeepSeek R1 671B",   "🌊", "671B", "big",  "mlx-community/DeepSeek-R1-0528-4bit",            None,                8930, 380.0, 360.0, False),
]

GROUP_TITLES = {"core": "General Models", "code": "Coding & Vision",
                "big": "Large Models"}

# ------------------------------------------------- hardware-class ladder
# The sidebar groups models by the MACHINE they need, not by family, and a
# model that cannot fit this machine is not shown at all — a 16 GB Air
# never sees a 70B, and only the 512 GB Studios ever see GLM-5.2.
HW_CLASSES = [   # (key, header, resident-GB ceiling for the class)
    ("everyday",    "Everyday · any machine",   10),
    ("performance", "Performance · 32 GB",      20),
    ("flagship",    "Flagship · 64–96 GB",      64),
    ("titan",       "Titan · 128 GB+",          1e9),
]


def hw_class(mem_gb: float) -> str:
    for key, _t, ceil in HW_CLASSES:
        if mem_gb <= ceil:
            return key
    return "titan"


_vram = {"b": None}


def gpu_vram_bytes():
    """Total VRAM on a discrete NVIDIA GPU, or 0. Cached — nvidia-smi is
    slow enough that per-model calls would be felt."""
    if _vram["b"] is not None:
        return _vram["b"]
    _vram["b"] = 0
    if not IS_MAC:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4).stdout
            mb = max(int(x) for x in out.split() if x.strip().isdigit())
            _vram["b"] = mb * 1024 * 1024
        except Exception:
            _vram["b"] = 0
    return _vram["b"]


def machine_budget_bytes():
    """What this machine can hold resident and still be FAST.

    Apple silicon shares one pool, so 75% of total memory is the real
    wired ceiling. A PC with a discrete GPU is different: what matters is
    VRAM, not the 165 GB of system RAM sitting behind it — sizing by RAM
    would offer a 120B to a 24 GB 3090, which technically runs and then
    crawls at a token a second with most layers spilled to CPU. Budget
    the card, plus a modest spill allowance, and cap by system RAM.
    None when psutil is missing — then nothing is hidden."""
    if not HAS_PSUTIL:
        return None
    ram = int(psutil.virtual_memory().total * 0.75)
    vram = gpu_vram_bytes()
    if vram:
        return int(min(ram, vram * 1.25))
    return ram


_no_limits = {"v": None}


def no_limits() -> bool:
    if _no_limits["v"] is None:
        try:
            _no_limits["v"] = bool(load_prefs(None).get("no_limits"))
        except Exception:
            _no_limits["v"] = False
    return bool(_no_limits["v"])


def model_fits_machine(label: str) -> bool:
    if no_limits():
        # Patrick's "disobey the limits" switch: every supported model is
        # offered. The runtime admission check still referees actual RAM.
        return SUPPORTED.get(label, False)
    budget = machine_budget_bytes()
    need = MODEL_MEM_BYTES.get(label)
    if budget is None or need is None:
        return True
    return need <= budget

MODEL_INFO = {c[0]: dict(icon=c[1], size=c[2], group=c[3], mlx=c[4],
                         ollama=c[5], port=c[6],
                         mem=int(c[7] * 1e9), gb=c[8], star=c[9])
              for c in CATALOG}

# a model is usable here if it has an engine this Mac can actually run
SUPPORTED = {l: bool((i["mlx"] and IS_ARM) or i["ollama"])
             for l, i in MODEL_INFO.items()}

# prefer MLX on Apple silicon (fast Metal), else Ollama
MODEL_ROUTES = {}
for _l, _i in MODEL_INFO.items():
    if _i["mlx"] and IS_ARM and _i["port"]:
        MODEL_ROUTES[_l] = ("mlx", _i["port"])
    elif _i["ollama"]:
        MODEL_ROUTES[_l] = ("ollama", _i["ollama"])

MLX_REPOS = {l: i["mlx"] for l, i in MODEL_INFO.items() if i["mlx"]}
MLX_EST_BYTES = {l: int(i["gb"] * 1e9) for l, i in MODEL_INFO.items()}
MODEL_MEM_BYTES = {l: i["mem"] for l, i in MODEL_INFO.items()}
OLLAMA_TAGS = {l: i["ollama"] for l, i in MODEL_INFO.items() if i["ollama"]}

# ------------------------------------------------------------------ turbo
# FREE CLOUD GPU, opt-in: Groq, Cloudflare Workers AI, OpenRouter and
# Together all speak the OpenAI chat-completions dialect and all have a
# free tier. Drop a JSON file at ~/…/MillenAI/cloud.json:
#   {"name":"Groq 70B","base":"https://api.groq.com/openai/v1",
#    "key":"YOUR_KEY","model":"llama-3.3-70b-versatile"}
# Nothing is sent anywhere until the Turbo switch in Settings is on.
CLOUD_FILE = os.path.join(app_dir(), "cloud.json")


def cloud_conf():
    try:
        with open(CLOUD_FILE) as f:
            c = json.load(f)
        if c.get("base") and c.get("key") and c.get("model"):
            return c
    except Exception:
        pass
    return None


def _anthropic_stream(c: dict, messages: list, emit) -> bool:
    """Anthropic speaks its own dialect: x-api-key, a version header, a
    hoisted system prompt, and content_block_delta events."""
    sys_txt = "\n\n".join(m["content"] for m in messages
                           if m.get("role") == "system")
    turns = [{"role": m["role"], "content": m["content"]}
             for m in messages if m.get("role") in ("user", "assistant")]
    body = {"model": c["model"], "max_tokens": 4096, "stream": True,
            "messages": turns}
    if sys_txt:
        body["system"] = sys_txt
    req = urllib.request.Request(
        c["base"].rstrip("/") + "/messages", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "x-api-key": c["key"],
                 "anthropic-version": "2023-06-01",
                 "User-Agent": "MillenAI/%s" % APP_VERSION})
    got = False
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    d = json.loads(line[5:].strip())
                except Exception:
                    continue
                if d.get("type") == "content_block_delta":
                    tok = (d.get("delta") or {}).get("text") or ""
                    if tok:
                        got = True
                        emit(tok)
    except Exception:
        return got
    return got


def cloud_stream(messages: list, emit) -> bool:
    """Stream from the configured cloud endpoint. False = fall back local."""
    c = cloud_conf()
    if not c:
        return False
    if "anthropic.com" in c.get("base", ""):
        return _anthropic_stream(c, messages, emit)
    payload = json.dumps({"model": c["model"], "messages": messages,
                          "max_tokens": 4096, "temperature": 0.75,
                          "stream": True}).encode()
    req = urllib.request.Request(
        c["base"].rstrip("/") + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream",
                 # a bare Python-urllib UA gets 403'd by provider edges
                 # (Cloudflare "error code: 1010" from Groq — seen live)
                 "User-Agent": "MillenAI/%s" % APP_VERSION,
                 "Authorization": "Bearer " + c["key"]})
    got = False
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if blob == "[DONE]":
                    break
                try:
                    d = json.loads(blob)
                    tok = (d.get("choices") or [{}])[0].get(
                        "delta", {}).get("content") or ""
                except Exception:
                    tok = ""
                if tok:
                    got = True
                    emit(tok)
    except Exception:
        return got
    return got


# ------------------------------------------------------------------ fleet
# CONTRIBUTE, per Patrick: friends flip a switch and their idle GPUs
# answer the hub's queries. Workers connect OUTBOUND (long-poll HTTP, so
# no router config, and every request fits inside Cloudflare's window);
# the router only offloads single-model jobs, and ANY failure falls back
# to running locally — the fleet can only ever make things faster.
# Trust model: workers see the prompts they serve. Friends only.
FLEET_KEY_FILE = os.path.join(app_dir(), "fleet_key")


def fleet_key() -> str:
    try:
        return open(FLEET_KEY_FILE).read().strip()
    except OSError:
        k = secrets.token_urlsafe(18)
        try:
            with open(FLEET_KEY_FILE, "w") as f:
                f.write(k)
            os.chmod(FLEET_KEY_FILE, 0o600)
        except OSError:
            pass
        return k


FLEET_HOME = "https://ai.millertechnology.net"   # one-click default hub
FLEET_APPROVED_FILE = os.path.join(app_dir(), "fleet_workers.json")


def _fleet_approved() -> dict:
    try:
        with open(FLEET_APPROVED_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _fleet_save_approved(d: dict):
    try:
        with open(FLEET_APPROVED_FILE, "w") as f:
            json.dump(d, f)
        os.chmod(FLEET_APPROVED_FILE, 0o600)
    except OSError:
        pass


_fleet_lock = threading.Lock()
_fleet_pending = {}   # wid -> {name, models, ts} awaiting owner approval
_fleet_workers = {}   # wid -> {name, models, last_seen, busy}
_fleet_jobs = {}      # jid -> {label, messages, done(Event), text, err, wid}
_fleet_queue = []     # jids waiting for a worker


def _fleet_alive() -> dict:
    now = time.time()
    with _fleet_lock:
        return {w: dict(v) for w, v in _fleet_workers.items()
                if now - v["last_seen"] < 45}


def fleet_pick(label: str):
    """An idle live worker that has the model, or None."""
    for wid, v in _fleet_alive().items():
        if label in v.get("models", []) and not v.get("busy"):
            return wid
    return None


def fleet_run(label: str, messages: list, status) -> str:
    """Offload one generation; empty string means 'do it locally'."""
    wid = fleet_pick(label)
    if not wid:
        return ""
    jid = secrets.token_hex(8)
    done = threading.Event()
    with _fleet_lock:
        name = _fleet_workers.get(wid, {}).get("name", "a friend")
        _fleet_jobs[jid] = {"label": label, "messages": messages,
                            "done": done, "text": "", "err": "",
                            "wid": wid}
        _fleet_workers[wid]["busy"] = True
        _fleet_queue.append(jid)
    status(f"{name}'s GPU is on it — {label}")
    ok = done.wait(150)          # heartbeat keeps the client stream alive
    with _fleet_lock:
        job = _fleet_jobs.pop(jid, {})
        try:
            _fleet_queue.remove(jid)
        except ValueError:
            pass
        if wid in _fleet_workers:
            _fleet_workers[wid]["busy"] = False
    text = (job.get("text") or "") if ok else ""
    if text and not _looks_degenerate(text):
        return text
    return ""


_contrib_stop = threading.Event()
_contrib_state = ["off"]
_contrib_thread = None


def _contrib_loop(url: str, key: str):
    """Friend mode, ONE CLICK: knock on the hub, wait to be approved,
    then long-poll for jobs and run them here. Outbound-only."""
    p = load_prefs(None)
    wid = str(p.get("contrib_wid") or secrets.token_hex(8))
    token = str(p.get("contrib_token") or "")
    if p.get("contrib_wid") != wid:
        p["contrib_wid"] = wid
        store_prefs(p)

    def post(path, data):
        req = urllib.request.Request(
            url.rstrip("/") + path, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json",
                     "X-Fleet-Key": key})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())

    while not _contrib_stop.is_set():
        try:
            pulled = ollama_pulled_tags() or set()
            models = [l for l in MODEL_INFO
                      if model_cached(l, pulled) and model_fits_memory(l)]
            out = post("/api/fleet/register",
                       {"id": wid, "token": token,
                        "name": platform.node().split(".")[0][:20],
                        "models": models})
            if out.get("pending"):
                _contrib_state[0] = "waiting for approval"
                _contrib_stop.wait(20)
                continue
            if out.get("err"):
                _contrib_state[0] = "not approved"
                _contrib_stop.wait(30)
                continue
            if out.get("token") and out["token"] != token:
                token = out["token"]
                p2 = load_prefs(None)
                p2["contrib_token"] = token
                store_prefs(p2)
            _contrib_state[0] = "contributing"
            job = post("/api/fleet/poll", {"id": wid, "token": token})
            if job.get("job"):
                parts = []
                try:
                    run_model(job["label"], job["messages"], parts.append)
                    post("/api/fleet/submit",
                         {"id": wid, "token": token, "job": job["job"],
                          "text": strip_think("".join(parts))})
                except Exception as exc:
                    post("/api/fleet/submit",
                         {"id": wid, "token": token, "job": job["job"],
                          "err": str(exc)[:100]})
        except Exception:
            _contrib_state[0] = "reconnecting"
            _contrib_stop.wait(8)


def contrib_apply(p=None):
    """Match the contribute thread to prefs. The old loop is retired
    FIRST — a running thread's url/key are baked in at start, so a
    settings change must always mean a fresh thread (seen live: an
    empty-key loop kept retrying forever after the key was fixed)."""
    global _contrib_thread
    p = p or load_prefs(None)
    on = bool(p.get("contrib_on"))
    _contrib_stop.set()
    if _contrib_thread is not None and _contrib_thread.is_alive():
        _contrib_thread.join(timeout=3)
    if on:
        _contrib_stop.clear()
        _contrib_thread = threading.Thread(
            target=_contrib_loop,
            args=(str(p.get("contrib_url") or FLEET_HOME),
                  str(p.get("contrib_key") or "")),
            daemon=True)
        _contrib_thread.start()


# ------------------------------------------------------------------ tiers
# Three plain-English modes instead of a wall of model names. Each lists
# candidates strongest-first; whatever is downloaded and fits RAM is used,
# and Gemma blends the answers when a tier has more than one.
TIERS = {
    # THE DEFAULT: Fast and Smart merged, per Patrick \u2014 one answer
    # from the strongest brain this machine holds. A single engine keeps
    # it the quick path; the ladder still gives every machine its best.
    "Fast": {
        "icon": "\u26a1\ufe0f", "desc": "the strongest model that fits",
        "picks": ["Qwen 3 235B MoE", "GPT-OSS 120B", "Llama 4 Scout",
                  "Llama 3.3 70B", "Qwen 3.6 35B MoE", "Qwen 3.6 27B",
                  "GPT-OSS 20B", "Gemma 4 26B", "Gemma 4 12B",
                  "Phi-4 14B", "Mistral Nemo 12B", "Llama 3.1 8B",
                  "Llama 3.2 3B", "Gemma 2 2B", "Llama 3.2 1B"],
        "count": 1,
    },
    "Thinking": {
        "icon": "\U0001f9e0", "desc": "reasons it through, blended",
        # strongest-first ladder: whatever the machine holds and the user
        # has installed autoselects \u2014 a Titan rig leads with the 235B, a
        # 16 GB laptop lands on Phi-4, nobody configures anything
        "picks": ["Qwen 3 235B MoE", "GPT-OSS 120B", "Llama 4 Scout",
                  "Llama 3.3 70B", "Qwen 3.6 35B MoE", "GPT-OSS 20B",
                  "Gemma 4 26B", "Phi-4 14B", "DeepSeek R1 7B",
                  "Qwen 2.5 Coder 14B", "Gemma 4 12B"],
        "count": 3,
    },
    "Pro": {
        "icon": "\u2728", "desc": "several models, blended",
        "picks": ["Qwen 3.6 35B MoE", "Qwen 3.6 27B", "GPT-OSS 20B",
                  "Gemma 4 12B", "Mistral Nemo 12B", "Gemma 2 9B IT",
                  "Qwen 2.5 7B", "Llama 3.1 8B"],
        "count": 5,
    },
    "Power": {
        "icon": "\u269b\ufe0f", "name": "Power Mode",
        "desc": "every model that fits, blended",
        "picks": [],          # purely memory-driven
        "count": 99,
        # no quality filtering — if it can run, it takes part
        "all": True,
    },
}

# ------------------------------------------------------------- agents
# Task specialists, named for what they're GOOD AT. An agent is a strong
# system prompt married to the best installed model for that craft —
# radio-selected in the sidebar against "Standard model".
AGENTS = {
    "Coding": {
        "icon": "💻", "desc": "working code, tight explanations",
        "picks": ["Qwen 2.5 Coder 14B", "Qwen 2.5 Coder 7B",
                  "Qwen 3.6 35B MoE", "GPT-OSS 20B", "Gemma 4 12B",
                  "Llama 3.1 8B"],
        "system": (
            "You are a senior software engineer. Give WORKING code first, "
            "in fenced blocks with the language tag, then a tight "
            "explanation of the non-obvious parts only. Prefer complete, "
            "runnable examples over fragments. State assumptions, name "
            "edge cases, and when something is a bad idea say so and give "
            "the better way. No filler, no apologies."),
    },
    "Resumes": {
        "icon": "📄", "desc": "bullets that get interviews",
        "picks": ["Hermes 3 8B", "Qwen 3.6 35B MoE", "Gemma 4 12B",
                  "Mistral Nemo 12B", "Llama 3.1 8B"],
        "system": (
            "You are an expert resume writer and hiring manager. Turn "
            "experience into crisp, quantified bullet points: strong verb "
            "first, concrete impact with numbers, no fluff words "
            "('responsible for', 'various'). Keep ATS-friendly plain "
            "formatting, tailor language to the target role when given, "
            "and be honest — never invent accomplishments. Offer a "
            "sharper alternative whenever a bullet is weak."),
    },
    "Writing": {
        "icon": "✍️", "desc": "emails, essays, anything with a reader",
        "picks": ["Qwen 3.6 35B MoE", "Gemma 4 26B", "Gemma 4 12B",
                  "Mistral Nemo 12B", "Hermes 3 8B"],
        "system": (
            "You are a sharp professional writer and editor. Match the "
            "asked-for tone exactly, lead with the point, cut every "
            "word that earns nothing, and vary sentence rhythm so it "
            "reads human. When editing, preserve the writer's voice and "
            "explain only the changes that teach something. For emails: "
            "subject line first, then the shortest body that gets the "
            "yes."),
    },
    "Mnemosyne": {
        "icon": "\U0001f9e0", "desc": "total recall — and how to remember",
        "picks": ["Qwen 3.6 35B MoE", "Gemma 4 26B", "Hermes 3 8B",
                  "Mistral Nemo 12B", "Llama 3.1 8B"],
        "system": (
            "You are Mnemosyne, the memory specialist. Two jobs.\n"
            "1) RECALL: when asked what you know or remember about the "
            "user, their preferences, or past topics, answer ONLY from "
            "the remembered-facts list provided in this conversation's "
            "system context. Quote it faithfully, organize it clearly, "
            "and when it holds nothing relevant say exactly that — "
            "never invent a memory, never guess at one. Uncertain "
            "recall is stated as uncertain.\n"
            "2) TEACH MEMORY: you are an expert in remembering things — "
            "mnemonics, memory palaces, spaced repetition, name-recall "
            "tricks, study schedules. Build concrete, personalized "
            "devices: real pegs, vivid images, an actual review "
            "calendar with dates. When someone needs to memorize "
            "something, give them the device, then a 30-second drill "
            "to prove it stuck."),
    },
    "Math & Logic": {
        "icon": "🧮", "desc": "careful step-by-step reasoning",
        "picks": ["Phi-4 14B", "DeepSeek R1 7B", "Gemma 4 26B",
                  "Qwen 3.6 35B MoE", "Gemma 4 12B"],
        "system": (
            "You are a meticulous mathematician. Work step by step, "
            "define variables before using them, and CHECK the result "
            "(substitute back, sanity-check magnitudes) before answering. "
            "If a problem is ambiguous, state the interpretation you "
            "chose. Show the reasoning compactly, then box the final "
            "answer on its own line."),
    },
    "Research": {
        "icon": "🔎", "desc": "searches the web, writes a cited brief",
        "picks": ["Hermes 3 8B", "Qwen 3.6 35B MoE", "Gemma 4 12B",
                  "Mistral Nemo 12B", "Llama 3.1 8B"],
        "research": True,
        "system": "",
    },
}


def resolve_agent(name):
    """(model_label, agent_dict) — best installed pick, or (None, None)."""
    a = AGENTS.get(name)
    if not a:
        return None, None
    pulled = ollama_pulled_tags() or set()
    for l in a["picks"]:
        if l in MODEL_ROUTES and model_cached(l, pulled) \
                and model_fits_memory(l):
            return l, a
    return None, a


def build_agent_rows() -> str:
    out = ['  <div class="agent" data-agent="">'
           '<span class="radio"></span><span class="ico">🤖</span>'
           'Standard model</div>']
    for name, a in AGENTS.items():
        out.append(
            f'  <div class="agent" data-agent="{name}" title="{a["desc"]}">'
            f'<span class="radio"></span><span class="ico">{a["icon"]}</span>'
            f'{name}</div>')
    return "\n".join(out)


# Auto-blending skips these: a vision model answers text poorly, and 1B-class
# models degrade into repetition (observed looping "address address address").
BLEND_EXCLUDE = {"LLaVA Vision 7B"}
BLEND_MIN_MEM = 2.4e9

THINK_HINT = ("Work through this carefully and step by step before giving "
              "your final answer.")


def resolve_tier(name: str) -> list:
    """Concrete model list for a tier, given what's actually usable now.

    The tier's own picks come first, then any other installed model that
    fits in RAM is blended in (strongest first) up to the tier's cap — so
    downloading more models makes Pro and Thinking richer automatically.
    """
    t = TIERS.get(name)
    if not t:
        return []
    pulled = ollama_pulled_tags() or set()

    def usable(l):
        return (l in MODEL_ROUTES and model_cached(l, pulled)
                and model_fits_memory(l))

    take_all = t.get("all")

    def blendable(l):
        if take_all:            # Power: memory is the only limit
            return usable(l)
        return (usable(l) and l not in BLEND_EXCLUDE
                and MODEL_MEM_BYTES.get(l, 0) >= BLEND_MIN_MEM)

    ready = [l for l in t["picks"] if usable(l)]
    if t["count"] > 1:
        # Only blend in models that leave room for the others. A 70B needs
        # most of the machine, so pairing it with four more would thrash
        # (and take many minutes) even if it fits on its own right now.
        total = psutil.virtual_memory().total if HAS_PSUTIL else 0
        # the 45% cap keeps one huge model from crowding a blend; Power
        # deliberately ignores it and relies on the real memory check
        budget = float("inf") if take_all else (
            total * 0.45 if total else float("inf"))
        ready += [l for l in MERGE_RANK
                  if blendable(l) and l not in ready
                  and MODEL_MEM_BYTES.get(l, 0) <= budget]
    if not ready:  # nothing at all from the tier — fall back to anything
        ready = [l for l in MERGE_RANK if usable(l)]
    return ready[:t["count"]]


# First run downloads the AUTOSELECTED set: for each tier, the single best
# pick this machine can hold — the strongest brain per job, nothing more.
# A 48 GB Mac gets the 35B MoE; a 16 GB Air lands on Phi-4/Gemma; nobody
# is asked for 100 GB of also-rans (that was possible when this listed
# every tier pick).
def _starter_labels() -> list:
    """The MAX spread: since the tier merge every tier leads with the same
    ladder, "best per tier" collapsed to ONE model (seen live: a fresh
    machine would have installed only the 35B — no merger, no quick
    path). Build the spread by ROLE instead: flagship, Gemma merger,
    everyday mid, the quick pair, vision."""
    fits = [l for l in MODEL_INFO
            if SUPPORTED.get(l) and model_fits_machine(l)]
    picks = []

    def add(label):
        if label and label in fits and label not in picks:
            picks.append(label)

    by_size = sorted(fits, key=lambda l: -MODEL_INFO[l]["gb"])
    if no_limits() and HAS_PSUTIL:
        # unlocked, not unhinged: the flagship stays within what RAM can
        # plausibly page (~1.6x memory = a 70B on 48GB, never the 235B)
        cap = psutil.virtual_memory().total
        sized = [l for l in by_size
                 if MODEL_MEM_BYTES.get(l, 0) <= cap]
        by_size = sized or by_size
    add(next((l for l in by_size), None))                      # flagship
    add(next((l for l in by_size if l.startswith("Gemma 4")), None))
    add(next((l for l in by_size if MODEL_INFO[l]["gb"] <= 8.5
              and "Vision" not in l), None))                   # everyday
    add("Llama 3.2 3B")
    add("Llama 3.2 1B")
    add("LLaVA Vision 7B")
    return picks


STARTER_LABELS = _starter_labels()


def plan_labels(plan: str) -> list:
    """First-run size choice, hardware-aware. The user never sees model
    names — just Basic / Pro / Max."""
    fits = [l for l in MODEL_INFO
            if SUPPORTED.get(l) and model_fits_machine(l)]
    if plan == "basic":
        # the smallest capable brain: ~1 GB, instant town
        small = sorted(fits, key=lambda l: MODEL_INFO[l]["gb"])
        return small[:1]
    if plan == "pro":
        # one strong everyday model plus the quick pair — ~10 GB
        mids = sorted((l for l in fits if MODEL_INFO[l]["gb"] <= 8.5),
                      key=lambda l: -MODEL_INFO[l]["gb"])
        picks = mids[:1]
        for extra in ("Llama 3.2 3B", "Llama 3.2 1B"):
            if extra in fits and extra not in picks:
                picks.append(extra)
        return picks
    return _starter_labels()

# who merges in combine mode — strongest first
MERGE_RANK = sorted((l for l in MODEL_ROUTES),
                    key=lambda l: -MODEL_INFO[l]["mem"])


def budget_label() -> str:
    """Human note about what the ladder was sized against."""
    v = gpu_vram_bytes()
    return ("%d GB VRAM" % round(v / 1e9)) if v else ""


def chip_name() -> str:
    """Short marketing name of the CPU: 'M4 PRO', 'CORE I7', etc."""
    if IS_WIN:
        try:
            gpu = subprocess.run(
                ["nvidia-smi", "--query-gpu=name",
                 "--format=csv,noheader"], capture_output=True, text=True,
                timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip().splitlines()
            if gpu:      # "NVIDIA GeForce RTX 4090" -> "RTX 4090"
                name = gpu[0].replace("NVIDIA", "").replace("GeForce", "")
                return " ".join(name.split()).upper()[:18]
        except Exception:
            pass
        return (platform.processor() or "PC").split()[0].upper()[:18]
    try:
        brand = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                               capture_output=True, text=True,
                               timeout=2).stdout.strip()
    except Exception:
        brand = ""
    if not brand:
        return "HARDWARE"
    if brand.startswith("Apple "):          # "Apple M4 Pro" -> "M4 PRO"
        return brand[6:].upper()
    m = re.search(r"(Core\(TM\)|Core)\s+(i\d)", brand)
    if m:                                    # long Intel string -> "CORE I7"
        return f"CORE {m.group(2)}".upper()
    return brand.split("@")[0].strip()[:18].upper()


def build_tier_rows() -> str:
    out = []
    for name, t in TIERS.items():
        out.append(
            f'  <div class="tier" data-tier="{name}">'
            f'<span class="ico">{t["icon"]}</span>'
            f'<span class="tname">{t.get("name", name)}</span>'
            f'<span class="infobtn" title="Which models does this use?">i</span>'
            f'</div>')
    return "\n".join(out)


def build_model_rows() -> str:
    """Sidebar rows grouped by HARDWARE CLASS, strongest last-to-first
    within a class. Models that cannot fit this machine's memory are not
    rendered at all — every visitor sees only their own ladder, with the
    best option of each class present. Models the platform can't run
    (MLX-only on Intel/Windows) stay visible but greyed."""
    out = []
    for key, title, _ceil in HW_CLASSES:
        members = [(l, i) for l, i in MODEL_INFO.items()
                   if hw_class(i["mem"] / 1e9) == key
                   and model_fits_machine(l)]
        if not members:
            continue
        members.sort(key=lambda p: -p[1]["mem"])   # strongest first
        out.append(f'  <div class="group-label mlx">{title}</div>')
        for label, info in members:
            ok = SUPPORTED[label]
            out.append(
                f'  <div class="model{"" if ok else " unsupported"}"'
                f' data-model="{label}">'
                f'<span class="ico">{info["icon"]}</span>{label}'
                + ("" if ok else
                   '<span class="memtag">APPLE SILICON ONLY</span>')
                + f'<span class="size">{info["size"]}</span></div>')
    return "\n".join(out)


def _mem_available():
    """Bytes of comfortably-usable RAM right now, or None if unknown."""
    if not HAS_PSUTIL:
        return None
    return psutil.virtual_memory().available


def model_fits_memory(label: str) -> bool:
    if no_limits():
        # "disobey the limits": admission stands down entirely — a 70B on
        # a 48GB Mac swaps hard, and that is the explicit ask
        return True
    # `available` on macOS omits reclaimable file cache — right after an
    # 18.5 GB model download the cache ATE the headroom and the freshly
    # installed flagship was refused admission (seen live). What the OS
    # will actually hand a wiring allocation is closer to total - used.
    avail = _mem_available()
    if HAS_PSUTIL:
        vm = psutil.virtual_memory()
        avail = max(avail or 0, vm.total - vm.used)
    need = MODEL_MEM_BYTES.get(label)
    if avail is None or need is None:
        return True  # unknown — don't cry wolf
    kind, target = MODEL_ROUTES.get(label, (None, None))
    if kind == "mlx" and _port_in_use(target):
        return True  # already resident and serving
    # Real footprints run above the estimate — a "44 GB" 70B was measured at
    # 49.7 GB and got OOM-killed — so demand real headroom, and never allow a
    # model that needs most of the machine even when RAM looks free.
    total = psutil.virtual_memory().total if HAS_PSUTIL else 0
    if total and need > total * 0.8:
        return False
    # 1.5x, not 1.25x: the KV cache and activations grow DURING generation,
    # and a 26B admitted at 1.25x OOM'd 97s into its answer on a busy
    # machine. Admission must survive the whole reply, not just the load.
    # MoE models get 1.3x — only a few billion parameters activate per
    # token, so their runtime overhead is a fraction of a dense model's.
    factor = 1.3 if "MoE" in label else 1.5
    return need * factor < avail

def weather_snippets(q: str):
    """Real numbers for weather questions. Generic web snippets for
    'weather in 11221' returned Moscow forecasts and kids' videos (seen
    live) — honest models reported garbage, confident ones invented a
    forecast. wttr.in resolves a zip or place name to actual conditions,
    no API key. None on any failure — the caller falls back to search."""
    m = re.search(r"\b(\d{5})\b", q)
    # a bare 5-digit zip is ambiguous worldwide — 11221 alone resolved to
    # Vilnius, Lithuania; ',us' pins it to Brooklyn
    loc = (m.group(1) + ",us") if m else re.sub(
        r".*?\b(?:weather|forecast|temperature)\b\s*(?:in|for|at|like in|like)?\s*",
        "", q, flags=re.I).strip(" ?.!") or ""
    if not loc or len(loc) > 60:
        return None
    try:
        with urllib.request.urlopen(
                "https://wttr.in/%s?format=j1" % urllib.parse.quote(loc),
                timeout=8) as r:
            d = json.load(r)
        cur = d["current_condition"][0]
        area = d["nearest_area"][0]
        name = "%s, %s" % (area["areaName"][0]["value"],
                           area["region"][0]["value"])
        out = ["LIVE WEATHER for %s (source: wttr.in, real data):" % name,
               "Right now: %s°F (feels like %s°F), %s, wind %s mph, "
               "humidity %s%%" % (
                   cur["temp_F"], cur["FeelsLikeF"],
                   cur["weatherDesc"][0]["value"],
                   cur["windspeedMiles"], cur["humidity"])]
        for day in d.get("weather", [])[:3]:
            out.append("%s: high %s°F / low %s°F, %s" % (
                day["date"], day["maxtempF"], day["mintempF"],
                day["hourly"][4]["weatherDesc"][0]["value"]))
        return "\n".join(out)
    except Exception:
        return None


_search_cache = {"query": "", "data": "", "timestamp": 0.0}
_search_lock = threading.Lock()

# Auto-search: local models have a training cutoff and no clock, so anything
# asking about *now* gets live snippets folded in before the model answers.
_FRESH_WORDS = (
    "today", "tonight", "right now", "currently", "current", "latest",
    "recent", "recently", "this week", "this month", "this year",
    "yesterday", "tomorrow", "so far", "up to date", "as of",
    "news", "headline", "weather", "forecast", "temperature",
    "price", "stock", "market", "score", "standings", "election",
    "release date", "released", "just announced", "who won", "what happened",
    "trending", "live", "update", "version",
)
_FRESH_PATTERNS = (
    re.compile(r"\b20[2-9]\d\b"),                 # a specific modern year
    re.compile(r"\bwho\s+is\s+the\s+(current|new)\b"),
    re.compile(r"\bhow\s+much\s+(is|does|are)\b"),
    re.compile(r"\bwhat('?s| is)\s+(the\s+)?(latest|newest|current)\b"),
    re.compile(r"\bis\s+there\s+(a|an)\s+new\b"),
    re.compile(r"\b(out|available|released)\s+yet\b"),
)
# never search these — they're about the conversation, not the world
_NO_SEARCH = re.compile(
    r"^\s*(hi|hey|hello|thanks|thank you|ok|okay|cool|nice|sure|yes|no|"
    r"continue|go on|again|more|summarize|rewrite|translate|explain that|"
    r"write|draft|code|refactor|debug|fix)\b", re.I)


def needs_search(prompt: str) -> bool:
    """Heuristic: does answering this require information from after the
    model's training cutoff? Cheap and deliberately conservative."""
    if not HAS_SEARCH:
        return False
    p = prompt.strip()
    if len(p) < 8 or _NO_SEARCH.match(p):
        return False
    low = p.lower()
    if any(w in low for w in _FRESH_WORDS):
        return True
    return any(rx.search(low) for rx in _FRESH_PATTERNS)

# ------------------------------------------------------- managed engines
# The app can run its own model servers, so a fresh machine needs nothing
# but a double-click. Anything already listening (e.g. launchd agents or a
# separately-run Ollama) is left alone.
_managed_procs = []
_mlx_procs = {}  # label -> Popen, so idle engines can be freed individually
_engine_lock = threading.Lock()


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _has_mlx() -> bool:
    try:
        import mlx_lm  # noqa: F401
        return True
    except ImportError:
        return False


# the app can fetch its own Ollama engine (signed universal CLI) so a fresh
# machine — Intel or Apple silicon — needs zero manual installs
OLLAMA_TGZ_URL = "https://ollama.com/download/ollama-darwin.tgz"
# Windows portable build — bundles the CUDA runtime, so an NVIDIA GPU is
# used automatically with no extra setup
# Windows ships two builds: amd64 bundles the CUDA runtime, arm64 is
# CPU-only (Windows-on-ARM has no NVIDIA support).
#
# On Windows-on-ARM the app itself normally runs as *emulated x64*, because
# pythonnet (pywebview's backend) and ctranslate2 (faster-whisper) publish
# win_amd64 wheels only. So `platform.machine()` reports the architecture of
# this process, not of the machine, and would send an ARM laptop after the
# 1.5 GB CUDA build it can never use. Ollama is a separate process talked to
# over HTTP, so it should always be the *native* build — emulated UI, native
# inference.
def _win_native_machine() -> str:
    """Hardware architecture, seeing through x64/x86 emulation."""
    try:
        import ctypes
        proc, native = ctypes.c_ushort(), ctypes.c_ushort()
        if ctypes.windll.kernel32.IsWow64Process2(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(proc), ctypes.byref(native)):
            return {0xAA64: "arm64", 0x8664: "amd64",
                    0x14C: "x86"}.get(native.value, "amd64")
    except Exception:
        pass  # pre-1709 Windows, or a non-Windows import — fall through
    return (os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE")
            or platform.machine()).lower().replace("aarch64", "arm64")


IS_WIN_ARM = IS_WIN and _win_native_machine() == "arm64"
# true when an ARM box is running us through x64 emulation
IS_WIN_EMULATED = IS_WIN_ARM and platform.machine().lower() not in (
    "arm64", "aarch64")
OLLAMA_ZIP_URL = ("https://github.com/ollama/ollama/releases/latest/download/"
                  + ("ollama-windows-arm64.zip" if IS_WIN_ARM
                     else "ollama-windows-amd64.zip"))
_MANAGED_BIN_DIR = os.path.join(app_dir(), "bin")
_MANAGED_BIN_DIR_FOUND = []   # nested location inside the win zip


def _ollama_bin():
    exe = "ollama.exe" if IS_WIN else "ollama"
    cands = [shutil.which("ollama"), os.path.join(_MANAGED_BIN_DIR, exe)]
    if IS_WIN:
        cands.append(os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", exe))
    else:
        cands.append("/usr/local/bin/ollama")
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def ollama_pulled_tags():
    """Set of pulled model names (with and without :tag), or None if down."""
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:11434/api/tags", timeout=1.5
        ) as r:
            tags = json.loads(r.read().decode("utf-8")).get("models", [])
            return ({m.get("name", "") for m in tags} |
                    {m.get("name", "").split(":")[0] for m in tags})
    except Exception:
        return None


def model_cached(label, pulled=None):
    # NB: exact tag match only — ollama refuses "llama3.2:3b" even when
    # ":latest" is the same digest. Bare requested tags (e.g. "command-r")
    # still match because the pulled set includes bare names for :latest.
    kind, target = MODEL_ROUTES[label]
    if kind == "mlx":
        return mlx_model_cached(MLX_REPOS[label])
    if pulled is None:
        pulled = ollama_pulled_tags() or set()
    return target in pulled


def _hf_model_dir(repo: str) -> str:
    base = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    return os.path.join(base, "hub", "models--" + repo.replace("/", "--"))


def mlx_model_cached(repo: str) -> bool:
    """True only when the weights are fully downloaded.

    hub-cache layout: snapshot symlinks appear per-file as each blob
    completes (config.json lands early!), and unfinished blobs sit in
    blobs/*.incomplete — so require the safetensors, every sharded part
    named by the index, and zero incomplete blobs.
    """
    d = _hf_model_dir(repo)
    snaps = glob.glob(os.path.join(d, "snapshots", "*", "config.json"))
    if not snaps:
        return False
    snap_dir = os.path.dirname(snaps[0])
    if not glob.glob(os.path.join(snap_dir, "*.safetensors")):
        return False
    if glob.glob(os.path.join(d, "blobs", "*.incomplete")):
        return False
    idx = os.path.join(snap_dir, "model.safetensors.index.json")
    if os.path.exists(idx):
        try:
            with open(idx, "r", encoding="utf-8") as f:
                parts = set(json.load(f)["weight_map"].values())
            if not all(os.path.exists(os.path.join(snap_dir, p))
                       for p in parts):
                return False
        except Exception:
            pass
    return True


def _dir_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _spawn_mlx_engine(label: str) -> bool:
    kind, port = MODEL_ROUTES[label]
    if kind != "mlx" or _port_in_use(port) or not _has_mlx():
        return False
    logdir = log_dir()
    os.makedirs(logdir, exist_ok=True)
    log = open(os.path.join(logdir, f"managed-{port}.log"), "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "mlx_lm", "server",
         "--model", MLX_REPOS[label], "--port", str(port)],
        stdout=log, stderr=log,
    )
    _managed_procs.append(proc)
    _mlx_procs[label] = proc
    print(f"  spawned MLX engine for {label} on port {port}")
    return True


def _stop_other_mlx(keep_label: str):
    """Keep one MLX model resident — each holds its full weights in RAM."""
    stopped = False
    for label, proc in list(_mlx_procs.items()):
        if label == keep_label or proc.poll() is not None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            pass
        _mlx_procs.pop(label, None)
        stopped = True
        print(f"  stopped idle MLX engine for {label}")
    if stopped:
        # Metal releases wired memory AFTER the process exits; spawning the
        # next engine immediately raced that teardown and died on startup
        # ("no MLX server answering" 6s after a swap, seen live). Give the
        # GPU allocator a beat to actually hand the memory back.
        time.sleep(2.5)


def ensure_mlx_engine(label: str, timeout: float = 180.0) -> bool:
    """Bring up the engine for `label` on demand, freeing the others first."""
    _, port = MODEL_ROUTES[label]
    if _port_in_use(port):
        _stop_other_mlx(label)
        return True
    if not mlx_model_cached(MLX_REPOS[label]):
        return False
    _stop_other_mlx(label)
    if not _spawn_mlx_engine(label):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_in_use(port):
            return True
        proc = _mlx_procs.get(label)
        if proc is not None and proc.poll() is not None:
            return False  # engine died on startup
        time.sleep(0.5)
    return False


def _spawn_ollama_serve() -> bool:
    """Start `ollama serve` if a binary exists and nothing owns port 11434."""
    if _port_in_use(11434):
        return True
    b = _ollama_bin()
    if not b:
        return False
    logdir = log_dir()
    os.makedirs(logdir, exist_ok=True)
    log = open(os.path.join(logdir, "managed-ollama.log"), "ab")
    _managed_procs.append(subprocess.Popen(
        [b, "serve"], stdout=log, stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ))
    print("  spawned ollama serve on port 11434")
    return True


def start_managed_engines():
    # MLX engines are started on demand (see ensure_mlx_engine) — each one
    # pins its whole model in RAM, so loading all of them at launch would
    # cost ~14 GB and starve the big Ollama models.
    _spawn_ollama_serve()


# ------------------------------------------------------- first-run setup
_setup_lock = threading.Lock()
_setup_jobs = {}  # label -> {"status": "downloading"|"done"|"error", "note": str}


def _download_model(label: str):
    repo = MLX_REPOS[label]
    try:
        from huggingface_hub import snapshot_download  # ships with mlx-lm
        snapshot_download(repo)
        # sweep carcasses: a KILLED earlier attempt leaves *.incomplete
        # blobs that poison the completeness check forever — a finished
        # 35B sat uncrowned behind eight of them (seen live)
        for p in glob.glob(os.path.join(
                _hf_model_dir(repo), "blobs", "*.incomplete")):
            try:
                os.remove(p)
            except Exception:
                pass
        with _setup_lock:
            _setup_jobs[label] = {"status": "done", "note": ""}
        _spawn_mlx_engine(label)
    except Exception as exc:
        with _setup_lock:
            _setup_jobs[label] = {"status": "error", "note": str(exc)[:200]}


TITLE_PROMPT = (
    "Summarise what this message is about in 3 to 6 words, written like a "
    "headline: a noun phrase, not a question, not first person, no quotes "
    "and no final punctuation. Do not answer the message \u2014 only label "
    "its topic.\n\nMESSAGE: ")


def make_title(text: str) -> str:
    """Name a chat with a small model — reusing whatever engine is already
    loaded, so it costs almost nothing."""
    pulled = ollama_pulled_tags() or set()
    usable = [l for l in MODEL_ROUTES
              if model_cached(l, pulled) and model_fits_memory(l)]
    # 1B models write poor titles; prefer something already resident, then
    # the smallest model that is still capable enough
    # 1B-class models produce garbage titles (seen looping "address address
    # address..."), so require a capable model even if a tiny one is resident
    capable = sorted((l for l in usable
                      if MODEL_MEM_BYTES.get(l, 0) >= 2.4e9),
                     key=lambda l: MODEL_MEM_BYTES.get(l, 0))
    live = [l for l in capable
            if MODEL_ROUTES[l][0] == "mlx" and _port_in_use(MODEL_ROUTES[l][1])]
    order = (live[:1] + [l for l in capable if l not in live[:1]])[:2] \
        or usable[:1]
    for label in order:
        try:
            parts = []
            run_model(label, [{"role": "user",
                               "content": TITLE_PROMPT + text[:600]}],
                      parts.append)
            title = " ".join(
                strip_think(strip_special("".join(parts))).split())
            title = title.split("\n")[0]
            title = re.sub(r"^(topic|title)\s*:?\s*", "", title, flags=re.I)
            title = title.strip("\"'*#\u2014- .")
            if 2 < len(title) < 70 and not _looks_degenerate(title):
                return title
        except Exception:
            pass
    return ""


# ------------------------------------------------------------- updates
_update = {"state": "idle", "pct": 0, "note": "", "latest": "", "url": "",
           "size": 0}

_SWAP_SCRIPT = """#!/bin/zsh
# Wait for MillenAI to quit so its bundle can be replaced safely.
for i in $(seq 1 60); do
  pgrep -f "%(app)s/Contents/MacOS/MillenAI" >/dev/null || break
  sleep 0.5
done
MP=$(mktemp -d)
hdiutil attach -nobrowse -readonly -quiet -mountpoint "$MP" "%(dmg)s" || exit 1
NEW=$(ls -d "$MP"/*.app 2>/dev/null | head -1)
if [[ -n "$NEW" ]]; then
  ditto "$NEW" "%(app)s.new" && rm -rf "%(app)s" && mv "%(app)s.new" "%(app)s"
  xattr -dr com.apple.quarantine "%(app)s" 2>/dev/null
fi
hdiutil detach -quiet "$MP"
rm -rf "%(tmp)s"
open -n "%(app)s"
"""


def _app_bundle_path():
    """/Applications/MillenAI.app when running from a bundle, else None.

    Windows installs aren't a single swappable bundle, so in-place update is
    macOS-only for now; Windows users are pointed at the release page.
    """
    if not IS_MAC:
        return None
    here = os.path.abspath(__file__)
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    return root if root.endswith(".app") else None


def _own_build_time() -> float:
    """When this build was produced — the yardstick for 'newer release'."""
    if APP_BUILD_DATE:
        try:
            return time.mktime(time.strptime(APP_BUILD_DATE, "%Y-%m-%d"))
        except ValueError:
            pass
    try:
        return os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        return 0.0


def _gh_time(iso: str) -> float:
    # GitHub stamps releases in UTC; mktime would read them as local time
    # and make every release look hours newer than it is
    try:
        return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0


def _build_from_tag(tag):
    nums = re.findall(r"\d+", tag or "")
    return int(nums[-1]) if nums else 0


_dl_cache = {"ts": 0, "data": {}}


def download_links() -> dict:
    """Latest installer URLs per platform, cached for an hour."""
    if time.time() - _dl_cache["ts"] < 3600 and _dl_cache["data"]:
        return _dl_cache["data"]
    out = {}
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "MillenAI"})
        with urllib.request.urlopen(req, timeout=8) as r:
            rel = json.loads(r.read().decode("utf-8"))
        for a in rel.get("assets", []):
            n = a.get("name", "")
            u = a.get("browser_download_url", "")
            if n.endswith(".dmg"):
                out["mac"] = u
            elif n.endswith(".msi"):
                out["win"] = u
            elif n.endswith("-Windows.zip"):
                out.setdefault("win_zip", u)
        out["version"] = (rel.get("name") or "").strip()
        _dl_cache.update({"ts": time.time(), "data": out})
    except Exception:
        pass
    return out


def check_update():
    if not UPDATE_REPO:
        return {"configured": False, "available": False}
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "MillenAI"})
        with urllib.request.urlopen(req, timeout=8) as r:
            rel = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 404 simply means the repo has no releases yet — not a failure
        note = ("no releases published yet" if exc.code == 404
                else "HTTP %s" % exc.code)
        return {"configured": True, "available": False, "note": note}
    except Exception as exc:
        return {"configured": True, "available": False, "note": str(exc)[:120]}
    tag = rel.get("tag_name", "")
    # the tag is a build counter (v19); the release *title* is the version
    # people recognise (1.0.3) — show that, but still compare on the tag
    shown = (rel.get("name") or "").strip() or tag
    dmg = next((a for a in rel.get("assets", [])
                if a.get("name", "").endswith(".dmg")), None)
    if dmg:
        _update["url"] = dmg["browser_download_url"]
        _update["size"] = dmg.get("size", 0)
    _update["latest"] = shown
    published = _gh_time(rel.get("published_at", ""))
    # a release is newer ONLY if its tag carries a higher build number.
    # (The old published-after-my-build-time clause false-alarmed on every
    # release when the bundle had been hot-patched: its mtime never moves,
    # so "Update available 2.0.1 — you have 2.0.1". Seen live.)
    newer = _build_from_tag(tag) > APP_BUILD
    return {"configured": True,
            "available": bool(dmg) and newer
                         and _app_bundle_path() is not None,
            "latest": shown, "tag": tag, "current": APP_VERSION,
            "published": rel.get("published_at", ""),
            "size_mb": round(dmg.get("size", 0) / 1e6, 1) if dmg else 0}


def _do_update():
    """Download the release DMG, then hand off to a helper that swaps the
    bundle after we quit and relaunches. Chats live in WebKit storage and
    memory in Application Support, so both survive the swap."""
    app = _app_bundle_path()
    if not app or not _update.get("url"):
        _update.update(state="error", note="no update available")
        return
    try:
        _update.update(state="downloading", pct=0, note="")
        tmp = tempfile.mkdtemp(prefix="millenai-up-")
        dmg = os.path.join(tmp, "update.dmg")
        req = urllib.request.Request(_update["url"],
                                     headers={"User-Agent": "MillenAI"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dmg, "wb") as f:
            total = int(r.headers.get("Content-Length") or _update["size"] or 1)
            done = 0
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                _update["pct"] = min(99, int(done / total * 100))
        _update.update(state="installing", pct=100)
        script = os.path.join(tmp, "swap.sh")
        with open(script, "w", encoding="utf-8") as f:
            f.write(_SWAP_SCRIPT % {"app": app, "dmg": dmg, "tmp": tmp})
        os.chmod(script, 0o755)
        subprocess.Popen(["/bin/zsh", script], start_new_session=True)
        _update["state"] = "restarting"
        threading.Timer(1.5, lambda: os._exit(0)).start()
    except Exception as exc:
        _update.update(state="error", note=str(exc)[:180])


# ------------------------------------------------------------- memory
# Lasting facts about the user (name, job, interests…) live in a local
# JSON file and are folded into the system prompt of every chat, so any
# model can reference them across conversations. Extraction runs in the
# background after each message, using the model that just answered
# (it's already loaded — no engine thrash).
# Chats live on disk, not in localStorage: WebKit keys its storage to the
# bundle identity, which differs between running from source and from the
# .app, and isn't guaranteed to survive a bundle swap. These files do.
#
# MULTI-USER: every function below takes a `base` directory. None means the
# legacy files in app_dir() — the machine owner's data, what the desktop
# app uses. Web visitors sign in at the WELCOME page and get their own
# base under app_dir()/users/<id>/, so nobody ever reads Patrick's chats
# through the tunnel.


def _pfile(name: str, base=None) -> str:
    return os.path.join(base or app_dir(), name)


def load_prefs(base=None) -> dict:
    try:
        with open(_pfile("prefs.json", base), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def store_prefs(d: dict, base=None):
    p = _pfile("prefs.json", base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)
_chats_lock = threading.Lock()


def load_chats(base=None) -> list:
    try:
        with open(_pfile("chats.json", base), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def store_chats(items: list, base=None):
    """Atomic write — a crash mid-save must not corrupt the history."""
    p = _pfile("chats.json", base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items[:60], f)
    os.replace(tmp, p)
_memory_lock = threading.Lock()

MEMORY_PROMPT = (
    "You maintain long-term memory for an assistant. From the user message "
    "below, extract lasting personal facts about the user worth remembering "
    "across conversations: their name, job, location, family, pets, "
    "interests, preferences, ongoing projects, goals. Ignore temporary "
    "context, questions, instructions, and anything about the assistant. "
    "Reply with each fact on its own line starting with '- ', at most 3 "
    "facts, each under 15 words. If there is nothing worth remembering, "
    "reply with exactly: NONE\n\nUSER MESSAGE: "
)


def _load_memory(base=None) -> list:
    try:
        with open(_pfile("memory.json", base), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_memory(items: list, base=None):
    p = _pfile("memory.json", base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(items[-60:], f, indent=1)


def memory_text(base=None) -> str:
    return "\n".join("- " + i["fact"] for i in _load_memory(base)[-40:])


def _extract_memory(label: str, user_msg: str, base=None):
    try:
        parts = []
        run_model(label, [{"role": "user",
                           "content": MEMORY_PROMPT + user_msg[:2000]}],
                  parts.append)
        out = "".join(parts)
        facts = [ln.strip()[2:].strip() for ln in out.splitlines()
                 if ln.strip().startswith("- ")]
        facts = [f for f in facts
                 if 5 < len(f) < 160 and "NONE" not in f.upper()]

        # GROUNDING: small models INVENT people wholesale — a real memory
        # file was found holding "Name: Emily Wilson", "Location: Munich",
        # "Job: Park Ranger", none of it ever said. A fact may only be
        # stored if every proper noun in it (past the first word) actually
        # appears in the user's message.
        msg_low = user_msg.lower()

        def grounded(f):
            # a NAME claim needs the user to have actually introduced
            # themselves — "whats that place in bk, seawolf?" produced
            # "User's name: Seawolf" (seen live; models then greeted the
            # user as a seafood restaurant)
            if re.match(r"\s*(user'?s?\s+)?name\b", f, re.I) and not re.search(
                    r"\b(my name is|i'?m called|call me|i am [A-Z])", user_msg):
                return False
            for w in re.findall(r"\b[A-Z][a-z]{2,}\b", f)[0:]:
                if f.strip().startswith(w) and f.strip().index(w) == 0:
                    continue          # sentence-initial capital is fine
                if w.lower() not in msg_low:
                    return False
            return True

        facts = [f for f in facts if grounded(f)]
        if not facts:
            return
        with _memory_lock:
            items = _load_memory(base)
            known = {i["fact"].lower() for i in items}
            for f in facts:
                if f.lower() not in known:
                    items.append({"fact": f, "ts": time.time()})
            _save_memory(items, base)
    except Exception:
        pass  # memory is best-effort — never break chat over it


# ------------------------------------------------------------- voice
# STT: whisper via MLX (Apple silicon only). TTS: macOS built-in `say`.
WHISPER_REPO = ("deepdml/faster-whisper-large-v3-turbo-ct2" if not IS_MAC
                else "mlx-community/whisper-large-v3-turbo")
_whisper_lock = threading.Lock()
_fw_model = None   # cached faster-whisper model (non-mac)
_say_proc = None


def _voice_supported() -> bool:
    """Speech-to-text needs MLX on Apple silicon, faster-whisper elsewhere."""
    if IS_ARM:
        try:
            import mlx_whisper  # noqa: F401
            return True
        except ImportError:
            return False
    if IS_WIN:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False
    return False


def _voice_ready() -> bool:
    d = _hf_model_dir(WHISPER_REPO)
    snaps = glob.glob(os.path.join(d, "snapshots", "*", "config.json"))
    if not snaps:
        return False
    snap = os.path.dirname(snaps[0])
    # the weights symlink only appears once its blob COMPLETED — that is
    # the real signal. (A stale *.incomplete carcass beside a finished
    # blob bricked voice when this gated on carcasses. Seen live.)
    return bool(glob.glob(os.path.join(snap, "weights.*"))
                or glob.glob(os.path.join(snap, "model.bin")))


VOICE_ROW = "Voice engine"


def _prepare_voice():
    with _setup_lock:
        if _setup_jobs.get(VOICE_ROW, {}).get("status") == "downloading":
            return
        _setup_jobs[VOICE_ROW] = {"status": "downloading", "note": "", "pct": 0}

    def work():
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(WHISPER_REPO)
            with _setup_lock:
                _setup_jobs[VOICE_ROW] = {"status": "done", "note": "",
                                          "pct": 100}
        except Exception as exc:
            with _setup_lock:
                _setup_jobs[VOICE_ROW] = {"status": "error",
                                          "note": str(exc)[:200], "pct": 0}
    threading.Thread(target=work, daemon=True).start()


def _transcribe_wav(wav_bytes: bytes) -> str:
    import io
    import wave as _wave
    import numpy as np
    if IS_ARM:
        import mlx_whisper
    with _wave.open(io.BytesIO(wav_bytes)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        audio = np.frombuffer(w.readframes(w.getnframes()),
                              np.int16).astype(np.float32) / 32768.0
    if ch == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)
    if sr != 16000:  # linear resample is fine for speech
        n = int(len(audio) * 16000 / sr)
        audio = np.interp(np.linspace(0, len(audio) - 1, n),
                          np.arange(len(audio)), audio).astype(np.float32)
    with _whisper_lock:
        if IS_ARM:
            out = mlx_whisper.transcribe(audio, path_or_hf_repo=WHISPER_REPO)
            return out["text"].strip()
        # faster-whisper: CUDA when the box has it, CPU otherwise
        from faster_whisper import WhisperModel
        global _fw_model
        if _fw_model is None:
            try:
                _fw_model = WhisperModel(WHISPER_REPO, device="cuda",
                                         compute_type="float16")
            except Exception:
                _fw_model = WhisperModel(WHISPER_REPO, device="cpu",
                                         compute_type="int8")
        segments, _info = _fw_model.transcribe(audio, beam_size=5)
        return " ".join(sg.text for sg in segments).strip()


def _speak(text: str):
    """Read a reply aloud with the system voice; new speech cuts off old."""
    global _say_proc
    _stop_speaking()
    # Reasoning is for reading, never for listening. The markdown pass below
    # does not know about the tags either, so "<think" was being spoken as a
    # word before the entire chain of thought.
    plain = strip_think(text)
    # a research brief ends in a bibliography — reading a list of source
    # titles aloud roughly doubled the length of every spoken answer
    plain = re.split(r"\n\s*\**\s*Sources\s*\**\s*\n", plain)[0]
    # strip the markdown the models produce so `say` doesn't read symbols
    plain = re.sub(r"```[\s\S]*?```", " code block omitted. ", plain)
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)   # links before refs
    plain = re.sub(r"\[\d+(?:\s*,\s*\d+)*\]", "", plain)     # "[1]", "[2, 5]"
    plain = re.sub(r"[*_#`>|]", "", plain)
    plain = re.sub(r"[ \t]{2,}", " ", plain)
    plain = re.sub(r"\s+([.,;:!?])", r"\1", plain)   # tidy the gap a cite left
    text = plain.strip()[:4000]
    if not text:
        return
    if IS_WIN:
        # SAPI through PowerShell — built in, no download
        ps = ("Add-Type -AssemblyName System.Speech;"
              "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              "$s.Speak([Console]::In.ReadToEnd())")
        _say_proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps],
            stdin=subprocess.PIPE, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            _say_proc.stdin.write(text)
            _say_proc.stdin.close()
        except Exception:
            pass
    else:
        _say_proc = subprocess.Popen(["say", text])


def _stop_speaking():
    global _say_proc
    if _say_proc and _say_proc.poll() is None:
        try:
            _say_proc.terminate()
        except Exception:
            pass
    _say_proc = None


ENGINE_ROW = "Ollama engine"


def _download_ollama_binary():
    """Fetch the signed universal Ollama CLI, with job progress."""
    os.makedirs(_MANAGED_BIN_DIR, exist_ok=True)
    url = OLLAMA_ZIP_URL if IS_WIN else OLLAMA_TGZ_URL
    tmp = os.path.join(_MANAGED_BIN_DIR,
                       "ollama.zip.part" if IS_WIN else "ollama.tgz.part")
    req = urllib.request.Request(url,
                                 headers={"User-Agent": "MillenAI/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 150_000_000)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            with _setup_lock:
                _setup_jobs[ENGINE_ROW]["pct"] = min(99, int(done / total * 100))
    if IS_WIN:
        import zipfile
        with zipfile.ZipFile(tmp) as z:
            z.extractall(_MANAGED_BIN_DIR)
        os.remove(tmp)
        # the zip nests the binary under bin/ or ollama/ depending on build
        if not os.path.exists(os.path.join(_MANAGED_BIN_DIR, "ollama.exe")):
            for root, _d, files in os.walk(_MANAGED_BIN_DIR):
                if "ollama.exe" in files:
                    _MANAGED_BIN_DIR_FOUND.append(root)
                    break
        return
    with tarfile.open(tmp) as t:
        try:
            t.extractall(_MANAGED_BIN_DIR, filter="data")
        except TypeError:  # python < 3.12 has no filter kwarg
            t.extractall(_MANAGED_BIN_DIR)
    os.remove(tmp)
    os.chmod(os.path.join(_MANAGED_BIN_DIR, "ollama"), 0o755)


def _ensure_ollama_ready() -> bool:
    """Binary on disk + server answering. Downloads the engine if needed."""
    if _ollama_bin() is None:
        with _setup_lock:
            _setup_jobs[ENGINE_ROW] = {"status": "downloading",
                                       "note": "", "pct": 0}
        _download_ollama_binary()
        with _setup_lock:
            _setup_jobs[ENGINE_ROW] = {"status": "done", "note": "",
                                       "pct": 100}
    _spawn_ollama_serve()
    for _ in range(40):
        if _port_in_use(11434):
            return True
        time.sleep(0.5)
    return False


def _pull_ollama_model(label: str, tag: str):
    """`ollama pull` via the API, streaming progress into the job dict."""
    payload = json.dumps({"model": tag, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/pull", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("error"):
                raise RuntimeError(obj["error"])
            total, done = obj.get("total"), obj.get("completed")
            if total:
                with _setup_lock:
                    _setup_jobs[label]["pct"] = min(
                        99, int((done or 0) / total * 100))


def _ollama_install_worker(labels: list):
    """Engine first, then the models one at a time (kind to old disks)."""
    try:
        if not _ensure_ollama_ready():
            raise RuntimeError("the Ollama engine did not start")
    except Exception as exc:
        with _setup_lock:
            _setup_jobs[ENGINE_ROW] = {"status": "error",
                                       "note": str(exc)[:200], "pct": 0}
            for l in labels:
                _setup_jobs[l] = {"status": "error",
                                  "note": "engine unavailable", "pct": 0}
        return
    for label in labels:
        with _setup_lock:
            _setup_jobs[label] = {"status": "downloading", "note": "",
                                  "pct": 0}
        try:
            _pull_ollama_model(label, MODEL_ROUTES[label][1])
            with _setup_lock:
                _setup_jobs[label] = {"status": "done", "note": "",
                                      "pct": 100}
        except Exception as exc:
            with _setup_lock:
                _setup_jobs[label] = {"status": "error",
                                      "note": str(exc)[:200], "pct": 0}


def start_model_downloads(labels=None) -> list:
    """Kick off background downloads (first-run starters, or a chosen few)."""
    started, ollama_batch = [], []
    pulled = ollama_pulled_tags() or set()
    for label in (labels if labels is not None else STARTER_LABELS):
        if not SUPPORTED.get(label):
            continue
        kind, _target = MODEL_ROUTES[label]
        if model_cached(label, pulled):
            continue
        with _setup_lock:
            if _setup_jobs.get(label, {}).get("status") in ("downloading",
                                                            "queued"):
                continue
        if kind == "mlx":
            with _setup_lock:
                _setup_jobs[label] = {"status": "downloading", "note": ""}
            threading.Thread(target=_download_model, args=(label,),
                             daemon=True).start()
        else:
            with _setup_lock:
                _setup_jobs[label] = {"status": "queued", "note": "",
                                      "pct": 0}
            ollama_batch.append(label)
        started.append(label)
    if ollama_batch:
        threading.Thread(target=_ollama_install_worker,
                         args=(ollama_batch,), daemon=True).start()
    return started


_dl_sample = {"bytes": 0, "ts": 0.0, "bps": 0.0}


def _downloaded_bytes(pulled) -> tuple:
    """(bytes on disk, bytes expected) across every first-run model."""
    have = want = 0
    for label in STARTER_LABELS:
        est = MLX_EST_BYTES.get(label, 0)
        want += est
        kind = MODEL_ROUTES.get(label, ("",))[0]
        with _setup_lock:
            job = dict(_setup_jobs.get(label, {}))
        if model_cached(label, pulled):
            have += est
        elif job.get("status") not in ("downloading", "queued"):
            pass          # stalled/never started — counts as nothing yet
        elif kind == "mlx":
            have += min(est, _dir_bytes(_hf_model_dir(MLX_REPOS[label])))
        else:
            have += int(est * job.get("pct", 0) / 100)
    return have, want


def _dl_speed(have: int) -> float:
    """Bytes/sec, smoothed, from the change since the last poll."""
    now = time.time()
    last_ts, last_b = _dl_sample["ts"], _dl_sample["bytes"]
    if last_ts and now > last_ts + 0.4:
        inst = max(0.0, (have - last_b) / (now - last_ts))
        # ignore the jump when a finished model flips to its full size
        if inst < 300e6:
            _dl_sample["bps"] = (0.6 * _dl_sample["bps"] + 0.4 * inst
                                 if _dl_sample["bps"] else inst)
    if not last_ts or now > last_ts + 0.4:
        _dl_sample.update(bytes=have, ts=now)
    return _dl_sample["bps"]


_job_watch = {}   # label -> (pct, ts of last movement)


def setup_status() -> dict:
    # WATCHDOG: a download thread that dies mid-write leaves its job in
    # "downloading" forever, and the whole setup panel reads busy for the
    # rest of the process's life (seen live: Phi-4 wedged at 99%). Ten
    # minutes without the pct moving flips the job to error.
    now = time.time()
    with _setup_lock:
        for label, job in list(_setup_jobs.items()):
            if job.get("status") != "downloading":
                _job_watch.pop(label, None)
                continue
            pct = job.get("pct", 0)
            prev = _job_watch.get(label)
            if prev is None or prev[0] != pct:
                _job_watch[label] = (pct, now)
            elif now - prev[1] > 600:
                job["status"] = "error"
                job["note"] = "stalled — press Retry"
                _setup_jobs[label] = job
                _job_watch.pop(label, None)
    pulled = ollama_pulled_tags() or set()
    models = []

    # engine pseudo-row: shown only while the app still has to fetch Ollama
    starters_need_ollama = any(
        MODEL_ROUTES[l][0] == "ollama" for l in STARTER_LABELS)
    with _setup_lock:
        ejob = dict(_setup_jobs.get(ENGINE_ROW, {}))
    if starters_need_ollama and (ejob or _ollama_bin() is None):
        status = ejob.get("status", "missing")
        if status == "done" or (_ollama_bin() and not ejob):
            status = "ready"
        models.append({"label": ENGINE_ROW, "est_gb": 0.2,
                       "status": status,
                       "pct": 100 if status == "ready"
                       else ejob.get("pct", 0),
                       "note": ejob.get("note", "")})

    # fit-filtered like the sidebar: the add-models panel never offers a
    # model this machine cannot hold resident
    stars_now = set(_starter_labels())
    for label in [l for l in MODEL_INFO
                  if SUPPORTED.get(l) and model_fits_machine(l)]:
        kind, _target = MODEL_ROUTES[label]
        est = MLX_EST_BYTES.get(label, 5_000_000_000)
        with _setup_lock:
            job = dict(_setup_jobs.get(label, {}))
        if model_cached(label, pulled) or job.get("status") == "done":
            status, pct = "ready", 100
        else:
            status = job.get("status", "missing")
            if kind == "mlx":
                pct = min(99, round(
                    _dir_bytes(_hf_model_dir(MLX_REPOS[label])) / est * 100))
            else:
                pct = job.get("pct", 0)
        models.append({"label": label, "est_gb": round(est / 1e9, 1),
                       "status": status, "pct": pct,
                       "star": label in stars_now,
                       "supported": SUPPORTED.get(label, True),
                       "note": job.get("note", "")})

    ready_n = sum(1 for x in models if x["status"] == "ready")
    have, want = _downloaded_bytes(pulled)
    bps = _dl_speed(have)
    busy = any(m["status"] in ("downloading", "queued") for m in models)
    return {
        "have_gb": round(have / 1e9, 1), "want_gb": round(want / 1e9, 1),
        "overall_pct": round(have / want * 100) if want else 100,
        "speed_mbs": round(bps / 1e6, 1) if busy else 0,
        "eta_min": (round((want - have) / bps / 60)
                    if busy and bps > 1e5 and want > have else None),
        "busy": busy,
        # nag on first run only: once a couple of models work, the welcome
        # screen is opt-in via "Add models…"
        "needs_setup": ready_n < 2,
        "mem_gb": round(psutil.virtual_memory().total / 1e9),
        "plans": {pl: round(sum(
            MODEL_INFO[l]["gb"] for l in plan_labels(pl)
            if not model_cached(l, pulled)), 1)
            for pl in ("basic", "pro", "max")},
        "ready_n": ready_n,
        "mlx_ok": _has_mlx() if IS_ARM else True,
        "ollama": _ollama_bin() is not None,
        "arch": "arm64" if IS_ARM else "x86_64",
        # human name for the About panel: "MillenAI Apple Silicon" etc.
        "plat": (("Apple Silicon" if IS_ARM else "Intel x64") if IS_MAC else
                 ("Windows ARM64" if IS_WIN_ARM else "Windows x64") if IS_WIN
                 else "Linux"),
        "disk_free_gb": round(
            shutil.disk_usage(os.path.expanduser("~")).free / 1e9),
        "models": models,
    }


def _other_millenai_running() -> bool:
    """Another MillenAI process on this machine — desktop, live service,
    or a dev instance on any port. Engines are shared by port, so our
    shutdown must never terminate one a sibling is still using. (Checking
    only 8889/9889 missed a :9899 instance and knifed the desktop's
    engine — seen live, twice.)"""
    try:
        out = subprocess.run(["pgrep", "-f", "millenai.py"],
                             capture_output=True, text=True, timeout=4).stdout
        pids = {int(x) for x in out.split() if x.isdigit()}
        pids.discard(os.getpid())
        pids.discard(os.getppid())
        if pids:
            return True
    except Exception:
        pass
    for p in (8889, 9889):
        if p != PORT and _port_in_use(p):
            return True
    return False


def stop_managed_engines():
    # THE ORPHAN FACTORY, finally closed: this used to clear() _mlx_procs
    # without terminating them — every quit left engines pinning wired
    # Metal memory (nine were found feral at once). Kill everything we
    # spawned, MLX engines included.
    # ...unless a SIBLING MillenAI is live on this machine: engines are
    # shared by port, so killing ours would knife the app still using
    # them (seen live: a live-service restart broke the desktop's next
    # query). The boot reaper and idle janitor clean up either way.
    if _other_millenai_running():
        _mlx_procs.clear()
        return
    for p in list(_managed_procs) + list(_mlx_procs.values()):
        try:
            p.terminate()
        except Exception:
            pass
    for p in list(_managed_procs) + list(_mlx_procs.values()):
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _managed_procs.clear()
    _mlx_procs.clear()


atexit.register(stop_managed_engines)


def _signal_exit(signum, _frame):
    # atexit does NOT run on SIGTERM/SIGHUP — without this, force-quitting
    # the app leaves multi-GB model servers resident forever
    stop_managed_engines()
    os._exit(0)


for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    try:
        signal.signal(_sig, _signal_exit)
    except (ValueError, OSError):
        pass  # not on the main thread / unsupported

_gpu_cache = {"pct": None, "ts": 0.0}


def _gpu_nvidia():
    """NVIDIA utilisation via nvidia-smi, which ships with the driver."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip().splitlines()
        return float(out[0].strip()) if out else None
    except Exception:
        return None


def gpu_utilization():
    """GPU busy percentage, or None when it can't be read."""
    now = time.time()
    if now - _gpu_cache["ts"] < 0.7:
        return _gpu_cache["pct"]
    if not IS_MAC:
        pct = _gpu_nvidia()
        _gpu_cache.update(pct=pct, ts=now)
        return pct
    pct = None
    try:
        out = subprocess.run(
            ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator", "-a"],
            capture_output=True, timeout=2,
        ).stdout
        for dev in plistlib.loads(out):
            val = dev.get("PerformanceStatistics", {}).get("Device Utilization %")
            if val is not None:
                pct = float(val)
                break
    except Exception:
        pct = None
    _gpu_cache.update(pct=pct, ts=now)
    return pct


_results_cache = {}        # query -> (fetched_at, [result dicts])
_RESULTS_TTL = 300.0


def _page_text(url: str, cap: int = 2600) -> str:
    """The readable text of a page, or "" — research quality lives and
    dies on this: models writing briefs from 200-char snippets invent the
    rest, so the top sources get actually READ."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh) MillenAI"})
        with urllib.request.urlopen(req, timeout=7) as r:
            if "text/html" not in (r.headers.get("Content-Type") or ""):
                return ""
            raw = r.read(400_000).decode("utf-8", "replace")
        raw = re.sub(r"(?is)<(script|style|nav|header|footer|aside)[^>]*>"
                     r".*?</\1>", " ", raw)
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        raw = re.sub(r"&[a-z]+;", " ", raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        return raw[:cap]
    except Exception:
        return ""


def search_results(query: str, limit: int = 5) -> list:
    """Structured DuckDuckGo hits — title, snippet and URL. Never raises.

    Deliberately separate from run_search's single-slot cache: a research
    run fires several queries back to back, and a one-entry cache would
    evict each one before the next could reuse it.
    """
    if not HAS_SEARCH:
        return []
    now = time.time()
    with _search_lock:
        hit = _results_cache.get(query)
        if hit and now - hit[0] < _RESULTS_TTL:
            return hit[1]
    try:
        out = [{"title": (r.get("title") or "").strip(),
                "body": (r.get("body") or "").strip(),
                "url": (r.get("href") or "").strip()}
               for r in DDGS().text(query, max_results=limit)]
    except Exception:
        out = []                      # offline or rate-limited — not fatal
    with _search_lock:
        if len(_results_cache) > 40:
            _results_cache.clear()
        _results_cache[query] = (now, out)
    return out


def run_search(query: str) -> str:
    """DuckDuckGo snippets with a 60s cache. Never raises."""
    if not HAS_SEARCH:
        return ("Search is unavailable — install it with: "
                "pip install ddgs")
    with _search_lock:
        fresh = (time.time() - _search_cache["timestamp"]) < 60
        if _search_cache["query"] == query and fresh:
            return _search_cache["data"]
    try:
        results = DDGS().text(query, max_results=4)
        ctx = "\n".join(
            f"- {r.get('title', '')}: {r.get('body', '')}" for r in results
        )
        if not ctx.strip():
            ctx = "No snippets found."
    except Exception as exc:  # network hiccups, rate limits, etc.
        ctx = f"Search failed: {exc}"
    with _search_lock:
        _search_cache.update(query=query, data=ctx, timestamp=time.time())
    return ctx


def stream_ollama(tag: str, messages: list, emit) -> None:
    """Stream NDJSON from Ollama, calling emit(text_chunk) as tokens arrive."""
    payload = json.dumps({
        "model": tag,
        "messages": messages,
        "stream": True,
        # unload fast after use: Ollama's default keep-alive left LLaVA
        # resident at 8.6 GB GPU for 5 minutes after every glance at an
        # image — the llama-server runner ate cores "even when closed"
        "keep_alive": "45s",
        "options": {"temperature": 0.75},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            obj = json.loads(line)
            if "error" in obj:
                raise RuntimeError(obj["error"])
            chunk = obj.get("message", {}).get("content", "")
            if chunk:
                emit(chunk)
            if obj.get("done"):
                break


def stream_openai_compat(port: int, model_label: str, messages: list, emit,
                         thinking: bool = False) -> None:
    """Stream from an OpenAI-compatible server (MLX / llama.cpp / LM Studio).

    Robust to servers that ignore `stream: true` and reply with one JSON
    blob — if no SSE tokens arrive, the whole body is parsed as a plain
    completion instead.
    """
    payload = json.dumps({
        # mlx_lm validates this as a HF repo id — the UI label 404s
        "model": MLX_REPOS.get(model_label, "default_model"),
        "messages": messages,
        # a reasoning model spends most of its budget thinking before it
        # writes a word — 2048 ran out mid-thought and produced nothing
        "max_tokens": 8192,
        "temperature": 0.75,
        "stream": True,
        # Native reasoning is OFF by default. Gemma 4 26B does not converge:
        # asked for a taco recommendation it produced 11,937 characters of
        # deliberation, hit the token ceiling and returned no answer at all,
        # after 77 seconds. With it off the same question answers in ~5s.
        # The parser below still handles reasoning if a server sends it
        # anyway; we simply stop asking for it. Templates that don't know the
        # flag ignore it, so this is safe to send to every model.
        "chat_template_kwargs": {"enable_thinking": thinking},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    emitted = False
    in_think = False
    raw_body = []
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace")
            raw_body.append(line)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choice = obj.get("choices", [{}])[0]
            delta = choice.get("delta") or {}
            whole = choice.get("message") or {}
            # Reasoning models stream their chain of thought in a separate
            # `reasoning` field — it is *not* `content`. Reading only
            # `content` meant a model like Gemma 4 appeared to answer with
            # nothing at all. Wrap it so it lands in the same collapsible
            # block the UI already renders for DeepSeek R1's <think> tags.
            think = delta.get("reasoning") or whole.get("reasoning") or ""
            if think:
                if not in_think:
                    in_think = True
                    emit("<think>")
                emitted = True
                emit(think)
            chunk = delta.get("content", "") or whole.get("content", "")
            if chunk:
                if in_think:
                    in_think = False
                    emit("</think>")
                emitted = True
                emit(chunk)
    if in_think:                      # ran out of budget still thinking
        emit("</think>")

    if not emitted:
        # server didn't stream — try the body as one plain JSON completion
        try:
            obj = json.loads("".join(raw_body))
            text = obj["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError):
            raise RuntimeError(
                "the server answered but sent no usable completion "
                f"(first bytes: {''.join(raw_body)[:120]!r})"
            )
        if text:
            emit(text)
        else:
            raise RuntimeError("the server returned an empty completion")


END_TOKENS = ("<end_of_turn>", "<|eot_id|>", "<|im_end|>", "</s>",
              "<|endoftext|>")


def strip_special(text: str) -> str:
    """Remove end-of-turn markers some engines leak as literal text."""
    for t in END_TOKENS:
        if t in text:
            text = text.replace(t, "")
    return text


# an unterminated block counts too — a model cut off mid-thought never
# closes the tag
_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.S)


def strip_think(text: str) -> str:
    """Drop chain-of-thought, leaving only the answer.

    Reasoning is worth showing a person but never worth feeding back into a
    prompt: it is many times longer than the answer it precedes, so leaving
    it in blows straight past the merge-prompt truncation and buries the
    actual answers.
    """
    return _THINK_RE.sub("", text).strip()


def fold_system(messages: list) -> list:
    """Some chat templates (Gemma 2) reject the system role outright —
    merge the system prompt into the first user turn instead."""
    sys_txt = "\n\n".join(
        m["content"] for m in messages if m["role"] == "system")
    out = [dict(m) for m in messages if m["role"] != "system"]
    if sys_txt:
        for m in out:
            if m["role"] == "user":
                m["content"] = sys_txt + "\n\n" + m["content"]
                break
    return out


def run_model(label: str, messages: list, emit, thinking: bool = False) -> None:
    """Stream one model's answer, handling engine startup and templates."""
    # NO 70B fallback: an unknown label used to route to llama3.3:70b on
    # Ollama — a 40 GB model on a 48 GB Mac. Its runner got OOM-killed,
    # Ollama respawned it, repeat forever ("llama-server won't stop
    # starting", seen live). Fall back to the SMALLEST cached model.
    if label not in MODEL_ROUTES:
        pulled = ollama_pulled_tags() or set()
        label = next((l for l in reversed(MERGE_RANK)
                      if model_cached(l, pulled)), label)
    kind, target = MODEL_ROUTES.get(label, (None, None))
    if kind is None:
        raise RuntimeError("no downloaded model can take this request — "
                           "grab one in Settings › Download models…")
    if kind == "mlx":
        global _mlx_last_use
        _mlx_last_use = time.time()
        with _engine_lock:
            ensure_mlx_engine(label)
    msgs, attempts, folded = messages, 0, False
    while True:
        try:
            got = []

            def _tap(chunk):
                got.append(chunk)
                emit(chunk)

            if kind == "ollama":
                stream_ollama(target, msgs, _tap)
            else:
                stream_openai_compat(target, label, msgs, _tap, thinking)
            if not "".join(got).strip() and attempts < 1 and kind == "mlx":
                # a silent engine is a dead engine — respawn once
                attempts += 1
                with _engine_lock:
                    _mlx_procs.pop(label, None)
                    ensure_mlx_engine(label)
                time.sleep(1.5)
                continue
            return
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if not folded and "system role" in detail.lower():
                # Gemma-style template — retry without a system turn
                folded = True
                msgs = fold_system(msgs)
                continue
            e.cached_body = detail  # for offline_hint (read-once)
            raise  # real answer from the engine — surface it
        except urllib.error.URLError:
            # engine may be mid-startup, or another MillenAI instance on
            # this machine terminated it on ITS exit (shared 88xx ports —
            # seen live: a release restart killed the desktop's engine).
            # Respawn it and try again before ever surfacing an error.
            attempts += 1
            if attempts > 2:
                raise
            if kind == "mlx":
                with _engine_lock:
                    _mlx_procs.pop(label, None)
                    ensure_mlx_engine(label)
            time.sleep(1.5 * attempts)


# The merge is where the final answer's VOICE gets written, so the style
# spec lives here: the qualities of a top-tier assistant reply — lead with
# the answer, confident flowing prose, no filler, no bullet-spam — without
# ever claiming any identity.
SYNTH_INSTRUCTION = (
    "Below are several draft answers to the same question. Write ONE "
    "final answer that keeps every correct and useful detail and drops "
    "repetition. If the drafts disagree on a fact, state the correct "
    "information plainly.\n"
    "VOICE — follow all of these:\n"
    "- Open with the answer itself. Never restate the question, never "
    "start with filler like 'Great question' or 'Certainly'.\n"
    "- Write in confident, flowing prose, as one smart person talking to "
    "another. Use a list ONLY when the content is truly enumerable; never "
    "turn an explanation into bullet points.\n"
    "- Keep every correct, useful, CONCRETE detail from the drafts and "
    "expand where they are thin — but match the final length to the "
    "question: complete beats long. A rich, satisfying reply beats a "
    "terse one. Only truly trivial questions get short answers. No summary "
    "paragraph that repeats what you just said.\n"
    "- Sound like a person: contractions, warmth, natural sentence rhythm "
    "— never robotic list-speak.\n"
    "- Be concrete and specific; prefer an example over an abstraction.\n"
    "- If something is uncertain or the drafts leave a gap, say so "
    "plainly instead of papering over it.\n"
    "STRICT RULE: your reply must read as a direct answer to the question "
    "and nothing else — never use the words 'draft', 'version', 'model', "
    "or 'answer 1/2/3', never compare or evaluate the drafts, never "
    "explain what you merged."
)


def _looks_degenerate(text: str) -> bool:
    """Detect output that has collapsed — repetition, or token salad.

    Two distinct failures, and the second is invisible to a test for the
    first. A model that melts down under memory pressure emits fragments
    fused with hyphens and single characters from a dozen scripts
    ("own-and-and ζ,탕s-तिर-der"). Every one of those "words" is unique, so
    the repetition ratio reads 0.79 — indistinguishable from good prose.
    """
    words = text.split()
    # a CONSECUTIVE run of one word can't be diluted by healthy prose
    # around it — "hipster" x60 inside a 200-token answer slid under the
    # tail-window ratio, and "user" x42 in a 50-word answer slid under the
    # too-short bail below (both seen live). Twelve in a row is never
    # language, at any length — so this check runs before the length gate.
    run = best = 1
    for a, b in zip(words, words[1:]):
        run = run + 1 if a == b else 1
        best = max(best, run)
    if best >= 12:
        return True
    if len(words) < 60:
        return False                      # too short to judge either way
    # runaway repetition: "to make up to make up to…"
    if len(words) >= 120 and len(set(words)) / len(words) < 0.15:
        return True
    # short-burst window: the last 60 words on their own
    if len(words) >= 60:
        t60 = words[-60:]
        if len(set(t60)) / len(t60) < 0.30:
            return True
    # a collapse AFTER a healthy start hides inside the whole-text
    # average: a real three-paragraph answer followed by "party" x600
    # still scored 0.33 and streamed to a phone (seen live). The TAIL
    # tells the truth — the last 120 words of genuine prose never drop
    # below ~0.4 unique; a loop, single-word or whole-phrase, sits at
    # nearly zero.
    if len(words) >= 120:
        tail = words[-120:]
        if len(set(tail)) / len(tail) < 0.25:
            return True
    # token salad: fragments welded together with hyphens. Real prose has
    # the odd "state-of-the-art"; it does not have 25% of every word.
    if sum(1 for w in words if w.count("-") >= 2) / len(words) > 0.25:
        return True
    # token salad: characters from many scripts scattered singly through the
    # text. A genuinely multilingual answer writes whole words in each script
    # (runs of 6+ characters); salad glues one or two onto Latin fragments.
    runs = re.findall(r"[^\x00-\x7f]+", text)
    scripts = set()
    for ch in text:
        if ch.isalpha() and ord(ch) > 0x7f:
            try:
                scripts.add(unicodedata.name(ch).split()[0])
            except ValueError:
                pass
    if len(scripts) >= 3 and runs:
        if sum(len(r) for r in runs) / len(runs) < 4:
            return True
    return False


class _Degenerate(RuntimeError):
    """Raised to abandon an answer that has collapsed mid-stream."""


def _stream_guarded(label: str, msgs: list, emit, status,
                    fallback: str, note: str) -> bool:
    """Stream one model, discarding everything if the output collapses.

    The drafts in a blend are each checked before use, but the final text —
    the merge, or a research brief — used to reach the reader unchecked. It
    is watched as it arrives now, and on collapse the UI is told to throw
    away what it has shown and the known-good `fallback` replaces it.
    """
    for attempt in (1, 2):
        seen = []

        def guarded(chunk):
            seen.append(chunk)
            if len(seen) % 40 == 0 and _looks_degenerate("".join(seen)):
                raise _Degenerate
            emit(chunk)

        try:
            run_model(label, msgs, guarded)
            if _looks_degenerate("".join(seen)):
                raise _Degenerate
            return True
        except _Degenerate:
            emit(f"{NUL}RESET{NUL}")  # tells the UI to discard the garbage
            if attempt == 1:
                # collapse is often sampling luck — but MLX engines seed
                # deterministically, so an IDENTICAL retry can replay the
                # identical collapse. Nudge the prompt so the second run
                # takes a different path, and say what went wrong.
                status(f"{label} lost the thread — trying again")
                if msgs and msgs[-1].get("role") == "user":
                    nudged = dict(msgs[-1])
                    nudged["content"] = str(nudged.get("content", "")) + (
                        "\n\n(Your previous attempt collapsed into "
                        "repetition. Answer cleanly this time — plain "
                        "prose, no repeated words.)")
                    msgs = msgs[:-1] + [nudged]
                continue
            status(f"{label} lost the thread — {note}")
            if fallback is None:
                # single-model runs have no other draft to fall back on:
                # keep the part BEFORE the collapse (seen in the wild: a
                # fine answer decaying into "a walking path, …" x300)
                fallback = _detruncate("".join(seen)) or (
                    "The model lost the thread on this one — ask again, "
                    "or switch tiers for a second opinion.")
            emit(fallback)
            return False


def _detruncate(text: str) -> str:
    """Trim a collapsed output back to its still-coherent prefix."""
    t = strip_think(text)
    while t and _looks_degenerate(t):
        t = t[:int(len(t) * 0.7)].rsplit(" ", 1)[0]
    cut = max(t.rfind(". "), t.rfind(".\n"), t.rfind("!"), t.rfind("?"))
    if cut > len(t) * 0.4:
        t = t[:cut + 1]
    return t.strip()


REVISE_INSTRUCTION = (
    "Below is your own first draft answer to a question. Rewrite it into "
    "the best answer you are capable of:\n"
    "- fix anything inaccurate or vague; add the concrete specifics, "
    "names, numbers and examples the draft was missing\n"
    "- match the length to the question: tighten a simple answer to its "
    "essentials, deepen a substantial one\n"
    "- cut filler, repetition, restating of the question, and any "
    "closing summary or offer to help further\n"
    "- keep it flowing prose in a confident, natural voice\n"
    "Output ONLY the improved answer itself. Never begin with a preamble "
    "like 'Here is the improved answer' or 'Here's a rewritten version' — "
    "start directly with the substance.\n\n")

_GREETING_RE = re.compile(
    r"^(hi|hey|hello|yo|sup|thanks|thank you|ok|okay|cool|nice|lol|"
    r"good (morning|afternoon|evening|night))\b[\s!.?]*$", re.I)


def _is_substantive(prompt: str) -> bool:
    """Worth a second pass? Greetings and one-liners are not."""
    p = (prompt or "").strip()
    if len(p) < 12 or _GREETING_RE.match(p):
        return False
    return len(p.split()) >= 3


PEER_INSTRUCTION = (
    "Below are several draft answers to the same question, plus the "
    "question itself. Write YOUR OWN single best answer: take the "
    "strongest material from every draft, correct anything wrong, fill "
    "the gaps the drafts missed, and write it as one coherent reply. "
    "Never mention the drafts or this process.\n\n")


def run_council(labels: list, messages: list, emit, status,
                reflect: bool = False, peer: bool = False) -> None:
    """Ask each selected model in turn, then stream a merged answer.

    Sequential on purpose: only one MLX engine can be resident at a time
    (each pins its whole model in RAM), so parallel calls would thrash.
    """
    # skip models that can't actually answer: not downloaded (their weights
    # aren't on disk) or too big for current free RAM (OOM-killed mid-load)
    usable, skipped = [], []
    for l in labels:
        if not model_cached(l):
            skipped.append(l + " (not downloaded)")
        elif not model_fits_memory(l):
            skipped.append(l + " (low memory)")
        else:
            usable.append(l)
    if skipped and usable:
        status("skipping " + ", ".join(skipped))
        time.sleep(1.2)  # let the notice be seen before it's replaced
    # sequential generation — cap the roster so a run stays minutes, not hours
    labels = (usable or labels[:1])[:12]

    drafts = []

    def took_part(label, text):
        """Record a draft and show it. Blending is the whole point of these
        modes, and until now its only visible trace was a status line."""
        drafts.append((label, text))
        try:
            emit(NUL + "DRAFT:" +
                 json.dumps({"m": label, "t": text[:1200]}) + NUL)
        except Exception:
            pass          # never let the display break the answer

    for i, label in enumerate(labels, 1):
        # free RAM drops as each engine loads — re-check before committing
        if i > 1 and not model_fits_memory(label):
            took_part(label, "(no answer — low memory)")
            continue
        status(f"asking {label} · {i} of {len(labels)}")
        parts = []
        try:
            run_model(label, messages, parts.append,
                      thinking=(reflect and label.startswith("Qwen")))
        except Exception as exc:
            took_part(label, f"(no answer — {type(exc).__name__})")
            continue
        # the merger gets answers, never the reasoning that produced them
        text = strip_think("".join(parts))
        if _looks_degenerate(text):
            # a runaway repetition loop would poison the merge prompt
            took_part(label, "(no answer — degenerate output)")
            continue
        if text:
            took_part(label, text)
        else:
            # an engine that returns NOTHING is left out of the blend, but
            # recorded — the contributor count must never lie
            took_part(label, "(no answer — empty)")

    good = [d for d in drafts if not d[1].startswith("(no answer")]
    if not good:
        raise RuntimeError("none of the selected models answered")
    if len(good) == 1:
        emit(good[0][1])  # only one survived — nothing to merge
        return

    # PEER REVIEW (Power mode, per Patrick): every contributor reads ALL
    # the drafts and rewrites its own best answer from them; Gemma then
    # merges the rewrites. Twice the engine passes — the mode that says
    # "take as long as you need, give me your best".
    if peer and len(good) >= 2:
        question0 = messages[-1]["content"] if messages else ""
        block = "\n\n".join(f"[draft {n}]\n{t[:1200]}"
                             for n, (_l, t) in enumerate(good[:5], 1))
        reviews = []
        for i, (label, _t) in enumerate(good, 1):
            if not model_fits_memory(label):
                continue
            status(f"peer review: {label} rewriting · {i} of {len(good)}")
            parts = []
            try:
                run_model(label, [messages[0],
                                  {"role": "user",
                                   "content": PEER_INSTRUCTION
                                   + "QUESTION: " + question0
                                   + "\n\n" + block}], parts.append)
            except Exception:
                continue
            text = strip_think("".join(parts))
            if text and not _looks_degenerate(text) and len(text) > 200:
                reviews.append((label, text))
                try:
                    emit(NUL + "DRAFT:" + json.dumps(
                        {"m": label + " (rewrite)", "t": text[:1200]}) + NUL)
                except Exception:
                    pass
        if len(reviews) >= 2:
            good = reviews

    # Gemma writes the final answer whenever it's on this machine and fits;
    # otherwise fall back to the strongest answering model that fits
    answered = [l for l, _t in good]
    merger = next((l for l in MERGE_RANK
                   if l in answered and model_fits_memory(l)), answered[0])
    # Gemma writes the merge; prefer the newest generation that's installed
    for pref in ("Gemma 4 12B", "Gemma 4 26B", "Gemma 2 9B IT"):
        if model_cached(pref) and model_fits_memory(pref):
            merger = pref
            break

    # feed the merger only the strongest few answers, each truncated:
    # an unbounded merge prompt overflows small models' context and sends
    # them into repetition loops (seen in the wild with 8 full drafts)
    rank = {l: i for i, l in enumerate(MERGE_RANK)}
    good.sort(key=lambda d: rank.get(d[0], 99))
    good = good[:5]

    status(f"{merger} is combining {len(good)} answers")
    question = messages[-1]["content"] if messages else ""
    body = "\n\n".join(f"[answer {n}]\n{t[:1500]}"
                       for n, (_l, t) in enumerate(good, 1))

    # REFLECTION (Thinking tier): before writing the final answer, the
    # merger reads the drafts as a critic and lists concrete problems —
    # wrong facts, gaps, waffle — and the synthesis prompt then carries
    # those notes. Critique-then-revise reliably beats a straight merge;
    # blending alone regresses toward the average draft. Best-effort:
    # any failure just means merging without notes.
    notes = ""
    if reflect and len(good) > 1:
        status(f"{merger} is double-checking the drafts")
        try:
            parts = []
            run_model(merger, [
                messages[0],
                {"role": "user", "content":
                 "You are reviewing draft answers before a final version "
                 "is written. List, tersely, the concrete problems to fix: "
                 "factual claims that look wrong or contradict each other, "
                 "missing angles a good answer needs, and filler to drop. "
                 "At most 6 bullet points, no praise, no rewrite.\n\n"
                 f"QUESTION: {question}\n\n{body}"}], parts.append)
            notes = strip_think("".join(parts)).strip()[:1200]
        except Exception:
            notes = ""

    synth = [
        messages[0],  # keep the dated system prompt
        {"role": "user",
         "content": f"{SYNTH_INSTRUCTION}\n\nQUESTION: {question}\n\n{body}"
                    + (f"\n\nA careful reviewer flagged these issues — "
                       f"your final answer must fix them without "
                       f"mentioning the review:\n{notes}" if notes else "")},
    ]
    # The drafts were each checked, but the merge never was — so a merger
    # that melted down streamed its collapse straight to the reader with
    # nothing in the way. Watch it as it arrives, and if it goes, throw away
    # what was shown and fall back to the best draft we already trust.
    # And if the merge stage ITSELF raises (engine died between drafts and
    # merge — seen in the wild as "engine returned nothing" after 3 good
    # drafts), the best draft still ships: with good answers in hand there
    # is no failure mode where the user gets nothing.
    try:
        _stream_guarded(merger, synth, emit, status, good[0][1],
                        "showing the best single answer")
    except Exception:
        try:
            emit(NUL + "RESET" + NUL)
            emit(good[0][1])
        except Exception:
            pass


RESEARCH_PLAN = (
    "Break this question into 2 short web search queries that cover "
    "different angles of it.\n"
    "Copy any product name, version number, place or date EXACTLY as "
    "written. Never replace a term with one you consider more familiar — if "
    "something looks unfamiliar to you it is probably newer than you are, "
    "and the search will find it.\n"
    "Reply with ONLY the queries, one per line — no numbering, no quotes, "
    "no commentary.\n\nQUESTION: ")

RESEARCH_WRITE = (
    "Write a short research brief answering the question, using ONLY the "
    "numbered sources below. Cite them inline as [1], [2] and so on, matching "
    "the numbers exactly as given. Lead with the answer, then the supporting "
    "detail. If the sources disagree or don't cover something, say so plainly "
    "rather than filling the gap. Never invent a fact or a source.")


def _plan_queries(label: str, question: str, status) -> list:
    """Ask the model what to search for. Falls back to the raw question."""
    status("planning the research")
    parts = []
    try:
        run_model(label, [{"role": "user",
                           "content": RESEARCH_PLAN + question}],
                  parts.append)
    except Exception:
        return [question]
    out = strip_think(strip_special("".join(parts)))
    lines = [re.sub(r'^[\s\d\.\)\-\*"]+', "", ln).strip(' "\'*')
             for ln in out.splitlines()]
    return [ln for ln in lines if 6 < len(ln) < 120][:2]


def run_research(labels: list, messages: list, emit, status) -> None:
    """Plan several searches, run them, then write a brief that cites them.

    One model does the whole run — planning and writing — so there is only
    ever a single engine load, which on MLX is the expensive part.
    """
    if not HAS_SEARCH:
        raise RuntimeError(
            "Research needs web search — install it with: pip install ddgs")
    question = messages[-1]["content"] if messages else ""
    usable = [l for l in labels
              if model_cached(l) and model_fits_memory(l)]
    if not usable:
        raise RuntimeError("no model is available to research with")
    rank = {l: i for i, l in enumerate(MERGE_RANK)}
    writer = min(usable, key=lambda l: rank.get(l, 99))

    # The user's own words always go first. A local model's knowledge stops
    # years before the question often does — asked about "macOS 26 Tahoe" it
    # planned searches for "macOS Monterey", a version it recognised, and
    # researched the wrong OS end to end. Searching verbatim first means the
    # planner can only ever add angles, never quietly replace the subject.
    queries = [question[:120]]
    for q in _plan_queries(writer, question, status):
        if q.lower() not in (x.lower() for x in queries):
            queries.append(q)

    sources, seen = [], set()
    for i, q in enumerate(queries, 1):
        status(f"searching {i} of {len(queries)} — {q}")
        for r in search_results(q):
            # the same page often surfaces for several queries
            if r["url"] and r["body"] and r["url"] not in seen:
                seen.add(r["url"])
                sources.append(r)
    if not sources:
        raise RuntimeError(
            "the searches came back empty — check the network connection")
    sources = sources[:12]

    # READ the top pages, don't just skim their snippets — fetched in
    # parallel, snippet kept whenever a page won't give up its text
    status("reading the top sources")
    def _enrich(s):
        text = _page_text(s["url"])
        if len(text) > 300:
            s["body"] = text
    threads = [threading.Thread(target=_enrich, args=(s,))
               for s in sources[:5]]
    for t_ in threads:
        t_.start()
    for t_ in threads:
        t_.join(timeout=9)

    block = "\n\n".join(
        f"[{n}] {s['title']}\n{s['body'][:2200]}"
        for n, s in enumerate(sources, 1))
    brief = [messages[0],
             {"role": "user",
              "content": f"{RESEARCH_WRITE}\n\nQUESTION: {question}\n\n"
                         f"SOURCES:\n{block}"}]

    status(f"{writer} is writing from {len(sources)} sources")
    # if the brief collapses, the raw snippets are still worth more than
    # nothing — they are what the answer would have been drawn from
    plain = "\n".join(f"- **{s['title'][:90]}** — {s['body'][:220]}"
                      for s in sources[:5])
    _stream_guarded(writer, brief, emit, status, plain,
                    "showing the raw findings instead")

    emit("\n\n**Sources**\n" + "\n".join(
        f"{n}. [{(s['title'] or s['url'])[:90]}]({s['url']})"
        for n, s in enumerate(sources, 1)))


def offline_hint(kind: str, err: Exception) -> str:
    """Turn a backend error into an actually useful message."""
    # NB: HTTPError subclasses URLError — it MUST be checked first,
    # and its body usually contains the engine's real explanation.
    if isinstance(err, urllib.error.HTTPError):
        detail = ""
        try:
            body = getattr(err, "cached_body", None)
            if body is None:
                body = err.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error", "") or body
            except json.JSONDecodeError:
                detail = body
        except Exception:
            pass
        detail = detail.strip()[:500]
        if kind == "ollama" and err.code == 404:
            return ("⚠️ Ollama is running but that model isn't pulled.\n\n"
                    f"`{detail or 'model not found'}`\n\n"
                    "Run `ollama pull <model>` and try again.")
        return (f"⚠️ The engine rejected the request (HTTP {err.code}).\n\n"
                + (f"It says: **{detail}**" if detail
                   else "No details were provided."))
    if isinstance(err, urllib.error.URLError):
        if kind == "ollama":
            return ("⚠️ Ollama isn't reachable on port 11434.\n\n"
                    "Start it with `ollama serve`, and make sure the model is "
                    "pulled (`ollama pull <model>`).")
        return ("⚠️ No MLX server answering on that port.\n\n"
                "Launch it first, e.g. `mlx_lm.server --model <model> "
                "--port <port>`.")
    # a model too big for RAM gets SIGKILLed mid-load; the engine reports
    # this as a terminated helper or a truncated stream
    text = str(err).lower()
    if any(s in text for s in ("signal: killed", "unexpected eof",
                               "process has terminated")):
        return ("⚠️ This model ran out of memory and the engine stopped it.\n\n"
                "It needs more free RAM than this Mac has right now. Close "
                "some apps and retry, or pick a smaller model — the ones at "
                "the top of the sidebar are much lighter.")
    return f"⚠️ Backend error — {type(err).__name__}: {err}"


# ------------------------------------------------------------ skyline cache
# The Apple aerials CANNOT be streamed straight to a browser: the phobos
# host is http-only (mixed-content-blocked on the https tunnel, broken TLS
# cert) and the sylvan AVC files put their moov atom AFTER 370 MB of mdat,
# so a browser has nothing to play until the entire file arrives — that is
# exactly the "background never loads" bug. So MillenAI serves the skyline
# itself: download once, remux fast-start IN PURE PYTHON (move moov ahead
# of mdat, shift every stco/co64 chunk offset by the moov size), cache in
# app_dir()/sky, and stream same-origin with Range support. One path that
# works in the app, on the tunnel, and in every browser.
SKY_SOURCES = [
    # The COMPLETE Apple aerial catalog (89 clips: cities, ISS space
    # flyovers, underwater) from resources-13.tar entries.json — every
    # url-1080-H264 on sylvan. Clips download lazily on first pick and the
    # cache is LRU-capped, so the list's size costs nothing up front.
    "https://sylvan.apple.com/Videos/SE_A016_C009_SDR_20190717_3m30s_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A114_C001_0305OT_v10_SDR_FINAL_22062018_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GL_G002_C002_PSNK_v03_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT026_363A_103NC_E1027_KOREA_JAPAN_NIGHT_v18_SDR_PS_20180907_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/PA_A001_C007_SDR_20190717_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_LA_A008_C004_ALTB_ED_FROM_FLAME_RETIME_v46_SDR_PS_20180917_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/g201_WH_D004_L014_SDR_20191031_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_L007_C007_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT312_162NC_139M_1041_AFRICA_NIGHT_v14_SDR_FINAL_20180706_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_CH_C002_C005_PSNK_v05_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GL_G004_C010_PSNK_v04_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/PA_A004_C003_SDR_20190719_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_LA_A005_C009_PSNK_ALT_v09_SDR_PS_201809134_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A009_C001_010181A_v09_SDR_PS_FINAL_20180725_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/DL_B002_C011_SDR_20191122_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_LA_A011_C003_DGRN_LNFIX_STAB_v57_SDR_PS_20181002_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/KP_A010_C002_SDR_20190717_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_N013_C004_PS_v01_SDR_PS_20180925_F1970F7193_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT329_113NC_396B_1105_ITALY_v03_SDR_FINAL_20180706_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/g201_TH_803_A001_8_SDR_20191031_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_L012_c002_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A105_C002_v06_SDR_FINAL_25062018_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A013_C012_0122D6_CC_v01_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/RS_A008_C010_SDR_20191218_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_CH_C007_C004_PSNK_v02_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT314_139M_170NC_NORTH_AMERICA_AURORA__COMP_v22_SDR_20181206_v12CC_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_LA_A006_C008_PSNK_ALL_LOGOS_v10_SDR_PS_FINAL_20180801_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/BO_A018_C029_SDR_20190812_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_DB_D011_C010_PSNK_DENOISE_v19_SDR_PS_20180914_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A001_C004_1207W5_v23_SDR_FINAL_20180706_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A083_C002_1130KZ_v04_SDR_PS_FINAL_20180725_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/PA_A002_C009_SDR_20190730_ALT01_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT308_139K_142NC_CARIBBEAN_DAY_v09_SDR_FINAL_22062018_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_HK_B005_C011_PSNK_v16_SDR_PS_20180914_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GL_G010_C006_PSNK_NOSUN_v12_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT306_139NC_139J_3066_CALI_TO_VEGAS_v08_SDR_PS_20180824_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/MEX_A006_C008_SDR_20190923_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_N008_C009_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A015_C018_0128ZS_v03_SDR_PS_FINAL_20180709__SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/g201_TH_804_A001_8_SDR_20191031_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_LA_A006_C004_v01_SDR_FINAL_PS_20180730_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_H004_C009_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/g201_CA_A016_C002_SDR_20191114_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A108_C001_v09_SDR_FINAL_22062018_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT329_117NC_401C_1037_IRELAND_TO_ASIA_v48_SDR_PS_FINAL_20180725_F0F6300_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_DB_D008_C010_PSNK_v21_SDR_PS_20180914_F0F16157_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_C003_C003_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/g201_AK_A003_C014_SDR_20191113_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_H012_C009_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_H005_C012_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT329_113NC_396B_1105_CHINA_v04_SDR_FINAL_20180706_F900F2700_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_LA_A009_C009_PSNK_v02_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/FK_U009_C004_SDR_20191220_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A050_C004_1027V8_v16_SDR_FINAL_20180706_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_DB_D002_C003_PSNK_v04_SDR_PS_20180914_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_CH_C007_C011_PSNK_v02_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/BO_A012_C031_SDR_20190726_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A105_C003_0212CT_FLARE_v10_SDR_PS_FINAL_20180711_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_LW_L001_C003__PSNK_DENOISE_v04_SDR_PS_FINAL_20180803_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_HK_H004_C010_PSNK_v08_SDR_PS_20181009_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A351_C001_v06_SDR_PS_20180725_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/CR_A009_C007_SDR_20191113_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A001_C001_120530_v04_SDR_FINAL_20180706_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A006_C003_1219EE_CC_v01_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_DB_D001_C001_PSNK_v06_SDR_PS_20180824_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/BO_A014_C023_SDR_20190717_F240F3709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_N008_C003_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT110_112NC_364D_1054_AURORA_ANTARTICA__COMP_FINAL_v34_PS_SDR_20181107_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_L010_C006_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_1223LV_FLARE_v21_SDR_PS_FINAL_20180709_F0F5700_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A103_C002_0205DG_v12_SDR_FINAL_20180706_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/PA_A010_C007_SDR_20190717_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_N003_C006_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_C001_C005_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A008_C007_011550_CC_v01_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT307_136NC_134K_8277_NY_NIGHT_01_v25_SDR_PS_20180907_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/BO_A014_C008_SDR_20190719_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_L004_C011_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_H004_C007_PS_v02_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_HK_H004_C013_t9_6M_HB_tag0.mov",
    "https://sylvan.apple.com/Videos/comp_H007_C003_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_DB_D001_C005_COMP_PSNK_v12_SDR_PS_20180912_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A012_C014_1223PT_v53_SDR_PS_FINAL_20180709_F0F8700_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/AK_A004_C012_SDR_20191217_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_HK_H004_C008_PSNK_v19_SDR_PS_20180914_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_A007_C017_01156B_v02_SDR_PS_20180925_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_LW_L001_C006_PSNK_DENOISE_v02_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT060_117NC_363D_1034_AUSTRALIA_v35_SDR_PS_FINAL_20180731_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_C004_C003_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov",
]

# Clips that read DARK (Apple's own labels: the Night city passes, the
# aurora, and the deep-ocean dives). After 7pm local the backdrop picker
# prefers these; in daylight it avoids them.
SKY_DARK = [0, 3, 4, 6, 8, 11, 14, 16, 23, 25, 27, 31, 36, 42, 47, 52,
            56, 61, 65, 71, 75, 76, 83]

_sky_lock = threading.Lock()
_last_seen = {}          # identity -> last request ts, for the user count
_sky_jobs = {}          # idx -> {"status": ..., "pct": int}


def _sky_dir() -> str:
    return os.path.join(app_dir(), "sky")


def _sky_path(i: int) -> str:
    # keyed by URL hash, not list index — editing SKY_SOURCES must never
    # make a cached file impersonate a different clip
    h = hashlib.sha1(SKY_SOURCES[i].encode()).hexdigest()[:10]
    return os.path.join(_sky_dir(), "sky-%s.mov" % h)


def _atoms(fh, end):
    """Top-level QuickTime atoms as (type, offset, size)."""
    off = fh.tell()
    while off + 8 <= end:
        fh.seek(off)
        hdr = fh.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        if size == 1:
            size = struct.unpack(">Q", fh.read(8))[0]
        elif size == 0:
            size = end - off
        if size < 8:
            return
        yield typ, off, size
        off += size


def _patch_moov(buf: bytearray, shift: int):
    """Shift every stco/co64 chunk offset inside a moov blob by `shift`.
    Recursive descent over the real container atoms — a naive byte scan
    for b'stco' can hit sample data and corrupt the file."""
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl",
                  b"edts", b"udta"}

    def walk(start, end):
        off = start
        while off + 8 <= end:
            size, typ = struct.unpack(">I4s", buf[off:off + 8])
            hs = 8
            if size == 1:
                size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
                hs = 16
            if size < hs or off + size > end:
                return
            if typ in containers:
                walk(off + hs, off + size)
            elif typ in (b"stco", b"co64"):
                n = struct.unpack(">I", buf[off + hs + 4:off + hs + 8])[0]
                base = off + hs + 8
                w = 4 if typ == b"stco" else 8
                fmt = ">I" if typ == b"stco" else ">Q"
                for k in range(n):
                    p = base + w * k
                    v = struct.unpack(fmt, buf[p:p + w])[0] + shift
                    buf[p:p + w] = struct.pack(fmt, v)
            off += size

    walk(8, len(buf))


def _faststart(src: str, dst: str):
    """qt-faststart: rewrite `src` so moov precedes mdat, into `dst`."""
    total = os.path.getsize(src)
    with open(src, "rb") as fh:
        atoms = list(_atoms(fh, total))
        moov = next(((o, s) for t, o, s in atoms if t == b"moov"), None)
        mdat = next(((o, s) for t, o, s in atoms if t == b"mdat"), None)
        if not moov or not mdat:
            raise ValueError("no moov/mdat atom")
        if moov[0] < mdat[0]:                     # already fast-start
            os.replace(src, dst)
            return
        fh.seek(moov[0])
        blob = bytearray(fh.read(moov[1]))
        if b"cmov" in blob[:256]:
            raise ValueError("compressed moov unsupported")
        # every atom after ftyp moves back by exactly len(moov)
        _patch_moov(blob, moov[1])
        with open(dst + ".part", "wb") as out:
            for typ, off, size in atoms:          # ftyp keeps pole position
                if typ == b"ftyp":
                    fh.seek(off)
                    out.write(fh.read(size))
            out.write(blob)
            for typ, off, size in atoms:
                if typ in (b"ftyp", b"moov"):
                    continue
                fh.seek(off)
                left = size
                while left:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    out.write(chunk)
                    left -= len(chunk)
    os.replace(dst + ".part", dst)
    os.remove(src)


def _sky_fetch(i: int):
    tmp = _sky_path(i) + ".dl"
    try:
        os.makedirs(_sky_dir(), exist_ok=True)
        req = urllib.request.Request(SKY_SOURCES[i],
                                     headers={"User-Agent": "MillenAI"})
        with urllib.request.urlopen(req, timeout=60) as r, \
                open(tmp, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                with _sky_lock:
                    _sky_jobs[i] = {
                        "status": "downloading",
                        "pct": int(got * 92 / total) if total else 0}
        with _sky_lock:
            _sky_jobs[i] = {"status": "remuxing", "pct": 96}
        _faststart(tmp, _sky_path(i))
        with _sky_lock:
            _sky_jobs[i] = {"status": "ready", "pct": 100}
        # LRU cap: 89 clips at ~220 MB each must never all land on disk —
        # keep the 12 most recently played (~2.6 GB), drop the rest. Six
        # was too few once rotation returned: the picker only chooses
        # from cache, so the same handful cycled forever (seen live).
        try:
            clips = sorted(glob.glob(os.path.join(_sky_dir(), "sky*.mov")),
                           key=os.path.getmtime)
            for old in clips[:-12]:
                os.remove(old)
        except Exception:
            pass
    except Exception as exc:
        with _sky_lock:
            _sky_jobs[i] = {"status": "error", "pct": 0,
                            "note": str(exc)[:120]}
        try:
            os.remove(tmp)
        except Exception:
            pass


def sky_status(i: int, warm: bool = False) -> dict:
    if not 0 <= i < len(SKY_SOURCES):
        return {"status": "error", "pct": 0, "note": "no such clip"}
    if os.path.exists(_sky_path(i)):
        return {"status": "ready", "pct": 100}
    with _sky_lock:
        job = _sky_jobs.get(i)
        if job and job.get("status") != "error":
            return dict(job)
        # ONE download at a time: several launches/refreshes each kicking a
        # 400 MB prewarm saturated the line and made everything feel slow.
        # A background warm never starts while anything else is fetching;
        # only a clip the user is actually waiting on may jump the queue.
        busy = any(j.get("status") in ("downloading", "remuxing")
                   for j in _sky_jobs.values())
        if warm and busy:
            return {"status": "busy", "pct": 0}
        _sky_jobs[i] = {"status": "downloading", "pct": 0}
    threading.Thread(target=_sky_fetch, args=(i,), daemon=True).start()
    return {"status": "downloading", "pct": 0}


# ---------------------------------------------------------------- sign-in
# Remote visitors (identified by the tunnel's Cf-Connecting-Ip /
# X-Forwarded-For headers — local requests never carry them) must pick an
# identity after the key gate: name+PIN, or Google when configured. The
# identity is a salted hash, the cookie carries it, and all chats, memory
# and prefs live under app_dir()/users/<id>/. A wrong PIN is simply a
# different (empty) profile — nobody can open someone else's.
GOOGLE_OAUTH_FILE = os.path.join(app_dir(), "google_oauth.json")
_oauth_states = {}         # state -> issued-at, for CSRF protection


def google_conf():
    try:
        with open(GOOGLE_OAUTH_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("client_id") and d.get("client_secret"):
            return d
    except Exception:
        pass
    return None


def _user_id(kind: str, ident: str) -> str:
    return hashlib.sha256(("millen:" + kind + ":" + ident)
                          .encode("utf-8")).hexdigest()[:20]


# OWNER ACCESS: the machine's owner can reach their REAL chats/memory
# remotely — sign in with the PIN stored in app_dir()/owner_pin (any
# name), and the identity maps to the legacy files instead of a walled
# web profile. The file is 0600 and never committed; delete it to turn
# owner access off. Admin endpoints stay owner-only-at-the-machine.
OWNER_PIN_FILE = os.path.join(app_dir(), "owner_pin")


def owner_uid():
    try:
        pin = open(OWNER_PIN_FILE).read().strip()
        if re.fullmatch(r"\d{8,12}", pin):
            return _user_id("owner", pin)
    except Exception:
        pass
    return None


WELCOME_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>MillenAI — sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
html,body{height:100%;margin:0;overflow:hidden}
body{background:#07080c;color:#ececec;display:flex;align-items:center;
  justify-content:center;
  font-family:'Space Grotesk','Helvetica Neue',system-ui,sans-serif}
/* the same living backdrop the app runs, behind the door */
#sky{position:fixed;inset:0;z-index:0;overflow:hidden;opacity:0;
  transition:opacity 2.4s ease}
#sky.on{opacity:1}
#sky video{width:100%;height:100%;object-fit:cover;
  transform:scale(1.06);filter:brightness(.5) saturate(1.1)}
#veil{position:fixed;inset:0;z-index:1;pointer-events:none;
  background:radial-gradient(120% 90% at 50% 40%,
    rgba(7,8,12,.15) 0%,rgba(7,8,12,.72) 60%,rgba(7,8,12,.94) 100%)}
#motes{position:fixed;inset:0;z-index:2;pointer-events:none}
.door{position:relative;z-index:3;text-align:center;padding:24px;
  max-width:430px;width:100%;
  animation:doorIn 1.5s cubic-bezier(.16,1,.3,1) both}
@keyframes doorIn{
  from{opacity:0;transform:translateY(26px) scale(.97);filter:blur(9px)}
  to{opacity:1;transform:none;filter:blur(0)}}
.wrap{position:relative;display:inline-block;margin:0 0 10px}
h1{font-size:clamp(44px,9vw,74px);letter-spacing:.01em;margin:0;
  font-weight:700;line-height:1.05;
  background:linear-gradient(90deg,#ff8f8f,#ffc46e,#f5e663,#7ef0a6,
             #6ec7ff,#8f9dff,#c98fff,#ff8fd8,#ff8f8f);
  background-size:200% 100%;
  -webkit-background-clip:text;background-clip:text;color:transparent;
  -webkit-text-fill-color:transparent;
  animation:rainbow 16s linear infinite}
/* the tube's halo — a blurred twin behind the letters */
.halo{position:absolute;left:0;top:0;z-index:-1;pointer-events:none;
  filter:blur(20px) saturate(1.5);opacity:.9}
.halo h1{animation:rainbow 16s linear infinite}
@keyframes rainbow{from{background-position:0% 50%}
                   to{background-position:200% 50%}}
p.tag{font-family:ui-serif,Georgia,serif;font-size:19px;font-weight:400;
  color:#d9d6cc;margin:6px 0 26px;line-height:1.5}
.err{color:#e26d5a;min-height:20px;margin:12px 0 0;font-size:13.5px}
input{background:rgba(18,20,26,.55);border:1px solid rgba(255,255,255,.14);
  border-radius:14px;color:#ececec;font-size:16px;padding:14px 16px;
  width:100%;box-sizing:border-box;outline:none;text-align:center;
  margin-bottom:11px;letter-spacing:.04em;
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
  transition:border-color .25s,box-shadow .25s,background .25s}
input::placeholder{color:#7e8390}
input:focus{border-color:rgba(143,157,255,.75);background:rgba(18,20,26,.75);
  box-shadow:0 0 0 4px rgba(143,157,255,.12),
             0 10px 40px -12px rgba(143,157,255,.5)}
button{position:relative;overflow:hidden;background:#ececec;color:#111;
  border:0;border-radius:14px;font-size:15px;font-weight:700;
  padding:14px 22px;cursor:pointer;width:100%;letter-spacing:.02em;
  transition:transform .16s ease,box-shadow .25s ease}
button:hover{transform:translateY(-1px);
  box-shadow:0 12px 34px -14px rgba(255,255,255,.75)}
button:active{transform:translateY(0)}
/* light sweeps across the button, endlessly */
button::after{content:"";position:absolute;inset:0;
  background:linear-gradient(105deg,transparent 38%,
    rgba(255,255,255,.75) 50%,transparent 62%);
  transform:translateX(-120%);animation:sweep 4.5s ease-in-out infinite}
@keyframes sweep{0%,55%{transform:translateX(-120%)}
                 85%,100%{transform:translateX(120%)}}
.gbtn{display:__GOOGLE_DISPLAY__;margin-top:13px;
  background:rgba(18,20,26,.55);color:#ececec;
  border:1px solid rgba(255,255,255,.14);text-decoration:none;
  border-radius:14px;font-size:15px;font-weight:600;padding:14px 22px;
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
  transition:border-color .25s,background .25s}
.gbtn:hover{border-color:rgba(143,157,255,.7);background:rgba(24,27,36,.8)}
.small{margin-top:20px;font-family:'IBM Plex Mono',monospace;
  font-size:11px;color:#6e727c;line-height:1.7;letter-spacing:.02em}
@media(prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important}}
</style></head><body>
<div id="sky"><video id="skyv" muted loop playsinline></video></div>
<div id="veil"></div>
<canvas id="motes"></canvas>
<div class="door">
  <div class="wrap">
    <div class="halo" aria-hidden="true"><h1>MillenAI</h1></div>
    <h1>MillenAI</h1>
  </div>
  <p class="tag">Pick a name and a PIN.<br>Your chats stay yours.</p>
  <form onsubmit="go();return false">
    <input id="n" autocomplete="off" maxlength="24" placeholder="your name"
           autofocus>
    <input id="p" type="password" autocomplete="off" maxlength="12"
           inputmode="numeric" placeholder="PIN (8+ digits)">
    <button>Continue</button>
  </form>
  <a class="gbtn" href="/auth/google">Continue with Google</a>
  <div class="err" id="e"></div>
  <div class="small">same name + PIN = same chats, on any device.<br>
       a different PIN opens a different, empty profile.</div>
</div>
<script>
// DRIFTING MOTES: slow points of light rising through the scene — the
// calm cousin of the app's warp
(function(){
  const c=document.getElementById("motes"),x=c.getContext("2d");
  let w,h,ps=[];
  function size(){
    const d=Math.min(devicePixelRatio||1,2);
    w=c.width=innerWidth*d;h=c.height=innerHeight*d;
    c.style.width=innerWidth+"px";c.style.height=innerHeight+"px";
    ps=Array.from({length:64},()=>({
      x:Math.random()*w,y:Math.random()*h,
      r:(Math.random()*1.6+.4)*d,
      v:(Math.random()*.22+.05)*d,a:Math.random()*.5+.15,
      t:Math.random()*6.28}));
  }
  size();addEventListener("resize",size);
  (function tick(){
    requestAnimationFrame(tick);
    x.clearRect(0,0,w,h);
    for(const p of ps){
      p.y-=p.v;p.t+=.008;
      if(p.y<-6){p.y=h+6;p.x=Math.random()*w;}
      const tw=p.a*(0.65+0.35*Math.sin(p.t));
      x.beginPath();x.arc(p.x+Math.sin(p.t)*6,p.y,p.r,0,6.283);
      x.fillStyle="rgba(200,214,255,"+tw.toFixed(3)+")";x.fill();
    }
  })();
})();
// the backdrop: whatever clip is already cached, so the door opens on a
// living scene without ever making a visitor wait for a download
(async function(){
  try{
    const c=await(await fetch("/api/sky/cached")).json();
    const list=c.cached||[];
    if(!list.length)return;
    const i=list[Math.floor(Math.random()*list.length)];
    const v=document.getElementById("skyv");
    const start=()=>{const pr=v.play();if(pr&&pr.catch)pr.catch(()=>{});};
    v.addEventListener("canplaythrough",()=>{
      document.getElementById("sky").classList.add("on");
      start();
      // some browsers refuse muted autoplay until the visitor touches
      // something — the first interaction starts the motion
      ["pointerdown","keydown"].forEach(ev=>
        addEventListener(ev,start,{once:true}));
    },{once:true});
    v.src="/sky/"+i+".mov";
  }catch(e){}
})();
function go(){
  const n=document.getElementById("n").value.trim();
  const p=document.getElementById("p").value.trim();
  const e=document.getElementById("e");
  if(n.length<2){e.textContent="pick a name (2+ characters)";return;}
  if(!/^[0-9]{8,12}$/.test(p)){e.textContent="PIN must be 8-12 digits";return;}
  fetch("/api/welcome",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name:n,pin:p})})
    .then(r=>r.json())
    .then(d=>{if(d.ok)location.reload();
              else e.textContent=d.err||"try again";})
    .catch(()=>{e.textContent="network error — try again";});
}
</script></body></html>"""


# The DOOR: what the bare public URL shows a browser with no cookie. Kept
# self-contained (inline styles, system fonts, no assets) so it renders
# instantly from anywhere — its whole job is one input box.
GATE_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>MillenAI</title>
<style>
html,body{height:100%;margin:0}
body{background:#0f1117;color:#ececec;display:flex;align-items:center;
  justify-content:center;font-family:'Helvetica Neue',system-ui,sans-serif}
.door{text-align:center;padding:24px}
h1{font-size:clamp(44px,9vw,76px);letter-spacing:.06em;margin:0 0 6px;
  font-weight:700;
  background:linear-gradient(90deg,#ff8f8f,#ffc46e,#f5e663,#7ef0a6,
             #6ec7ff,#8f9dff,#c98fff,#ff8fd8);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  filter:drop-shadow(0 0 22px rgba(140,150,255,.25))}
p{color:#8e8e8e;margin:0 0 26px;font-size:15px}
.err{color:#e26d5a;min-height:20px;margin:12px 0 0;font-size:14px}
form{display:flex;gap:10px;justify-content:center}
input{background:#171717;border:1px solid #3d3d3d;border-radius:12px;
  color:#ececec;font-size:16px;padding:13px 16px;width:min(320px,60vw);
  outline:none;text-align:center;letter-spacing:.08em}
input:focus{border-color:#8f9dff}
button{background:#ececec;color:#111;border:0;border-radius:12px;
  font-size:15px;font-weight:600;padding:13px 22px;cursor:pointer}
button:hover{background:#fff}
</style></head><body>
<div class="door">
  <h1>MillenAI</h1>
  <p>private &middot; enter your access key</p>
  <form onsubmit="location.href='/?key='+encodeURIComponent(
      document.getElementById('k').value.trim());return false">
    <input id="k" type="password" autocomplete="off" autofocus
           placeholder="access key">
    <button>Enter</button>
  </form>
  <div class="err">__GATE_NOTE__</div>
</div>
</body></html>"""


class StudioHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # lets us stream then close, no chunking

    def log_message(self, *args):
        pass

    def _gate(self):
        """True = let the request through; False = already answered it.

        The access-key door is RETIRED, per Patrick: the welcome screen
        (account + PIN, Google SSO when configured) is the front door now.
        Old /?key=... links simply land on the app; the admin lockdown and
        per-identity storage below are what actually protect the host."""
        return True
        if not ACCESS_KEY:
            return True
        cookie = self.headers.get("Cookie", "") or ""
        m = re.search(r"millen_key=([^;\s]+)", cookie)
        # compare_digest: a plain == leaks how many leading characters
        # matched through response timing — slow to exploit over a tunnel,
        # free to prevent
        if m and secrets.compare_digest(m.group(1), ACCESS_KEY):
            return True
        wrong = False
        if self.path.startswith("/?key="):
            if secrets.compare_digest(self.path[len("/?key="):],
                                      ACCESS_KEY):
                self.send_response(302)
                self.send_header("Set-Cookie",
                                 "millen_key=%s; Path=/; Max-Age=2592000; "
                                 "SameSite=Lax" % ACCESS_KEY)
                self.send_header("Location", "/")
                self.end_headers()
                return False
            wrong = True
        if self.path == "/" or self.path.startswith("/?"):
            body = (GATE_PAGE.replace(
                "__GATE_NOTE__",
                "that key isn’t right — try again" if wrong else "")
                .encode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
            return False
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        try:
            self.wfile.write(b"MillenAI: access key required.")
        except Exception:
            pass
        return False

    # ADMIN endpoints act on the HOST MACHINE — trigger downloads, run the
    # updater, open Finder, speak through the Mac's speakers. Remote
    # visitors (even with the key) get a flat 403 on all of them; they are
    # guests in the chat, not operators of the computer.
    ADMIN_PATHS = ("/api/open-logs", "/api/setup/install",
                   "/api/model/download", "/api/update/install",
                   "/api/speak", "/api/voice/prepare")

    def _admin_gate(self) -> bool:
        """True = allowed. Answers the request itself when blocked."""
        if not self._remote():
            return True
        if not any(self.path.startswith(p) for p in self.ADMIN_PATHS):
            return True
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(b'{"ok": false, "err": "owner only"}')
        except Exception:
            pass
        return False

    # ------------------------------------------------------------ identity
    def _remote(self) -> bool:
        """True for requests arriving through the tunnel/proxy. The native
        app and local browsers talk straight to this server and never
        carry these headers."""
        return bool(self.headers.get("Cf-Connecting-Ip")
                    or self.headers.get("X-Forwarded-For"))

    def _uid(self):
        m = re.search(r"millen_user=([0-9a-f]{20})",
                      self.headers.get("Cookie", "") or "")
        return m.group(1) if m else None

    def _data_base(self):
        """Directory whose chats/memory/prefs this request may touch.
        None = the legacy files (the machine owner's, desktop app only).
        A remote request NEVER gets None: signed-in visitors get their own
        dir, and a cookieless remote fetch gets a throwaway shared pen —
        the owner's data is unreachable through the tunnel, full stop."""
        uid = self._uid()
        if uid:
            if uid == owner_uid():
                return None          # the owner's cookie opens the legacy files
            d = os.path.join(app_dir(), "users", uid)
            os.makedirs(d, exist_ok=True)
            return d
        if self._remote():
            d = os.path.join(app_dir(), "users", "_anon")
            os.makedirs(d, exist_ok=True)
            return d
        return None

    def _set_user_cookie(self, uid: str, location="/"):
        self.send_response(302)
        self.send_header("Set-Cookie",
                         "millen_user=%s; Path=/; Max-Age=15552000; "
                         "HttpOnly; SameSite=Lax" % uid)
        self.send_header("Location", location)
        self.end_headers()

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        if not self._gate():
            return
        if self.path == "/" or self.path.startswith("/?"):
            # ("/?key=..." legacy links included — the key is simply ignored)
            # tunnel visitors must have an identity before the app loads —
            # this is what keeps the owner's chats out of everyone's hands
            if self._remote() and not self._uid():
                body = (WELCOME_PAGE.replace(
                    "__GOOGLE_DISPLAY__",
                    "inline-block" if google_conf() else "none")
                    .encode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Type",
                                 "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            html = (HTML_CONTENT
                    .replace("__AGENT_ROWS__", build_agent_rows())
                    .replace("__APP_BETA__",
                             'VERSION <b class="vnum">%s</b>' % short_version())
                    .replace("__TIER_ROWS__", build_tier_rows())
                    .replace("__CHIP__", chip_name())
                    .replace("__WIN_WIPE__",
                             "1" if (HAS_WEBVIEW and IS_MAC) else "0")
                    .replace("__SKY_N__", str(len(SKY_SOURCES)))
                    .replace("__SKY_DARK__", json.dumps(SKY_DARK))
                    .replace("__APP_VER__", short_version()))
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/auth/google":
            conf = google_conf()
            if not conf:
                self.send_error(404, "Google sign-in not configured")
                return
            state = secrets.token_hex(16)
            now = time.time()
            for k in [k for k, t in _oauth_states.items() if now - t > 600]:
                _oauth_states.pop(k, None)
            _oauth_states[state] = now
            host = self.headers.get("Host", "")
            params = urllib.parse.urlencode({
                "client_id": conf["client_id"],
                "redirect_uri": "https://%s/auth/google/callback" % host,
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "prompt": "select_account",
            })
            self.send_response(302)
            self.send_header(
                "Location",
                "https://accounts.google.com/o/oauth2/v2/auth?" + params)
            self.end_headers()
        elif self.path.startswith("/auth/google/callback"):
            conf = google_conf()
            q = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            state = (q.get("state") or [""])[0]
            code = (q.get("code") or [""])[0]
            if not (conf and code and _oauth_states.pop(state, None)):
                self.send_error(403, "sign-in state mismatch — try again")
                return
            host = self.headers.get("Host", "")
            try:
                # the id_token comes straight from Google over TLS in this
                # server-to-server exchange, so decoding its payload
                # without signature verification is sound here
                body = urllib.parse.urlencode({
                    "code": code,
                    "client_id": conf["client_id"],
                    "client_secret": conf["client_secret"],
                    "redirect_uri":
                        "https://%s/auth/google/callback" % host,
                    "grant_type": "authorization_code",
                }).encode()
                with urllib.request.urlopen(urllib.request.Request(
                        "https://oauth2.googleapis.com/token", data=body),
                        timeout=15) as r:
                    tok = json.load(r)
                payload = tok["id_token"].split(".")[1]
                payload += "=" * (-len(payload) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload))
                email = (claims.get("email") or "").lower()
                if not email:
                    raise ValueError("no email in token")
            except Exception as exc:
                self.send_error(502, ("Google sign-in failed: %s"
                                      % str(exc)[:80]))
                return
            self._set_user_cookie(_user_id("google", email))
        elif self.path == "/api/sky/cached":
            self._send_json({"cached": [
                i for i in range(len(SKY_SOURCES))
                if os.path.exists(_sky_path(i))]})
        elif self.path.startswith("/api/sky/status"):
            m = re.search(r"[?&]i=(\d+)", self.path)
            self._send_json(sky_status(int(m.group(1)) if m else 0,
                                       warm="warm=1" in self.path))
        elif self.path.startswith("/sky/"):
            self._send_sky()
        elif self.path == "/api/fleet/mine":
            if self._remote():
                self._send_json({"err": "owner only"})
                return
            self._send_json({"on": bool(load_prefs(None).get("contrib_on")),
                             "state": _contrib_state[0]})
        elif self.path == "/api/fleet/status":
            if self._remote():
                self._send_json({"err": "owner only"})
                return
            alive = _fleet_alive()
            with _fleet_lock:
                pend = [{"id": w, "name": v["name"]}
                        for w, v in _fleet_pending.items()]
            self._send_json({"key": fleet_key(),
                             "pending": pend,
                             "workers": [{"name": v["name"],
                                          "busy": v.get("busy", False),
                                          "models": len(v.get("models", []))}
                                         for v in alive.values()]})
        elif self.path == "/api/cloud":
            if self._remote():
                self._send_json({"err": "owner only"})
                return
            c = cloud_conf()
            self._send_json({"configured": bool(c),
                             "name": (c or {}).get("name", "")})
        elif self.path == "/api/downloads":
            self._send_json(download_links())
        elif self.path == "/api/stats":
            self._send_stats()
        elif self.path == "/api/engines":
            self._send_engines()
        elif self.path == "/api/setup":
            self._send_json(setup_status())
        elif self.path == "/api/update/check":
            self._send_json(check_update())
        elif self.path == "/api/update/status":
            self._send_json(dict(_update))
        elif self.path == "/api/tiers":
            pulled = ollama_pulled_tags() or set()
            out = {}
            for name, t in TIERS.items():
                chosen = resolve_tier(name)
                # installed models this tier can't use right now
                skipped = [l for l in MODEL_INFO
                           if model_cached(l, pulled) and l not in chosen
                           and not model_fits_memory(l)]
                out[name] = {"desc": t["desc"], "models": chosen,
                             "skipped": skipped}
            self._send_json(out)
        elif self.path == "/api/prefs":
            self._send_json(load_prefs(self._data_base()))
        elif self.path == "/api/chats":
            with _chats_lock:
                self._send_json({"chats": load_chats(self._data_base())})
        elif self.path == "/api/memory":
            self._send_json({"facts": _load_memory(self._data_base())})
        elif self.path == "/api/voice/status":
            with _setup_lock:
                job = dict(_setup_jobs.get(VOICE_ROW, {}))
            pct = job.get("pct", 0)
            if job.get("status") == "downloading":
                est = 1_600_000_000
                pct = min(99, round(
                    _dir_bytes(_hf_model_dir(WHISPER_REPO)) / est * 100))
            self._send_json({"supported": _voice_supported(),
                             "ready": _voice_supported() and _voice_ready(),
                             "downloading": job.get("status") == "downloading",
                             "pct": pct,
                             "note": job.get("note", "")})
        else:
            self.send_error(404)

    def _send_sky(self):
        """Stream a cached skyline clip with Range support — Safari asks
        for dozens of byte ranges while scrubbing a video into playback,
        and a plain 200 would make it re-pull the whole file each time."""
        m = re.match(r"/sky/(\d+)\.mov$", self.path)
        p = _sky_path(int(m.group(1))) if m else None
        if not (p and os.path.exists(p)):
            self.send_error(404)
            return
        try:
            os.utime(p, None)   # LRU keys on mtime = "recently played"
        except OSError:
            pass
        size = os.path.getsize(p)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        partial = rng.startswith("bytes=")
        if partial:
            try:
                a, b = rng[6:].split(",")[0].split("-")[:2]
                start = int(a) if a else max(0, size - int(b))
                if a:
                    end = min(int(b), size - 1) if b else size - 1
            except ValueError:
                partial = False
                start, end = 0, size - 1
        if start > end or start >= size:
            self.send_error(416)
            return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/quicktime")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        try:
            with open(p, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:
            pass          # client hung up mid-stream — normal for video

    def _send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_engines(self):
        """Probe every backend so the UI can show live status dots."""
        status = {}

        # Ollama: one call tells us it's up AND which models are pulled
        pulled = None
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:11434/api/tags", timeout=1.5
            ) as r:
                tags = json.loads(r.read().decode("utf-8")).get("models", [])
                pulled = {m.get("name", "").split(":")[0] for m in tags} | \
                         {m.get("name", "") for m in tags}
        except Exception:
            pulled = None  # ollama down

        for label, (kind, target) in MODEL_ROUTES.items():
            if kind == "ollama":
                if pulled is None:
                    status[label] = {"up": False, "note": "ollama offline",
                                     "cmd": "ollama serve"}
                elif target in pulled:
                    status[label] = {"up": True, "note": "ready"}
                else:
                    status[label] = {"up": False,
                                     "note": f"not pulled — ollama pull {target}",
                                     "cmd": f"ollama pull {target}"}
            else:
                repo = MLX_REPOS.get(label, "<model-repo>")
                if _port_in_use(target):
                    status[label] = {"up": True, "note": f"loaded · port {target}"}
                elif mlx_model_cached(repo):
                    # downloaded but idle — starts on demand, so it IS usable
                    status[label] = {"up": True, "note": "ready · loads on use"}
                else:
                    status[label] = {
                        "up": False,
                        "note": "not downloaded",
                        "cmd": (f"mlx_lm.server --model {repo} "
                                f"--port {target}"),
                    }

        for label, st in status.items():
            st["mem_ok"] = model_fits_memory(label)
            st["supported"] = SUPPORTED.get(label, True)
            st["downloadable"] = not st.get("up") and st["supported"]
            st["mem"] = MODEL_MEM_BYTES.get(label, 0)  # strength proxy
            with _setup_lock:
                job = dict(_setup_jobs.get(label, {}))
            if job.get("status") in ("downloading", "queued"):
                st["dl"] = job.get("status")
                pct = job.get("pct", 0)
                # MLX downloads report no progress of their own — measure
                # the growing cache directory instead (ollama streams pct)
                if MODEL_ROUTES.get(label, ("",))[0] == "mlx":
                    est = MLX_EST_BYTES.get(label) or 1
                    grown = _dir_bytes(_hf_model_dir(MLX_REPOS[label]))
                    pct = min(99, round(grown / est * 100))
                st["pct"] = pct

        # models this Mac can't run never appear as "up"
        for label, ok in SUPPORTED.items():
            if not ok:
                status.setdefault(label, {})
                status[label].update(up=False, supported=False,
                                     note="needs Apple silicon")

        body = json.dumps(status).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stats(self):
        # who's around: every profile ever created, and identities seen in
        # the last 5 minutes (the owner's desktop counts via this very poll)
        now = time.time()
        uid = self._uid()
        _last_seen[uid or "owner"] = now
        try:
            total = 1 + len([d for d in os.listdir(
                os.path.join(app_dir(), "users")) if d != "_anon"])
        except Exception:
            total = 1
        online = sum(1 for t in _last_seen.values() if now - t < 300)
        gpu = gpu_utilization()
        if HAS_PSUTIL:
            vm = psutil.virtual_memory()
            stats = {
                "real": True,
                "mem_used_gb": round(vm.used / 1e9, 1),
                "mem_total_gb": round(vm.total / 1e9, 1),
                "mem_pct": vm.percent,
                "gpu_pct": gpu,  # None when ioreg has no accelerator stats
                "users_online": online, "users_total": total,
                "fleet_online": len(_fleet_alive()),
                "fleet_busy": sum(1 for v in _fleet_alive().values()
                                  if v.get("busy")),
            }
        else:
            stats = {"real": False, "gpu_pct": gpu,
                     "users_online": online, "users_total": total}
        body = json.dumps(stats).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        if not self._gate():
            return
        if not self._admin_gate():
            return
        if self.path == "/api/welcome":
            n = int(self.headers.get("Content-Length", 0))
            try:
                d = json.loads(self.rfile.read(n))
                name = str(d.get("name", "")).strip()
                pin = str(d.get("pin", "")).strip()
            except (ValueError, json.JSONDecodeError):
                name = pin = ""
            if len(name) < 2 or not re.fullmatch(r"\d{8,12}", pin):
                self._send_json({"ok": False,
                                 "err": "name (2+) and an 8-12 digit PIN"})
                return
            # the owner PIN (any name) opens the owner's real data; every
            # other combination gets its own private profile as before
            own = owner_uid()
            if own and _user_id("owner", pin) == own:
                uid = own
            else:
                uid = _user_id("pin", name.lower() + ":" + pin)
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Set-Cookie",
                             "millen_user=%s; Path=/; Max-Age=15552000; "
                             "HttpOnly; SameSite=Lax" % uid)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/fleet/"):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                d = json.loads(self.rfile.read(n)) if n else {}
            except (ValueError, json.JSONDecodeError):
                d = {}
            if self.path == "/api/fleet/approve":
                # the OWNER approving a knock — no fleet key involved
                if self._remote():
                    self._send_json({"err": "owner only"})
                    return
                wid0 = str(d.get("id", ""))
                with _fleet_lock:
                    pend = _fleet_pending.pop(wid0, None)
                if pend:
                    appr = _fleet_approved()
                    appr[wid0] = {"name": pend["name"],
                                  "token": secrets.token_urlsafe(18)}
                    _fleet_save_approved(appr)
                self._send_json({"ok": bool(pend)})
                return
            key = self.headers.get("X-Fleet-Key", "")
            keyed = secrets.compare_digest(key, fleet_key())
            wid = str(d.get("id") or "")
            approved = _fleet_approved()
            token_ok = (wid in approved and secrets.compare_digest(
                str(d.get("token", "")), approved[wid].get("token", "?")))
            if self.path == "/api/fleet/register":
                # ONE-CLICK flow: no key typed anywhere. A new worker
                # lands in the pending list until the owner approves it
                # in Settings; then a token rides every request.
                name = str(d.get("name", "worker"))[:40]
                models = [m for m in (d.get("models") or [])
                          if isinstance(m, str)][:40]
                if not wid:
                    wid = secrets.token_hex(8)
                claim = approved.get(wid)
                if keyed or token_ok or (claim and not claim.get("claimed")):
                    if claim and not claim.get("claimed"):
                        # ONE-TIME token handover right after approval —
                        # a lost token means the owner approves again
                        claim["claimed"] = True
                        approved[wid] = claim
                        _fleet_save_approved(approved)
                    with _fleet_lock:
                        _fleet_workers[wid] = {
                            "name": name, "models": models,
                            "last_seen": time.time(),
                            "busy": _fleet_workers.get(wid, {}).get(
                                "busy", False)}
                        _fleet_pending.pop(wid, None)
                    tok = approved.get(wid, {}).get("token", "")
                    self._send_json({"id": wid, "token": tok})
                    return
                with _fleet_lock:
                    _fleet_pending[wid] = {"name": name, "models": models,
                                           "ts": time.time()}
                    # forgotten knocks expire
                    for w in [w for w, v in _fleet_pending.items()
                              if time.time() - v["ts"] > 900]:
                        _fleet_pending.pop(w, None)
                self._send_json({"id": wid, "pending": True})
                return
            if not (keyed or token_ok):
                self._send_json({"err": "not approved"})
                return
            if self.path == "/api/fleet/poll":
                wid = str(d.get("id", ""))
                deadline = time.time() + 25
                while time.time() < deadline:
                    with _fleet_lock:
                        if wid in _fleet_workers:
                            _fleet_workers[wid]["last_seen"] = time.time()
                        for jid in list(_fleet_queue):
                            job = _fleet_jobs.get(jid)
                            if job and job["wid"] == wid:
                                _fleet_queue.remove(jid)
                                self._send_json(
                                    {"job": jid, "label": job["label"],
                                     "messages": job["messages"]})
                                return
                    time.sleep(0.4)
                self._send_json({})
                return
            if self.path == "/api/fleet/submit":
                jid = str(d.get("job", ""))
                with _fleet_lock:
                    job = _fleet_jobs.get(jid)
                    if job:
                        job["text"] = str(d.get("text", ""))[:60000]
                        job["err"] = str(d.get("err", ""))[:200]
                        job["done"].set()
                self._send_json({"ok": True})
                return
            self.send_error(404)
            return
        if self.path == "/api/setup/install":
            # warm one backdrop alongside the models, so the very first
            # launch already opens onto a moving city
            try:
                sky_status(random.randrange(len(SKY_SOURCES)))
            except Exception:
                pass
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                plan = (json.loads(self.rfile.read(n)) or {}).get("plan", "max")
            except (ValueError, json.JSONDecodeError):
                plan = "max"
            self._send_json(
                {"started": start_model_downloads(plan_labels(plan))})
            return
        if self.path == "/api/update/install":
            if _update["state"] in ("idle", "error"):
                threading.Thread(target=_do_update, daemon=True).start()
            self._send_json({"ok": True})
            return
        if self.path == "/api/prefs":
            n = int(self.headers.get("Content-Length", 0))
            try:
                d = json.loads(self.rfile.read(n))
            except (ValueError, json.JSONDecodeError):
                d = None
            if isinstance(d, dict):
                base = self._data_base()
                cur = load_prefs(base)
                cur.update(d)
                if base is None and "no_limits" in d:
                    _no_limits["v"] = bool(d.get("no_limits"))
                if base is None and any(k.startswith("contrib_") for k in d):
                    threading.Thread(target=contrib_apply, args=(cur,),
                                     daemon=True).start()
                store_prefs(cur, base)
            self._send_json({"ok": isinstance(d, dict)})
            return
        if self.path == "/api/chats":
            n = int(self.headers.get("Content-Length", 0))
            try:
                items = json.loads(self.rfile.read(n)).get("chats", [])
            except (ValueError, json.JSONDecodeError):
                items = None
            if isinstance(items, list):
                with _chats_lock:
                    store_chats(items, self._data_base())
            self._send_json({"ok": isinstance(items, list)})
            return
        if self.path == "/api/title":
            n = int(self.headers.get("Content-Length", 0))
            try:
                txt = json.loads(self.rfile.read(n)).get("text", "")
            except (ValueError, json.JSONDecodeError):
                txt = ""
            self._send_json({"title": make_title(txt) if txt else ""})
            return
        if self.path == "/api/open-logs":
            subprocess.Popen(
                ["open", log_dir()])
            self._send_json({"ok": True})
            return
        if self.path == "/api/memory/clear":
            with _memory_lock:
                _save_memory([], self._data_base())
            self._send_json({"ok": True})
            return
        if self.path == "/api/voice/prepare":
            if _voice_supported() and not _voice_ready():
                _prepare_voice()
            self._send_json({"ok": True})
            return
        if self.path == "/api/transcribe":
            n = int(self.headers.get("Content-Length", 0))
            wav = self.rfile.read(n)
            if not (_voice_supported() and _voice_ready()):
                self.send_error(503, "voice engine not ready")
                return
            try:
                self._send_json({"text": _transcribe_wav(wav)})
            except Exception as exc:
                self.send_error(500, str(exc)[:100])
            return
        if self.path == "/api/speak":
            n = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(n))
            except (ValueError, json.JSONDecodeError):
                req = {}
            if req.get("stop"):
                _stop_speaking()
            elif req.get("text"):
                _speak(req["text"])
            self._send_json({"ok": True})
            return
        if self.path == "/api/model/download":
            n = int(self.headers.get("Content-Length", 0))
            try:
                want = json.loads(self.rfile.read(n)).get("labels", [])
            except (ValueError, json.JSONDecodeError):
                want = []
            self._send_json({"started": start_model_downloads(want)})
            return
        if self.path != "/api/chat":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            req_json = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return

        messages = list(req_json.get("messages", []))
        # pasted images ride beside the text; vision always routes to
        # LLaVA on Ollama (native /api/chat takes raw base64 per message)
        images = [i for i in (req_json.get("images") or [])
                  if isinstance(i, str) and len(i) < 8_000_000][:3]
        model_name = req_json.get("model", "")
        auto_web = req_json.get("auto_web", True)
        # a tier resolves to its own line-up; otherwise honour explicit picks
        tier = req_json.get("tier") or ""
        if tier == "Smart":
            tier = "Fast"   # merged tiers (1.20) — old clients still send Smart
        if tier in TIERS:
            council = resolve_tier(tier)
        else:
            council = [m for m in req_json.get("models", [])
                       if m in MODEL_ROUTES]
        if not council:
            council = [model_name]
        prompt = messages[-1]["content"] if messages else ""

        # a selected AGENT owns the request: its best installed model, its
        # specialist system prompt; Research routes to the research flow
        agent_name = req_json.get("agent") or ""
        ag_system, ag_research = "", False
        if agent_name and not req_json.get("images"):
            ag_label, ag = resolve_agent(agent_name)
            if ag:
                ag_research = bool(ag.get("research"))
                ag_system = ag.get("system", "")
                if ag_label:
                    council = [ag_label]
                    model_name = ag_label
                    tier = "Research" if ag_research else ""

        docs = [d for d in (req_json.get("docs") or [])
                if isinstance(d, dict) and d.get("text")][:2]
        if docs:
            # attached files ARE the context: fold them into the message,
            # keep the search out of the way
            auto_web = False
            block = "\n\n".join(
                "--- FILE: %s ---\n%s" % (str(d.get("name", "file"))[:120],
                                           str(d["text"])[:50000])
                for d in docs)
            vm = dict(messages[-1]) if messages else {"role": "user",
                                                      "content": ""}
            base = str(vm.get("content", "")).strip() \
                or "Summarize the attached file(s) and note anything notable."
            # files first, question last, and an explicit "this is real
            # data" frame: without it the 35B read ZEBRA-42 and then
            # DENIED it existed, treating "secret launch code" as a prank
            vm["content"] = (
                "The user attached the following file(s). Their contents "
                "are real data provided by the user — read them and "
                "answer from them directly and factually.\n\n"
                "ATTACHED FILES:\n" + block +
                "\n\nQUESTION: " + base)
            messages = messages[:-1] + [vm] if messages else [vm]
            prompt = base

        if images:
            # vision answers come from the pixels: no web search, no tier
            # council — LLaVA takes the whole request
            auto_web = False
            tier = ""
            b64s = [u.split(",", 1)[1] if u.startswith("data:") else u
                    for u in images]
            vm = dict(messages[-1]) if messages else {"role": "user",
                                                      "content": ""}
            if not str(vm.get("content", "")).strip():
                vm["content"] = "Describe this image in useful detail."
            vm["images"] = b64s
            messages = messages[:-1] + [vm] if messages else [vm]
            council = ["LLaVA Vision 7B"]
            model_name = "LLaVA Vision 7B"
            prompt = vm["content"]

        # "/search …" forces a lookup; otherwise auto-search decides.
        query, forced = None, prompt.lower().startswith("/search")
        if forced:
            query = prompt[7:].strip()
        elif (auto_web and needs_search(prompt)
              and not TIERS.get(tier, {}).get("research")):
            query = prompt.strip()

        if query:
            snippets = None
            is_weather = bool(re.search(
                r"\bweather\b|\bforecast\b|\btemperature\b", query, re.I))
            if is_weather:
                snippets = weather_snippets(query)
            if snippets is not None:
                # data answers need the DATA: a 3B given real degrees once
                # replied "warm the cockles" with no numbers at all
                messages[-1] = {
                    "role": "user",
                    "content": (
                        "Answer using ONLY the live data below. Begin "
                        "your reply with the current temperature and "
                        "conditions (e.g. \'It\'s 74\u00b0F and clear "
                        "right now\'), then wind and the forecast days. "
                        "Never omit the temperature.\n"
                        f"{snippets}\n\nQUESTION: {query}"
                    ),
                }
            else:
                snippets = run_search(query)
                messages[-1] = {
                    "role": "user",
                    "content": (
                        "You have internet access. Using these live search "
                        f"snippets, answer the prompt.\n"
                        f"SNIPPETS FOR '{query}':\n{snippets}\n\nPROMPT: "
                        f"{query}"
                    ),
                }

        # local models have no clock — without this "today" is meaningless
        today = time.strftime("%A, %B %-d, %Y")
        dated_system = dict(SYSTEM_PROMPT)
        if ag_system:
            dated_system["content"] = ag_system
        dated_system["content"] += f"\n\nToday's date is {today}."
        if tier == "Thinking" and messages:
            messages[-1] = dict(messages[-1])
            messages[-1]["content"] += "\n\n" + THINK_HINT
        user_base = self._data_base()      # whose memory/persona this is
        mem = memory_text(user_base)
        if mem:
            dated_system["content"] += (
                "\n\nFrom earlier conversations you remember these facts "
                "about the user:\n" + mem +
                "\nUse them naturally when relevant — don't recite them.")
        # standing preferences the user wrote themselves (About panel) — they
        # outrank remembered facts, which are extracted guesses
        persona = (load_prefs(user_base).get("persona") or "").strip()[:2000]
        if persona:
            dated_system["content"] += (
                "\n\nThe user has set standing instructions for how you "
                "should respond, in their own words:\n\"" + persona + "\"\n"
                "Follow them in every reply without restating them. If the "
                "current message conflicts with them, the message wins.")
        full_messages = [dated_system] + messages

        route, route_label = None, None
        for label, target in MODEL_ROUTES.items():
            if label in model_name:
                route, route_label = target, label
                break
        if route is None:
            # smallest cached model, never a 40 GB bomb (see run_model)
            pulled = ollama_pulled_tags() or set()
            route_label = next((l for l in reversed(MERGE_RANK)
                                if model_cached(l, pulled)), None)
            route = MODEL_ROUTES.get(route_label, (None, None))

        # MLX engines load on demand so only the model in use holds RAM
        if route[0] == "mlx" and route_label:
            with _engine_lock:
                ensure_mlx_engine(route_label)

        # stream plain text back; the browser reads it progressively
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Web-Search", "1" if query else "0")
        self.send_header("X-Models", ", ".join(council)[:300])
        self.end_headers()

        # Cloudflare drops a proxied response after ~100s without bytes,
        # and an engine swap + big-model load is a multi-minute SILENCE:
        # remote council runs died with "network error" while localhost
        # sailed (seen live). A heartbeat thread re-sends the last status
        # marker whenever the stream has been quiet too long. Writes are
        # serialized with a lock; the client treats repeated STATUS
        # markers as idempotent.
        wlock = threading.Lock()
        last_write = [time.time()]
        last_status = ["working"]
        hb_stop = threading.Event()

        def _write(data: bytes):
            with wlock:
                self.wfile.write(data)
                self.wfile.flush()
                last_write[0] = time.time()

        def _heartbeat():
            while not hb_stop.wait(5):
                if time.time() - last_write[0] > 20:
                    try:
                        _write(f"{NUL}STATUS:{last_status[0]}{NUL}"
                               .encode("utf-8"))
                    except Exception:
                        return
        threading.Thread(target=_heartbeat, daemon=True).start()

        sent = [0]

        def emit(chunk: str):
            chunk = strip_special(chunk)
            if not chunk:
                return
            sent[0] += len(chunk)
            _write(chunk.encode("utf-8"))

        def status(text: str):
            # sentinel-wrapped so the UI can show progress without it
            # ending up inside the answer text
            last_status[0] = text
            _write(f"{NUL}STATUS:{text}{NUL}".encode("utf-8"))

        kind, target = route
        # first image before the vision engine exists: kick the download
        # and say so, instead of a cryptic connection error
        if images and not model_cached("LLaVA Vision 7B",
                                       ollama_pulled_tags() or set()):
            try:
                start_model_downloads(["LLaVA Vision 7B"])
                emit("Getting the vision engine ready (LLaVA, ~4.7 GB) — "
                     "the download just started. Progress is under "
                     "**Settings › Download models…**; paste the image again once it "
                     "shows the check mark.")
            except (BrokenPipeError, ConnectionResetError):
                pass
            hb_stop.set()
            return
        try:
            if TIERS.get(tier, {}).get("research") or ag_research:
                run_research(council, full_messages, emit, status)
            elif len(council) > 1:
                run_council(council, full_messages, emit, status,
                            reflect=(tier == "Thinking"),
                            peer=(tier == "Power"))
            else:
                lbl = route_label or model_name
                turbo = (load_prefs(None).get("turbo") and cloud_conf()
                         and not images)
                if turbo:
                    status(f"turbo \u2014 {cloud_conf().get('name','cloud')}")
                    if cloud_stream(full_messages, emit):
                        hb_stop.set()
                        return
                    status("turbo unavailable — running locally")
                ftext = fleet_run(lbl, full_messages, status) \
                    if not images else ""
                polish = (load_prefs(None).get("polish", True)
                          and not images and not query
                          and _is_substantive(prompt))
                if ftext:
                    emit(ftext)
                elif polish:
                    # TWO PASS: draft in silence, then stream the rewrite.
                    # The reader waits a little longer and gets a visibly
                    # better answer instead of a first-take one.
                    status(f"{lbl} is thinking it through")
                    parts = []
                    draft = ""
                    try:
                        run_model(lbl, full_messages, parts.append)
                        draft = strip_think("".join(parts))
                    except Exception:
                        draft = ""
                    if draft and not _looks_degenerate(draft):
                        status(f"{lbl} is sharpening the answer")
                        _stream_guarded(
                            lbl,
                            [full_messages[0],
                             {"role": "user",
                              "content": REVISE_INSTRUCTION
                              + "QUESTION: " + prompt
                              + "\n\nFIRST DRAFT:\n" + draft[:6000]}],
                            emit, status, draft,
                            "showing the first draft")
                    else:
                        _stream_guarded(lbl, full_messages, emit, status,
                                        None,
                                        "kept the part before it wandered")
                else:
                    # guarded like every other path: a lone model that
                    # collapses into repetition gets cut back to its
                    # coherent prefix instead of streaming the loop
                    _stream_guarded(lbl, full_messages,
                                    emit, status, None,
                                    "kept the part before it wandered")
        except (BrokenPipeError, ConnectionResetError):
            pass  # user hit Stop — browser closed the connection
        except Exception as exc:
            try:
                emit("\n" + offline_hint(kind, exc))
            except (BrokenPipeError, ConnectionResetError):
                pass
            if not sent[0]:
                # every path stayed silent (engine died mid-answer, a
                # provider returned nothing). Try the smallest brain on
                # disk before admitting defeat.
                try:
                    pulled = ollama_pulled_tags() or set()
                    alt = next((l for l in reversed(MERGE_RANK)
                                if model_cached(l, pulled)
                                and l != (route_label or model_name)), None)
                    if alt:
                        status(f"retrying on {alt}")
                        run_model(alt, full_messages, emit)
                except Exception:
                    pass
            if not sent[0]:
                emit("That engine stopped responding and the backup "
                     "didn't answer either. Ask again — it usually "
                     "comes straight back.")
        finally:
            hb_stop.set()
            plain = prompt[8:] if prompt.lower().startswith("/search") \
                else prompt
            if plain and len(plain) > 12:
                threading.Thread(
                    target=_extract_memory,
                    args=(route_label or council[0], plain, user_base),
                    daemon=True).start()


def start_backend():
    server = socketserver.ThreadingTCPServer(
        ("127.0.0.1", PORT), StudioHandler, bind_and_activate=False
    )
    server.allow_reuse_address = True
    server.daemon_threads = True
    server.server_bind()
    server.server_activate()
    server.serve_forever()


HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>
/* Window-wipe boot (native Mac app only): the NSWindow starts transparent
   and the whole page is clipped to nothing, so tagging the root BEFORE the
   first paint is what stops a flash of the normal UI. Performance mode
   opts out here too — rainbowWipe() would skip its half later anyway. */
if("__WIN_WIPE__"==="1"&&
   (location.hostname==="127.0.0.1"||location.hostname==="localhost")){
  // hostname check: remote/tunnel visitors share this server but sit in a
  // real browser, where a transparent page is a white flash, not a desktop
  try{
    if(localStorage.getItem("millen.perf")!=="1")
      document.documentElement.classList.add("winwipe");
  }catch(e){}
  // DEAD-MAN'S SWITCH, in THIS script: the unclip normally runs from the
  // main script — when a bug killed the main script, the page stayed
  // clipped to nothing and the window was pure black (seen live, v85).
  // Whatever happens below, the app becomes visible.
  setTimeout(function(){
    document.documentElement.classList.remove("winwipe","winwipe-run");
  },3000);
}
</script>
<title>MillenAI __APP_VER__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#212121;
  --panel:#171717;
  --panel2:#2f2f2f;
  --line:#3d3d3d;
  --line-soft:#333;
  --text:#ececec;
  --dim:#b4b4b4;
  --faint:#8e8e8e;
  --accent:#ececec;          /* white is the accent, as in GPT */
  --accent-hot:#fff;
  --accent-dim:rgba(255,255,255,.10);
  --teal:#c8c8c8;            /* was the secondary hue; now a light grey */
  --red:#e26d5a;             /* kept: errors and the update flag */
  --radius:10px;
  --mono:'IBM Plex Mono',ui-monospace,monospace;
  --sans:'Space Grotesk',system-ui,sans-serif;
  --helv:'Helvetica Neue',Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:var(--bg);color:var(--text);font-family:var(--sans);
  display:flex;overflow:hidden;font-size:15px;
}
/* ------------------------------------------------- window-wipe boot (Mac) */
/* The macOS window itself wipes into existence: the NSWindow is created
   transparent (see the cocoa block near create_window), the page is clipped
   to a zero-width sliver at the RIGHT edge, then unclipped right-to-left.
   The rainbow wash that follows travels left-to-right — always from the
   opposite side of the window's arrival.
   Two traps live here:
   1. Canvas propagation — body's background paints the whole viewport even
      when body is clipped, so during the wipe the background moves onto
      body::before, which clips with everything else.
   2. An occluded window gets no animation frames and no animationend; the
      1.6s timeout in winWipeFinish is what guarantees the page ever becomes
      visible. */
html.winwipe,html.winwipe body{background:transparent}
html.winwipe body{clip-path:inset(0 0 0 100%)}
html.winwipe body::before{
  content:"";position:fixed;inset:0;background:var(--bg);z-index:-99;
}
html.winwipe.winwipe-run body{
  animation:winWipe .95s cubic-bezier(.3,.75,.25,1) forwards;
}
@keyframes winWipe{
  from{clip-path:inset(0 0 0 100%)}
  to  {clip-path:inset(0 0 0 0)}
}

::selection{background:var(--accent-dim);color:var(--accent-hot)}
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
::-webkit-scrollbar-track{background:transparent}

/* ---------------------------------------------------------------- sidebar */
/* Frosted glass, not plain transparency: the skyline now runs under the
   whole window, and unblurred video behind 13px chat titles is unreadable
   noise. 70% panel over a 24px blur is the macOS material look — the city
   reads as light and colour through the glass, never as detail. */
#sidebar{
  position:relative;z-index:1;
  width:384px;min-width:384px;height:100%;
  /* real frosted glass, per Patrick: ~30% panel, heavy blur carrying the
     legibility instead of the tint */
  background:rgba(21,23,29,.24);
  -webkit-backdrop-filter:blur(18px) saturate(1.4);
          backdrop-filter:blur(18px) saturate(1.4);
  border-right:1px solid var(--line-soft);
  display:flex;flex-direction:column;padding:14px 18px 18px;gap:6px;
}
body.perf #sidebar{
  background:var(--panel);
  -webkit-backdrop-filter:none;backdrop-filter:none;
}
#sb-resize{
  position:absolute;top:0;right:-3px;width:7px;height:100%;
  cursor:col-resize;z-index:20;
}
#sb-resize:hover,body.resizing #sb-resize{background:rgba(255,255,255,.18)}
body.resizing{cursor:col-resize;user-select:none}
/* the 34px brand outgrew a single row (clipped to "lenAI" beside the
   buttons): the name owns its line now, controls sit beneath it */
#brand-wrap{padding:0 6px 12px}
#brand-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
#brand-row #newchat{margin-left:2px}
#update-flag{margin-top:4px}
#update-flag{
  font-family:var(--mono);font-size:10px;letter-spacing:.12em;
  color:#fff;background:#e26d5a;border-radius:8px;padding:5px 9px;
  cursor:pointer;font-weight:700;align-self:center;
  animation:updatePulse 2.2s ease-in-out infinite;
}
@keyframes updatePulse{0%,100%{opacity:1}50%{opacity:.65}}
#models-flag{
  font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;
  color:#fff;background:#4a7fd4;border-radius:8px;padding:5px 9px;
  cursor:pointer;font-weight:700;
  flex:1 0 100%;text-align:left;
  animation:updatePulse 2.6s ease-in-out infinite;
}
#models-flag:hover{text-decoration:underline}
#models-flag[hidden]{display:none}
/* web visitors only: a quiet outline chip pointing at the real app */
/* INVERTED: a solid white pill — the one thing on the page that reads
   like a real call to action */
#get-app{
  font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;
  color:#111;background:#f2f2f2;text-decoration:none;
  border:none;border-radius:8px;
  padding:7px 10px;font-weight:700;flex:1 0 100%;
  display:flex;align-items:center;gap:6px;margin-top:4px;
  box-shadow:0 6px 20px -10px rgba(255,255,255,.55);
  transition:background .18s,transform .18s,box-shadow .25s;
}
#get-app:hover{background:#fff;transform:translateY(-1px);
  box-shadow:0 10px 26px -10px rgba(255,255,255,.8)}
#get-app[hidden]{display:none}
#get-app i{
  font-style:normal;width:13px;height:13px;flex:none;cursor:help;
  border:1px solid rgba(0,0,0,.42);border-radius:50%;
  font-size:9px;line-height:11px;text-align:center;
  font-family:var(--helv);margin-left:auto;color:#111;
}
#update-flag:hover{text-decoration:underline}
#update-flag[hidden]{display:none}
/* centred, not baseline-aligned: the version pill is a bordered box, so
   sitting it on the wordmark's baseline hangs it low against the taller type */
.vghost{
  font-family:var(--mono);font-size:21.5px;letter-spacing:.04em;
  color:rgba(255,255,255,.62);user-select:none;cursor:pointer;
  padding-top:0;margin-right:auto;line-height:1.1;
  display:flex;align-items:baseline;gap:8px;
}
.vghost b{font-weight:700}
.vghost i{font-style:normal;font-weight:400;font-size:.82em;
  color:rgba(255,255,255,.42)}
.vghost:hover{color:rgba(255,255,255,.85)}

#newchat,#settings-btn{
  width:28px;height:28px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  background:none;border:1px solid var(--line);border-radius:8px;
  color:var(--accent-hot);cursor:pointer;padding:0;
  transition:border-color .15s,background .15s,color .15s;
}
#settings-btn{margin-left:6px;color:var(--dim)}
#newchat svg{width:15px;height:15px}
#settings-btn svg{width:15px;height:15px}
#newchat:hover,#settings-btn:hover{border-color:var(--accent-hot);background:var(--accent-dim);color:var(--text)}

.group-label{
  font-family:var(--mono);font-size:10px;letter-spacing:.14em;
  color:var(--faint);padding:16px 6px 6px;text-transform:uppercase;
}
.group-label.mlx{color:var(--teal);opacity:.75}
.group-label.ollama{color:var(--accent);opacity:.75}

.model{
  display:flex;align-items:center;gap:9px;padding:8px 10px;
  border-radius:8px;cursor:pointer;color:var(--dim);
  font-size:13.5px;border:1px solid transparent;transition:all .13s;
  user-select:none;white-space:nowrap;
}
.model:hover{color:var(--text);background:var(--panel2)}
#model-list{overflow-y:auto;overflow-x:hidden;flex:1;min-height:0}
.model.unsupported{opacity:.34;cursor:not-allowed}
.model.unsupported:hover{background:none;color:var(--dim)}
.model.pending .size{color:var(--accent)}
.group-label.chats{color:var(--dim);opacity:.75}
.group-label.adv{cursor:pointer;color:var(--faint);user-select:none;padding-top:12px}
.group-label.adv:hover{color:var(--dim)}
#tier-rows{margin:6px 0 4px}
.tier{
  display:flex;align-items:center;gap:9px;padding:6px 10px;margin-bottom:2px;
  border-radius:9px;cursor:pointer;color:var(--dim);font-size:13.5px;
  border:1px solid transparent;transition:all .13s;user-select:none;
}
.tier:hover{color:var(--text);background:var(--panel2)}
/* the library tabs + agent radio rows */
#mode-tabs{display:flex;gap:6px;margin:12px 0 8px}
#mode-tabs .ltab{
  flex:1;text-align:center;font-family:var(--mono);font-size:11px;
  letter-spacing:.12em;text-transform:uppercase;color:var(--faint);
  padding:7px 0;border:1px solid var(--line-soft);border-radius:9px;
  cursor:pointer;user-select:none;
}
#mode-tabs .ltab:hover{color:var(--dim)}
#mode-tabs .ltab.on{color:var(--text);background:var(--panel2);
  border-color:var(--line)}
#agents-wrap{margin:6px 0 4px}
.agent{
  display:flex;align-items:center;gap:9px;padding:6px 10px;margin-bottom:2px;
  border-radius:9px;color:var(--dim);font-size:13.5px;cursor:pointer;
  border:1px solid transparent;transition:all .13s;user-select:none;
}
.agent:hover{color:var(--text);background:var(--panel2)}
.agent .radio{display:none}
.agent .aname,.agent b{font-weight:600}
.agent.on{
  color:var(--text);background:var(--accent-dim);
  border-color:rgba(255,255,255,.26);
}

/* model-group dropdowns: carets on the hardware-class headers */

/* dropdown behaviour: collapsed shows only the active tier + caret */
#tier-rows.closed .tier:not(.active){display:none}
#tier-rows.closed .tier.active::after{
  content:"▾";margin-left:auto;color:var(--faint);font-size:12px;
}
#tier-rows:not(.closed) .tier{animation:tierDrop .16s ease both}
/* the agent list folds the same way: closed shows only the choice */
#agents-wrap.closed .agent:not(.on){display:none}
#agents-wrap.closed .agent.on::after{
  content:"▾";margin-left:auto;color:var(--faint);font-size:12px;
}
#agents-wrap:not(.closed) .agent{animation:tierDrop .16s ease both}
@keyframes tierDrop{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:none}}

.tier.active{
  color:var(--text);background:var(--accent-dim);
  border-color:rgba(255,255,255,.26);
}
.tier .tname{font-weight:600}

#chat-list{margin-bottom:2px;overflow-y:auto}
#chat-list:empty::after{
  content:"no chats yet";display:block;color:var(--faint);
  font-size:11px;padding:2px 10px 6px;
}
.chat-item{
  margin-bottom:1px;
  display:flex;align-items:center;gap:6px;padding:5px 11px;
  border-radius:8px;cursor:pointer;color:var(--dim);font-size:13.5px;
  border:1px solid transparent;user-select:none;
}
.chat-item:hover{color:var(--text);background:var(--panel2)}
.chat-item.active{
  color:var(--text);background:var(--accent-dim);
  border-color:rgba(255,255,255,.22);
}
.chat-item .ct{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-item .cx{color:var(--faint);visibility:hidden;font-size:13px;padding:0 3px}
.chat-item:hover .cx{visibility:visible}
.chat-item .cx:hover{color:var(--red)}
.infobtn{
  margin-left:auto;width:17px;height:17px;border-radius:50%;flex-shrink:0;
  margin-left:9px;background:var(--line);color:var(--dim);
  font:italic 600 10px var(--helv);line-height:17px;text-align:center;
  cursor:pointer;opacity:.5;transition:opacity .13s,background .13s,color .13s;
}
.tier:hover .infobtn,.infobtn:hover{opacity:1}
.infobtn:hover{background:var(--accent);color:#1a1a1a}
#tierpop{
  position:fixed;z-index:70;max-width:250px;
  background:var(--panel2);border:1px solid var(--line);border-radius:10px;
  padding:11px 13px;box-shadow:0 14px 40px rgba(0,0,0,.55);
  font-size:12px;color:var(--dim);line-height:1.6;
}
#tierpop[hidden]{display:none}
#tierpop b{color:var(--text);display:block;margin-bottom:5px;font-size:12.5px}
#tierpop .mline{font-family:var(--mono);font-size:11px;color:var(--accent)}
#tierpop .note{color:var(--faint);font-size:10.5px;margin-top:7px;display:block}
.model.active{
  color:var(--text);background:var(--accent-dim);
  border-color:rgba(255,255,255,.22);
}
.model .ico{width:18px;text-align:center;font-size:13px}
.model .size{margin-left:auto;font-family:var(--mono);font-size:10px;color:var(--faint)}
.model.active .size{color:var(--accent)}
.model .dot{
  width:7px;height:7px;border-radius:50%;background:var(--line);
  flex-shrink:0;transition:background .3s;
}
.model .dot.up{background:#5fbf77;box-shadow:0 0 6px rgba(95,191,119,.5)}
.model .dot.down{background:var(--red);opacity:.75}

#settings{padding:14px 6px 4px;margin-top:auto}
.toggle-row{
  display:flex;align-items:center;gap:10px;cursor:pointer;
  color:var(--dim);font-size:12.5px;user-select:none;
}
.toggle-row:hover{color:var(--text)}
.switch{
  width:30px;height:17px;border-radius:9px;background:var(--line);
  position:relative;transition:background .15s;flex-shrink:0;
}
.switch::after{
  content:"";position:absolute;top:2px;left:2px;width:13px;height:13px;
  border-radius:50%;background:var(--dim);transition:all .15s;
}
.toggle-row.on .switch{background:var(--accent)}
.toggle-row.on .switch::after{left:15px;background:#1a1a1a}

/* telemetry — the instrument cluster */
#telemetry{
  margin-top:12px;background:rgba(21,23,29,.8);border:1px solid var(--line-soft);
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
  border-radius:var(--radius);padding:12px 12px 11px;
  font-family:var(--mono);
}
#telemetry .t-head{
  font-size:14px;letter-spacing:.08em;color:var(--dim);
  display:flex;justify-content:space-between;align-items:baseline;
  margin-bottom:10px;gap:10px;
}
#telemetry .t-head span{white-space:nowrap}
#models-up{cursor:pointer;color:var(--dim);font-weight:700;
  padding:0 4px;border:1px solid var(--line);border-radius:6px;
  font-size:11px;line-height:1.5}
#models-up:hover{color:var(--text);border-color:var(--accent-hot)}
#telemetry .t-head .live{color:var(--text);white-space:nowrap}
.meter-row{margin-bottom:9px}
.meter-row:last-child{margin-bottom:0}
.meter-label{
  display:flex;justify-content:space-between;font-size:13px;
  color:var(--dim);margin-bottom:4px;
}
.meter-label b{color:var(--text);font-weight:500}
.meter{height:3px;border-radius:2px;background:rgba(255,255,255,.10);
  overflow:hidden}
.meter .mfill{height:100%;width:0;border-radius:2px;
  background:linear-gradient(90deg,var(--accent),var(--accent-hot));
  box-shadow:0 0 10px var(--accent-dim);
  transition:width .6s cubic-bezier(.4,0,.2,1),background .4s}
.meter .mfill.hot{background:linear-gradient(90deg,var(--accent-hot),#e26d5a)}
@keyframes blink{50%{opacity:.25}}
/* performance mode: telemetry goes dark AND stops polling (the GPU probe
   and meter repaints are the expensive part) */

/* ------------------------------------------------------------------ main */
#main{flex:1;height:100%;display:flex;flex-direction:column;position:relative}
#stars{position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;pointer-events:none}
body.perf #stars{display:none}
/* The skyline: one of Apple's ATV aerial clips of New York, hidden behind
   the same travelling diagonal mask that paints the wordmark — the launch
   wash REVEALS the city out of darkness as its front crosses, and the
   colour stays. One video, no grey understudy: revealing beats colourising,
   and it halves the decode. */
#skyline{
  position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;
  overflow:hidden;pointer-events:none;
  /* no transition: starTick writes the zoom transform every frame, and a
     transition here would smear each frame's update over 1.5s */
}
#skyline[hidden]{display:none}
/* arriving late (stream buffering) it eases in rather than popping */
#skyline:not([hidden]){animation:skyFadeIn .8s ease both}
@keyframes skyFadeIn{from{opacity:0}to{opacity:1}}
#skyline video{
  position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;
}
#sky-color{
  filter:brightness(.5) saturate(1.15);
  -webkit-mask-image:linear-gradient(114deg,#000 0 42%,transparent 58% 100%);
          mask-image:linear-gradient(114deg,#000 0 42%,transparent 58% 100%);
  -webkit-mask-size:300% 100%;mask-size:300% 100%;
  -webkit-mask-position:100% 0;mask-position:100% 0;
}
body.painted #sky-color{-webkit-mask-position:0 0;mask-position:0 0}

/* while a query runs the whole backdrop dims 30% — the starburst becomes
   ambience and the answer text owns the contrast */
#skyline,#stars{transition:filter .6s ease}
body.gen #skyline,body.gen #stars{filter:brightness(.7)}

/* macOS-style loading bar while the server warms the skyline cache —
   big, it just says Loading, and it sits BELOW the greeting, centred on
   the MAIN PANEL like the hero text (50% of the viewport is the window's
   centre, which the sidebar pushes visibly off-axis — the --sbw var is
   kept current by setSidebar) */
#skyload{position:fixed;left:calc(50% + var(--sbw,384px)/2);top:57%;
  transform:translateX(-50%);
  z-index:4;width:min(440px,50vw);text-align:center;pointer-events:none}
#skyload[hidden]{display:none}
#skyload .track{height:10px;border-radius:5px;overflow:hidden;
  background:rgba(255,255,255,.14)}
#skyload .fill{height:100%;width:0;border-radius:5px;background:#d6d8de;
  transition:width .5s ease}
#skyload .lbl{margin-top:10px;font-size:14px;letter-spacing:.16em;
  text-transform:uppercase;color:#a8a8a8;font-family:var(--mono)}
/* the band crosses the full viewport ~0.55s..2.0s; the backdrop's reveal
   follows it edge-for-edge, unlike the wordmark's tighter window */
body.painting #sky-color{
  transition:-webkit-mask-position 4.2s linear .3s,mask-position 4.2s linear .3s;
}
body.perf #skyline{display:none}
#chat-scroll{flex:1;overflow-y:auto;overflow-x:hidden;scroll-behavior:smooth;position:relative;z-index:1}
body.perf #chat-scroll{scroll-behavior:auto}
#chat-inner{
  max-width:780px;margin:0 auto;padding:36px 24px 150px;
  -webkit-user-select:text;user-select:text;   /* chat is copyable */
}

#hero{
  min-height:60vh;display:flex;flex-direction:column;
  align-items:center;justify-content:center;text-align:center;gap:10px;
}
/* one typeface across the whole landing screen */
#hero,#hero h1,#hero p{font-family:var(--helv)}
/* The wordmark is a neon sign. Unpowered it is a grey glass tube; the launch
   sweep is the power arriving — it paints the letters, the tube catches with
   a strike flicker, then hums. Two pseudo-copies of the same text do all of
   it: ::before is the glow (same travelling gradient, heavily blurred, behind
   the glyphs), ::after is the lit tube (crisp). Both are revealed through the
   same diagonal mask that rides with the band, so glow and colour arrive
   together under it. */
#hero h1{
  font-size:132px;font-weight:700;letter-spacing:-.012em;
  position:relative;z-index:0;color:#9a9a9a;-webkit-text-fill-color:#9a9a9a;
}
/* The halo is a REAL child element (.halo > span), not ::before: Blink
   drops background-clip:text when the SAME box also carries a filter, so
   a blurred ::before rendered as a rectangular fog on the web. Splitting
   them — blur on the wrapper, gradient-clip on the inner span — renders
   a text-shaped glow in every engine. */
#hero h1::after,#hero h1 .halo span{
  position:absolute;left:0;top:0;white-space:nowrap;pointer-events:none;
  /* tile starts and ends on the same color; sliding one full tile
     (background-size 200% -> position 200%) loops seamlessly */
  background:linear-gradient(90deg,#ff8f8f,#ffc46e,#f5e663,#7ef0a6,
             #6ec7ff,#8f9dff,#c98fff,#ff8fd8,#ff8f8f);
  background-size:200% 100%;
  -webkit-background-clip:text;background-clip:text;
  color:transparent;-webkit-text-fill-color:transparent;
  animation:rainbow 16s linear infinite;
  /* the mask is far wider than the text and slides across it: the opaque
     half trails the band, the transparent half runs ahead of it */
  -webkit-mask-image:linear-gradient(114deg,#000 0 42%,transparent 58% 100%);
          mask-image:linear-gradient(114deg,#000 0 42%,transparent 58% 100%);
  -webkit-mask-size:300% 100%;mask-size:300% 100%;
  -webkit-mask-position:100% 0;mask-position:100% 0;
}
#hero h1::after{content:attr(data-word)}
/* the tube's halo: the same travelling colours, thrown 16px */
#hero h1 .halo{
  position:absolute;left:0;top:0;z-index:-1;opacity:1;
  pointer-events:none;
  filter:blur(19px) saturate(1.55);
}
#hero h1 .halo span{position:static;display:block}
/* once painted it stays painted */
body.painted #hero h1 .halo span,body.painted #hero h1::after{
  -webkit-mask-position:0 0;mask-position:0 0;
}
body.painting #hero h1 .halo span,body.painting #hero h1::after{
  transition:-webkit-mask-position .55s linear,mask-position .55s linear;
  transition-delay:2.15s;
}
body.painting #hero h1::after{
  animation:rainbow 16s linear infinite,neonCatch 1s 2.75s both;
}
@keyframes neonCatch{
  0%{opacity:1}8%{opacity:.15}16%{opacity:1}28%{opacity:.45}
  36%{opacity:1}46%{opacity:.82}56%,100%{opacity:1}
}
body.painting #hero h1 .halo{animation:neonCatchGlow 1s 2.75s both}
@keyframes neonCatchGlow{
  0%{opacity:1}8%{opacity:.12}16%{opacity:1}28%{opacity:.4}
  36%{opacity:1}46%{opacity:.8}56%,100%{opacity:1}
}
@keyframes rainbow{from{background-position:0% 50%}to{background-position:200% 50%}}
body.perf #hero h1{animation:none}
/* performance mode skips the theatre — show it lit immediately */
body.perf #hero h1 .halo span,body.perf #hero h1::after{
  animation:none;-webkit-mask-position:0 0;mask-position:0 0;
}
#hero p{color:var(--dim);font-size:15px}
/* the greeting reads as a headline, not a caption */
#hero .greet{
  font-family:ui-serif,Georgia,'Times New Roman',serif;
  font-size:44px;font-weight:400;letter-spacing:-.01em;
  color:#eceade;margin-top:22px;
}
/* the wordmark centres on its own; LIVE is pulled out of the flow so it
   sits further right without dragging the title off-centre */
#hero .h1row{display:flex;align-items:center;justify-content:center;position:relative}
/* subdued deep-blue accents — deliberately quiet next to the wordmark */
#hero .beta-tag{
  font-family:var(--helv);font-weight:600;color:#8e8e8e;
  letter-spacing:.32em;text-transform:uppercase;
}
#hero .beta-tag{font-size:11px;margin:6px 0 8px;padding-left:.32em}
#hero .beta-tag .vnum{color:#c9c9c9;font-weight:700;letter-spacing:.18em}

.msg{margin-bottom:26px;animation:rise .25s ease both}
body.perf .msg{animation:none}
@keyframes rise{from{opacity:0;transform:translateY(6px)}}
.msg .who{
  font-family:var(--mono);font-size:10px;letter-spacing:.14em;
  text-transform:uppercase;margin-bottom:7px;color:var(--faint);
}
.msg.user .who{color:var(--teal)}
.msg.ai .who{color:var(--accent)}
.msg .body{
  font-family:var(--helv);font-size:15.5px;line-height:1.75;
  letter-spacing:.002em;
  font-kerning:normal;text-rendering:optimizeLegibility;
  -webkit-font-smoothing:antialiased;
}
.msg.user .body{
  background:rgba(12,13,17,.38);border:1px solid rgba(255,255,255,.10);
  -webkit-backdrop-filter:blur(16px) saturate(1.2);
          backdrop-filter:blur(16px) saturate(1.2);
  border-radius:var(--radius);padding:12px 16px;white-space:pre-wrap;
}
.msg.ai .body{padding:0 2px}
.msg .meta{
  font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-top:8px;
}
.msg .meta b{color:var(--accent);font-weight:500}

.body p{margin:0 0 1.05em}
.body p:last-child{margin-bottom:0}
/* a real hierarchy instead of three identical sizes */
.body h1,.body h2,.body h3{
  font-family:var(--helv);font-weight:600;color:#fff;
  line-height:1.3;letter-spacing:-.005em;
  margin:1.8em 0 .6em;
}
.body h1{font-size:21px}
.body h2{font-size:18px}
.body h3{font-size:16px;color:var(--text)}
.body>h1:first-child,.body>h2:first-child,.body>h3:first-child{margin-top:0}
.body ul,.body ol{margin:0 0 1.05em;padding-left:1.35em}
.body li{margin-bottom:.42em;padding-left:.15em}
.body li:last-child{margin-bottom:0}
.body li>ul,.body li>ol{margin:.42em 0 0}
.body ul li::marker{color:var(--faint)}
.body ol li::marker{color:var(--faint);font-variant-numeric:tabular-nums}
.body blockquote{
  margin:0 0 1.05em;padding:.15em 0 .15em 1.1em;color:var(--dim);
  border-left:2px solid var(--line);font-style:normal;
}
.body hr{
  border:0;border-top:1px solid var(--line-soft);margin:1.6em 0;
}
.body table{
  border-collapse:collapse;width:100%;margin:0 0 1.05em;
  font-size:14px;display:block;overflow-x:auto;
}
.body th,.body td{
  text-align:left;padding:9px 12px;
  border-bottom:1px solid var(--line-soft);vertical-align:top;
}
.body th{
  font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--faint);font-weight:600;
  border-bottom:1px solid var(--line);white-space:nowrap;
}
.body tbody tr:last-child td{border-bottom:none}
.body code{
  font-family:var(--mono);font-size:12.5px;background:var(--panel2);
  border:1px solid var(--line-soft);padding:1.5px 5px;border-radius:4px;
  color:var(--accent-hot);
}
/* content panels are SMOKED GLASS, not drywall: barely-there black with
   a heavy blur doing the readability, so the warp lives behind the text */
.body pre{
  background:rgba(8,9,12,.32);border:1px solid rgba(255,255,255,.09);
  -webkit-backdrop-filter:blur(16px) saturate(1.2);
          backdrop-filter:blur(16px) saturate(1.2);
  border-radius:var(--radius);padding:13px 15px;overflow-x:auto;margin:0 0 10px;
}
.body pre code{background:none;border:none;padding:0;color:var(--text);font-size:12.5px}
.body strong{color:#fff;font-weight:600}
.body em{color:var(--text)}
.body a{color:var(--accent-hot);text-decoration:none;
  border-bottom:1px solid rgba(255,255,255,.22)}
.body a:hover{border-bottom-color:currentColor}
.body details{
  border:1px solid rgba(255,255,255,.09);border-radius:8px;
  margin:0 0 10px;background:rgba(8,9,12,.32);
  -webkit-backdrop-filter:blur(16px) saturate(1.2);
          backdrop-filter:blur(16px) saturate(1.2);
}
.body details summary{
  cursor:pointer;padding:8px 12px;font-family:var(--mono);
  font-size:11px;color:var(--faint);letter-spacing:.08em;user-select:none;
}
.body details[open] summary{border-bottom:1px solid var(--line-soft);color:var(--dim)}
.body details .think-body{padding:10px 14px;color:var(--dim);font-size:13.5px;line-height:1.6}
/* ---- who contributed to a blended answer */
.contrib{margin:0 0 10px}
.contrib>summary{
  cursor:pointer;list-style:none;display:inline-flex;align-items:center;gap:7px;
  font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;
  color:var(--faint);padding:3px 0;user-select:none;
}
.contrib>summary::-webkit-details-marker{display:none}
.contrib>summary:hover{color:var(--dim)}
.contrib>summary .caretmark{transition:transform .16s}
.contrib[open]>summary .caretmark{transform:rotate(90deg)}


/* pasted images: chips above the composer, thumbnails in the sent bubble */
#imgchips{max-width:780px;margin:0 auto 8px;pointer-events:auto;
  display:flex;gap:8px}
#imgchips[hidden]{display:none}
.imgchip{position:relative;display:inline-block}
.imgchip img{height:56px;border-radius:10px;border:1px solid var(--line);
  display:block}
.imgchip b{position:absolute;top:-7px;right:-7px;width:20px;height:20px;
  border-radius:50%;background:#2a2a2a;border:1px solid var(--line);
  color:var(--dim);font-size:12px;line-height:18px;text-align:center;
  cursor:pointer}
.imgchip b:hover{color:#fff}
.docchip{position:relative;display:inline-flex;align-items:center;gap:6px;
  height:34px;padding:0 14px 0 10px;border-radius:10px;font-size:12.5px;
  color:var(--text);border:1px solid var(--line);background:rgba(21,23,29,.55);
  -webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
  max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.docchip b{position:absolute;top:-7px;right:-7px;width:20px;height:20px;
  border-radius:50%;background:#20232b;border:1px solid var(--line);
  color:var(--dim);font-size:12px;line-height:18px;text-align:center;
  cursor:pointer}
.docchip b:hover{color:#fff}
.sentimgs{display:flex;gap:8px;margin-top:8px}
.sentimgs img{max-height:140px;max-width:46%;border-radius:12px;
  border:1px solid var(--line)}

/* the blend progress bar — replaces live draft output entirely */
.blendprog{margin:10px 0 16px;max-width:520px}
.blendprog .track{height:4px;border-radius:2px;overflow:hidden;
  background:rgba(255,255,255,.10)}
.blendprog .fill{height:100%;width:0;border-radius:2px;
  background:linear-gradient(90deg,var(--accent),var(--accent-hot));
  box-shadow:0 0 12px var(--accent-dim)}
.blendprog .lbl{font-family:var(--mono);font-size:13px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--dim);margin-bottom:9px}
.blendprog .track{height:9px;border-radius:5px;overflow:hidden;
  background:rgba(255,255,255,.12)}
.blendprog .fill{height:100%;width:0;border-radius:5px;background:#d6d8de;
  transition:width .5s ease}

.draft{
  border-left:2px solid var(--line);margin:8px 0 0;padding:2px 0 2px 12px;
  animation:draftIn .32s ease-out both;
}
@keyframes draftIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.draft .dm{
  font-family:var(--mono);font-size:10px;letter-spacing:.08em;
  color:var(--accent);text-transform:uppercase;
}
.draft .dt{color:var(--dim);font-size:13px;line-height:1.55;margin-top:3px;
  max-height:150px;overflow:hidden;white-space:pre-wrap}
.draft.empty .dt{color:var(--faint);font-style:italic}
body.perf .draft{animation:none}

.statusline{
  display:block;font-family:var(--mono);font-size:11px;
  color:var(--accent);margin-bottom:9px;letter-spacing:.04em;
}
body:not(.perf) .statusline{animation:blink 1.4s ease infinite}
.model.picked{
  color:var(--text);background:var(--accent-dim);
  border-color:rgba(255,255,255,.22);
}
.model .memtag{
  font-family:var(--mono);font-size:7px;letter-spacing:.02em;
  color:var(--red);white-space:nowrap;flex-shrink:0;
}
.model .rank{
  font-family:var(--mono);font-size:9px;color:var(--accent);
  border:1px solid rgba(255,255,255,.3);border-radius:3px;
  padding:0 3px;margin-left:6px;
}
.websrc{
  display:inline-block;font-family:var(--mono);font-size:10px;
  letter-spacing:.08em;color:var(--teal);background:rgba(255,255,255,.07);
  border:1px solid rgba(255,255,255,.18);border-radius:5px;
  padding:2px 7px;margin-bottom:9px;
}
.caret{
  display:inline-block;width:8px;height:15px;background:var(--accent);
  vertical-align:-2px;margin-left:2px;border-radius:1px;
}
body:not(.perf) .caret{animation:blink .9s step-end infinite}

/* -------------------------------------------------------------- composer */
#composer-wrap{
  position:absolute;left:0;right:0;bottom:0;z-index:2;
  padding:0 24px 22px;pointer-events:none;
  background:linear-gradient(transparent,var(--bg) 55%);
}
body.perf #composer-wrap{background:var(--bg);border-top:1px solid var(--line-soft);padding-top:14px}
#skyline video{transition:transform 1.1s cubic-bezier(.22,.61,.36,1);
  will-change:transform}
/* thinking: the city steps back so the words own the screen */
#skyline{transition:filter .9s ease,opacity .9s ease}
body.gen #skyline{filter:brightness(.62) saturate(.9)}
/* streaming: the newest line emerges from nothing instead of snapping in */
.msg.ai.live .body{
  -webkit-mask-image:linear-gradient(to bottom,#000 calc(100% - 2.4em),
    rgba(0,0,0,.35) calc(100% - .8em),transparent 100%);
          mask-image:linear-gradient(to bottom,#000 calc(100% - 2.4em),
    rgba(0,0,0,.35) calc(100% - .8em),transparent 100%);
}
.msg.ai .body{animation:answerIn .5s ease both}
@keyframes answerIn{from{opacity:0;transform:translateY(3px)}
                    to{opacity:1;transform:none}}
body.perf .msg.ai .body{animation:none}
#composer:focus-within{border-color:rgba(255,255,255,.28);
  box-shadow:0 0 0 1px var(--accent-dim),
             0 10px 34px -14px var(--bwglow,rgba(150,160,255,.4))}
#send{transition:transform .18s ease,box-shadow .25s ease}
#send:hover{transform:translateY(-1px);
  box-shadow:0 4px 16px -6px var(--accent-hot)}
#composer{
  max-width:780px;margin:0 auto;pointer-events:auto;
  background:var(--panel2);border:1px solid var(--line);
  border-radius:20px;display:flex;align-items:flex-end;gap:6px;
  padding:9px 10px;transition:border-color .15s;
}
#composer:focus-within{border-color:var(--accent)}
body.gen #composer{
  border-color:var(--accent);
  box-shadow:0 0 34px rgba(255,255,255,.10),0 0 90px rgba(255,255,255,.05);
}
body.gen #chip-model{color:var(--accent)}
body.perf #composer{box-shadow:none}
#input{
  flex:1;background:none;border:none;outline:none;resize:none;
  color:var(--text);font:15px/1.5 var(--sans);max-height:180px;
  padding:6px 4px;
}
#input::placeholder{color:var(--faint)}
#cbtns{display:flex;align-items:center;gap:2px;flex-shrink:0;
  margin-left:auto;padding-left:4px}
.cbtn{
  width:34px;height:34px;border-radius:9px;border:none;cursor:pointer;
  display:flex;align-items:center;justify-content:center;font-size:15px;
  background:none;color:rgba(255,255,255,.78);transition:all .13s;
  flex-shrink:0;
}
.cbtn svg{width:18px;height:18px;display:block}
.cbtn:hover{color:#fff;background:rgba(255,255,255,.10)}
#send{background:var(--accent);color:#1a1a1a;font-weight:700;font-size:16px}
#send svg{width:17px;height:17px}
#send:hover{background:var(--accent-hot);color:#000}
#send:disabled{background:var(--line);color:var(--faint);cursor:default}
#send.stop{background:var(--red);color:#fff;font-size:11px}
#mic.rec{color:var(--red);background:rgba(226,109,90,.14)}
#voicebtn svg{width:17px;height:17px}
#voicebtn.on{color:var(--accent-hot);background:var(--accent-dim)}
body:not(.perf) #mic.rec{animation:blink 1s ease infinite}
#model-chip{
  max-width:780px;margin:0 auto 8px;pointer-events:auto;
  font-family:var(--mono);font-size:10.5px;color:var(--faint);
  padding:0 4px;display:flex;gap:6px;
}
#model-chip b{color:var(--dim);font-weight:500}

/* -------------------------------------------------------------- about */
#dlhelp-veil{position:fixed;inset:0;z-index:61;display:flex;
  align-items:center;justify-content:center;background:rgba(6,7,10,.72);
  -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px)}
#dlhelp-veil[hidden]{display:none}
#dlhelp-card{max-width:430px;margin:24px;padding:26px 26px 20px;
  background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);text-align:center;
  animation:doorPop .5s cubic-bezier(.16,1,.3,1) both}
#dlhelp-card .sh-icon{font-size:30px;margin-bottom:4px}
#dlhelp-card h2{margin:0 0 10px;font-size:20px}
#dlhelp-card p{color:var(--dim);font-size:13.5px;line-height:1.75;
  margin:0;text-align:left}
#dlhelp-card b{color:var(--text)}
#dlhelp-card .sh-foot{display:flex;gap:10px;margin-top:20px}
#dlhelp-card button{flex:1;padding:11px 14px;border-radius:10px;
  border:none;background:var(--accent);color:#1a1a1a;font-weight:700;
  font-size:13.5px;cursor:pointer}
#share-veil{position:fixed;inset:0;z-index:60;display:flex;
  align-items:center;justify-content:center;background:rgba(6,7,10,.72);
  -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px)}
#share-veil[hidden]{display:none}
#share-card{max-width:420px;margin:24px;padding:26px 26px 20px;
  background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);text-align:center;
  animation:doorPop .5s cubic-bezier(.16,1,.3,1) both}
@keyframes doorPop{from{opacity:0;transform:translateY(18px) scale(.97)}
                   to{opacity:1;transform:none}}
#share-card .sh-icon{font-size:34px;margin-bottom:6px}
#share-card h2{margin:0 0 8px;font-size:21px}
#share-card p{color:var(--dim);font-size:13.5px;line-height:1.6;margin:0}
#share-card .sh-foot{display:flex;gap:10px;margin-top:20px}
#share-card button{flex:1;padding:11px 14px;border-radius:10px;
  border:1px solid var(--line);background:none;color:var(--dim);
  font-size:13.5px;cursor:pointer}
#share-card button.primary{background:var(--accent);color:#1a1a1a;
  border:none;font-weight:700}
#share-card button:hover{color:var(--text)}
#new-veil,#update-veil,#about-veil{
  position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.66);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
  display:flex;align-items:center;justify-content:center;
}
#new-veil[hidden],#update-veil[hidden],#about-veil[hidden]{display:none}
#about-card{
  width:340px;max-height:min(86vh,760px);
  background:var(--panel2);border:1px solid var(--line);
  border-radius:16px;text-align:center;
  box-shadow:0 24px 80px rgba(0,0,0,.6);
  display:flex;flex-direction:column;overflow:hidden;
}
/* the small dialogs (update, new models) have no head/body/foot — give
   them their own padding, or their buttons run to the card's edge */
#update-veil #about-card,#new-veil #about-card{
  padding:24px 22px 18px;max-height:none;
}
#update-veil .about-btn,#new-veil .about-btn{margin-top:10px}
#about-head{padding:22px 24px 12px;flex:none}
#about-body{
  padding:0 24px;overflow-y:auto;flex:1 1 auto;min-height:0;
  scrollbar-width:thin;
}
#about-body::-webkit-scrollbar{width:8px}
#about-body::-webkit-scrollbar-thumb{
  background:rgba(255,255,255,.14);border-radius:4px}
#about-foot{
  padding:12px 24px 18px;flex:none;
  border-top:1px solid var(--line-soft);background:var(--panel2);
}
#about-foot .about-btn{margin-top:0}
#about-icon{width:54px;height:54px;margin-bottom:8px}
#persona-label{
  font-family:var(--mono);font-size:10px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--faint);text-align:left;
  margin:16px 0 6px;
}
#persona{
  width:100%;resize:none;padding:10px 12px;
  font:13.5px/1.55 var(--helv);color:var(--text);
  background:var(--panel);border:1px solid var(--line);border-radius:10px;
  outline:none;
}
#persona:focus{border-color:var(--dim)}
#persona::placeholder{color:var(--faint)}
#about-name{font-family:var(--helv);font-size:24px;font-weight:600;color:var(--text)}
#about-name em{font-style:italic;font-weight:400;opacity:.85}
#about-ver,#up-ver{font-family:var(--helv);font-size:14px;color:var(--dim);margin-top:6px}
#up-detail{font-size:11.5px;color:var(--faint);margin:10px 0 4px;line-height:1.5}
#about-sub{font-size:11.5px;color:var(--faint);margin-top:10px;line-height:1.5}
#new-list{margin:16px 0 2px;text-align:left}
#new-list .mrow{
  display:flex;align-items:baseline;justify-content:space-between;gap:14px;
  padding:8px 2px;border-bottom:1px solid var(--line-soft);
}
#new-list .mrow:last-child{border-bottom:none}
#new-list .mname{
  font-family:var(--helv);font-size:13.5px;color:var(--text);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
#new-list .msize{
  font-family:var(--mono);font-size:11px;color:var(--faint);flex:none;
  font-variant-numeric:tabular-nums;
}
#new-list .mmore{
  padding:10px 2px 0;font-size:11.5px;color:var(--faint);text-align:center;
}
#turbo-row[hidden]{display:none}
#turbo-row{display:flex;gap:8px;align-items:flex-start;font-size:11.5px;
  color:var(--dim);margin:12px 2px 2px;cursor:pointer;line-height:1.5;
  text-align:left}
#turbo-row input,#contrib-row input,#nolimits-row input{
  appearance:none;-webkit-appearance:none;
  width:17px;height:17px;flex:none;margin:0;border-radius:5px;
  border:1.5px solid var(--faint);background:rgba(255,255,255,.04);
  cursor:pointer;position:relative;transition:all .15s;
}
#turbo-row input:hover,#contrib-row input:hover,#nolimits-row input:hover{
  border-color:var(--accent-hot)}
#turbo-row input:checked,#contrib-row input:checked,
#nolimits-row input:checked{
  background:var(--accent);border-color:var(--accent)}
#turbo-row input:checked::after,#contrib-row input:checked::after,
#nolimits-row input:checked::after{
  content:"";position:absolute;left:5px;top:1.5px;
  width:4px;height:9px;border:solid #14161c;
  border-width:0 2.2px 2.2px 0;transform:rotate(45deg)}
#turbo-row,#contrib-row,#nolimits-row{
  display:flex;align-items:center;gap:10px;font-size:13px;
  color:var(--text);padding:8px 2px;cursor:pointer;line-height:1.4}
#turbo-row[hidden]{display:none}
.hint{
  font-style:normal;width:15px;height:15px;flex:none;cursor:help;
  border:1px solid var(--line);border-radius:50%;color:var(--faint);
  font-size:10px;line-height:13px;text-align:center;
  font-family:var(--helv);margin-left:auto;
}
.hint:hover{color:var(--text);border-color:var(--dim)}

#fleet-box{margin:14px 0 4px;text-align:left}
#fleet-own{font-family:var(--mono);font-size:10.5px;color:var(--dim);
  margin-bottom:8px;line-height:1.6}
#contrib-state{font-family:var(--mono);font-size:10px;color:var(--faint);
  margin:4px 0 6px;min-height:12px}
#fleet-pending .preq{display:flex;align-items:center;gap:8px;
  font-size:12.5px;color:var(--text);margin-bottom:6px}
#fleet-pending .preq button{margin-left:auto;padding:5px 12px;
  border-radius:8px;border:none;background:var(--accent);color:#1a1a1a;
  font-weight:600;cursor:pointer}
#adv-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
#adv-grid .about-btn{margin-top:0}
#fleet-adv{margin-top:6px}
#fleet-adv summary{font-family:var(--mono);font-size:9.5px;
  color:var(--faint);cursor:pointer;letter-spacing:.1em}
#fleet-box input{
  width:100%;box-sizing:border-box;margin-bottom:6px;padding:8px 10px;
  background:var(--panel2);border:1px solid var(--line);border-radius:8px;
  color:var(--text);font-size:12.5px;outline:none;
}
#fleet-box input:focus{border-color:var(--accent-dim)}
#about-facts{
  font-family:var(--mono);font-size:11.5px;font-weight:700;
  color:var(--dim);margin-top:10px;line-height:1.6;
}
.about-btn{
  display:block;width:100%;margin-top:8px;padding:9px 12px;
  font:500 13.5px var(--helv);cursor:pointer;color:var(--text);
  background:none;border:1px solid var(--line);border-radius:10px;
  transition:background .13s,border-color .13s;
}
.about-btn:hover{background:var(--panel);border-color:var(--dim)}
.about-btn.primary{background:var(--accent);color:#1a1a1a;border:none;margin-top:14px}
.about-btn.primary:hover{background:var(--accent-hot);color:#000}
.about-btn.quiet{border:none;color:var(--faint);font-size:12px;padding:5px;margin-top:4px}
.about-btn.quiet:hover{background:none;color:var(--dim)}

/* ------------------------------------ downloads-complete celebration */
/* card lifts away like a macOS sheet, a rainbow sweeps the window, then
   collapses into the wordmark */
#setup-card.done{animation:cardPoof .9s cubic-bezier(.2,.7,.3,1) forwards}
@keyframes cardPoof{
  40%{transform:scale(1.06);opacity:1}
  100%{transform:scale(1.5);opacity:0;filter:blur(10px)}
}
#setup-veil.fading{animation:veilOut .9s ease forwards;pointer-events:none}
@keyframes veilOut{to{opacity:0}}

#celebrate{position:fixed;inset:0;z-index:90;pointer-events:none;overflow:hidden}
#celebrate[hidden]{display:none}
/* a diagonal band of light that travels across the window */
/* What made this read as a ribbon dragged over the window: evenly spaced
   colour stops at uniform opacity, a light blur, and hard rectangular ends
   that ran off the screen. So the stops are now unevenly spaced and each
   carries its own alpha, the blur is heavy enough to dissolve the banding,
   and an elliptical mask fades the whole thing out at its edges — closer to
   light spilling across the window than a strip passing over it. */
/* A WASH, not a pass. The colour field is stationary and fills the window;
   what moves is only a hugely feathered reveal front (a 44%-wide soft edge
   in the mask), crossing over ~4.2s. Once fully arrived the colour
   dissolves in place — nothing ever slides off-screen, so nothing reads as
   an object passing by. A gentle scale-breathe keeps the field liquid. */
#celebrate .sweep{
  position:absolute;top:-8%;left:-8%;width:116%;height:116%;
  background:linear-gradient(114deg,#ff8f8f,#ffc46e,#f5e663,#7ef0a6,
             #6ec7ff,#8f9dff,#c98fff,#ff8fd8);
  opacity:0;mix-blend-mode:screen;filter:saturate(1.2) blur(2px);
  -webkit-mask-image:linear-gradient(114deg,#000 0 28%,transparent 72% 100%);
          mask-image:linear-gradient(114deg,#000 0 28%,transparent 72% 100%);
  -webkit-mask-size:320% 100%;mask-size:320% 100%;
  -webkit-mask-position:100% 0;mask-position:100% 0;
  animation:washIn 4.2s linear .3s both,
            washBreathe 6.4s ease-in-out both,
            washOut 1.5s ease 4.7s forwards;
}
@keyframes washIn{
  from{-webkit-mask-position:100% 0;mask-position:100% 0;opacity:.82}
  to  {-webkit-mask-position:0 0;mask-position:0 0;opacity:.82}
}
@keyframes washOut{to{opacity:0}}
@keyframes washBreathe{
  0%{transform:scale(1)}55%{transform:scale(1.045)}100%{transform:scale(1.01)}
}
}
/* The wordmark is *deposited* by the sweep: it rushes in oversized and
   blurred and lands just as the band crosses the middle of the window.
   The colour layers live on the pseudo-elements, so animating the h1 itself
   here collides with nothing. */
/* Timing is `linear` on purpose — the deceleration is written into the
   keyframes instead. An eased curve here is far too front-loaded: the
   wordmark had already settled by 0.35s, well before the band reached it, so
   it read as a separate event rather than as something the sweep delivered.
   These stops put it at ~1.7x when the band is entering and landing at
   ~0.8s, exactly when the band crosses the middle. */
/* THE ENTRANCE, serene cut: no shockwave, no quake, no chromatic snap —
   the wordmark drifts in from a deep blur over 2.6s, decelerating, and
   LANDS at the exact moment the wash (delay 2.15s + .55s sweep) paints
   the colour through it. Slow zoom + unblur + get swiped into colour. */
#hero h1.flyin{animation:heroIn 2.6s linear both}
@keyframes heroIn{
  0%  {opacity:0;transform:scale(1.6) translateY(10px);filter:blur(26px)}
  14% {opacity:1}
  35% {transform:scale(1.34) translateY(6px);filter:blur(15px)}
  60% {transform:scale(1.15) translateY(3px);filter:blur(7px)}
  82% {transform:scale(1.045) translateY(1px);filter:blur(2px)}
  100%{opacity:1;transform:scale(1) translateY(0);filter:blur(0)}
}
/* the small type follows a beat later, so the screen assembles rather than
   simply appearing all at once */
#hero .beta-tag.flyin,#hero .greet.flyin{
  animation:heroRise .7s cubic-bezier(.2,.8,.3,1) .34s both;
}
@keyframes heroRise{
  from{opacity:0;transform:translateY(9px)}
  to  {opacity:1;transform:translateY(0)}
}



/* ------------------------------------------------------- first-run setup */
#setup-veil{
  position:fixed;inset:0;z-index:50;background:rgba(0,0,0,.66);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
  display:flex;align-items:center;justify-content:center;
}
#setup-veil[hidden]{display:none}
#setup-card{
  width:440px;max-width:calc(100vw - 40px);background:var(--panel2);
  border:1px solid var(--line);border-radius:14px;padding:22px 22px 18px;
  box-shadow:0 24px 80px rgba(0,0,0,.55);
}
#setup-card h2{font-size:19px;margin-bottom:6px}
#setup-card .sub{color:var(--dim);font-size:13px;line-height:1.55;margin-bottom:16px}
.setup-row{
  display:grid;grid-template-columns:1fr auto;gap:3px 10px;
  margin-bottom:12px;font-size:13.5px;
}
.setup-row .nm{color:var(--text)}
.setup-row .st{font-family:var(--mono);font-size:11px;align-self:center}
.setup-row.done{align-items:center}
.setup-row .tick{width:17px;height:17px;align-self:center;flex-shrink:0}
.setup-row .tick circle{fill:#3ecf8e}
.setup-row .tick path{stroke:var(--panel2)}
.setup-row .st.dl{color:var(--accent)}
.setup-row .st.err{color:var(--red);cursor:help}
.setup-row .st.wait{color:var(--faint)}
.setup-row .bar{
  grid-column:1/-1;height:5px;background:var(--line-soft);
  border-radius:3px;overflow:hidden;
}
.setup-row .bar i{
  display:block;height:100%;width:0;border-radius:3px;
  background:linear-gradient(90deg,var(--accent),var(--teal));
  transition:width .6s ease;
}
.big-bar{height:10px;background:var(--line-soft);border-radius:6px;overflow:hidden;margin:6px 0 10px}
.big-bar i{display:block;height:100%;width:0;border-radius:6px;
  background:linear-gradient(90deg,#8e8e8e,#ececec);transition:width .5s ease}
.big-stat{display:flex;justify-content:space-between;font-family:var(--mono);
  font-size:11px;color:var(--dim)}
.big-speed{font-family:var(--mono);font-size:11px;color:var(--teal);margin-top:6px}
.setup-head{
  font-family:var(--mono);font-size:9.5px;letter-spacing:.14em;
  color:var(--faint);text-transform:uppercase;margin:14px 0 8px;
}
.setup-head:first-child{margin-top:0}
.setup-row.clickable{cursor:pointer;border-radius:6px}
.setup-row.clickable:hover{background:var(--accent-dim)}
.setup-row .st.get{color:var(--accent)}
#setup-list{max-height:46vh;overflow-y:auto;margin-right:-6px;padding-right:6px}
.setup-fold{margin:10px 0 0}
.setup-fold summary{
  cursor:pointer;font-family:var(--mono);font-size:10.5px;
  letter-spacing:.14em;text-transform:uppercase;color:var(--faint);
  padding:6px 0;user-select:none;list-style:none;
}
.setup-fold summary::before{content:"\25b8  ";font-size:9px}
.setup-fold[open] summary::before{content:"\25be  "}
.setup-fold summary:hover{color:var(--dim)}
#setup-note{color:var(--faint);font-size:11.5px;margin-top:10px;font-family:var(--mono)}
#setup-foot{display:flex;gap:10px;justify-content:flex-end;margin-top:14px}
#setup-foot button{
  font:600 13px var(--sans);padding:9px 15px;border-radius:9px;cursor:pointer;
  border:1px solid var(--line);background:none;color:var(--dim);
  transition:all .13s;
}
#setup-foot button:hover{color:var(--text);border-color:var(--dim)}
.plans{display:flex;gap:10px;margin:14px 0 4px}
.plan{flex:1;border:1px solid var(--line);border-radius:12px;
  padding:12px 12px 10px;cursor:pointer;transition:all .15s;
  display:flex;flex-direction:column;gap:4px}
.plan b{font-size:15px}
.plan span{font-size:11.5px;color:var(--dim);line-height:1.35}
.plan em{font-style:normal;font-family:var(--mono);font-size:10.5px;
  color:var(--faint)}
.plan:hover{border-color:var(--accent-hot)}
.plan.on{border-color:var(--accent-hot);background:var(--accent-dim)}
.plan.done{opacity:.45;cursor:default}
.plan.done:hover{border-color:var(--line)}
#nolimits-row{display:flex;gap:8px;align-items:flex-start;
  font-size:11px;color:var(--faint);margin:10px 2px 0;cursor:pointer;
  line-height:1.5;text-align:left}
#setup-go{background:var(--accent);color:#1a1a1a;border:none}
#setup-go:hover{background:var(--accent-hot);color:#000}
#setup-go:disabled{opacity:.55;cursor:default}

@media(max-width:760px){
  #sidebar{display:none}
  #chat-inner{padding:24px 14px 150px}
}
/* ------------------------------------------------------------- mobile */
/* On a phone the 284px sidebar swallowed the screen — "doesn't work on
   my iPhone" was a layout catastrophe, not a bug. Under 700px the
   sidebar becomes a slide-in drawer behind a ☰ button, main owns the
   full width, and the hero scales to fit. */
#mburger{display:none}
@media (max-width:700px){
  #sidebar{
    position:fixed;left:0;top:0;bottom:0;z-index:60;
    width:300px!important;min-width:300px!important;
    transform:translateX(-105%);transition:transform .28s ease;
    box-shadow:8px 0 40px rgba(0,0,0,.45);
  }
  body.sbopen #sidebar{transform:none}
  #mburger{
    display:flex;align-items:center;justify-content:center;
    position:fixed;left:12px;top:12px;z-index:61;width:40px;height:40px;
    border-radius:12px;background:rgba(21,23,29,.55);color:var(--text);
    font-size:19px;cursor:pointer;
    -webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);
  }
  #hero h1{font-size:14vw}
  #hero .greet{font-size:28px}
  #skyload{left:50%;width:min(340px,78vw)}
  #composer-wrap{padding:0 10px 12px}
  #tierpop{left:12px!important;right:12px;max-width:none}
  #hero{padding:0 12px}
  #hero .greet{font-size:24px;margin-top:14px}
}

</style>
</head>
<body>

<div id="mburger" title="Menu">☰</div>

<aside id="sidebar">
  <div id="sb-resize" title="Drag to resize"></div>
  <div id="brand-wrap">
    <div id="brand-row">
    <span class="vghost" title="About MillenAI"><b>MillenAI</b><i>__APP_VER__</i></span>
<button id="newchat" title="New chat">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M13 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/>
        <path d="M18.4 2.6a1.7 1.7 0 0 1 2.4 2.4L12.8 13l-3.2.8.8-3.2z"/>
      </svg>
    </button>
    <button id="settings-btn" title="Settings — preferences &amp; about"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.1"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.01a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.01a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z"/></svg></button>
    <div id="update-flag" hidden title="Install the update">UPDATE</div>
    <div id="models-flag" hidden
         title="More models fit this machine">MODELS AVAILABLE</div>
    <a id="get-app" hidden target="_blank" rel="noopener">DOWNLOAD NOW<i
      title="The desktop version runs on your own computer — faster, private, and it works offline.">i</i></a>
    </div>
  </div>


  <div id="mode-tabs">
    <span class="ltab" data-m="ai">AI</span>
    <span class="ltab" data-m="agents">Agents</span>
  </div>
  <div id="tier-rows">__TIER_ROWS__</div>
  <div id="agents-wrap" hidden>
__AGENT_ROWS__
  </div>

  <div id="model-list">
  <div class="group-label chats">Chats</div>
  <div id="chat-list"></div>

  </div>

  <div id="settings">
    <div class="toggle-row" id="perf-toggle" style="margin-top:14px">
      <div class="switch"></div>
      Performance mode
    </div>
  </div>

  <div id="telemetry">
    <div class="t-head"><span>__CHIP__</span></div>
    <div class="meter-row">
      <div class="meter" id="gpu-meter"></div>
    </div>
    <div class="meter-row">
      <div class="meter-label"><span>MODELS</span>
        <b id="models-up" title="Get more models">&uarr;</b></div>
      <div class="meter" id="models-meter"></div>
    </div>
    <div class="meter-row">
      <div class="meter-label"><span>COMMUNITY GPU</span></div>
      <div class="meter" id="fleet-meter"></div>
    </div>
  </div>
</aside>

<main id="main">
  <div id="skyline" hidden>
    <video id="sky-color" muted loop playsinline></video>
  </div>
  <div id="skyload" hidden>
    <div class="track"><div class="fill"></div></div>
    <div class="lbl">loading the backdrop</div>
  </div>
  <canvas id="stars"></canvas>
  <div id="chat-scroll"><div id="chat-inner">
    <div id="hero">
      <div class="h1row"><h1 data-word="MillenAI">MillenAI<span class="halo" aria-hidden="true"><span>MillenAI</span></span></h1></div>
      <div class="beta-tag">__APP_BETA__</div>
      <p class="greet">What's going on today?</p>
    </div>
  </div></div>

  <div id="composer-wrap">
    <div id="model-chip">engine <b id="chip-model">Llama 3.2 3B</b></div>
    <div id="imgchips" hidden></div>
    <div id="composer">

      <input type="file" id="fpick" multiple hidden
        accept="image/*,.txt,.md,.markdown,.csv,.json,.js,.ts,.py,.html,.css,.log,.sh,.yaml,.yml,.xml,.toml,.rtf">
      <textarea id="input" rows="1" placeholder="How can I help you today?"></textarea>
      <div id="cbtns">
      <button class="cbtn" id="attach" title="Attach a file">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21.4 11.05 12.2 20.2a5.5 5.5 0 0 1-7.78-7.78l9.2-9.19a3.67 3.67 0 0 1 5.18 5.18l-9.2 9.2a1.83 1.83 0 0 1-2.6-2.6l8.5-8.48"/>
        </svg>
      </button>
      <button class="cbtn" id="mic" title="Dictate — speak your message">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
          <rect x="9" y="2.5" width="6" height="11.5" rx="3"/>
          <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0"/>
          <path d="M12 18v3.2"/><path d="M8.6 21.4h6.8"/>
        </svg>
      </button>
      <button class="cbtn" id="voicebtn" title="Voice chat — replies are read aloud">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 10v4"/><path d="M7 6v12"/><path d="M11 3v18"/>
          <path d="M15 7v10"/><path d="M19 10v4"/>
        </svg>
      </button>
      <button class="cbtn" id="send" title="Send">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 19V5.5"/><path d="M5.8 11.7 12 5.4l6.2 6.3"/>
        </svg>
      </button>
      </div>
    </div>
  </div>
</main>

<div id="tierpop" hidden></div>
<div id="celebrate" hidden></div>

<div id="new-veil" hidden>
  <div id="about-card">
    <div id="about-name">New models available</div>
    <div id="up-detail">This version adds models you don&rsquo;t have yet.</div>
    <div id="new-list"></div>
    <button class="about-btn primary" id="new-get">Download</button>
    <button class="about-btn" id="new-skip">Not now</button>
    <button class="about-btn quiet" id="new-off" hidden>Don&rsquo;t remind me again</button>
  </div>
</div>

<div id="update-veil" hidden>
  <div id="about-card">
    <div id="about-name">Update available</div>
    <div id="up-ver"></div>
    <div id="up-detail"></div>
    <div class="big-bar" id="up-bar" hidden><i></i></div>
    <button class="about-btn primary" id="up-go">Update now</button>
    <button class="about-btn" id="up-later">Later</button>
  </div>
</div>

<div id="about-veil" hidden>
  <div id="about-card">
    <div id="about-head">
    <svg id="about-icon" viewBox="0 0 120 120" aria-hidden="true">
      <defs><linearGradient id="ag" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stop-color="#8b5cf6"/><stop offset=".5" stop-color="#7d8fff"/>
        <stop offset="1" stop-color="#4cc9e0"/></linearGradient></defs>
      <rect x="18" y="62" width="14" height="40" rx="6" fill="url(#ag)"/>
      <rect x="39" y="44" width="14" height="58" rx="6" fill="url(#ag)"/>
      <rect x="60" y="30" width="14" height="72" rx="6" fill="url(#ag)"/>
      <rect x="81" y="52" width="14" height="50" rx="6" fill="url(#ag)"/>
      <circle cx="95" cy="24" r="7" fill="#4cc9e0"/>
    </svg>
    <div id="about-name">MillenAI</div>
    <div id="about-ver">Version __APP_VER__</div>
    <div id="about-facts"></div>
    </div>
    <div id="about-body">
    <div id="persona-label">How should MillenAI reply?</div>
    <textarea id="persona" rows="3" maxlength="2000" spellcheck="false"
      placeholder="e.g. Be direct, skip the pleasantries. I work in finance, so assume I know the vocabulary."></textarea>
    <button class="about-btn" id="persona-save">Save preferences</button>
    <label id="contrib-row"><input type="checkbox" id="contrib">
      <span>Contribute GPU power</span><i class="hint" id="contrib-hint"
      title="When this machine is idle, it answers questions for friends on MillenAI — and theirs answer yours. Nothing runs while you are using it.">i</i></label>
    <label id="turbo-row" hidden><input type="checkbox" id="turbo">
      <span>Use cloud power</span><i class="hint" id="turbo-hint"
      title="Answers come from a cloud GPU instead of this Mac — much faster, but your prompts leave this computer while it is on.">i</i></label>
    <div id="fleet-box">
      <div id="fleet-own" hidden><span id="fleet-n"></span></div>
      <div id="fleet-pending"></div>
      <div id="contrib-state"></div>
      <details id="fleet-adv"><summary>advanced</summary>
        <input id="contrib-url" placeholder="Hub URL (blank = default)">
      </details>
    </div>
    <div id="adv-grid">
      <button class="about-btn" id="about-preload">Preload backdrops</button>
      <button class="about-btn" id="open-setup">Model updates&hellip;</button>
      <button class="about-btn" id="about-check">Check for updates</button>
      <button class="about-btn" id="about-forget">Forget me</button>
    </div>
    </div>
    <div id="about-foot">
    <button class="about-btn primary" id="about-close">Close</button>
  </div>
</div>

<div id="dlhelp-veil" hidden>
  <div id="dlhelp-card">
    <div class="sh-icon">&#11015;</div>
    <h2>Downloading&hellip;</h2>
    <p id="dlhelp-body"></p>
    <div class="sh-foot">
      <button id="dlhelp-ok" class="primary">Got it</button>
    </div>
  </div>
</div>

<div id="share-veil" hidden>
  <div id="share-card">
    <div class="sh-icon">&#9889;</div>
    <h2>Share your GPU?</h2>
    <p>When your machine is idle, it can answer questions for friends on
       MillenAI — and theirs can answer yours. Nothing leaves your computer
       unless you turn this on, and you can stop any time in Settings.</p>
    <div class="sh-foot">
      <button id="share-no">Not now</button>
      <button id="share-yes" class="primary">&#9889; Share GPU power</button>
    </div>
  </div>
</div>

<div id="setup-veil" hidden>
  <div id="setup-card">
    <h2 id="setup-title">Updates available</h2>
    <p class="sub" id="setup-sub">New models are ready for this machine.
      They download in the background while you keep chatting.</p>
    <div id="setup-list"></div>
    <label id="nolimits-row"><input type="checkbox" id="nolimits">
      No limits — offer models beyond this machine&rsquo;s memory
      (can swap hard)</label>
    <div id="setup-note"></div>
    <div id="setup-foot">
      <button id="setup-later">Later</button>
      <button id="setup-go">Download</button>
    </div>
  </div>
</div>

<script>
"use strict";
const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);

/* ------------------------------------------------------------- state */
let messages=[], generating=false, abortCtl=null;
let model=localStorage.getItem("millen.model")||"Llama 3.2 3B";
let perf=localStorage.getItem("millen.perf")==="1";
const autoWeb=true;   // live web is ALWAYS on now — no switch
let combine=false;   // superseded by tiers
let voiceChat=localStorage.getItem("millen.voice")==="1";
let statsTimer=null;   // telemetry poll handle; perf mode clears it
let lastModels="";  // line-up the backend actually used
let councilManual=false;
// declared up here: setCombine() runs during boot and reads it, which would
// hit the temporal dead zone if it were declared further down
let engineState={};

/* ------------------------------------------------------- model picker */
// council[0] is the active model and, in combine mode, also the merger
let council=[];
try{council=JSON.parse(localStorage.getItem("millen.council"))||[];}catch(e){}
if(!council.length)council=[model];

function paintModels(){
  model=council[0];
  localStorage.setItem("millen.model",model);
  localStorage.setItem("millen.council",JSON.stringify(council));
  const manual=!tier;            // no tier selected => one model is driving
  $$(".model").forEach(el=>{
    if(!el.dataset.model)return;  // rows without a model manage themselves
    el.classList.toggle("active",manual&&el.dataset.model===council[0]);
    const old=el.querySelector(".rank"); if(old)old.remove();
  });
  $$(".tier").forEach(el=>el.classList.toggle("active",el.dataset.tier===tier));
  $("#chip-model").textContent=tier||model;
}
function selectModel(name){
  if(!name)return;  // rows without a model (Power Mode) don't select
  const st=engineState[name];
  if(st&&st.supported===false)return;         // not runnable on this Mac
  if(st&&!st.up&&st.downloadable&&!st.dl){    // present but not downloaded
    fetch("/api/model/download",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({labels:[name]})}).then(pollEngines);
    return;
  }
  tier="";                       // an explicit pick overrides any tier
  localStorage.setItem("millen.tier","");
  council=[name];
  paintModels();
}
$$(".model").forEach(el=>el.addEventListener("click",()=>selectModel(el.dataset.model)));

/* --------------------------------------------------------- perf mode */
function setPerf(on){
  perf=on; document.body.classList.toggle("perf",on);
  $("#perf-toggle").classList.toggle("on",on);
  localStorage.setItem("millen.perf",on?"1":"0");
  applyStatsPolling();   // hoisted; safe to call before the telemetry block
}
$("#perf-toggle").addEventListener("click",()=>setPerf(!perf));
setPerf(perf);


/* ------------------------------------------------------- voice chat */
function setVoice(on){
  voiceChat=on;$("#voicebtn").classList.toggle("on",on);
  localStorage.setItem("millen.voice",on?"1":"0");
  if(!on)fetch("/api/speak",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({stop:true})});
}
$("#voicebtn").addEventListener("click",()=>setVoice(!voiceChat));
setVoice(voiceChat);

/* --------------------------------------------------------------- tiers */
// Fast / Pro / Thinking replace hand-picking models. The backend resolves
// each tier to whatever is downloaded and fits RAM, and Gemma blends.
let agent="";           // declared early: setTier reads it (TDZ!)
let tier=localStorage.getItem("millen.tier")||"Fast";
if(tier==="Smart")tier="Fast";        // merged tiers (1.20)
function setTier(name){
  tier=name;localStorage.setItem("millen.tier",name);
  councilManual=false;
  if(agent){agent="";localStorage.setItem("millen.agent","");
    if(typeof paintAgents==="function")paintAgents();}
  if(typeof modeShow==="function")modeShow("ai");
  paintModels();                 // paints both tier and model highlights
}
const tierPop=$("#tierpop");
async function showTierPop(el,name){
  let info={};
  try{info=(await(await fetch("/api/tiers")).json())[name]||{};}catch(e){}
  const list=(info.models||[]);
  tierPop.innerHTML="<b>"+esc(name)+"</b>"+
    (list.length
      ? list.map(m=>'<div class="mline">'+esc(m)+'</div>').join("")+
        (list.length>1?'<span class="note">answers blended by Gemma</span>'
                      :'<span class="note">single model — fastest</span>')
      : '<div class="mline">nothing downloaded yet</div>')+
    ((info.skipped||[]).length
      ? '<span class="note">skipped, needs more memory: '+
        esc(info.skipped.join(", "))+'</span>' : "");
  const r=el.getBoundingClientRect();
  tierPop.hidden=false;
  tierPop.style.left=Math.round(r.right+10)+"px";
  tierPop.style.top=Math.round(r.top-4)+"px";
}
function hideTierPop(){tierPop.hidden=true;}
// the tier list is a DROPDOWN: collapsed it shows only the active tier
// with a caret; clicking it unfolds the rest, picking one folds it back
const tierRows=$("#tier-rows");
tierRows.classList.add("closed");
$$(".tier").forEach(el=>{
  el.addEventListener("click",ev=>{
    if(tierRows.classList.contains("closed")){
      ev.stopPropagation();
      tierRows.classList.remove("closed");
      return;
    }
    setTier(el.dataset.tier);
    tierRows.classList.add("closed");
  });
  const ib=el.querySelector(".infobtn");
  if(ib)ib.addEventListener("click",ev=>{
    ev.stopPropagation();
    if(!tierPop.hidden&&tierPop.dataset.for===el.dataset.tier){hideTierPop();return;}
    tierPop.dataset.for=el.dataset.tier;showTierPop(el,el.dataset.tier);
  });
});
document.addEventListener("click",e=>{
  if(!e.target.classList.contains("infobtn"))hideTierPop();
  // clicking anywhere outside the tier list folds it
  if(!e.target.closest||!e.target.closest("#tier-rows"))
    tierRows.classList.add("closed");
  if(!e.target.closest||!e.target.closest("#agents-wrap"))
    agentsWrap.classList.add("closed");
});
setTier(tier);

// AI | Agents: the primary selector is tabbed — AI shows the tier
// dropdown, Agents shows the specialist radio list
function modeShow(which){
  $("#tier-rows").hidden=which!=="ai";
  $("#agents-wrap").hidden=which!=="agents";
  $$("#mode-tabs .ltab").forEach(t=>
    t.classList.toggle("on",t.dataset.m===which));
}
$$("#mode-tabs .ltab").forEach(t=>
  t.addEventListener("click",()=>modeShow(t.dataset.m)));

/* ------------------------------------------------------------ agents */
// radio choice: a task specialist (Coding, Resumes…) or the standard
// model path. Picking a tier or model flips back to Standard.
agent="";localStorage.setItem("millen.agent","");   // AI is the default view
function paintAgents(){
  $$("#agents-wrap .agent").forEach(el=>
    el.classList.toggle("on",(el.dataset.agent||"")===agent));
  const chip=$("#chip-model");
  if(agent&&chip)chip.textContent=agent+" agent";
  else if(chip)paintModels();
}
function setAgent(name){
  agent=name;localStorage.setItem("millen.agent",name);
  paintAgents();
}
const agentsWrap=$("#agents-wrap");
agentsWrap.classList.add("closed");
$$("#agents-wrap .agent").forEach(el=>
  el.addEventListener("click",ev=>{
    if(agentsWrap.classList.contains("closed")){
      ev.stopPropagation();agentsWrap.classList.remove("closed");return;
    }
    setAgent(el.dataset.agent||"");
    agentsWrap.classList.add("closed");
  }));
paintAgents();
modeShow("ai");

// each hardware-class group inside is its own dropdown, folded by default —
// open one tier of the ladder at a time instead of a wall of models
/* ------------------------------------------------------ markdown-lite */
function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function renderMD(raw){
  // pull out think blocks first (DeepSeek R1)
  let thinks=[];
  raw=raw.replace(/<think>([\s\S]*?)<\/think>/g,(_,t)=>{thinks.push(t.trim());return "\u0000THINK"+(thinks.length-1)+"\u0000";});
  const openThink=/<think>([\s\S]*)$/.exec(raw);
  if(openThink){thinks.push(openThink[1].trim());raw=raw.replace(/<think>[\s\S]*$/,"\u0000THINKOPEN"+(thinks.length-1)+"\u0000");}

  let s=esc(raw);
  // fenced code
  s=s.replace(/```(\w*)\n?([\s\S]*?)(```|$)/g,(_,lang,code)=>"<pre><code>"+code.replace(/\n$/,"")+"</code></pre>");
  // inline code, bold, italics, headings
  s=s.replace(/`([^`\n]+)`/g,"<code>$1</code>");
  s=s.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
  s=s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g,"$1<em>$2</em>");
  s=s.replace(/^### (.*)$/gm,"<h3>$1</h3>").replace(/^## (.*)$/gm,"<h2>$1</h2>").replace(/^# (.*)$/gm,"<h1>$1</h1>");
  // markdown links — research briefs cite their sources this way. Only
  // http(s) is allowed through, so a model cannot emit javascript: or data:
  s=s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_,t,u)=>'<a href="'+u+'" target="_blank" rel="noopener noreferrer">'+t+"</a>");
  // PIPE TABLES — models reach for them constantly and they used to
  // render as raw pipes
  s=s.replace(/(^|\n)((?:\|.*\|[ \t]*(?:\n|$)){2,})/g,(m,pre,block)=>{
    const rows=block.trim().split(/\n/).map(r=>r.trim());
    if(!/^\|[\s:|-]+\|$/.test(rows[1]||""))return m;   // needs a divider
    const cells=r=>r.replace(/^\||\|$/g,"").split("|").map(c=>c.trim());
    const head=cells(rows[0]).map(c=>"<th>"+c+"</th>").join("");
    const body=rows.slice(2).map(r=>
      "<tr>"+cells(r).map(c=>"<td>"+c+"</td>").join("")+"</tr>").join("");
    return pre+"<table><thead><tr>"+head+"</tr></thead><tbody>"
           +body+"</tbody></table>";
  });
  // horizontal rules and block quotes
  s=s.replace(/(^|\n)(?:---|\*\*\*|___)[ \t]*(?=\n|$)/g,"$1<hr>");
  s=s.replace(/(^|\n)((?:&gt; ?.*(?:\n|$))+)/g,(m,pre,block)=>
    pre+"<blockquote>"+block.trim().split(/\n/)
      .map(l=>l.replace(/^&gt; ?/,"")).join("<br>")+"</blockquote>");
  // lists
  s=s.replace(/(^|\n)((?:[-*] .*(?:\n|$))+)/g,(m,pre,block)=>{
    const items=block.trim().split(/\n/).map(l=>"<li>"+l.replace(/^[-*] /,"")+"</li>").join("");
    return pre+"<ul>"+items+"</ul>";
  });
  s=s.replace(/(^|\n)((?:\d+\. .*(?:\n|$))+)/g,(m,pre,block)=>{
    const items=block.trim().split(/\n/).map(l=>"<li>"+l.replace(/^\d+\. /,"")+"</li>").join("");
    return pre+"<ol>"+items+"</ol>";
  });
  // paragraphs
  s=s.split(/\n{2,}/).map(p=>{
    if(/^<(pre|ul|ol|h\d|details|table|blockquote|hr)/.test(p.trim()))return p;
    return "<p>"+p.replace(/\n/g,"<br>")+"</p>";
  }).join("");
  // restore think blocks
  s=s.replace(/\u0000THINKOPEN(\d+)\u0000/g,(_,i)=>
    '<details open><summary>◈ reasoning…</summary><div class="think-body">'+esc(thinks[+i]).replace(/\n/g,"<br>")+"</div></details>");
  s=s.replace(/\u0000THINK(\d+)\u0000/g,(_,i)=>
    '<details><summary>◈ reasoning (click to expand)</summary><div class="think-body">'+esc(thinks[+i]).replace(/\n/g,"<br>")+"</div></details>");
  return s;
}

/* ----------------------------------------------------------- chat ui */
const inner=$("#chat-inner"), scroller=$("#chat-scroll");
/* While a blend is RUNNING: a clean progress bar \u2014 model answers stay
   hidden ("random junk", Patrick) until the merge writes the real answer.
   Once merged: the familiar collapsed "N of M contributed" details, with
   the drafts inside for whoever wants to peek. */
function paintDrafts(div,drafts,live,statusText){
  if(!div)return;
  let d=div.querySelector(".contrib"),p=div.querySelector(".blendprog");
  if(live){
    if(d)d.remove();
    if(!drafts.length&&!statusText)return;
    if(!p){
      p=document.createElement("div");p.className="blendprog";
      p.innerHTML='<div class="track"><div class="fill"></div></div>';
      div.insertBefore(p,div.querySelector(".body"));
    }
    const mm=/(\d+)\s*of\s*(\d+)/.exec(statusText||"");
    const total=mm?+mm[2]:Math.max(drafts.length+1,2);
    const done=Math.min(drafts.length,total);
    const now=performance.now();
    if(!p.dataset.start)p.dataset.start=now;
    if(p.dataset.done!==String(done)){
      if(done>0)p.dataset.per=(now-(+p.dataset.start))/done;
      p.dataset.done=done;p.dataset.t0=now;
    }
    p.dataset.total=total;
    // a 150ms ticker owns the motion: MONOTONIC glide toward the pace
    // estimate, alive even through silent engine loads
    if(!p._tick){
      p._tick=setInterval(()=>{
        const t2=+p.dataset.total||2,d2=+p.dataset.done||0;
        const per2=+p.dataset.per||28000;
        const fr=Math.min(.96,(performance.now()-(+p.dataset.t0||performance.now()))/per2);
        const want=Math.min(97,((d2+fr)/t2*100));
        const cur=+p.dataset.w||0;
        const w=Math.max(cur,cur+(want-cur)*.06);
        p.dataset.w=w;
        const f=p.querySelector(".fill");
        if(f)f.style.width=w.toFixed(2)+"%";
      },150);
    }
    return p;
  }
  if(p){if(p._tick)clearInterval(p._tick);p.remove();}
  if(!drafts||!drafts.length)return;
  if(!d){
    d=document.createElement("details");
    d.className="contrib";
    div.insertBefore(d,div.querySelector(".body"));
  }
  const answered=drafts.filter(x=>!/^\(no answer/.test(x.t)).length;
  d.innerHTML='<summary><span class="caretmark">\u203a</span>'
    +answered+" of "+drafts.length+" models contributed"
    +'</summary>'
    +drafts.map(x=>{
       const none=/^\(no answer/.test(x.t);
       return '<div class="draft'+(none?" empty":"")+'">'
         +'<div class="dm">'+esc(x.m)+'</div>'
         +'<div class="dt">'+esc(x.t)+'</div></div>';}).join("");
  return d;
}

// a wall of model names above every blend read as noise — count them
// instead; a single model keeps its name
function whoLabel(s){
  if(!s)return s;
  return s.split(",").length>1?"":s;
}

function addMsg(role,text,drafts){
  const hero=$("#hero"); if(hero)hero.remove();
  const div=document.createElement("div");
  div.className="msg "+(role==="user"?"user":"ai");
  const who=role==="user"?"you":(whoLabel(lastModels)||tier);
  div.innerHTML='<div class="who">'+who+'</div><div class="body"></div>';
  const body=div.querySelector(".body");
  if(role==="user")body.textContent=text; else body.innerHTML=renderMD(text);
  if(role!=="user"&&drafts&&drafts.length)paintDrafts(div,drafts,false);
  inner.appendChild(div);
  scroller.scrollTop=scroller.scrollHeight;
  return div;
}

/* -------------------------------------------------------- tok/s meter */
// throughput readout was removed from the panel; per-message tok/s still
// appears under each answer
function setToks(){}

/* --------------------------------------------------------------- send */
const input=$("#input"),sendBtn=$("#send");
input.addEventListener("input",()=>{input.style.height="auto";input.style.height=Math.min(input.scrollHeight,180)+"px";});
// NEVER a silent dead send button: a runtime error inside send() (a
// dangling identifier killed every send in 2.0.0, seen live) surfaces in
// the composer instead of eating the click.
function sendSafe(){
  try{
    const r=send();
    if(r&&r.catch)r.catch(e=>{
      input.placeholder="send failed — "+(e&&e.message||e);});
  }catch(e){
    input.placeholder="send failed — "+(e&&e.message||e);
  }
}
input.addEventListener("keydown",e=>{
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendSafe();}
});
sendBtn.addEventListener("click",()=>{ generating?abortCtl.abort():sendSafe(); });

/* ------------------------------------------------------ image paste */
// paste a screenshot/photo straight into the composer: it becomes a chip,
// and the request routes to the vision engine. Downscaled client-side so
// a 12 MP photo doesn't ride the wire.
let pendingImages=[],pendingDocs=[];
function paintChips(){
  const w=$("#imgchips");
  w.hidden=!pendingImages.length&&!pendingDocs.length;
  w.innerHTML=pendingImages.map((d,i)=>
    '<span class="imgchip"><img src="'+d+'">'+
    '<b data-k="i" data-i="'+i+'" title="Remove">×</b></span>').join("")
   +pendingDocs.map((d,i)=>
    '<span class="docchip" title="'+esc(d.name)+'">📄 '+esc(d.name.slice(0,22))+
    '<b data-k="d" data-i="'+i+'" title="Remove">×</b></span>').join("");
  w.querySelectorAll("b").forEach(b=>b.addEventListener("click",()=>{
    (b.dataset.k==="i"?pendingImages:pendingDocs).splice(+b.dataset.i,1);
    paintChips();
  }));
}
function addImageFile(f){
  if(pendingImages.length>=3)return;
  const img=new Image();
  img.onload=()=>{
    const s=Math.min(1,1280/Math.max(img.width,img.height));
    const c=document.createElement("canvas");
    c.width=Math.round(img.width*s);c.height=Math.round(img.height*s);
    c.getContext("2d").drawImage(img,0,0,c.width,c.height);
    pendingImages.push(c.toDataURL("image/jpeg",.85));
    URL.revokeObjectURL(img.src);
    paintChips();
  };
  img.src=URL.createObjectURL(f);
}
async function addDocFile(f){
  if(pendingDocs.length>=2)return;
  try{
    const text=(await f.text()).slice(0,50000);
    if(text.trim()){pendingDocs.push({name:f.name,text});paintChips();}
  }catch(e){}
}
$("#attach").addEventListener("click",()=>$("#fpick").click());
$("#fpick").addEventListener("change",()=>{
  [...$("#fpick").files].forEach(f=>{
    if(f.type.startsWith("image/"))addImageFile(f);
    else if(f.size<2_000_000)addDocFile(f);
  });
  $("#fpick").value="";
});
input.addEventListener("paste",e=>{
  const items=[...(e.clipboardData||{}).items||[]]
    .filter(it=>it.type&&it.type.startsWith("image/"));
  if(!items.length)return;
  e.preventDefault();
  items.slice(0,3-pendingImages.length).forEach(it=>{
    const f=it.getAsFile();if(f)addImageFile(f);
  });
});

/* stick to the bottom only when the reader is already there — scrolling
   up mid-answer used to be a losing fight against every chunk */
function autoScroll(){
  if(scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<140)
    scroller.scrollTop=scroller.scrollHeight;
}


async function send(){
  const text=input.value.trim();
  if((!text&&!pendingImages.length&&!pendingDocs.length)||generating)return;

  // engine down? give launch instructions instead of a doomed request.
  // in combine mode, drop unavailable models rather than failing outright
  if(combine&&council.length>1){
    const live=council.filter(m=>!engineState[m]
      ||(engineState[m].up&&engineState[m].mem_ok!==false));
    if(live.length&&live.length<council.length){council=live;paintModels();}
  }
  const eng=engineState[model];
  if(eng&&!eng.up){
    input.value="";input.style.height="auto";
    addMsg("user",text);
    const help="⚠️ **"+model+"** isn't running ("+eng.note+").\n\n"+
      (eng.cmd?"Start it in a terminal:\n\n```\n"+eng.cmd+"\n```\n\nOr just click a model with a green dot — those are ready now.":
      "Click a model with a green dot — those are ready now.");
    addMsg("assistant",help);
    return;
  }

  fetch("/api/speak",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({stop:true})});
  input.value="";input.style.height="auto";
  const sentImages=pendingImages.slice();
  const sentDocs=pendingDocs.slice();
  pendingImages=[];pendingDocs=[];paintChips();
  const shown=(text||(sentImages.length?"🖼️ (image)":"")
    ||(sentDocs.length?"📄 (file)":""))
    +(sentDocs.length&&text?"":"")
    +(sentDocs.length?"\n📄 "+sentDocs.map(d=>d.name).join(", "):"");
  messages.push({role:"user",content:shown});
  const uDiv=addMsg("user",shown);
  if(sentImages.length){
    const row=document.createElement("div");row.className="sentimgs";
    sentImages.forEach(d=>{const im=new Image();im.src=d;row.appendChild(im);});
    uDiv.querySelector(".body").appendChild(row);
  }

  generating=true; document.body.classList.add("gen");
  sendBtn.textContent="■"; sendBtn.classList.add("stop"); sendBtn.title="Stop";
  const aiDiv=addMsg("assistant",""); const body=aiDiv.querySelector(".body");
  aiDiv.classList.add("live");     // soft mask on the newest line
  body.innerHTML='<span class="caret"></span>';

  abortCtl=new AbortController();
  let full="",t0=performance.now(),tokEst=0,lastRate=0,wasAborted=false,searched=false,status=null,drafts=[];
  lastModels="";

  try{
    const resp=await fetch("/api/chat",{
      method:"POST",headers:{"Content-Type":"application/json"},
      signal:abortCtl.signal,
      body:JSON.stringify({model,models:council,tier,messages,
        auto_web:autoWeb,images:sentImages,docs:sentDocs,agent}),
    });
    searched=resp.headers.get("X-Web-Search")==="1";
    lastModels=resp.headers.get("X-Models")||"";
    if(lastModels){const w=aiDiv.querySelector(".who");if(w)w.textContent=whoLabel(lastModels);}
    if(searched)body.innerHTML='<span class="websrc">🌐 searched the web</span><span class="caret"></span>';
    const reader=resp.body.getReader(),dec=new TextDecoder();
    let raw="";
    while(true){
      const {done,value}=await reader.read();
      if(done)break;
      raw+=dec.decode(value,{stream:true});
      // pull progress markers out so they never land in the answer
      full=raw.replace(/\u0000STATUS:(.*?)\u0000/g,(_,t)=>{status=t;return "";})
              .replace(/\u0000STATUS:[^\u0000]*$/,"")    // partial marker
              .replace(/\u0000DRAFT:(.*?)\u0000/g,(_,j)=>{
                 try{const d=JSON.parse(j);
                     if(!drafts.some(x=>x.m===d.m))drafts.push(d);}catch(e){}
                 return "";})
              .replace(/\u0000DRAFT:[^\u0000]*$/,"");
      if(drafts.length||(status&&/of \d+/.test(status)))
        paintDrafts(aiDiv,drafts,true,status);
      // a merge that collapsed mid-stream sends RESET \u2014 discard
      // everything streamed before it, keep the replacement answer
      const cut=full.lastIndexOf("\u0000RESET\u0000");
      if(cut>=0)full=full.slice(cut+7);
      tokEst=full.length/4;
      const secs=(performance.now()-t0)/1000;
      lastRate=secs>0.3?tokEst/secs:0;
      setToks(lastRate,"streaming");
      const hasBar=!!aiDiv.querySelector(".blendprog");
      body.innerHTML=(status&&!full&&!hasBar
          ?'<span class="statusline">◇ '+esc(status)+'…</span>':"")
        +(searched?'<span class="websrc">🌐 searched the web</span>':"")
        +renderMD(full)+'<span class="caret"></span>';
      autoScroll();
    }
  }catch(err){
    if(err.name==="AbortError")wasAborted=true;
    else{
      // the wire died but drafts already arrived: the best draft IS the
      // answer — only surface the error when we truly have nothing
      const rescued=drafts.filter(x=>!/^\(no answer/.test(x.t));
      if(!full.trim()&&rescued.length)full=rescued[0].t;
      else full+="\n\n⚠️ "+err.message;
    }
  }

  aiDiv.classList.remove("live");
  paintDrafts(aiDiv,drafts,false);   // merge done: collapse (or clear bar)
  // the stream died but good drafts exist — the best one IS the answer;
  // never show "engine returned nothing" over a usable draft
  if(!full&&!wasAborted){
    const rescued=drafts.filter(x=>!/^\(no answer/.test(x.t));
    if(rescued.length)full=rescued[0].t;
  }
  body.innerHTML=(searched&&full?'<span class="websrc">🌐 searched the web</span>':"")
    +renderMD(full||(wasAborted?"*(stopped)*":
    "⚠️ The engine returned nothing. Is the model server for **"+model+"** actually running?"));
  const secs=((performance.now()-t0)/1000);
  const isErr=full.trim().startsWith("⚠️")||full.includes("\n⚠️");
  if(full&&!isErr){
    const meta=document.createElement("div");meta.className="meta";
    meta.innerHTML="<b>"+lastRate.toFixed(1)+" tok/s</b> · ~"+Math.round(tokEst)+" tokens · "+secs.toFixed(1)+"s";
    aiDiv.appendChild(meta);
    messages.push(drafts.length?{role:"assistant",content:full,drafts:drafts}
                              :{role:"assistant",content:full});
    persistCurrent();
  }else{
    // error or empty: keep it out of the model's context, refresh the dots
    messages.pop();
    pollEngines();
  }
  if(voiceChat&&full&&!isErr&&!wasAborted){
    fetch("/api/speak",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({text:full})});
  }
  setToks(0,"idle");
  generating=false;abortCtl=null;document.body.classList.remove("gen");
  sendBtn.textContent="↑";sendBtn.classList.remove("stop");sendBtn.title="Send";
  autoScroll();
  input.focus();
}

/* ------------------------------------------------------------- greeting */
const GREETINGS=[
  "What's going on today?","What's on your mind?","Where should we start?",
  "What are you working on?","What can I help with?","What's up today?",
  "Ask me anything.","What are you curious about?",
  "What shall we get into?","How can I help right now?",
  "What are we building today?","Give me something hard.",
  "What's the plan?","Talk to me.","What needs solving?",
  "Where's your head at?","What's cooking?","Let's figure something out.",
  "What's the big idea?","Drop a question.","What are we chasing today?",
  "What's puzzling you?","Fire away.","What should we untangle?",
  "What's next on the list?","Let's make something.","What's the mission?",
  "Got a problem? Bring it.","What deserves an answer?",
  "What would help right now?","Pick my brain.","What's rattling around?",
  "Let's get after it.","What's worth knowing today?",
  "What can we knock out?","Start anywhere.","What's the question?",
  "Something on your plate?","Let's dig in.","What's bugging you?",
  "Where do we begin?","Hit me with it.","What are we learning today?",
  "What's the goal tonight?","Bring me a challenge.",
  "What's one thing you want done?","Let's move the needle.",
  "What's unclear?","I'm all ears.","What do you want to know?",
  "Sketch me the problem.","What's brewing?","Ready when you are.",
  "What matters most right now?","Give me the messy version.",
  "What are you stuck on?","Let's sort it out.","What's the itch?",
  "Ideas welcome.","What should exist that doesn't?",
  "What's today's rabbit hole?","Ask the thing.","What's half-finished?",
  "Let's close a loop.","What would make today a win?",
  "What's the story?","Curious about anything?","Let's think it through.",
  "What needs a second brain?","Throw me a curveball.",
  "What's on the whiteboard?","Where's the friction?",
  "What do you want to understand?","Let's break it down.",
  "What's the dream?","Name the target.","What's blocking you?",
  "Big question or small — go.","What should we explore?",
  "What's worth doing well?","Let's write something great.",
  "What's the puzzle?","Give me the details.","What's your angle?",
  "What can I take off your plate?","Let's crack this.",
  "What are you wondering?","Set the scene.","What's the ask?",
  "Anything keeping you up?","Let's get specific.",
  "What deserves a deep dive?","Point me at it.",
  "What's the first move?","Make it interesting.",
  "What's on deck?","Let's ship something.","What's the vision?",
  "Questions, plans, wild ideas — go.","What needs fixing?",
  "Tell me everything.","What's the endgame?","Let's find out.",
  "What are we solving first?","Your move.","What's it gonna be?",
  "Feed me a problem.","Where can I earn my keep?",
  "What's worth a long answer?","Let's go deep.",
  // FULL NYC, per Patrick
  "What's up?","Let's fucking go.","Yo.","What's good?",
  "What's good dawg?","Talk to me, Goose.","Whaddaya need?",
  "Let's get it.","What are we doing today?","Hit me.","I'm listening.",
  "What's the move?","Say less.","Let's cook.","Bet — what's up?",
  "Alright, what've you got?","Lay it on me.","What's the word?",
  "Ready when you are, chief.","Let's run it.","What's the play?",
  "Go ahead, I got time.","Shoot.","What're we getting into?",
  "Let's make it happen.","Straight up — what do you need?",
  "Yerrr.","What's poppin'?","Ayo, what's good?","We outside.",
  "Talk your talk.","Run me the play.","What we cookin'?",
  "You already know what it is.","Deadass, what do you need?",
  "No cap — hit me.","What's the science?","Gimme the rundown.",
  "It's whatever you need, b.","What's the deal?","How we movin'?",
  "Brick outside, warm in here — what's up?","What's the wave?",
  "Aight, let's work.","You good? What do you need?",
  "The city's up. So am I.","What's crackin'?","Speak your piece.",
  "Word — what's next?","On God, let's get to it.","What it do?",
  "Whole squad's ready. Ask away.","Mad questions? Start with one.",
  "Son, just ask.","What's the story, kid?","Let's eat.",
];
function greeting(){return GREETINGS[Math.floor(Math.random()*GREETINGS.length)];}
(function(){const g=$(".greet");if(g)g.textContent=greeting();})();

/* ------------------------------------------------- chats: list + store */
// Chats are owned by the backend (survives app updates); localStorage is
// only a fast local mirror so the list paints before the fetch returns.
let chats=[];
try{chats=JSON.parse(localStorage.getItem("millen.chats"))||[];}catch(e){}
let curChat=null;   // every launch starts fresh; history stays in the list
let chatSaveTimer=null;

async function loadChatsFromDisk(){
  try{
    const server=(await(await fetch("/api/chats")).json()).chats||[];
    if(server.length){chats=server;}
    else if(chats.length){await pushChatsToDisk();}   // migrate old localStorage
    renderChats();
  }catch(e){}
}
async function pushChatsToDisk(){
  try{
    await fetch("/api/chats",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({chats:chats})});
  }catch(e){}
}

function resetHero(){
  inner.innerHTML='<div id="hero"><div class="h1row"><h1 data-word="MillenAI">MillenAI<span class="halo" aria-hidden="true"><span>MillenAI</span></span></h1></div><div class="beta-tag">__APP_BETA__</div><p class="greet">'+esc(greeting())+'</p></div>';
}
function saveChats(){
  // write through to disk, coalesced so a burst of messages is one write
  clearTimeout(chatSaveTimer);
  chatSaveTimer=setTimeout(pushChatsToDisk,400);
  try{localStorage.setItem("millen.chats",JSON.stringify(chats.slice(0,30)));}
  catch(e){chats=chats.slice(0,10);localStorage.setItem("millen.chats",JSON.stringify(chats));}
}
function persistCurrent(){
  if(!messages.length)return;
  if(!curChat)curChat="c"+Date.now();
  let c=chats.find(x=>x.id===curChat);
  if(!c){c={id:curChat};chats.unshift(c);}
  const first=messages.find(m=>m.role==="user");
  // show the raw text immediately, then let a small model name it properly
  if(!c.title)c.title=(first?first.content:"chat").slice(0,48);
  c.ts=Date.now();c.messages=messages.slice();
  chats.sort((a,b)=>b.ts-a.ts);
  saveChats();renderChats();
  if(!c.named&&first){c.named=true;nameChat(c,first.content);}
}

async function nameChat(c,text){
  try{
    const r=await fetch("/api/title",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({text:text})});
    const t=(await r.json()).title;
    if(t){c.title=t;saveChats();renderChats();}
    else c.named=false;          // let a later turn try again
  }catch(e){c.named=false;}
}
function renderChats(){
  const el=$("#chat-list");
  el.innerHTML=chats.map(c=>
    '<div class="chat-item'+(c.id===curChat?" active":"")+'" data-id="'+c.id+
    '"><span class="ct">'+esc(c.title||"chat")+'</span><span class="cx" title="Delete chat">×</span></div>').join("");
  el.querySelectorAll(".chat-item").forEach(it=>{
    it.querySelector(".cx").addEventListener("click",ev=>{
      ev.stopPropagation();
      chats=chats.filter(c=>c.id!==it.dataset.id);
      if(curChat===it.dataset.id){curChat=null;messages=[];resetHero();}
      saveChats();renderChats();
    });
    it.addEventListener("click",()=>loadChat(it.dataset.id));
  });
}
function loadChat(id){
  if(id===curChat)return;
  persistCurrent();
  const c=chats.find(x=>x.id===id);if(!c)return;
  if(generating&&abortCtl)abortCtl.abort();
  curChat=id;
  messages=c.messages.slice();
  inner.innerHTML="";
  messages.forEach(m=>addMsg(m.role==="user"?"user":"assistant",m.content,m.drafts));
  renderChats();
}
renderChats();
loadChatsFromDisk();

/* ----------------------------------------------------------- new chat */
$("#newchat").addEventListener("click",()=>{
  if(generating&&abortCtl)abortCtl.abort();
  persistCurrent();
  curChat=null;messages=[];
  resetHero();renderChats();
  input.focus();
});

/* ---------------------------------------------------------- telemetry */
function buildMeter(el){const f=document.createElement("div");
  f.className="mfill";el.appendChild(f);}
buildMeter($("#gpu-meter"));buildMeter($("#models-meter"));
buildMeter($("#fleet-meter"));
function paintMeter(el,pct){
  const f=el.firstChild;if(!f)return;
  f.style.width=Math.max(0,Math.min(100,pct))+"%";
  f.classList.toggle("hot",pct>=80);
}
let simGpu=12,fleetStat=null;
async function pollStats(){
  let gpu;
  try{
    const st=await(await fetch("/api/stats")).json();
    gpu=st.gpu_pct;
    fleetStat={online:st.fleet_online||0,busy:st.fleet_busy||0};
  }catch(e){}
  if(gpu==null){
    // ambient fallback — clearly approximate
    simGpu=Math.max(2,Math.min(97,simGpu+(Math.random()-0.5)*8+(generating?22:-16)));
    gpu=simGpu;
  }
  paintMeter($("#gpu-meter"),gpu);
  // COMMUNITY GPU: each friend online lights a quarter of the bar;
  // it burns hot while any of them is actually working
  const fm=$("#fleet-meter");
  if(fm&&fm.firstChild&&fleetStat){
    fm.firstChild.style.width=Math.min(100,(fleetStat.online||0)*25)+"%";
    fm.firstChild.classList.toggle("hot",(fleetStat.busy||0)>0);
  }
}
// polling is owned by applyStatsPolling so perf mode can shut it off
// (statsTimer is declared with the rest of the state — re-declaring it here
//  would orphan the timer setPerf already started at boot)
function applyStatsPolling(){
  if(perf){
    if(statsTimer){clearInterval(statsTimer);statsTimer=null;}
  }else if(!statsTimer){
    pollStats();statsTimer=setInterval(pollStats,1000);
  }
}
applyStatsPolling();

/* -------------------------------------------------- engine status dots */
// original size labels, so a finished download restores "7B" not "100%"
const MODEL_SIZES={};
$$(".model").forEach(el=>{
  if(el.dataset.model)MODEL_SIZES[el.dataset.model]=el.querySelector(".size").textContent;
});
$$(".model").forEach(el=>{
  const d=document.createElement("span");d.className="dot";
  el.insertBefore(d,el.querySelector(".size"));
});
async function pollEngines(){
  try{
    const r=await fetch("/api/engines"),st=await r.json();
    engineState=st;
    $$(".model").forEach(el=>{
      const s=st[el.dataset.model];if(!s)return;
      const d=el.querySelector(".dot");
      d.className="dot "+(s.up?"up":"down");
      el.title=s.note;
      el.classList.toggle("unsupported",s.supported===false);
      // show live progress in the size slot while a model downloads
      const sz=el.querySelector(".size");
      if(s.dl){
        el.classList.add("pending");
        sz.textContent=s.dl==="queued"?"queued":(s.pct||0)+"%";
      }else if(el.classList.contains("pending")){
        el.classList.remove("pending");
        sz.textContent=MODEL_SIZES[el.dataset.model]||sz.textContent;
      }else if(!s.up&&s.supported!==false){
        sz.textContent=MODEL_SIZES[el.dataset.model]||sz.textContent;
        el.title=s.note+" — click to download";
      }
      let mt=el.querySelector(".memtag");
      if(s.supported===false){
        if(mt)mt.textContent="APPLE SILICON ONLY";
      }else if(s.mem_ok===false){
        if(!mt){
          mt=document.createElement("span");mt.className="memtag";
          mt.textContent="INSUFFICIENT MEMORY";
          el.insertBefore(mt,el.querySelector(".size"));
        }
      }else if(mt)mt.remove();


    });
    // headline tally: how many models are actually usable right now
    {
      const all=Object.entries(st).filter(([,v])=>v.supported!==false);
      const up=all.filter(([,v])=>v.up).length;
      const mm=$("#models-meter");
      if(mm&&mm.firstChild&&all.length)
        mm.firstChild.style.width=Math.round(up/all.length*100)+"%";
    }
    // engine states just arrived — prune hand-picked rosters of models
    // that can't run (red dots showing council ranks was a lie), then
    // fill the roster automatically if the user hasn't curated one
    if(combine&&councilManual&&council.length>1){
      const ok=council.filter((m,i)=>{const s=engineState[m];
        return i===0||!s||(s.up&&s.mem_ok!==false);});
      if(ok.length!==council.length){council=ok;paintModels();}
    }
  }catch(e){}
}
pollEngines();setInterval(pollEngines,8000);

/* ------------------------------------------------- NYC skyline backdrop */
// Apple's ATV aerial loops of New York, served by OUR OWN server from
// /sky/<i>.mov — the raw CDNs are unusable in a browser (phobos: http-only;
// sylvan: moov atom after 370 MB of mdat, nothing plays until the whole
// file lands). The server downloads once, remuxes fast-start, caches, and
// streams with Range support. While it warms, the macOS-style #skyload bar
// tracks the download; a different clip still plays every launch.
const SKY_N=parseInt("__SKY_N__",10)||5;   // injected: len(SKY_SOURCES)
const skyline=$("#skyline");
async function bootSkyline(){
  if(perf||!skyline)return;
  // PLAY FROM THE CACHE: picking blind across 89 clips meant nearly every
  // launch hit an undownloaded one and sat behind the Loading bar. Now the
  // pick comes from what's already on disk (instant), and ONE new clip
  // warms silently in the background so variety keeps growing anyway.
  const last=parseInt(localStorage.getItem("millen.sky")||"-1",10);
  const firstEver=last<0;
  let cached=[];
  try{cached=(await(await fetch("/api/sky/cached")).json()).cached||[];}
  catch(e){}
  const darkSet=new Set(JSON.parse('__SKY_DARK__'));
  // THE POOL IS OPEN: all 89 Apple clips are eligible ("getting kinda
  // stale"). The dark set is only a first-run preference now — the warp
  // reads best over them — and any clip is fair game after that.
  const mood=x=>darkSet.has(x)||!firstEver;
  let i;
  // INSTANT START, per Patrick: never make a launch wait on a download —
  // play something already on disk, and pull exactly ONE new clip in the
  // background so tomorrow's rotation is wider than today's.
  const fresh=[];
  for(let n=0;n<SKY_N;n++)if(!cached.includes(n)&&mood(n))fresh.push(n);
  if(cached.length){
    let pool=cached.filter(x=>mood(x)&&x!==last);
    if(!pool.length)pool=cached.filter(x=>x!==last);
    if(!pool.length)pool=cached.slice();
    i=pool[Math.floor(Math.random()*pool.length)];
  }else{
    const moody=[];
    for(let n=0;n<SKY_N;n++)if(mood(n))moody.push(n);
    const src=moody.length?moody:[...Array(SKY_N).keys()];
    i=src[Math.floor(Math.random()*src.length)];
    if(i===last)i=(i+1)%SKY_N;
  }
  localStorage.setItem("millen.sky",i);
  // exactly one new clip per load, after the backdrop is already playing
  if(fresh.length){
    const n=fresh[Math.floor(Math.random()*fresh.length)];
    setTimeout(()=>{
      fetch("/api/sky/status?i="+n+"&warm=1").catch(()=>{});},9000);
  }
  const c=$("#sky-color");
  const bar=$("#skyload"),fill=$("#skyload .fill"),lbl=$("#skyload .lbl");
  c.preload="auto";
  function hideBar(){if(bar)bar.hidden=true;}
  function attach(){
    // the bar rides the BUFFER now: unhiding on the first frame let
    // playback race the network and stutter ("super jittery") — wait for
    // canplaythrough, showing buffered % meanwhile. A 12s cap means a
    // slow link still gets its city rather than an eternal bar.
    let shown=false;
    function reveal(){
      if(shown)return;shown=true;
      hideBar();skyline.hidden=false;
    }
    c.addEventListener("canplaythrough",reveal,{once:true});
    c.addEventListener("error",()=>{
      // evicted or hiccuped mid-session: re-warm THIS clip and resume —
      // the backdrop only changes on reload, never on its own
      hideBar();
      fetch("/api/sky/status?i="+i+"&warm=1").then(()=>{
        const re=setInterval(async()=>{
          const st=await(await fetch("/api/sky/status?i="+i)).json();
          if(st.status==="ready"){clearInterval(re);c.src="/sky/"+i+".mov";
            const p2=c.play();if(p2&&p2.catch)p2.catch(()=>{});}
          if(st.status==="error"){clearInterval(re);skyline.hidden=true;}
        },1500);
      }).catch(()=>{skyline.hidden=true;});
    },{once:true});
    function buf(){
      if(shown)return;
      if(bar){bar.hidden=false;}   // visible from the FIRST moment
      try{
        const d=c.duration,e=c.buffered.length?c.buffered.end(0):0;
        // STREAM as it loads: ~6s of runway is enough cushion to play
        // smoothly while the rest keeps downloading — on a decent
        // connection the city is on screen in well under 10 seconds
        if(d>0&&e>=Math.min(6,d*.25)){reveal();return;}
        if(bar&&d>0){
          bar.hidden=false;
          const p=Math.min(99,Math.round(e/Math.min(d,6)*100));
          fill.style.width=p+"%";
          lbl.textContent="Loading · "+p+"%";
        }
      }catch(err){}
      setTimeout(buf,400);
    }
    buf();
    setTimeout(reveal,10000);   // 10 seconds, tops — then play with what we have
    c.src="/sky/"+i+".mov";
    const pr=c.play(); if(pr&&pr.catch)pr.catch(()=>{});
  }
  let rotations=0;
  function poll(){
    fetch("/api/sky/status?i="+i).then(r=>r.json()).then(st=>{
      if(st.status==="ready"){attach();return;}
      if(st.status==="error"){
        // a dead clip rotates to the next; after all fail the backdrop
        // gives up quietly, exactly as it always has offline
        if(++rotations>=SKY_N){hideBar();skyline.hidden=true;return;}
        i=(i+1)%SKY_N;localStorage.setItem("millen.sky",i);
        poll();return;
      }
      if(bar){
        bar.hidden=false;
        fill.style.width=(st.pct||0)+"%";
        lbl.textContent=(st.status==="remuxing"?"Loading":
          "Loading · "+(st.pct||0)+"%");
      }
      setTimeout(poll,900);
    }).catch(hideBar);
  }
  poll();
}
bootSkyline();

/* --------------------------------------------- shard-warp (the only warp) */
// The classic starfield is gone on Patrick's call — no white dots, ever.
// The warp is the image itself: see buildTiles/starTick below. Offline
// there is no backdrop and therefore no warp: nothing animates, by design.
// NB: drawImage(video) taints this canvas (CORS-less Apple stream) — that
// is fine for DRAWING, but no code may ever getImageData from it.
const starCv=$("#stars"),sctx=starCv.getContext("2d");
let sw=0,sh=0,tiles=[],tileMeta=null;
function starResize(){
  // 1.5 caps fill-rate cost: the warp is fast-moving slats, where the
  // difference from 2x is invisible but the pixel count nearly halves
  const dpr=Math.min(window.devicePixelRatio||1,1.5);
  sw=starCv.width=Math.max(1,starCv.offsetWidth*dpr);
  sh=starCv.height=Math.max(1,starCv.offsetHeight*dpr);
  tiles=[];tileMeta=null;           // anchors depend on the viewport
}
starResize();
window.addEventListener("resize",starResize);

// THE CITY'S OWN LIGHTS answer you: while a model thinks, the brightest
// real pixels of the footage (windows, headlights, stars) are harvested
// from a tiny probe of the frame and released as glowing motes that drift
// toward the viewer. Only possible since the videos went same-origin —
// reading a CORS-tainted canvas was illegal all night long.
const probeCv=document.createElement("canvas");
probeCv.width=160;probeCv.height=90;
const probeCtx=probeCv.getContext("2d",{willReadFrequently:true});
let lightMotes=[],lastHarvest=0;
function harvestLights(ts){
  if(!generating||ts-lastHarvest<420||lightMotes.length>140)return;
  lastHarvest=ts;
  try{
    probeCtx.drawImage(c,0,0,160,90);
    const d=probeCtx.getImageData(0,0,160,90).data;
    const found=[];
    for(let i=0;i<d.length;i+=16){          // stride: every 4th pixel
      if(d[i]+d[i+1]+d[i+2]>560){
        found.push([(i/4)%160,Math.floor(i/4/160),d[i],d[i+1],d[i+2]]);
      }
    }
    for(let k=0;k<12&&found.length;k++){
      const p=found[Math.floor(Math.random()*found.length)];
      lightMotes.push({
        x:p[0]/160*sw, y:p[1]/90*sh,
        vx:(Math.random()-.5)*sw*.05,
        vy:-sh*(.05+Math.random()*.09),
        r:p[2],g:p[3],b:p[4],
        life:1.6, max:1.6, size:2+Math.random()*2.5});
    }
  }catch(err){}
}
/* the sidebar wordmark takes its colours FROM the footage: probe the
   frame, average three luminance bands (shadow / mid / light), brighten
   them into text-worthy tones, hand them to the CSS vars */
let lastBrand=0;
function paintBrandFromSky(ts){
  const c=$("#sky-color");
  if(!c||c.videoWidth<1||ts-lastBrand<6000)return;
  lastBrand=ts;
  try{
    probeCtx.drawImage(c,0,0,160,90);
    const d=probeCtx.getImageData(0,0,160,90).data;
    const px=[];
    for(let i=0;i<d.length;i+=24)
      px.push([d[i],d[i+1],d[i+2],d[i]+d[i+1]+d[i+2]]);
    px.sort((a,b)=>a[3]-b[3]);
    // BRIGHT bands only: feeding the shadow tone into text made letters
    // read half-disabled grey (seen live). Boost saturation away from
    // mud, lift to legible brightness, keep the hue.
    const band=q=>{
      const s=Math.floor(px.length*q),e=Math.min(px.length,Math.floor(px.length*(q+.22)));
      let r=0,g=0,b=0,n=0;
      for(let k=s;k<e;k++){r+=px[k][0];g+=px[k][1];b+=px[k][2];n++;}
      r/=n;g/=n;b/=n;
      const m=(r+g+b)/3;
      const f=v=>Math.max(0,Math.min(255,
        Math.round(112+(m+(v-m)*1.7)*.56)));
      return [f(r),f(g),f(b)];
    };
    const rgb=c=>"rgb("+c[0]+","+c[1]+","+c[2]+")";
    const b1=band(.45),b2=band(.68),b3=band(.86);
    const root=document.documentElement.style;
    root.setProperty("--bwavg",rgb([0,1,2].map(i=>
      Math.round((b1[i]+b2[i]+b3[i])/3))));
    root.setProperty("--bw1",rgb(b1));
    root.setProperty("--bw2",rgb(b2));
    root.setProperty("--bw3",rgb(b3));
    root.setProperty("--bwglow",
      "rgba("+b2[0]+","+b2[1]+","+b2[2]+",.35)");
  }catch(err){}
}

function drawMotes(dt){
  if(!lightMotes.length)return;
  sctx.globalCompositeOperation="screen";
  for(let k=lightMotes.length-1;k>=0;k--){
    const p=lightMotes[k];
    p.life-=dt;
    if(p.life<=0){lightMotes.splice(k,1);continue;}
    p.x+=p.vx*dt;p.y+=p.vy*dt;
    const a=p.life/p.max;
    sctx.globalAlpha=a*.9;
    sctx.fillStyle="rgb("+p.r+","+p.g+","+p.b+")";
    sctx.beginPath();
    sctx.arc(p.x,p.y,p.size,0,6.2832);
    sctx.fill();
    sctx.globalAlpha=a*.28;                 // soft halo, no shadowBlur cost
    sctx.beginPath();
    sctx.arc(p.x,p.y,p.size*3,0,6.2832);
    sctx.fill();
  }
  sctx.globalAlpha=1;
  sctx.globalCompositeOperation="source-over";
}

/* --------------------------------------------------------- warp audio */
// A synthesized engine, no audio files: two detuned saws through a
// resonant lowpass (the drone) + looped noise through a bandpass (the
// wind), both enveloped by the SAME e/recoil that drive the visuals —

const WARP_UP=1.35, WARP_DOWN=2.3;  // turbo: readable spool, long tail
// Per-frame snapshot of the video at capped resolution: every slat then
// blits canvas->canvas, which skips the per-drawImage video-frame
// conversion that made ~150 tiles x 60fps expensive while a model is
// already eating the machine. The snapshot is the ONLY video read per
// frame, and the warp looks identical.
const snapCv=document.createElement("canvas"),snapCtx=snapCv.getContext("2d");
const WARP_IDLE=0.5, WARP_FULL=22;
let warpT=0,warpLast=0,warpSpeed=WARP_IDLE,skyCreep=0;

// The warp is the IMAGE ITSELF flying at you, split into LONG VERTICAL
// LINES — Patrick's spec, chosen from a live A/B against square shards:
// ~28 CSS-px-wide slats, three per column height, each at its own depth
// speed. At onset every slat sits at z=1, which reconstructs the picture
// exactly; then the slats rush the viewer with true perspective (pos and
// scale both 1/z), desynced so the frame visibly splits, and settle
// NEATLY afterwards: z pulls home, scatter is proportional to (1-z) so it
// collapses to zero, and the intact video fades up beneath the landing.
// No spin, no radial rotation — slats stay upright and just zoom.
function buildTiles(vw,vh){
  // ~28px chips, TONS of them — the frame splits like pizza slices from
  // the centre and every chip streaks radially, "like stars" (Patrick,
  // after the slat era). Cap keeps the worst-case draw count sane.
  let cols=Math.max(84,Math.round(sw/6)),rows=Math.max(50,Math.round(sh/8));
  while(cols*rows>5200){cols=Math.round(cols*.94);rows=Math.round(rows*.94);}
  const cover=Math.max(sw/vw,sh/vh);
  const srcW=sw/cover,srcH=sh/cover;
  const srcX=(vw-srcW)/2,srcY=(vh-srcH)/2;
  const tw=sw/cols,th=sh/rows,stw=srcW/cols,sth=srcH/rows;
  tiles=[];
  for(let r=0;r<rows;r++)for(let c=0;c<cols;c++){
    tiles.push({ax:(c+.5)*tw,ay:(r+.5)*th,
                sx:srcX+c*stw,sy:srcY+r*sth,
                z:1,
                zj:.7+Math.random()*.9,           // depth desync = the split
                jx:(Math.random()-.5)*2,          // slight lateral drift
                jy:(Math.random()-.5)*2});
  }
  tileMeta={tw:tw,th:th,stw:stw,sth:sth};
}

let qLev=0,qAvg=8,qFrame=0;
function starTick(){
  // THE WARP IS RETIRED, per Patrick: gorgeous, but it fought the model
  // for the GPU on every query. The backdrop now carries the moment on
  // its own (a gentle CSS dim while generating) and the answer fades in
  // as it streams — all compositor work, zero canvas.
  if(starCv){starCv.style.display="none";
    try{sctx.clearRect(0,0,starCv.width,starCv.height);}catch(e){}}
}
starTick();
// PARALLAX: the city leans a few px toward the cursor — barely there,
// endlessly alive. Desktop pointers only, and never in perf mode.
if(matchMedia("(pointer:fine)").matches){
  const skv=$("#sky-color");let pxT=0,pyT=0,pxN=0,pyN=0;
  document.addEventListener("mousemove",e=>{
    pxT=(e.clientX/innerWidth-.5);pyT=(e.clientY/innerHeight-.5);
  },{passive:true});
  (function paraTick(){
    requestAnimationFrame(paraTick);
    if(document.body.classList.contains("perf")||generating)return;
    pxN+=(pxT-pxN)*.04;pyN+=(pyT-pyN)*.04;
    skv.style.transform="scale(1.05) translate("+(-pxN*16).toFixed(1)+"px,"+(-pyN*11).toFixed(1)+"px)";
  })();
}
// the brand chameleon runs on its own gentle clock — the warp loop only
// draws while a query runs, but the wordmark should match the city always
(function brandTick(ts){
  requestAnimationFrame(brandTick);
  if(!perf)paintBrandFromSky(ts||0);
})();

/* ------------------------------------------- mic: whisper voice input */
const micBtn=$("#mic");
let recording=false,recCtx=null,recProc=null,recSrc=null,recStream=null,recBuf=[];
let voiceReady=false,voicePoll=null;

function wavEncode(chunks,srIn){
  let len=0;for(const c of chunks)len+=c.length;
  let all=new Float32Array(len),o=0;
  for(const c of chunks){all.set(c,o);o+=c.length;}
  const sr=16000;
  if(srIn!==sr){                       // linear resample to 16 kHz
    const n=Math.round(all.length*sr/srIn),out=new Float32Array(n);
    for(let i=0;i<n;i++){
      const x=i*(all.length-1)/(n-1),lo=Math.floor(x),hi=Math.min(lo+1,all.length-1);
      out[i]=all[lo]+(all[hi]-all[lo])*(x-lo);
    }
    all=out;
  }
  const buf=new ArrayBuffer(44+all.length*2),v=new DataView(buf);
  const ws=(off,str)=>{for(let i=0;i<str.length;i++)v.setUint8(off+i,str.charCodeAt(i));};
  ws(0,"RIFF");v.setUint32(4,36+all.length*2,true);ws(8,"WAVE");ws(12,"fmt ");
  v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
  v.setUint32(24,sr,true);v.setUint32(28,sr*2,true);v.setUint16(32,2,true);
  v.setUint16(34,16,true);ws(36,"data");v.setUint32(40,all.length*2,true);
  for(let i=0;i<all.length;i++)
    v.setInt16(44+i*2,Math.max(-1,Math.min(1,all[i]))*32767,true);
  return new Blob([buf],{type:"audio/wav"});
}

async function ensureVoice(){
  if(voiceReady)return true;
  const st=await(await fetch("/api/voice/status")).json();
  if(!st.supported){input.placeholder="voice input needs an Apple silicon Mac";return false;}
  if(st.ready){voiceReady=true;return true;}
  await fetch("/api/voice/prepare",{method:"POST"});
  input.placeholder="getting the voice engine ("+(st.pct||0)+"%)\u2026 tap the mic again soon";
  if(!voicePoll)voicePoll=setInterval(async()=>{
    const s2=await(await fetch("/api/voice/status")).json();
    if(s2.ready){clearInterval(voicePoll);voicePoll=null;voiceReady=true;
      input.placeholder="voice ready \u2014 tap the mic and talk";}
    else input.placeholder="getting the voice engine ("+(s2.pct||0)+"%)\u2026";
  },2000);
  return false;
}

async function startRec(){
  recStream=await navigator.mediaDevices.getUserMedia({audio:true});
  recCtx=new (window.AudioContext||window.webkitAudioContext)();
  recSrc=recCtx.createMediaStreamSource(recStream);
  recProc=recCtx.createScriptProcessor(4096,1,1);
  recBuf=[];
  recProc.onaudioprocess=e=>recBuf.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  recSrc.connect(recProc);recProc.connect(recCtx.destination);
  recording=true;micBtn.classList.add("rec");
  input.placeholder="listening\u2026 tap the mic to finish";
}

async function stopRec(){
  recording=false;micBtn.classList.remove("rec");
  try{recProc.disconnect();recSrc.disconnect();}catch(e){}
  recStream.getTracks().forEach(t=>t.stop());
  const sr=recCtx.sampleRate;recCtx.close();
  input.placeholder="transcribing\u2026";
  try{
    const wav=wavEncode(recBuf,sr);recBuf=[];
    const r=await fetch("/api/transcribe",{method:"POST",body:wav});
    if(!r.ok)throw new Error("transcribe failed");
    const text=(await r.json()).text;
    input.placeholder="Message MillenAI\u2026";
    if(text){
      input.value=text;input.dispatchEvent(new Event("input"));
      if(voiceChat)send();          // voice chat: straight to the model
    }
  }catch(e){input.placeholder="couldn\u2019t transcribe \u2014 try again";}
}

micBtn.addEventListener("click",async()=>{
  if(recording){stopRec();return;}
  fetch("/api/speak",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({stop:true})});   // barge-in: stop any reply audio
  if(!(await ensureVoice()))return;
  try{await startRec();}
  catch(e){input.placeholder="microphone blocked \u2014 allow it in System Settings \u25b8 Privacy";}
});

input.focus();

/* ---------------------------------------------------- first-run setup */
const veil=$("#setup-veil"),setupList=$("#setup-list"),
      setupGo=$("#setup-go"),setupLater=$("#setup-later"),setupNote=$("#setup-note");
let setupTimer=null,setupAllReady=false;

// verified-style badge: filled disc, knocked-out tick
const TICK='<svg class="tick" viewBox="0 0 24 24" aria-label="installed">'
  +'<circle cx="12" cy="12" r="11"/>'
  +'<path d="M7 12.4l3.3 3.3L17 9" fill="none" stroke-width="2.7"'
  +' stroke-linecap="round" stroke-linejoin="round"/></svg>';

function renderSetup(st){
  const stars=st.models.filter(m=>m.star);
  setupAllReady=stars.every(m=>m.status==="ready");
  const anyDl=st.busy;
  const pct=st.overall_pct;
  // headline: overall progress across the recommended set
  let html=
    '<div class="big-bar"><i style="width:'+pct+'%"></i></div>'+
    '<div class="big-stat"><span>'+st.have_gb+' / '+st.want_gb+' GB</span>'+
    '<span>'+(anyDl?pct+'%':(setupAllReady?'complete':'not started'))+'</span></div>'+
    (anyDl?'<div class="big-speed">'+
      (st.speed_mbs>0?st.speed_mbs+' MB/s':'starting\u2026')+
      (st.eta_min?' \u00b7 about '+st.eta_min+' min left':'')+'</div>':'');

  // WHILE DOWNLOADING (first run or updates): one bar, bandwidth,
  // percent — never a wall of per-model rows
  if(anyDl){
    setTitle("Downloading updates",
      "Keep chatting \u2014 this finishes in the background.");
    setupList.innerHTML=html;
    $("#setup-later").textContent="Continue in background";
    finishSetupChrome(st,stars,anyDl);
    return;
  }
  $("#setup-later").textContent="Later";

  // FIRST RUN stays simple: the machine already picked its best brains —
  // show what it chose and one number, never the catalog. The full list
  // only exists behind "Add models…" for people who go looking.
  if(!setupManual){
    setTitle("Welcome to MillenAI",
      "We\u2019re getting you set up \u2014 private, and entirely on "
      +"this Mac. Start chatting the moment the first piece lands.");
    html+=planCards(st);
    setupList.innerHTML=html;
    wirePlans(st);
    finishSetupChrome(st,stars,anyDl);
    return;
  }

  // …then every model individually, so anything can be added on its own
  const state=m=>{
    if(m.status==="ready")   return TICK;
    if(m.status==="downloading") return '<span class="st dl">'+m.pct+'%</span>';
    if(m.status==="queued")  return '<span class="st wait">queued</span>';
    if(m.status==="error")   return '<span class="st err" title="'+esc(m.note)+'">failed</span>';
    return '<span class="st get">'+m.est_gb+' GB \u2193</span>';
  };
  // an installed model gets its name and a tick - a full progress bar on
  // something already at 100% is just noise on every row you have finished
  const row=m=>
    m.status==="ready"
      ? '<div class="setup-row done"><span class="nm">'+esc(m.label)+'</span>'
        +TICK+'</div>'
      : '<div class="setup-row clickable" data-model="'+esc(m.label)+'">'
        +'<span class="nm">'+esc(m.label)+'</span>'+state(m)
        +'<div class="bar"><i style="width:'+(m.pct||0)+'%"></i></div></div>';
  // CONSOLIDATED "Update Models" view: recommended picks up front,
  // everything else counted and folded — never a wall of every model
  // manual = the same three choices as first run, plus the sentence
  const missing=st.models.filter(m=>m.status!=="ready");
  const recs=missing.filter(m=>m.star);
  if(recs.length){
    setTitle("Updates available",
      "New models are ready for this machine \u2014 pick how much you "
      +"want. They download in the background while you keep chatting.");
    html+=planCards(st);
  }else{
    setTitle("You\u2019re up to date",
      "Every model this machine can run is installed.");
  }
  setupList.innerHTML=html;
  wirePlans(st);

  if(!st.mlx_ok){
    setupNote.textContent="engine not installed — reopen the app to finish setup";
    setupGo.disabled=true;
  }else if(stars.some(m=>m.status==="error")){
    setupNote.textContent="a download failed — check your connection, then retry";
  }else{
    setupNote.textContent="";
  }

  if(anyDl){
    setupGo.disabled=true;setupGo.textContent="Downloading\u2026";
  }else if(setupAllReady){
    setupGo.disabled=false;setupGo.textContent="Let\u2019s run it";
  }else{
    const left=(st.plans||{})[setupPlan]||0;
    setupGo.disabled=!st.mlx_ok||left<=0;
    setupGo.textContent=left<=0?"Up to date \u2713"
      :(stars.some(m=>m.status==="error")?"Retry":"Update")+
       " \u00b7 "+planGB(st)+" GB";
  }
}
// the button quotes the CHOSEN plan, not the whole catalog
function setTitle(t,s){
  const h=$("#setup-title"),p=$("#setup-sub");
  if(h)h.textContent=t;
  if(p)p.textContent=s;
}
function planCards(st){
  const rem=st.plans||{};
  const meta=[["basic","Basic","Fast answers, tiny download"],
              ["pro","Pro","Great everyday quality"],
              ["max","Max","The best this machine can run"]];
  if((rem[setupPlan]||0)<=0){
    const next=meta.find(([k])=>rem[k]>0);
    if(next)setupPlan=next[0];
  }
  return '<div class="plans">'+meta.map(([k,name,desc])=>{
    const left=rem[k]||0;
    return '<div class="plan'+(left<=0?' done':'')+'" data-plan="'+k+'">'
      +'<b>'+name+'</b><span>'+desc+'</span>'
      +'<em>'+(left<=0?'Installed \u2713':'~'+Math.max(1,Math.round(left))+' GB')+'</em></div>';
  }).join("")+'</div>';
}
function wirePlans(st){
  setupList.querySelectorAll(".plan").forEach(el=>{
    el.classList.toggle("on",el.dataset.plan===setupPlan);
    if(el.classList.contains("done"))return;
    el.addEventListener("click",()=>{
      setupPlan=el.dataset.plan;
      setupList.querySelectorAll(".plan").forEach(x=>
        x.classList.toggle("on",x===el));
      setupGo.textContent="Update \u00b7 "+planGB(st)+" GB";
    });
  });
}
function planGB(st){
  return Math.max(1,Math.round((st.plans||{})[setupPlan]||0));
}

function finishSetupChrome(st,stars,anyDl){
  if(!st.mlx_ok){
    setupNote.textContent="engine not installed \u2014 reopen the app to finish setup";
    setupGo.disabled=true;
  }else if(stars.some(m=>m.status==="error")){
    setupNote.textContent="a download failed \u2014 check your connection, then retry";
  }else{
    setupNote.textContent="";
  }
  if(anyDl){
    setupGo.disabled=true;setupGo.textContent="Downloading\u2026";
  }else if(setupAllReady){
    setupGo.disabled=false;setupGo.textContent="Let\u2019s run it";
  }else{
    const left=(st.plans||{})[setupPlan]||0;
    setupGo.disabled=!st.mlx_ok||left<=0;
    setupGo.textContent=left<=0?"Up to date \u2713"
      :(stars.some(m=>m.status==="error")?"Retry":"Update")+
       " \u00b7 "+planGB(st)+" GB";
  }
}

/* The rainbow wipe — a diagonal band of light crosses the window, then
   collapses into the wordmark. Shared by the app-open flourish and the
   downloads-complete celebration so the two are always identical. */
let wipeBusy=false;
function rainbowWipe(){
  const cel=$("#celebrate");
  if(perf||!cel||wipeBusy)return;         // performance mode: no theatre
  wipeBusy=true;
  cel.hidden=false;
  cel.innerHTML='<div class="sweep"></div>';
  // the wordmark flies in under the band. Measure first — once .flyin is on,
  // the element is scaled and the rect no longer describes its resting place.
  const hero1=$("#hero h1");
  if(hero1){
    hero1.classList.add("flyin");
    [$("#hero .beta-tag"),$("#hero .greet")].forEach(e=>{
      if(e)e.classList.add("flyin");
    });
  }
  // the band paints the wordmark on its way past: arm the transition, then
  // flip the end state on the next frame so it actually animates
  document.body.classList.add("painting");
  requestAnimationFrame(()=>document.body.classList.add("painted"));
  setTimeout(()=>{
    const h1=$("#hero h1");
    if(h1)h1.classList.remove("flyin");
    [$("#hero .beta-tag"),$("#hero .greet")].forEach(e=>{
      if(e)e.classList.remove("flyin");
    });
  },2700);
  setTimeout(()=>{
    cel.hidden=true;cel.innerHTML="";
    // leave `painted` on — the colour stays where the band left it. Set it
    // here too: the animated add rides an animation frame, and if the window
    // was occluded that frame never came.
    document.body.classList.add("painted");
    document.body.classList.remove("painting");
    wipeBusy=false;
  },6400);
}

let wasDownloading=false;
// true when the panel was opened to add models rather than by first-run setup
let setupManual=false;
let setupPlan="pro";
function celebrateDownloads(){
  const card=$("#setup-card"),veil=$("#setup-veil");
  if(perf){closeSetup();return;}          // performance mode: no theatre
  // the card grows and dissolves, then the wipe runs
  card.classList.add("done");veil.classList.add("fading");
  setTimeout(()=>{
    closeSetup();card.classList.remove("done");veil.classList.remove("fading");
    rainbowWipe();
  },910);
}

async function setupTick(){
  try{
    const st=await(await fetch("/api/setup")).json();
    renderSetup(st);
    pollEngines();
    if(st.busy)wasDownloading=true;
    else if(wasDownloading&&setupAllReady&&!veil.hidden&&!setupManual){
      // only first-run setup finishes with the celebration; when the panel
      // was opened to add models it stays open until it is dismissed
      wasDownloading=false;celebrateDownloads();
    }else if(!st.busy){
      wasDownloading=false;
    }
  }catch(e){}
}
function openSetup(){
  setupManual=true;
  veil.hidden=false;setupTick();
  if(!setupTimer)setupTimer=setInterval(setupTick,1200);
}
function closeSetup(){veil.hidden=true;if(setupTimer){clearInterval(setupTimer);setupTimer=null;}input.focus();}
setupLater.addEventListener("click",closeSetup);
setupGo.addEventListener("click",async()=>{
  if(setupAllReady){closeSetup();return;}
  await fetch("/api/setup/install",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({plan:setupPlan})});
  setupTick();
});
$("#open-setup").addEventListener("click",()=>{aboutVeil.hidden=true;openSetup();});
// PRELOAD: warm a handful of uncached clips so later launches open fast
// — the loading bar stays, it just has far less to do
$("#about-preload").addEventListener("click",async()=>{
  const btn=$("#about-preload");
  if(btn.dataset.busy)return;
  btn.dataset.busy="1";
  try{
    const have=new Set(((await(await fetch("/api/sky/cached")).json())
      .cached)||[]);
    const want=[];
    for(let n=0;n<SKY_N&&want.length<6;n++)if(!have.has(n))want.push(n);
    if(!want.length){
      btn.textContent="All cached \u2713";
    }else{
      for(let k=0;k<want.length;k++){
        btn.textContent="Preloading "+(k+1)+"/"+want.length+"\u2026";
        // one download lane server-side: queue it, then wait for it
        await fetch("/api/sky/status?i="+want[k]+"&warm=1").catch(()=>{});
        for(let t=0;t<200;t++){
          let st={status:"error"};
          try{st=await(await fetch("/api/sky/status?i="+want[k])).json();}
          catch(e){}
          if(st.status==="ready"||st.status==="error")break;
          await new Promise(r=>setTimeout(r,1500));
        }
      }
      btn.textContent="Done \u2713";
    }
  }catch(e){btn.textContent="Preload backdrops";}
  setTimeout(()=>{btn.textContent="Preload backdrops";
    delete btn.dataset.busy;},3000);
});
$("#models-up").addEventListener("click",openSetup);
// ONE-TIME invitation, and never during the show: it waits for the
// rainbow wipe to LAND (body.painted, wipeBusy clear) so the card never
// crowds the boot flourish. Marked seen the moment it appears, so it is
// genuinely once — answered or not.
(async function shareInvite(){
  try{
    const pr=await(await fetch("/api/prefs")).json();
    if(pr.seen_share||pr.contrib_on)return;
    const st=await(await fetch("/api/setup")).json();
    if(st.needs_setup||st.busy)return;      // let them finish setting up
    const t0=Date.now();
    const wait=setInterval(()=>{
      const settled=document.body.classList.contains("painted")&&!wipeBusy;
      if(!settled&&Date.now()-t0<20000)return;   // 20s failsafe
      clearInterval(wait);
      setTimeout(()=>{
        if(!veil.hidden||!aboutVeil.hidden)return;   // never stack modals
        $("#share-veil").hidden=false;
        fetch("/api/prefs",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({seen_share:true})});
      },900);
    },400);
  }catch(e){}
})();
function shareDone(on){
  $("#share-veil").hidden=true;
  const cb=$("#contrib");if(cb&&on)cb.checked=true;
  if(on)fetch("/api/prefs",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({contrib_on:true})});
}
$("#dlhelp-ok").addEventListener("click",()=>{
  $("#dlhelp-veil").hidden=true;});
$("#share-no").addEventListener("click",()=>shareDone(false));
$("#share-yes").addEventListener("click",()=>shareDone(true));
// WEB ONLY: a browser visitor is borrowing someone else's GPU — offer
// them the real app for their own platform
(async()=>{
  if(location.hostname==="127.0.0.1"||location.hostname==="localhost")return;
  try{
    const d=await(await fetch("/api/downloads")).json();
    const ua=navigator.userAgent||"";
    const win=/Windows|Win64|WOW64/i.test(ua);
    const mac=/Mac OS X|Macintosh/i.test(ua);
    const mobile=/iPhone|iPad|Android/i.test(ua);
    const url=win?(d.win||d.win_zip):(mac?d.mac:null);
    if(mobile||!url)return;
    const a=$("#get-app");
    a.href=url;a.hidden=false;
    a.firstChild.textContent="DOWNLOAD "+(win?"FOR WINDOWS":"FOR MAC");
    // FIRST-OPEN HELP: MillenAI is free and unsigned by Apple/Microsoft,
    // so the OS blocks the first launch. Say so plainly, at the moment
    // of the download, in the words the dialogs actually use.
    a.addEventListener("click",()=>{
      $("#dlhelp-body").innerHTML=win
        ? "Windows may say <b>&ldquo;Windows protected your PC&rdquo;</b>."
          +"<br><br>1. Open the downloaded file<br>"
          +"2. Click <b>More info</b><br>"
          +"3. Click <b>Run anyway</b><br><br>"
          +"That happens because MillenAI is free and independent \u2014 "
          +"it only ever runs on your own computer."
        : "Mac will say it <b>&ldquo;cannot be opened&rdquo;</b> or "
          +"<b>&ldquo;Apple could not verify&rdquo;</b> the first time. "
          +"That is normal for a free app.<br><br>"
          +"1. Open the downloaded file and drag <b>MillenAI</b> into "
          +"<b>Applications</b><br>"
          +"2. Open it once \u2014 Mac will refuse<br>"
          +"3. Go to <b>System Settings \u25b8 Privacy &amp; Security</b>, "
          +"scroll down and click <b>Open Anyway</b><br><br>"
          +"You only do this once.";
      $("#dlhelp-veil").hidden=false;
    });
  }catch(e){}
})();
(async()=>{try{
  $("#nolimits").checked=!!(await(await fetch("/api/prefs")).json()).no_limits;
}catch(e){}})();
$("#nolimits").addEventListener("change",async()=>{
  await fetch("/api/prefs",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({no_limits:$("#nolimits").checked})});
  setupTick();   // the plans + GB re-price under the new rules
});
$("#models-flag").addEventListener("click",()=>{openSetup();});
function paintModelsFlag(st){
  const f=$("#models-flag");
  if(st.busy){
    f.hidden=!veil.hidden?true:false;
    f.textContent="DOWNLOADING MODELS \u00b7 "+st.overall_pct+"%";
    f.style.background="linear-gradient(90deg,#e26d5a "+st.overall_pct
      +"%,#4b1f18 "+st.overall_pct+"%)";
  }else{
    f.style.background="";
    f.textContent="MODELS AVAILABLE";
    f.hidden=!(st.mlx_ok&&!st.needs_setup&&
      st.models.some(m=>m.star&&m.status!=="ready"));
  }
}
// keep the chip honest while downloads run behind a closed panel
(function flagTick(){
  const busyish=$("#models-flag").textContent.startsWith("DOWNLOADING");
  setTimeout(async()=>{
    if(veil.hidden){
      try{paintModelsFlag(await(await fetch("/api/setup")).json());}
      catch(e){}
    }
    flagTick();
  },busyish?6000:240000);
})();
// Every launch opens with the wipe. It deliberately does *not* wait on the
// /api/setup round trip below — that call enumerates every model on disk and
// can take seconds, which would leave the window sitting there looking frozen
// before the flourish finally played.
// rAF for the fast path, a timeout as the guarantee: an occluded window gets
// NO animation frames, and a wipe that never runs would leave the wordmark
// grey forever — `painted` is only ever set by the wipe.
let wipeKicked=false,wwDone=false;
function winWipeFinish(){
  if(wwDone)return;wwDone=true;
  // dropping the classes restores body's normal background and clip in one
  // move; the native window is re-opaqued by a timer on the Python side
  document.documentElement.classList.remove("winwipe","winwipe-run");
  rainbowWipe();
}
function winWipeRun(){
  const root=document.documentElement;
  // double rAF: the clipped-to-nothing state must be committed before the
  // animation class lands, or WebKit coalesces them and nothing wipes
  requestAnimationFrame(()=>requestAnimationFrame(()=>root.classList.add("winwipe-run")));
  document.body.addEventListener("animationend",e=>{
    if(e.animationName==="winWipe")winWipeFinish();
  });
  setTimeout(winWipeFinish,1600);   // occluded-window guarantee
}
function kickWipe(){
  if(wipeKicked)return;wipeKicked=true;
  // native Mac boot: the window wipes in from the right first, and the
  // rainbow answers from the left inside winWipeFinish
  if(document.documentElement.classList.contains("winwipe"))winWipeRun();
  else rainbowWipe();
}
requestAnimationFrame(kickWipe);
setTimeout(kickWipe,450);
(async()=>{
  try{
    const st=await(await fetch("/api/setup")).json();
    // auto-open only when the app can't hold a conversation yet
    paintModelsFlag(st);
    if(st.needs_setup){
      // first run opens on the Basic / Pro / Max choice — the pick is
      // the only decision, then everything is automatic
      openSetup();setupManual=false;
    }
  }catch(e){}
})();

/* ------------------------------------------------------ mobile drawer */
$("#mburger").addEventListener("click",e=>{
  e.stopPropagation();
  document.body.classList.toggle("sbopen");
});
// tapping the chat area closes the drawer
$("#main").addEventListener("click",()=>{
  document.body.classList.remove("sbopen");
});

/* -------------------------------------------------- resizable sidebar */
const sidebarEl=$("#sidebar"),SB_MIN=210,SB_MAX=560;
function setSidebar(w){
  w=Math.max(SB_MIN,Math.min(SB_MAX,Math.round(w)));
  sidebarEl.style.width=w+"px";sidebarEl.style.minWidth=w+"px";
  // anything centred on the MAIN panel (the Loading bar) reads this
  document.documentElement.style.setProperty("--sbw",w+"px");
  localStorage.setItem("millen.sbw",w);
}
const savedW=parseInt(localStorage.getItem("millen.sbw")||"0",10);
if(savedW)setSidebar(savedW);
$("#sb-resize").addEventListener("mousedown",e=>{
  e.preventDefault();document.body.classList.add("resizing");
  const move=ev=>setSidebar(ev.clientX-sidebarEl.getBoundingClientRect().left);
  const up=()=>{document.body.classList.remove("resizing");
    window.removeEventListener("mousemove",move);
    window.removeEventListener("mouseup",up);};
  window.addEventListener("mousemove",move);
  window.addEventListener("mouseup",up);
});
$("#sb-resize").addEventListener("dblclick",()=>setSidebar(384));

/* ---------------------------------------------------------------- about */
const aboutVeil=$("#about-veil");
async function openAbout(){
  aboutVeil.hidden=false;
  try{
    const pr=await(await fetch("/api/prefs")).json();
    $("#persona").value=pr.persona||"";
  }catch(e){}
  try{
    const [m,st]=await Promise.all([
      (await fetch("/api/memory")).json(),
      (await fetch("/api/setup")).json()]);
    if(st.plat)$("#about-name").innerHTML="MillenAI <em>"+esc(st.plat)+"</em>";
    try{
      const fs=await(await fetch("/api/fleet/status")).json();
      if(fs.key!==undefined){
        $("#fleet-own").hidden=false;
        $("#fleet-n").textContent="Your fleet: "+fs.workers.length+" friend"
          +(fs.workers.length===1?"":"s")+" online"
          +(fs.workers.some(w=>w.busy)?" \u00b7 working":"");
        $("#fleet-pending").innerHTML=(fs.pending||[]).map(p=>
          '<div class="preq">\u26a1 '+esc(p.name)
          +' wants to contribute<button data-id="'+esc(p.id)
          +'">Approve</button></div>').join("");
        $("#fleet-pending").querySelectorAll("button").forEach(b=>
          b.addEventListener("click",async()=>{
            await fetch("/api/fleet/approve",{method:"POST",
              headers:{"Content-Type":"application/json"},
              body:JSON.stringify({id:b.dataset.id})});
            b.closest(".preq").remove();
          }));
      }
    }catch(e){}
    try{
      const pr2=await(await fetch("/api/prefs")).json();
      const mine=await(await fetch("/api/fleet/mine")).json();
      $("#turbo").checked=!!pr2.turbo;
      $("#contrib").checked=!!pr2.contrib_on;
      try{
        const cs=await(await fetch("/api/cloud")).json();
        $("#turbo-row").hidden=!cs.configured;
        if(cs.name)$("#turbo-hint").title=
          "Answers come from "+cs.name+" instead of this Mac \u2014 much "
          +"faster, but your prompts leave this computer while it is on.";
      }catch(e){}
      $("#contrib-url").value=pr2.contrib_url||"";
      $("#contrib-state").textContent=
        pr2.contrib_on&&mine.state!=="off"?mine.state:"";
    }catch(e){}
    const ready=st.models.filter(x=>x.status==="ready").length;
    $("#about-facts").textContent=
      st.arch+" · "+ready+"/"+st.models.length+" models ready";
  }catch(e){$("#about-facts").textContent="";}
}
/* ------------------------------------------- new models in this release */
// Two tiers of model discovery, one card. Models the user has NEVER been
// offered are announced once with a download button (a release adding models
// must surface them). Beyond that, a gentle daily nudge points at anything
// still uninstalled — primary action is Browse, never download-everything
// (the full missing set can top 100 GB), and it carries its own permanent
// opt-out. At most one card per launch, and never during first-run setup.
const REMIND_GAP=20*60*60*1000;       // "daily", forgiving of launch times
async function announceModels(){
  try{
    const [st,prefs]=await Promise.all([
      (await fetch("/api/setup")).json(),
      (await fetch("/api/prefs")).json()]);
    if(st.needs_setup)return;         // the installer owns the screen
    const seen=prefs.seen_models||[];
    const all=st.models.map(m=>m.label);
    const stamp=extra=>fetch("/api/prefs",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(Object.assign({seen_models:all,
        remind_models_ts:Date.now()},extra||{}))});
    if(!seen.length){await stamp();return;}   // first run: nothing is "new"

    const veil=$("#new-veil"),title=document.querySelector("#new-veil #about-name");
    const missing=st.models.filter(m=>m.status!=="ready"&&m.supported!==false);
    const fresh=missing.filter(m=>seen.indexOf(m.label)<0);

    if(fresh.length){                 // tier 1: genuinely new — announce once
      const gb=fresh.reduce((a,m)=>a+m.est_gb,0);
      title.textContent="New models available";
      $("#up-detail").textContent="This version adds models you don\u2019t have yet.";
      $("#new-list").innerHTML=fresh.slice(0,4).map(m=>
        "<div class='mrow'><span class='mname'>"+esc(m.label)+
        "</span><span class='msize'>"+m.est_gb+" GB</span></div>").join("")+
        (fresh.length>4?"<div class='mmore'>+"+(fresh.length-4)+" more</div>":"");
      $("#new-get").textContent="Download \u00b7 "+gb.toFixed(1)+" GB";
      $("#new-off").hidden=true;
      veil.hidden=false;
      $("#new-skip").onclick=async()=>{veil.hidden=true;await stamp();};
      $("#new-get").onclick=async()=>{
        veil.hidden=true;await stamp();
        await fetch("/api/model/download",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({labels:fresh.map(m=>m.label)})});
        openSetup();                  // watch them come down
      };
      return;
    }

    // tier 2: the daily nudge
    if(prefs.remind_models_off)return;
    if(!missing.length)return;
    if(Date.now()-(prefs.remind_models_ts||0)<REMIND_GAP)return;
    title.textContent="More models available";
    $("#up-detail").textContent="Your computer can run "+missing.length+
      " more model"+(missing.length===1?"":"s")+".";
    $("#new-list").innerHTML=missing.slice(0,4).map(m=>
      "<div class='mrow'><span class='mname'>"+esc(m.label)+
      "</span><span class='msize'>"+m.est_gb+" GB</span></div>").join("")+
      (missing.length>4?"<div class='mmore'>+"+(missing.length-4)+" more</div>":"");
    $("#new-get").textContent="Browse models";
    $("#new-off").hidden=false;
    veil.hidden=false;
    $("#new-skip").onclick=async()=>{veil.hidden=true;await stamp();};
    $("#new-get").onclick=async()=>{veil.hidden=true;await stamp();openSetup();};
    $("#new-off").onclick=async()=>{
      veil.hidden=true;await stamp({remind_models_off:true});};
  }catch(e){}
}
setTimeout(announceModels,2500);      // after the first paint

$("#settings-btn").addEventListener("click",openAbout);
$("#persona-save").addEventListener("click",async ev=>{
  const b=ev.currentTarget;
  try{
    await fetch("/api/prefs",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({persona:$("#persona").value.trim()})});
    b.textContent="Saved \u2713";
  }catch(e){b.textContent="Couldn\u2019t save";}
  setTimeout(()=>{b.textContent="Save preferences";},1800);
});
$("#turbo").addEventListener("change",()=>{
  fetch("/api/prefs",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({turbo:$("#turbo").checked})});
});
$("#contrib").addEventListener("change",async()=>{
  const on=$("#contrib").checked;
  $("#contrib-state").textContent=on?"connecting\u2026":"";
  await fetch("/api/prefs",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({contrib_on:on,
      contrib_url:$("#contrib-url").value.trim()})});
});
$("#about-close").addEventListener("click",()=>{aboutVeil.hidden=true;});
aboutVeil.addEventListener("click",e=>{if(e.target===aboutVeil)aboutVeil.hidden=true;});
$("#about-check").addEventListener("click",async ev=>{
  const b=ev.currentTarget,was=b.textContent;
  b.disabled=true;b.textContent="Checking\u2026";
  try{
    const r=await(await fetch("/api/update/check")).json();
    if(!r.configured){b.textContent="Updates not configured";}
    else if(r.available){
      upInfo=r;$("#update-flag").hidden=false;
      b.textContent="Update to "+r.latest;
      b.disabled=false;
      b.onclick=()=>{aboutVeil.hidden=true;openUpdate();};
      return;
    }
    else b.textContent=r.note?("No update \u2014 "+r.note)
                             :"You\u2019re up to date";
  }catch(e){b.textContent="Couldn\u2019t reach GitHub";}
  setTimeout(()=>{b.textContent=was;b.disabled=false;},2600);
});
$("#about-forget").addEventListener("click",async ev=>{
  const b=ev.currentTarget;
  if(b.dataset.sure!=="1"){
    b.dataset.sure="1";b.textContent="Really forget everything? Click again";return;
  }
  await fetch("/api/memory/clear",{method:"POST"});
  b.dataset.sure="";b.textContent="Memory cleared";
  openAbout();
  setTimeout(()=>{b.textContent="Forget me";},2500);
});

/* --------------------------------------------------------- self-update */
const upVeil=$("#update-veil"),upBar=$("#up-bar"),upGo=$("#up-go");
let upInfo=null;
async function checkUpdate(){
  try{
    const r=await(await fetch("/api/update/check")).json();
    if(r.available){
      upInfo=r;$("#update-flag").hidden=false;
    }
  }catch(e){}
}
function openUpdate(){
  if(!upInfo)return;
  $("#up-ver").textContent=upInfo.latest+"  \u2022  you have "+upInfo.current;
  $("#up-detail").textContent=
    "Downloads "+upInfo.size_mb+" MB from GitHub, then restarts. "+
    "Your chats and everything it remembers are kept.";
  upVeil.hidden=false;
}
$("#update-flag").addEventListener("click",openUpdate);
$("#up-later").addEventListener("click",()=>{upVeil.hidden=true;});
upGo.addEventListener("click",async()=>{
  upGo.disabled=true;upGo.textContent="Downloading\u2026";
  upBar.hidden=false;
  await fetch("/api/update/install",{method:"POST"});
  const poll=setInterval(async()=>{
    let st;try{st=await(await fetch("/api/update/status")).json();}
    catch(e){return;}   // the app is restarting — the fetch will fail
    upBar.querySelector("i").style.width=(st.pct||0)+"%";
    if(st.state==="installing")upGo.textContent="Installing\u2026";
    if(st.state==="restarting"){
      clearInterval(poll);
      upGo.textContent="Restarting\u2026";
      $("#up-detail").textContent="MillenAI is reopening with the new version.";
    }
    if(st.state==="error"){
      clearInterval(poll);upGo.disabled=false;upGo.textContent="Try again";
      $("#up-detail").textContent="Update failed: "+(st.note||"unknown error");
    }
  },700);
});
const DAY=86400000;
function maybeCheckUpdate(){
  const last=parseInt(localStorage.getItem("millen.upcheck")||"0",10);
  if(Date.now()-last<DAY)return;
  localStorage.setItem("millen.upcheck",Date.now());
  checkUpdate();
}
maybeCheckUpdate();                 // once on launch, at most once a day
setInterval(maybeCheckUpdate,3600000);

input.focus();
</script>
</body>
</html>
"""


_mlx_last_use = 0.0


def _mlx_janitor():
    """An MLX engine held its full model in RAM FOREVER after last use —
    the always-on instance pinned 17 GB around the clock and the music
    skipped. Five idle minutes and the engine is released; the next
    question just pays the reload."""
    while True:
        time.sleep(60)
        try:
            if _mlx_procs and _mlx_last_use and \
                    time.time() - _mlx_last_use > 300:
                with _engine_lock:
                    if time.time() - _mlx_last_use > 300:
                        _stop_other_mlx("")   # no keeper: stop them all
        except Exception:
            pass


# ---------------------------------------------------- version splash
# After an update installs, the FIRST launch of the new build throws a
# frameless transparent always-on-top window over the whole screen:
# WELCOME TO x.y.z zooms out of a blur, a light band sweeps it, and the
# window destroys itself. Pure theatre, ~3 seconds, Mac only.
SPLASH_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:transparent;overflow:hidden}
#w{position:fixed;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;
  animation:allout .5s ease 2.3s forwards}
#hello{font-family:'Helvetica Neue',sans-serif;font-size:22px;
  letter-spacing:.55em;color:#cfcfcf;text-transform:uppercase;
  text-shadow:0 2px 18px rgba(0,0,0,.8);opacity:0;
  animation:helloIn .5s ease .25s forwards}
#v{font-family:'Helvetica Neue',sans-serif;font-weight:800;
  font-size:150px;letter-spacing:.02em;margin-top:6px;
  background:linear-gradient(90deg,#ff8f8f,#ffc46e,#f5e663,#7ef0a6,
             #6ec7ff,#8f9dff,#c98fff);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  filter:drop-shadow(0 4px 30px rgba(120,140,255,.45));
  animation:vIn 1.1s cubic-bezier(.16,.8,.24,1) both}
#flash{position:fixed;inset:-20%;pointer-events:none;opacity:0;
  background:linear-gradient(105deg,transparent 30%,
             rgba(255,255,255,.9) 50%,transparent 70%);
  background-size:300% 100%;background-position:120% 0;
  mix-blend-mode:screen;
  animation:sweep .7s ease-out 1.15s forwards}
@keyframes vIn{
  0%{opacity:0;transform:scale(3.2);filter:blur(40px)
     drop-shadow(0 4px 30px rgba(120,140,255,0))}
  55%{opacity:1;filter:blur(4px)
     drop-shadow(-10px 0 rgba(255,60,90,.7))
     drop-shadow(10px 0 rgba(60,170,255,.7))}
  100%{opacity:1;transform:scale(1);filter:blur(0)
     drop-shadow(0 4px 30px rgba(120,140,255,.45))}}
@keyframes helloIn{to{opacity:1}}
@keyframes sweep{0%{opacity:1;background-position:120% 0}
  100%{opacity:0;background-position:-20% 0}}
@keyframes allout{to{opacity:0;transform:scale(1.07)}}
#aura{position:fixed;left:50%;top:50%;width:900px;height:900px;
  transform:translate(-50%,-50%) scale(.3);border-radius:50%;opacity:0;
  background:conic-gradient(from 0deg,rgba(255,143,143,.5),
    rgba(255,196,110,.5),rgba(245,230,99,.5),rgba(126,240,166,.5),
    rgba(110,199,255,.5),rgba(143,157,255,.5),rgba(201,143,255,.5),
    rgba(255,143,143,.5));
  filter:blur(70px);mix-blend-mode:screen;
  animation:auraIn 1.4s cubic-bezier(.16,.8,.3,1) .15s both,
            auraSpin 3s linear infinite,allout .5s ease 2.3s forwards}
@keyframes auraIn{0%{opacity:0;transform:translate(-50%,-50%) scale(.25)}
  100%{opacity:.9;transform:translate(-50%,-50%) scale(1.25)}}
@keyframes auraSpin{to{filter:blur(70px) hue-rotate(360deg)}}
.spk{position:fixed;left:50%;top:52%;width:6px;height:6px;
  border-radius:50%;mix-blend-mode:screen;opacity:0;
  animation:spkOut .9s cubic-bezier(.1,.75,.2,1) 1.15s forwards}
@keyframes spkOut{0%{opacity:0;transform:translate(0,0) scale(1)}
  10%{opacity:1}
  100%{opacity:0;transform:translate(var(--dx),var(--dy)) scale(.3)}}
</style></head><body>
<div id="aura"></div>
<div id="w"><div id="hello">Welcome to</div><div id="v">__V__</div></div>
<div id="flash"></div>
<script>
for(let i=0;i<30;i++){
  const s=document.createElement("div");s.className="spk";
  const a=Math.random()*Math.PI*2,d=180+Math.random()*380;
  s.style.setProperty("--dx",Math.round(Math.cos(a)*d)+"px");
  s.style.setProperty("--dy",Math.round(Math.sin(a)*d*.6)+"px");
  s.style.background="hsl("+Math.round(Math.random()*360)+" 100% 70%)";
  s.style.boxShadow="0 0 12px 2px "+s.style.background;
  document.body.appendChild(s);
}
</script>
</body></html>"""


def maybe_version_splash():
    """Show the WELCOME splash on the first launch of a NEW version."""
    try:
        prefs = load_prefs()
        last = prefs.get("last_version")
        if last == APP_VERSION:
            return
        prefs["last_version"] = APP_VERSION
        store_prefs(prefs)
        if last is None or not (HAS_WEBVIEW and IS_MAC):
            return          # fresh install gets the boot wipe, not this
        w = webview.create_window(
            "", html=SPLASH_HTML.replace("__V__", short_version()),
            frameless=True, transparent=True, on_top=True,
            width=1100, height=420, focus=False)

        def _bye():
            try:
                w.destroy()
            except Exception:
                pass
        threading.Timer(3.1, _bye).start()
    except Exception:
        pass          # theatre must never block the app


def reap_orphan_engines():
    """MLX engines whose parent died keep multi-GB of WIRED Metal memory
    pinned forever (their RSS reads ~0, which is how it hid). Seen live:
    NINE orphans (ppid 1) starved two 12B models into OOM mid-answer.
    At boot, any listener on our engine ports whose parent is init and
    whose command looks like a python server is a corpse — reap it.
    Engines owned by a living instance (desktop AND the go-live service
    coexist) have that instance as their parent and are left alone."""
    if IS_WIN:
        return
    ports = sorted({i["port"] for i in MODEL_INFO.values() if i["port"]})
    for port in ports:
        try:
            pids = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5).stdout.split()
            for pid in pids:
                ppid = subprocess.run(
                    ["ps", "-o", "ppid=", "-p", pid],
                    capture_output=True, text=True, timeout=5).stdout.strip()
                cmd = subprocess.run(
                    ["ps", "-o", "command=", "-p", pid],
                    capture_output=True, text=True, timeout=5).stdout
                if ppid == "1" and ("ython" in cmd or "mlx" in cmd):
                    os.kill(int(pid), signal.SIGTERM)
                    print(f"  reaped orphan engine on :{port} (pid {pid})")
        except Exception:
            pass


if __name__ == "__main__":
    threading.Thread(target=start_backend, daemon=True).start()
    print(f"\n  MillenAI {short_version()}")
    print(f"  running on http://127.0.0.1:{PORT}")
    reap_orphan_engines()
    maybe_version_splash()
    threading.Thread(target=_mlx_janitor, daemon=True).start()
    contrib_apply()   # resume Contribute mode if it was left on
    start_managed_engines()
    if not HAS_SEARCH:
        print("  (web search disabled — pip install ddgs to enable)")
    if not HAS_PSUTIL:
        print("  (telemetry simulated — pip install psutil for real numbers)")
    print()
    url = f"http://127.0.0.1:{PORT}"
    if ACCESS_KEY:
        url += "?key=" + ACCESS_KEY   # the app window authenticates itself

    if HAS_WEBVIEW and IS_MAC:
        # WKWebView ships with getUserMedia dead in two separate ways, and
        # both fail as a silent hang, not an error (measured: the promise
        # neither resolves nor rejects). 1) media devices are OFF at the
        # preferences level until the private 'mediaDevicesEnabled' flag is
        # set — Safari sets it, embedders must too (via KVC). 2) pywebview's
        # UIDelegate never implements requestMediaCapturePermission, and
        # WebKit waits forever on a decision that never comes. After both,
        # macOS TCC shows the normal one-time mic prompt (the usage string
        # is in Info.plist). Guarded top to bottom: if any of this bridging
        # breaks in a future pywebview, voice degrades — the window opens.
        try:
            import objc
            from webview.platforms import cocoa as _cocoa

            def _grant_mic(self, wv, origin, frame, media_type, handler):
                handler(1)          # WKPermissionDecisionGrant

            _sel = objc.selector(
                _grant_mic,
                selector=b"webView:requestMediaCapturePermissionForOrigin:"
                         b"initiatedByFrame:type:decisionHandler:",
                signature=b"v@:@@@q@?")
            objc.classAddMethods(_cocoa.BrowserView.BrowserDelegate, [_sel])

            _bv_init = _cocoa.BrowserView.__init__

            def _bv_init_media(self, window):
                _bv_init(self, window)
                try:
                    prefs = self.webview.configuration().preferences()
                    for _k in ("mediaDevicesEnabled", "mediaStreamEnabled"):
                        try:
                            prefs.setValue_forKey_(True, _k)
                        except Exception:
                            pass
                except Exception:
                    pass
                # Window-wipe boot: the NSWindow starts fully transparent so
                # the page (clipped to nothing, see html.winwipe) can wipe in
                # over the desktop. Everything is restored by a timer rather
                # than a JS bridge — if the page-side wipe dies, the window
                # still becomes a normal opaque window 2s in. Shadow is off
                # during the wipe because macOS computes it from the opaque
                # content outline and does not track a moving clip edge.
                try:
                    from AppKit import NSColor, NSTimer

                    self.window.setOpaque_(False)
                    self.window.setBackgroundColor_(NSColor.clearColor())
                    self.window.setHasShadow_(False)
                    try:
                        self.webview.setValue_forKey_(False, "drawsBackground")
                    except Exception:
                        pass
                    try:
                        self.webview.setUnderPageBackgroundColor_(
                            NSColor.clearColor())
                    except Exception:
                        pass

                    _win = self.window

                    def _resolidify(_timer=None):
                        try:
                            _win.setBackgroundColor_(
                                NSColor.colorWithSRGBRed_green_blue_alpha_(
                                    0x21 / 255, 0x21 / 255, 0x21 / 255, 1.0))
                            _win.setOpaque_(True)
                            _win.setHasShadow_(True)
                            _win.invalidateShadow()
                        except Exception:
                            pass

                    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                        2.0, False, _resolidify)
                except Exception:
                    pass

            _cocoa.BrowserView.__init__ = _bv_init_media
        except Exception:
            pass

    if os.environ.get("MILLENAI_HEADLESS") == "1":
        # go-live service mode: no window, no browser tab — just the server.
        # A LaunchAgent must never call webbrowser.open (it lands in the
        # user's face) or pywebview (it needs a WindowServer session).
        print("  headless — serving, no window. ctrl-c to stop.\n")
        try:
            while True:
                time.sleep(100)
        except KeyboardInterrupt:
            print("\n  shutting down. o7\n")
    elif HAS_WEBVIEW:
        # Native macOS window (WKWebView). Blocks until the window closes.
        window = webview.create_window(
            f"MillenAI {short_version()}",
            url,
            width=1320,
            height=860,
            min_size=(940, 620),
            background_color="#0f1117",
            text_select=True,   # pywebview blocks selection by default
        )
        webview.start()
        print("  window closed — shutting down. o7\n")
    else:
        print("  (browser mode — pip install pywebview for a native window)")
        time.sleep(0.8)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(100)
        except KeyboardInterrupt:
            print("\n  shutting down. o7\n")
