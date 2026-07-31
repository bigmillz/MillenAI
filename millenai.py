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
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import http.server
import socketserver
import urllib.request
import urllib.error
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

APP_VERSION = "1.0"   # bump here — UI, window, DMG all follow
APP_BUILD = 14               # integer compared against the GitHub release tag
APP_BUILD_DATE = ""         # ISO date; blank falls back to this file's mtime

# Set to "youruser/yourrepo" once this is on GitHub. Publish each build as a
# Release whose tag ends in the build number (e.g. "v5") with the .dmg
# attached; the app then offers a one-click in-place update.
UPDATE_REPO = "bigmillz/MillenAI"

PORT = 8889

# delimiter for out-of-band progress lines in the chat stream — the UI
# strips these so they never appear inside an answer
NUL = chr(0)

SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "You are a warm, authentic, adaptive, and insightful AI collaborator. "
        "Avoid sounding like a rigid textbook, robot, or bullet-point generator. "
        "Speak naturally in clear, engaging prose as if talking to a smart peer. "
        "Be direct, concise, and grounded—use brief analogies and natural human "
        "warmth instead of formal fluff."
    ),
}

# MLX needs Apple silicon; on Intel Macs the starter models run on Ollama
# (CPU) instead, so the same app works everywhere.
IS_ARM = platform.machine() == "arm64"


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
    ("Qwen 2.5 7B",        "🧭", "7B",  "core", "mlx-community/Qwen2.5-7B-Instruct-4bit",          "qwen2.5:7b",        8896,  5.0,  4.3, False),
    ("Qwen 2.5 Coder 7B",  "💻", "7B",  "code", "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",    "qwen2.5-coder:7b",  8898,  5.0,  4.3, False),
    ("Qwen 2.5 Coder 14B", "🛠️", "14B", "code", "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit",   "qwen2.5-coder:14b", 8900,  9.5,  8.1, False),
    ("Phi-4 14B",          "🔬", "14B", "core", "mlx-community/phi-4-4bit",                        "phi4:14b",          8902,  9.5,  8.2, False),
    ("DeepSeek R1 7B",     "🧠", "7B",  "core", "mlx-community/DeepSeek-R1-Distill-Qwen-7B-4bit",  "deepseek-r1:7b",    8904,  5.0,  4.3, False),
    ("Mistral Small 24B",  "🧊", "24B", "big",  "mlx-community/Mistral-Small-24B-Instruct-2501-4bit", "mistral-small:24b", 8906, 15.0, 13.0, False),
    ("LLaVA Vision 7B",    "👁️", "7B",  "code", None,                                              "llava:7b",          None,  5.0,  4.7, False),
    ("Command R 35B",      "🔮", "35B", "big",  None,                                              "command-r",         None, 20.0, 18.0, False),
    ("Llama 3.3 70B",      "🐋", "70B", "big",  None,                                              "llama3.3:70b",      None, 44.0, 42.0, False),
    ("Qwen 2.5 72B",       "🐲", "72B", "big",  None,                                              "qwen2.5:72b",       None, 49.0, 47.0, False),
    ("DeepSeek R1",        "☁️", "R1",  "big",  None,                                              "deepseek-r1",       None,  5.5,  4.7, False),
]

GROUP_TITLES = {"core": "General Models", "code": "Coding & Vision",
                "big": "Large Models"}

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

# ------------------------------------------------------------------ tiers
# Three plain-English modes instead of a wall of model names. Each lists
# candidates strongest-first; whatever is downloaded and fits RAM is used,
# and Gemma blends the answers when a tier has more than one.
TIERS = {
    "Fast": {
        "icon": "\u26a1\ufe0f", "desc": "one quick model",
        "picks": ["Llama 3.2 3B", "Gemma 2 2B", "Llama 3.2 1B"],
        "count": 1,
    },
    "Thinking": {
        "icon": "\U0001f9e0", "desc": "reasons it through, blended",
        "picks": ["Phi-4 14B", "DeepSeek R1 7B", "Qwen 2.5 Coder 14B",
                  "Gemma 2 9B IT", "Mistral Nemo 12B"],
        "count": 3,
    },
    "Power": {
        "icon": "\u269b\ufe0f", "desc": "every model that fits, blended",
        "picks": [],          # purely memory-driven
        "count": 99,
    },
    "Pro": {
        "icon": "\u2728", "desc": "several models, blended",
        "picks": ["Mistral Nemo 12B", "Gemma 2 9B IT", "Qwen 2.5 7B",
                  "Llama 3.1 8B", "Llama 3.2 3B"],
        "count": 5,
    },
}

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

    def blendable(l):
        return (usable(l) and l not in BLEND_EXCLUDE
                and MODEL_MEM_BYTES.get(l, 0) >= BLEND_MIN_MEM)

    ready = [l for l in t["picks"] if usable(l)]
    if t["count"] > 1:
        # Only blend in models that leave room for the others. A 70B needs
        # most of the machine, so pairing it with four more would thrash
        # (and take many minutes) even if it fits on its own right now.
        total = psutil.virtual_memory().total if HAS_PSUTIL else 0
        budget = total * 0.45 if total else float("inf")
        ready += [l for l in MERGE_RANK
                  if blendable(l) and l not in ready
                  and MODEL_MEM_BYTES.get(l, 0) <= budget]
    if not ready:  # nothing at all from the tier — fall back to anything
        ready = [l for l in MERGE_RANK if usable(l)]
    return ready[:t["count"]]


# offered on first run: everything the three tiers can draw on
STARTER_LABELS = [l for l in MODEL_INFO
                  if SUPPORTED[l] and any(l in t["picks"]
                                          for t in TIERS.values())]

# who merges in combine mode — strongest first
MERGE_RANK = sorted((l for l in MODEL_ROUTES),
                    key=lambda l: -MODEL_INFO[l]["mem"])


def chip_name() -> str:
    """Short marketing name of the CPU: 'M4 PRO', 'CORE I7', etc."""
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
        if name == "Power":
            continue          # lives under "All models", not up top
        out.append(
            f'  <div class="tier" data-tier="{name}">'
            f'<span class="ico">{t["icon"]}</span>'
            f'<span class="tname">{name}</span>'
            f'<span class="infobtn" title="Which models does this use?">i</span>'
            f'</div>')
    return "\n".join(out)


def build_model_rows() -> str:
    """Sidebar rows, grouped. Every model is listed; ones this Mac can't
    run are marked unsupported and rendered greyed out."""
    out, seen = [], set()
    for label, info in MODEL_INFO.items():
        g = info["group"]
        if g not in seen:
            seen.add(g)
            cls = "mlx" if g == "core" else "ollama"
            out.append(f'  <div class="group-label {cls}">'
                       f'{GROUP_TITLES[g]}</div>')
        ok = SUPPORTED[label]
        out.append(
            f'  <div class="model{"" if ok else " unsupported"}"'
            f' data-model="{label}">'
            f'<span class="ico">{info["icon"]}</span>{label}'
            + ("" if ok else '<span class="memtag">APPLE SILICON ONLY</span>')
            + f'<span class="size">{info["size"]}</span></div>')
    return "\n".join(out)


def _mem_available():
    """Bytes of comfortably-usable RAM right now, or None if unknown."""
    if not HAS_PSUTIL:
        return None
    return psutil.virtual_memory().available


