# DnD Stage

A live D&D session companion that listens to your table, tracks HP and combat state, describes the scene, and surfaces decision helpers — all powered by a local LLM.

## What it does

- **Voice → Transcript** — microphone audio chunks sent to a local Speaches/Whisper instance; transcript streams into the log in real time
- **Two-speed AI** — a fast pass (~4s) extracts HP/enemies/conditions from the transcript; a slow pass (~20s) updates Scene, Story, Map, and Next Steps panels
- **Party tracker** — fuzzy HP tracking for all party members; enemies appear automatically when mentioned; long rests restore HP; damage and healing are applied from conversation
- **Decision modal** — when the AI detects an active choice (shopping, path split, tactical decision), a helper card pops up with options and context
- **Map panel** — ASCII map generated from transcript, plus a fullscreen D&D Beyond VTT view
- **Session archive** — End Session concatenates all audio chunks into an MP3 and archives all panels/transcript/state

## Requirements

| Service | Purpose | Default |
|---------|---------|---------|
| [Ollama](https://ollama.ai) | Local LLM for state/panel generation | `localhost:11434` |
| [Speaches](https://github.com/speaches-ai/speaches) | Whisper STT for voice transcription | `localhost:8000` |
| `ffmpeg` | Audio chunk concat → MP3 (session recording) | system PATH |

**Recommended model:** `gemma4:e4b` — fits in 12GB VRAM, ~50 t/s on RTX 4070, good instruction following.

```bash
ollama pull gemma4:e4b
```

## Quickstart

```bash
# 1. Clone
git clone https://github.com/superterran/dnd-stage
cd dnd-stage

# 2. Configure
cp .env.example .env
# Edit .env — set OLLAMA_MODEL to whatever model you have

# 3. Run (requires uv)
./run.sh
# or: uv run uvicorn server.main:app --host 0.0.0.0 --port 3200 --reload

# 4. Open http://localhost:3200
```

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

> **Note:** Use `host.docker.internal` to reach Ollama/Speaches running on the host.

## Systemd (Linux)

```bash
cp systemd/dnd-stage.service ~/.config/systemd/user/
# Edit WorkingDirectory and PATH to match your setup
systemctl --user daemon-reload
systemctl --user enable --now dnd-stage
```

## Configuration

All settings via environment variables or `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_BASE` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `gemma4:e4b` | Model to use for all generation |
| `SPEACHES_BASE` | `http://localhost:8000` | Speaches STT endpoint |
| `PORT` | `3200` | HTTP/WS port |
| `STATE_TRIGGER_CHARS` | `80` | New transcript chars before fast state update |
| `STATE_DEBOUNCE_SECS` | `6` | Debounce for fast state updates |
| `PANEL_TRIGGER_CHARS` | `300` | New transcript chars before full panel update |
| `PANEL_DEBOUNCE_SECS` | `12` | Debounce for full panel updates |

## Project layout

```
dnd-stage/
├── client/          # Frontend (vanilla JS, no build step)
│   ├── index.html
│   ├── stage.js
│   └── style.css
├── server/          # FastAPI backend
│   ├── main.py      # Routes, WebSocket, file watcher
│   ├── gemma.py     # LLM prompting, panel/state updates
│   ├── stt.py       # Speaches STT integration
│   └── config.py    # All config, reads from env
├── data/
│   ├── characters/  # Character .md files (git-ignored at runtime)
│   └── sessions/    # Archived sessions (git-ignored)
├── session/         # Live session files (git-ignored)
│   ├── transcript.md
│   ├── state.json
│   ├── scene.md / story-log.md / map.md / ...
│   └── audio/       # Recorded chunks → MP3 on session end
├── systemd/         # Systemd user service unit
├── .env.example     # Config template
├── Dockerfile
└── pyproject.toml
```

## UI overview

- **Party** (left) — character cards with HP bar and conditions; click to edit; AI fills in HP from conversation
- **Log** (center) — live transcript color-coded by type (DM narration / Player action / Dice roll / OOC / Noise); filter buttons at top
- **Scene / Map / Next** (right) — AI-updated panels; map opens fullscreen with D&D Beyond VTT tab
- **Decision modal** — slides in bottom-right when AI detects an active choice; dismiss with ✕
- **Recording** — click Rec or press R; audio chunks saved server-side; End Session produces MP3
