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

### Research (the agent)
A fifth mode alongside the tiers. One model does the whole run — it plans the
searches *and* writes the brief — so there is only ever one engine load, which
on MLX is the expensive part. The flow: plan queries → search each → dedupe
sources by URL → write a brief citing them as `[1]`, `[2]` → append a linked
source list. Typical run is ~25s over 12 sources.

**Hermes 3 8B leads the Research picks.** The tier's `count` is 1, so the
first *installed* pick is the agent — order in `picks` is the whole selection
mechanism. Hermes is tuned for instruction-following and structured output,
which is most of what planning queries is, and it shows: asked about "macOS 26
Tahoe" it planned *"macOS 26 Tahoe release date"* and *"Key features of macOS
26 Tahoe"*, keeping the version intact, where Mistral Nemo drifted to "macOS
13.0" on both. Adding it to a tier's picks also adds it to `STARTER_LABELS`,
so a fresh install now pulls 4.6 GB more.

**The user's own question is always the first search query.** A local model's
knowledge stops years before the question often does: asked what changed in
"macOS 26 Tahoe", the planner searched for "macOS Monterey" — a version it
recognised — and researched the wrong operating system from end to end,
confidently and with citations. Searching verbatim first means the planner can
only ever *add* angles, never quietly replace the subject. The prompt also
tells it to copy names and versions exactly, and that an unfamiliar term is
probably newer than it is.

Auto web search is suppressed for this tier so the agent isn't handed
pre-fetched snippets for a query it hasn't planned yet. `search_results()`
keeps its own multi-entry cache — `run_search`'s single slot would evict each
query before the next could use it.

`renderMD` gained markdown links for the source list. Only `http(s)` is
matched, so a model cannot emit a `javascript:` or `data:` href; anything else
stays escaped text. Verified — no anchor, no tag, nothing live.

### Showing the blend
The drafts already existed in `run_council`; they were just thrown away. Each
one is now pushed to the UI as `\0DRAFT:{json}\0` the moment its model
finishes, and rendered as a card above the answer — open while they land, then
collapsed to "*2 of 3 models contributed*" once the merge starts. Models that
produced nothing are listed too, greyed, with the reason; a blend that quietly
ran on one model is worth seeing.

Drafts ride on the assistant message (`{role, content, drafts}`) so a reopened
chat still shows the panel, and `addMsg` takes them as a third argument. They
are deliberately kept **out of `content`** — that string is what goes back to
the model as context, what gets spoken aloud, and what a title is generated
from.

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

**The daily model nudge.** Beyond the one-time announce below, anything
still uninstalled earns a gentle once-a-day card ("More models to try",
20-hour gap so launch times drift freely). Its primary action is
**Browse models…**, deliberately not download-everything — the full missing
set can top 100 GB — and it carries its own permanent "Don't remind me
again" (`remind_models_off` in prefs.json, with `remind_models_ts` as the
clock). At most one card per launch, fresh-model announce wins the slot,
and nothing shows during first-run setup.

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

**The wordmark is the solid neon sign again.** The stroke-drawn "cycling
lines" variant lived one release (1.7.7) and was reverted on Patrick's call
— the marching dashes read as ants crawling. Solid fill + halo + paint mask
+ strike, exactly as documented above this entry.

**The warp SHATTERS, then reassembles.** Coherent tile motion read as
"just zooming in" — the explicit anti-goal — so tiles now carry wide speed
desync (zj .82–1.32), lateral scatter proportional to 1−z, and individual
spin. On completion they do not crossfade: z pulls home to 1, spin unwinds,
scatter collapses, and the pieces visibly land back in the grid as the
intact video fades up (sim: depth spread 1.32 → 0.055 within 0.6s of the
answer landing).

The skyline arrives on `loadeddata` (first decodable frame), not
`playing` — a cold cache left ~10s of black — fades in over .8s, and a
dead clip URL rotates to the next clip rather than blacking out the
session. **The warp is made OF the video**: no backdrop → no warp, by
design, which is what "the effect didn't apply" looks like when a query
runs during the buffering window.

Three things to preserve if this is ever retouched:

- **A longer duration does not slow the sweep.** Raising it 1.6s → 2.8s
  changed almost nothing visible: the eased curve plus a ±175vw travel still
  threw the band across the middle of the window in ~0.6s. Measured band
  centre against the wordmark to find it. Linear travel over only the
  distance actually needed (±120vw) is what makes it read as slow.
