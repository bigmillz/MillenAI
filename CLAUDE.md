# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**ConcordeAI** — a local-first AI desktop app for macOS and Windows. It runs LLMs on the
user's own machine (MLX on Apple silicon, Ollama elsewhere), optionally blends in cloud
models, and composites several drafts into one answer.

Two things make this repo unlike most:

1. **The entire app is one file.** `millenai.py` (~16.8k lines) is the HTTP server, the
   model orchestration, *and* the whole client — HTML, CSS and JS live inside it as Python
   string constants. There is no bundler, no framework, no `node_modules`. Stdlib only
   (`http.server` + `socketserver.ThreadingTCPServer`), plus optional `psutil`, `ddgs`,
   `mlx_lm`, `pywebview`.
2. **`NOTES.md` (~3.9k lines) is the real documentation.** It is a chronological
   engineering log, one entry per build, recording what broke and why. Most non-obvious
   code has a `(6bNNN, per Patrick: …)` comment pointing at a NOTES entry. **Read the
   relevant entry before "fixing" something that looks odd — it usually isn't.** Append a
   new entry for each build you cut.

The repo lives in Google Drive, which has silently rolled `.git` back before. **Always
`git fetch` and compare against origin before releasing.**

## Commands

Everything runs on the app's own venv. A bare/Homebrew Python is missing `psutil` and
`ddgs`, which silently degrades web search and `/api/setup` and sends you chasing ghosts.

```bash
VENV="$HOME/Library/Application Support/MillenAI/venv/bin/python3"
```

### Dev server

```bash
MILLENAI_PORT=9894 MILLENAI_KEY=smoketestkey123 MILLENAI_HEADLESS=1 \
  "$VENV" millenai.py > /tmp/dev.log 2>&1 &
```

`MILLENAI_HEADLESS=1` suppresses the window and `webbrowser.open`. Port convention:
**8889** desktop app, **9889** the always-on hosted instance, **9894** dev. Ports
**8884–8930 are reserved for model engines** — never bind a server there.

The page is assembled at request time but **the process reads `millenai.py` once at
import**, so *any edit needs a server restart*. Kill by port, never `pkill -f`:

```bash
kill $(lsof -tnP -iTCP:9894 -sTCP:LISTEN)
```

`pkill -f "MILLENAI_PORT=9894"` does **not** work — env assignments aren't in Python's
argv, so it kills the wrapper shell, orphans the server, and the next start fails to bind
while you unknowingly test stale code.

### Tests — the "gauntlet"

`tests_smoketest.py` is not pytest. It is a linear script of ~81 `check()` call sites (a
few sit in loops, so the scorecard reads a little higher) run against a **live server it
does not start**. Start one on 9894 first (above), then:

```bash
cd "/Users/patrickmiller/My Drive/Downloads/files"
"$VENV" tests_smoketest.py > /tmp/gauntlet.log 2>&1; echo "exit=$?"; tail -20 /tmp/gauntlet.log
```

**Never pipe the run into `tail`** — you get *tail's* exit code, and a red run has slipped
into a release that way. It must run from the repo root (it opens `millenai.py`
relatively). Full runs take minutes because they load real engines and stream real
answers. Exits 0 only on a clean sweep.

It asserts against three surfaces, which is why a UI change can fail a test:
- **live HTTP** (auth, owner-only lockdown, range serving, the risk classifier),
- **the served HTML** — many checks are exact CSS/JS substring assertions guarding
  against UI regressions,
- **the source text** (`_MILLENAI_SRC`) for server-side Python that never reaches the page.

**Running one check:** there is no selector. Assert over the wire instead:

```bash
curl -s -b 'millen_key=smoketestkey123' \
  'http://127.0.0.1:9894/api/remote/classify?cmd=rm%20-rf%20/'     # {"risk":"danger"}
curl -s -b 'millen_key=smoketestkey123' http://127.0.0.1:9894/ | grep -c 'barBreathe'
```

A test instance is **not isolated** — it shares `prefs.json`, `chats.json` and the 88xx
engine ports with the desktop app, so a run can evict the app's resident engines or flip
its prefs.

### Release

```bash
./release.sh 6.0.0        # explicit version, or patch|minor|major
```