def model_fits_memory(label: str) -> bool:
    avail = _mem_available()
    need = MODEL_MEM_BYTES.get(label)
    if avail is None or need is None:
        return True  # unknown — don't cry wolf
    kind, target = MODEL_ROUTES.get(label, (None, None))
    if kind == "mlx" and _port_in_use(target):
        return True  # already resident and serving
    return need * 1.05 < avail

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
_MANAGED_BIN_DIR = os.path.expanduser(
    "~/Library/Application Support/MillenAI/bin")


def _ollama_bin():
    for c in (shutil.which("ollama"), "/usr/local/bin/ollama",
              os.path.join(_MANAGED_BIN_DIR, "ollama")):
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
    logdir = os.path.expanduser("~/Library/Logs/MillenAI")
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
    for label, proc in list(_mlx_procs.items()):
        if label == keep_label or proc.poll() is not None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
        _mlx_procs.pop(label, None)
        print(f"  stopped idle MLX engine for {label}")


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
    logdir = os.path.expanduser("~/Library/Logs/MillenAI")
    os.makedirs(logdir, exist_ok=True)
    log = open(os.path.join(logdir, "managed-ollama.log"), "ab")
    _managed_procs.append(subprocess.Popen(
        [b, "serve"], stdout=log, stderr=log,
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
            title = " ".join("".join(parts).split())
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
    """/Applications/MillenAI.app when running from a bundle, else None."""
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
    dmg = next((a for a in rel.get("assets", [])
                if a.get("name", "").endswith(".dmg")), None)
    if dmg:
        _update["url"] = dmg["browser_download_url"]
        _update["size"] = dmg.get("size", 0)
    _update["latest"] = tag
    published = _gh_time(rel.get("published_at", ""))
    # a release counts as newer if GitHub published it after this build was
    # made, or if its tag carries a higher build number
    newer = (published > _own_build_time() + 60
             or _build_from_tag(tag) > APP_BUILD)
    return {"configured": True,
            "available": bool(dmg) and newer
                         and _app_bundle_path() is not None,
            "latest": tag, "current": APP_VERSION,
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
MEMORY_FILE = os.path.expanduser(
    "~/Library/Application Support/MillenAI/memory.json")
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


def _load_memory() -> list:
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_memory(items: list):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(items[-60:], f, indent=1)


def memory_text() -> str:
    return "\n".join("- " + i["fact"] for i in _load_memory()[-40:])


def _extract_memory(label: str, user_msg: str):
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
        if not facts:
            return
        with _memory_lock:
            items = _load_memory()
            known = {i["fact"].lower() for i in items}
            for f in facts:
                if f.lower() not in known:
                    items.append({"fact": f, "ts": time.time()})
            _save_memory(items)
    except Exception:
        pass  # memory is best-effort — never break chat over it


# ------------------------------------------------------------- voice
# STT: whisper via MLX (Apple silicon only). TTS: macOS built-in `say`.
WHISPER_REPO = "mlx-community/whisper-large-v3-turbo"
_whisper_lock = threading.Lock()
_say_proc = None


def _voice_supported() -> bool:
    if not IS_ARM:
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _voice_ready() -> bool:
    d = _hf_model_dir(WHISPER_REPO)
    if glob.glob(os.path.join(d, "blobs", "*.incomplete")):
        return False
    snaps = glob.glob(os.path.join(d, "snapshots", "*", "config.json"))
    if not snaps:
        return False
    return bool(glob.glob(os.path.join(os.path.dirname(snaps[0]), "weights.*")))


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
        out = mlx_whisper.transcribe(audio, path_or_hf_repo=WHISPER_REPO)
    return out["text"].strip()


def _speak(text: str):
    """Read a reply aloud with the system voice; new speech cuts off old."""
    global _say_proc
    _stop_speaking()
    # strip the markdown the models produce so `say` doesn't read symbols
    plain = re.sub(r"```[\s\S]*?```", " code block omitted. ", text)
    plain = re.sub(r"[*_#`>|]", "", plain)
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)
    if plain.strip():
        _say_proc = subprocess.Popen(["say", plain.strip()[:4000]])


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
    tmp = os.path.join(_MANAGED_BIN_DIR, "ollama.tgz.part")
    req = urllib.request.Request(OLLAMA_TGZ_URL,
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


def setup_status() -> dict:
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

    for label in [l for l in MODEL_INFO if SUPPORTED.get(l)]:
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
                       "star": label in STARTER_LABELS,
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
        "ready_n": ready_n,
        "mlx_ok": _has_mlx() if IS_ARM else True,
        "ollama": _ollama_bin() is not None,
        "arch": "arm64" if IS_ARM else "x86_64",
        "disk_free_gb": round(
            shutil.disk_usage(os.path.expanduser("~")).free / 1e9),
        "models": models,
    }


def stop_managed_engines():
    for p in _managed_procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in _managed_procs:
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


def gpu_utilization():
    """Apple GPU 'Device Utilization %' via ioreg (no sudo). None if unknown."""
    now = time.time()
    if now - _gpu_cache["ts"] < 0.7:
        return _gpu_cache["pct"]
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


def stream_openai_compat(port: int, model_label: str, messages: list, emit) -> None:
    """Stream from an OpenAI-compatible server (MLX / llama.cpp / LM Studio).

    Robust to servers that ignore `stream: true` and reply with one JSON
    blob — if no SSE tokens arrive, the whole body is parsed as a plain
    completion instead.
    """
    payload = json.dumps({
        # mlx_lm validates this as a HF repo id — the UI label 404s
        "model": MLX_REPOS.get(model_label, "default_model"),
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.75,
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    emitted = False
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
            chunk = (choice.get("delta", {}).get("content", "")
                     or choice.get("message", {}).get("content", ""))
            if chunk:
                emitted = True
                emit(chunk)

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


def run_model(label: str, messages: list, emit) -> None:
    """Stream one model's answer, handling engine startup and templates."""
    kind, target = MODEL_ROUTES.get(label, ("ollama", "llama3.3:70b"))
    if kind == "mlx":
        with _engine_lock:
            ensure_mlx_engine(label)
    msgs, attempts, folded = messages, 0, False
    while True:
        try:
            if kind == "ollama":
                stream_ollama(target, msgs, emit)
            else:
                stream_openai_compat(target, label, msgs, emit)
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
            # engine may be mid-startup — one short grace retry
            attempts += 1
            if attempts > 1:
                raise
            time.sleep(1.2)


SYNTH_INSTRUCTION = (
    "Below are several draft answers to the same question. Write ONE "
    "thorough, well-organised final answer that keeps every correct and "
    "useful detail and drops repetition. If the drafts disagree on a fact, "
    "state the correct information plainly. STRICT RULE: your reply must "
    "read as a direct answer to the question and nothing else — never use "
    "the words 'draft', 'version', 'model', or 'answer 1/2/3', never "
    "compare or evaluate the drafts, never explain what you merged."
)


def _looks_degenerate(text: str) -> bool:
    """Runaway repetition detector ('to make up to make up to…')."""
    words = text.split()
    if len(words) < 120:
        return False
    return len(set(words)) / len(words) < 0.15


def run_council(labels: list, messages: list, emit, status) -> None:
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
    labels = (usable or labels[:1])[:8]

    drafts = []
    for i, label in enumerate(labels, 1):
        # free RAM drops as each engine loads — re-check before committing
        if i > 1 and not model_fits_memory(label):
            drafts.append((label, "(no answer — low memory)"))
            continue
        status(f"asking {label} · {i} of {len(labels)}")
        parts = []
        try:
            run_model(label, messages, parts.append)
        except Exception as exc:
            drafts.append((label, f"(no answer — {type(exc).__name__})"))
            continue
        text = "".join(parts).strip()
        if _looks_degenerate(text):
            # a runaway repetition loop would poison the merge prompt
            drafts.append((label, "(no answer — degenerate output)"))
            continue
        if text:
            drafts.append((label, text))

    good = [d for d in drafts if not d[1].startswith("(no answer")]
    if not good:
        raise RuntimeError("none of the selected models answered")
    if len(good) == 1:
        emit(good[0][1])  # only one survived — nothing to merge
        return

    # Gemma writes the final answer whenever it's on this machine and fits;
    # otherwise fall back to the strongest answering model that fits
    answered = [l for l, _t in good]
    merger = next((l for l in MERGE_RANK
                   if l in answered and model_fits_memory(l)), answered[0])
    pref = "Gemma 2 9B IT"
    if model_cached(pref) and model_fits_memory(pref):
        merger = pref

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
    synth = [
        messages[0],  # keep the dated system prompt
        {"role": "user",
         "content": f"{SYNTH_INSTRUCTION}\n\nQUESTION: {question}\n\n{body}"},
    ]
    run_model(merger, synth, emit)


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


class StudioHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # lets us stream then close, no chunking

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        if self.path == "/":
            html = (HTML_CONTENT
                    .replace("__MODEL_ROWS__", build_model_rows())
                    .replace("__APP_VER_TAG__",
                             APP_VERSION.replace(" ", "&nbsp;"))
                    .replace("__APP_BETA__",
                             'VERSION <b class="vnum">%s</b>' % APP_VERSION)
                    .replace("__TIER_ROWS__", build_tier_rows())
                    .replace("__CHIP__", chip_name())
                    .replace("__APP_VER__", APP_VERSION))
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
            self._send_json({name: {"desc": t["desc"],
                                    "models": resolve_tier(name)}
                             for name, t in TIERS.items()})
        elif self.path == "/api/memory":
            self._send_json({"facts": _load_memory()})
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
        gpu = gpu_utilization()
        if HAS_PSUTIL:
            vm = psutil.virtual_memory()
            stats = {
                "real": True,
                "mem_used_gb": round(vm.used / 1e9, 1),
                "mem_total_gb": round(vm.total / 1e9, 1),
                "mem_pct": vm.percent,
                "cpu_pct": psutil.cpu_percent(interval=None),
                "gpu_pct": gpu,  # None when ioreg has no accelerator stats
            }
        else:
            stats = {"real": False, "gpu_pct": gpu}
        body = json.dumps(stats).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        if self.path == "/api/setup/install":
            self._send_json({"started": start_model_downloads()})
            return
        if self.path == "/api/update/install":
            if _update["state"] in ("idle", "error"):
                threading.Thread(target=_do_update, daemon=True).start()
            self._send_json({"ok": True})
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
                ["open", os.path.expanduser("~/Library/Logs/MillenAI")])
            self._send_json({"ok": True})
            return
        if self.path == "/api/memory/clear":
            with _memory_lock:
                _save_memory([])
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
        model_name = req_json.get("model", "")
        auto_web = req_json.get("auto_web", True)
        # a tier resolves to its own line-up; otherwise honour explicit picks
        tier = req_json.get("tier") or ""
        if tier in TIERS:
            council = resolve_tier(tier)
        else:
            council = [m for m in req_json.get("models", [])
                       if m in MODEL_ROUTES]
        if not council:
            council = [model_name]
        prompt = messages[-1]["content"] if messages else ""

        # "/search …" forces a lookup; otherwise auto-search decides.
        query, forced = None, prompt.lower().startswith("/search")
        if forced:
            query = prompt[7:].strip()
        elif auto_web and needs_search(prompt):
            query = prompt.strip()

        if query:
            snippets = run_search(query)
            messages[-1] = {
                "role": "user",
                "content": (
                    "You have internet access. Using these live search "
                    f"snippets, answer the prompt.\n"
                    f"SNIPPETS FOR '{query}':\n{snippets}\n\nPROMPT: {query}"
                ),
            }

        # local models have no clock — without this "today" is meaningless
        today = time.strftime("%A, %B %-d, %Y")
        dated_system = dict(SYSTEM_PROMPT)
        dated_system["content"] += f"\n\nToday's date is {today}."
        if tier == "Thinking" and messages:
            messages[-1] = dict(messages[-1])
            messages[-1]["content"] += "\n\n" + THINK_HINT
        mem = memory_text()
        if mem:
            dated_system["content"] += (
                "\n\nFrom earlier conversations you remember these facts "
                "about the user:\n" + mem +
                "\nUse them naturally when relevant — don't recite them.")
        full_messages = [dated_system] + messages

        route, route_label = None, None
        for label, target in MODEL_ROUTES.items():
            if label in model_name:
                route, route_label = target, label
                break
        if route is None:
            route = ("ollama", "llama3.3:70b")

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

        def emit(chunk: str):
            # some engines leak their end-of-turn token as literal text
            for s in ("<end_of_turn>", "<|eot_id|>", "<|im_end|>", "</s>"):
                if s in chunk:
                    chunk = chunk.replace(s, "")
            if not chunk:
                return
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        def status(text: str):
            # sentinel-wrapped so the UI can show progress without it
            # ending up inside the answer text
            self.wfile.write(f"{NUL}STATUS:{text}{NUL}".encode("utf-8"))
            self.wfile.flush()

        kind, target = route
        try:
            if len(council) > 1:
                run_council(council, full_messages, emit, status)
            else:
                run_model(route_label or model_name, full_messages, emit)
        except (BrokenPipeError, ConnectionResetError):
            pass  # user hit Stop — browser closed the connection
        except Exception as exc:
            try:
                emit("\n" + offline_hint(kind, exc))
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            plain = prompt[8:] if prompt.lower().startswith("/search") \
                else prompt
            if plain and len(plain) > 12:
                threading.Thread(
                    target=_extract_memory,
                    args=(route_label or council[0], plain),
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
::selection{background:var(--accent-dim);color:var(--accent-hot)}
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
::-webkit-scrollbar-track{background:transparent}

/* ---------------------------------------------------------------- sidebar */
#sidebar{
  position:relative;
  width:284px;min-width:284px;height:100%;
  background:var(--panel);border-right:1px solid var(--line-soft);
  display:flex;flex-direction:column;padding:20px 16px 14px;gap:4px;
}
#sb-resize{
  position:absolute;top:0;right:-3px;width:7px;height:100%;
  cursor:col-resize;z-index:20;
}
#sb-resize:hover,body.resizing #sb-resize{background:rgba(255,255,255,.18)}
body.resizing{cursor:col-resize;user-select:none}
#brand-wrap{padding:0 6px 12px}
#brand-row{display:flex;align-items:center;gap:8px}
#update-flag{
  font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;
  color:#e26d5a;cursor:pointer;margin-top:-6px;
}
#update-flag:hover{text-decoration:underline}
#update-flag[hidden]{display:none}
#brand{display:flex;cursor:pointer;align-items:baseline;gap:8px}
#brand .name{font-weight:700;font-size:22px;letter-spacing:.02em}
#brand .tag{font-family:var(--mono);font-size:10px;color:var(--accent);
  border:1px solid var(--accent-dim);background:var(--accent-dim);
  padding:2px 6px;border-radius:4px;letter-spacing:.08em}

