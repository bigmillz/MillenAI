# MillenAI — developer notes

A local-only LLM desktop app for macOS. Everything runs on the user's machine:
models, transcription, speech, memory. Nothing is sent anywhere except
optional DuckDuckGo lookups and the GitHub update check.

Current: **1.1.0** (build 26) · repo `bigmillz/MillenAI`

---

## Layout

| File | What it is |
|---|---|
| `millenai.py` | The whole app — HTTP backend, model routing, and the UI as one embedded HTML string (~3,400 lines) |
| `build_macos_app.sh` | Wraps `millenai.py` into `MillenAI.app` |
| `build_dmg.sh` | Builds the app, then a styled DMG with custom artwork and Finder layout |
| `release.sh` | Bumps version → builds → commits → pushes → publishes a GitHub Release |
| `MillenAI.icns` | App/volume icon |

Not in git (see `.gitignore`): built `.app`, `.dmg`, `__pycache__`, and
`v23-*.sh` — those are unrelated VPN scripts and one contains a private
server address.

## Running it

```bash
python3 millenai.py          # serves on 127.0.0.1:8889, opens a pywebview window
```

Everything user-generated lives outside the bundle, so updates never
clobber it:

- `~/Library/Application Support/MillenAI/venv` — private Python env
- `~/Library/Application Support/MillenAI/memory.json` — long-term memory
- `~/Library/Logs/MillenAI/` — engine + bootstrap logs
- `chats.json` — conversation history (see below)

## Releasing

```bash
./release.sh patch     # bug fix        1.0.1 -> 1.0.2
./release.sh minor     # new feature    1.0.1 -> 1.1.0
./release.sh major     # rewrite        1.0.1 -> 2.0.0
./release.sh 1.4.2     # explicit
```

Semantic versioning: **patch** for fixes, **minor** for features, **major**
only for a deliberate rewrite.

`APP_BUILD` is a separate monotonic counter that always increments and is
what the updater actually compares — so the marketing version can move
however you like (even backwards) without breaking updates. The release tag
is `v<build>`; the release *title* is the version.

**`APP_VERSION` and `APP_BUILD` in `millenai.py` are the single source of
truth.** Both build scripts read them at build time — the Info.plist, DMG
volume name, filename, and artwork all derive from them. This exists because
earlier releases shipped with mismatched versions in three different places.

Needs `gh` authenticated (`brew install gh && gh auth login`). Tokens stay in
the Keychain; nothing is stored in the repo.

---

## How it works

### Model catalog
One `CATALOG` list defines all 19 models: label, icon, MLX repo, Ollama tag,
port, RAM need, download size. Everything else derives from it —
`MODEL_ROUTES`, `MLX_REPOS`, `MODEL_MEM_BYTES`, `SUPPORTED`, the sidebar rows.
Adding a model is one line.

Apple silicon prefers MLX (fast Metal); Intel falls back to Ollama for the
same models. A model with no Ollama tag is greyed out as "Apple silicon only".

### Tiers
`Fast` (1 model) → `Thinking` (3, plus a "reason step by step" hint) →
`Pro` (5) → `Power` (everything that fits, under *All models*). Each resolves
at request time against what is actually downloaded *and* fits in current free
RAM, then tops up with any other installed model, strongest first.

Blending is sequential — **only one MLX engine can be resident at a time**,
since each pins its full weights in RAM. Parallel calls thrash.

Excluded from auto-blending: vision models (`LLaVA`) and anything under
2.4 GB (1B-class models produce degenerate output). **Power Mode opts out of
those quality filters** — if a model can run, it takes part.

Memory is the only hard limit, and it scales with the machine: a model must
fit in 1.25× its estimated need *and* stay under 80% of total RAM. Estimates
run low — a "44 GB" 70B was measured at 49.7 GB before being OOM-killed — so
a 70B is refused on a 51 GB Mac but allowed on a 128 GB one.

