"""Dungeon Master panel renderer — reads transcript, updates stage panels."""
import asyncio
import json
import re
import httpx
from datetime import datetime, timezone

from .config import (
    OLLAMA_BASE, OLLAMA_MODEL, PANEL_FILES,
    TRANSCRIPT_FILE, CHARACTERS_DIR, STATE_FILE,
    GEMMA_STATE_TRIGGER_CHARS, GEMMA_STATE_DEBOUNCE_SECS,
    GEMMA_PANEL_TRIGGER_CHARS, GEMMA_PANEL_DEBOUNCE_SECS,
)

SYSTEM_PROMPT = """You are a precise D&D session state tracker. Output only what is asked, in exact format specified. No extra commentary."""

# Fast prompt: state JSON only — ~150-200 tokens output, ~4s at 50 t/s
STATE_PROMPT = """Extract current D&D game state from this transcript as a single JSON object. Output ONLY the JSON, nothing else.

Rules:
- Apply HP arithmetic: "takes 21 damage" subtracts from current HP; "heals X" adds
- Add every named character/enemy the moment they appear
- Enemies (monsters, hostile NPCs): is_enemy=true
- Set status="dead" when killed, status="unconscious" when knocked out
- Slug: lowercase-hyphens only e.g. "rides-the-wake", "priest-b"
- location: update only when the party clearly moves to a new in-game place (ignore OOC discussion)
- Party characters: if someone speaks in first-person about their character's actions or equipment (even OOC), infer they are a PC and add them if named
- Preserve ALL existing characters from current_state — never remove a character, only update fields
- Fuzzy HP: "long rest" or "fully healed" = set hp to max_hp. "short rest" = add ~25% of max. "bloodied/badly hurt" ≈ 50%. "nearly dead/single digits" ≈ 10%. "looks fine/healthy" ≈ 90%. Approximate is better than null.
- If you can infer a character's max HP from class/level context (e.g. "Barbarian 7 with CON 14" ≈ 70hp), do so

JSON schema (output only this, filled in):
{{"location":"string","combat_active":bool,"round":int,"initiative_order":["Name"],"characters":{{"slug":{{"name":"Full Name","hp":int_or_null,"max_hp":int_or_null,"ac":int_or_null,"conditions":[],"class":"","notes":"","is_enemy":false,"status":"alive"}}}}}}

Current state:
{current_state}

Party baseline:
{party_data}

Recent transcript (last events):
{transcript}

JSON:"""

# Full prompt: all panels + state — ~800-1000 tokens output, ~20s at 50 t/s
PANEL_PROMPT = """Update these D&D session panels from the transcript. Output ONLY the blocks below, no other text.

IMPORTANT RULES:
- OOC = out-of-character table talk (players discussing rules, joking, or chatting as themselves). IGNORE OOC for scene/map/story.
- Scene and Map describe the GAME WORLD only, based on last known in-game location and events — never describe the players talking at the table.
- If recent transcript is mostly OOC, keep previous scene/map content unchanged.
- Next-steps: write 3-5 COMPLETE sentences. If party is shopping or in downtime, suggest specific items to buy based on class and situation.
- Every sentence in every panel must be complete — never cut off mid-sentence.

## PANEL: scene
Line 1: ONE punchy sentence (≤12 words, present tense, vivid, no "the party"). Then 1-2 sentences of optional supporting detail on separate lines.

## PANEL: story-log
(growing bullet list of major IN-GAME events only, keep all previous, add newest last)

## PANEL: party
(each character: name HP/max AC, active conditions — enemies listed separately)

## PANEL: next-steps
3-5 bullets. Each bullet: ≤7 words, starts with a verb, no full sentences. If shopping/downtime, suggest specific items by class.

## PANEL: map
(Mermaid graph TD diagram showing locations and connections — use node labels for rooms/areas, edge labels for paths/doors. Include character positions if known. No ASCII art, no prose.)

## DECISION: (ONLY include this block if there is an active, clear choice being discussed right now — shopping options, path splits, tactical decisions. OMIT entirely if no active decision.)
TITLE: (one short question, e.g. "What should Rides the Wake buy?")
CONTEXT: (1-2 sentences of relevant context: gold available, class, current HP, situation)
OPTION: Name | Description | Cost or mechanical detail
OPTION: Name | Description | Cost or mechanical detail
OPTION: Name | Description | Cost or mechanical detail
(2-5 options max, each on its own OPTION: line)

Current game state:
{current_state}

Transcript:
{transcript}

Party data:
{party_data}
"""

_last_state_transcript_len = 0
_last_panel_transcript_len = 0
_broadcast_callback = None
_state_queue: asyncio.Queue | None = None   # fast: state-only updates
_panel_queue: asyncio.Queue | None = None   # slow: full panel updates


def set_broadcast_callback(cb):
    global _broadcast_callback
    _broadcast_callback = cb


