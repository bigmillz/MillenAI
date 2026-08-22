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
- 2.7.2 ONE-CLICK CONTRIBUTE: no URLs, no keys for friends. Worker knocks
  keyless (persistent wid in prefs) -> owner sees "X wants to contribute
  [Approve]" in Settings -> approval mints a token handed over in a
  ONE-TIME claim window (lost token = approve again). approve lives
  INSIDE the /api/fleet/ prefix branch (a standalone route after it was
  dead code — the prefix router ate it. Seen live.) Legacy shared-key
  workers still work. Hub URL defaults to FLEET_HOME; advanced fold
  keeps the override.
- 2.8 NO LIMITS: models-up arrow on the MODELS bar opens the plan panel;
  "No limits" checkbox (prefs no_limits, cached in _no_limits) makes
  model_fits_machine offer everything SUPPORTED and model_fits_memory
  stand down entirely (a 70B on 48GB swaps hard — explicit ask). The
  unlocked Max flagship is capped at <= physical RAM (70B yes, 120B no).
  GOTCHA: docstring-anchored inserts — model_fits_memory has NO
  docstring; the gate landed in weather_snippets and would have returned
  True for every forecast. Anchor on the def line, always.

## 2.10 — quality + fleet invite
- TWO-PASS ANSWERS (biggest local quality lever): single-model tiers now
  draft SILENTLY, then stream a self-revision (REVISE_INSTRUCTION). Same
  weights, markedly better prose — councils already had a critic step,
  single answers never did. Skipped for greetings/short prompts
  (_is_substantive), images, and web-data answers. Pref `polish`
  (default on) + Settings checkbox. Measured: 1583 -> 2664 chars.
- One-time "Share your GPU?" invite after the app is usable (prefs
  seen_share); Yes flips contrib_on straight to the fleet.
- REMINDER (cost me a test cycle again): a stale server holding the port
  means the new process silently fails to bind and you test OLD code.
  Always `kill $(lsof -tnP -iTCP:<port> -sTCP:LISTEN)` first.
- 2.10.1 SELF-HEALING ENGINES (root cause of "The engine returned
  nothing"): MillenAI instances SHARE engine ports 8884-8930, so the
  live service restarting (every release kickstart!) or any second
  instance exiting terminated engines the desktop was mid-use of.
  Fixes: (a) run_model respawns the MLX engine and retries on URLError
  AND on a silent/empty stream (once each); (b) stop_managed_engines
  leaves engines alone when a sibling MillenAI is listening on 8889/9889.
  Verified by killing the live engine pid mid-session: next query
  recovered with no user-visible error.
- 2.10.4 GATEKEEPER: the app is only AD-HOC signed (runs, but every
  download is quarantined) — the real fix is a $99/yr Apple Developer ID
  + notarization, which needs Patrick's enrollment. Until then: a help
  card fires WITH the download click on the web page, in the OS's own
  words (mac: "cannot be opened"/"Apple could not verify" -> System
  Settings > Privacy & Security > Open Anyway; win: SmartScreen "More
  info" > "Run anyway"). The DMG background already carries the same
  three steps.
- 2.12 TURBO (optional free cloud GPU): ~/…/MillenAI/cloud.json
  {"name","base","key","model"} enables an OpenAI-compatible endpoint
  (Groq / Cloudflare Workers AI / OpenRouter / Together all fit, all have
  free tiers). Switch in Settings appears ONLY when the file exists, and
  prompts leave the machine only while it is on; any failure falls back
  to local silently. The key is never entered through the UI or chat.
  NOT usable: Colab/Kaggle notebooks — their terms forbid using them as
  a remote inference server.
- 2.12.1 TURBO GOTCHA: provider edges (Groq behind Cloudflare) 403 a bare
  `Python-urllib` UA with "error code: 1010" — cloud_stream now sends a
  real User-Agent + Accept. curl works where urllib doesn't; if a
  provider "tests fine in turbo.sh but says unavailable in-app", that is
  the fingerprint. Also: the revise pass had to be told not to open with
  "Here's a rewritten version".

## 2.14
- THE WARP IS RETIRED (Patrick: "too much GPU and too laggy"). starTick
  is a no-op that hides #stars; no canvas, no per-frame video reads. The
  moment is carried by CSS only: body.gen dims #skyline, and the
  streaming answer wears a bottom mask so the newest line emerges from
  transparency (.msg.ai.live). paintBrandFromSky now samples the VIDEO
  directly — it used to read the warp's snapshot canvas, which no longer
  exists.
- Backdrop rotates per launch again (loading bar is the point) and the
  New backdrop button is gone.
- Greetings got a New York accent.
- 2.14.2 BACKDROP VARIETY: the picker only ever chose from the CACHE, and
  the LRU held 6 — so the same six clips cycled forever even though all
  89 were "eligible". Fixes: reach for an uncached clip on ~45% of
  launches (or always while the cache is thin), never repeat the last
  clip, LRU 6 -> 12 (~2.6 GB), and one background prewarm per launch.
  Modelled over 400 launches: 82 distinct clips.
- 2.14.5 "engine returned nothing", ROOT CAUSE (2nd time): the sibling
  check in stop_managed_engines listed only ports 8889/9889, so a dev
  instance on ANY other port (mine on 9899) killed the desktop's shared
  engines on exit. Now it pgrep's for millenai.py — any sibling process
  spares the engines. Plus a last-resort guarantee: if the whole chat
  pipeline emits ZERO bytes, retry on the smallest cached model and, if
  that is silent too, say so in plain language. A reply is never blank.

## 2.15 — Fable-grade voice
- CALIBRATION over inflation: the "always 2-3x longer" mandate made
  simple questions insufferable. The prompt now matches depth to the ask
  (tight+priced for quick facts, full treatment for meaty ones), demands
  specifics over hedges, and bans closing fluff ("In conclusion", offers
  to help further). One worked micro-example anchors the quick register.
  REVISE + SYNTH calibrate too (complete beats long).
- Turbo upgraded to openai/gpt-oss-120b on Groq (was llama-3.3-70b) —
  found via /models on the configured key; turbo.sh default matches.
- 2.16.1 VRAM-AWARE SIZING: machine_budget_bytes used 75% of SYSTEM RAM,
  which is right for Apple's unified pool and wrong for a discrete GPU —
  a 165 GB PC with a 24 GB 3090 was offered a 120B that would spill to
  CPU and crawl. Now: budget = min(RAM*0.75, VRAM*1.25) whenever
  nvidia-smi reports a card (cached; Mac unaffected). Simulated:
  165GB+3090 -> 30 GB budget, flagship Qwen 35B MoE (fits the card).
- 2.16.2 TURBO PROVIDERS: added Anthropic's native dialect to
  cloud_stream (x-api-key + anthropic-version + /v1/messages +
  content_block_delta SSE; system prompt hoisted out of messages) and
  Google Gemini via its OpenAI-compatible endpoint (needs no new code).
  turbo.sh now offers Groq / Gemini / Claude / xAI / OpenRouter /
  Cloudflare and tests each in the right dialect.
  NOTE: there is no free Claude API — it is paid per token, a Claude.ai
  or Claude Code subscription does NOT grant API access, and proxying
  subscription credentials would breach Anthropic's terms. Gemini's free
  tier is the free frontier-class option.
- 2.17.3 DUPLICATE-ID TRAP (again): #about-card is shared by THREE
  dialogs (settings, update, new-models). The settings restructure moved
  padding into #about-head/#about-body/#about-foot, which the small
  cards don't have — so their buttons ran to the card edge. Scoped
  padding added via #update-veil/#new-veil #about-card. Renaming those
  ids is still the real fix.
- 2.17.5 LIVE DATA: business-hours question got FABRICATED hours + a 555
  phone number (seen live). Three-layer fix: (a) needs_search learns
  local/live-fact triggers (hours, open now, phone number, address,
  menu, showtimes…, plus is/when…open patterns) — over-searching is
  cheap, an invented phone number is not; (b) system prompt bans
  inventing verifiable specifics outright; (c) place-shaped searches get
  the weather treatment — snippets are the ONLY source, unverified means
  say so. Live: same query now cites real sources, flags their
  disagreement, invents nothing.

## 3.1 — place answers + backdrop pool
- PLACE ANSWERS Gemini-shaped: placey searches use run_search_deep
  (snippets + readable text of the top 2 result pages via _page_text —
  the hours live in pages, not blurbs) and a strict ANSWER SHAPE:
  verdict first, <=3 bold-name lines, one heads-up, <=120 words.
- ROOT CAUSE of the fabricated restobar essay: the message started with
  "Hey", and _NO_SEARCH fires on greeting-PREFIXED messages, so search
  never ran. needs_search now strips a leading greeting before judging.
- BACKDROPS: LRU 12 -> 30 (~6.6 GB); 10-min in-session trickle keeps
  warming uncached clips; skyhist (last 8) prevents repeats. The pool IS
  the rotation — it has to be wide.

## 3.3 — place search that actually finds places
- "is ables in bushwick open" -> "I couldn't find any information" at
  0.9 tok/s. Three separate causes, all fixed:
  1. THE ENGINE: default DDG backend returned neighborhood listicles;
     bing found the actual business (an Instagram-only steakhouse). All
     searches now go through _ddg_text(), which tries bing -> auto ->
     duckduckgo — engines rate-limit individually for ~a minute, so one
     strike must never mean "no results".
  2. THE QUERY: the raw conversational prompt was sent verbatim.
     place_search() strips filler (_PLACE_FILLER) to entity+locality
     ("ables bushwick"), runs three variants, and match-checks results:
     a direct hit must contain the anchor AND next term as WHOLE WORDS
     ("pool tables" must not count as "ables"; "Ables" obituaries have
     the name but not the place).
  3. THE SHRUG: matched=False now gets its own answer shape — say you
     can't find it by that name, offer the closest real result, ask one
     pin-down question. Shape is taught by a WORKED EXAMPLE for a
     different query; abstract templates get parroted back literally
     ("**Name** - What is it?" appeared in a live answer).
- INSTRUCTIONS AFTER DATA: the answer-shape prompt now comes AFTER the
  snippets/pages, right before the question. Buried before 4KB of
  scraped page text it was forgotten — the model answered by pasting
  Lucali's entire menu and email-signup form.
- Page fetches parallelized (_fetch_pages): serial 7s timeouts were the
  25s time-to-first-token. Pages are fetched only for MATCHED results —
  reading listicles about the neighborhood is pure latency.
- Smoketest: sign-in copy check was case-sensitive and broke silently
  when the copy got capitalized; ZEBRA-42 check normalized (models emit
  U+2011 non-breaking hyphens). New gauntlet check: unknown place must
  get a helpful no-match answer, not a shrug.

## 3.3.1 — greetings out of queries, phrase-loop guard
- "Yo is abes in bushwick open" produced an answer about a place called
  "Yo is Abe's": needs_search stripped the greeting only for its own
  judgment; the QUERY kept it. strip_greeting() factored out, applied to
  the query itself, and it now PEELS STACKED greetings ("yo yo yo",
  "whats good dawg") in a loop. Slang also backstopped in _PLACE_FILLER.
- _looks_degenerate learned phrase loops: "…which is considered to be X
  since the restaurant is not busy" seven times over sailed under every
  uniqueness ratio because the varied nouns diluted it. New rule: any
  4-gram repeated 6+ times is a collapse.
- place_search page fetches ranked by authority: the place's own domain
  (anchor in host) then yelp/tripadvisor/opentable, then the rest — a
  blog post's stale hours once beat the official site to the fetch slot.
- The Milano's/Ridgewood worked example leaked into a real answer
  verbatim; the prompt now fences it ("belong to the example ONLY") and
  the smoketest asserts the fence holds.

## 3.4 — Gemma takes the Fast slot
- A/B'd Qwen 3.6 35B MoE vs Gemma 4 26B vs Phi-4 14B (facts, trick
  math, noisy-data extraction, hallucination bait): accuracy IDENTICAL,
  so the ladder decision came down to temperament. Qwen's hidden
  thinking mode stalls random turns for 15-19s (the "0.9 tok/s" answer)
  and it produced the phrase-loop slop; Gemma held 1-6s on everything,
  never collapsed, followed shape instructions tighter. Phi-4: chatty,
  ignores "just the line", 21s on facts — disqualified.
- Fast and Thinking ladders now rank Gemma 4 26B above the Qwen MoE.
  NOTE both are MoE (Gemma a4b = 4B active, Qwen A3B = 3B active) —
  the "35B" badge is marketing; these are ~3-4B-activation brains.
  A dense Qwen 3.6 27B might beat both but disk is 99% full (22GB
  free), so it stays undownloaded and untested.
- The closed-day check is MECHANICAL now: code scans the snippets for
  "closed …Tue / Tue… closed" matching today's weekday and, on a hit,
  dictates the exact verdict sentence. Lucali-on-a-Tuesday went from
  right-1-in-3 to right-3-of-3; without the hit, the prompt still pins
  today's weekday ("never name any other weekday as today" — a run
  once said "It's closed tonight, Friday" on a Tuesday).
- No-match shape got hard bookends for Gemma (first sentence = can't
  find it, last sentence = a question) — it liked presenting nearby
  cafes as if they were the answer.

## 3.5 — answers voiced like Claude, and the bug that hid a model swap
- VOICE, in SYSTEM_PROMPT and both rewrite passes: never open with a
  title/heading (first line is a sentence spoken TO the person, in
  their register); meet something personal in one genuine clause; end
  an arranging/building task with momentum (the one or two details
  needed) instead of the blanket no-offers rule; NEVER invent named
  things (businesses, retreats, programs) or dress invention as
  experience ("tried-and-tested") — the burnout-retreat answer invented
  three retreats with prices and cohort dates.
- Instructions alone don't stop a 4-bit model from confabulating —
  GROUNDING does: _BOOKING_RX (arranging verb + bookable noun) routes
  recommendation asks to web search, deep pages included, searching
  only the SENTENCE with the ask (whole-message search surfaced Swiss
  burnout clinics). Fence vocabulary matters: when the injected block
  said "results"/"snippets" the answers said "the snippets show" — it
  now says "what you just found", which echoes back as "I found".
- Searched answers were EXCLUDED from the two-pass polish ('not query')
  — every live-data reply was a single take. Bookish answers now get
  the rewrite, fed the grounded message so the reviser can check names
  against data. REVISE no longer says "add missing specifics"
  unconditionally (that INVITED confabulated dates); adds only what is
  certainly true, cuts suspects.
- ROUTER BUG (the big one): a tier request arrives with model="" and
  the route matcher matches on model_name — empty matched nothing and
  fell through to the smallest-cached fallback. The header said Gemma
  while Llama 3.2 1B answered. The desktop UI masked it by sending
  model=council[0]; the tier API path (and my whole test harness) hit
  it. model_name now defaults to the resolved council leader.
- The disk hit 99% (~00:34), Gemma's engine died, respawns failed
  silently, and the never-empty guarantee served 1B for hours — with
  the router bug making it invisible. Lesson: when a test result
  suddenly looks like a different model, CHECK WHICH ENGINE IS
  LISTENING (lsof the port) before tuning prompts against it.
- Turbo "unavailable": Groq free tier is 200k tokens/DAY and Fast
  burns it — the probe worked (16 tok) while real requests 429'd.
  Gemini free tier is the roomier fallback provider.
- No-match place answers: first + last sentences are now DICTATED by
  code (entity split: last term = locality). Gemma paraphrases the
  first ("I'm not aware of…") — semantically identical, accepted.

## 3.6 — the whole sky, and search you can see
- BACKDROPS, third and final round: the "same 3-4" wasn't the picker's
  width — it was (a) days of disk-full silently failing every warm, and
  (b) 18 of 34 cached files being ORPHANS from old catalog hashes
  (unplayable, invisible, eating gigabytes). Now: every launch picks
  from ALL 89 clips (skyhist last-32 excluded), an uncached pick shows
  the loading bar with real progress ("it makes it feel special" — the
  bar IS the feature, per Patrick, reversing the old instant-start
  rule), the 5-min trickle backfills until the whole catalog (~20 GB)
  is local, and the LRU pass deletes orphans + day-old .dl partials.
- SOURCES ROW: searched answers now carry clickable chips (favicon +
  domain, opens the page) under the "searched the web" badge — the
  graphical proof of the search. Server stashes the structured hits in
  a thread-local (_tl_search, cleared per request — keep-alive reuses
  threads), emits one \x00SOURCES:json\x00 marker; client strips it
  like STATUS/DRAFT, renders srcRow(), persists m.sources so chips
  survive reload. Favicons via google s2 (the page already loads
  Google Fonts; same trust boundary).
- run_search's 60s cache now stores rows too — a cache hit used to
  leave the sources row empty while the text answered from cache.
- Every searched answer gets "Today is %A." pinned in the wrapper — a
  generic search reply opened "Mondays can be challenging" on a
  Tuesday (seen live).

## 3.7 — the front door, the icon, and the home team
- NEW ICON (MillenAI.icns + .ico, generated by scripts/pillow at 1024
  then iconutil): rainbow M with a two-pass glow over a starfield and
  the ISS horizon arc — the app's whole identity in one mark. Old icns
  kept only in that session's scratchpad; regenerate from the design
  code in the transcript if ever needed.
- SIGN-IN: primary = Continue with Google + Continue as guest (new
  /api/guest mints a random cookie-scoped profile, 180 days); the
  name+PIN form lives behind an "I have a name & PIN" reveal — owner
  PIN access unchanged. Copy: "Your AI. Walk right in."
- NYC PRIORITY: SKY_NYC (regex comp_N\d{3}_|NY_NIGHT → the four
  N-series aerials + NY-at-night ISS pass) — half of launches lean NYC;
  NYC clips only dodge the last THREE played, not the 32-deep history,
  or five clips could never resurface.
- Wipe .95s → .62s (same full coverage). Version splash covers the
  WHOLE screen (webview.screens size, type in vw) — still only after
  an update, never on fresh installs.
- Searched answers: off-topic results are invisible — never narrated
  ("Mental Floss mentions Generation Beta" appeared in a burnout
  answer; the reader must never learn what the search returned).