Needs `gh`. In order it: bumps `APP_VERSION`/`APP_BUILD` **in `millenai.py`** (the only
place either is stored), builds the DMG and the Windows zip, commits, pushes, creates a
GitHub release, then updates the hosted instance. GitHub Actions
(`.github/workflows/windows-installer.yml`) attaches the x64 MSI a few minutes later.

Two things to internalise:
- **The release tag is `v<APP_BUILD>`, not the version** (`v256`). `APP_BUILD` is a
  monotonic counter and is what the in-app updater compares; the marketing version can
  move independently.
- **While `APP_BETA = True`, releases publish as prereleases.** `/releases/latest`
  excludes prereleases, so stable users are held back and only those who tick
  `beta_updates` are offered them. This is deliberate, not a bug.

## Architecture

### Request lifecycle (`/api/chat`)

One request owns one thread for the whole generation. Roughly:

1. Parse body; **alias retired tier names** (`Smart`/`Best` → `Fast`, `Power` → `Pro`) so
   old clients keep working.
2. Resolve the council from the tier. If it has >1 model, the projected compositor is
   moved to the **end** of the roster so the merge stage finds it already resident instead
   of reloading multi-GB weights.
3. Agents / images / attached docs each **hijack the council** (images force the vision
   model and disable search).
4. **Route resolution and MLX pre-warm happen *before* the web search**, on a background
   thread, so a disk load and a network fetch overlap — and headers go out immediately,
   because the Cloudflare heartbeat only starts after headers.
5. Search branches into weather / OSM places / deep research / plain search. Results land
   on a **thread-local** (`_tl_search`) that must be cleared per request, since keep-alive
   reuses threads and stale rows leak between questions.
6. Stream, then a `finally` block emits map/place pins, logs a line to `quality.jsonl`,
   and extracts memories on a daemon thread.

### The wire protocol

The response body is plain text with **out-of-band control frames**: `\0TAG:json\0`
inline in the stream (`NUL = chr(0)`). Tags include `STATUS`, `STEP`, `DRAFT`, `RUN`,
`SOURCES`, `PHOTOS`, `MAP`, `PLACES2`, `PLACEHINT`, `CTX`, `APPROVE`, and bare `RESET`
(throw away the answer already on screen). The client strips them so they never appear in
prose. Two custom headers matter: `X-Web-Search` and `X-Models`.

A **heartbeat thread** re-sends the last `STATUS` when the stream goes quiet >20s —
Cloudflare drops a silent proxied response after ~100s, and an engine swap is a
multi-minute silence.

### The client

`HTML_CONTENT` is a giant string with `__TOKEN__` placeholders substituted server-side at
request time (`__TIER_META__`, `__CHIP__`, `__APP_VER__`, `__MEM_LABEL__`, `__SKY_*__`,
etc). A gauntlet check fails on any unreplaced token. The page is served with
`ETag: "b<APP_BUILD>"` + `no-cache`, so browsers revalidate and a new build always lands.

Three lanes — **Chat**, **Code**, **Funnels** — share one composer; `uiMode` selects
which. Client state lives in `localStorage` under `millen.*` keys.

### Models and the council

- **`CATALOG`** declares every model (engine, port, memory, size). `MODEL_ROUTES` picks
  MLX on Apple silicon, else Ollama.
- **`TIERS`** (Fast / Thinking / Pro / Cloud Only) → `resolve_tier()` returns concrete
  models that are *downloaded and fit in RAM right now*.
- **`run_council()`**: local models draft **sequentially** — only one MLX engine can be
  resident, so parallel loads would thrash — while every healthy cloud key drafts **in
  parallel** on threads. Then optional reflection/peer review, then a **compositor**
  writes the single final answer. Per-model and whole-loop timeouts mean a straggler is
  simply absent rather than holding the answer hostage.
- **Two ladders, different orders**: `compositor_ladder()` is strength-first (Claude →
  Kimi → Gemini → Groq); `fast_cloud_ladder()` is *speed*-first (Groq → Gemini → Kimi →
  Claude-as-Haiku) for the single-answer path.
