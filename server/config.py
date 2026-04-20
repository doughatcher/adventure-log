from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
SESSION_DIR = BASE_DIR / "session"
DATA_DIR = BASE_DIR / "data"
CHARACTERS_DIR = DATA_DIR / "characters"
SESSIONS_ARCHIVE_DIR = DATA_DIR / "sessions"

OLLAMA_BASE = "http://localhost:11434"
OLLAMA_MODEL = "gemma4-26b"

SPEACHES_BASE = "http://localhost:8000"

PANEL_FILES = {
    "scene": SESSION_DIR / "scene.md",
    "story-log": SESSION_DIR / "story-log.md",
    "party": SESSION_DIR / "party.md",
    "next-steps": SESSION_DIR / "next-steps.md",
    "map": SESSION_DIR / "map.md",
}

TRANSCRIPT_FILE = SESSION_DIR / "transcript.md"

# How many new transcript chars before triggering a Gemma update
GEMMA_TRIGGER_CHARS = 200
# Debounce: wait this many seconds after last change before calling Gemma
GEMMA_DEBOUNCE_SECS = 60
