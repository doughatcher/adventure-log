"""DnD Stage — FastAPI backend."""
import asyncio
import json
import re
import subprocess
import aiofiles
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from watchfiles import awatch

from .config import (
    BASE_DIR, SESSION_DIR, CHARACTERS_DIR, SESSIONS_ARCHIVE_DIR,
    PANEL_FILES, TRANSCRIPT_FILE, STATE_FILE, AUDIO_DIR
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
        state_changed = False
        for change_type, path_str in changes:
            path = Path(path_str)
            for name, panel_path in PANEL_FILES.items():
                if path == panel_path:
                    changed_panels.append(name)
            if path == TRANSCRIPT_FILE:
                gemma.on_transcript_change()
                content = TRANSCRIPT_FILE.read_text()
                lines = content.splitlines()
                tail = "\n".join(lines[-12:])
                await manager.broadcast({"type": "transcript", "content": content, "tail": tail})
            if path == STATE_FILE:
                state_changed = True

        if changed_panels:
            panels_content = {}
            for name in changed_panels:
                p = PANEL_FILES[name]
                if p.exists():
                    panels_content[name] = p.read_text()
            await manager.broadcast({"type": "panels", "data": panels_content})

        if state_changed:
            try:
                state_contents = json.loads(STATE_FILE.read_text())
                await manager.broadcast({"type": "state", "data": state_contents})
            except Exception as e:
                print(f"[watch] Error broadcasting state: {e}")


@app.on_event("startup")
async def startup():
    gemma.start_update_loop()
    asyncio.create_task(watch_panels())


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def root():
    html = (CLIENT_DIR / "index.html").read_text()
    ts = int(datetime.now().timestamp())
    html = html.replace('/static/style.css"', f'/static/style.css?v={ts}"')
    html = html.replace('/static/stage.js"', f'/static/stage.js?v={ts}"')
    return HTMLResponse(html)


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


@app.get("/api/state")
async def get_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


@app.post("/api/voice")
async def receive_voice(audio: UploadFile = File(...)):
    data = await audio.read()
    # Save chunk for session recording
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    chunk_index = len(list(AUDIO_DIR.glob("chunk_*.webm")))
    chunk_path = AUDIO_DIR / f"chunk_{chunk_index:04d}.webm"
    chunk_path.write_bytes(data)
    # Transcribe
    text = await stt.transcribe_audio(data, audio.content_type or "audio/webm")
    if text:
        await stt.append_to_transcript(text)
        return {"text": text, "ok": True}
    return {"text": "", "ok": False}


_STRIP_HEADERS = {
    "x-frame-options", "content-security-policy", "content-security-policy-report-only",
    "x-content-type-options", "transfer-encoding",
}
_DDB_ORIGIN = "https://www.dndbeyond.com"

# Stored DDB session cookie (set via /api/ddb-cookie)
_ddb_cookie: str = ""


@app.post("/api/ddb-cookie")
async def set_ddb_cookie(payload: dict):
    """Store the DDB session cookie for proxy requests."""
    global _ddb_cookie
    _ddb_cookie = payload.get("cookie", "")
    return {"ok": True}


@app.get("/api/proxy")
async def proxy_ddb(url: str = Query(...)):
    """Proxy a DDB URL, stripping frame-blocking headers so it embeds in an iframe."""
    if not url.startswith(_DDB_ORIGIN):
        raise HTTPException(400, "Only dndbeyond.com URLs are allowed")
    headers = {
        "User-Agent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if _ddb_cookie:
        headers["Cookie"] = _ddb_cookie
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=headers)
    except Exception as e:
        raise HTTPException(502, f"Proxy error: {e}")

    # Build response headers, stripping frame-blockers
    out_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _STRIP_HEADERS
    }
    # Rewrite absolute-root URLs in HTML so assets load from DDB directly
    content = resp.content
    if "text/html" in resp.headers.get("content-type", ""):
        text = content.decode("utf-8", errors="replace")
        text = text.replace('src="/', f'src="{_DDB_ORIGIN}/')
        text = text.replace("src='/", f"src='{_DDB_ORIGIN}/")
        text = text.replace('href="/', f'href="{_DDB_ORIGIN}/')
        text = text.replace("href='/", f"href='{_DDB_ORIGIN}/")
        text = text.replace('action="/', f'action="{_DDB_ORIGIN}/')
        content = text.encode("utf-8")
        out_headers["content-length"] = str(len(content))

    return Response(content=content, status_code=resp.status_code,
                    headers=out_headers, media_type=resp.headers.get("content-type"))


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


def _write_char_file(slug: str, char: Character):
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


def _sync_char_to_state(slug: str, char: Character):
    """Merge a manually added/edited character into state.json."""
    existing = {}
    if STATE_FILE.exists():
        try:
            existing = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    chars = existing.setdefault("characters", {})
    entry = chars.get(slug, {})
    entry["name"] = char.name
    if char.char_class:
        entry["class"] = char.char_class
    if char.hp_current:
        entry["hp"] = char.hp_current
    if char.hp_max:
        entry["max_hp"] = char.hp_max
    if char.ac:
        entry["ac"] = char.ac
    if char.notes:
        entry["notes"] = char.notes
    entry.setdefault("is_enemy", False)
    entry.setdefault("status", "alive")
    entry.setdefault("conditions", [])
    chars[slug] = entry
    existing["last_updated"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(existing, indent=2))