- **Band width and brightness are coupled.** 132vw at opacity .92 flooded the
  entire window with saturated colour and made the wordmark unreadable.
  112vw at .72 is wider than the original and still leaves the page legible.
- **The paint is timed off the band, not guessed.** Travel is symmetric and
  linear, so the band centre reaches the middle of the window at exactly half
  the duration whatever the width; the wordmark sits slightly right of centre,
  so the reveal is centred on 1.53s (delay 1.28s, duration .5s). Verified:
  at 1.28s the band is at x=528 with the wordmark starting at 611 and paint at
  0; at 1.53s band 761, wordmark centre 782, paint 50%; at 1.78s band 994,
  wordmark ending 953, paint 100%.

- **It must not wait on `/api/setup`.** That call enumerates every model on
  disk and took 2.3s here; gating the flourish on it left the window sitting
  there looking frozen. It now fires on the first `requestAnimationFrame`
  (measured: 22ms) and the setup check runs independently.
- **The fly-in easing is `linear` on purpose.** Deceleration is written into
  the keyframes. Any eased curve is far too front-loaded — the wordmark had
  settled by 0.35s, well before the band reached it, so it read as an
  unrelated event instead of something the sweep delivered.

### The skyline backdrop
One of Apple's classic ATV aerial loops of New York (the H.264 set on
`a1.phobos.apple.com` — the same feed the open-source Aerial screensaver
streams; all six URLs verified live, 87–230 MB each, streamed progressively
and never stored). A different clip every launch, never the same one twice
running (`millen.sky` in localStorage).

The launch wash REVEALS the city out of darkness — one `<video>`, hidden
behind the same travelling diagonal mask that paints the wordmark
(4.2s linear, .3s delay), and the colour stays once painted. There used to
be a greyscale copy underneath that the wash "colourised"; it was cut on
Patrick's call — revealing beats colourising — which also deleted the
dual-video sync machinery and half the decode cost.

Sending a query turns the image INTO the warp — not particles over it, the
picture itself. `buildTiles` grids the visible frame into ~850–1150 tiles
(each sampling the LIVE video every frame); at onset every tile sits at
depth z=1, which reconstructs the picture exactly, then the whole plane
accelerates through the viewer with true perspective (position and scale
both 1/z), tiles recycling behind at staggered depths into an endless
tunnel of the footage. Attack is fast (WARP_UP 1.4s) so a 3s query shows
the full effect; teardown restores the intact video seamlessly because the
canvas and the element are the same frame. Two hard-won rules: the canvas
must sit AFTER #skyline in the DOM (below it, the opaque video hides
everything), and every `let` this block touches at load time must be
declared before `starResize()` runs — the TDZ gotcha killed the whole
script once already. The CORS taint stands: never `getImageData` this
canvas.

**Failure is the old behaviour.** The div starts hidden and is shown only
after BOTH videos fire `playing`; any error hides it again. Offline, blocked,
or slow → the starfield alone, exactly as before the feature. Perf mode never
starts the videos. Note this is the one place the app talks to a third host
(read-only, no user data); the About text's "no cloud" refers to chat.

### The starfield
Idle drift; while a query streams the stars stretch into streaks. The ramp is
a 0–1 progress driven by **real elapsed time** and then eased (smoothstep),
not an exponential approach on the speed itself. Approaching a target by a
fixed fraction per frame spends most of its travel in the first fraction of a
second — it landed as a jump rather than a launch — and it runs at whatever
rate the display happens to refresh at. Now: 3.0s up, 1.8s back to idle,
measured 0.5 → 2.7 → 7.1 → 12.3 → 17.4 → 21.0 → 22 across the three seconds.
`dt` is clamped so a backgrounded tab doesn't resume at full speed. Star
brightness follows the same eased value; switching it on `generating`
flickered at the moment a query started.

### Standing preferences (the persona box)
About panel ▸ "How should MillenAI reply?" — free text the user writes
("be direct, I work in finance"), stored as `persona` in `prefs.json` and
folded into the system prompt on every request, quoted verbatim in the
user's own words with "the current message wins" as the tie-breaker.
Deliberately distinct from memory: memory is *extracted guesses*, this is
*authored instruction*, and the prompt ranks it above remembered facts.
Because it rides `dated_system`, it flows into blends and Research briefs
too, and the Gemma fold-system retry carries it automatically. Capped at
2000 chars both in the UI (`maxlength`) and the backend (slice — the API
can be hit directly). Verified end to end: "Always begin your reply with
ACK, be extremely brief" produced `ACK, blue, typically a light blue…`.

