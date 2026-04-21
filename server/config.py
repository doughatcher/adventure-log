import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
SESSION_DIR = BASE_DIR / "session"
DATA_DIR = BASE_DIR / "data"
CHARACTERS_DIR = DATA_DIR / "characters"
SESSIONS_ARCHIVE_DIR = DATA_DIR / "sessions"

CONTEXT_DIR = BASE_DIR / "context"
NEXT_SESSION_BRIEF_FILE = CONTEXT_DIR / "next-session-brief.md"

# ── GitHub release publishing ──
GITHUB_REPO  = os.environ.get("GITHUB_REPO",  "doughatcher/game-telemetry")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ── External services (override via .env or environment) ──
OLLAMA_BASE  = os.environ.get("OLLAMA_BASE",  "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")
SPEACHES_BASE = os.environ.get("SPEACHES_BASE", "http://localhost:8000")

# ── Server ──
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "3200"))

PANEL_FILES = {
    "scene":      SESSION_DIR / "scene.md",
    "story-log":  SESSION_DIR / "story-log.md",
    "party":      SESSION_DIR / "party.md",
    "next-steps": SESSION_DIR / "next-steps.md",
    "map":        SESSION_DIR / "map.md",
}

TRANSCRIPT_FILE = SESSION_DIR / "transcript.md"
STATE_FILE      = SESSION_DIR / "state.json"
AUDIO_DIR       = SESSION_DIR / "audio"

# ── AI update tuning ──
# Fast state update: extract HP/enemies/conditions — fires frequently
GEMMA_STATE_TRIGGER_CHARS = int(os.environ.get("STATE_TRIGGER_CHARS", "80"))
GEMMA_STATE_DEBOUNCE_SECS = float(os.environ.get("STATE_DEBOUNCE_SECS", "6"))

# Full panel update: scene, story, map, next-steps — fires less often
GEMMA_PANEL_TRIGGER_CHARS = int(os.environ.get("PANEL_TRIGGER_CHARS", "300"))
GEMMA_PANEL_DEBOUNCE_SECS = float(os.environ.get("PANEL_DEBOUNCE_SECS", "12"))

# Legacy aliases
GEMMA_TRIGGER_CHARS = GEMMA_STATE_TRIGGER_CHARS
GEMMA_DEBOUNCE_SECS = GEMMA_STATE_DEBOUNCE_SECS