### The merger
Gemma writes the final blended answer, preferring the newest generation
installed: **Gemma 4 12B → Gemma 4 26B → Gemma 2 9B IT**, then the strongest
model that fits. The choice of Gemma was measured, not assumed — same three drafts containing nine distinct facts, merged by each
candidate:

| merger | time | words | facts kept | notes |
|---|---|---|---|---|
| **Gemma 2 9B IT** | 11.9s | 121 | **9/9** | concise, clean |
| Mistral Nemo 12B | 13.8s | 207 | 9/9 | injects markdown headers |
| Llama 3.1 8B | 11.3s | 260 | 9/9 | verbose |
| DeepSeek R1 | 18.3s | 183 | 8/9 | slow, **invented a fact** |

Drafts are capped at the 5 strongest and truncated to ~1,500 chars each —
an unbounded merge prompt overflows small models and triggers repetition
loops.

**Chats live on disk, not in localStorage.** WebKit keys its storage to the
bundle identity — `MillenAI` when launched from the .app but
`org.python.python` when run from source, and that store is shared with every
other Python/pywebview app. Relying on it meant history could vanish on an
update or a launch-method change. The backend now owns `chats.json`
(atomic writes); localStorage is only a mirror so the sidebar paints
instantly, and existing localStorage chats are migrated up on first run.

**Tier and single-model are mutually exclusive.** Picking a tier clears any
individual model selection and vice versa, so exactly one row is ever
highlighted. This matters beyond cosmetics: the backend prefers `tier` over
`models`, so leaving a stale tier set made explicit model picks silently
ignored.

**New models announce themselves.** `prefs.json` records which model labels
the user has already been offered. On launch, anything in the catalog that is
neither installed nor previously offered gets a one-time "New models
available" prompt — so shipping a release that adds models surfaces them
instead of leaving them buried in "Add models…". First run records the whole
catalog as seen, so nothing is announced to a brand-new install.

### The opening flourish
`rainbowWipe()` runs on every launch and again when downloads finish — one
function, so the two are always identical. A rainbow band crosses the window
diagonally (1.6s) with a narrow white core just behind it; the wordmark rushes
in from 2.3× scale under 22px of blur and lands at ~0.8s, exactly when the band
crosses the middle, with a bloom flaring behind it. The version tag and
greeting rise in on a 0.34s delay so the screen assembles rather than appears.
Then the existing converge-and-absorb finish plays.

Two things to preserve if this is ever retouched:

- **It must not wait on `/api/setup`.** That call enumerates every model on
  disk and took 2.3s here; gating the flourish on it left the window sitting
  there looking frozen. It now fires on the first `requestAnimationFrame`
  (measured: 22ms) and the setup check runs independently.
- **The fly-in easing is `linear` on purpose.** Deceleration is written into
  the keyframes. Any eased curve is far too front-loaded — the wordmark had
  settled by 0.35s, well before the band reached it, so it read as an
  unrelated event instead of something the sweep delivered.

### Memory
Facts about the user are extracted in the background after each message by
whichever model just answered, stored in `memory.json`, and folded into the
system prompt. Best-effort: failures never break a chat. Clear it from the
About panel.

### Voice
STT is Whisper large-v3-turbo via MLX (Apple silicon only, ~1.6 GB, fetched
on first mic tap). TTS is the macOS `say` binary — free, no download, works
on Intel. Voice chat mode auto-sends after transcription and reads replies
aloud; a new message or mic tap barges in.

### Updates
Polls GitHub Releases once a day. A release counts as newer if its
`published_at` is after this build's timestamp, or its tag carries a higher
build number. Downloading hands off to a helper script that waits for the app
to quit, swaps the bundle, strips quarantine, and relaunches.

---

## Gotchas

Things that cost real debugging time. Most are non-obvious and will bite
again if forgotten.

**Gemma rejects the `system` role.** Its chat template errors outright. The
app detects this and retries with the system prompt folded into the first
user turn.

**`duckduckgo_search` is dead.** Renamed to `ddgs`; the old package still
imports fine but returns **zero results silently**. Web search was quietly
broken until this was caught. Use `ddgs`.

