#!/usr/bin/env python3
"""
Campaign journal generator — runs in GitHub Actions after each session push.

Reads the latest session archive, calls Claude to generate:
  1. content/sessions/YYYY-MM-DD-HHMM.md  — narrative journal post
  2. content/characters/<slug>.md          — character pages (updated)
  3. context/next-session-brief.md         — dense AI context for next game

Usage:
    ANTHROPIC_API_KEY=... python .github/scripts/generate_journal.py
"""
import json
import os
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

MODEL = "claude-sonnet-4-6"
client = anthropic.Anthropic()


# ── Helpers ──

def find_latest_session() -> Path | None:
    if not SESSIONS_DIR.exists():
        return None
    dirs = sorted([d for d in SESSIONS_DIR.iterdir() if d.is_dir()])
    return dirs[-1] if dirs else None


def read_file(path: Path, max_chars: int = 0) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if max_chars and len(text) > max_chars:
        text = "...[truncated]\n" + text[-max_chars:]
    return text


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
Style: in-world prose, past tense, evocative but not overwrought, 3-5 paragraphs.
Write as an omniscient narrator who witnessed everything. Use character names, not player names.
Include: what happened, dramatic moments, consequences, atmosphere.
Omit: rules discussion, OOC chat, meta-game talk."""

JOURNAL_USER = """Write a narrative journal entry for this D&D session.

SCENE AT SESSION END:
{scene}

STORY LOG (key events):
{story_log}

NEXT STEPS (hooks going forward):
{next_steps}

TRANSCRIPT EXCERPT (last portion of session):
{transcript}

GAME STATE:
{state}

CAMPAIGN BACKGROUND:
{history}

Output ONLY the journal prose — no titles, no headers, no frontmatter."""


def generate_journal(archive: Path, state: dict, history: str) -> str:
    print("[journal] Generating session narrative...")
    scene = read_file(archive / "scene.md").replace("## PANEL: scene", "").strip()
    story = read_file(archive / "story-log.md").replace("## PANEL: story-log", "").strip()
    nexts = read_file(archive / "next-steps.md").replace("## PANEL: next-steps", "").strip()
    transcript = read_file(archive / "transcript.md", max_chars=6000)
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
        max_tokens=1500,
    )
    return prose


def write_session_page(archive_name: str, prose: str, state: dict) -> Path:
    session_name = state.get("session_name") or "Session"
    location = state.get("location") or ""
    date_str = archive_name[:10]  # "2026-04-20"

    post = frontmatter.Post(
        prose,
        title=f"{session_name}",
        date=f"{date_str}T00:00:00Z",
        location=location,
        source_archive=archive_name,
    )
    out = CONTENT_DIR / "sessions" / f"{archive_name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(frontmatter.dumps(post))
    print(f"[journal] Wrote session page: {out}")
    return out


# ── Phase 2: Character pages ──

CHAR_SYSTEM = """You are writing character profile pages for a D&D campaign website.
For each character, write 2-4 short paragraphs of personality, inferred backstory, and what they did this session.
Base inferences on how they spoke and acted. Be evocative but not overwrought.
Use in-world perspective — these are real people in the world."""

CHAR_USER = """Update character profiles based on this session.

CHARACTERS IN GAME STATE:
{char_list}

SESSION STORY LOG:
{story_log}

TRANSCRIPT EXCERPT:
{transcript}

EXISTING CHARACTER PROFILES (preserve continuity, build on these):
{existing}

For each character slug in the game state, output a block:
## CHARACTER: <slug>
<2-4 paragraphs of narrative profile prose>
## END

Output ALL party characters (is_enemy=false). Skip enemies."""


def generate_characters(archive: Path, state: dict) -> dict[str, str]:
    print("[journal] Generating character profiles...")
    chars = state.get("characters", {})
    party = {s: c for s, c in chars.items() if not c.get("is_enemy") and c.get("status") != "dead"}
    if not party:
        print("[journal] No party characters found in state, skipping characters.")
        return {}

    existing = load_existing_characters()
    existing_text = ""
    for slug, data in existing.items():
        fm = data["frontmatter"]
        existing_text += f"\n### {slug}\n{data['body']}\n"

    char_list = "\n".join(
        f"- {s}: {c.get('name',s)} ({c.get('class','?')}) HP:{c.get('hp','?')}/{c.get('max_hp','?')} status:{c.get('status','alive')}"
        for s, c in party.items()
    )

    story = read_file(archive / "story-log.md").replace("## PANEL: story-log", "").strip()
    transcript = read_file(archive / "transcript.md", max_chars=4000)

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

    # Parse ## CHARACTER: <slug> ... ## END blocks
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

        # Load existing page to preserve any hand-written content
        existing_path = out_dir / f"{slug}.md"
        existing_fm = {}
        if existing_path.exists():
            try:
                existing_post = frontmatter.load(str(existing_path))
                existing_fm = dict(existing_post.metadata)
            except Exception:
                pass

        # Merge: game state wins for stats, existing wins for hand-written fields
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


def generate_brief(archive: Path, state: dict, history: str) -> str:
    print("[journal] Generating next-session brief...")
    story = read_file(archive / "story-log.md").replace("## PANEL: story-log", "").strip()
    nexts = read_file(archive / "next-steps.md").replace("## PANEL: next-steps", "").strip()
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
    out.write_text(f"# Next Session Brief\n\n*Auto-generated. Do not edit — will be overwritten after each session.*\n\n{brief}\n")
    print(f"[journal] Wrote context brief: {out}")


# ── Main ──

def main():
    archive = find_latest_session()
    if not archive:
        print("[journal] No session archives found. Exiting.")
        sys.exit(0)

    print(f"[journal] Processing archive: {archive.name}")

    # Load state
    state_path = archive / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    # Load campaign history
    history = read_file(CONTEXT_DIR / "campaign-history.md")

    # Phase 1: Journal
    try:
        prose = generate_journal(archive, state, history)
        write_session_page(archive.name, prose, state)
    except Exception as e:
        print(f"[journal] ERROR — journal generation failed: {e}")

    # Phase 2: Characters
    try:
        char_prose = generate_characters(archive, state)
        if char_prose:
            write_character_pages(char_prose, state)
    except Exception as e:
        print(f"[journal] ERROR — character generation failed: {e}")

    # Phase 3: Brief
    try:
        brief = generate_brief(archive, state, history)
        write_brief(brief)
    except Exception as e:
        print(f"[journal] ERROR — brief generation failed: {e}")

    print("[journal] Done.")


if __name__ == "__main__":
    main()
