#!/usr/bin/env python3
"""
Campaign journal generator — runs in GitHub Actions after each session push.

The table runs two campaigns and alternates between them, so the first question
this script has to answer is *which game was that*. Every session archive says
so in its `state.json`, and everything downstream is scoped by the answer:

  content/sessions/<campaign>/YYYY-MM-DD-HHMM.md    — narrative journal post
  content/characters/<campaign>/<slug>.md           — character pages
  context/campaigns/<campaign>/next-session-brief.md — dense AI context

The scoping is not cosmetic. An unscoped run reads every character page in the
repository as "existing profiles", hands them all to the model as context, and
writes back whatever comes out — which is how a barbarian from the Shard Sea
campaign twice acquired a death scene during a session she was not in. A
character is only ever written under the campaign that owns her, and only if she
is in that night's party.

Session archives are named YYYY-MM-DD or YYYY-MM-DD-HHMM. When several share a
date, the most recent is canonical for state/scene/story-log/next-steps and all
transcripts are concatenated chronologically. Archives from different campaigns
are never merged, whatever their timestamps say.

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
import yaml

# ── Paths ──
REPO = Path(__file__).parent.parent.parent
DATA_DIR = REPO / "data"
CONTENT_DIR = REPO / "content"
CONTEXT_DIR = REPO / "context"
SESSIONS_DIR = DATA_DIR / "sessions"
CHARS_DATA_DIR = DATA_DIR / "characters"
CAMPAIGNS_FILE = DATA_DIR / "campaigns.yaml"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
# The time suffix is optional: an archive imported by hand may be just a date,
# or a date plus a word. Both still belong to a night that needs writing up.
DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-(\d{4}))?(?:-.*)?$")

# Archives whose timestamps are within this many hours of each other are merged.
SESSION_MERGE_HOURS = 20

MODEL = "claude-sonnet-4-6"
client = anthropic.Anthropic()


# ── Campaigns ──

def load_campaigns() -> dict[str, dict]:
    """The registry in data/campaigns.yaml, keyed by slug."""
    if not CAMPAIGNS_FILE.exists():
        return {}
    entries = yaml.safe_load(CAMPAIGNS_FILE.read_text()) or []
    return {e["slug"]: e for e in entries if e.get("slug")}


CAMPAIGNS = load_campaigns()


def campaign_of(archive: Path) -> str | None:
    """Which campaign an archive belongs to, or None if it does not say.

    Read from state.json rather than guessed from the folder name or the
    character list. Guessing is how the two campaigns got mixed in the first
    place, and a wrong guess writes a session into the wrong story — so an
    archive that does not declare its campaign is skipped and reported, not
    filed somewhere plausible.
    """
    state_path = archive / "state.json"
    if state_path.exists():
        try:
            slug = json.loads(state_path.read_text()).get("campaign")
        except Exception:
            slug = None
        if slug:
            return slug if slug in CAMPAIGNS else None
    marker = archive / "campaign"
    if marker.exists():
        slug = marker.read_text().strip()
        return slug if slug in CAMPAIGNS else None
    return None


def sessions_dir(campaign: str) -> Path:
    return CONTENT_DIR / "sessions" / campaign


def characters_dir(campaign: str) -> Path:
    return CONTENT_DIR / "characters" / campaign


def context_dir(campaign: str) -> Path:
    return CONTEXT_DIR / "campaigns" / campaign


# ── Session discovery ──

def _is_date_archive(d: Path) -> bool:
    return d.is_dir() and bool(DATE_RE.match(d.name))


def _parse_archive_dt(d: Path) -> datetime | None:
    """Parse an archive name to a UTC datetime, or None if it is not one.

    `YYYY-MM-DD-HHMM` gives a real time; a bare `YYYY-MM-DD`, or a date with a
    word after it, sorts at midnight. The looser forms exist because archives
    imported by hand have not always carried a time, and one that cannot be
    parsed is one that never gets written up.
    """
    m = DATETIME_RE.match(d.name)
    if not m:
        return None
    time_part = m.group(2) or "0000"
    try:
        return datetime.strptime(f"{m.group(1)} {time_part}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def find_session_group() -> tuple[Path, list[Path], str] | None:
    """Return (canonical_archive, merged_group, campaign) for the latest session.

    Archives are grouped when consecutive timestamps are within
    SESSION_MERGE_HOURS of each other — which handles a session spanning
    midnight, and an evening filed as several archives — but **only within one
    campaign**. Two campaigns played on consecutive nights would otherwise merge
    into a single incoherent write-up, and the window is wide enough for that to
    be a real possibility rather than a theoretical one.

    canonical = the archive with the latest timestamp in the group.
    """
    if not SESSIONS_DIR.exists():
        return None

    dated = [(d, _parse_archive_dt(d)) for d in SESSIONS_DIR.iterdir() if _is_date_archive(d)]
    dated = [(d, dt) for d, dt in dated if dt is not None]
    if not dated:
        return None
    dated.sort(key=lambda x: x[1])

    canonical, canonical_dt = dated[-1]
    campaign = campaign_of(canonical)
    if not campaign:
        print(f"[journal] {canonical.name} does not name a campaign in its state.json — "
              f"refusing to guess. Add a `campaign` key with one of: "
              f"{', '.join(sorted(CAMPAIGNS)) or '(registry empty)'}")
        return None

    group = [canonical]
    for d, dt in reversed(dated[:-1]):
        if (canonical_dt - dt).total_seconds() > SESSION_MERGE_HOURS * 3600:
            break
        if campaign_of(d) != campaign:
            # Close in time, different game. Not the same evening.
            continue
        group.append(d)
    group.sort(key=lambda d: _parse_archive_dt(d))
    return canonical, group, campaign


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


def load_existing_characters(campaign: str) -> dict[str, dict]:
    """Existing profiles for one campaign, as continuity context for the model.

    Scoped deliberately. Handing the model every character in the repository
    invites it to write about people who were not at the table, and it has taken
    that invitation before.
    """
    chars = {}
    char_content_dir = characters_dir(campaign)
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


def campaign_owning(slug: str) -> str | None:
    """The campaign whose roster contains this slug, if any.

    Used as a guard before writing: a slug that belongs to another campaign is
    never written under this one, no matter what the model returned.
    """
    for camp in CAMPAIGNS:
        if (characters_dir(camp) / f"{slug}.md").exists():
            return camp
        if (CHARS_DATA_DIR / camp / f"{slug}.md").exists():
            return camp
    return None


def load_char_stats(campaign: str) -> dict[str, dict]:
    """Load data/characters/<campaign>/*.md for HP/AC stats (written by gemma.py)."""
    stats = {}
    stats_dir = CHARS_DATA_DIR / campaign
    if not stats_dir.exists():
        return stats
    for md in stats_dir.glob("*.md"):
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
Omit: OOC chat, meta-game talk, rules mechanics. Prose only — no headers, no bullet points.

This table runs more than one campaign. You are writing about ONE of them, named below.
Write only about the party listed in the game state. Do not mention, introduce, or carry over
characters, places or plot from any other campaign, and do not invent a character who is not in
that party — if the transcript is thin, write less rather than filling the gap."""

JOURNAL_USER = """Write a narrative journal entry for this D&D session.

CAMPAIGN: {campaign_title} — {campaign_setting}
This entry belongs to that campaign and no other.

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


TITLE_SYSTEM = """You write titles for D&D campaign journal entries.
Rules: 2-5 words. Evocative, in-world, specific to what happened. No generic phrases like
"Session", "The Adventure", "A New Chapter". Draw from a key moment, location, NPC, or turning point.
Examples of good titles: "Poison and Rebels", "The Purposeful Currents", "Duren at Pier Seven",
"What the Priest Would Not Say", "Lyvriele Does Not Fall"."""

TITLE_USER = """Write a session title for this D&D session.

CAMPAIGN: {campaign_title}

SCENE AT SESSION END:
{scene}

STORY LOG:
{story_log}

TRANSCRIPT EXCERPT (first 1000 chars):
{transcript_excerpt}

Output ONLY the title — no quotes, no punctuation at the end, no explanation."""


def generate_title(canonical: Path, all_archives: list[Path], campaign: dict) -> str:
    scene = read_file(canonical / "scene.md").replace("## PANEL: scene", "").strip()
    story = read_file(canonical / "story-log.md").replace("## PANEL: story-log", "").strip()
    transcript = merge_transcripts(all_archives, max_chars=1000)
    return call_claude(
        TITLE_SYSTEM,
        TITLE_USER.format(
            campaign_title=campaign["title"],
            scene=scene or "(not recorded)",
            story_log=story or "(not recorded)",
            transcript_excerpt=transcript or "(empty)",
        ),
        max_tokens=30,
    )


def generate_journal(canonical: Path, all_archives: list[Path], state: dict, history: str,
                     campaign: dict) -> str:
    print("[journal] Generating session narrative...")
    scene = read_file(canonical / "scene.md").replace("## PANEL: scene", "").strip()
    story = read_file(canonical / "story-log.md").replace("## PANEL: story-log", "").strip()
    nexts = read_file(canonical / "next-steps.md").replace("## PANEL: next-steps", "").strip()
    transcript = merge_transcripts(all_archives, max_chars=8000)
    state_str = json.dumps(state, indent=2)[:2000]
    prose = call_claude(
        JOURNAL_SYSTEM,
        JOURNAL_USER.format(
            campaign_title=campaign["title"],
            campaign_setting=campaign.get("setting", "unspecified"),
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


def find_existing_session_file(campaign: str, date_str: str) -> Path | None:
    """Return an existing journal page for this campaign and date, if any."""
    d = sessions_dir(campaign)
    if not d.exists():
        return None
    candidates = sorted(d.glob(f"{date_str}*.md"))
    hand_named = [f for f in candidates if not f.name.endswith("-session.md") and not f.stem.startswith("_")]
    if hand_named:
        return hand_named[0]
    generated = [f for f in candidates if f.name.endswith("-session.md")]
    return generated[0] if generated else None


def is_hand_edited(path: Path) -> bool:
    """Return True if this file should not be overwritten by the generator.

    A file is hand-edited if it exists and has generated: false (or no generated field).
    Files written by the generator carry generated: true.
    """
    if not path.exists():
        return False
    try:
        post = frontmatter.load(str(path))
        return not post.metadata.get("generated", False)
    except Exception:
        return True


def write_session_page(canonical_name: str, title: str, prose: str, state: dict,
                       campaign: str) -> Path | None:
    location = state.get("location") or ""
    date_str = canonical_name[:10]

    existing = find_existing_session_file(campaign, date_str)
    if existing and is_hand_edited(existing):
        print(f"[journal] Skipping session page — hand-edited file exists: {existing}")
        return existing

    out = existing if existing else sessions_dir(campaign) / f"{date_str}-session.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    post = frontmatter.Post(
        prose,
        title=title,
        date=f"{date_str}T00:00:00Z",
        campaign=campaign,
        generated=True,
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
Be evocative but not overwrought. Use in-world perspective — these are real people in the world.

This table runs more than one campaign. Output a block for EXACTLY the character slugs listed in
the game state and no others — those are the only people who were at this table tonight. Never
write about, kill off, or change the fate of a character who is not on that list."""

CHAR_USER = """Update character profiles based on this session.

CAMPAIGN: {campaign_title}

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


def generate_characters(canonical: Path, all_archives: list[Path], state: dict,
                        campaign: dict) -> dict[str, str]:
    print("[journal] Generating character profiles...")
    chars = state.get("characters", {})
    party = {s: c for s, c in chars.items() if not c.get("is_enemy") and c.get("status") != "dead"}
    if not party:
        print("[journal] No party characters found in state, skipping characters.")
        return {}

    existing = load_existing_characters(campaign["slug"])
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
            campaign_title=campaign["title"],
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


def write_character_pages(char_prose: dict[str, str], state: dict, campaign: str) -> None:
    chars = state.get("characters", {})
    char_stats = load_char_stats(campaign)
    out_dir = characters_dir(campaign)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for slug, prose in char_prose.items():
        # Two guards, because the model has previously written about people who
        # were not there. First: only characters the night's state actually
        # names. Second: never a slug another campaign owns — that is the exact
        # shape of the bug where a Shard Sea barbarian got killed off during a
        # session of Courts of the Shadow Fey.
        if slug not in chars:
            print(f"[journal] Skipping '{slug}' — not in this session's party.")
            continue
        owner = campaign_owning(slug)
        if owner and owner != campaign:
            print(f"[journal] Refusing to write '{slug}' under {campaign} — belongs to {owner}.")
            continue

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

        extra = {}
        for key in ("player", "race", "level"):
            if existing_fm.get(key) is not None:
                extra[key] = existing_fm[key]

        post = frontmatter.Post(
            prose,
            title=char_data.get("name") or existing_fm.get("title") or slug.replace("-", " ").title(),
            slug=slug,
            campaign=campaign,
            **{"class": char_data.get("class") or stats.get("class") or existing_fm.get("class", "Adventurer")},
            hp_current=hp or 0,
            hp_max=max_hp or 0,
            ac=ac or 0,
            status=char_data.get("status") or existing_fm.get("status") or "alive",
            last_updated=today,
            **extra,
        )
        existing_path.write_text(frontmatter.dumps(post))
        print(f"[journal] Wrote character: {campaign}/{slug}")


# ── Phase 3: Next-session context brief ──

BRIEF_SYSTEM = """You write dense, structured AI context briefs for D&D session assistants.
This is NOT for human readers — it feeds directly into AI prompts.
Be information-dense. Short sentences. Every word earns its place."""

BRIEF_USER = """Generate a next-session context brief from this session's data.

CAMPAIGN: {campaign_title} — {campaign_setting}
Everything below concerns that campaign only.

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


def generate_brief(canonical: Path, state: dict, history: str, campaign: dict) -> str:
    print("[journal] Generating next-session brief...")
    story = read_file(canonical / "story-log.md").replace("## PANEL: story-log", "").strip()
    nexts = read_file(canonical / "next-steps.md").replace("## PANEL: next-steps", "").strip()
    return call_claude(
        BRIEF_SYSTEM,
        BRIEF_USER.format(
            campaign_title=campaign["title"],
            campaign_setting=campaign.get("setting", "unspecified"),
            state=json.dumps(state, indent=2)[:3000],
            story_log=story or "(not recorded)",
            next_steps=nexts or "(not recorded)",
            history=history,
        ),
        max_tokens=1200,
    )


def write_brief(brief: str, campaign: str) -> None:
    out = context_dir(campaign) / "next-session-brief.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Next Session Brief\n\n"
        "*Auto-generated. Do not edit — will be overwritten after each session.*\n\n"
        f"{brief}\n"
    )
    print(f"[journal] Wrote context brief: {out}")


# ── Main ──

def main():
    if not CAMPAIGNS:
        print(f"[journal] No campaigns defined in {CAMPAIGNS_FILE}. Exiting.")
        sys.exit(0)

    result = find_session_group()
    if not result:
        print("[journal] No processable session archive found. Exiting.")
        sys.exit(0)

    canonical, all_archives, slug = result
    campaign = {**CAMPAIGNS[slug], "slug": slug}
    print(f"[journal] Campaign: {campaign['title']} ({slug})")
    print(f"[journal] Canonical archive: {canonical.name}")
    if len(all_archives) > 1:
        print(f"[journal] Merging {len(all_archives)} archives for {canonical.name[:10]}: {[a.name for a in all_archives]}")

    state_path = canonical / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    history = read_file(context_dir(slug) / "campaign-history.md")
    if not history:
        print(f"[journal] WARNING — no campaign history at {context_dir(slug) / 'campaign-history.md'}; "
              "the narrative will have no backstory to build on.")

    try:
        title = generate_title(canonical, all_archives, campaign)
        print(f"[journal] Session title: {title}")
        prose = generate_journal(canonical, all_archives, state, history, campaign)
        write_session_page(canonical.name, title, prose, state, slug)
    except Exception as e:
        print(f"[journal] ERROR — journal generation failed: {e}")

    try:
        char_prose = generate_characters(canonical, all_archives, state, campaign)
        if char_prose:
            write_character_pages(char_prose, state, slug)
    except Exception as e:
        print(f"[journal] ERROR — character generation failed: {e}")

    try:
        brief = generate_brief(canonical, state, history, campaign)
        write_brief(brief, slug)
    except Exception as e:
        print(f"[journal] ERROR — brief generation failed: {e}")

    print("[journal] Done.")


if __name__ == "__main__":
    main()
