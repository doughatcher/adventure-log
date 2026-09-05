# Adventure Log — Live Session Companion

A live D&D session companion that listens to your table, tracks HP and combat state in real time, generates narrative scene panels, and publishes a living campaign journal to GitHub Pages — all powered by local LLMs.

**Campaign journal:** [doughatcher.github.io/adventure-log](https://doughatcher.github.io/adventure-log/)

---

## Screenshots

**Start screen** — shown when no session is in progress. Click to begin or load a past session.

![Start screen](docs/screenshot-start.png)

**Live session** — party tracker, color-coded transcript log (DM / PC / Roll / OOC), AI-updated scene and next-steps panels, and map graph. The decision helper pops up automatically when the AI detects an active fork in play.

![Live session](docs/screenshot-live.png)

![Live session — clean view](docs/screenshot-live-clean.png)

**Past sessions** — browse archived sessions, view scene/story/transcript tabs, download the MP3 recording, or load the session into the main stage for review.

![History modal](docs/screenshot-history.png)

---

## What It Does

| Feature | How |
|---------|-----|
| **Voice → Transcript** | Microphone audio → local Speaches/Whisper → live transcript log |
| **Fast AI pass (~4s)** | Extracts HP, enemies, conditions from transcript |
| **Full AI pass (~20s)** | Updates Scene, Story, Map, and Next Steps panels |
| **Party tracker** | Fuzzy HP tracking; enemies appear when mentioned; long rest restores |
| **Decision modal** | Pops up when AI detects an active choice (shop, fork, tactic) |
| **Session archive** | End Session: audio → MP3, all panels → markdown archive |
| **Campaign journal** | GitHub Action auto-generates narrative HTML and deploys to Pages |
| **D&D Beyond polling** | Live character HP/conditions synced from DDB character API (see below) |

---

## Architecture

```
Microphone → Speaches STT → transcript.md
                               ↓
                     dnd-stage server (FastAPI)
                               ↓
                    Ollama (local LLM, e.g. gemma4)
                     ├── fast pass: state.json     (HP, conditions)
                     └── full pass: panels/*.md    (scene, story, map)
                               ↓
                    Session archive (data/sessions/YYYY-MM-DD-HHMM/)
                               ↓
              GitHub Action (generate_journal.py → Claude API)
                     ├── content/sessions/*.md     (narrative journal post)
                     ├── content/characters/*.md   (character pages)
                     └── context/next-session-brief.md
                               ↓
                    Hugo → GitHub Pages (adventure-log)

Optional: D&D Beyond → ddb_poll.py → PATCH /api/characters/{slug}
```

### Two ways in

The diagram above is the **live** path: the server listens during play, so hit
points and conditions update while the fight is still happening.

There is a second, quieter path for tables that record with
[Transcripts](https://transcripts.doughatcher.com) instead of running the live
server. Transcripts files one Markdown document per recording, and an evening is
usually several of them — someone starts it before people settle, it stops for
pizza, it starts again. When the session is genuinely over, the app's
`onComplete` hook hands over every document at once, and
`scripts/import_transcripts_session.py` folds them into the single
`transcript.md` the journal generator already reads:

```
Transcripts (Mac) → onComplete hook → import_transcripts_session.py
                                            ↓
                              data/sessions/<slug>/transcript.md
                                            ↓
                              (rejoins the pipeline above)
```

Receiving the whole evening in one call is what makes this work. A hook that
fired per recording would see four fragments with no way to know they were
fragments, and the journal entry would read as four short sessions rather than
one night.

Two rules the importer will not bend, both because this repository is public:

- **Nothing before the session start is published.** A recorder that ran all
  afternoon has an afternoon in it. Each document's `recorded_at` plus its turn
  timestamps give a real wall-clock time for every line, and anything earlier
  than the session is dropped.
- **A line it cannot place on that timeline stops the import.** Older documents
  written before turns were stamped cannot be trimmed reliably, so it refuses
  rather than guessing. It also opens a pull request instead of pushing, so a
  person sees the session before the world does.

Transcripts' own guide covers this from the other side, including the mechanics
of the hook: [A worked example](https://transcripts.doughatcher.com/guide/worked-example/).

---

## Requirements

| Service | Purpose | Default |
|---------|---------|---------|
| [Ollama](https://ollama.ai) | Local LLM for state/panel generation | `localhost:11434` |
| [Speaches](https://github.com/speaches-ai/speaches) | Whisper STT for voice transcription | `localhost:8000` |
| `ffmpeg` | Audio chunk concat → MP3 | system PATH |

**Recommended model:** `gemma4:e4b` — 12GB VRAM, ~50 t/s on RTX 4070.

```bash
ollama pull gemma4:e4b
```

---

## Quickstart

```bash
# 1. Clone
git clone https://github.com/doughatcher/adventure-log
cd adventure-log

# 2. Configure
cp .env.example .env
# Edit .env — set OLLAMA_MODEL, optionally GITHUB_TOKEN for releases

# 3. Run (requires uv)
uv run uvicorn server.main:app --host 0.0.0.0 --port 3200 --reload

# 4. Open http://localhost:3200
```

---

## Docker

```bash
docker build -t dnd-stage .
docker run -p 3200:3200 \
  -e OLLAMA_BASE=http://host.docker.internal:11434 \
  -e SPEACHES_BASE=http://host.docker.internal:8000 \
  -v $(pwd)/session:/app/session \
  -v $(pwd)/data:/app/data \
  dnd-stage
```

---

## D&D Beyond Integration

Live HP and condition sync from D&D Beyond's character API into the party tracker.

### Setup (one-time, requires desktop session with display)

```bash
# Step 1: Open browser, log in to DDB, capture CobaltSession cookie
python scripts/ddb_auth.py
# → Writes DDB_COOKIE to .env

# Step 2: Discover campaign character IDs
python scripts/ddb_discover.py
# → Writes DDB_CHARACTER_IDS to .env

# Step 3: Start polling loop
python scripts/ddb_poll.py
# → Polls DDB every 60s, PATCHes http://localhost:3200/api/characters/{slug}
```

### How It Works

1. `ddb_auth.py` — opens a Playwright browser (non-headless) for manual login to D&D Beyond, extracts the `CobaltSession` HttpOnly cookie using CDP, writes to `.env`
2. `ddb_discover.py` — exchanges the cookie for a cobalt API token, calls `api.dndbeyond.com/campaign/stt/active-short-characters/{campaign_id}`, writes character IDs to `.env`
3. `ddb_poll.py` — polls each character's stat endpoint on `DDB_POLL_INTERVAL` (default 60s), maps slugs to DDB character IDs, PATCHes the dnd-stage server with current HP and conditions

### Notes

- **Requires GNOME/desktop session:** DDB login uses PerimeterX bot detection that blocks headless Chromium. Run `ddb_auth.py` from a terminal with a display.
- **HttpOnly cookie:** `CobaltSession` is not readable via `document.cookie`. The scripts use Playwright CDP (`context.cookies()`) which can access HttpOnly cookies after an authenticated session.
- **Token refresh:** `ddb_poll.py` refreshes the cobalt token each poll cycle — tokens expire (~1800s TTL).
- **Slug mapping:** Character slugs (e.g. `rides-the-wake`) must match filenames in `data/characters/`. Edit `DDB_CHARACTER_IDS` in `.env` to set `slug=ddb_id` pairs.

---

## Campaign Journal (GitHub Pages)

After each session, push the archived `data/sessions/YYYY-MM-DD-HHMM/` folder to main. A GitHub Action fires:

1. **Generates narrative** — calls Claude API to write journal prose, character updates, and a dense next-session AI brief from the session transcript and panels
2. **Commits content** — writes `content/sessions/` and `content/characters/` markdown
3. **Deploys Hugo** — builds the static site and pushes to GitHub Pages

The journal accumulates session by session. Each entry builds on the last — characters develop, the story deepens, context grows. `context/campaign-history.md` and `context/next-session-brief.md` are fed to Claude on each generation run so the narrative always has full backstory.

**Required secrets:** `ANTHROPIC_API_KEY` in repo Settings → Secrets → Actions.

---

## Character PATCH API

Party HP and conditions can be partially updated without overwriting other fields:

```bash
# HP only — preserves class, AC, notes, conditions
curl -X PATCH http://localhost:3200/api/characters/granit \
  -H "Content-Type: application/json" \
  -d '{"hp_current": 30}'

# Conditions only
curl -X PATCH http://localhost:3200/api/characters/rides-the-wake \
  -H "Content-Type: application/json" \
  -d '{"conditions": ["Poisoned", "Frightened"]}'
```

---

## Configuration

All settings via `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_BASE` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `gemma4:e4b` | Model for all generation |
| `SPEACHES_BASE` | `http://localhost:8000` | Speaches STT endpoint |
| `PORT` | `3200` | HTTP/WS port |
| `GITHUB_REPO` | — | `owner/repo` for release publishing |
| `GITHUB_TOKEN` | — | Token with repo write scope |
| `DDB_CAMPAIGN_ID` | — | D&D Beyond campaign ID |
| `DDB_COOKIE` | — | CobaltSession cookie (set by ddb_auth.py) |
| `DDB_CHARACTER_IDS` | — | `slug=id,slug=id` pairs (set by ddb_discover.py) |
| `DDB_POLL_INTERVAL` | `60` | DDB poll interval in seconds |
| `STATE_TRIGGER_CHARS` | `80` | Transcript chars before fast state update |
| `STATE_DEBOUNCE_SECS` | `6` | Fast state update debounce |
| `PANEL_TRIGGER_CHARS` | `300` | Transcript chars before full panel update |
| `PANEL_DEBOUNCE_SECS` | `12` | Full panel update debounce |

---

## Project Layout

```
dnd-stage/
├── client/              # Frontend (vanilla JS, no build step)
├── server/              # FastAPI backend
│   ├── main.py          # Routes, WebSocket, PATCH endpoint
│   ├── gemma.py         # LLM prompting, panel/state updates
│   ├── stt.py           # Speaches STT integration
│   └── config.py        # Env vars + DDB config
├── scripts/             # D&D Beyond integration
│   ├── ddb_auth.py      # Browser login → CobaltSession cookie
│   ├── ddb_discover.py  # Campaign character ID discovery
│   └── ddb_poll.py      # Live HP/conditions polling loop
├── data/
│   ├── characters/      # Character .md files (stats frontmatter)
│   └── sessions/        # Archived sessions (push to trigger journal)
├── session/             # Live session state (gitignored)
├── content/             # Hugo site content (auto-generated)
│   ├── characters/      # Character narrative pages
│   └── sessions/        # Session journal entries
├── context/             # AI generation context
│   ├── campaign-history.md      # World lore, character profiles
│   └── next-session-brief.md   # Dense AI brief, updated each session
├── layouts/             # Custom Hugo templates
├── .github/
│   ├── workflows/campaign-journal.yml   # Pages deploy pipeline
│   └── scripts/generate_journal.py      # Claude journal generator
├── hugo.toml
└── .env.example
```