**Hugging Face's Xet backend hides progress.** Files only materialise at the
end, so progress bars sit at 0% then jump. `HF_HUB_DISABLE_XET=1` is set at
import to force the classic CDN path (also dodges harsher anonymous rate
limits).

**`config.json` is not a completeness signal.** It lands early in a download.
Completeness requires the safetensors, every shard named in the index, and
zero `*.incomplete` blobs — otherwise models report "ready" at 1% downloaded.

**`atexit` does not run on SIGTERM.** Force-quitting orphaned multi-GB model
servers every time. Signal handlers are installed for TERM/INT/HUP.

**Ollama tag matching must be exact.** Having `llama3.2:latest` does not mean
`llama3.2:3b` will resolve — Ollama 404s. Loose matching made models look
ready when chat would fail.

**GitHub timestamps are UTC.** `time.mktime` reads them as local, making every
release look hours newer than it is — so every install would nag about an
update to the version it is already running. Use `calendar.timegm`.

**launchd `KeepAlive` agents are a trap.** The old autostart agents fought the
Ollama menubar app for port 11434 and respawned instantly when OOM-killed,
producing two permanent crash loops and ~14 GB of pinned RAM that survived
quitting the app. The app manages its own engines now; those agents are
removed.

**1B models write garbage titles.** Observed looping "address address
address…" for 16k characters. Title generation requires a ≥2.4 GB model.

**Few-shot prompts confuse small chat models.** Given completion-style
examples, they echo the examples instead of reading the actual message.
Direct instructions work; few-shot does not.

**Reasoning arrives in `delta.reasoning`, not `delta.content`.** mlx_lm
streams a reasoning model's chain of thought in its own field. The parser read
only `content`, so Gemma 4 appeared to answer with *nothing* — and because
Gemma 4 is the preferred merger, every blended answer died with "the server
answered but sent no usable completion". `reasoning` is now wrapped in
`<think>` tags and flows into the same collapsible block DeepSeek R1 uses.

**Native reasoning is requested OFF** via
`chat_template_kwargs: {"enable_thinking": false}`. Gemma 4 26B does not
converge: asked for a taco recommendation it emitted 11,937 characters of
deliberation, hit the token ceiling and returned no answer at all, in 77
seconds. The same question answers in 8.9s with thinking off, and a five-draft
merge went from *15k characters of thought and no answer* to a clean merge in
5.2s. Templates that don't know the flag ignore it, so it is safe to send to
every model. `run_model(..., thinking=True)` can still opt back in.

**Never feed reasoning back into a prompt.** It runs many times longer than
the answer it precedes, so an unstripped draft blows straight past the
1,500-character merge truncation and buries the actual answers. `strip_think()`
is applied to council drafts, titles and extracted memories; only the text
streamed to the user keeps its `<think>` block.

**Two CSS animations on one property: the last in the list wins, silently.**
The wordmark already ran `hueshift`, which animates `filter`. Adding a fly-in
that also animated `filter: blur()` meant one of them was simply discarded —
no warning, no console error, the blur just never rendered and the effect
degraded to a bare scale. `hueshift` is now dropped for the duration of the
fly-in. Related: setting `animation` on a class **replaces** the whole list
rather than adding to it, so `#hero h1.flyin` has to restate `rainbow`.

**Temporal dead zone kills the whole script silently.** A `let` referenced
during boot before its declaration throws, aborting everything after it —
with no console error if the tab attached late. Declare shared state at the
top. Syntax-check the served page (`node --check`) as part of verification.

### macOS packaging

**Finder only persists a DMG window size if it sees the bounds *change*
while frontmost.** Set them twice with a one-pixel nudge.

**Apply the volume icon *after* the Finder styling pass** — that pass deletes
`.VolumeIcon.icns` and clears the custom-icon flag.

**Stale mounts break builds.** A leftover `/Volumes/MillenAI …` makes the
styling step fail with "Can't get disk". The script now detaches first and
waits for the volume to appear.

