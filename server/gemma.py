"""Gemma4 panel renderer — reads transcript, updates stage panels."""
import asyncio
import json
import re
import httpx
from pathlib import Path
from datetime import datetime

from .config import OLLAMA_BASE, OLLAMA_MODEL, PANEL_FILES, TRANSCRIPT_FILE, GEMMA_DEBOUNCE_SECS, GEMMA_TRIGGER_CHARS

SYSTEM_PROMPT = """You are a D&D session tracker and narrator assistant.
You read live session transcripts and maintain a set of display panels for the players.
Be concise, evocative, and helpful. Track what matters: story beats, party status, dangers, opportunities.
When party members' HP or status is mentioned, update accordingly.
For the map panel, use simple ASCII or descriptive text to show the current location and nearby areas."""

RENDER_PROMPT = """Given the D&D session transcript below, update all stage panels.

Output ONLY the panel blocks below — no preamble, no commentary.
Each panel starts with ## PANEL: <name> on its own line.
Keep each panel under 300 words. Be specific and current.

Panels to update:
- scene: Current location, atmosphere, active encounter or situation
- story-log: Chronological bullet points of major events this session (keep growing list, newest last)
- party: Each party member with HP if known, current status/conditions, notable items
- next-steps: 3-5 actionable suggestions or unresolved threads the party should consider
- map: Simple ASCII map or descriptive location layout of current area

Transcript:
{transcript}

Current party data:
{party_data}
"""

_last_transcript_len = 0
_pending_update: asyncio.Task | None = None
_broadcast_callback = None


def set_broadcast_callback(cb):
    global _broadcast_callback
    _broadcast_callback = cb


async def _call_ollama(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{OLLAMA_BASE}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "system": SYSTEM_PROMPT,
                "stream": False,
            },
        )
        resp.raise_for_status()
        return resp.json()["response"]


def _parse_panels(text: str) -> dict[str, str]:
    """Parse ## PANEL: name blocks from Gemma output."""
    panels = {}
    pattern = re.compile(r"## PANEL:\s*(\S+)\s*\n(.*?)(?=\n## PANEL:|\Z)", re.DOTALL)
    for match in pattern.finditer(text):
        name = match.group(1).lower()
        content = match.group(2).strip()
        panels[name] = f"## PANEL: {name}\n\n{content}"
    return panels


async def _write_panels(panels: dict[str, str]):
    for name, content in panels.items():
        path = PANEL_FILES.get(name)
        if path:
            path.write_text(content)


def _read_transcript() -> str:
    if not TRANSCRIPT_FILE.exists():
        return ""
    text = TRANSCRIPT_FILE.read_text()
    # Strip the markdown header
    lines = text.splitlines()
    lines = [l for l in lines if not l.startswith("# ")]
    return "\n".join(lines).strip()


def _read_party_data() -> str:
    from .config import CHARACTERS_DIR
    parts = []
    for md in CHARACTERS_DIR.glob("*.md"):
        parts.append(md.read_text())
    return "\n\n".join(parts) if parts else "No character data yet."


async def _do_update():
    transcript = _read_transcript()
    if not transcript:
        return
    party_data = _read_party_data()
    prompt = RENDER_PROMPT.format(transcript=transcript, party_data=party_data)
    try:
        raw = await _call_ollama(prompt)
        panels = _parse_panels(raw)
        if panels:
            await _write_panels(panels)
            if _broadcast_callback:
                await _broadcast_callback({"type": "panels_updated", "panels": list(panels.keys())})
    except Exception as e:
        print(f"[gemma] Error: {e}")


async def _debounced_update():
    await asyncio.sleep(GEMMA_DEBOUNCE_SECS)
    await _do_update()


def on_transcript_change():
    """Call this when transcript file changes. Debounces and triggers Gemma."""
    global _last_transcript_len, _pending_update

    current_len = len(_read_transcript())
    delta = current_len - _last_transcript_len

    if delta < GEMMA_TRIGGER_CHARS:
        return

    _last_transcript_len = current_len

    if _pending_update and not _pending_update.done():
        _pending_update.cancel()

    loop = asyncio.get_event_loop()
    _pending_update = loop.create_task(_debounced_update())


async def force_update():
    """Immediately trigger a Gemma panel update (bypasses debounce)."""
    global _last_transcript_len
    _last_transcript_len = len(_read_transcript())
    await _do_update()