- Cloud keys live in `cloud.json`. Providers are validated *and* model-discovered on save,
  and rested on a cooldown when quota-limited — a resting provider is healthy, not broken.

### Remote SSH agent (Code lane)

A plan → run → read → repeat loop over `ssh`, **key-only** (`BatchMode=yes`; it cannot use
passwords by design). Every command is classified `read` / `write` / `danger`, and the
autonomy level (Manual / Auto / Full) decides what needs approval — `danger` always does.
Approval round-trips through an `APPROVE:` frame and blocks on a `threading.Event`.

Long jobs run detached under `systemd-run --no-block` and are polled, so a 40-minute
upgrade doesn't hit the per-command timeout; the job **writes its own exit code to a file**
because `--collect` deletes the unit before systemd can be asked. Reboots are a first-class
action: the agent issues one, waits for the box, reconnects and continues.

### Identity

The server binds **127.0.0.1 only**; remote access is always a tunnel. Requests carrying
`Cf-Connecting-Ip`/`X-Forwarded-For` are "remote" and get a different surface: they must
mint an identity (guest or PIN), see only their own chats, and are refused every
owner-only endpoint (downloads, updater, TTS, log access). Per-user data is separated by
`_data_base()`.

## Traps

These are the ones that have bitten repeatedly. Most are invisible from the code.

**Verification**
- The Browser preview pane is **Blink**; the shipped desktop app is **WKWebView**. A
  rendering fix verified only in the pane can be wrong in the app.
- The pane is a **hidden document**: `rAF` never fires and CSS transitions never advance,
  so working code looks dead. Inject `transition:none!important` and read the endpoint
  state, or use a `setTimeout` fallback beside every `rAF`.
- Screenshots and computed style lie. Measure — but measure the *right* quantity;
  `getBoundingClientRect` returns the box, not the ink. Use `document.elementFromPoint()`
  to prove a control is actually hittable (a `pointer-events` bug made chips look fine and
  do nothing).

**The single-file client**
- A shared `let` referenced by earlier code throws in the temporal dead zone and **kills
  the entire page silently**. Downstream console errors are noise from the one real abort.
- Several ids (`#about-card`, `#about-name`, …) are **duplicated across dialogs**, so a
  bare `querySelector` returns a hidden copy that measures 0×0. Scope measurement to the
  ancestor. Old id rules from previous layouts are still live and still outranking new ones.
- Appending text after an already-closed CSS comment leaves a stray `*/` that kills that
  rule **and everything below it**. Audit `<style>` blocks after any comment edit.
- The `hidden` attribute is only the UA stylesheet's `display:none` — any author rule
  setting `display` outranks it. Elements that declare `display` need their own `[hidden]`.
- Streaming replaces `innerHTML`, dropping every per-element listener. **Delegate** from a
  container instead.
- Duplicate declarations silently win by order — two CSS rules for the same thing, two
  media queries fighting over the sidebar.

**Editing and shipping**
- Anchor-based scripted edits fail **silently**: a non-matching anchor makes `.replace()` a
  no-op. Assert the anchor exists and that the content actually changed.
- Substring greps miss the real thing — sweeps need `grep -i`, and `"pro" in
  "llama-prompt-guard"` once seated a 22M safety classifier on every council.
- Swallowing an exception and returning `""` turns a dead component into "it had nothing
  to say" — a revoked key showed a green ✓ while every call 401'd. Relatedly, a **probe
  payload that differs from the runtime payload** will validate a key that then fails on
  every real call.
- A bare `Python-urllib` User-Agent gets 403'd by provider edges. "Works in curl, fails
  in-app" is usually a UA fingerprint, not logic.

## Repo hygiene

`.gitignore` covers build artifacts across all three brand generations
(`MillenAI`/`Concorde`/`ConcordeAI` — `.app` bundles, DMGs, Windows zips and
installers); the previously tracked `Concorde.app/` and `Concorde-*-Windows.zip`
were untracked in the ConcordeAI rename. Edit `millenai.py` at the repo root —
any `.app` copy of it is a build output, never the source. The repo is
`bigmillz/concordeai`; GitHub 301-redirects the old `concorde` and `MillenAI`
URLs, which is what keeps pre-rename installs updating.