### Memory
Facts about the user are extracted in the background after each message by
whichever model just answered, stored in `memory.json`, and folded into the
system prompt. Best-effort: failures never break a chat. Clear it from the
About panel.

### Voice
**getUserMedia in WKWebView is dead by default, and it fails as a silent
hang, not an error** — measured: the promise neither resolves nor rejects,
so the mic button just did nothing. Three gates stack: (1) media devices are
disabled at the WebKit preferences level until the private
`mediaDevicesEnabled` flag is set via KVC — Safari sets it, embedders must
too; (2) pywebview (6.2.1) never implements
`webView:requestMediaCapturePermissionForOrigin:…` on its UIDelegate, and
WebKit waits forever on the missing decision; (3) macOS TCC, which needs
`NSMicrophoneUsageDescription` in Info.plist (present) and shows the normal
one-time prompt. millenai.py patches (1) and (2) at startup by wrapping
`BrowserView.__init__` and `classAddMethods`-ing a grant onto the delegate —
verified with an instrumented probe window: pref set → delegate invoked with
type 1 (microphone) → grant delivered. Everything is wrapped in try/except so
a future pywebview that fixes this natively (or changes internals) degrades
to voice-unavailable instead of breaking launch.

STT is Whisper large-v3-turbo via MLX (Apple silicon only, ~1.6 GB, fetched
on first mic tap). TTS is the macOS `say` binary — free, no download, works
on Intel. Voice chat mode auto-sends after transcription and reads replies
aloud; a new message or mic tap barges in.

**What is read aloud is not what is on screen.** `_speak()` used to receive
the raw reply, so voice chat spoke three things nobody wants to hear: the
whole chain of thought (with the tag itself pronounced, because the markdown
pass turned `<think>` into the word "<think"), the research brief's `Sources`
bibliography — which roughly doubled the length of every spoken answer — and
inline citations as bare numbers mid-sentence. It now strips think blocks,
cuts everything from a trailing `Sources` heading, drops `[1]` / `[2, 5]`
markers, and tidies the space they leave before punctuation.

### Updates
Polls GitHub Releases once a day. A release counts as newer if its
`published_at` is after this build's timestamp, or its tag carries a higher
build number. Downloading hands off to a helper script that waits for the app
to quit, swaps the bundle, strips quarantine, and relaunches.

---

### Remote access (phone / friends) — no GPU hosting needed
The app already IS a web app: pywebview is just a shell over
`http://127.0.0.1:8889`, every fetch is relative, and the viewport meta is
set. So "hosting" is exposing the Mac's own backend — the models keep
running on the M4 Pro, and no cloud GPU is ever involved. The Mac must be
awake.

**The backend has no auth of its own** — it was built for a same-machine
window. `MILLENAI_KEY` (env) is the opt-in gate: when set, every request
needs the key — `/?key=...` once sets a 30-day cookie, everything else is
403, and the app's own window appends the key automatically. Unset = old
behaviour, byte for byte. Verified: no/wrong key 403, right key 302+cookie,
cookie passes page and API, POST without cookie 403.

Personal use: Tailscale (free) — the port is reachable at the Mac's tailnet
address from the phone; nothing public. Friends: `cloudflared tunnel --url
http://127.0.0.1:8889` gives a free public HTTPS URL — set MILLENAI_KEY
first and share the URL with `?key=` included. Quirks: TTS (`say`) speaks
on the Mac, not the phone; mic input works remotely because tunnels are
HTTPS.

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

**A repetition detector cannot see token salad.** When a model melts down it
does not always loop — under memory pressure Gemma 4 emitted fragments fused
with hyphens and single characters from nine scripts
(`own-and-and ζ,탕s-तिर-der`). Every "word" there is unique, so the
unique-word ratio read **0.79**, indistinguishable from good prose, and the
guard waved it through. `_looks_degenerate()` now also tests for words
carrying 2+ hyphens (>25% of the text) and for characters from 3+ non-Latin
scripts appearing in runs averaging under 4 characters. That last condition is
what separates salad from a legitimately multilingual answer: real answers
write whole words in each script, salad glues one or two characters onto Latin
fragments.