#newchat{
  margin-left:auto;width:28px;height:28px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  background:none;border:1px solid var(--line);border-radius:8px;
  color:var(--accent-hot);cursor:pointer;padding:0;
  transition:border-color .15s,background .15s,color .15s;
}
#newchat svg{width:15px;height:15px}
#newchat:hover{border-color:var(--accent-hot);background:var(--accent-dim);color:var(--text)}

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
#tier-rows{margin:8px 0 4px}
.tier{
  display:flex;align-items:center;gap:9px;padding:9px 10px;margin-bottom:4px;
  border-radius:9px;cursor:pointer;color:var(--dim);font-size:13.5px;
  border:1px solid transparent;transition:all .13s;user-select:none;
}
.tier:hover{color:var(--text);background:var(--panel2)}
.tier.active{
  color:var(--text);background:var(--accent-dim);
  border-color:rgba(255,255,255,.26);
}
.tier .tname{font-weight:600}

#chat-list{margin-bottom:2px;overflow-y:auto;max-height:34vh}
#chat-list:empty::after{
  content:"no chats yet";display:block;color:var(--faint);
  font-size:11px;padding:2px 10px 6px;
}
.chat-item{
  display:flex;align-items:center;gap:6px;padding:6px 10px;
  border-radius:8px;cursor:pointer;color:var(--dim);font-size:12.5px;
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
  margin-top:12px;background:var(--panel2);border:1px solid var(--line-soft);
  border-radius:var(--radius);padding:12px 12px 11px;
  font-family:var(--mono);
}
#telemetry .t-head{
  font-size:9.5px;letter-spacing:.16em;color:var(--faint);
  display:flex;justify-content:space-between;margin-bottom:10px;
}
#telemetry .t-head .live{color:var(--accent)}
.meter-row{margin-bottom:9px}
.meter-row:last-child{margin-bottom:0}
.meter-label{
  display:flex;justify-content:space-between;font-size:10px;
  color:var(--dim);margin-bottom:4px;
}
.meter-label b{color:var(--text);font-weight:500}
.meter{display:flex;gap:2px;height:8px}
.meter i{
  flex:1;background:var(--line);border-radius:1px;transition:background .3s;
}
.meter i.lit{background:var(--accent)}
.meter i.hot{background:var(--accent-hot)}
#toks-big{
  font-size:22px;color:var(--accent);font-weight:600;line-height:1;
  margin:2px 0 1px;font-variant-numeric:tabular-nums;
}
#toks-big span{font-size:10px;color:var(--faint);font-weight:400;margin-left:4px}
body:not(.perf) #telemetry .live{animation:blink 1.6s ease infinite}
@keyframes blink{50%{opacity:.25}}
/* performance mode: telemetry goes dark AND stops polling (the GPU probe
   and meter repaints are the expensive part) */