## 3.8 — the bar gets its moment
- BACKDROPS, final form (reverses 3.6's stockpile, per Patrick): pick
  fresh every launch, ALWAYS ride the loading bar, keep only the
  playing clip + its predecessor on disk (4.9 GB cache observed →
  1.4 GB). Trickle, cache-pool pick and the Preload button are gone.
  NYC bias and 32-deep history stay.
- The bar itself: 18px tall, pastel-rainbow fill that shimmers (perf
  mode: static), bordered glowing track, lighter tracking-wide label.
- FIRST RUN: plan cards renamed Basic→Fast (matches the tier), and the
  setup card carries a "Share GPU power" checkbox — ticking it at
  Download arms Contribute and marks seen_share so the later one-time
  invite never re-asks. Anyone who already decided has the row hidden.
- Cloud GPU without signup: doesn't exist legitimately — any keyless
  "free LLM API" is someone's abused proxy. The honest answers are the
  Community GPU fleet (no signup, already built) and Turbo with a
  2-minute free key. Documented here so we stop re-asking.

## 3.8.1 — the payoff line
- When the loading bar finishes a real download, "LFG, BITCH." pops in
  rainbow gradient where the bar stood and wipes itself away in 1.25s
  — fires ONLY after an actual wait (hadBar check), never on instant
  starts, never in performance mode.
- The flat grey band across the bottom was #composer-wrap's opaque
  --bg gradient painting over the video — now a translucent scrim
  (rgba(5,6,10,.62)) so the backdrop runs to the window edge.

## 3.8.2 — borrowers don't manage the host
- The models nudge appeared on a PHONE visiting the tunnel (seen live:
  "shouldn't the mobile app use my laptop's models?") — exactly right:
  a remote visitor borrows the host's models. New IS_LOCAL gate
  (hostname is 127.0.0.1/localhost) turns off, for borrowers: the
  first-run installer, the daily models nudge, the share-GPU invite,
  and the sidebar models-up button. Server-side admin lockdown already
  blocked the actions; now the UI stops offering them.

## 3.9 — the fleet is one toggle
- AUTO-APPROVE (fleet_auto pref, default on): a worker that registers
  gets its token in the same response — the whole community-GPU flow is
  now: flip "Contribute GPU power", done. The knock-and-approve flow
  survives behind fleet_auto=false. "reconnecting" state renamed "hub
  offline — retrying"; the advanced Hub URL field is gone from Settings
  (contrib_url in prefs.json still honored).
- REVISE + ATTACHMENTS: the two-pass reviser saw only the bare prompt
  for doc questions — combined with the anti-invention clause it
  deleted a CORRECT answer as unvouchable ("you haven't attached the
  file", 2x in the gauntlet). Doc-carrying answers now feed the
  reviser the full message, same as searched ones. Triple-verified.
- Debugging note: first suspect was a zombie test worker eating fleet
  jobs — wrong (model mismatch made offload impossible); the 45s
  _fleet_alive window plus model matching already guards that.

## 3.9.2 — answers survive chat switches
- Switching chats mid-answer LOST the response (seen live): loadChat
  swaps the global `messages` array, so the in-flight send() pushed the
  finished answer into whichever chat the user switched TO — and
  loadChat also aborted the stream outright.
- Fix: send() pins its owning chat (myChat/myMessages) at start; every
  completion write (push, pop, persist) targets the pinned chat via
  persistChat(id,msgs). loadChat no longer aborts — the answer streams
  on quietly, lands in its own chat, and if you're back viewing that
  chat when it finishes, it paints in. Auto-scroll only fires when the
  owning chat is on screen, so a background finish never yanks the
  view.
- Verified in-browser with the exact repro: send, switch away
  mid-stream, return — full answer present.

## 3.10 — answers with maps and photos (the Fable treatment)
- A matched place answer now carries: source chips, up to three PHOTOS
  (og:image from the pages the search actually fetched — _page_text
  grew a meta out-param, plumbed through _fetch_pages' threads), and a
  pinned LIVE MAP (OpenStreetMap embed iframe; geocoding via Nominatim
  — keyless, cached, identified UA; "Open in Maps" deep-links Apple
  Maps). Bookish answers get photos too.
- Wire format: \x00PHOTOS:[urls]\x00 and \x00MAP:{lat,lon,name}\x00
  markers alongside SOURCES; persisted per message (m.photos, m.map) so
  history keeps its visuals. Photos render with no-referrer + onerror
  self-removal (hotlink-hostile CDNs just disappear quietly).
- Verified live: Lucali → closed-Tuesday verdict, lucali.com/yelp
  chips, the shop's own two photos, and a Henry Street pin on the map.

## 3.10.1 — greetings, full NYC
- The hero greetings rewritten NYC-majority: bodega warmth, subway
  pace ("Bodega's open. What do you need?", "In a New York minute —
  go."). Purged per Patrick: "On God" (no church), "Let's ship
  something" / "move the needle" / "whiteboard" (no startup-speak).
  A plain-spoken handful stays for balance.

## 3.10.2 — the boot wash
- "LFG, BITCH." is a boot ritual now: once per launch, ~2.3s after the
  rainbow wipe starts (right as the wordmark and version settle), it
  washes across the hero — in from the left on a skew, a beat over
  center, out the right, 2.2s total. Sits at top:64% (it originally
  rode the greeting line at 47% and both went muddy — frozen-frame
  check caught it). The loading-bar payoff pop still fires separately
  after real downloads. Perf mode skips both.

## 3.10.3 — one LFG only
- The loading-bar payoff pop is gone; the boot wash is the single
  "LFG, BITCH." moment per launch. lfgPop keyframes retired with it.

## 3.11 — lighter idle, guest passes, more bodega
- PERFORMANCE (no feature lost, per Patrick — "gobbling up my m4 pro"):
  the two always-on rAF loops are gone. Parallax now runs ONLY while
  easing toward a fresh mouse target (was 60-120Hz forever, mouse still
  or not); the wordmark chameleon moved from rAF to a 1.5s clock (its
  probe was 6s-gated anyway). Telemetry polls at 2s (was 1s). A hidden
  window now pauses the 2K video and stops all polling — everything
  resumes on visibilitychange. Idle CPU/GPU drops to near-zero in the
  background; on-screen behavior is pixel-identical.
- GUEST PASSES are temporary now: 24h cookie (was 180d), profile dir
  marked with .guest at creation, and the mlx janitor sweeps marked
  profiles untouched for a week (every ~6h). Sign-in copy says so.
- +25 NYC greetings (bodega-core, transit pain, street wisdom, pure
  attitude — "Showtime. What time is it? SHOWTIME.").

## 3.11.1 — instant city for borrowers, snugger header
- WEB BACKDROP was a black void (seen live in incognito): a tunnel
  visitor's blind pick meant a 250 MB server download + tunnel stream
  before anything showed. Borrowers now pick from the host's CACHED
  clips — instant playback, no ritual; the fresh-pick ceremony stays
  local-only. Blind pick only if the cache is somehow empty.
- Sidebar top consolidated Claude-snug: brand-wrap 12→5px bottom pad,
  mode-tabs margins 12/8→5/6, tab pads 7→5px.

## 3.12 — the standby city, and Claude's chat
- BACKDROPS never blank now: while the fresh pick downloads behind the
  bar, a cached clip plays UNDERNEATH — when the new clip is ready the
  city dips to 22% opacity, swaps src, and fades back up. Progress bar
  + variety + zero wait, all three at once.
- CLAUDE-STYLE CHAT: user messages are compact right-aligned pills (no
  "YOU" label); answers are flat serif prose (ui-serif/Georgia 16.5px)
  straight on the backdrop. Code/pre stay mono inside the serif flow.

## 3.13 — the Fable lever
- BEST TIER: always answers from the configured frontier cloud (the
  Turbo config — Gemini free tier is the roomy default) with the model
  chip naming the provider; falls back to the Fast ladder offline. The
  turbo pref now governs Fast only. Honest architecture: local silicon
  is the floor, frontier cloud is the ceiling, the user picks per query.
- FOLLOW-UP THREADING: "what about tomorrow?" / "do they take
  reservations?" inherit the entity from the last searched turn
  (_thread_terms scans user history; _entity_thin spots queries that
  name nothing — "about" had to join _PLACE_FILLER or "what about
  tomorrow" searched for a BOOK by that name, seen in test). Verified
  three turns deep on Lucali.
- Facts credit their source in-line ("per their website") — the
  attribution rule showed up unprompted in the reservations answer.
- QUALITY LEDGER: app_dir()/quality.jsonl gets one line per answer
  (tier, model, searched, chars) — "make it better" gets numbers.

## 4.0 — the sexy-clean pass
- One design language, per Patrick ("crazy sexy UI... not just vfx but
  cleanliness"): a single glass recipe (rgba(13-15,15-17,20-23) + 26px
  blur + hairline rgba(255,255,255,.07-.13) + 1px inner top highlight)
  unifies sidebar, composer and telemetry. Light does the work borders
  used to do: chat rows are borderless quiet text with soft light-fill
  hover/active; the active mode tab is a bright light pill (dark text)
  — the one pop of contrast in the chrome.
- The composer is the jewel: 24px radius, deep drop shadow, calm
  4px-halo focus ring. Micro-motion: buttons compress (scale .94).
  Scrollbars are 6px glass. Hero greeting wraps balanced. The who
  labels whisper; the rainbow stays exclusive to wordmark/hero/wash.

## 4.1 — the places module (answers like Claude's)
- Place/recommendation answers now end with a machine-read [[PLACES]]
  JSON trailer (max 4 real venues; the client strips it from display).
  The client renders a MODULE: dark multi-pin Leaflet map (CARTO dark
  tiles + OSM, keyless) over a card rail (name, descriptor, hours).
  Pins geocode through the new /api/geo proxy (shared Nominatim cache,
  no CORS). Persisted per message (m.places/m.loc).
- LESSONS: (a) the two-pass reviser DELETED the trailer as filler —
  REVISE_INSTRUCTION now preserves a trailing [[PLACES]] line exactly;
  (b) "pizza spots" wasn't bookish — the noun list gained spots/places/
  joints/shops/diners/delis/bakeries/pizzerias/venues/bodegas;
  (c) geocode sanity: "food bushwick" once pinned EDINBURGH — a pin
  only counts when the result name contains the locality (both the
  server MAP pin and the client module pins).
- The backdrop loading bar can no longer paint over an answer: every
  bar-show site is gated on hero-present + not-generating (plus a
  body.gen CSS kill switch).

## 4.2 — free cloud (honest version), sliding tabs, softer boot
- FREE CLOUD, the truth: scraping Gemini/Claude web UIs is out (their
  terms, and dead-in-a-week endpoints). What exists legitimately:
  pollinations.ai's ANONYMOUS tier (gpt-oss-20b, keyless, built for
  this). Measured behavior: answers for a while, then 402s everything —
  so it's wired as an opportunistic BONUS: Best tier (and keyless
  turbo) tries it with a 15s cap; one failure buys an hour of cooldown;
  never taxes the latency when it's down. Streaming SSE 402s on the
  anonymous tier (measured) — take the whole answer, emit in slices.
- The real "no effort, better answers" path: /api/cloud/set + a
  Settings panel — pick Gemini/Groq/Claude, paste a key, it live-tests
  before saving (0600), arms turbo. Owner-at-machine only. turbo.sh
  still works; nobody needs it now.
- AI|AGENTS is a real segmented control: one lit pill (#tab-glide)
  SLIDES between tabs on a spring curve, Claude-style, labels cross-
  fade. Grouped track, hairline border.
- Backdrops FADE in on every source change (.swapping opacity ramp) —
  boot, standby crossfade, error re-warm — never a hard cut.
- "LFG, BITCH." → "LET'S FUCKING GO." — and after an update, the line
  lives INSIDE the version splash (rainbow gradient, rises at 1.35s);
  the boot wash skips that launch (__SPLASH_LFG__ flag) so it never
  says it twice.
- Web UI gets everything (same file serves both); cloud-key panel is
  IS_LOCAL-gated like the rest of model management.

## 4.3 — "hub offline" fixed, and the map is guaranteed
- THE HUB BUG: the contribute loop's POSTs carried a bare
  "Python-urllib" User-Agent, which the edge 403s — every knock failed
  and Settings read "hub offline — retrying" forever. curl worked;
  we didn't. Same fingerprint that bit us with Groq in 3.x. Fixed by
  sending a real UA; register+poll verified against the live hub.
- "whats a good bar in bushwick" NEVER SEARCHED (no verb for
  _BOOKING_RX) so the model invented three bars from memory (seen
  live). New _ASKY_RX: a quality word (good/best/great/top/worth/
  hidden gem…) plus a place noun is a recommendation ask too. It feeds
  needs_search AND the bookish path, so those answers get grounded,
  deep-searched, photographed and mapped.
- THE MODULE NO LONGER DEPENDS ON MODEL COMPLIANCE. Measured: the
  [[PLACES]] trailer appears maybe half the time, and some answers
  carry no bold spans either — so both the trailer and text-mining
  fail silently. Now a short EXTRACTION PASS runs after the answer on
  the already-resident model ("list the venues this text recommends,
  JSON only"), verifies each name appears in the answer, and emits
  PLACES2. Live: "good bar in bushwick" → The Cobra Club, duckduck,
  House of Yes, Old Stanley's, pinned on the dark map.
  NOTE: use the RESIDENT model, never the smallest — reaching for the
  1B swaps engines and evicts the model that just answered.
- Settings: fleet status block and the button grid get real spacing.

## 4.2.2 — the header download strip
- Background model downloads get a whisper-thin progress strip in the
  sidebar header (under the wordmark, above the AI|Agents slider):
  pastel shimmer fill, "models · 47% · 38 MB/s" mono label, click
  opens the full setup panel. Polls /api/setup every 4s, skips ticks
  while the window is hidden (and corrects itself on visibilitychange
  the instant it's back), shows ONLY when a download runs with the
  setup veil closed.

## 4.2.3 — product type scale
- The 5.0 direction ("total claude replacement, not a backyard
  project") starts with type discipline: serif answers 16.5→15.5px at
  1.62 leading, base body 14.5, sidebar rows 12.5, composer 14.5,
  message gap 26→20, meta at 10px/.85. Same look, product rhythm.
- The backdrop bar could linger over a freshly opened chat: the gates
  only prevented SHOWING it, nothing hid an already-visible bar when
  the hero left. Every tick now corrects visibility both ways, and
  addMsg force-hides it.

## 5.0 — the "it's a real app" release
Five gaps that read as backyard-project, all closed:
- CHAT ORGANIZATION: day grouping (Pinned / Today / Yesterday / This
  week / This month / Older), pin-to-top, dblclick rename in place,
  and delete with a 6s UNDO toast that restores the chat at its old
  index (and reopens it if it was the current one). All fields ride
  the existing chat store, so they persist without schema work.
- COMMAND PALETTE (⌘K): fuzzy over chat TITLES and MESSAGE BODIES —
  searching "cobra" surfaces the chat plus the surrounding sentence —
  plus actions (new chat, settings, model updates, perf toggle, switch
  to any tier). Tier names read from the rendered rows, one source of
  truth. Arrows navigate, Enter opens, Esc closes.
- MESSAGE ACTIONS: hover row under every message — Copy (with a green
  tick), Try again on answers (drops the answer, re-asks), Edit &
  resend on questions (rewinds the thread, loads the text). 
- KEYBOARD: ⌘K palette, ⌘N new chat, Esc stops generation / closes the
  top modal, ↑ on an empty composer recalls the last message, "/"
  focuses the composer.
- HUMAN FAILURE: "The engine returned nothing. Is the model server for
  X actually running?" became "That answer didn't come through — the
  model was still warming up. Try again and it usually lands." with an
  actual Try again button under it. The meta line now carries a
  WHERE badge — THIS MAC / CLOUD / A FRIEND'S GPU.
- NOTE: the Browser pane swallows real ⌘K before the page sees it —
  the handler is fine (verified by dispatching the event); test with a
  synthetic KeyboardEvent, not a real keypress.

## 5.1 — full send
- ONE BACKDROP PER LAUNCH: the standby-then-swap (cached clip playing
  while the real pick downloaded, then flipping) read as the app
  changing its mind — gone. The picker now leans 60% toward clips
  already on disk so most launches are instant, and when it does
  download, the bar waits for the ONE chosen clip.
- LIVE ACTIVITY TREE: STEP markers stream from the real pipeline
  (searched N sources / read pages / located on map / drafting /
  sharpening / finding places) into a Claude-style panel with a
  shimmer progress bar; it collapses to "› N steps · done" and
  re-expands on click. The tree DOUBLES AS A LIE DETECTOR: "best pizza
  in williamsburg" showed only 2 steps — no search — because "pizza"
  wasn't a trigger noun, and the memory-answer had put pizza on
  Lilia's menu. Food nouns (pizza, tacos, coffee, ramen…) now count.
- SETTINGS REBUILT: PERSONALITY / POWER / MAINTENANCE sections with
  micro-headers, the cloud-key card gridded so nothing truncates,
  maintenance as a full-width stacked list, pinned Close.
- WORKSPACE (the Claude-Code seed): owner-only, read-only. Point it at
  a folder (/api/workspace/set), the Workspace agent ranks files
  against the question and pastes the best windows under the prompt.
  Window anchor = the RAREST matching word — anchoring on the earliest
  hit put the window at the top of the file where "file" and
  "function" live (seen live). Verified: explained place_search from
  millenai.py accurately, citing the file.

## 5.2 — the drop
- THE DROP: the boot LFG line is dead-center of the WINDOW both axes
  (was hero-area, top:64%, offset by the sidebar). Letters slam in one
  by one — per-char spans, each carrying its own two-stop slice of the
  palette, staggered 38ms — because animating children under a parent
  background-clip:text repaints unreliably; per-char gradients are the
  workaround. An aurora conic bloom breathes behind (::before), an
  elliptical ring shockwave detonates at ~0.95s (::after), 16 sparks
  eject, and the exit pulls THROUGH the camera (scale+blur+fade), not
  off to the side. Gauntlet gotcha: the JS flag `lfgWashed` contains
  the substring "lfgWash" — assert on "keyframes lfgWash{", not the
  bare name.
- PREPARED CITY: after the backdrop reveals (+9s), the client warms
  ONE different clip (same NYC bias — the prepared clip IS tomorrow's
  pick) and records it in millen.skynext only once READY. Next launch
  short-circuits the picker to it: instant start, no bar, never a
  flip. Server unchanged: _send_sky already touches mtime on serve, so
  the keep-two LRU holds exactly {playing, prepared}. Borrowers never
  prefetch (IS_LOCAL gate) — web visitors must not grow the disk.
- CODE IS A TAB: AI | Code | Agents. The Code tab owns Coding +
  Workspace (CODE_AGENTS); Agents keeps the rest. Opening Code
  activates the last-used code specialist (millen.codeagent) on the
  spot; leaving it drops back to Standard so the chip never says
  "Coding" under the AI tab. The glide pill generalizes to thirds:
  width calc(33.334% - 2px), translateX(100%/200%) — %-transforms are
  relative to the pill's own width, so no container math.
- PINWHEEL: ✱ spinning the identity gradient (background-clip:text +
  rotate) sits left of the activity-tree bar (.wthead) and replaces ◇
  in the statusline. perf mode stills it.
- ICON: the old artwork painted its tile edge-to-edge on the 1024
  canvas; modern macOS shrinks non-conforming icons into the system
  squircle — THAT's why it read smaller than neighbours. New icon
  (make_icon.py) draws on the real Apple grid: 824×824 squircle,
  r=185, margins 100 — plus glowing rainbow M (Condensed Black, 66%),
  starfield, aurora, amber horizon, rim light. Same art → MillenAI.ico.
- LATENT BUG FIXED: setup_status had the ONE bare psutil call in the
  file — /api/setup died (and the header download strip with it) on
  any python without psutil. Found because the bare Homebrew 3.14 test
  instance also lacks ddgs → HAS_SEARCH=False → "no search step" red
  herring. Test instances must run on the app venv:
  ~/Library/Application Support/MillenAI/venv/bin/python3.

## 5.3 — housekeeping with teeth
- THE DEAD BUTTON was a missing </div>: the 5.1 Settings rebuild never
  closed #about-veil, so the PARSER adopted every veil below it
  (#dlhelp, #share, #setup) as children of the hidden modal —
  position:fixed inside a display:none ancestor renders at 0x0, so
  openSetup() "ran" invisibly. Computed style looked perfect
  (display:flex, opacity:1); only getBoundingClientRect told the
  truth. When a fixed overlay opens at 0x0, count your closing tags.
- TIERS: Best removed (without a cloud key it WAS Fast — same ladder,
  same answer); Power removed, Pro absorbed it whole: all:True,
  count:99, peer review on, and the merge pass now prefers the LARGEST
  Gemma 4 that fits (26B before 12B — the old order quietly picked the
  small one on big machines). Old clients aliased server- AND
  client-side: Smart→Fast, Best→Fast, Power→Pro.
- SETTINGS: MAINTENANCE header gone, the three rows compressed
  (7px 12px, 5px gap). Header wordmark switched to the hero's Space
  Grotesk (tracking -.012em), greys untouched; the version keeps mono.
- METERS: t-head 11px, labels 10.5px with align-items:center +
  min-height so the MODELS caption sits centered against the ↑ chip
  (it hung off baseline before), card padding tightened.
- ICON: greyscale — brushed-silver M on charcoal, faint stars, quiet
  glow. Same Apple-grid envelope as 5.2 (that part was right); the
  rainbow was the problem, not the size.

## 5.3.1 — the pantry
- BACKDROP CACHING, THIRD TRY (per Patrick: "no background, or takes
  forever, or super slow"): the 3.8 no-stockpile rule is rescinded.
  The server now keeps up to 8 clips (~2 GB ceiling, LRU on mtime
  which serving touches). After the backdrop reveals, fillPantry
  stocks the shelf one clip at a time until 5 spares sit on disk,
  NYC-biased, skipping recent history and clips that errored this
  session. The boot picker is DISK FIRST, ALWAYS: fresh-on-disk from
  the biased pool, else any cached clip that isn't last night's —
  the download bar is a true-first-run experience only. skynext stays
  primed so the next pick is decided before the app closes.
- ICON: reverted to the About-panel bar-chart mark by ask — four
  rounded bars sweeping #8b5cf6→#7d8fff→#4cc9e0 with the teal dot,
  charcoal tile, Apple-grid envelope kept from 5.2. Bars drawn 2x and
  LANCZOS-downsampled because PIL has no antialiasing.

## 5.3.2 — lanes
- THE SIDEBAR FOLLOWS THE TAB (like Claude): every chat is born with a
  lane — the tab it started on (ai/code/agents) — and renderChats shows
  the active lane only. Legacy records without a lane read as "ai" and
  live under Chat. ⌘K still reaches everything; opening a chat from
  another lane hops the tab (and its agent) along via switchLane, so
  the sidebar context always matches the screen. Empty lanes say "No
  code chats yet" instead of sitting blank.
- AI is now CHAT, and all three tabs carry 12px inline stroke icons
  (bubble / </> / spark), flexed with a 6px gap.
- TDZ BIT TWICE: setTier(tier) runs at boot and reaches modeShow. A
  `let uiMode` declared next to modeShow crashed the ENTIRE boot script
  (empty sidebar, dead app) — it lives in the early state block with
  engineState, which exists for exactly this. And renderChats() called
  synchronously from modeShow hit the same wall via `let chats` below —
  it's a setTimeout(,0) now. The console errors that follow such an
  abort (simGpu, agentsWrap) are downstream noise of the one real
  crash, and stale entries persist across reloads — timestamp a marker
  before trusting them.
- Dev preview launcher moved to .claude/run_backend.py — the session
  scratchpad gets wiped between sessions and silently took the old
  launcher (and launch.json's target) with it.

## 5.3.3 — the seam
- THE "WEIRD EDGE": the boot reveal drives THREE masked layers
  (#sky-color, #hero h1::after, and the blurred .halo span) by sliding
  a 114° gradient mask. A stalled slide — occluded window, throttled
  frame, cancelled transition — strands a mask mid-screen, and the
  HALO's stranded edge (blur 19px + saturate 1.55) reads as a
  permanent teal glowing seam beside the wordmark. Diagnosed by
  elimination: steady-state mask-position computes to 0 (seamless),
  the warp canvas is retired and cleared, and a forced mid-flight
  backdrop mask fades the WRONG way (bright-left) with a far softer
  ramp than the artifact.
- FIX SHAPE, not symptom: masks now exist only during the show. The
  6.4s wipe cleanup adds body.paintdone, which sets mask-image:none
  !important on all three layers — steady state carries ZERO mask, so
  there is nothing left to strand, whatever WebKit does to a
  transition mid-flight.

## 5.3.4 — the seam, actually
- 5.3.3's mask teardown was CORRECT HARDENING BUT THE WRONG CULPRIT —
  the seam survived it (verified against the live 5.3.3 app: all three
  masks computed to none, edge still present in the render). The real
  cause: WebKit rasterizes a filtered element into a layer sized to
  its BOX and CLIPS the blur output there. The wordmark halo
  (blur 19px, saturate 1.55) is exactly the h1's text box — measured
  identical rects — so the bloom terminated in a hard vertical line
  ~40-60px beside the M. The "seam colour" was the glow itself: teal
  over the night clip, amber over the sunset clip.
- DIAGNOSIS THAT WORKED: amplify the suspect (blur 30 / brightness
  2.2) and screenshot — the rectangular clip became unmissable. Column
  -mean pixel scans had already cleared the video (no coherent edge in
  the footage) and elementsFromPoint cleared the overlay stack.
- FIX: the classic filter-clip workaround — padding:130px;
  margin:-130px on .halo. The raster bounds grow 130px past the text,
  the blur fades to nothing well inside them, and the negative margin
  keeps alignment (span rect verified unmoved). Amplified re-test:
  smooth falloff on every side, no straight edges.

## 5.3.5 — the seam, third form, and the rolling shelf
- THE SEAM SURVIVED 5.3.4 in the app while the Chromium pane verified
  clean — because the pane is BLINK and the app is WKWEBVIEW. The
  padded-wrapper workaround that satisfies Blink turned the artifact
  into a crisper rainbow sliver in WebKit (ancestor filter +
  background-clip:text misrender). LESSON, in caps: A FIX FOR A
  RENDERING BUG MUST BE VERIFIED ON THE ENGINE THAT SHOWS IT — the
  desktop app is Safari's engine, the preview pane is Chrome's.
- FINAL FORM: the halo is a CANVAS. haloTick (400ms, hero-only,
  skips perf/hidden) redraws "MillenAI" with the travelling 16s
  rainbow phase and blurs AT DRAW TIME via ctx.filter — the pixels
  arrive pre-blurred, so no engine compositor ever gets a chance to
  clip them. Measured: max per-pixel alpha step across the glow is
  4/255 — smoothness by construction. haloCap() probes that
  ctx.filter actually spreads ink (a no-op filter would paint SHARP
  text behind the wordmark); unsupported engines get no halo rather
  than a wrong one. The DOM .halo stays in the markup (the gauntlet
  and wipe classes reference it) but is display:none.
- ROLLING SHELF (per Patrick: "randomize as much as possible… not
  100gb"): fillPantry now sets millen.skynext IMMEDIATELY (favoring
  never-seen spares), and even with full shelves streams ONE fresh
  never-seen clip per session — the keep-8 LRU evicts the oldest, so
  disk stays ~2 GB while the catalog cycles. When the fresh clip
  lands it TAKES OVER skynext: most launches open on footage the
  user has literally never seen, downloaded invisibly the session
  before. True stream-on-first-play is impossible with Apple's
  sources: moov sits at the END of the file (hence _faststart), so
  nothing can play until the last byte arrives — rotation is the
  honest fix.
- Browser-pane gotcha: document.hidden is TRUE in the pane even when
  the page renders — anything gated on it (haloTick, the chameleon)
  looks dead there. Override the getter to test.

## 5.3.6 — the amnesiac window
- WHY THE BACKDROP NEVER ROTATED despite a working pantry: pywebview
  defaults to private_mode=True, and its cocoa backend implements that
  by ERASING ALL WEBSITE DATA from the default WKWebsiteDataStore at
  every window creation (cocoa.py: removeDataOfTypes_ since epoch).
  Every app launch wiped localStorage: millen.skynext (the prepared
  clip), millen.skyhist (rotation memory) and millen.sky all vanished,
  so each boot ran as a FIRST RUN — and the first-run courtesy
  restricts picks to the dark set. Result: the same space/earth clips
  forever, while fresh clips downloaded dutifully next to them.
  Fix: webview.start(private_mode=False, storage_path=app_dir()/webkit)
  — on cocoa the storage_path is ignored and persistence simply means
  "don't wipe the default store". Verified in pywebview's source, not
  the browser pane (the pane can't run WKWebView).
- COLLATERAL HEALED: every localStorage pref was silently resetting
  each launch on desktop all along — performance mode, last code
  agent, tier choice. They stick now.
- BELT + SUSPENDERS: firstEver is now also false whenever the disk
  already holds 2+ clips — a stocked pantry is proof of a veteran
  install even if storage ever gets wiped again, so the dark-set
  first-run preference can never re-trap the picker.

## 6.0 — Concorde
- THE REBRAND: MillenAI is Concorde everywhere a user looks — wordmark,
  window, tab, splash, sign-in, gate, DMG, MSI, shortcuts, README.
  One APP_NAME constant + brand() applied at the three HTML serve
  points (index, WELCOME_PAGE, GATE_PAGE); "Concorde" is 8 characters
  like "MillenAI", so every wordmark metric survived untouched.
- WHAT DELIBERATELY KEEPS THE OLD NAME (the rename-safety spine):
  app_dir()/venv paths (data continuity), CFBundleIdentifier
  com.millen.millenai (WebKit keys storage to bundle identity — the
  5.3.6 persistence win dies if this changes), CFBundleExecutable
  MillenAI (_SWAP_SCRIPT pgreps ".../MacOS/MillenAI"), MillenAI.icns/
  .ico filenames, UPDATE_REPO bigmillz/MillenAI, User-Agents, the
  Windows INSTALLDIR + registry key, and the MSI UpgradeCode (change
  it and upgrades stop replacing the old install).
- UPDATE CHAIN VERIFIED SAFE BY READING, NOT HOPE: the updater picks
  release assets by .dmg EXTENSION (never name), the swap script
  globs "$MP"/*.app and renames it onto the EXISTING bundle path, so
  a MillenAI.app updating from a Concorde DMG stays at its old path
  with the new app inside. Existing installs cross the rename without
  knowing it happened.
- brand() is a GLOBAL replace on served HTML — before shipping,
  grep the page for URLs containing the repo name (a link to
  bigmillz/MillenAI would be rewritten into a 404). Zero today.

## 6.1 — chrome
- THE LOOK (per Patrick: "greyscale… techno… not bland, not a visual
  shitshow"): every rainbow became THE SILVER RAMP (9/7/5-stop
  greyscale loops with first==last so the shimmer animations keep
  cycling) — wordmark, canvas halo, LFG drop, celebrate sweep, all
  progress shimmers, pinwheel, splash, About mark. Violet glow tints
  went neutral chrome. KEPT COLOURED on purpose: the backdrops (the
  cinema), content (maps/photos), the red error accent, and the
  red/blue chromatic-aberration flash in the letter slam — that
  glitch accent is the "still fun".
- THE FACE: nailfairy.art loads pragmatica-extended via Adobe Fonts
  (plus ibm-plex-mono — already ours). Pragmatica is licence-locked;
  Michroma is the free wide-techno stand-in. New --disp var on
  display surfaces only: hero h1, .vghost, #lfg, splash. Wide faces
  run ~1.4x — sizes stepped down (hero 132px -> clamp 8.2vw,
  vghost 22 -> 16.5) and tracking flipped positive. Michroma has ONE
  weight: bold requests would synthesize, so weights are pinned 400.
  Canvas halo font string must match the h1 face by hand — it
  measures and draws text itself. The splash window is self-contained
  and needed its own Google Fonts link or it falls back silently.
- ICON: bars now TOUCH (step == width) and BLEED — drawn overlong and
  cropped flush by the squircle mask at composite. Silver ramp,
  brightest at the diagonal. Body copy and answers keep their faces —
  readability is not a mood.

## 6.0 beta 2 — darker, hero-less
- NO IN-APP HERO BRANDING (per Patrick: "claude doesn't even have
  branding in the app"): the giant wordmark + beta-tag left the hero;
  the serif greeting stands alone over the backdrop. The canvas halo
  and h1 gradient machinery are dead code now (haloTick self-cleans
  when no h1 exists) — left in place, cheap and inert. Gauntlet
  gotcha: assert on class="h1row" absence, not the substring — the
  dead CSS selector keeps the bare string in the page.
- FRAME-WIDE WORDMARK: CONCORDE spans the sidebar edge to edge in
  Michroma caps (the NAIL FAIRY treatment) and SCALES with the
  sidebar via font-size:calc(var(--sbw)*.105). Version + controls
  moved to a slim row beneath (.vsub).
- DARKER: base tokens dropped ~8 shades (--bg #212121 -> #101013,
  panels/lines to match), the glass recipe's ground went from
  rgba(13,15,20,a) to rgba(6,7,10,a) everywhere in one replace, and
  the native window ground matches (#0a0a0c).
- STILL 6.0.0 BETA: released as v200 PRERELEASE via the APP_BETA
  path — fleet stays parked on v197; the live instance (raw tags)
  picks the beta up for remote kink-hunting.

## 6.0 beta 3 — the box and the cubes
- CLAUDE-STYLE EMPTY STATE: the composer floats mid-panel under the
  greeting, IN FLOW (a pinned top-% collided with two-line greetings,
  seen live) — #main:has(#hero) flips chat-scroll to auto-height and
  the wrap to static; with a chat open the same DOM docks back to the
  bottom untouched. The engine chip moved INSIDE the box (#crow:
  pill left, actions right) and clicking it opens the sidebar tier
  picker — with stopPropagation, because the document-level
  dropdown-closer re-adds "closed" on any outside click and undid the
  open in the same tick (caught live).
- THE CUBE WAVE replaces the chrome sweep (per Patrick: "dark techno
  party… not chrome chevrolet", after Claude Code's dithered meter):
  a canvas grid of quantized grey cells swept by one diagonal front —
  dark rumble ahead, strobing decay behind, rare white pings. Sized
  LAZILY because the viewport can measure 0 at boot. Verified by
  pixel audit (888/1280 mid-row cells lit at t=0.5, zero colored);
  the pane throttles rAF when document.hidden, so the loop needs a
  setTimeout-shimmed rAF to test there — CSS animations run in the
  pane, rAF loops do NOT.
- Old .sweep CSS stays (inert); downloads-complete celebration uses
  the cube wave too via the shared rainbowWipe path.

## 6.0 beta 4 — corner mark + the beta channel
- WORDMARK: frame-wide lasted one beta — now a gpt/gemini-style corner
  mark (Michroma 12.5px, .18em tracking) inline with the version and
  controls. The frame-wide look moved to NOTES history.
- BETA CHANNEL, THE REAL ONE: Settings grew "Beta updates — new
  builds first, kinks included" above the maintenance stack (styled
  with the checkbox family). Server: _channel_release() — stable
  reads /releases/latest (GitHub excludes prereleases), beta opt-in
  lists releases and takes the newest non-draft. Verified live on
  /api/update/check: unchecked -> 5.3.7 (v197); checked -> 6.0 beta
  (v201). Toggling ON immediately re-runs the update check so a
  waiting beta surfaces at once. download_links() (the DOWNLOAD NOW
  chip for web guests) deliberately stays stable-only.
- NB the test instance SHARES prefs.json with the desktop app —
  toggling prefs in tests must reset them (done here), or the
  desktop quietly changes channels.

## 6.0 beta 5 — settings truthfulness
- THE MISSING CHECKBOX WASN'T MISSING: beta 4's /Applications patch
  never ran — the && chain died at release.sh's TLS timeout and took
  the cp with it, while the summary still said "app patched". RULE:
  the app patch is its OWN command with its own grep-verification,
  never the tail of a release chain.
- Beta row moved to the TOP of Settings (first set-sec, above
  Personality) with the running version baked in ("you're on 6.0
  beta") — discoverable without scrolling past the cloud card.
- FOLDING POWER: the fleet box hides when Contribute is unchecked;
  the frontier-cloud key card hides when Use cloud power is
  unchecked; both restore on re-check (verified with dispatched
  change events both directions) and populate folded/open from prefs
  when Settings opens.

## 6.0 beta 6 — version says which beta
- short_version() carries the BUILD in beta: "6.0 beta 203" — window
  title, tab, About header, splash, corner vsub all agree, and each
  beta release visibly increments. No derived "beta N" counting; the
  build number IS the beta number.
- The opt-in checkbox settled under "Check for updates" (adv-grid:
  updates → check → Include Beta Releases → forget), label shortened
  to exactly that. Top-of-settings placement lasted one beta —
  betas are for finding this out.

## 6.0 beta 205 — slim rail, engine menu, Hermes
- SIDEBAR defaults 384 -> 300px (was ~30% of the window); dblclick
  reset and the --sbw fallback follow. SB_MIN 210 still governs.
- ENGINE MENU: clicking the composer's "engine" pill drops a glass
  card RIGHT THERE — emoji + name + desc per tier (TIER_META token),
  hover reuses showTierPop so the bubble lists the actual resolved
  models, click picks. Positions below the chip on the empty state,
  above when docked. The document dropdown-closer learned about it.
  The old behavior (chip opened the SIDEBAR rows) is gone.
- HERMES, the infamous one: first-class agent (🪽, first among the
  specialists), picks Hermes 3 8B first. The system prompt sets TONE
  not permissions — direct, opinionated, no disclaimers, refuses in
  one sentence when it must. Verified live: "is a hot dog a
  sandwich" -> flat "No," one argument, zero hedging, on Hermes 3 8B.
- AGENT POPUPS: hovering any specialist row shows a tierpop-style
  card (icon, desc, top picks) from the AGENT_META token — the
  "popup description" ask, and it covers every agent, not just
  Hermes.

## 6.0 beta 206 — answers that look like Claude's
- THE ASK (per Patrick, with a side-by-side): "diagrams and code etc
  in different font/color/typeface". Three layers shipped:
  1. RENDERER — flow fences become REAL diagrams: 'A -> B' edges with
     optional '(note)' per node, layered by longest-path topology,
     glass boxes + SVG bezier wires with arrowheads (wireFlow runs
     post-layout and on resize; edges URI-encoded in a data attribute
     because esc() leaves double quotes alone and the JSON truncated
     the attribute at its first quote — seen live). Code fences became
     language-labeled CARDS with a four-class mini-highlighter
     (keywords/strings/comments/numbers, input pre-escaped). Ordered
     lists and setext headers (text over -----, which small models
     love and which rendered as stray hr's) now parse.
  2. CSS — inline code went warm (#e8a08f) against the serif, token
     colors are quiet blues/greens/golds, code cards get a mono
     language bar.
  3. PROMPT — SYSTEM_PROMPT teaches the flow syntax and demands
     language-tagged fences; Gemma-class models follow it, the
     smallest ones won't always. The renderer is verified against the
     reference; model ADOPTION varies by model — the remaining kink.
- Pane gotchas again: rAF throttling means wireFlow needed manual
  driving to verify there. Also: never put backticks in a git commit
  -m double-quoted string — command substitution eats the chain.

## 6.0 beta 207 — the stand, the gear, the type
- BRAND: the icon's silver diagonal bars (no tile) lean against the
  first C like a stand — inline SVG with the icon's gradient, 15px,
  2px off the C. Gear moved OUT of the brand row to the far right of
  the Performance mode line (#settings became a flex row; the gear
  keeps its own click handler so it opens Settings, not the toggle).
  New chat stays beside the version.
- TYPE: answers left the serif. Precise sans stack (-apple-system /
  SF Pro Text first), 14.75px / 1.7 / -.006em — the "line gaps"
  complaint was Georgia's uneven vertical rhythm; SF's metrics read
  like Claude Code. Serif remains only in the hero greeting (the
  charm) — code/tables/chips keep their own faces.
- SHELL LESSON No. 2 this beta line: a bare "cat >> file" with no
  input hangs reading stdin — it ate a 10-minute timeout between the
  gauntlet and the release.

## 6 beta 208 — version numbers that say something
- UPDATE OFFERS name both beta builds: check_update appends the tag's
  build when the release title ends in "beta" ("6 beta 208"), and
  "current" ships as short_version() — so the dialog reads
  "6 beta 208 • you have 6 beta 207", never "6.0.0 to 6.0.0".
- TRAILING-ZERO TRUNCATION everywhere a version is DISPLAYED:
  short_version() loops off .0s — 6.0.0 -> 6, 6.1.0 -> 6.1, 6.1.1
  untouched. Artifacts/compare paths still use APP_VERSION raw.
  The same truncation applies to the numeric part of release titles
  in the offer.

## 6 beta 209 — agents pulled
- THE AGENTS TAB AND SPECIALIST LIST ARE GONE (per Patrick: "until i
  get the logistics of that sorted"). Two tabs again — Chat | Code —
  glide back to halves. DORMANT, NOT DELETED: the AGENTS dict,
  AGENT_META, showAgentPop and the Hermes definition all stay live
  (the Code tab's Coding/Workspace rows and their hover cards run on
  the same machinery). SHOW_AGENTS=False marks the intent; re-adding
  is the b205 markup + the thirds glide from git history.
- LANES: chats born in the agents lane fold into the Chat list while
  the tab is gone (laneOK: code vs not-code) — nothing a user made
  vanishes from every list. The Hermes hot-dog chat survives visibly.
- Inert leftovers kept on purpose: #agents-wrap CSS rules (no matching
  DOM) and the __AGENT_ROWS__ token replace (no token in the page).
- POST-SHIP CATCH on b209: the release chain ran DESPITE a red
  scorecard — `tests | tail -2` reports TAIL's exit code, not the
  gauntlet's. The failure was only my stale 5.2 three-tabs check
  contradicting the new halves check (page was correct), but the hole
  was real. Gauntlet now runs unpiped (redirect to a log, tail after)
  so a red scorecard actually stops the train.

## 6 beta 210 — one line, one size
- Wordmark and version now share 12.5px and a baseline: .vsub came up
  from 9.5px, tracking matched at .18em, and #brand-row switched to
  align-items:baseline (buttons opt back to center) — the version no
  longer floats above the wordmark's line.

## 6 beta 211 — the brand row settles
- The new-chat button now sits on the TEXT's axis: brand-row is
  nowrap + center (baseline flex parked the text high against the
  28px button — measured mids 29/29/29 after). The version never
  drops or wraps: white-space:nowrap + ellipsis as the cramped-width
  fallback, and enough width reclaimed (vsub tracking .06em, gaps
  5px, buttons 26px, wrap padding 2px) that the full "6 beta 210"
  fits untruncated at the 300px default.
- Lesson that cost four rounds: flex baseline + tall centered
  siblings + flex-wrap is a three-way trap — measure mids, not vibes.

## 6 beta 212 — the key that "didn't work"
- GEMINI RETURNS 400 FOR A BAD KEY, not 401 (verified live against
  the OpenAI-compat endpoint with a fake key: 400 INVALID_ARGUMENT
  "Please pass a valid API key" — the model id is irrelevant
  pre-auth). So "HTTP Error 400: Bad Request" almost always means
  the KEY, not our request.
- The validator now surfaces the provider's OWN error message from
  the response body, plus a shape hint: Gemini keys start with AIza
  and are exactly 39 chars — a shorter one means the paste was cut
  (the masked field in Patrick's screenshot showed ~22 dots).

## 6 beta 213 — cloud that discovers itself
- THE REAL b212 BUG, surfaced by b212's own error fix: Google retired
  gemini-2.5-flash FOR NEW USERS — Patrick's 53-char key was fine
  (and the 39-char hint was wrong: keys come longer now; the hint
  only fires under 35 chars as a truncation smell).
- RETIREMENT-PROOF SAVE: /api/cloud/set now LISTS models with the
  key first (auth check + inventory in one call), filters to
  chat-capable ids, picks by preference ladder (gemini-3-flash →
  flash-latest → 2.5 → any flash → pro; groq/claude have their own),
  THEN runs the 1-token probe on the pick. A retired default can
  never brick a save again. Inventory stored in cloud.json.
- UI (per Patrick): "FRONTIER" dropped from the card; under the key
  box, the model list — grey possibilities per provider before a key,
  the REAL inventory in white with green ✓ once live, "· in use" on
  the active one. Old configs without an inventory stay grey until
  re-saved (a configured key with no model list must NOT check
  placeholder names — caught live).

## 6 beta 214 — LFG retired
- THE WHOLE MOMENT IS GONE (per Patrick: "entirely"): the #lfg
  element, the letter-cascade wash and its five keyframe families,
  the sparks, the splash's LFG line, the __SPLASH_LFG__ token and its
  sessionStorage dedupe. Grep says zero refs in source AND the served
  page; the boot (cube wave -> reveal -> greeting) runs clean without
  it — painted/paintdone land, no console errors.
- It was born in 3.x as the loading-bar payoff and got its own drop
  in 5.2. Pour one out; the greeting carries the personality now.
- b214 FOLLOW-UP: one straggler survived the sweep — "Let's fucking
  go." in the GREETING ROTATION (lowercase; the removal grep was
  case-sensitive). Gone now, and the gauntlet check went
  case-insensitive. Sweeps grep -i or they aren't sweeps.

## 6 beta 216 — fenced tables + the live label
- FENCED PIPE TABLES render as REAL tables: models constantly wrap
  tables in bare fences (seen live: UberX costs as mono soup with $7
  gold-highlighted as a number token). The fence handler detects an
  all-pipe-rows body with a divider (bare/md/text/table langs only)
  and emits the styled table; real code stays a code card.
- THE MODEL LABEL IS LIVE (per Patrick: no Gemma-as-compositor
  credit): whoLive() mirrors each STATUS into the .who slot — the
  council's CURRENT model while it runs, "compositing…" during the
  merge (the status no longer names the merger), and at rest singles
  keep their model name while councils settle to the TIER (the
  last-runner name was sticking, which recredited Gemma — caught
  live). Verified through a full Thinking run.
- PROCESS INCIDENT, fully owned: this release shipped from an
  UNGUARDED chain — a heredoc inside a && chain ends the chain, so
  everything after the heredoc ran unconditionally while the gauntlet
  had actually CRASHED (ConnectionResetError: it collided with the
  verification generation still holding the engine). A clean re-run
  came back 59/59 so v216 stands, but the rules are now: (1) the
  gauntlet gets a QUIET instance, never one mid-generation; (2) NO
  heredocs inside release chains — NOTES first, then gauntlet, then
  release, each its own command with its exit code checked.

## 6 beta 217 — the brand row, engine-proof
- Patrick's b212 screenshot showed the b210/211 "fixed" alignment
  still broken IN THE APP — Blink and WKWebView compute different
  line boxes for Michroma vs Plex Mono, so flex centering that
  measured 29/29/29 in the pane landed differently in WebKit. Fourth
  round on this row; the flex approach is abandoned.
- THE INVARIANT FIX: the version moved INSIDE the wordmark's span —
  one inline formatting context = one shared baseline, in every
  engine, by CSS law rather than by metric luck. vsub is 13px mono
  (caps optically match Michroma 12.5 caps), the combined span
  ellipsizes as one unit, buttons flex beside it.
- screencapture(1) needs Screen Recording permission — no Safari
  screenshots from the harness shell; WebKit checks ride on
  construction-level invariants or Patrick's own eyes.

## 6 beta 218 — the provider board
- THE CLOUD CARD'S LIST IS PROVIDERS NOW, not models (per Patrick):
  three fixed rows — Gemini / Groq / Claude — grey until a key is
  saved, green ✓ (with "· in use" on the active one) when its key
  works, red ✗ with the provider's reason when it doesn't. The rows
  never change with the dropdown.
- MULTI-KEY STORAGE: cloud.json became {providers:{id:{...,status,
  note}}, active} — adding a Gemini key no longer overwrites the
  Groq one. Legacy single-provider files wrap transparently on read
  (provider inferred from the base URL; Patrick's real Groq config
  migrated live without a touch). cloud_conf() still returns the
  classic single-provider shape for everything downstream, so
  cloud_stream and the tiers never knew anything changed. Failed
  saves are RECORDED (status fail + note) so the ✗ persists with its
  reason instead of evaporating with the toast.
- Verified end-to-end on the shared store: fake Gemini key -> ✗ row
  with "Please pass a valid API key" while ✓ Groq · in use stayed
  put; probe entry removed from the real config afterwards.

## 6 beta 219 — the cloud pulls its weight
- THE ASK (per Patrick: "use the cloud models to their fullest…
  offload as much as possible"): cloud was only wired into Fast's
  single-model path. Now, on every council run with "Use cloud
  power" on:
  1. THE CLOUD BENCH — every provider with a working key drafts IN
     PARALLEL with the local loop (threads kicked before it, joined
     after, 75s cap). Frontier voices join the council at zero local
     cost; failures record "(no answer — cloud)" and never block.
  2. CLOUD DRAFTS OUTRANK locals in the merge feed (rank -1) so the
     top-5 trim can't drop them.
  3. THE COMPOSITE OFFLOADS — the merge (the heaviest single step,
     and the one that writes the final text) runs on the ACTIVE
     cloud model; local Gemma remains the no-key/failure path
     untouched. X-Models carries the bench so whoLive and the where
     badge label it right.
- THE BUBBLE: "answers blended by Gemma" becomes "✓ Cloud Enabled"
  (green check) whenever configured+turbo — key-less machines keep
  the old texts.
- Verified live with Patrick's real Groq key: Thinking run showed
  compositing… then settled to tier, and the meta badge read CLOUD —
  the composite came from Groq's 120B, not local Gemma.

## 6 beta 220 — the bench and the ladder
- PATRICK'S QUESTION ("will this still use gemma 4 to composite?")
  had the right instinct and an outdated premise: Gemma 4 was only
  ever the best LOCAL compositor. With frontier keys live the answer
  is a LADDER, not a name.
- CLOUD BENCH v2: each working provider fields its picked model PLUS
  one alternate from its stored inventory (pro/120b/70b-class
  preferred), capped at two per provider for free-tier rate limits —
  all drafting simultaneously with the local loop. On Patrick's real
  config that's Groq 120B + Gemini 3 Flash + Gemini 2.5 Pro, three
  frontier drafts beside three local ones.
- COMPOSITOR LADDER: Claude -> Gemini (auto-upgraded to its pro
  model when the inventory has one) -> Groq -> local Gemma 4 as the
  floor. First non-degenerate result ships; every failure falls
  through. cloud_bench()/compositor_ladder() are shared by
  run_council and the X-Models header so the who-label and drafts
  panel name the frontier voices correctly.
- Verified live on real keys: Thinking run -> cloud badge, tier at
  rest, and the final text reads like the pro model that wrote it.

## 6 beta 221 — the bench is visible (and Opus is off it)
- PATRICK'S QUESTION ("also use cloud for the council, or just
  compositing?") was answered by b219/220 — both — but the tier
  bubble only listed local models, so the bench was invisible.
  Council bubbles now seat the cloud voices too ("Groq 120B · cloud"
  rows under the locals), fed by /api/cloud's new bench field.
- COST BUG CAUGHT BY THE NEW BUBBLE: Patrick added a Claude key, and
  the blind alternate-picker benched claude-opus-5 — a paid,
  premium-priced draft on EVERY council question. Alternates are now
  free-tier-only (Gemini/Groq); Anthropic fields one seat and earns
  its keep at the top of the compositor ladder instead. All three
  providers ok: bench = Groq 120B, Gemini, gemini-2.5-pro, Claude;
  ladder = Claude -> gemini-2.5-pro -> Groq 120B -> local Gemma.

## 6 beta 222 — the About window grows up
- POWER reordered (per Patrick): Use cloud power + the key card
  first, Contribute GPU power below it. The "CLOUD FREE KEY · 2
  MINUTES" header is gone — the card explains itself.
- FLEET COPY: the "Your fleet: 0 friends online / contributing" trio
  collapsed into one grey-italic line, "Contributing to N users"
  (N = live hub users from /api/stats). fleet-own and its CSS
  removed; pending-approval requests still render.
- THE LOGO IS A BANNER: the About icon is the dock icon's diagonal
  stripes stretched across the card — seven 45° silver strokes,
  gradient fading in from the left edge and out at the right,
  full-width viewBox with preserveAspectRatio none.
- SELF-INFLICTED: a malformed replacement string in the edit script
  landed mid-file and broke the fleet JS into an unterminated string
  (caught by inspection before it shipped). Rule reinforced: every
  scripted edit batch gets a page-load check before anything else.

## 6 beta 223 — the drip, the pinwheel, the honest label
- SMOOTH UNFOLDING (per Patrick: "not chunks magically appearing"):
  network chunks land in `full`; a paced rAF animator reveals toward
  the backlog (rate eases at lag*0.055, min 2 chars/frame), so text
  flows like typing. RESET replays the replacement smoothly. Stream
  end waits up to 1.8s for the reveal to catch up (hidden windows
  throttle rAF — then it snaps, which nobody sees).
- THE CARET IS DEAD: streaming text ends in the pinwheel (.scaret)
  instead of a blinking block. The tree spinner grew to 17px and is
  flex-centered against the bar.
- THE LABEL SPEAKS PLAINLY: dedicated RUN markers (not
  status-sniffing) drive it — "Running… a, b" listing every
  simultaneously active voice (bench threads + local loop each
  narrate add/remove under a lock), then "Compositor: name" as the
  ladder tries each rung (the marker that sticks is the one that
  wrote the answer). Singles emit their own RUN. Verified live with
  a shimmed-rAF run: Running… -> Compositor: claude-sonnet-5, text
  growing 0 -> 34 -> 238 progressively.

## 6 beta 224 — search asks first, apologizes never
- NOT A REGRESSION (verified before touching anything): search fired
  and returned sources for triggering queries on both tiers. The gap
  was the TRIGGER — needs_search wanted a freshness word or a
  quality-word+place-noun pair, so "what sound system does nowadays
  use" matched nothing and the model answered from memory with an
  "I can't browse the web" apology (Patrick's screenshot).
- INVERTED THE DEFAULT: a real question about the world now searches.
  _WORLDLY_RX (leading question word, or an explicit ask for facts/
  specs/reviews/comparisons, or a trailing "?") triggers, guarded by
  _SELF_CONTAINED — translate/rewrite/summarize/refactor/debug/
  creative-writing and "this code|my essay" phrasing never search,
  because that work carries its own context.
- Battery-tested 17 prompts (8 should / 9 shouldn't): all correct.
  End-to-end on Fast, the exact failing question now reports
  X-Web-Search: 1 and "Searched the web · 4 sources".

## 6 beta 224 — one bar, one tree
- TWO BARS WAS ONE TOO MANY (per Patrick): the council's .blendprog
  bar (with its own 150ms ticker) sat above the worktree card, which
  already had a bar. blendprog is retired — paintDrafts' live branch
  now just clears it; the finished-state "N of M models contributed"
  chip is untouched.
- COUNCIL PROGRESS MOVED INTO THE TREE: the "asking X · i of n"
  status becomes a step row, "Consulting models · 2 of 3", in
  STEP_ORDER between geo and draft. One bar on top, every stage
  listed beneath it — the Claude shape.
- DEDUPED: the "searched the web" chip row only renders once real
  source chips exist (it used to echo the tree's search row while
  empty), and the bare status line yields whenever a worktree is
  present.
- Verified live: a search+council run showed bars=1 and steps
  [Searched the web · 5 sources / Read the pages · 1 image /
  Located it on the map / Consulting models · 2 of 3].

## 6 beta 225 — say it once
- THE LAST ECHO: srcRow still opened with its own "🌐 searched the
  web" label above the chips, so the phrase appeared twice whenever
  sources landed (tree row + chip label). The label is gone and its
  CSS with it — the tree reports WHAT happened ("Searched the web ·
  5 sources"), the chips report WHERE (clickable favicon+domain).
  Informative, not repetitive.
- POWER header removed from Settings; only Personality keeps a
  micro-header now, and the cloud/contribute controls stand on their
  own like the maintenance rows do.
- Verified live: a Fast search run counts exactly ONE "searched the
  web" in the whole message, 5 source chips, 1 bar.

## 6 beta 226 — a bar that means something
- ONE SPINNER, AND IT'S A RING: the ✱ glyph is retired for a real CSS
  circular spinner (.cspin — 15px, bright top arc on a dim track,
  0.7s spin, stilled in perf mode). The trailing text caret-spinner
  is gone entirely; the tree head owns the only one (or the status
  line before a tree exists — never both).
- THE BAR IS HONEST NOW. It used to be done/steps.length, so the
  first finished step read 100% and then sat there. Replaced with a
  weighted plan built at stream start from facts we actually have:
  the X-Web-Search header and the model lineup tell us whether this
  run will search (search/read/geo) and whether it's a council, and
  each phase carries a weight (search 10, read 10, geo 5, council 35,
  draft 30, polish 8).
  * the RUNNING phase gets real sub-progress: council reads its
    "i of n", drafting uses streamed characters on a saturating
    curve (1-exp(-chars/900)).
  * between milestones it CREEPS on a decaying curve toward — never
    past — the next checkpoint, repainted on a 600ms clock so a
    silent 20s model load still shows life.
  * capped at 96% until the stream truly ends, then exactly 100 (a
    planned phase that never materialises, e.g. geo on a non-place
    question, must not hold it short).
- Measured end to end: search-done 10% -> council 1of3 aged 21% ->
  drafting@1500 chars 71% -> finished 100%, monotonic throughout.

## 6 beta 227 — the length dial
- A 1-5 RESPONSE LENGTH SLIDER under Personality's Save button
  (Brief / Short / Balanced / Detailed / In depth), persisted as
  prefs.length, appended to the dated system prompt as a LENGTH
  clause. Level 3 writes NOTHING — the prompt's own calibration is
  the neutral default, so the dial only speaks when the user moved
  it.
- EACH RUNG NAMES A SHAPE, NOT A MOOD: "two or three sentences",
  "one or two tight paragraphs", "several developed paragraphs",
  "up to several pages with headings" — concrete instructions hold
  where adjectives drift. The long rungs carry an explicit
  anti-padding clause ("every paragraph must carry new information…
  if you have said everything worth saying, stop there") so depth
  never becomes rambling. No token ceilings were touched: local
  models run to their natural stop and 4096 already allows ~6 pages,
  so trimming max_tokens would only truncate mid-sentence.
- MEASURED, not assumed: identical question ("how does espresso
  differ from drip coffee?") returned 616 chars at level 1 and 2623
  at level 5 — a 4.3x spread from one dial.

## 6 beta 228 — Funnels
- A THIRD TAB (Chat | Code | Funnels; glide back to thirds). The
  sidebar form asks Patrick's five questions — decision,
  requirements, prompt type (text/images), options per prompt (2-6),
  stages (1-20) — and "Start funnel" runs it in the main panel.
- HOW IT WORKS: /api/funnel is stateless; the CLIENT owns the path.
  Each call sends {goal, reqs, opts, stages, images, picks[]} and
  gets back one stage — a short question plus N option cards (label +
  one-clause tradeoff), generated as strict JSON by the cloud when a
  key is live, else the best local model. Picking appends to picks[]
  and asks for the next stage, so each stage is CONDITIONED on the
  whole path. Past the last stage the same endpoint returns a
  recommendation instead of options.
- IMAGE MODE IS HONEST: nothing is generated. Each option gets a real
  photo harvested from the web via the existing og:image pipeline
  (_page_text appends image URLs as plain strings — corrected my
  first pass, which assumed dicts).
- Funnels are chats in their own lane, so they group in history and
  never mix with Chat or Code.
- Verified end to end on a real decision: stage 1 offered Bushwick /
  Sunset Park / East Flatbush with rent-and-subway tradeoffs; by
  stage 3, conditioned on earlier picks, it offered three specific
  Wyckoff Ave addresses with rents inside the stated $5k budget, and
  the finish returned a single recommendation with a next step.
- b228 FOLLOW-UP: the funnel form leaked into Chat and Code. Cause is
  a CSS-vs-HTML precedence trap — `hidden` is only `display:none`
  from the UA stylesheet, so the author rule `#funnel-wrap{display:
  flex}` outranked it and the element stayed visible no matter what
  modeShow set. #agents-wrap/#code-wrap never declared `display`,
  which is why they were never affected. Added
  `#funnel-wrap[hidden]{display:none}`. RULE: any wrap that sets
  `display` needs its own `[hidden]` rule. Verified across all four
  tab transitions — each lane shows only its own controls.

## 6 beta 230 — the funnel row lines up
- A <select> and an <input type=number> have DIFFERENT intrinsic
  heights, and the number carries spin buttons on top — so the three
  boxes could never match by accident. All of it is stated now:
  height 32px, box-sizing border-box, appearance:none, zero margin,
  matching padding/line-height, spin buttons suppressed, and one
  shared inline-SVG chevron so both selects use the same arrow
  instead of the two native ones. Grid gained align-items:end and a
  slightly wider gap; focus lightens the border on all three.
- Measured: all three boxes 32px tall with identical top and bottom
  edges.

## 6 beta 231 — the length slider gets dressed
- THE b227 SLIDER CSS NEVER SHIPPED: its insertion anchor didn't
  match, so `.replace()` silently no-op'd and the control fell back
  to the native blue iOS slider with a big sans label. Third silent
  no-op in this line — from here scripted CSS inserts assert the
  anchor exists AND assert the result changed.
- Now: 2px hairline track full width (290px in the panel), 11px
  round thumb that scales slightly on hover, native appearance reset
  on track and thumb for both WebKit and Gecko. The label is the
  window's standard micro-header — IBM Plex Mono 9px, .18em, uppercase,
  --faint — measured EQUAL to the PERSONALITY header's computed
  font, size, tracking and colour, with the value right-aligned in
  --dim.

## 6 beta 232 (pending release) — the ZITO override
- Hold Z, I, T and O together anywhere in the app and the chrome falls
  away to a mission-control board. It is an easter egg, but not a fake
  one: every panel is fed by the endpoints the real UI already uses.
  Spokes are the models `/api/engines` reports UP (biggest nine by
  memory) plus each cloud provider whose status is `ok`; the ticker
  carries measured latency, real memory/GPU from `/api/stats`, the live
  tier and key count; the left roster is real subsystem state (facts in
  memory, clips in the pantry, fleet peers, workspace root, updater
  version). The only invented figures are the ones that are obviously
  jokes — `ui unbeatable`, `vibes`, and the three `wip` checkboxes.
- The terminal runs a REAL query. It posts to `/api/chat` like send()
  does and narrates the same wire markers: STATUS, STEP, RUN, DRAFT,
  SOURCES, RESET. Token counts come from the draft payloads, timings
  from the clock. Nothing is scripted.
- WIRE PARSING: send() re-runs its regexes over the whole buffer every
  chunk and relies on idempotent handlers. The terminal needs each
  marker exactly ONCE, so it consumes frames instead — scan for
  `\0`, find the closing `\0`, emit, slice; a trailing partial frame is
  held back rather than printed. Half a marker never reaches the screen.
- The combo types four letters into whatever had focus, so engaging
  strips up to four trailing z/i/t/o characters back off the focused
  field and re-fires its `input` event. Escape closes the terminal,
  Escape again stands the board down — wired through `window.zitoEsc()`
  so the existing Escape chain asks it first and is otherwise untouched.
- Right rail: only the log panel flexes (`.pb.grow`); the code board and
  mission control size to content, otherwise the bottom meter was
  cropped off. The log is `justify-content:flex-end` so overflow spills
  off the TOP and the newest line is always the visible one.
- Debug lines written after the answer block exists are inserted ABOVE
  it (`zAnchor`), so the transcript reads dispatch → answer → close
  instead of interleaving draft/polish steps under the prose.
- Everything is scoped under `#zito` with its own `--z*` palette and
  carries no dependency on the app's tokens — deliberately single-theme,
  that screen is always night.
- Verified live: board built from 12 real spokes, Thinking-tier run
  (7 models, web on, 4 sources, claude-sonnet-5 composite, 49s) and a
  Fast-tier run (Claude, 3.6s) both narrated end to end; letter-strip,
  focus hand-off and both Escape levels measured. Gauntlet 60/60.
- NOT verified on WebKit — the browser pane is Blink. Worth one glance
  in the desktop build before this ships.

## 6 beta 233 (pending release) — ☁️ Cloud Only, and honest key status
- A fourth mode sits under Fast/Thinking/Pro in BOTH pickers (the sidebar
  rows and the composer dropdown, which build from `TIERS` and
  `TIER_META` respectively, so one dict entry populated both). It answers
  entirely off the API keys: every working key drafts in parallel and the
  compositor ladder writes the final answer; with a single key it streams
  straight through.
- NOTHING RUNS LOCALLY, and that took more than skipping the council.
  `resolve_tier` returns [] early; the MLX engine load is skipped; the
  smallest-cached-model route fallback is skipped; reflection and peer
  review are forced off (both are local passes); the silent-answer rescue
  that retries on the smallest local brain is skipped; the place-pinning
  pass and the memory-extraction pass are skipped — both run a local
  model. Any one of those left in would have quietly broken the promise.
- Picking the tier IS the cloud opt-in, so the bench runs regardless of
  the separate `turbo` preference.
- Images say so instead of answering blind: the cloud path sends text
  only, so an attached image in this mode gets a note pointing at the
  other tiers rather than an answer about nothing.
- No keys: the tier greys out in both lists, its bubble says to add one
  under Settings › Cloud power, `setTier` refuses it, and a saved
  "Cloud Only" that has lost its keys falls back to Fast at boot. Asking
  anyway returns instructions, never a quiet local fallback.

### The reason "active keys" wasn't true yet
- `cloud_text` swallowed EVERY exception and returned "", so a revoked
  key and a retired model both looked like "that model had nothing to
  say". Found live while testing this: Groq showed a green ✓ in Settings
  while every call came back **401 Invalid API Key**, and the
  `gemini-2.5-pro` bench seat **404'd on every question** ("no longer
  available to new users" — the same trap gemini-2.5-flash sprang once
  before, now handled generically instead of per-model).
  Two of four cloud seats were dead weight on every council, silently.
- Now HTTPErrors are classified. 401/403 means the KEY is bad, so the
  provider is marked `fail` with the code — which is what makes "grey it
  out when no keys are active" mean *active*, not *last known good*.
  400/404 means the MODEL is gone, so only that model is retired, and
  the retirement persists in `cloud.json` (`dead: [...]`) so a launch
  doesn't re-donate a seat to it. Re-saving that provider's key clears
  the list and revives the in-memory set — re-saving IS the retry.
- The alternate picker no longer falls back to `alts[0]`. Once dead
  models were skipped it walked the inventory into
  `gemini-3.7-flash-video-understanding-eap`, seating an unknown voice
  on the council. An alternate must be a bigger sibling (pro/120b/70b)
  or the provider fields one seat.
- Measured: bench went 4 seats → 2 real ones; Settings reads
  `✓Gemini  ✗Groq · key rejected (HTTP 401)  ✓Claude · in use`;
  retirement survived a restart; Fast-tier turbo (the refactored
  `cloud_stream`) still streams. Gauntlet 60/60.
- STILL OPEN, not touched: the composite gate is `len(_t) > 120`, so a
  legitimately terse composite ("Lisbon.") is rejected and the ladder
  burns every rung before falling back to a raw draft. Pre-existing and
  shared with Thinking/Pro — worth a look, but it's a tuned quality
  heuristic and not this change's business.

## 6 beta 234 (pending release) — telling a cut-off key from a dead one
- "Invalid API Key" is what a provider says for a HALF-PASTED key and for
  a REVOKED one alike, and the field is a password box, so there is no
  way to tell by eye. The save handler now judges the key's shape and
  names which failure it is.
- PREFIX FIRST, then length. The prefix confirms the vendor; only then is
  the length worth judging. That ordering means a format change upstream
  can never block a good key — an unrecognised prefix only ever adds a
  hint, never a rejection.
- A right-prefix, short key is rejected BEFORE the network: no round trip
  to be told something we already know.
- ONLY GROQ HAS AN EXACT LENGTH (gsk_ + 52 = 56). This was almost a bug
  that shipped: the first cut had Gemini at a fixed 39 (the old AIza
  width), and measuring this machine's own working keys showed Google
  now issues 53 and Anthropic 108. With `!=` on an exact length, both
  real keys would have been reported as truncated pastes — the precise
  failure the feature exists to prevent. Gemini and Claude are floors
  now, and the message says "at least N" for them.
- Three outcomes, verified against synthetic keys at real-world lengths:
    prefix ok, short          -> "that paste looks cut off — 28 characters,
                                  but a Groq key is 56"
    prefix wrong              -> "...doesn't look like a Groq key: they
                                  start with gsk_. Wrong provider
                                  selected, or the front of the paste was
                                  lost"
    prefix ok, plausible len  -> "the key is the right shape, so this isn't
                                  a bad paste: it has been revoked or
                                  regenerated. Issue a fresh one"
- FOUND BY THIS: the Groq key on this machine is a full, well-formed 56
  characters — so it was never a paste problem. It is revoked. That is
  now what the app says instead of leaving "Invalid API Key" to be
  argued with.
- TESTING NOTE: the failure branch WRITES the attempted key into
  cloud.json (status fail). Any test of the save path must back the file
  up and restore it — done here, verified byte-identical both times.
- Gauntlet 60/60.

## 6 beta 235 (pending release) — a spent quota is not a dead key
- REGRESSION FROM b233/b234, found live within the hour: Settings showed
  ✗ Gemini with "You exceeded your current quota", while `/models` on
  the very same key answered **200**. The key was perfect. The app had
  marked a healthy provider permanently failed over a free-tier quota
  that refills by itself, and nothing but a manual re-paste would clear
  it. Two builds' worth of "honest key status" had made the app
  confidently wrong.
- ROOT CAUSE, two places. The save path marked `fail` on ANY HTTPError —
  and the save probe SPENDS QUOTA, so a 429 there is entirely normal on
  a free tier. And `cloud_note_failure` classified purely on the status
  code, treating 403 as auth; Google returns 403 for some quota
  conditions, so a throttle could down a provider at runtime too.
- THE BODY GETS A VOTE. `cloud_failure_kind(code, body)` returns
  auth / quota / other, matching on the code AND on quota language
  (quota, rate limit, resource exhausted, too many requests, billing).
  Unit-checked against eight real messages, including 403
  RESOURCE_EXHAUSTED (quota, not auth) and 403 "not authorized" (auth,
  not quota).
- A THROTTLED PROVIDER RESTS, IT DOES NOT FAIL. status stays `ok` and a
  `cool` timestamp benches it for 10 minutes: `cloud_ok_providers()`
  skips it so no council seat is wasted on a guaranteed 429, and it
  returns on its own the moment the window passes. A rate-limited key
  now SAVES successfully with a warning, because it is a good key.
- `cloud_conf()` no longer hands back a failed or resting active
  provider — it falls through to any other working one before giving up
  on the turbo path. Matched on provider id, not dict equality.
- ONE-TIME REPAIR: `_cloud_repair()` runs once per process and converts
  any provider sitting at `fail` with a quota-shaped note back to ok
  plus a cooldown. Anyone who ran b233/b234 heals on next launch without
  touching a thing. Verified on this machine: gemini fail -> ok, resting.
- Board shows a third state — amber ⏳ "resting 10m · quota" instead of
  the red ✗ that means "go fix your key".
- Measured end to end: repair fired on launch; bench dropped to Claude
  alone while Gemini rested; Cloud Only took its single-provider
  STREAMING path (first exercise of that branch) and answered; Fast-tier
  turbo answered; the cooldown expiring put Gemini back on the bench
  unaided, and the next 429 re-rested it — still never `fail`.
- Gauntlet 60/60.
- NOT CHANGED, worth a look: the Gemini pick is `gemini-3-flash-preview`,
  chosen because "gemini-3-flash" heads the discovery preference order.
  Preview models carry the tightest free-tier quotas, which is why this
  keeps happening; `gemini-flash-latest` is in the same inventory.

## 6 beta 236 (pending release) — one cloud model failing never ends a query
- The rule, per Patrick: a cloud model that hits a limit OR fails in any
  other way is DROPPED (hourglass) and the query carries on with whatever
  still works. Five gaps stood between b235 and that rule.
- ONLY QUOTA RESTED ANYTHING. A 500, a timeout, a dropped connection or
  an empty completion recorded nothing at all, so the same broken
  provider was asked again on the very next question. Now every one of
  those rests it for GLITCH_COOLDOWN (2 min, vs 10 for a quota — a
  glitch is usually a glitch and the provider is wanted back). Only a
  rejected KEY still marks `fail`, because only that needs a human.
- THE COUNCIL'S CLOUD THREAD WAS UNGUARDED. `status()` writes to the
  client socket, so a reader closing the tab raised inside the thread —
  killing it before `run_mark(rm=)` and `took_part()` ran, which pinned
  that model in the "Running…" label forever and lost it from the
  ledger. Whole body is now try/except with the un-marking in a
  `finally`. A cloud voice can fail in every way there is without taking
  anything else with it.
- THE JOIN WAS 75s PER THREAD, sequentially — four hung providers could
  have held the answer for five minutes. They start together, so they
  share ONE deadline now; whatever hasn't landed is simply absent.
- TWO PLACES COULD ABORT THE QUERY OUTRIGHT: the single-provider Cloud
  Only path raised "the cloud provider didn't answer", and a council
  where every cloud voice failed raised "none of the selected models
  answered" straight at the reader. Both are caught now.
- `_cloud_all_down()` writes the honest version: which providers are
  resting AND ROUGHLY WHEN THEY RETURN, which need a new key, and that
  Fast/Thinking/Pro will answer it on this machine right now. "Try again
  later" is useless without the later.
- Measured with every provider rested by hand: bench empty, Cloud Only
  greyed (`available:false`), the query answered with the explanation
  above instead of an error, and Fast tier fell straight through to
  Gemma 4 26B and answered normally. Unreachable-endpoint checks confirm
  `cloud_text` and `cloud_stream_conf` return ""/False rather than
  raising, and that an unrecognised base writes nothing to config.
- Gauntlet 60/60.

## 6 beta 237 (pending release) — place questions search; nothing waits forever
### The 1/5 answer: the search gate was a GRAMMAR test
- "late night restaurants in 11221" got an apology for having no data.
  It never searched. `needs_search()` fires on a leading question word,
  a "?", or a freshness word — and that phrasing has none, so the one
  class of question where a model's memory is guaranteed useless went
  straight to memory. Measured, all previously FALSE:
      late night restaurants in 11221 · restaurants open late in 11221
      late night eats bushwick · coffee near 11221 · sushi in brooklyn
  Adding "best", or a "?", or leading with "where" flipped every one of
  them to True. The gate never asked the only question that mattered:
  is this about a PLACE?
- `_place_terms()` is no help as a detector — it is a filler-stripper
  and returns non-empty for "explain recursion in python" too. So:
  `_VENUE_RX` (venue and cuisine categories), `_ZIP_RX` (a US zip), and
  `_NEARBY_RX` ("near me", "open now"). Any hit searches, whatever the
  grammar. Placed AFTER the `_SELF_CONTAINED` check so "translate my
  restaurant menu into spanish" still stays local — verified.
- SECOND HALF, same bug: `placey` (the map + pins path) was gated on
  hours/open/closed/phone/address/menu/reservation, which "late night
  restaurants in 11221" also fails — so even a searched place query took
  the plain web path. `_VENUE_RX` counts there too now.
- Before: an apology. After: 5 sources incl. the Yelp page for 11221,
  named venues with addresses and Sunday hours, 3 pinned.

### The seven-minute answer: no deadline on the local loop
- Phi-4 14B benchmarks at 4.3s standalone. Inside that council it ran
  336 SECONDS and produced nothing. Not the model: every council model
  here is MLX, MLX pins the whole model in RAM, so each one in turn is a
  full disk load with the previous evicted — and with 18.9 GB free
  against Gemma 4 26B's 17.0 GB it thrashed. Qwen 3.6 35B MoE wants
  20.0 GB and was correctly skipped ("(no answer — low memory)", the
  ~6 tok in the log).
- The cloud bench got a shared deadline in b236. The local loop had NONE
  — one straggler could hold the answer indefinitely. Now each model
  runs on a thread with a 120s cap under a 240s whole-loop budget;
  whatever hasn't answered is simply absent, the same treatment a failed
  cloud voice gets. Partial output over 200 chars is kept rather than
  thrown away. Abandoned threads are daemons and the next model's engine
  swap stops the process they are stuck in.
- Measured on the same question: 414s -> 251s, and Phi-4 delivered a
  real 1068-char draft instead of nothing.
- HONEST LIMIT: 251s is still slow, and the deadline only caps the worst
  case. Three large MLX models in one tier means three sequential engine
  loads on this machine; making Thinking genuinely fast needs resident
  engines or a smaller roster, not a timeout.

### The number that started it
- The ZITO terminal printed one clock with no marker, so "Phi-4 44.91s"
  read as a duration when it was a timestamp — it meant Phi-4 STARTED
  there. Now `@44.9s` is the wall clock and `+336.5s` is how long the
  spoke took, and a draft line shows both.
- Gauntlet 60/60.

## 6 beta 238 (pending release) — a follow-up after a funnel knows its subject
- "where can i find this in 11221?" straight after a funnel that had
  settled on a sushi combo searched a BARE ZIP and came back with
  zillow, hotelplanner, crimegrade and housecashin — apartment listings
  and crime stats. The model then said, correctly, that it had no idea
  what "this" referred to.
- TWO faults, and the second one nearly shipped unnoticed.
  1. `_entity_thin("where can i find this in 11221?")` was FALSE.
     `_place_terms` leaves "find 11221" — a verb and a zip — so the code
     decided the query names a thing and never tried to inherit a
     subject. A demonstrative with no noun of its own IS the signal that
     the subject is upstream, so `_REFERS_BACK_RX` (this/that/these/it/
     them/there/the same) now makes a query thin on its own.
  2. FUNNEL PICKS ARE ASSISTANT TURNS. The client records each one as
     `{role:"assistant", content:"question → choice"}`, and
     `_thread_terms` only ever scanned USER turns — so the only user
     turn was the goal line and it returned "". `_thread_terms` now
     harvests the "→ choice" lines from the last 14 messages, earliest
     first (the early picks are the category, "Sushi"; the late ones are
     trailing detail, "Water").
- PROCESS NOTE, worth keeping: fix 1 was "verified" against a transcript
  written by hand with the picks as USER turns — it passed, and it was
  meaningless. Re-running it against the shape the client actually emits
  returned "" and exposed fault 2. Reconstructed fixtures must be copied
  from the producing code, not from memory of it.
- Measured on the real shape:
    before: "where can i find this in 11221?"
    after : "sushi combo platter deluxe where can i find this in 11221?"
  Ordinary follow-ups unchanged ("how tall is it?" -> "brooklyn bridge"),
  and with no history nothing is prepended.

## 6 beta 238 — the acceleration lockup
- A chip beside the engine chip naming the silicon path local models
  actually run on: MLX on Apple Silicon, CUDA on an NVIDIA box, and
  nothing at all on plain CPU (there is nothing to boast about). Served
  as `accel` on /api/setup from `accel_name()`, cached because
  nvidia-smi is a subprocess.
- A WORDMARK, not either vendor's artwork: it stays in the app's own
  greyscale type instead of importing a green eye, reads at 10px where a
  logo would not, and avoids reproducing a trademark. The dot carries
  the vendor colour (#76b900 NVIDIA green, #c9ccd2 Apple silver) so the
  two read apart instantly.
- 9px made the pill 22px against the engine chip's 23 and the pair sat a
  hair out of true. At 10px both measure 23px with identical top and
  bottom — checked, not eyeballed.
- Gauntlet 60/60.

## 6 beta 239 (pending release) — the MLX swap actually completes
- Patrick asked whether the council could run models sequentially instead
  of loading them at once. IT ALREADY DOES, and always did: the local
  loop is a plain `for`, `run_model` calls `ensure_mlx_engine` under
  `_engine_lock`, and that calls `_stop_other_mlx`, which terminates
  every other MLX engine. One resident engine is an existing invariant —
  nothing in b237 changed it. Concurrency was never the problem.
- MEASURED: a healthy Gemma 4 26B -> Phi-4 14B swap is 1-4 SECONDS. So
  the 336s had nothing to do with load cost.
- WHAT IT ACTUALLY WAS: `_stop_other_mlx` sent SIGTERM, waited 8s, and
  then dropped the handle NO MATTER WHAT. A big engine slow to die still
  had its ~17 GB wired when the next one spawned into it; the newcomer
  crawled or died; `ensure_mlx_engine` then polled its full 180s; and
  `run_model`'s URLError path retried with the SAME 180s default, twice
  over. 180+180 = 360, and the observed figure was 336.
- Now: SIGTERM, then SIGKILL if it ignores that, and the handle is not
  dropped until the process is genuinely gone. The flat 2.5s Metal beat
  became a watch — poll available memory until it stops climbing, capped
  at 6s — so teardown is observed rather than guessed.
- And a retry gets a SHORT window (45s, not 180). The first attempt
  already had the long one; if the engine didn't come up then, more
  waiting is not the missing ingredient. Worst case went from ~540s
  (three full attempts) to ~270s, and the b237 per-model cap of 120s
  bounds it well below that inside a council anyway.
- Measured after: swaps 1.1-3.6s, single-resident invariant holds, and a
  three-model Thinking run finished in 31.2s with four real drafts.
- Gauntlet 60/60.

## 6 beta 240 (pending release) — the search QUERY was the problem
- Side by side against Claude on "any bars or clubs open" in Bushwick,
  Concorde said its search "pulled results for Clifton, NJ instead" and
  cited oneminuteenglish.org, superpages, restaurantji and yellowpages.
  It is not the backend. Measured on the same engine:
      "i meant whats a good spot. any bars or clubs open"
          -> Virginia Beach, San Diego, BODRUM, GTA5 mods, Yandex Translate
      "bars bushwick brooklyn open late"
          -> Yelp, The Infatuation's Bushwick bar guide, barsforkings
  Garbage in, garbage out. Three faults built the garbage.
- 1. THE LOCATION WAS TRUNCATED OFF. `_thread_terms` returned
  `toks[:4]` — from "any good bars or clubs open late/now in bushwick
  ny" that is "any good bars clubs", four generic words with the ONLY
  part that mattered cut off the end. It now borrows what the new query
  LACKS (`avoid=`), which leaves exactly "bushwick ny".
- 2. CONVERSATIONAL PREAMBLE WENT TO THE INDEX. "i meant", "actually",
  "sorry", "wait" are for the reader. `_PREAMBLE_RX` strips them.
- 3. A PLACE QUESTION WITH NO LOCATION never threaded: "any bars or
  clubs open" names venues, so `_entity_thin` said it has a subject —
  but a place search with no location is worthless. Venue queries thread
  too now.
- Subjective words joined `_PLACE_FILLER` (any/good/best/spot/place/
  recommend/…): the index has nouns, not opinions. Query went from
  "bushwick ny whats a good spot any bars clubs" to "bushwick ny bars
  clubs".
- HOST RANKING. Even a clean query returned tagvenue (venue hire),
  pinterest, tiktok and a YOGA STUDIO, while Yelp / Time Out / The
  Infatuation sat lower in the SAME list. `_host_score` demotes
  directory spam and promotes sources a person would actually open —
  demote, never drop, because sometimes the junk is all there is.
- AND `is_direct` WAS OUTRANKING IT. That gate asks "does this result
  mention the thing asked about", which is right for "is ables open" and
  useless for "bars in bushwick", where every directory mentions
  Bushwick. A query naming a CATEGORY is a discovery question and is
  ranked by host alone.
- Tripadvisor is deliberately NOT promoted: it put a yoga studio at rank
  0. Its guides are good, its per-venue pages are noise, and a hostname
  cannot tell them apart.
- Measured end state for that question: Time Out's Bushwick neighborhood
  guide first, then a Bushwick bars-and-restaurants guide naming
  Roberta's, then a nightclubs guide. Spam at the bottom.
- STILL NOT PARITY, and ranking heuristics will not get there. Claude's
  answer came from a PLACES API with structured hours, ratings and
  coordinates; this reads search snippets. The real fix is Google Places
  or Yelp Fusion behind `place_search` — a key, not a regex.
- Gauntlet 60/60.

## 6 beta 241 (pending release) — answer type, closer to Claude Code
- The rule that actually governs answer prose is `.msg.ai .body`, not the
  `.msg .body` above it — `--helv` never applied at all. Measured before
  touching anything: 14.75px / 25.08px leading / 15px between paragraphs,
  in a 661px column.
- Now 16px / 1.72 (27.5px) with a 1.2em block gap (19px), tracking a
  hair tighter at -.009em because a larger size needs less of it. The
  heading scale moved with it (23 / 19.5 / 17) — an 18px h2 over 16px
  prose barely read as a heading. List items breathe at .5em.
- Same 661px column: at 16px that is still roughly 70 characters, which
  is where prose wants to sit, so the measure did not need touching.
- HONEST LIMIT: this matches the METRICS — size, leading, block rhythm,
  measure. It cannot match the typeface. Claude's faces (Styrene,
  Tiempos) are commercial licences that can't be embedded in a shipped
  binary, so the stack stays system-native: SF Pro on macOS, Segoe on
  Windows. That is the same family of grotesque and the rhythm is what
  the eye actually reads, but it is a near-match, not a replica.
- Gauntlet 60/60.

## 6 beta 241 — settings hierarchy
- "Include Beta Releases" was 13px at full text colour, the same weight
  as "Check for updates" directly above it, so a niche preference read
  as a headline. Now 11.5px italic in --faint (measured 11.5 vs the
  button's 12.5), lifting to --dim on hover.
- "Forget me" was a bordered danger BUTTON competing with Close. Now
  "Forget Me": borderless, transparent, 11.5px, centred, underlined with
  a 3px offset, full row width — a quiet way out rather than a control.
  Its confirm-state reset string was updated to match the new casing,
  which is the kind of thing that silently reverts a label.

## Research note — hours and ratings WITHOUT an API key
- Scraping Google Maps was raised. Against it: it breaks Google's terms,
  the results are JS-rendered behind obfuscated endpoints that change
  without notice, and — the part that actually decides it — Concorde is
  a DISTRIBUTED DESKTOP APP, so the traffic comes from every user's own
  IP. The failure mode isn't our scraper breaking, it's users getting
  CAPTCHA'd on their personal Google accounts.
- MEASURED ALTERNATIVE, OpenStreetMap Overpass, free and keyless, same
  project as the Nominatim geocoder already in use:
      Bushwick bbox -> 40 bar/pub/nightclub venues in 1.1s
      33 of 40 (82%) carry structured opening_hours
      e.g. Mood Ring "Mo-Tu 17:00-02:00; We-Fr 17:00-04:00"
           Keybar    "Mo-Th 18:00-02:00; Fr-Sa 18:00-04:00; Su 18:00-24:00"
      real venues, machine-readable, no key, no billing account
      ratings: ZERO — OSM carries none
- So HOURS, the perishable half of "open late tonight", are available
  free and legally. RATINGS are the part that needs a commercial
  provider (Foursquare's free tier has them; Yelp has them).
- Gauntlet 60/60.

## 6 beta 241 — the wordmark becomes a delta wing
- Per Patrick's sketch: the dock icon's diagonal gradient sweeps INTO
  the C rather than sitting beside it as a separate glyph. Right idea
  for something called Concorde — the mark is a wing whose trailing
  edge is the letter.
- Same construction as make_icon.py (parallel bars, each shorter toward
  the corner so the group reads as a triangle) and the same greyscale
  ramp, but REVERSED: steel at the far tip, brightest where it meets
  the C, so the eye carries the sweep into the type instead of stopping
  at it.
- The sweep is shallower than the icon's 45 degrees — measured against
  the sketch, whose hypotenuse is noticeably wider than tall, so it
  reads as a wing rather than a corner. viewBox widened to 23, four
  bars at ~39 degrees, 2.4 stroke.
- The 2px gap is gone (margin-right:-.5px): a gap read as
  icon-then-word, and the whole point is one lockup.
- The type was NOT touched — "the rest is fine". Notably the `b` was
  left as a plain inline run: making it inline-block to reach
  ::first-letter would have re-opened the b217 baseline divergence
  between Blink and WKWebView, which is documented right above this
  rule. The blend is carried by the gradient landing on the wordmark's
  own colour instead.
- Gauntlet 60/60, wordmark tests included.

## 6 beta 242 (pending release) — real venue hours, from OpenStreetMap
- `osm_places(terms, locality)`: Nominatim geocodes the locality, then
  Overpass returns named venues within 1400m of it. Free, keyless, and
  the same project the pin geocoder already uses. Cached 30 min per
  (category, locality) — venue hours barely change and the endpoint is
  a volunteer service.
- Fed into the answer as a LABELLED AUTHORITY placed ABOVE the web
  snippets, because a blog post's stale hours must never outrank a
  structured tag. Purely additive: no venues found changes nothing.
- Pins now come straight from the structured rows, preferring the venues
  the answer actually named. That replaces the old local-model
  extraction pass for these questions — OSM already knows the names and
  the coordinates, so running a model to re-read them was pure cost.
- `_oh_open_now()` parses a PRAGMATIC SUBSET of the opening_hours
  grammar: day ranges and lists, clock ranges, past-midnight spans,
  24/7. Anything with holidays, weeks or months returns False rather
  than guessing — a missed venue is a small loss, a venue wrongly called
  open is the whole failure.
- BUG CAUGHT BY TESTING AT 01:58 ON A SUNDAY: a past-midnight span
  belongs to YESTERDAY's rule. A bar posting "Mo-Sa 18:00-04:00" is open
  at 2am Sunday — it is still inside Saturday's span — but matching only
  today's weekday called it shut. Since "open late" is the entire point
  of the feature, that bug would have broken it precisely when it
  mattered. Now 7/7 on hand-built cases including that one.
- MEASURED, the same question that started this:
    before  "My search didn't turn up real bar or club listings for
             Bushwick tonight — it pulled results for Clifton, NJ"
    after   Bossa Nova Civic Club (open until 4am), Boobie Trap,
            Bonus Room — named, with hours, plus a caveat that Keybar
            and Christophers Palace close early on a Sunday, and four
            pins with coordinates.
  Bossa Nova is the same venue Claude named in the side-by-side.
- STILL NO RATINGS. OSM carries none; that half needs a commercial
  provider and is garnish next to knowing the door is open.
- Gauntlet 60/60.

## 6 beta 242 — the wing goes BEHIND the C, and a CSS bug that hid it
- A 72%-transparent letter occludes nothing, so the bars showed straight
  through the C's stroke — that was the "funky" overlap. The TYPE is
  opaque now and the 72% moved to the group: children composite first
  (the C hides the bars it crosses), then the lockup dims as one.
  `.vsub` compensates at .53 so the version row still renders at the
  .38 it always did.
- Overlap cut from 2.6px to 1.3px of an 11.7px mark — a tuck under the
  left stroke rather than a collision in the bowl.
- CAUGHT ONLY BY MEASURING: the mark was rendering 179x214px instead of
  11.7x9.8. The previous edit had appended a comment continuation AFTER
  an already-closed block comment, leaving a stray `*/` that killed the
  `#vmark` rule and everything after it in the stylesheet. A screenshot
  would have shown "a big logo"; getBoundingClientRect showed 179 and
  named the cause. Balance-audited every CSS comment afterwards: 165
  opens / 167 closes, and BOTH extra closers were false positives —
  `*/` inside JS regex literals like /\*\*(...)\*\*/g. One real bug.

## 6 beta 242 — starter prompts
- A row of clickable chips in the gap between the greeting and the
  composer. Placed INSIDE #composer-wrap, so it is the composer's width
  by construction rather than by a number that would drift — measured
  661 = 661.
- HOW MANY IS MEASURED, NOT GUESSED: ten candidates are laid out, then
  any that wrapped past the second row is removed. Chip widths vary with
  their text, so a fixed count would either overflow or leave a gap.
  `flex:1 1 auto` then grows the survivors to fill each row — measured
  99% span on both rows; centred chips had left ragged gutters against a
  full-width composer.
- 200 prompts in ten themed sets, one drawn from each and shuffled, so a
  refresh never shows five dinner questions in a row. The emoji is
  decoration: it is stripped before the question is sent.
- Emoji fonts are named explicitly in the stack — Space Grotesk has no
  glyphs for them and the ZWJ sequences fell through to tofu.
- Visible only on the empty hero, and re-fitted on window resize.
- Gauntlet 60/60.

## 6 beta 242 — the wing was genuinely short, and the same CSS trap twice
- Patrick: "still doesn't match the top and bottom of the C — looks
  smaller." He was right, and the box measurement said otherwise: the
  ELEMENT was 9.8px against a 9.77 cap. The ink was not filling it.
  The bars' right ends stopped at staggered heights, so at the junction
  — the one place the eye compares wing to letter — the painted span
  was 84% of the box with an empty bottom-right corner.
- A fifth short bar carries the trailing edge down to ~94%, and the
  element is now sized so the INKED span, not the box, equals the cap:
  painted 10.38 against cap 9.77, a ~6% optical overshoot that a
  tapering triangle needs against a solid stroke.
- LESSON: `getBoundingClientRect()` on the element measures the BOX.
  For a shape that does not fill its own viewBox, that number is a
  reassuring lie. Measure the painted geometry (the <g>, plus half the
  stroke width, which getBoundingClientRect excludes).
- AND THE SAME CSS TRAP FIRED TWICE: appending explanation to a comment
  that was already closed leaves a stray `*/`, which kills the rule
  after it AND everything below it in the sheet. First time it rendered
  the mark at 179x214. RULE: after editing any CSS comment, balance-audit
  the <style> blocks specifically — a whole-file scan reports false
  positives from `*/` inside JS regex literals like /\*\*(...)\*\*/g.
  Style blocks now 118 opens / 118 closes.

## 6 beta 243 (pending release) — Settings becomes rail and pane
- The dialog was one column of unrelated widgets. Now a 212px named rail
  on the left and ONE pane at a time on the right: Personality, Cloud
  power, Community, Models, Updates. The surface stops growing — the next
  setting gets a rail entry, not another row on a stack.
- EVERY CONTROL KEPT ITS ID and its markup; they were only reparented, so
  the JS wired to each one is untouched. Verified all 15 still resolve
  (persona, len-slider, turbo, ck-*, contrib, open-setup, about-check,
  betaup, about-forget, about-close) and every pane shows on click.
- THE SPEC LIST. "6 BETA 238 · M4 PRO" wrapped mid-word in a narrow rail
  and read as debris. It is a definition list now — label left, value
  right, one fact per line, so it cannot wrap and there is room for the
  memory Patrick asked for plus the accelerator:
      version 6 beta 238 · chip M4 PRO · memory 52 GB
      accel MLX · models 11 / 20
  Memory comes from /api/stats mem_total_gb, accel from /api/setup.
- TAG-COUNT CHECKED before running it: 26 <div> / 26 </div>, 5/5
  <section>, 1/1 <nav>. The 5.1 rebuild dropped one closing div and
  swallowed every veil below into the hidden modal; that is the failure
  this restructure was most likely to repeat.
- FALSE ALARM WORTH RECORDING: `#about-card` is a DUPLICATED id — the
  update and new-models dialogs reuse it. `querySelector("#about-card")`
  returns #new-veil's hidden copy and measures 0x0, which looks exactly
  like the old 0x0 bug. The settings CSS is ancestor-scoped
  (`#about-veil #about-card`) so it targets the right one; only the
  measurement was wrong. Measure with the ancestor in the selector.

## 6 beta 243 — answer type, chip width, hairline, vendor names
- Answer prose moves to the sidebar's own face: Space Grotesk 13px
  against the sidebar's measured 12.5. The system stack (SF here, Segoe
  on Windows) was a stranger in its own window and at 16px read as cheap.
  Heading scale came back down with it (18 / 15.5 / 13.5).
- Starter chips are PINNED TO THE COMPOSER: #composer-wrap is full width,
  so on a maximised window they ran 1252px against the composer's 780.
  Same max-width and auto margins — measured left 560 = 560, right
  1340 = 1340 at a 1600px viewport.
- The hairline under the hero was `body.perf #composer-wrap`'s border-top
  — perf mode only, which is why it looked intermittent. Gone.
- The accelerator chip names the VENDOR now, not the toolkit: NVIDIA
  rather than CUDA, AMD when rocm-smi answers, MLX unchanged. Each
  branch forced and verified. An AMD card without ROCm reads as CPU,
  which is honest.
- Gauntlet 60/60.

## 6 beta 243 — the stacked lockup, uniform everywhere
- Study 06 wins, in Michroma: wing centred ABOVE the wordmark. That is
  the primary mark wherever there is vertical room. The horizontal form —
  bars to the LEFT of the wordmark — stays as the compact variant for
  tight inline spots, which is the sidebar header.
- AUDITED EVERY SURFACE THAT DRAWS THE WORDMARK, and two of four were
  not even in the right face:
      sidebar        Michroma, horizontal   (already correct)
      settings rail  now Michroma, STACKED
      welcome door   was the body sans -> Michroma
      gate page      was the body sans AND never loaded the font at all
  So a new user's first sight of the logo was a different logo from the
  one inside the app. Both now load Michroma and render at weight 400 —
  the 700 they were using is a synthetic bold Michroma has no cut for.
- Measured after: rail brand and sidebar brand both report Michroma,
  wing 40x16 sits above a 114px wordmark inside a 211px rail, nothing
  clipped.
- SECOND DUPLICATE-ID TRAP TODAY: `#about-name` exists THREE times (the
  update and new-models dialogs reuse it), so
  `querySelector("#about-name")` returns a hidden copy in another veil
  and reports Helvetica at 0px wide. `#about-card` did the same thing an
  hour earlier. The CSS is fine because it is ancestor-scoped; it is
  MEASUREMENT that has to carry the ancestor. Scope every settings query
  to `#set-brand`/`#about-veil`, never a bare id.
- Gauntlet 60/60.

## 6 beta 240 (pending release) — the starter chips were INERT
- They looked right, hovered right, and did nothing. The handler was
  never the problem: `#composer-wrap` is `pointer-events:none` so it
  cannot block the backdrop behind it, and every child that wants clicks
  re-enables them — `#composer` does, `#suggest` never did. The click
  landed on <main> and the chips never saw it.
- PROVED WITH A HIT TEST, not a screenshot:
  `document.elementFromPoint()` at a chip's own centre returned MAIN, not
  the chip. Nothing visual distinguishes an inert control from a live
  one, so this is the only check that can find it — worth reaching for
  any time a control "looks fine but does nothing".
- One line: `#suggest{pointer-events:auto}`. After it, clicking
  "How do I beat jet lag?" asked the question, stripped the emoji,
  cleared the hero and streamed a real answer with 4 sources.
- Gauntlet 60/60.

## 6 beta 240 — the wing goes back beside the C, and maps come back
- OVERLAP REVERTED, per Patrick. The wing sits BESIDE the C with a real
  4px gap. Tucking it under the letterform read as a collision at every
  size tried; two clean shapes next to each other beat one muddled one.
  Measured: gap 4px, not overlapping, Michroma both sides.
- MAPS AND PINS WERE GONE, and the cause was one line. The locality
  handed to the geocoder was "the last two words of the place terms" —
  fine for a long question, catastrophic for a short one:
      "whats some good bbq in bushwick?"  ->  terms "bbq bushwick"
      last two words = "bbq bushwick"     ->  _geocode() = None
  No coordinates meant no OSM venues, no pins AND no map card, on a
  question that names the neighbourhood outright. The earlier bars
  query only worked by luck — it was long enough that its last two
  words happened to be the location.
- The locality is now WHAT REMAINS after removing the venue words and
  the relative-time words, which is the actual place:
      bbq bushwick               -> bushwick
      bars clubs late bushwick ny -> bushwick ny
      coffee williamsburg        -> williamsburg
  The map card's own geocode was reading the same bad string and is
  fixed with it.
- Verified end to end on the reported query: geo step resolves to
  Bushwick, MAP carries 40.694/-73.919, PLACES2 carries a pin.
- HONEST LIMIT: OSM is queried by AMENITY, not cuisine, so "bbq" asks
  for restaurants near Bushwick and the pins are not bbq-specific. The
  prose still names the right places from the web sources; the pins are
  neighbourhood context, not a filtered result.
- THIRD TIME on the stray `*/`: appending explanation to a closed
  comment killed the stylesheet again. The audit caught it before it ran
  this time, which is the only reason it cost seconds instead of an
  hour. Run it after EVERY css comment edit, no exceptions.
- Gauntlet 60/60.

## 6 beta 240 — thumbnails when the question asks for pictures
- "do you have any photos?" returned sources from PEXELS and an answer
  apologising that it cannot show images. The PHOTOS marker, the
  photoRow renderer and the harvesting code all already existed —
  photos were only ever collected on the PLACE path. `run_search` reads
  snippets and never opens a page, so a non-place question had nothing
  to harvest from.
- Now gated on actually asking (`_WANTS_IMAGES`: photo/pic/picture/
  image/screenshot/diagram/"show me"/"look like"), because opening pages
  costs seconds and most questions do not want them. Verified it fires
  on five asking phrasings and none of four ordinary ones.
- AND THE MODEL IS TOLD. Without it, it apologised for being unable to
  show what was already on screen beneath it.
- THREE BUGS FOUND BY RUNNING IT, none of which any amount of reading
  would have caught:
   1. `step()` is defined AFTER the response opens; the search phase runs
      before. Calling it there is an UnboundLocalError — a hard 500 on
      every image question.
   2. `_stash_sources` rewrites rows as {"t","u"}. The harvest read
      "href", got None every time, and ran with an empty URL list. The
      mechanism was never at fault; it was handed nothing.
   3. og:image ALONE IS TOO THIN — two of three real sources never set
      it. Falling back to <img> tags (plus data-src, since lazy loading
      is the norm, and relative URLs resolved against the page) took
      three sources from 1 image to 11.
- FURNITURE FILTER, earned the hard way: the first working run returned
  LANGUAGE FLAGS from a site nav — a photo by every technical measure
  and of no use to anyone. icon/logo/sprite/flag/banner/arrow/social/
  star and the usual chrome paths are skipped now.
- HONEST LIMIT: picking the GOOD image is heuristic. Dimensions are
  unknown without downloading, so a site with unusual markup can still
  surface something dull. og:image is preferred first because it is the
  one image a page has deliberately chosen to represent itself.
- Gauntlet 60/60.

## 6 beta 241 (pending release) — the question keeps the face it was typed in
- The user bubble was inheriting `--helv` (Helvetica Neue) at 23.9
  leading while the composer it was typed into is Space Grotesk at
  21.75 — same 14.5px size, different typeface, so the words visibly
  changed shape the instant you pressed enter.
- Matched to #input on every axis and set to pure white rather than
  --text. Verified all five: face, size, leading, tracking, colour.
- Both panels of the window now speak one typeface: sidebar, answer
  prose and the question bubble are all Space Grotesk; only the
  micro-labels (mono) and the wordmark (Michroma) differ, which is the
  point of having them.
- Gauntlet 60/60.

## 6 beta 242 (pending release) — Compositor label, and sources fold away
- The `.who` line names the ROLE now, then who filled it:
  "**Compositor** Gemma 4 26B", role bold, model not. Whatever the
  ladder actually picked appears there, cloud or local.
- SOURCES ARE TUCKED INTO THE DISCLOSURE once the answer lands, the way
  Claude does it: visible while the work runs, folded away with the
  steps when it settles, so a finished answer is prose rather than
  prose under a pile of chips. The summary now says what is behind the
  chevron — "3 steps · 4 sources" instead of "3 steps · done".
- THE CHEVRON WAS ALREADY DEAD, and this feature could not exist until
  it wasn't. send() re-inserts the worktree card from its outerHTML
  STRING when an answer settles; that parses fresh nodes and drops every
  handler, so the per-element click listener collapseSteps() attached
  had been useless on every finished answer. Proved it before building:
  rebuild the settled state, click the summary, list.hidden never
  changed. Replaced with ONE delegated listener on the chat container,
  which survives any number of innerHTML swaps.
- Measured on a real searched answer: who = "<b>Compositor</b> Gemma 4
  26B", summary "3 steps · 4 sources", 4 chips INSIDE the disclosure,
  0 loose in the body, and the toggle opens and closes.
- Gauntlet 60/60.

## 6 beta 242 (cont.) — one mode picker, and the DMG goes black
NB the in-code markers: this batch is 6b242. A run of `6b243` comments
already exists in millenai.py from the beta-239 commit — previous-me
labelled forward and the counter never caught up. They are wrong but
they are history; left alone rather than rewritten.
- THE SIDEBAR'S TIER LIST IS GONE. Two controls for one setting, a few
  hundred pixels apart: the sidebar dropdown and the composer's engine
  pill opened the same four modes and wrote the same `millen.tier`. The
  composer one wins — it sits with the query, which is where you decide
  how hard to think.
- Removed with it: `build_tier_rows()` and the `__TIER_ROWS__` token,
  `.tier` / `#tier-rows` / `.infobtn` CSS, the fold-open-fold-shut click
  handlers, and the `$$(".tier")` sweeps in setTier and paintTierAvail.
  `#tierpop` STAYS — the composer menu uses it for the hover bubble.
- ⌘K used to enumerate modes by reading the rendered sidebar rows, which
  would have silently emptied. It reads `TIER_META` now: the same object
  the composer picker is built from, so the two cannot drift.
- Verified live: 0 `.tier` in the DOM, picker opens with all four modes
  and Fast marked on, ⌘K still offers all four.
- THE DMG WINDOW IS BLACK with grey/white stars, and the lockup is the
  SETTINGS lockup — not redrawn by eye. Every number in build_dmg.sh was
  measured off `#set-brand` in the running app and written as a ratio of
  the wing height: width 1.1951, gap 0.4268, cap 0.5368, wordmark ink
  6.7982. The wing is SMALL against a long wide-tracked wordmark, and
  that ratio is the whole character of the mark — eyeballing it drifts
  every time.
- The wing is the app's own SVG replayed in PIL: same five bars in
  viewBox units, round caps drawn as end-circles, 4x supersampled, and
  the real objectBoundingBox gradient (steel bottom-left -> silver
  top-right) computed per pixel and masked, not five flat shades.
- MICHROMA IS A WEBFONT — Google-hosted, no local file — so PIL cannot
  set it. Helvetica stands in, sized to the same cap height and tracked
  out to the same ink width, which keeps the proportions but not the
  letterforms. Shipping the TTF in-repo is the only exact fix.
- Dropped the blur/bloom pass: on navy it read as glow, on true black it
  just lifts the whole field to charcoal. Arrow and step 3 went
  greyscale too — nothing coloured is left in the window.
- Gauntlet 60/60 (the "tier dropdown js present" check was asserting the
  thing we deleted; it now guards that the composer picker exists AND
  that the sidebar duplicate has not crept back).

## 6 beta 243 (pending release) — voice chat is parked
- Greyed, not deleted: `#voicebtn` gets `.parked` (opacity .3, not-allowed),
  the click returns early, and `setVoice` forces `on=false` behind a single
  `VOICE_PARKED` flag. Flip that one constant to bring it back.
- THE STALE FLAG WAS THE ONLY REAL TRAP. `voiceChat` initialises from
  `localStorage["millen.voice"]`, so a machine that had voice chat ON
  before the update would have carried a "1" across and kept talking after
  every answer with no visible control to stop it. Boot now writes "0".
- Verified by priming localStorage to "1", reloading, and reading back:
  parked, opacity .3, cursor not-allowed, voiceChat false, stored "0",
  and a click changes nothing. The MIC is untouched — dictation is a
  different feature and still live (opacity 1, cursor pointer).
- WHY, so nobody "fixes" the wrong layer: `_speak()` is not slow. `say`
  is instant. The wait is that voice chat speaks the FINISHED answer, and
  finishing means the whole tier ladder — council, search, compositor.
  Speeding up TTS would do nothing.
- The route back, if it's ever wanted: voice mode pins the Fast tier (one
  model, no search, no compositor) AND speaks sentence-by-sentence off the
  DRAFT stream instead of waiting for `full`. That is seconds, not minutes
  — but it makes spoken answers deliberately dumber than typed ones, which
  is a product decision, not a patch.
- Gauntlet 61/61 (new check guards the parked state and the flag clear).

## 6 beta 243 — the mobile burger was wired to a ghost
- THE CLICK HANDLER WAS FINE. Two media queries fought over the sidebar:
  an older `max-width:760px` block set it `display:none`, and the newer
  `max-width:700px` drawer block only animated `transform`. On a phone
  both applied, display:none won, and the ☰ toggled `body.sbopen` on an
  element that was never rendered. A dead button that LOOKED wired.
  Bonus: between 700 and 760px there was no sidebar AND no burger.
- Merged into ONE 760px block. If a rule ever needs to differ by width,
  it goes inside that block — never a second breakpoint for the sidebar.
- The drawer gets a real ground now (rgba(10,12,17,.92)): the desktop
  34% glass slid over white chat prose and read as text-on-text.
- The open burger sat exactly on the wordmark ("ONCORDE"), and while
  open it is redundant — the exposed strip of chat closes the drawer —
  so `body.sbopen #mburger` fades out and drops pointer-events.
- VERIFYING THIS IN THE PANE HAS A TRAP: the Browser pane is a hidden
  document — document.hidden true, rAF never fires — so CSS TRANSITIONS
  NEVER ADVANCE. The drawer sat at translateX(-105%) with sbopen set and
  the transition "running" at currentTime 0 forever, which looks exactly
  like the bug you just fixed. Inject `transition:none!important`, then
  read positions; the endpoint state is the truth the phone will see.
  (Second trap, again: the dev server bakes the page at boot — edits
  after preview_start are NOT served until restart.)
- Verified at 375px and at 730px (the formerly dead band): open x=0 and
  the hit-test lands on the sidebar, tap-chat closes to -315, burger
  reopens, wordmark unobscured, mic/composer untouched.
- Gauntlet 61/61 (drawer check now guards one-breakpoint + sbopen rule
  + no display:none, so the second block cannot creep back).

## 6 beta 243 — the council loses its wasted minutes
Reviewed run_council + the /api/chat orchestration end to end for speed.
The bones were right (parallel cloud bench, correctly-sequential MLX
loop, one shared join deadline, per-model caps, in-memory dead-model
set). Four real inefficiencies found, all fixed:
- ENGINE PRE-WARM NOW OVERLAPS THE SEARCH. It used to run serially
  AFTER the search and BEFORE the headers: 5-20s of network, then up to
  180s of disk, then the first byte — and the Cloudflare heartbeat only
  starts after the headers, so the load sat in exactly the silent
  window the heartbeat exists to cover. Routing is resolved before the
  search now and the warm-up runs on a daemon thread; run_model's own
  _engine_lock ensure makes the first draft wait if it's still coming
  up. Prep time is max(search, load) instead of search + load.
- THE MERGER DRAFTS LAST. The local loop leaves the LAST engine
  resident, and the merge wants the biggest Gemma — which on this very
  machine is also the roster LEADER, so every Thinking run loaded the
  26B, evicted it for Phi-4 and Nemo, then RELOADED the largest model
  on the machine for reflection + merge. The handler now seats the
  projected merger last (merge_pref_label(), ONE definition shared with
  run_council's pick). Guarded by `not model_name` so a manual pick
  keeps the user's leader.
- THE time.sleep(1.2) IS GONE. Skipped models were a status flash held
  on screen by a literal sleep; they are ledger chips now (only when a
  usable roster remains — with nothing usable the loop tries labels[0]
  anyway, and a skip chip + a real draft for the same model would make
  the contributor count lie).
- THE CLOUD COMPOSITE STREAMS. cloud_only and turbo waited for
  cloud_text to return the ENTIRE composite before showing a byte —
  drafts all in, user staring at "compositing…" for the whole cloud
  generation. It streams now with the _stream_guarded contract: a rung
  that collapses is wiped with RESET and the next rung (or the best
  draft) takes over. Single-provider paths already streamed raw via
  cloud_stream_conf, so this is consistent, not novel.
- NOT touched, deliberately: peer review's second pass per contributor
  (Pro's stated contract), reflection (critique-then-revise beats
  straight merge), the 75s cloud join (a backstop — cloud_text's own
  60s timeout means threads are long dead by then).
- Gauntlet 61/61 (live Fast-tier generation exercises the moved
  routing + threaded pre-warm). Verified against the real roster:
  Thinking resolves [Gemma 26B, Phi-4, Nemo] here, so the reorder
  demonstrably saves reloading the 26B — the machine's biggest model —
  once per council.

## 6 beta 244 (pending release) — the fleet is real, and measured
- PROVED END TO END, twice: a real worker speaking the real protocol
  (register -> auto-approve + token handover -> long-poll -> submit)
  against the dev hub, then a genuine /api/chat Fast query answered BY
  the worker — sentinel text back through the stream, "e2e-rig's GPU is
  on it" status, zero local engine loads. Now a permanent gauntlet
  check (62nd): fleet loopback with turbo parked and restored.
- BANDWIDTH, MEASURED, is a non-issue BY CONSTRUCTION: the job payload
  was 6.8KB down (system prompt + question) and under 1KB up; a searched
  answer might reach ~50KB. Idle costs one register+poll round every
  ~25s (~170 B/s). This design ships whole jobs to a machine that runs
  the whole model locally — only prompt and answer cross the wire. The
  bandwidth-doomed version of this idea (splitting one model's LAYERS
  across homes, activations crossing the net every token) is not what
  Concorde does.
- FIXED A REAL BUG found in review: fleet_run's status() writes to the
  client socket BEFORE the busy-flag cleanup, and a closed tab raised
  through it — skipping the reset. Register PRESERVES busy across
  re-registers (mid-job workers must not be double-booked), so ONE
  dropped stream sidelined that worker forever. try/finally now.
- THE PECKING ORDER, worth knowing: in the single-model path the fleet
  sits BELOW cloud — turbo + healthy key means fleet_run is never
  consulted. On a keyless machine (the actual community) it is first
  in line after local. Councils never offload (single-model jobs only,
  by design).
- Trust flags surfaced, not changed (Patrick's calls): fleet_auto
  defaults to ON, so anyone who knows the hub URL can register a worker
  and will RECEIVE user prompts — and the job payload carries the
  system prompt with the user's MEMORY and persona in it. "Friends
  only" is the documented model; auto-approve is the one-toggle UX
  choice (6bXXX "AUTOMATED, per Patrick").
- 7 real workers sit approved on the hub today.
- Gauntlet 62/62. Test residue cleaned: e2e worker entry removed from
  fleet_workers.json, turbo restored both times.

## 6 beta 244 — copy button on code cards
- Every code card's bar carries "copy" at the right, in the bar's own
  mono caps. GREYED WHILE THE FENCE IS OPEN: renderMD's fence regex
  third group is the closer — ``` or $ — so a block still streaming
  renders `.ccopy.wait` (opacity .32, disabled) and flips live the
  chunk the closing fence lands. Zero state tracking; the re-render IS
  the state machine.
- Lang-less fences get the bar now too (label "code") — the button
  needs a home. Fenced tables and ```flow diagrams stay button-free.
- DELEGATED handler on `inner`, same reason as the chevron (6b242):
  streaming re-renders via innerHTML kill per-element listeners within
  the second. Copies `pre code` textContent — the un-highlighted raw —
  then flashes "copied" for 1.2s.
- THE GAUNTLET'S FLEET CHECK WENT FLAKY and the cause is worth keeping:
  the hub hands a worker its token ONCE (the claim is marked claimed);
  a known wid arriving tokenless is an imposter and parks in pending.
  Correct security — but the test's fixed wid worked exactly once. The
  worker now persists its (wid, token) pair in the temp dir and mints a
  fresh identity when the cache is gone.
- Gauntlet 63/63 (new check: ccopy present, wait state wired).

## 6 beta 245 (pending release) — Kimi K3 joins as the 4th provider
- K3 IS NOT A LOCAL-CATALOG CANDIDATE and never will be: 2.8T-param MoE
  (104B active per token), open weights under Modified MIT, ~64 H100s
  to self-host. It joins the CLOUD side instead: provider id "kimi",
  base https://api.moonshot.ai/v1, OpenAI-compatible — the existing
  streaming/compat path speaks it unchanged.
- Discovery-first saves us from id guesswork: the default "kimi-k3" is
  corrected by the /models inventory on key save (prefs_order: k3,
  kimi-latest, k2). WIRING PROVEN LIVE without a key: a deliberately
  invalid probe through /api/cloud/set came back with Moonshot's own
  "Invalid Authentication" — base, headers, discovery and the
  provider's-own-words error path all real. cloud.json snapshotted and
  restored around the probe.
- Seated in the pecking order as PAID: compositor ladder is now claude,
  kimi, gemini, groq; the bench fields ONE kimi seat (no blind
  alternate — same rule as Anthropic, it bills per token).
- Touchpoints (the full 4th-provider checklist, for next time):
  KEY_SHAPE ("sk-", floor 40 — Moonshot keys are OpenAI-styled bare
  sk-, no vendor infix; per-selected-provider check so no sk-ant-
  collision), _provider_of (moonshot -> kimi), spec map, prefs_order,
  cloud_bench paid-skip, compositor_ladder tuple, dropdown option,
  CK_PROVS board row, ZITO PROV map + ladder array.
- Gauntlet 64/64 (new check: dropdown + board carry Kimi K3).

## 6 beta 245 (cont.) — tier audit: three defects found and fixed
- PRO WAS SEATING LLAVA ON TEXT QUESTIONS. BLEND_EXCLUDE exists to keep
  the vision model out of text councils and take_all bypassed it — a 7B
  vision model spent a whole engine swap drafting prose. Excluded now;
  images still route to LLaVA directly before tiers resolve.
- THINKING COULDN'T SEAT THE INSTALLED REASONING MODEL. Picks named
  "DeepSeek R1 7B" (MLX distill); this machine holds the "DeepSeek R1"
  ollama row — same brain, different label — so the reasoning tier
  blended a plain Nemo instead. Both labels are in the picks now.
- THE MERGE WAS CHOPPING FRONTIER DRAFTS TO STUMPS (Patrick: "will
  Gemma distilling ruin it?"). The 1500-char per-draft cap exists for
  small local mergers — repetition loops, seen in the wild — but it
  also applied when Claude/Kimi K3 wrote the composite, so a frontier
  draft was truncated to 1500 chars before a frontier compositor read
  it. Two cuts now: cloud rungs get 6000 chars/draft (their contexts
  are six to seven figures), the local merger keeps 1500.
- THE ANSWER TO "does Gemma ruin Kimi": mostly no, BY THE LADDER — with
  turbo on, the composite is written by Claude first, then Kimi, then
  Gemini/Groq; local Gemma only writes when every cloud rung fails.
  The residual flattening case: all-cloud-rungs-down mid-run, Gemma
  rewrites a pot that contains a K3 draft. Option (NOT implemented,
  Patrick's call): in that case ship the strongest cloud draft verbatim
  instead — precedent exists (Cloud Only does exactly this).
- Where Kimi sits per tier, once a key is saved: Fast = only if Moonshot
  is the ACTIVE provider (turbo streams one provider); Thinking/Pro =
  drafts on the bench (one seat, no paid alternate) + compositor rung 2;
  Cloud Only = bench + rung 2.
- Gauntlet 64/64.

## 6 beta 245 (cont.) — "where is this turbo mode?"
- IT'S THE "USE CLOUD POWER" CHECKBOX, Settings › Cloud power. "turbo"
  is only the pref key; the one place the internal name leaked to the
  screen was the generation status line ("turbo — Gemini") — reworded
  to "cloud power — …" so the status speaks the switch's name.
- FRESH-INSTALL TRAP FIXED: the key box folded away when cloud power
  was off, and the toggle hides until a key is configured — so a fresh
  machine's Cloud power pane was EMPTY, with no way to paste the first
  key. The box now opens while the feature is on OR while nothing is
  configured; it folds only for someone who has keys and switched it
  off. (Never seen on this machine because turbo has been on forever.)
- Gauntlet 64/64.

## 6 beta 245 (cont.) — the spec list speaks one voice
- VERSION and MODELS rendered in different type than CHIP/MEMORY/ACCEL
  inside #set-spec: two STALE ID RULES from the pre-rail About layout —
  #about-ver (Helvetica 14px) and #about-facts (mono 11.5px bold,
  margin-top:10px) — outranked the list's shared mono 9.5px. The fix is
  deletion: #up-ver keeps its rule (the update dialog still uses it),
  #about-facts has no rule at all now. Measured after: all five rows
  IBM Plex Mono / 9.5px / 400 / 0 margin.
- The recurring lesson (third time now, after the duplicate #about-card
  and #about-name ids): the rail redesign REUSED old element ids, and
  every rule that ever targeted those ids is still live. When a row in
  a shared list looks wrong, grep the ID before touching the list.
- Gauntlet 64/64.

## 6 beta 246 (pending release) — Fast rides the speed ladder, and Kimi shows its money
- FAST PREFERS A FAST CLOUD MODEL OVER ANY LOCAL LLM (per Patrick).
  fast_cloud_ladder(): groq -> gemini -> kimi -> claude, SPEED order
  not strength order — Groq's LPUs first, Gemini's pick is already
  flash, Claude last and downshifted to haiku when the inventory has
  one (Fast fires constantly; frontier tokens don't belong in it).
  Every healthy rung gets a try before local silicon; the old path took
  ONE shot at whichever provider was "active" — which could be the
  slowest paid one — and dropped straight to local when it hiccuped.
- The turbo gate now rides the ladder, not cloud_conf(): a dead ACTIVE
  key used to skip cloud entirely while a healthy second key sat
  unused.
- PROVEN LIVE, including the fallthrough: one run answered via Groq in
  0.7s; the next caught Groq mid-hiccup and the ladder walked to Gemini
  ("cloud power — Groq 120B" then "cloud power — Gemini"), 7.7s total,
  answer intact. The old code would have run Gemma locally instead.
- Fast's hover bubble names the actual rung now: "✓ Cloud Enabled —
  Groq 120B answers first, this machine is the fallback."
- BALANCES, honestly: only Moonshot exposes money to a normal key
  (GET /users/me/balance). The board shows it — measured live, "Kimi K3
  ✓ · $25.00 left" off Patrick's real account — cached 5 minutes.
  Anthropic exposes cost only to an org ADMIN key; Groq and Gemini are
  dashboard-only. Those rows show nothing rather than something
  invented.
- Gauntlet 64/64.

## 6 beta 247 (pending release) — the Kimi seat actually answers
Patrick's screenshot: Cloud Only, "KIMI K3 (no answer — cloud)". Three
distinct defects stacked on that one seat, all found by replaying the
EXACT bench payload against Moonshot and reading the body the app
swallows:
- TEMPERATURE: Moonshot pins each model's legal temperature — the bench
  payload's 0.75 got 400 "only 1 is allowed" on EVERY council call,
  while the save-probe (which sends no temperature) showed a green ✓.
  Probe and runtime payloads differing is the same class of bug as the
  b233 Groq tick. cloud_text and cloud_stream_conf now OMIT temperature
  on moonshot bases; the server default is always legal.
- STALE PICK: discovery ran once, at key save — when Patrick's account
  (pre-funding) exposed only k2 models, so the stored pick was
  kimi-k2.7-code, a CODE SPECIALIST, forever. kimi-k3 appeared in the
  inventory after funding and nothing ever looked again.
  _cloud_refresh_picks() now re-discovers every healthy provider once
  per boot (background thread off the _cloud_repair latch) and upgrades
  picks via CLOUD_PICK_ORDER — ONE policy table shared with the save
  handler. Stored conf healed by hand for the interim (snapshot kept).
- "pro" IS A SUBSTRING OF "prompt": the alternate-seat heuristic put
  llama-prompt-guard-2-22m — a 22M SAFETY CLASSIFIER — on every Groq
  council as the "stronger sibling". Word-boundary match now, and
  guard/moderation ids are skipped at inventory level entirely.
- Verified live end to end: Cloud Only bench = Groq 120B, Gemini,
  Claude, Kimi K3 — all four answered, claude-sonnet-5 composited,
  15.2s. Boot refresh then purged the junk inventories on its own and
  upgraded Gemini's pick to gemini-3-flash-preview.
- Gauntlet 64/64.

## 6 beta 247 (cont.) — the wine question grew a map of France
Patrick's screenshot: a health question about daily wine rendered a
world map pinning "Short term", "Liver" and "Brain". The chain, fully
traced:
- WINE IS A _PLACE_NOUN (so "best wine bar in bushwick" searches
  properly), so a quality word + a consumable classified the health
  question as a venue ask -> bookish -> the [[PLACES]]/extraction
  machinery engaged -> the extractor read the ANSWER'S SECTION HEADINGS
  as venue names (they pass the exists-in-answer check, because of
  course they do) -> the geocoder pinned them, because BRAIN IS A REAL
  COMMUNE IN FRANCE.
- THREE GATES NOW, layered:
  1. _NOT_PLACEY_RX — "bad/good/healthy/safe/… for you/health",
     health words, "why is X bad" — forces placey=bookish=False AFTER
     the follow-up threading step, so a glued-on entity from a previous
     venue turn can't resurrect the machinery. Tested both sides: five
     health phrasings blocked, four venue asks untouched (the qzxvbn
     placey gauntlet test still passes).
  2. PLACEHINT only emits when placey/bookish — for a plain searched
     answer the "venues" mined from article titles are headline
     fragments.
  3. CLIENT COHERENCE: pins wider than 250km apart = garbage in, no
     map. Real venue answers share one metro; junk names geocode
     SOMEWHERE on every continent.
- Verified live twice: "is a glass of wine a day actually good for you"
  and the reported "why is drinking bad for you" — SOURCES only, no
  MAP, no PLACES2, no PLACEHINT, grounded prose answer.
- Gauntlet 64/64.

## 6 beta 247 (cont.) — the four-step first-run wizard
- #wiz-veil over the app, once per machine (prefs.wizard_done; skip
  counts). The boot gate: needs_setup && IS_LOCAL && !wizard_done ->
  openWizard(); done/skipped machines fall back to the plain download
  panel. Remote visitors never see either.
- Step 1: the stacked lockup at hero size (wing SVG + Michroma wordmark
  + version), two paragraphs — what Concorde is, and "this takes a
  minute".
- Step 2: one paragraph on LLMs + compositing, then Basic/Pro/Max cards
  priced from /api/setup plans (live GB, "installed ✓" when owned), and
  the ignore-system-limits checkbox riding the existing no_limits pref
  (re-prices the cards on toggle).
- Step 3: one paragraph on cloud power, then the four providers —
  checkbox left, free/paid tag, "get a key ↗" (aistudio / console.groq /
  console.anthropic / platform.moonshot), checking reveals paste + Save
  through /api/cloud/set. Connected providers show ✓ instead.
- Step 4: thanks + "Let's go" -> /api/setup/install with the chosen
  plan, wizard_done, and the OLD setup veil takes over for the progress
  bar it already draws well. Nothing was duplicated: plans, install,
  keys, no-limits all ride the existing endpoints.
- Traps hit: .about-btn's display:block beats the UA [hidden] rule
  (Back showed on step 1 — same fix as every veil: an explicit [hidden]
  rule); the key input needed min-width:0 + border-box or the card
  grew a horizontal scrollbar.
- Verified: all four steps walked live; fresh-machine branches proven
  by stubbing /api/setup + /api/cloud (GB prices render, checkbox
  reveals the key row, right links, right placeholders); skip writes
  wizard_done and the gate honours it. Gauntlet 65/65.

## 6 beta 248 (pending release) — the hosted page can never go stale again
- Patrick: "i dont think the hosted web ui is up to date". IT WAS — the
  local :9889 self-updated to 247 on its hourly tick, the tunnel door
  was byte-identical to a locally simulated remote request, and a
  signed-in remote fetch titled beta 247. The staleness was HIS
  BROWSER: the app page shipped with NO cache headers, and mobile
  browsers heuristically cache such pages for days.
- ETag = "b<APP_BUILD>" + Cache-Control: no-cache on the app page.
  Every load revalidates: unchanged build = instant tiny 304, new
  build = full fetch. Verified all three: 200+ETag on first fetch,
  304 on matching If-None-Match, 200 on a stale one.
- Diagnosis path worth keeping: door pages hide the version, so
  compare builds by simulating remoteness against localhost
  (curl -H "X-Forwarded-For: …" — and a cookie with any 20-hex
  millen_user gets the app page, which titles its build).
- Gauntlet 65/65.

## 6 beta 248 (cont.) — one row of starter chips
- rows.slice(0,2) -> slice(0,1) in paintSuggest's measurement pass (per
  Patrick: single row, even if only 3-4 fit). The measured-not-guessed
  approach did all the work — one character changed. Verified live:
  3 chips, 1 row at 780px.
- Gauntlet 65/65.

## 6 beta 248 (cont.) — the Advanced council
- ⚙️ Advanced sits under a thin rule (.engdiv) at the bottom of the
  engine menu. Its veil: every READY local model with a checkbox and a
  small grey-italic best-use line (ADV_USE map; LLaVA excluded — vision
  routes itself), the four cloud providers (keyless ones greyed with
  "no key — add one in Settings"), then the COMPOSITOR dropdown —
  Automatic, each keyed cloud, and the local Gemmas — with a guidance
  line per pick (research/writing -> Claude, long docs/code -> Kimi,
  quick general -> Gemini, speed -> Groq, privacy -> local).
- Wire contract: the request carries models + cloud + compositor.
  cloud=None means no opinion; cloud=[] means EXPLICITLY none — it
  suppresses the fast ladder, the bench, and (caught live) the free
  community cloud, which fired on the first pass because its elif only
  checked the turbo pref. Naming providers IS the opt-in: a custom run
  engages its clouds even with turbo off.
- Compositor override: local label -> merger=comp, no cloud ladder at
  all (the user chose a private pen); provider id -> the ladder narrows
  to that one provider, engaged even without turbo. The handler's
  merger-last roster reorder follows the override.
- State: localStorage millen.adv + millen.advon; chip reads "Custom";
  picking any real tier exits custom (the boot call with the stored
  empty tier keeps it). Save requires ≥1 local model — pure cloud is
  what ☁️ Cloud Only is for, and the note says so.
- Verified live: menu row + divider render; picker lists 10 locals /
  4 clouds / 7 compositor options; save -> chip Custom, stored JSON
  correct; a 1-local/no-cloud run answered locally in 4.9s with zero
  cloud attempts. Gauntlet 66/66.

## 6 beta 249 (pending release) — the Remote SSH agent
Patrick's ask: his competitor's AI edits a VPS over SSH; roll the same
into the Code tab so "help me set up a VPN on my VPS" asks the right
questions and grinds. Built as a "Remote" agent (🛰️) beside Coding and
Workspace.
- LOOP: plan -> run one command over SSH -> read output -> repeat, up to
  REMOTE_CAP=40. The driver is the STRONGEST available brain
  (compositor_ladder first — cloud when keyed — else the best local
  coder); agentic multi-step work needs it. Each command + exit code
  streams into the same activity tree the council uses.
- TRANSPORT: shells out to the system ssh binary, KEY-FIRST. BatchMode
  means a password prompt can never hang the loop; a keyless box fails
  with a clean "ssh-copy-id …" nudge. accept-new host key, 12s connect
  timeout. The app never invents a target and never takes a secret —
  the user saves their own host/user/port/key (remote.json, 0600,
  owner-only, never over the tunnel).
- THE AUTONOMY THROTTLE (the "be creative" bit): three escalating
  segments — 🔒 Manual (approve every command) / ⚡ Auto (reads run,
  changes ask) / 🔥 Full (grinds, pauses only for irreversible). Cool
  grey -> amber -> hot red left to right; Full pulses. Stored in
  millen.autonomy, sent as `autonomy`.
- THE REAL GATE is classify_cmd() -> read|write|danger, unit-tested
  36/36 including rm -rf /, mkfs, dd, reboot, fork bombs, and compound
  commands (a pipeline takes its riskiest segment). Auto pauses on
  write+danger; even FULL always pauses on danger — a floor no mode
  crosses. Guarded in the gauntlet over the wire via /api/remote/classify.
- APPROVAL CHANNEL: the loop emits an APPROVE marker and blocks on an
  Event (the fleet-job pattern); the client shows a Run/Skip card with a
  risk chip; POST /api/remote/approve sets the Event and the loop
  continues (or feeds "user declined" back to the model). 600s window.
- SECURITY: the whole feature is owner-only. A tunnel guest selecting
  Remote gets "owner's machine only"; every /api/remote/* is in
  ADMIN_PATHS AND re-checks _remote() in its handler.
- VERIFIED: classifier 36/36 (unit) + over the wire (gauntlet); ssh argv
  construction; config save/read roundtrip with the key; bogus-host test
  fails in ~5s, NO hang; the full /api/chat loop reaches the connect
  step and returns the guided failure in 5s; the approval card renders
  read/write/danger and POSTs {jid,ok} correctly.
- NOT YET EXERCISED (needs a real reachable VPS — the WebKit-pass
  equivalent): the live multi-command grind with the model driving, and
  the end-to-end approval gate through an actually-running command. The
  mechanism is the proven fleet Event pattern; only the command phase
  is untested from here. Ship, then shake out against a real box.
- Gauntlet 69/69.

## 6 beta 250 (pending) — first LIVE remote-agent run + classifier polish
- PROVED END TO END against a real DigitalOcean droplet (Ubuntu 26.04):
  the Remote agent, driven by Claude Sonnet 5, set up a full WireGuard
  VPN autonomously in 17 commands, all clean. Recon-first (OS, ifaces,
  firewall, SSH port — it checked the SSH port BEFORE touching ufw so it
  couldn't lock itself out), then install, keys, wg0.conf with NAT +
  MASQUERADE, client config, ufw (kept 22 open), DEFAULT_FORWARD_POLICY
  DROP->ACCEPT, systemctl enable --now. Independently verified over SSH:
  service active+enabled, wg0 up on 51820, peer present, forwarding on,
  firewall correct. NOT the agent's word — my own probe.
- Key bootstrap stayed the user's hands (the app is BatchMode key-only,
  and I don't handle plaintext passwords): generated a throwaway
  keypair, wrote remote.json, user ran one ssh-copy-id. Background
  harness (scratchpad/remote_grind.py) waited for the key then drove the
  real run_remote_agent with a danger-DENY approval callback.
- CLASSIFIER BUG the live run exposed: `_WRITE_RX` carried a DUPLICATE
  redirect pattern `>>?\s*[^&\s]` WITHOUT the /dev/null exclusion the
  dedicated _classify_seg check has — so every `cmd 2>/dev/null` recon
  line (nearly all of them) read as a mutation. In Full auto it didn't
  matter (writes run), but Auto would have paused on pure inspection.
  Removed the duplicate; redirects are handled once, with the exclusion.
- Also widened the read set: lsb_release, apt-cache, dpkg-query, getcap,
  needrestart, and wg (safe subs show/showconf — genkey/set stay write;
  wg needed to be in BOTH _READ_CMDS and _READ_SAFE_SUB, like systemctl).
- Re-verified 17/17 incl. the two real recon lines now 'read', every
  write still 'write', full danger floor intact. Gauntlet 69/69.
- Reminder for Pat: destroy that test droplet (root pw was pasted in
  chat); client1.conf holds a live private key.

## 6 beta 250 (pending) — task library, guided flows, batching, risk cards
- THE CODE TAB'S CHIPS BECOME SERVER TASKS. 53 of them, Patrick's list
  verbatim, across 7 categories (Security 12, Updates 7, Monitoring 9,
  Services 8, Networking 6, Storage 6, Setup 5). A "⋯" chip opens a
  rail/pane picker (categories left, tasks right, live search) styled
  like Settings. Chips are lane-aware — syncSuggest repaints on every
  Chat<->Code switch via box.dataset.lane.
- RISK CARDS (per Patrick): 22 tasks carry a `w` note and render a small
  GREY warning triangle — grey not red, it's a heads-up not an alarm.
  Clicking one shows a card FIRST (large grey triangle, bold "This task
  has a higher risk of causing issues that may be challenging to undo",
  a plain-language paragraph on the actual failure mode) with "🤞 Let's
  go for it" / "🙅‍♂️ Not today". Nothing is sent until confirmed;
  declining runs nothing at all. Unflagged tasks skip the gate entirely.
- THE LOCKOUT RULE is now taught to the agent, not just described in
  copy — Patrick's insight that one pattern covers most of the ⚠️ list.
  REMOTE_SYSTEM gained five numbered rules for any sshd/firewall/network
  change: never end your own session; permissive change BEFORE the
  restrictive one; verify with a SECOND connection while the old path
  still works; arm an automatic revert (systemd-run/at/backgrounded
  sleep) that fires in 5-10 min unless confirmed; say what you're
  protecting against.
- INTERACTIVE FORM CARDS: the model ends a turn with a [[FORM]] trailer
  ({"q","multi","opts"}) and the reader answers by CLICKING — radios for
  one-of, checkboxes for many-of. The trailer is stripped from the prose;
  the answer posts as a normal user turn. TASK_GUIDE (server-side, added
  to Code-lane system prompts) teaches the voice: warm opener, ONE
  question per turn, forms only where options are small and discrete.
- MULTI-STEP BATCHING: the model may answer {"plan","cmds":[...]} for
  2-6 independent steps. ONE approval covers the batch, priced at its
  RISKIEST member (never averaged — verified), the card lists every
  command so a tap is never blind, and execution stops the moment a step
  fails. Single-command form unchanged.
- Verified live: 53/22 counts and category split; picker filtering +
  search; one-row chip trim with "⋯" always surviving; risk card gates
  (nothing sent), declines silently, proceeds on confirm; unflagged
  tasks bypass; form multi-select accumulates, radio replaces, answer
  posts as "Security, Low maintenance", card locks; [[FORM]] trailer
  parsed AND stripped from prose; batch parser + risk aggregation.
- rAF NEVER FIRES IN A HIDDEN DOCUMENT — the chip trim needed a
  setTimeout fallback beside requestAnimationFrame or it silently never
  ran in the Browser pane (same trap as the drawer transitions).
- Gauntlet 75/75.

## 6 beta 251 (pending) — live droplet shakeout + prereq cards
- DROPLET VERIFICATION of the 6b250 remote-agent work, and it earned its
  keep — two real bugs the live run caught, both fixed:
  1. _parse_action was REGEX-based and choked on the JSON the model
     legitimately produces — heredocs, [ini sections], {awk braces} in
     command strings truncated the match. Replaced with a balanced,
     string-aware brace scanner (_json_objects). 6/6 hard cases pass.
  2. A transient empty turn (cloud_text swallows a 429/timeout as "")
     used to END the whole run mid-task. Now the loop retries the turn
     up to 4x with backoff and, if it truly gives up, says "keep going"
     rather than dying silently.
- LOCKOUT RULE PROVEN IN THE WILD: asked to install fail2ban, the agent
  (Claude Sonnet 5) whitelisted the connecting IP in ignoreip FIRST and
  tried to arm an 8-minute systemd-run rollback timer — exactly the
  taught pattern. Independently verified on the box: fail2ban active,
  sshd jail protecting SSH (118 fails, 1 attacker already banned),
  ignoreip carries the SSH source IP, no rollback timer left armed.
- ARCHITECTURE ANSWER (per Patrick's gate): long jobs need NO install
  (systemd-run, present everywhere — the agent reached for it live);
  reboot survival's minimal form is Concorde-side reconnect, but the
  FOOLPROOF form wants a small server-side helper. So: yes, build the
  prereq card, scoped tightly to reboot/long-job tasks only.
- PREREQ CARD: a grey beetle (BUG_SVG) where the triangle was, a short
  why, then "Required tools" — each as a mono `name` + plain
  description (concorde-resume: reconnect after reboot; tmux: survive a
  dropped connection on a long step). Chained AFTER the risk card via a
  `stage` counter: risk -> prereq -> send. Only 3 tasks carry `req`
  (distro upgrade [reboot+long], SELinux/AppArmor [reboot], persistent
  mount [reboot]) — deliberately NOT overused for normal task packages.
- NOT YET BUILT (the actual parity engine): systemd-run long-job polling
  and the reconnect-after-reboot loop in run_remote_agent, plus the
  agent actually installing concorde-resume as its opening move. The
  card fronts this; the execution wiring is the next build, verifiable
  against the same droplet.
- Gauntlet 76/76.