**The merge was never checked.** Drafts were, the merge wasn't — so a merger
that collapsed streamed its collapse straight to the reader. The merge is now
watched as it arrives; on collapse it emits a `\0RESET\0` sentinel, which
tells the UI to discard everything shown so far, and falls back to the
strongest draft (already checked). Verified end to end: 1,155 characters of
salad streamed, 121 characters of clean answer displayed.

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

**Keep every `.ps1` pure ASCII.** Windows PowerShell 5.1 reads a `.ps1` with
no BOM as the ANSI codepage, not UTF-8. A UTF-8 em-dash (`E2 80 94`) therefore
arrives as three CP1252 characters, and the last of them is **U+201D, a curly
double quote — which PowerShell honours as a string delimiter.** One dash in a
*comment* silently desynced the quoting for the remaining 90 lines, so the
parser reported errors inside comments and an unterminated string at the end
of the file, with nothing wrong at any of those places. `build_windows_exe.ps1`
now carries a note to that effect and is checked with
`raw.decode("cp1252") == raw.decode("utf-8")` — if that holds, the encoding
cannot bite.

### macOS packaging

**The app icon should fill 82.4% of its canvas, not Apple's 80.5%.** The
strict macOS grid is an 824px body inside 1024, but nothing actually ships at
that: measured across every app installed here, Canva 82.6%, Ollama 82.4%,
Signal 82.3%, Firefox 82.2%, Sublime 82.2% — a tight cluster at **844/1024**.
Ours started at 897px (87.6%) and loomed over its Dock neighbours; rebuilt at
824 it read as visibly small. 844 matches the room. Rebuild by cropping to the
opaque bbox, resizing to 844, centring on a transparent 1024 canvas, and
running `iconutil` over a full 10-size iconset — the original was missing the
16×16 and 32×32 @1x variants the menu bar and list views use.

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

**Built on macOS, by `./build_windows.sh`** — and `release.sh` runs it, so
every release publishes the DMG *and* the Windows zip as assets. There is
nothing to compile: the package is `millenai.py` plus a `.bat` and a README,
so the output is identical whatever machine builds it. This replaced
`build_windows.ps1`, which could only run on Windows — keeping two copies of
the launcher and readme text would have guaranteed they drifted.

**CUDA is not built, it is downloaded.** Ollama's Windows amd64 build bundles
the CUDA runtime; the app fetches it on the user's PC and Ollama offloads to
the GPU by itself. So "a CUDA version" isn't a build target — there is one
Python file that runs everywhere.

The `.bat` and README are written through a CRLF filter. `cmd.exe` is
unforgiving about bare LF in a batch file, and PowerShell's `Set-Content` had
been supplying CRLF for free.

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

### The MSI (built in CI)
`.github/workflows/windows-installer.yml` — every published release gets
`MillenAI-<ver>-x64.msi` attached automatically: a windows-latest runner
builds the exe with `build_windows_exe.ps1` (PyInstaller cannot
cross-compile, so the Mac that cuts releases can never do this itself), then
WiX (heat harvest → candle → light) wraps `dist\MillenAI` in a per-user MSI —
no admin, Start Menu + desktop shortcuts, uninstaller in Settings.
`workflow_dispatch` with a `tag` input backfills old releases.

Two CI gotchas that cost an iteration each: **the checkout must not be the
release tag** — packaging files postdate old tags, so build scripts come from
main and only `millenai.py` + the icon are pinned to the tag; and **PowerShell
does not interpolate `-dVer=$ver`** — a token starting with `-` and containing
`=` passes literally unless quoted (`"-dVer=$ver"`), which candle reports as
version '$ver'.

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

## 1.8.0 — window-wipe boot, slat warp, the hardware ladder, go-live

### Window-wipe boot (Mac native only)
The NSWindow is born transparent (`setOpaque_(False)`, clear background,
WKWebView `drawsBackground` off via KVC) and the page starts with
`html.winwipe`: body clipped to `inset(0 0 0 100%)`, so launching the app
wipes the UI in RIGHT-to-left over the desktop, and the rainbow wash then
answers LEFT-to-right — always from the opposite side. Traps, learned hard:
* **Canvas propagation** — body's background paints the whole viewport even
  when body is clipped. During the wipe the background lives on
  `body::before` (z-index -99), which clips with everything else.
* **Occlusion** — no rAF, no animationend. `winWipeFinish` has a 1.6 s
  timeout; the classes are always dropped, the page always appears.
* **Remote visitors** share the server but sit in a real browser where a
  transparent page flashes white — the head script gates on
  `location.hostname` being 127.0.0.1/localhost.