**macOS 15+ removed right-click → Open.** Unnotarized apps must be allowed
via System Settings ▸ Privacy & Security ▸ Open Anyway. The DMG artwork
explains this in three numbered steps. Ad-hoc signing (free) does *not*
satisfy Gatekeeper — only paid notarization does. AirDrop sets no quarantine
flag at all and sidesteps the whole thing.

---

## Windows / CUDA

A platform layer now covers both OSes from one `millenai.py`. `IS_MAC` /
`IS_WIN` / `IS_ARM` drive the branches; everything else is shared.

| Concern | macOS | Windows |
|---|---|---|
| Inference | MLX (Apple silicon) → Ollama fallback | Ollama only — **CUDA automatically** |
| Speech-to-text | `mlx-whisper` large-v3-turbo | `faster-whisper` CT2 turbo, CUDA fp16 → CPU int8 |
| Text-to-speech | `say` | PowerShell SAPI |
| GPU telemetry | `ioreg` Device Utilization % | `nvidia-smi --query-gpu=utilization.gpu` |
| Chip label | `sysctl` brand string | GPU name, e.g. `RTX 4090` |
| Data dir | `~/Library/Application Support/MillenAI` | `%LOCALAPPDATA%\MillenAI` |
| Ollama engine | `ollama-darwin.tgz` (146 MB) | amd64 zip (1.5 GB, bundles CUDA) or arm64 zip (209 MB, CPU-only) |
| Package | DMG + `.app` | `MillenAI-<ver>-Windows.zip` + `.bat` launcher |
| In-place update | yes (swaps the bundle) | not yet — points at the release page |

Build with `powershell -ExecutionPolicy Bypass -File build_windows.ps1`.
Like the Mac build the zip is tiny: the launcher creates a venv on first run
and the app fetches Ollama and models itself.

**Windows-on-ARM must run the app as emulated x64.** Not a preference — a
hard dependency wall. `pythonnet` 3.1.0 (pywebview's Windows backend; there
is no alternative, `cefpython3` stopped at Python 3.7) publishes a single
`win32.win_amd64` wheel, and `ctranslate2` 4.8.1 (faster-whisper) is
`win_amd64` only. On an ARM64 Python both fall back to building from source
and fail, so the window never opens. Install the x64 python.org build; Win11
emulates it transparently.

Ollama stays **native ARM64** regardless, because it is a separate process
reached over HTTP — the architecture of the Python process is irrelevant to
it. Emulation cost therefore lands on the UI, where it is invisible, not on
inference. This is why `IS_WIN_ARM` comes from `IsWow64Process2` (the
*machine*) and not `platform.machine()` (this *process*): an emulated x64
process reports `AMD64` and would otherwise pull the 1.5 GB CUDA build onto
a machine that can never load it.

**Windows-on-ARM has no CUDA** — no NVIDIA support exists for it, so those
machines are CPU-only whichever build they run.

**CUDA needs no code.** Ollama detects an NVIDIA GPU and offloads on its own;
the Windows zip ships the CUDA runtime. A 4090 will comfortably outrun an
M4 Pro here.

### Status: written, not yet run on Windows
No Windows machine or NVIDIA GPU was available. What *was* verified, by
forcing the Windows branches on a Mac: paths resolve under `%LOCALAPPDATA%`,
all 17 models route to Ollama with zero MLX, the correct Ollama zip and
CT2 Whisper repo are selected, `nvidia-smi` output parses into both the GPU
percentage and an `RTX 4090` chip label, and speech builds a PowerShell SAPI
command with markdown stripped. macOS was re-tested end to end afterwards
(chat, telemetry, voice, transcription) and is unchanged.

Expect first-run friction on Windows: Python must be installed manually,
SmartScreen will warn about an unknown publisher, and `faster-whisper` needs
a working CUDA/cuDNN install for GPU transcription — it falls back to CPU
rather than failing.
