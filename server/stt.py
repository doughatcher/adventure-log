"""Speaches STT integration."""
import httpx
from datetime import datetime
from .config import SPEACHES_BASE, TRANSCRIPT_FILE


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/webm") -> str | None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{SPEACHES_BASE}/v1/audio/transcriptions",
            files={"file": ("audio.webm", audio_bytes, mime_type)},
            data={"model": "Systran/faster-whisper-medium", "response_format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("text", "").strip()


async def append_to_transcript(text: str):
    if not text:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"\n**[{ts}]** {text}"
    with open(TRANSCRIPT_FILE, "a") as f:
        f.write(line)