@app.post("/api/characters")
async def add_character(char: Character):
    slug = _slug(char.name)
    _write_char_file(slug, char)
    _sync_char_to_state(slug, char)
    await _refresh_party_panel()
    await manager.broadcast({"type": "state", "data": json.loads(STATE_FILE.read_text())})
    return {"ok": True, "slug": slug}


@app.patch("/api/characters/{slug}")
async def update_character(slug: str, char: Character):
    path = CHARACTERS_DIR / f"{slug}.md"
    if not path.exists():
        raise HTTPException(404, "Character not found")
    _write_char_file(slug, char)
    _sync_char_to_state(slug, char)
    await _refresh_party_panel()
    await manager.broadcast({"type": "state", "data": json.loads(STATE_FILE.read_text())})
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
    if STATE_FILE.exists():
        (archive_dir / "state.json").write_text(STATE_FILE.read_text())

    # Concatenate audio chunks into MP3
    recording_path = None
    chunks = sorted(AUDIO_DIR.glob("chunk_*.webm")) if AUDIO_DIR.exists() else []
    if chunks:
        filelist = archive_dir / "filelist.txt"
        filelist.write_text("\n".join(f"file '{c}'" for c in chunks))
        mp3_path = archive_dir / "recording.mp3"
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(filelist), "-c:a", "libmp3lame", "-q:a", "2", str(mp3_path)],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                recording_path = str(mp3_path)
                print(f"[session] Recording saved: {mp3_path} ({mp3_path.stat().st_size // 1024}KB)")
            else:
                print(f"[session] ffmpeg error: {result.stderr[-500:]}")
        except Exception as e:
            print(f"[session] Recording failed: {e}")
        filelist.unlink(missing_ok=True)

    # Reset files
    TRANSCRIPT_FILE.write_text("# Session Transcript\n\n")
    for name, path in PANEL_FILES.items():
        path.write_text(f"## PANEL: {name}\n\n*New session.*\n")
    STATE_FILE.write_text(json.dumps({
        "session_name": "New Session",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "location": "Unknown",
        "combat_active": False,
        "round": 0,
        "initiative_order": [],
        "characters": {},
        "last_updated": None
    }, indent=2))
    # Clear audio chunks
    if AUDIO_DIR.exists():
        for f in AUDIO_DIR.glob("chunk_*.webm"):
            f.unlink()

    return {"ok": True, "archived_to": str(archive_dir), "recording": recording_path}


@app.get("/api/recording/latest")
async def latest_recording():
    """Download the most recent session's MP3."""
    sessions = sorted(SESSIONS_ARCHIVE_DIR.glob("*/recording.mp3")) if SESSIONS_ARCHIVE_DIR.exists() else []
    if not sessions:
        raise HTTPException(404, "No recording found")
    return FileResponse(str(sessions[-1]), media_type="audio/mpeg", filename=f"{sessions[-1].parent.name}.mp3")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # Send current state on connect
    panels = {}
    for name, path in PANEL_FILES.items():
        panels[name] = path.read_text() if path.exists() else ""
    transcript_content = TRANSCRIPT_FILE.read_text() if TRANSCRIPT_FILE.exists() else ""
    state_contents = {}
    if STATE_FILE.exists():
        try:
            state_contents = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    # Build characters from .md files (source of truth for manually added chars)
    md_characters = {}
    for md in sorted(CHARACTERS_DIR.glob("*.md")):
        text = md.read_text()
        fm = {}
        fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if fm_match:
            for line in fm_match.group(1).splitlines():
                if ": " in line:
                    k, v = line.split(": ", 1)
                    fm[k.strip()] = v.strip().strip('"')
        if fm.get("name"):
            slug = re.sub(r"[^a-z0-9]+", "-", fm["name"].lower()).strip("-")
            md_characters[slug] = {
                "name": fm.get("name", ""),
                "class": fm.get("class", ""),
                "hp": int(fm.get("hp_current", 0) or 0),
                "max_hp": int(fm.get("hp_max", 0) or 0),
                "ac": int(fm.get("ac", 0) or 0),
                "notes": fm.get("notes", ""),
                "is_enemy": False,
                "status": "alive",
                "conditions": [],
            }

    # Merge: md_characters is baseline, state_contents.characters overlays AI-tracked data
    merged_chars = dict(md_characters)
    for slug, char_data in state_contents.get("characters", {}).items():
        if slug in merged_chars:
            # Overlay state data onto md baseline (state wins for HP/conditions/status)
            for k, v in char_data.items():
                if v is not None:
                    merged_chars[slug][k] = v
        else:
            merged_chars[slug] = char_data
    if merged_chars:
        state_contents["characters"] = merged_chars

    await ws.send_json({
        "type": "init",
        "panels": panels,
        "transcript": transcript_content,
        "state": state_contents,
    })
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        manager.disconnect(ws)
