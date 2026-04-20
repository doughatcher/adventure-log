"""DnD Stage — FastAPI backend."""
import asyncio
import json
import re
import aiofiles
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from watchfiles import awatch

from .config import (
    BASE_DIR, SESSION_DIR, CHARACTERS_DIR, SESSIONS_ARCHIVE_DIR,
    PANEL_FILES, TRANSCRIPT_FILE
)
from . import gemma, stt

app = FastAPI(title="DnD Stage")

# Serve static client files
CLIENT_DIR = BASE_DIR / "client"
app.mount("/static", StaticFiles(directory=str(CLIENT_DIR)), name="static")

# --- WebSocket connection manager ---

class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self._connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.remove(ws)

manager = ConnectionManager()

async def broadcast_cb(data: dict):
    await manager.broadcast(data)

gemma.set_broadcast_callback(broadcast_cb)


# --- File watcher task ---

async def watch_panels():
    watch_paths = [str(SESSION_DIR)]
    async for changes in awatch(*watch_paths):
        changed_panels = []
        for change_type, path_str in changes:
            path = Path(path_str)
            for name, panel_path in PANEL_FILES.items():
                if path == panel_path:
                    changed_panels.append(name)
            if path == TRANSCRIPT_FILE:
                gemma.on_transcript_change()
                # Broadcast latest transcript tail
                content = TRANSCRIPT_FILE.read_text()
                lines = content.splitlines()
                tail = "\n".join(lines[-8:])
                await manager.broadcast({"type": "transcript", "tail": tail})

        if changed_panels:
            panels_content = {}
            for name in changed_panels:
                p = PANEL_FILES[name]
                if p.exists():
                    panels_content[name] = p.read_text()
            await manager.broadcast({"type": "panels", "data": panels_content})


@app.on_event("startup")
async def startup():
    asyncio.create_task(watch_panels())


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(str(CLIENT_DIR / "index.html"))


@app.get("/api/panels")
async def get_panels():
    panels = {}
    for name, path in PANEL_FILES.items():
        panels[name] = path.read_text() if path.exists() else ""
    return panels


@app.get("/api/transcript")
async def get_transcript():
    if not TRANSCRIPT_FILE.exists():
        return {"content": "", "tail": ""}
    content = TRANSCRIPT_FILE.read_text()
    lines = content.splitlines()
    tail = "\n".join(lines[-8:])
    return {"content": content, "tail": tail}


@app.post("/api/voice")
async def receive_voice(audio: UploadFile = File(...)):
    data = await audio.read()
    text = await stt.transcribe_audio(data, audio.content_type or "audio/webm")
    if text:
        await stt.append_to_transcript(text)
        return {"text": text, "ok": True}
    return {"text": "", "ok": False}


@app.post("/api/update")
async def force_update():
    """Manually trigger a Gemma panel update."""
    asyncio.create_task(gemma.force_update())
    return {"ok": True, "message": "Update triggered"}


# --- Character management ---

class Character(BaseModel):
    name: str
    char_class: str = ""
    hp_current: int = 0
    hp_max: int = 0
    ac: int = 0
    notes: str = ""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@app.get("/api/characters")
async def list_characters():
    chars = []
    for md in sorted(CHARACTERS_DIR.glob("*.md")):
        text = md.read_text()
        # Parse frontmatter
        fm = {}
        fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if fm_match:
            for line in fm_match.group(1).splitlines():
                if ": " in line:
                    k, v = line.split(": ", 1)
                    fm[k.strip()] = v.strip().strip('"')
        chars.append(fm)
    return chars


@app.post("/api/characters")
async def add_character(char: Character):
    slug = _slug(char.name)
    path = CHARACTERS_DIR / f"{slug}.md"
    content = f"""---
name: {char.name}
class: {char.char_class}
hp_current: {char.hp_current}
hp_max: {char.hp_max}
ac: {char.ac}
notes: "{char.notes}"
---

# {char.name}
"""
    path.write_text(content)
    # Regenerate party panel
    await _refresh_party_panel()
    return {"ok": True, "slug": slug}


@app.patch("/api/characters/{slug}")
async def update_character(slug: str, char: Character):
    path = CHARACTERS_DIR / f"{slug}.md"
    if not path.exists():
        raise HTTPException(404, "Character not found")
    content = f"""---
name: {char.name}
class: {char.char_class}
hp_current: {char.hp_current}
hp_max: {char.hp_max}
ac: {char.ac}
notes: "{char.notes}"
---

# {char.name}
"""
    path.write_text(content)
    await _refresh_party_panel()
    return {"ok": True}


async def _refresh_party_panel():
    chars = await list_characters()
    lines = ["## PANEL: party\n"]
    for c in chars:
        name = c.get("name", "Unknown")
        cls = c.get("class", "")
        hp_c = c.get("hp_current", "?")
        hp_m = c.get("hp_max", "?")
        ac = c.get("ac", "?")
        notes = c.get("notes", "")
        hp_str = f"HP {hp_c}/{hp_m}" if hp_m != "0" else "HP ?"
        line = f"- **{name}**"
        if cls:
            line += f" — {cls}"
        line += f" | {hp_str} | AC {ac}"
        if notes:
            line += f"\n  *{notes}*"
        lines.append(line)
    PANEL_FILES["party"].write_text("\n".join(lines))


# --- Session archive ---

@app.post("/api/session/end")
async def end_session():
    """Archive current session files and reset for next session."""
    ts = datetime.now().strftime("%Y-%m-%d-%H%M")
    archive_dir = SESSIONS_ARCHIVE_DIR / ts
    archive_dir.mkdir(parents=True, exist_ok=True)

    for name, path in PANEL_FILES.items():
        if path.exists():
            (archive_dir / path.name).write_text(path.read_text())
    if TRANSCRIPT_FILE.exists():
        (archive_dir / "transcript.md").write_text(TRANSCRIPT_FILE.read_text())

    # Reset files
    TRANSCRIPT_FILE.write_text("# Session Transcript\n\n")
    for name, path in PANEL_FILES.items():
        path.write_text(f"## PANEL: {name}\n\n*New session.*\n")

    return {"ok": True, "archived_to": str(archive_dir)}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # Send current state on connect
    panels = {}
    for name, path in PANEL_FILES.items():
        panels[name] = path.read_text() if path.exists() else ""
    await ws.send_json({"type": "init", "panels": panels})
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        manager.disconnect(ws)