async def _call_ollama(prompt: str, max_tokens: int = 300) -> str:
    import time
    t0 = time.time()
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{OLLAMA_BASE}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "system": SYSTEM_PROMPT,
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_ctx": 4096,
                    "num_predict": max_tokens,
                    "top_k": 20,
                    "top_p": 0.9,
                    "repeat_penalty": 1.1,
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.time() - t0
        tps = data.get("eval_count", 0) / max(data.get("eval_duration", 1), 1) * 1e9
        print(f"[gemma] {elapsed:.1f}s — {data.get('eval_count',0)} tokens @ {tps:.0f} t/s")
        return data["response"]


def _parse_panels(text: str) -> dict[str, str]:
    panels = {}
    pattern = re.compile(r"## PANEL:\s*(\S+)\s*\n(.*?)(?=\n## PANEL:|\n## DECISION:|\n## STATE:|\Z)", re.DOTALL)
    for match in pattern.finditer(text):
        name = match.group(1).lower()
        content = match.group(2).strip()
        panels[name] = f"## PANEL: {name}\n\n{content}"
    return panels


def _parse_decision(text: str) -> dict | None:
    """Extract ## DECISION: block into a structured dict."""
    match = re.search(r"## DECISION:\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    if not match:
        return None
    block = match.group(1).strip()
    if not block:
        return None
    result = {"title": "", "context": "", "options": []}
    for line in block.splitlines():
        line = line.strip()
        if line.lower().startswith("title:"):
            result["title"] = line[6:].strip()
        elif line.lower().startswith("context:"):
            result["context"] = line[8:].strip()
        elif line.lower().startswith("option:"):
            parts = [p.strip() for p in line[7:].split("|")]
            if parts:
                result["options"].append({
                    "name": parts[0] if len(parts) > 0 else "",
                    "desc": parts[1] if len(parts) > 1 else "",
                    "detail": parts[2] if len(parts) > 2 else "",
                })
    if not result["options"]:
        return None
    return result


def _parse_state(text: str) -> dict | None:
    """Extract JSON from ## STATE: block. Returns dict or None on failure."""
    match = re.search(r"## STATE:\s*\n(\{.*?\})\s*(?:\Z|\n##)", text, re.DOTALL)
    if not match:
        # Try greedy fallback — everything after ## STATE:
        match = re.search(r"## STATE:\s*\n(.+)", text, re.DOTALL)
    if not match:
        return None
    raw = match.group(1).strip()
    # Strip any trailing panel headers or extra text
    brace_depth = 0
    end = 0
    started = False
    for i, ch in enumerate(raw):
        if ch == '{':
            brace_depth += 1
            started = True
        elif ch == '}':
            brace_depth -= 1
        if started and brace_depth == 0:
            end = i + 1
            break
    if end == 0:
        return None
    try:
        return json.loads(raw[:end])
    except json.JSONDecodeError as e:
        print(f"[gemma] State JSON parse error: {e}")
        return None


def _update_state(state_dict: dict):
    """Deep-merge state_dict into state.json, update last_updated, write character files."""
    existing = {}
    if STATE_FILE.exists():
        try:
            existing = json.loads(STATE_FILE.read_text())
        except Exception:
            pass

    # Merge top-level scalar fields (only update non-null values)
    for key in ("location", "combat_active", "round", "initiative_order"):
        val = state_dict.get(key)
        if val is not None:
            existing[key] = val

    # Merge characters dict — deep merge per character
    incoming_chars = state_dict.get("characters", {})
    if incoming_chars:
        existing_chars = existing.get("characters", {})
        for slug, char_data in incoming_chars.items():
            if slug not in existing_chars:
                existing_chars[slug] = {}
            for k, v in char_data.items():
                if v is not None:
                    existing_chars[slug][k] = v
        existing["characters"] = existing_chars

    existing["last_updated"] = datetime.now(timezone.utc).isoformat()

    STATE_FILE.write_text(json.dumps(existing, indent=2))
    chars = existing.get("characters", {})
    alive = [s for s, c in chars.items() if c.get("status") != "dead"]
    enemies = [s for s in alive if chars[s].get("is_enemy")]
    party = [s for s in alive if not chars[s].get("is_enemy")]
    print(f"[gemma] State updated: location={existing.get('location')}, combat={existing.get('combat_active')}, party={len(party)}, enemies={len(enemies)}")

    # Write character markdown files (party only — enemies are tracked in state, not as persistent chars)
    for slug, char in chars.items():
        name = char.get("name")
        if not name or char.get("is_enemy"):
            continue
        path = CHARACTERS_DIR / f"{slug}.md"
        content = f"""---
name: {name}
class: {char.get("class", "")}
hp_current: {char.get("hp") or 0}
hp_max: {char.get("max_hp") or 0}
ac: {char.get("ac") or 0}
notes: "{char.get("notes", "")}"
conditions: {json.dumps(char.get("conditions", []))}
---

# {name}
"""
        path.write_text(content)


def _read_transcript() -> str:
    if not TRANSCRIPT_FILE.exists():
        return ""
    text = TRANSCRIPT_FILE.read_text()
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("# ")]
    # Keep last 150 lines — enough context, keeps prompt short for fast prefill
    return "\n".join(lines[-150:]).strip()