* The native window re-solidifies by an **NSTimer at 2 s** (opaque, #212121,
  shadow + `invalidateShadow`) — deliberately not a JS bridge, so a dead
  page still yields a normal window. All AppKit/WebKit selectors dry-run
  in the app venv (`pyobjc` is not in system python).
* Boot order: kickWipe → winWipeRun (double rAF so the clipped state
  commits first) → winWipeFinish → rainbowWipe. Performance mode and
  non-Mac/browser serve skip straight to rainbowWipe.

### The warp is now VERTICAL SLATS (user-picked from a live A/B)
~28 CSS-px-wide strips, THREE rows tall, no spin, no radial rotation:
each slat keeps its own depth speed (`zj .82+rand*.5`), rushes the viewer
with true 1/z perspective, and motion-stretch runs along the slat's LENGTH
(`vstr`, ×5) so speed reads as longer lines, never sideways smear. Lateral
drift is small (`.035`) and proportional to (1−z), so settling is exact:
z pulls home at `dt*5`, scatter collapses to zero, the intact video fades
up underneath. Square-shard and spin variants are dead — "split into long
vertical lines and just zooms" is the spec, and it must "settle neatly".
Tuning harness: scratchpad/warp.html (synthetic skyline — the pane blocks
the Apple CDN — with `setGen()`/`step()` because the pane starves rAF).

### The hardware ladder (catalog 2.0 groundwork)
`HW_CLASSES` groups the sidebar by the MACHINE a model needs (Everyday /
Performance 32 GB / Flagship 64–96 GB / Titan 128 GB+), and
`model_fits_machine` (needs ≤ 75% of total RAM) HIDES what can't fit —
sidebar, add-models panel, and setup all filter. New verified rungs (HF +
Ollama registries, 2026-08-01): GPT-OSS 20B/120B, Qwen 3.6 27B/35B-A3B,
Llama 3.3 70B (now MLX too), Llama 4 Scout, Qwen 3 235B-A22B, GLM-5.2 and
DeepSeek R1 671B (MLX-only, 512 GB-class). `STARTER_LABELS` is now the
AUTOSELECT: best fitting pick per tier only (~25 GB on a 48 GB Mac, three
models), not every pick that fits (~118 GB — the bug this replaced).
NB: this Mac is 48 GB total, budget 36 GB — the 70B correctly vanishes here.

### go-live.sh — the always-on, self-updating instance
One idempotent script: managed clone in `~/Library/MillenAI-live` pinned to
the newest `v*` tag, LaunchAgent serving HEADLESS on :9889 (8890 was a trap —
it's Gemma 2 9B's engine port; engines own 8884–8930), 6-hourly updater
(fetch tags → checkout → `launchctl kickstart`), Cloudflare named tunnel at
ai.millertechnology.net once `cert.pem` exists (the login click is the one
human step; the script opens the page and waits, and everything else
installs regardless). Needs `MILLENAI_HEADLESS=1` (no window, no
webbrowser.open) and `MILLENAI_PORT` — both shipped in 1.8.0, so the live
instance only works from v49 tags onward. The access key lives in
`~/Library/MillenAI-live/key` (0600), never in the repo.