body.perf #telemetry{opacity:.13;filter:grayscale(1);pointer-events:none}

/* ------------------------------------------------------------------ main */
#main{flex:1;height:100%;display:flex;flex-direction:column;position:relative}
#stars{position:absolute;top:0;left:0;width:100%;height:100%;z-index:0;pointer-events:none}
body.perf #stars{display:none}
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
/* the whole wordmark rides the rainbow, not just the version tag */
#hero h1{
  font-size:61px;font-weight:700;letter-spacing:-.01em;
  /* tile starts and ends on the same color; sliding one full tile
     (background-size 200% -> position 200%) loops seamlessly */
  background:linear-gradient(90deg,#ff8f8f,#ffc46e,#f5e663,#7ef0a6,
             #6ec7ff,#8f9dff,#c98fff,#ff8fd8,#ff8f8f);
  background-size:200% 100%;
  -webkit-background-clip:text;background-clip:text;
  color:transparent;-webkit-text-fill-color:transparent;
  animation:rainbow 16s linear infinite,hueshift 45s linear infinite;
}
@keyframes hueshift{to{filter:hue-rotate(360deg)}}
@keyframes rainbow{from{background-position:0% 50%}to{background-position:200% 50%}}
body.perf #hero h1{animation:none}
#hero p{color:var(--dim);font-size:15px}
/* the wordmark centres on its own; LIVE is pulled out of the flow so it
   sits further right without dragging the title off-centre */
#hero .h1row{display:flex;align-items:center;justify-content:center;position:relative}
#hero .live-tag{position:absolute;left:100%;margin-left:30px;white-space:nowrap}
/* subdued deep-blue accents — deliberately quiet next to the wordmark */
.live-tag,#hero .beta-tag{
  font-family:var(--helv);font-weight:600;color:#8e8e8e;
  letter-spacing:.32em;text-transform:uppercase;
}
.live-tag{font-size:11px;padding-left:.32em}
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
.msg .body{line-height:1.65;font-size:15px}
.msg.user .body{
  background:var(--panel2);border:1px solid var(--line-soft);
  border-radius:var(--radius);padding:12px 16px;white-space:pre-wrap;
}
.msg.ai .body{padding:0 2px}
.msg .meta{
  font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-top:8px;
}
.msg .meta b{color:var(--accent);font-weight:500}

