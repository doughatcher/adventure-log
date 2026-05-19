#!/usr/bin/env python3
"""
Campaign journal generator — runs in GitHub Actions after each session push.

Reads the latest session archive, calls Claude to generate:
  1. content/sessions/YYYY-MM-DD-HHMM.md  — narrative journal post
  2. content/characters/<slug>.md          — character pages (updated)
  3. context/next-session-brief.md         — dense AI context for next game

Session archives must be named YYYY-MM-DD-HHMM (e.g. 2026-05-18-2100).
Non-date-named directories in data/sessions/ are ignored.

When multiple archives share the same calendar date, they are merged:
the most recent (by time suffix) is treated as canonical for state/scene/
story-log/next-steps, and all transcripts are concatenated chronologically.

Usage:
    ANTHROPIC_API_KEY=... python .github/scripts/generate_journal.py
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import frontmatter

# ── Paths ──
REPO = Path(__file__).parent.parent.parent
DATA_DIR = REPO / "data"
CONTENT_DIR = REPO / "content"
CONTEXT_DIR = REPO / "context"
SESSIONS_DIR = DATA_DIR / "sessions"
CHARS_DATA_DIR = DATA_DIR / "characters"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{4})$")

# Archives whose timestamps are within this many hours of each other are merged.
SESSION_MERGE_HOURS = 20

MODEL = "claude-sonnet-4-6"
client = anthropic.Anthropic()


# ── Session discovery ──

def _is_date_archive(d: Path) -> bool:
    return d.is_dir() and bool(DATE_RE.match(d.name))


def _parse_archive_dt(d: Path) -> datetime | None:
    """Parse YYYY-MM-DD-HHMM archive name to a UTC datetime, or None."""
    m = DATETIME_RE.match(d.name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def find_session_group() -> tuple[Path, list[Path]] | None:
    """Return (canonical_archive, merged_group) for the latest session.

    Archives are grouped when consecutive timestamps are within SESSION_MERGE_HOURS
    of each other (handles sessions spanning midnight and same-day multi-archives).
    canonical = the archive with the latest timestamp in the group.
    Non-date-named directories are skipped entirely.
    """
    if not SESSIONS_DIR.exists():
        return None

    dated = [(d, _parse_archive_dt(d)) for d in SESSIONS_DIR.iterdir() if _is_date_archive(d)]
    dated = [(d, dt) for d, dt in dated if dt is not None]
    if not dated:
        return None
    dated.sort(key=lambda x: x[1])

    # Walk backwards from the latest archive, absorbing any that fall within the window.
    canonical, canonical_dt = dated[-1]
    group = [canonical]
    for d, dt in reversed(dated[:-1]):
        if (canonical_dt - dt).total_seconds() <= SESSION_MERGE_HOURS * 3600:
            group.append(d)
        else:
            break
    group.sort(key=lambda d: _parse_archive_dt(d))
    return canonical, group


# ── Helpers ──

def read_file(path: Path, max_chars: int = 0) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if max_chars and len(text) > max_chars:
        text = "...[truncated]\n" + text[-max_chars:]
    return text


def merge_transcripts(archives: list[Path], max_chars: int = 8000) -> str:
    """Concatenate transcripts from multiple archives, oldest first.

    Whisper output is noisy — strip lines that look like ASR artefacts
    (repeated filler phrases, URL spam, generic meta-commentary) before
    passing to Claude so it spends tokens on actual game content.
    """
    NOISE = re.compile(
        r"(character(?:s)? (?:are|is) often (?:called|described|used)|"
        r"globalonenessproject\.org|www\.\S+\.(?:com|org|au)|"
        r"audio from (?:a )?tabletop|tabletop role-playing game session|"
        r"for more information|visit (?:our |the )?website|"
        r"gameplay\s*$|sound effects from the game|"
        r"the game(?:'s gameplay)? is (?:a game|played|pretty|also)|"
        r"the character(?:'s character)? is (?:a|the) (?:human|monster|character))",
        re.IGNORECASE,
    )

    parts = []
    for archive in archives:
        raw = read_file(archive / "transcript.md")
        if not raw:
            continue
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not NOISE.search(stripped):
                lines.append(line)
        cleaned = "\n".join(lines).strip()
        if cleaned:
            parts.append(f"[from archive {archive.name}]\n{cleaned}")

    combined = "\n\n".join(parts)
    if max_chars and len(combined) > max_chars:
        combined = "...[truncated]\n" + combined[-max_chars:]
    return combined


def load_existing_characters() -> dict[str, dict]:
    """Load content/characters/*.md frontmatter + body for context."""
    chars = {}
    char_content_dir = CONTENT_DIR / "characters"
    if char_content_dir.exists():
        for md in char_content_dir.glob("*.md"):
            if md.stem.startswith("_"):
                continue
            try:
                post = frontmatter.load(str(md))
                chars[md.stem] = {
                    "frontmatter": dict(post.metadata),
                    "body": post.content[:1000],
                }
            except Exception:
                pass
    return chars


def load_char_stats() -> dict[str, dict]:
    """Load data/characters/*.md for HP/AC stats (written by gemma.py)."""
    stats = {}
    if not CHARS_DATA_DIR.exists():
        return stats
    for md in CHARS_DATA_DIR.glob("*.md"):
        try:
            post = frontmatter.load(str(md))
            stats[md.stem] = dict(post.metadata)
        except Exception:
            pass
    return stats


def call_claude(system: str, user: str, max_tokens: int = 2000) -> str:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text.strip()


# ── Phase 1: Session journal ──

JOURNAL_SYSTEM = """You are a chronicler writing narrative session journals for a D&D campaign website.
Style: in-world prose, past tense, evocative but not overwrought, 4-7 paragraphs.
Write as an omniscient narrator who witnessed everything. Use character names, not player names.
Include: what happened scene by scene, dramatic moments, consequences, atmosphere, NPC interactions.
The transcript is a noisy Whisper transcription — extract real in-game dialogue and events; ignore
out-of-character chatter, rules discussion, and any lines that are clearly transcription garbage.
Omit: OOC chat, meta-game talk, rules mechanics. Prose only — no headers, no bullet points."""

JOURNAL_USER = """Write a narrative journal entry for this D&D session.

SCENE AT SESSION END:
{scene}

STORY LOG (key events):
{story_log}

NEXT STEPS (hooks going forward):
{next_steps}

TRANSCRIPT (cleaned Whisper output — extract real game events, ignore noise):
{transcript}

GAME STATE:
{state}

CAMPAIGN BACKGROUND:
{history}

Output ONLY the journal prose — no titles, no headers, no frontmatter."""


def generate_journal(canonical: Path, all_archives: list[Path], state: dict, history: str) -> str:
    print("[journal] Generating session narrative...")
    scene = read_file(canonical / "scene.md").replace("## PANEL: scene", "").strip()
    story = read_file(canonical / "story-log.md").replace("## PANEL: story-log", "").strip()
    nexts = read_file(canonical / "next-steps.md").replace("## PANEL: next-steps", "").strip()
    transcript = merge_transcripts(all_archives, max_chars=8000)
    state_str = json.dumps(state, indent=2)[:2000]
    prose = call_claude(
        JOURNAL_SYSTEM,
        JOURNAL_USER.format(
            scene=scene or "(not recorded)",
            story_log=story or "(not recorded)",
            next_steps=nexts or "(not recorded)",
            transcript=transcript or "(empty)",
            state=state_str,
            history=history,
        ),
        max_tokens=2000,
    )
    return prose


def find_existing_session_file(date_str: str) -> Path | None:
    """Return an existing content/sessions file for this date, if any."""
    sessions_dir = CONTENT_DIR / "sessions"
    if not sessions_dir.exists():
        return None
    # Prefer files that start with the date prefix over generic -session.md
    candidates = sorted(sessions_dir.glob(f"{date_str}*.md"))
    # Exclude _index.md and the generic -session.md fallback so we land on any
    # hand-named file (e.g. 2026-05-18-2100.md) before the generated one.
    hand_named = [f for f in candidates if not f.name.endswith("-session.md") and not f.stem.startswith("_")]
    if hand_named:
        return hand_named[0]
    generated = [f for f in candidates if f.name.endswith("-session.md")]
    return generated[0] if generated else None


def write_session_page(canonical_name: str, prose: str, state: dict) -> Path:
    session_name = state.get("session_name") or "Session"
    location = state.get("location") or ""
    date_str = canonical_name[:10]

    # Reuse an existing file for this date rather than creating a parallel one.
    existing = find_existing_session_file(date_str)
    out = existing if existing else CONTENT_DIR / "sessions" / f"{date_str}-session.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    post = frontmatter.Post(
        prose,
        title=f"{session_name}",
        date=f"{date_str}T00:00:00Z",
        location=location,
        source_archive=canonical_name,
    )
    out.write_text(frontmatter.dumps(post))
    print(f"[journal] Wrote session page: {out}")
    return out


# ── Phase 2: Character pages ──

CHAR_SYSTEM = """You are writing character profile pages for a D&D campaign website.
For each character, write 2-4 short paragraphs of personality, inferred backstory, and what they did this session.
Base inferences on how they spoke and acted in the transcript. Ignore transcript noise (ASR garbage).
Be evocative but not overwrought. Use in-world perspective — these are real people in the world."""

CHAR_USER = """Update character profiles based on this session.

CHARACTERS IN GAME STATE:
{char_list}

SESSION STORY LOG:
{story_log}

TRANSCRIPT (cleaned — extract real in-game moments only):
{transcript}

EXISTING CHARACTER PROFILES (preserve continuity, build on these):
{existing}

For each character slug in the game state, output a block:
## CHARACTER: <slug>
<2-4 paragraphs of narrative profile prose>
## END

Output ALL party characters (is_enemy=false). Skip enemies."""


def generate_characters(canonical: Path, all_archives: list[Path], state: dict) -> dict[str, str]:
    print("[journal] Generating character profiles...")
    chars = state.get("characters", {})
    party = {s: c for s, c in chars.items() if not c.get("is_enemy") and c.get("status") != "dead"}
    if not party:
        print("[journal] No party characters found in state, skipping characters.")
        return {}

    existing = load_existing_characters()
    existing_text = ""
    for slug, data in existing.items():
        existing_text += f"\n### {slug}\n{data['body']}\n"

    char_list = "\n".join(
        f"- {s}: {c.get('name',s)} ({c.get('class','?')}) HP:{c.get('hp','?')}/{c.get('max_hp','?')} status:{c.get('status','alive')}"
        for s, c in party.items()
    )

    story = read_file(canonical / "story-log.md").replace("## PANEL: story-log", "").strip()
    transcript = merge_transcripts(all_archives, max_chars=5000)

    raw = call_claude(
        CHAR_SYSTEM,
        CHAR_USER.format(
            char_list=char_list,
            story_log=story or "(not recorded)",
            transcript=transcript or "(empty)",
            existing=existing_text or "(no existing profiles yet)",
        ),
        max_tokens=2000,
    )

    results = {}
    for block in re.split(r"^## CHARACTER:\s*", raw, flags=re.MULTILINE):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        slug = lines[0].strip().lower().replace(" ", "-")
        prose = "\n".join(lines[1:]).replace("## END", "").strip()
        if slug and prose:
            results[slug] = prose
    return results


def write_character_pages(char_prose: dict[str, str], state: dict) -> None:
    chars = state.get("characters", {})
    char_stats = load_char_stats()
    out_dir = CONTENT_DIR / "characters"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for slug, prose in char_prose.items():
        char_data = chars.get(slug, {})
        stats = char_stats.get(slug, {})

        existing_path = out_dir / f"{slug}.md"
        existing_fm = {}
        if existing_path.exists():
            try:
                existing_post = frontmatter.load(str(existing_path))
                existing_fm = dict(existing_post.metadata)
            except Exception:
                pass

        hp = char_data.get("hp") or stats.get("hp_current") or existing_fm.get("hp_current", 0)
        max_hp = char_data.get("max_hp") or stats.get("hp_max") or existing_fm.get("hp_max", 0)
        ac = char_data.get("ac") or stats.get("ac") or existing_fm.get("ac", 0)

        post = frontmatter.Post(
            prose,
            title=char_data.get("name") or existing_fm.get("title") or slug.replace("-", " ").title(),
            slug=slug,
            **{"class": char_data.get("class") or stats.get("class") or existing_fm.get("class", "Adventurer")},
            hp_current=hp or 0,
            hp_max=max_hp or 0,
            ac=ac or 0,
            status=char_data.get("status") or "alive",
            last_updated=today,
        )
        (out_dir / f"{slug}.md").write_text(frontmatter.dumps(post))
        print(f"[journal] Wrote character: {slug}")


# ── Phase 3: Next-session context brief ──

BRIEF_SYSTEM = """You write dense, structured AI context briefs for D&D session assistants.
This is NOT for human readers — it feeds directly into AI prompts.
Be information-dense. Short sentences. Every word earns its place."""

BRIEF_USER = """Generate a next-session context brief from this session's data.

GAME STATE (end of session):
{state}

STORY LOG:
{story_log}

NEXT STEPS:
{next_steps}

CAMPAIGN HISTORY:
{history}

Output in EXACTLY this format (no additional sections):

LOCATION: <current in-game location>
PARTY: <one line per character: Name (Class) HP/maxHP AC>
RECENT EVENTS:
- <most important event>
- <second most important>
- (5-10 bullets, most impactful first)
OPEN THREADS:
- <unresolved plot hook or pending decision>
- (2-5 bullets)
KEY NPCS:
- Name: <one line — role, disposition, last known status>
- (only NPCs who matter now)
PARTY CONDITION: <1-2 sentences on overall health, resources spent, morale>
CAMPAIGN CONTEXT: <1 paragraph of world/setting context relevant to next session>"""


def generate_brief(canonical: Path, state: dict, history: str) -> str:
    print("[journal] Generating next-session brief...")
    story = read_file(canonical / "story-log.md").replace("## PANEL: story-log", "").strip()
    nexts = read_file(canonical / "next-steps.md").replace("## PANEL: next-steps", "").strip()
    return call_claude(
        BRIEF_SYSTEM,
        BRIEF_USER.format(
            state=json.dumps(state, indent=2)[:3000],
            story_log=story or "(not recorded)",
            next_steps=nexts or "(not recorded)",
            history=history,
        ),
        max_tokens=1200,
    )


def write_brief(brief: str) -> None:
    out = CONTEXT_DIR / "next-session-brief.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Next Session Brief\n\n"
        "*Auto-generated. Do not edit — will be overwritten after each session.*\n\n"
        f"{brief}\n"
    )
    print(f"[journal] Wrote context brief: {out}")


# ── Main ──

def main():
    result = find_session_group()
    if not result:
        print("[journal] No date-named session archives found. Exiting.")
        sys.exit(0)

    canonical, all_archives = result
    print(f"[journal] Canonical archive: {canonical.name}")
    if len(all_archives) > 1:
        print(f"[journal] Merging {len(all_archives)} archives for {canonical.name[:10]}: {[a.name for a in all_archives]}")

    state_path = canonical / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    history = read_file(CONTEXT_DIR / "campaign-history.md")

    try:
        prose = generate_journal(canonical, all_archives, state, history)
        write_session_page(canonical.name, prose, state)
    except Exception as e:
        print(f"[journal] ERROR — journal generation failed: {e}")

    try:
        char_prose = generate_characters(canonical, all_archives, state)
        if char_prose:
            write_character_pages(char_prose, state)
    except Exception as e:
        print(f"[journal] ERROR — character generation failed: {e}")

    try:
        brief = generate_brief(canonical, state, history)
        write_brief(brief)
    except Exception as e:
        print(f"[journal] ERROR — brief generation failed: {e}")

    print("[journal] Done.")


if __name__ == "__main__":
    main()