def _read_party_data() -> str:
    parts = []
    for md in sorted(CHARACTERS_DIR.glob("*.md")):
        parts.append(md.read_text())
    return "\n\n".join(parts) if parts else "No character data yet."


async def _broadcast_state():
    if _broadcast_callback and STATE_FILE.exists():
        try:
            await _broadcast_callback({"type": "state", "data": json.loads(STATE_FILE.read_text())})
        except Exception as e:
            print(f"[gemma] State broadcast error: {e}")


async def _do_state_update():
    """Fast path: extract STATE JSON only (~4s). Runs every ~80 transcript chars."""
    transcript = _read_transcript()
    if not transcript:
        return
    current_state = "{}"
    if STATE_FILE.exists():
        try:
            current_state = STATE_FILE.read_text()
        except Exception:
            pass
    party_data = _read_party_data()
    prompt = STATE_PROMPT.format(
        transcript=transcript[-2000:],  # last ~2000 chars is plenty
        party_data=party_data,
        current_state=current_state,
    )
    try:
        print("[gemma] Fast state update...")
        raw = await _call_ollama(prompt, max_tokens=350)
        # Strip any markdown fences the model might add
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
        raw = re.sub(r"\n?```$", "", raw.strip())
        state_dict = None
        try:
            state_dict = json.loads(raw.strip())
        except json.JSONDecodeError:
            # Try extracting first JSON object
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                try:
                    state_dict = json.loads(m.group(0))
                except Exception:
                    pass
        if state_dict:
            _update_state(state_dict)
            await _broadcast_state()
        else:
            print(f"[gemma] State parse failed: {raw[:100]}")
    except Exception as e:
        print(f"[gemma] State update error: {e}")


async def _do_panel_update():
    """Slow path: update all display panels (~20s). Runs every ~300 transcript chars."""
    transcript = _read_transcript()
    if not transcript:
        return
    party_data = _read_party_data()
    current_state = "{}"
    location = "unknown"
    if STATE_FILE.exists():
        try:
            current_state = STATE_FILE.read_text()
            location = json.loads(current_state).get("location") or "unknown"
        except Exception:
            pass
    prompt = PANEL_PROMPT.format(
        transcript=transcript[-3000:],
        party_data=party_data,
        current_state=current_state,
        location=location,
    )
    try:
        print("[gemma] Full panel update...")
        raw = await _call_ollama(prompt, max_tokens=1400)
        panels = _parse_panels(raw)
        print(f"[gemma] Got panels: {list(panels.keys())}")
        if panels:
            for name, content in panels.items():
                path = PANEL_FILES.get(name)
                if path:
                    path.write_text(content)
            if _broadcast_callback:
                await _broadcast_callback({"type": "panels", "data": panels})
        decision = _parse_decision(raw)
        if decision and _broadcast_callback:
            print(f"[gemma] Decision detected: {decision['title']}")
            await _broadcast_callback({"type": "decision", "data": decision})
    except Exception as e:
        print(f"[gemma] Panel update error: {e}")


async def _debounced_loop(queue: asyncio.Queue, debounce_secs: float, handler):
    while True:
        try:
            await asyncio.wait_for(queue.get(), timeout=5.0)
            while not queue.empty():
                queue.get_nowait()
            await asyncio.sleep(debounce_secs)
            while not queue.empty():
                queue.get_nowait()
            await handler()
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[gemma] Loop error: {e}")


def start_update_loop():
    global _state_queue, _panel_queue
    _state_queue = asyncio.Queue()
    _panel_queue = asyncio.Queue()
    asyncio.create_task(_debounced_loop(_state_queue, GEMMA_STATE_DEBOUNCE_SECS, _do_state_update))
    asyncio.create_task(_debounced_loop(_panel_queue, GEMMA_PANEL_DEBOUNCE_SECS, _do_panel_update))


def on_transcript_change():
    """Signal that transcript has changed — queues fast and/or slow updates."""
    global _last_state_transcript_len, _last_panel_transcript_len

    current_len = len(_read_transcript())

    state_delta = current_len - _last_state_transcript_len
    if state_delta >= GEMMA_STATE_TRIGGER_CHARS and _state_queue is not None:
        _last_state_transcript_len = current_len
        print(f"[gemma] +{state_delta} chars — queuing state update")
        try:
            _state_queue.put_nowait(True)
        except asyncio.QueueFull:
            pass

    panel_delta = current_len - _last_panel_transcript_len
    if panel_delta >= GEMMA_PANEL_TRIGGER_CHARS and _panel_queue is not None:
        _last_panel_transcript_len = current_len
        print(f"[gemma] +{panel_delta} chars — queuing panel update")
        try:
            _panel_queue.put_nowait(True)
        except asyncio.QueueFull:
            pass


async def force_update():
    """Immediately run both state and panel updates."""
    global _last_state_transcript_len, _last_panel_transcript_len
    tlen = len(_read_transcript())
    _last_state_transcript_len = tlen
    _last_panel_transcript_len = tlen
    print("[gemma] Forced full update")
    await _do_state_update()
    await _do_panel_update()