### 1.9.x — the door, and why the web skyline was black
1.9.0 replaced the plain-text 403 with THE DOOR: the bare public URL shows
a styled key box (wrong key = note, API paths keep the terse 403), so the
shareable address is just ai.millertechnology.net + a spoken key.
1.9.1: the skyline never played on the https tunnel because the phobos
clip URLs are http-only (its https cert is broken — curl exit 60) and
browsers hard-block http media on an https page, silently. The clips now
come from sylvan.apple.com (tvOS-13 CDN, valid TLS, H.264/AVC so every
browser decodes them — the 2x/entries.json variants are HEVC-only, which
Firefox can't play). NYC URLs live in Apple's resources-13.tar
entries.json; guessing `NY_*_2K_SDR_HEVC.mov` names 404s.

## 1.10.0 — the server owns the skyline; the web got people

### Skyline: cache + remux, never stream the CDN to a browser
The sylvan AVC files are `ftyp/wide/mdat/moov` — the moov INDEX sits after
370 MB of data, so a browser has nothing to play until the entire file
arrives ("background not loading", again). The server now downloads each
clip once, remuxes it fast-start in PURE PYTHON (recursive moov walk,
stco/co64 offsets shifted by exactly len(moov) — a naive byte-scan for
'stco' can hit sample data), caches under app_dir()/sky, and serves
/sky/<i>.mov same-origin with real Range support (Safari scrubs with
dozens of byte-range requests, including suffix ranges `bytes=-N`).
`/api/sky/status?i=` drives the macOS-style #skyload bar while a clip
warms. Verified in-browser: remuxed file plays in ~6s and the reveal
unhides; atom order ftyp/moov/wide/mdat; both range forms 206.

### Multi-user: nobody sees Patrick's chats through the tunnel
Remote requests are the ones carrying Cf-Connecting-Ip/X-Forwarded-For
(cloudflared adds them; local/native requests never have them). Remote
visitors with no identity get the WELCOME page (name + 4-12 digit PIN;
"Continue with Google" appears once app_dir()/google_oauth.json holds a
client_id/client_secret). Identity = sha256 hash → cookie `millen_user`
(HttpOnly) → all chats/memory/prefs live under app_dir()/users/<id>/.
Every storage function takes `base=None`; None = legacy owner files,
which a remote request can NEVER reach (cookieless remotes get a shared
`_anon` pen). A wrong PIN is just a different empty profile — that is the
security model, not a bug. Verified: owner/buddy/anon fully isolated in
both directions, desktop app untouched.

### Warp: slats now shoot DIAGONALLY
Per Patrick ("more diagonal like stars shooting"): the slat field drifts
up-right (.28/-.16, jittered by zj) and leans ~7° into the motion, both
scaled by scat=(1-z) so the settle still lands pixel-exact. Vertical
motion-stretch unchanged.

### 1.10.2 — warp: more split, and optimized
More fragments (44px slats, FOUR rows, zj .7+.9, scatter .05, rate
.45+2.8e, WARP_UP 1.1) and two render optimizations that keep the look
identical: the video is drawn ONCE per frame into a 1280-wide snapshot
canvas and every slat blits canvas->canvas (the per-tile video reads were
the cost), and the warp canvas caps at 1.5x DPR (invisible on fast-moving
slats, nearly halves fill). Snapshot only happens while the warp is
active — idle cost is zero. Tiles rebuild if the snapshot dims change.

## 1.11.0 — needle streaks, guarded singles, hardened web, Claude-grade voice
* Warp: ~1800 needle-fine streaks (22px cells, thickness .32 of cell and
  thinning further with speed via /sqrt(len)) — "tesla launch mode".
* Single-model streams now run through _stream_guarded too; a collapse is
  cut back to its coherent prefix by _detruncate (repetition loop like
  "a walking path" x300 reached the reader unguarded before).
* Security: constant-time key compares (secrets.compare_digest), ADMIN
  endpoints (downloads, updater, open-logs, speak, voice/prepare) 403 for
  remote visitors — guests chat, they don't operate the host. Server was
  already localhost-bound; renderMD already escapes model HTML.
* PIN minimum is 8 digits (client + server).
* SYNTH_INSTRUCTION carries the voice spec (lead with the answer, prose
  over bullets, no filler, length matched to the question) — the merge is
  where the final answer's personality is written; SYSTEM_PROMPT aligned.

## 1.13.0 — the masterpiece pass
* THE SLAM replaces the bloom at wash-impact (2.3s): two conic-rainbow
  shockwave rings (ring shape cut by a radial mask), a screen flash
  centred on the wordmark (--fx/--fy custom props), an 18-spark burst
  (per-spark --dx/--dy/--hue), chromaSnap on the h1 (red/cyan ghosts at
  ±14px snapping together with overshoot), and a decaying quake on #main.
  All CSS-driven; perf mode kills the lot. Verified by frozen-frame
  (paused animations at negative delays).
* Google SSO is LIVE end-to-end: project "millenai" under the
  millertechnology.net org, client "MillenAI Web", redirect
  https://ai.millertechnology.net/auth/google/callback, audience External
  + In production (no verification needed for openid/email). Secret went
  clipboard->google_oauth.json (0600), clipboard cleared, never displayed.
  GOTCHA: curl with a spoofed Cf-Connecting-Ip header gets Cloudflare
  error 1000 — CF rejects requests carrying its reserved headers; test
  remote behaviour with plain requests through the tunnel instead.
* Reliability run (live engines): Llama 3.2 3B passed the exact
  central-park looper prompt post-guard (2995 chars, max 3-gram x5);
  Hermes 3 8B clean. NB: offline single models hallucinate facts
  confidently (Hermes invented a "Hot Dog Palace") — that is what Live
  web search is for. Voice prompts now push generous, human answers.
* First-run: "N models fit in your memory", button "Send it" -> "LFG".
* Dock icon: the icns body is already 922px/90% (bigger than Apple's
  824px standard) in BOTH repo and installed app — the "tiny icon" is
  macOS icon-cache staleness. lsregister -f + Dock restart applied; the
  system store (/Library/Caches/com.apple.iconservices.store) needs sudo.

## 1.13.0 — vision: paste an image, MillenAI reads it
Paste (⌘V) an image into the composer: client downscales to ≤1280px JPEG,
shows removable chips, sends `images:[dataURL]` beside the text. Server:
any request with images routes WHOLE to LLaVA Vision 7B on Ollama's
NATIVE /api/chat (per-message `images:[raw-base64]` — strip the dataURL
prefix), tier/council/web-search all bypassed ("vision answers come from
the pixels"). Empty text gets a default "describe this" prompt. If LLaVA
isn't pulled yet the request kicks its download and says so instead of
erroring. Verified end-to-end: a 1x1 red PNG came back described as "a
solid red background". Guarded stream path applies to vision too.
Also in 1.12.7: _looks_degenerate now judges the TAIL (last 120 words
< 0.25 unique) — a collapse behind a healthy preamble amortized the
whole-text ratio to 0.33 and "party" x600 reached a phone.

## 1.15.0 — the pixel-aware VFX trio
Same-origin video (since the sky cache) un-tainted the canvas, making
getImageData LEGAL for the first time. Three effects ride it:
* CITY LIGHTS ANSWER YOU: a 160x90 probe of the live frame harvests the
  brightest real pixels (windows/headlights/stars) every ~420ms during
  generation; up to ~140 motes drift viewer-ward in their TRUE colours,
  drawn with a cheap two-circle glow (no shadowBlur) under 'screen'.
* LONG-EXPOSURE TRAILS: the warp canvas fades via destination-out
  (alpha .28) instead of clearRect while active — streaks leave phosphor.
  Calm path still hard-clears; motes purge on settle.
* HYPERLAPSE THINKING: vid.playbackRate = 1 + e*5 — the city races to ~6x
  while a model works and eases home with the settle. The tiles sample
  the live frame, so the streaks carry the accelerated footage.

### The TDZ rule (three strikes tonight)
`tiles`, then `agent`, then `sndOn`: a `let` used by ANY code that runs
earlier in the script kills the WHOLE page silently (typeof does NOT
save you — TDZ throws on typeof too). Every shared mutable `let` now
belongs at the TOP of the script next to `messages`. Diagnosis trick
that found all three: re-execute the page's own script text via
`new Function(src)()` in the console and read the thrown line.

## 1.20.0
- File upload: 📎 in the composer. Images join the vision pipeline
  (shared addImageFile with paste), text-like files ride as ATTACHED FILES
  blocks in the last message (2 max, 50k chars each, auto_web off).
  Doc chips reuse the imgchips strip. Smoketest: ZEBRA-42 retrieval.
- Fast + Smart MERGED into "Fast" (strongest fitting model, count 1).
  Aliases in BOTH places: client localStorage may hold "Smart", old
  clients may POST tier:"Smart" — both map to Fast. Smoketest keeps a
  legacy-alias check.
- ACCESS KEY DOOR RETIRED per Patrick: _gate() returns True; the welcome
  screen (name+PIN, Google SSO button when configured) is the front door.
  Old /?key= links land on the app harmlessly. ADMIN_PATHS + per-identity
  storage are the real protection now. GATE_PAGE is dead code.
- Sidebar 340px; controls row order: version pill, UPDATE, (spacer),
  newchat, gear. The .tag moved OUT of #brand — selector is #brand-row .tag.
- Wordmark is HOLLOW: gradient lives in the stroke. Trick: background-clip
  clips gradient to text+stroke, then a SOLID -webkit-text-fill-color
  paints the fill back on top, leaving gradient only in the ring.
  51px/800, drift slowed to 52s. Chameleon vars unchanged.
- Hero: halo opacity .85->1 + blur 16->19 (the "+20% glow"); greet 48px;
  LIVE fill rgba(85,85,85,.5).
- Agents list folds like the tier dropdown (#agents-wrap.closed). Boot
  always opens the AI tab and CLEARS any stored agent (per-session now).
- Telemetry: meters 4px; t-head 12.5px nowrap (13.5 wrapped M4 PRO into
  the models count at 340px).
- GOTCHA: `pkill -f "MILLENAI_PORT=9894"` does NOT kill the server — env
  assignments aren't in python's argv; it kills the background *shell
  wrapper* only, orphaning the python (which keeps the port; the "new"
  server then silently fails to bind and you test STALE CODE). Kill by
  port: `kill $(lsof -tnP -iTCP:9894 -sTCP:LISTEN)`.
- GOTCHA: mlx_lm.server seeds its RNG identically at spawn — same prompt
  on a fresh engine can reproduce output byte-for-byte even at temp .75.
  Consequences: (a) identical-output "caching" mirages while testing,
  (b) a bare retry after a collapse can replay the SAME collapse — the
  guard's retry now appends an anti-repetition nudge to the last user
  message so attempt 2 takes a different path.
- Doc QA framing: question-first + raw ATTACHED FILES block made the 35B
  read ZEBRA-42 and then DENY it existed ("is this a prank?"). Files
  first, explicit "real data, answer factually" frame, QUESTION: last.
  Smoketest rejects denial-shaped answers, not just substring hits.
- 1.20.2 TUNNEL HEARTBEAT: Cloudflare drops a proxied response after
  ~100s without bytes. Engine swap + big-model load = multi-minute wire
  silence → remote council runs died as "network error" with zero drafts
  while every localhost test passed. Fix: heartbeat thread in the chat
  handler re-sends the last STATUS marker after >20s quiet (writes behind
  a lock, hb_stop.set() on every exit path). Verified by measuring
  inter-byte gaps through a full Thinking run: max 22.2s.

## 2.0.0
- ZERO-CLICK FIRST RUN: needs_setup now auto-POSTs /api/setup/install —
  the machine-sized starter set downloads with no button press; headline
  reads "NN GB memory detected". Endpoint stays owner-only, so remote
  guests can't trigger host downloads (their POST 403s silently).
- setup_status() gained mem_gb (psutil total, rounded).
- USERS row removed from telemetry; box is rgba(47,47,47,.5) + 14px
  backdrop blur (sidebar's frosted material).
- Context: ~/.cache/huggingface was manually deleted (Finder, 03:33) —
  five stale 70B ollama pulls freed 194GB; ladder re-downloaded via
  snapshot_download. NOT app code — nothing in MillenAI deletes that dir.
- 2.0.1 HOTFIX: audio removal left `audioCtx.resume()` inside send() —
  ReferenceError on EVERY send, silently (2.0.0, ~15 min in the wild).
  `x&&x.y` does NOT guard an undeclared identifier — same family as the
  TDZ rule: grep for EVERY identifier a removal deletes, including uses
  inside guards. send() is now wrapped (sendSafe): any exception paints
  "send failed — <msg>" into the composer instead of eating the click.
- 2.5.2 hardening: (a) stuck-download WATCHDOG in setup_status — a job
  10 min at the same pct flips to error instead of holding busy forever
  (Phi-4 wedged at 99% after my .incomplete sweep raced its writer);
  (b) _voice_ready keys on the weights symlink existing, NOT on carcass
  absence — a stale *.incomplete beside a finished blob bricked voice.
  Voice verified end-to-end: say -> /api/transcribe exact match, speak ok.

## 2.7 — FLEET (Contribute)
- Friends' GPUs answer hub queries: worker connects OUTBOUND via
  long-poll HTTP (25s poll < CF 100s window, no router config). Endpoints
  /api/fleet/{register,poll,submit} gated by X-Fleet-Key (fleet_key file,
  0600, auto-minted). /api/fleet/status is owner/local-only (shows key +
  workers). Router offloads SINGLE-model, non-vision jobs only; 150s
  wait; degenerate or timed-out results fall back to local silently —
  the fleet can only make things faster.
- Worker side: prefs contrib_on/url/key; contrib_apply() retires the old
  thread BEFORE starting (args are baked at spawn — an empty-key loop
  kept retrying forever after the key was fixed. Seen live.)
- Trust: workers see the prompts (incl. the hub user's memory in the
  system message). Friends only. UI: Settings › Contribute my GPU.
- Verified: two local instances, hub routed "why is the sky blue" to a
  registered worker, 377 chars in 5s, status line names the friend.