.body p{margin:0 0 10px}
.body p:last-child{margin-bottom:0}
.body h1,.body h2,.body h3{margin:14px 0 8px;font-size:16.5px}
.body ul,.body ol{margin:0 0 10px 22px}
.body li{margin-bottom:3px}
.body code{
  font-family:var(--mono);font-size:12.5px;background:var(--panel2);
  border:1px solid var(--line-soft);padding:1.5px 5px;border-radius:4px;
  color:var(--accent-hot);
}
.body pre{
  background:var(--panel);border:1px solid var(--line-soft);
  border-radius:var(--radius);padding:13px 15px;overflow-x:auto;margin:0 0 10px;
}
.body pre code{background:none;border:none;padding:0;color:var(--text);font-size:12.5px}
.body strong{color:#fff}
.body details{
  border:1px solid var(--line-soft);border-radius:8px;
  margin:0 0 10px;background:var(--panel);
}
.body details summary{
  cursor:pointer;padding:8px 12px;font-family:var(--mono);
  font-size:11px;color:var(--faint);letter-spacing:.08em;user-select:none;
}
.body details[open] summary{border-bottom:1px solid var(--line-soft);color:var(--dim)}
.body details .think-body{padding:10px 14px;color:var(--dim);font-size:13.5px;line-height:1.6}

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
#composer{
  max-width:780px;margin:0 auto;pointer-events:auto;
  background:var(--panel2);border:1px solid var(--line);
  border-radius:14px;display:flex;align-items:flex-end;gap:6px;
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
.cbtn{
  width:36px;height:36px;border-radius:9px;border:none;cursor:pointer;
  display:flex;align-items:center;justify-content:center;font-size:15px;
  background:none;color:var(--dim);transition:all .13s;flex-shrink:0;
}
.cbtn:hover{color:var(--text);background:var(--line-soft)}
#send{background:var(--accent);color:#1a1a1a;font-weight:700;font-size:16px}
#send:hover{background:var(--accent-hot);color:#000}
#send:disabled{background:var(--line);color:var(--faint);cursor:default}
#send.stop{background:var(--red);color:#fff;font-size:11px}
#mic.rec{color:var(--red)}
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
#update-veil,#about-veil{
  position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.66);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
  display:flex;align-items:center;justify-content:center;
}
#update-veil[hidden],#about-veil[hidden]{display:none}
#about-card{
  width:330px;background:var(--panel2);border:1px solid var(--line);
  border-radius:16px;padding:30px 26px 22px;text-align:center;
  box-shadow:0 24px 80px rgba(0,0,0,.6);
}
#about-icon{width:96px;height:96px;margin-bottom:16px}
#about-name{font-family:var(--helv);font-size:24px;font-weight:600;color:var(--text)}
#about-name em{font-style:italic;font-weight:400;opacity:.85}
#about-ver,#up-ver{font-family:var(--helv);font-size:14px;color:var(--dim);margin-top:6px}
#up-detail{font-size:11.5px;color:var(--faint);margin:10px 0 4px;line-height:1.5}
#about-sub{font-size:11.5px;color:var(--faint);margin-top:10px;line-height:1.5}
#about-facts{
  font-family:var(--mono);font-size:10.5px;color:var(--faint);
  margin-top:8px;line-height:1.6;
}
.about-btn{
  display:block;width:100%;margin-top:9px;padding:10px 14px;
  font:500 13.5px var(--helv);cursor:pointer;color:var(--text);
  background:none;border:1px solid var(--line);border-radius:10px;
  transition:background .13s,border-color .13s;
}
.about-btn:hover{background:var(--panel);border-color:var(--dim)}
.about-btn.primary{background:var(--accent);color:#1a1a1a;border:none;margin-top:14px}
.about-btn.primary:hover{background:var(--accent-hot);color:#000}

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
#celebrate .sweep{
  position:absolute;top:50%;left:50%;
  width:88vw;height:280vh;margin:-140vh 0 0 -44vw;
  background:linear-gradient(90deg,transparent,#ff8f8f,#ffc46e,#f5e663,
             #7ef0a6,#6ec7ff,#8f9dff,#c98fff,transparent);
  filter:blur(16px);opacity:.8;mix-blend-mode:screen;
  animation:sweepDiag 1.6s cubic-bezier(.35,0,.25,1) forwards;
}
@keyframes sweepDiag{
  from{transform:rotate(24deg) translate(-135vw,-32vh)}
  to  {transform:rotate(24deg) translate(135vw,32vh)}
}
/* soft blurred glow that collapses into the wordmark — no hard edges, so
   nothing ever reads as a box sitting on top of the text */
#celebrate .converge{
  position:absolute;border-radius:50%;
  background:linear-gradient(115deg,#ff8f8f,#ffc46e,#f5e663,#7ef0a6,
             #6ec7ff,#8f9dff,#c98fff);
  opacity:.6;mix-blend-mode:screen;filter:blur(30px);
  transition:left 1s cubic-bezier(.45,0,.2,1),
             top 1s cubic-bezier(.45,0,.2,1),
             width 1s cubic-bezier(.45,0,.2,1),
             height 1s cubic-bezier(.45,0,.2,1),
             opacity 1s ease-in;
}
#hero h1.absorb{animation:absorb .9s ease-out}
@keyframes absorb{
  0%{filter:brightness(1)}
  45%{filter:brightness(2.1) drop-shadow(0 0 22px rgba(255,255,255,.5))}
  100%{filter:brightness(1)}
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
.setup-row .st.ok{color:#5fbf77}
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
#setup-note{color:var(--faint);font-size:11.5px;margin-top:10px;font-family:var(--mono)}
#setup-foot{display:flex;gap:10px;justify-content:flex-end;margin-top:14px}
#setup-foot button{
  font:600 13px var(--sans);padding:9px 15px;border-radius:9px;cursor:pointer;
  border:1px solid var(--line);background:none;color:var(--dim);
  transition:all .13s;
}
#setup-foot button:hover{color:var(--text);border-color:var(--dim)}
#setup-go{background:var(--accent);color:#1a1a1a;border:none}
#setup-go:hover{background:var(--accent-hot);color:#000}
#setup-go:disabled{opacity:.55;cursor:default}

@media(max-width:900px){#hero .live-tag{display:none}}
@media(max-width:760px){
  #sidebar{display:none}
  #chat-inner{padding:24px 14px 150px}
}
</style>
</head>
<body>

<aside id="sidebar">
  <div id="sb-resize" title="Drag to resize"></div>
  <div id="brand-wrap">
    <div id="brand-row">
    <div id="brand" title="About MillenAI">
      <span class="name">MillenAI</span>
      <span class="tag">__APP_VER_TAG__</span>
    </div>
    <button id="newchat" title="New chat">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M13 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/>
        <path d="M18.4 2.6a1.7 1.7 0 0 1 2.4 2.4L12.8 13l-3.2.8.8-3.2z"/>
      </svg>
    </button>
    </div>
    <div id="update-flag" hidden>UPDATE AVAILABLE</div>
  </div>


  <div id="tier-rows">__TIER_ROWS__</div>

  <div id="model-list">
  <div class="group-label chats">Chats</div>
  <div id="chat-list"></div>

  <div class="group-label adv" id="adv-toggle"><span id="adv-caret">▸</span> All models</div>
  <div id="adv-wrap" hidden>
  <div class="tier" data-tier="Power"><span class="ico">⚛️</span>
    <span class="tname">Power Mode</span>
    <span class="infobtn" title="Which models does this use?">i</span></div>
__MODEL_ROWS__
  <div class="model" id="open-setup" title="Download more models">
    <span class="ico">⬇</span>Add models…</div>
  </div>
  </div>

  <div id="settings">
    <div class="toggle-row" id="web-toggle" title="Looks up live snippets when a question needs current info">
      <div class="switch"></div>
      Live web search
    </div>
    <div class="toggle-row" id="perf-toggle" style="margin-top:9px">
      <div class="switch"></div>
      Performance mode
    </div>
  </div>

  <div id="telemetry">
    <div class="t-head"><span>__CHIP__</span><span class="live">●&nbsp;LIVE</span></div>
    <div class="meter-row">
      <div class="meter-label"><span>THROUGHPUT</span><b id="toks-label">idle</b></div>
      <div id="toks-big">0<span>tok/s</span></div>
    </div>
    <div class="meter-row">
      <div class="meter-label"><span>UNIFIED MEMORY</span><b id="mem-label">—</b></div>
      <div class="meter" id="mem-meter"></div>
    </div>
    <div class="meter-row">
      <div class="meter-label"><span>SYSTEM COMPUTE</span><b id="cpu-label">—</b></div>
      <div class="meter" id="cpu-meter"></div>
    </div>
    <div class="meter-row">
      <div class="meter-label"><span>GPU COMPUTE</span><b id="gpu-label">—</b></div>
      <div class="meter" id="gpu-meter"></div>
    </div>
  </div>
</aside>

<main id="main">
  <canvas id="stars"></canvas>
  <div id="chat-scroll"><div id="chat-inner">
    <div id="hero">
      <div class="h1row"><h1>MillenAI</h1><span class="live-tag" hidden>LIVE</span></div>
      <div class="beta-tag">__APP_BETA__</div>
      <p class="greet">What's going on today?</p>
    </div>
  </div></div>

  <div id="composer-wrap">
    <div id="model-chip">engine <b id="chip-model">Llama 3.2 3B</b></div>
    <div id="composer">
      <button class="cbtn" id="mic" title="Voice input">🎙️</button>
      <textarea id="input" rows="1" placeholder="Message MillenAI…"></textarea>
      <button class="cbtn" id="voicebtn" title="Voice chat — replies are read aloud">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 10v4"/><path d="M7 6v12"/><path d="M11 3v18"/>
          <path d="M15 7v10"/><path d="M19 10v4"/>
        </svg>
      </button>
      <button class="cbtn" id="send" title="Send">↑</button>
    </div>
  </div>
</main>

<div id="tierpop" hidden></div>
<div id="celebrate" hidden></div>

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
    <div id="about-name">MillenAI <em>for</em> Mac</div>
    <div id="about-ver">Version __APP_VER__</div>
    <div id="about-sub">Everything runs on this Mac. No cloud, no accounts.</div>
    <div id="about-facts"></div>
    <button class="about-btn" id="about-logs">Open logs folder</button>
    <button class="about-btn" id="about-forget">Forget what you know about me</button>
    <button class="about-btn primary" id="about-close">Close</button>
  </div>
</div>

<div id="setup-veil" hidden>
  <div id="setup-card">
    <h2>Welcome to MillenAI</h2>
    <p class="sub">Everything runs 100% on this Mac — no cloud, no accounts.
      One tap gets every model the three modes use. You can start chatting
      as soon as the first one lands — the rest keep downloading.</p>
    <div id="setup-list"></div>
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
let autoWeb=localStorage.getItem("millen.web")!=="0";  // on unless turned off
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
  $$(".model").forEach(el=>{
    if(!el.dataset.model)return;  // the Power Mode row manages itself
    const i=council.indexOf(el.dataset.model);
    el.classList.toggle("active",i===0);
    el.classList.toggle("picked",i>0);
    const old=el.querySelector(".rank"); if(old)old.remove();
    if(combine&&i>=0&&council.length>1){
      const r=document.createElement("span");
      r.className="rank"; r.textContent=i===0?"merge":String(i+1);
      el.appendChild(r);
    }
  });
  $("#chip-model").textContent=tier;
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
  if(combine){
    councilManual=true;                            // user is curating now
    const i=council.indexOf(name);
    if(i<0)council.push(name);
    else if(council.length>1)council.splice(i,1);   // never empty
  }else{
    council=[name];
  }
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

/* --------------------------------------------------- live web search */
function paintLive(){
  const t=$(".live-tag"); if(t)t.hidden=!autoWeb;
}
function setWeb(on){
  autoWeb=on; $("#web-toggle").classList.toggle("on",on);
  localStorage.setItem("millen.web",on?"1":"0");
  paintLive();
}
$("#web-toggle").addEventListener("click",()=>setWeb(!autoWeb));
setWeb(autoWeb);

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
let tier=localStorage.getItem("millen.tier")||"Pro";
function setTier(name){
  tier=name;localStorage.setItem("millen.tier",name);
  $$(".tier").forEach(el=>el.classList.toggle("active",el.dataset.tier===name));
  councilManual=false;
  paintModels();
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
      : '<div class="mline">nothing downloaded yet</div>');
  const r=el.getBoundingClientRect();
  tierPop.hidden=false;
  tierPop.style.left=Math.round(r.right+10)+"px";
  tierPop.style.top=Math.round(r.top-4)+"px";
}
function hideTierPop(){tierPop.hidden=true;}
$$(".tier").forEach(el=>{
  el.addEventListener("click",()=>setTier(el.dataset.tier));
  const ib=el.querySelector(".infobtn");
  if(ib)ib.addEventListener("click",ev=>{
    ev.stopPropagation();
    if(!tierPop.hidden&&tierPop.dataset.for===el.dataset.tier){hideTierPop();return;}
    tierPop.dataset.for=el.dataset.tier;showTierPop(el,el.dataset.tier);
  });
});
document.addEventListener("click",e=>{
  if(!e.target.classList.contains("infobtn"))hideTierPop();
});
setTier(tier);

// advanced list stays collapsed until asked for
$("#adv-toggle").addEventListener("click",()=>{
  const w=$("#adv-wrap");w.hidden=!w.hidden;
  $("#adv-caret").textContent=w.hidden?"▸":"▾";
});

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
    if(/^<(pre|ul|ol|h\d|details)/.test(p.trim()))return p;
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
function addMsg(role,text){
  const hero=$("#hero"); if(hero)hero.remove();
  const div=document.createElement("div");
  div.className="msg "+(role==="user"?"user":"ai");
  const who=role==="user"?"you":(lastModels||tier);
  div.innerHTML='<div class="who">'+who+'</div><div class="body"></div>';
  const body=div.querySelector(".body");
  if(role==="user")body.textContent=text; else body.innerHTML=renderMD(text);
  inner.appendChild(div);
  scroller.scrollTop=scroller.scrollHeight;
  return div;
}

/* -------------------------------------------------------- tok/s meter */
const toksBig=$("#toks-big"),toksLabel=$("#toks-label");
function setToks(rate,state){
  toksBig.innerHTML=(rate>0?rate.toFixed(1):"0")+"<span>tok/s</span>";
  toksLabel.textContent=state;
}

/* --------------------------------------------------------------- send */
const input=$("#input"),sendBtn=$("#send");
input.addEventListener("input",()=>{input.style.height="auto";input.style.height=Math.min(input.scrollHeight,180)+"px";});
input.addEventListener("keydown",e=>{
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();}
});
sendBtn.addEventListener("click",()=>{ generating?abortCtl.abort():send(); });

async function send(){
  const text=input.value.trim();
  if(!text||generating)return;

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
  messages.push({role:"user",content:text});
  addMsg("user",text);

  generating=true; document.body.classList.add("gen");
  sendBtn.textContent="■"; sendBtn.classList.add("stop"); sendBtn.title="Stop";
  const aiDiv=addMsg("assistant",""); const body=aiDiv.querySelector(".body");
  body.innerHTML='<span class="caret"></span>';

  abortCtl=new AbortController();
  let full="",t0=performance.now(),tokEst=0,lastRate=0,wasAborted=false,searched=false,status=null;
  lastModels="";

  try{
    const resp=await fetch("/api/chat",{
      method:"POST",headers:{"Content-Type":"application/json"},
      signal:abortCtl.signal,
      body:JSON.stringify({model,models:council,tier,messages,auto_web:autoWeb}),
    });
    searched=resp.headers.get("X-Web-Search")==="1";
    lastModels=resp.headers.get("X-Models")||"";
    if(lastModels){const w=aiDiv.querySelector(".who");if(w)w.textContent=lastModels;}
    if(searched)body.innerHTML='<span class="websrc">🌐 searched the web</span><span class="caret"></span>';
    const reader=resp.body.getReader(),dec=new TextDecoder();
    let raw="";
    while(true){
      const {done,value}=await reader.read();
      if(done)break;
      raw+=dec.decode(value,{stream:true});
      // pull progress markers out so they never land in the answer
      full=raw.replace(/\u0000STATUS:(.*?)\u0000/g,(_,t)=>{status=t;return "";})
              .replace(/\u0000STATUS:[^\u0000]*$/,"");   // partial marker
      tokEst=full.length/4;
      const secs=(performance.now()-t0)/1000;
      lastRate=secs>0.3?tokEst/secs:0;
      setToks(lastRate,"streaming");
      body.innerHTML=(status&&!full?'<span class="statusline">◇ '+esc(status)+'…</span>':"")
        +(searched?'<span class="websrc">🌐 searched the web</span>':"")
        +renderMD(full)+'<span class="caret"></span>';
      scroller.scrollTop=scroller.scrollHeight;
    }
  }catch(err){
    if(err.name==="AbortError")wasAborted=true;
    else full+="\n\n⚠️ "+err.message;
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
    messages.push({role:"assistant",content:full});
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
  scroller.scrollTop=scroller.scrollHeight;
  input.focus();
}

/* ------------------------------------------------------------- greeting */
const GREETINGS=[
  "What's going on today?","What's on your mind?","Where should we start?",
  "What are you working on?","What can I help with?","What's up today?",
  "Ask me anything.","What are you curious about?",
  "What shall we get into?","How can I help right now?",
];
function greeting(){return GREETINGS[Math.floor(Math.random()*GREETINGS.length)];}
(function(){const g=$(".greet");if(g)g.textContent=greeting();})();

/* ------------------------------------------------- chats: list + store */
let chats=[];
try{chats=JSON.parse(localStorage.getItem("millen.chats"))||[];}catch(e){}
let curChat=null;   // every launch starts fresh; history stays in the list

function resetHero(){
  inner.innerHTML='<div id="hero"><div class="h1row"><h1>MillenAI</h1><span class="live-tag" hidden>LIVE</span></div><div class="beta-tag">__APP_BETA__</div><p class="greet">'+esc(greeting())+'</p></div>';
  paintLive();
}
function saveChats(){
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
  messages.forEach(m=>addMsg(m.role==="user"?"user":"assistant",m.content));
  renderChats();
}
renderChats();

/* ----------------------------------------------------------- new chat */
$("#newchat").addEventListener("click",()=>{
  if(generating&&abortCtl)abortCtl.abort();
  persistCurrent();
  curChat=null;messages=[];
  resetHero();renderChats();
  input.focus();
});

/* ---------------------------------------------------------- telemetry */
function buildMeter(el,n){for(let i=0;i<n;i++)el.appendChild(document.createElement("i"));}
buildMeter($("#mem-meter"),18);buildMeter($("#cpu-meter"),18);buildMeter($("#gpu-meter"),18);
function paintMeter(el,pct){
  const segs=el.children,lit=Math.round(pct/100*segs.length);
  for(let i=0;i<segs.length;i++){
    segs[i].className=i<lit?(i>=segs.length*0.8?"hot":"lit"):"";
  }
}
let simMem=58,simCpu=22,simGpu=12;
function paintGpu(pct){
  if(pct==null){$("#gpu-label").textContent="—";paintMeter($("#gpu-meter"),0);return;}
  $("#gpu-label").textContent=pct.toFixed(0)+"%";
  paintMeter($("#gpu-meter"),pct);
}
async function pollStats(){
  let gpu;
  try{
    const r=await fetch("/api/stats"),st=await r.json();
    gpu=st.gpu_pct;
    if(st.real){
      $("#mem-label").textContent=st.mem_used_gb+" / "+st.mem_total_gb+" GB";
      paintMeter($("#mem-meter"),st.mem_pct);
      $("#cpu-label").textContent=st.cpu_pct.toFixed(0)+"%";
      paintMeter($("#cpu-meter"),st.cpu_pct);
      paintGpu(gpu);
      return;
    }
  }catch(e){}
  // ambient fallback — clearly approximate
  simMem=Math.max(35,Math.min(88,simMem+(Math.random()-0.5)*4+(generating?1.5:-0.8)));
  simCpu=Math.max(6,Math.min(96,simCpu+(Math.random()-0.5)*10+(generating?14:-9)));
  $("#mem-label").textContent="~"+(simMem*0.48).toFixed(0)+" / 48 GB";
  paintMeter($("#mem-meter"),simMem);
  $("#cpu-label").textContent="~"+simCpu.toFixed(0)+"%";
  paintMeter($("#cpu-meter"),simCpu);
  if(gpu!=null){paintGpu(gpu);return;}  // ioreg is real even without psutil
  simGpu=Math.max(2,Math.min(97,simGpu+(Math.random()-0.5)*8+(generating?22:-16)));
  $("#gpu-label").textContent="~"+simGpu.toFixed(0)+"%";
  paintMeter($("#gpu-meter"),simGpu);
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

/* ------------------------------------------------------- warp starfield */
// Idle: colored stars drift gently toward the viewer.
// While a query streams, speed ramps up and stars stretch into light
// streaks — Tesla-launch-control style. Perf mode disables it entirely.
const starCv=$("#stars"),sctx=starCv.getContext("2d");
const STAR_COLORS=["#ececec","#ececec","#d4d4d4","#b4b4b4",
                   "#8e8e8e","#f5f5f5","#c8c8c8","#a0a0a0"];
let starList=[],sw=0,sh=0,warpSpeed=0.5;
function starSpawn(far){
  return {x:(Math.random()-0.5)*sw*1.6, y:(Math.random()-0.5)*sh*1.6,
          z:far?sw:1+Math.random()*sw,
          c:STAR_COLORS[Math.random()*STAR_COLORS.length|0]};
}
function starReset(s){const n=starSpawn(true);s.x=n.x;s.y=n.y;s.z=n.z;s.c=n.c;}
function starResize(){
  const dpr=Math.min(window.devicePixelRatio||1,2);
  sw=starCv.width=Math.max(1,starCv.offsetWidth*dpr);
  sh=starCv.height=Math.max(1,starCv.offsetHeight*dpr);
  starList=[];
  const n=Math.min(640,Math.round(sw*sh/6000));   // ~50% denser
  for(let i=0;i<n;i++)starList.push(starSpawn(false));
}
starResize();
window.addEventListener("resize",starResize);
function starTick(){
  requestAnimationFrame(starTick);
  if(perf)return;
  sctx.clearRect(0,0,sw,sh);
  // hard launch, soft glide back down
  warpSpeed+=((generating?22:0.5)-warpSpeed)*(generating?0.055:0.03);
  const cx=sw/2,cy=sh/2,fov=sw*0.45,move=warpSpeed*(sw/1400);
  sctx.lineCap="round";
  for(const s of starList){
    s.z-=move;
    if(s.z<1){starReset(s);continue;}
    const k=fov/s.z, x=cx+s.x*k, y=cy+s.y*k;
    if(x<-60||x>sw+60||y<-60||y>sh+60){starReset(s);continue;}
    // streak tail = where the star was a few frames back (deeper in z)
    const pk=fov/(s.z+move*3.5+0.5), px=cx+s.x*pk, py=cy+s.y*pk;
    const t=1-s.z/sw;
    sctx.globalAlpha=(generating?0.12:0.30)+0.62*t*t;  // brighter idle
    sctx.strokeStyle=s.c;
    sctx.lineWidth=Math.max(0.7,t*2.6);
    sctx.beginPath();sctx.moveTo(px,py);sctx.lineTo(x,y);sctx.stroke();
  }
  sctx.globalAlpha=1;
}
starTick();

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

function renderSetup(st){
  const stars=st.models.filter(m=>m.star);
  setupAllReady=stars.every(m=>m.status==="ready");
  const anyDl=st.busy;
  const pct=st.overall_pct;
  setupList.innerHTML=
    '<div class="big-bar"><i style="width:'+pct+'%"></i></div>'+
    '<div class="big-stat"><span>'+st.have_gb+' / '+st.want_gb+' GB</span>'+
    '<span>'+(anyDl?pct+'%':(setupAllReady?'complete':'not started'))+'</span></div>'+
    (anyDl?'<div class="big-speed">'+
      (st.speed_mbs>0?st.speed_mbs+' MB/s':'starting\u2026')+
      (st.eta_min?' \u00b7 about '+st.eta_min+' min left':'')+'</div>':'');

  if(!st.mlx_ok){
    setupNote.textContent="engine not installed — reopen the app to finish setup";
    setupGo.disabled=true;
  }else if(stars.some(m=>m.status==="error")){
    setupNote.textContent="a download failed — check your connection, then retry";
  }else{
    setupNote.textContent="free disk: "+st.disk_free_gb+" GB";
  }

  if(anyDl){
    setupGo.disabled=true;setupGo.textContent="Downloading\u2026";
  }else if(setupAllReady){
    setupGo.disabled=false;setupGo.textContent="Let\u2019s go";
  }else{
    setupGo.disabled=!st.mlx_ok;
    setupGo.textContent=(stars.some(m=>m.status==="error")?"Retry":"Let\u2019s go")+
      " \u00b7 "+Math.max(0,Math.round(st.want_gb-st.have_gb))+" GB";
  }
}

let wasDownloading=false;
function celebrateDownloads(){
  const card=$("#setup-card"),veil=$("#setup-veil"),cel=$("#celebrate");
  if(perf){closeSetup();return;}          // performance mode: no theatre
  // 1. the card grows and dissolves
  card.classList.add("done");veil.classList.add("fading");
  setTimeout(()=>{
    closeSetup();card.classList.remove("done");veil.classList.remove("fading");
    // 2. a rainbow sweeps the window
    cel.hidden=false;
    cel.innerHTML='<div class="sweep"></div>';
    setTimeout(()=>{
      // 3. …then collapses into the wordmark
      const h1=$("#hero h1");
      const r=h1?h1.getBoundingClientRect()
                :{left:innerWidth/2-60,top:innerHeight/2-20,width:120,height:40};
      const cx=r.left+r.width/2, cy=r.top+r.height/2;
      const box=document.createElement("div");
      box.className="converge";
      // enters along the same diagonal the sweep travelled
      const W=r.width*2.6, H=r.height*4.5;
      box.style.width=W+"px";box.style.height=H+"px";
      box.style.left=(cx-W/2-170)+"px";box.style.top=(cy-H/2-110)+"px";
      cel.appendChild(box);
      requestAnimationFrame(()=>{
        const w2=r.width*.55, h2=r.height*.5;
        box.style.left=(cx-w2/2)+"px";box.style.top=(cy-h2/2)+"px";
        box.style.width=w2+"px";box.style.height=h2+"px";
        box.style.opacity="0";
        if(h1)setTimeout(()=>h1.classList.add("absorb"),520);
      });
      setTimeout(()=>{
        cel.hidden=true;cel.innerHTML="";
        if(h1)h1.classList.remove("absorb");
      },1600);
    },1240);
  },910);
}

async function setupTick(){
  try{
    const st=await(await fetch("/api/setup")).json();
    renderSetup(st);
    pollEngines();
    if(st.busy)wasDownloading=true;
    else if(wasDownloading&&setupAllReady&&!veil.hidden){
      wasDownloading=false;celebrateDownloads();
    }
  }catch(e){}
}
function openSetup(){
  veil.hidden=false;setupTick();
  if(!setupTimer)setupTimer=setInterval(setupTick,1200);
}
function closeSetup(){veil.hidden=true;if(setupTimer){clearInterval(setupTimer);setupTimer=null;}input.focus();}
setupLater.addEventListener("click",closeSetup);
setupGo.addEventListener("click",async()=>{
  if(setupAllReady){closeSetup();return;}
  await fetch("/api/setup/install",{method:"POST"});
  setupTick();
});
$("#open-setup").addEventListener("click",openSetup);
(async()=>{
  try{
    const st=await(await fetch("/api/setup")).json();
    // auto-open only when the app can't hold a conversation yet
    if(st.needs_setup)openSetup();
  }catch(e){}
})();

/* -------------------------------------------------- resizable sidebar */
const sidebarEl=$("#sidebar"),SB_MIN=210,SB_MAX=560;
function setSidebar(w){
  w=Math.max(SB_MIN,Math.min(SB_MAX,Math.round(w)));
  sidebarEl.style.width=w+"px";sidebarEl.style.minWidth=w+"px";
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
$("#sb-resize").addEventListener("dblclick",()=>setSidebar(284));

/* ---------------------------------------------------------------- about */
const aboutVeil=$("#about-veil");
async function openAbout(){
  aboutVeil.hidden=false;
  try{
    const [m,st]=await Promise.all([
      (await fetch("/api/memory")).json(),
      (await fetch("/api/setup")).json()]);
    const ready=st.models.filter(x=>x.status==="ready").length;
    $("#about-facts").textContent=
      st.arch+" · "+ready+"/"+st.models.length+" models ready · "+
      m.facts.length+" things remembered";
  }catch(e){$("#about-facts").textContent="";}
}
$("#brand").addEventListener("click",openAbout);
$("#about-close").addEventListener("click",()=>{aboutVeil.hidden=true;});
aboutVeil.addEventListener("click",e=>{if(e.target===aboutVeil)aboutVeil.hidden=true;});
$("#about-logs").addEventListener("click",()=>fetch("/api/open-logs",{method:"POST"}));
$("#about-forget").addEventListener("click",async ev=>{
  const b=ev.currentTarget;
  if(b.dataset.sure!=="1"){
    b.dataset.sure="1";b.textContent="Really forget everything? Click again";return;
  }
  await fetch("/api/memory/clear",{method:"POST"});
  b.dataset.sure="";b.textContent="Memory cleared";
  openAbout();
  setTimeout(()=>{b.textContent="Forget what you know about me";},2500);
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


if __name__ == "__main__":
    threading.Thread(target=start_backend, daemon=True).start()
    print(f"\n  MillenAI {APP_VERSION}")
    print(f"  running on http://127.0.0.1:{PORT}")
    start_managed_engines()
    if not HAS_SEARCH:
        print("  (web search disabled — pip install ddgs to enable)")
    if not HAS_PSUTIL:
        print("  (telemetry simulated — pip install psutil for real numbers)")
    print()
    url = f"http://127.0.0.1:{PORT}"

    if HAS_WEBVIEW:
        # Native macOS window (WKWebView). Blocks until the window closes.
        window = webview.create_window(
            f"MillenAI {APP_VERSION}",
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
