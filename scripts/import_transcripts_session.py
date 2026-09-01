#!/usr/bin/env python3
"""Turn a finished Transcripts session into a journal archive under data/sessions/.

Transcripts (the Mac recorder) files one Markdown document per recording. An
evening at the table is several of those, and its `onComplete` hook hands us all
of them at once. This script folds them into the single `transcript.md` that
`generate_journal.py` reads, and opens a pull request rather than pushing to
main — the repository is public, and a session should be looked at by a person
before it is.

    scripts/import_transcripts_session.py \
        --slug 2026-08-31-1927 \
        --started-at 2026-08-31T23:27:53Z \
        --transcripts "$(cat paths.txt)"

The one rule worth stating plainly: **nothing before the session start is
published.** A recording often begins well before the game does — the recorder
may have been running through a work call all afternoon — and this repository is
public. Turns are stamped relative to their own recording's start, so combining
that with each document's `recorded_at` gives a real wall-clock time for every
line, and anything earlier than the session is dropped.

If a document cannot be placed on that timeline (an older transcript written
before Transcripts stamped its turns), the script refuses rather than guessing.
Publishing an untrimmed transcript to a public site is not a recoverable
mistake.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# `**Speaker:** [1:02:03] text` or `**Speaker:** [12:04] text`
TURN = re.compile(r"^\*\*(?P<speaker>[^*]+):\*\*\s*(?:\[(?P<stamp>\d+:\d{2}(?::\d{2})?)\]\s*)?(?P<text>.*)$")


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def stamp_seconds(stamp: str) -> int:
    parts = [int(p) for p in stamp.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def frontmatter_value(text: str, key: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    for line in text[4:end].splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


def transcript_section(text: str) -> list[str]:
    """The lines under `## Transcript`, which is where the turns live."""
    marker = "\n## Transcript"
    at = text.find(marker)
    if at == -1:
        return []
    return text[at + len(marker):].splitlines()


class Refusal(Exception):
    """Raised when the session cannot be trimmed safely."""


def collect_turns(paths: list[Path], session_start: dt.datetime,
                  session_end: dt.datetime | None) -> tuple[list[tuple[dt.datetime, str, str]], dict]:
    turns: list[tuple[dt.datetime, str, str]] = []
    stats = {"documents": 0, "kept": 0, "dropped_before_session": 0, "dropped_after_session": 0}

    for path in paths:
        text = path.read_text(encoding="utf-8")
        recorded_at_raw = frontmatter_value(text, "recorded_at")
        if not recorded_at_raw:
            raise Refusal(f"{path.name} has no `recorded_at` — cannot place it on a timeline")
        recorded_at = parse_iso(recorded_at_raw)

        lines = transcript_section(text)
        if not lines:
            continue
        stats["documents"] += 1

        saw_stamp = False
        pending: list[tuple[dt.datetime, str, str]] = []
        for line in lines:
            m = TURN.match(line.strip())
            if not m:
                continue
            stamp = m.group("stamp")
            body = m.group("text").strip()
            if not body:
                continue
            if stamp is None:
                # Unstamped turn. Harmless if the whole document is inside the
                # session; otherwise there is no way to know which side of the
                # boundary it falls on.
                at = recorded_at
            else:
                saw_stamp = True
                at = recorded_at + dt.timedelta(seconds=stamp_seconds(stamp))
            pending.append((at, m.group("speaker").strip(), body))

        if not saw_stamp and recorded_at < session_start:
            raise Refusal(
                f"{path.name} starts at {recorded_at:%H:%M} — before the session began at "
                f"{session_start:%H:%M} — and its turns carry no timestamps, so the part "
                f"before the session cannot be identified and removed."
            )

        for at, speaker, body in pending:
            if at < session_start:
                stats["dropped_before_session"] += 1
                continue
            if session_end and at > session_end:
                stats["dropped_after_session"] += 1
                continue
            turns.append((at, speaker, body))
            stats["kept"] += 1

    turns.sort(key=lambda t: t[0])
    return turns, stats


def render(turns, session_start, session_end, stats, slug) -> str:
    head = [
        f"# Session transcript — {slug}",
        "",
        f"Recorded by Transcripts. Session ran from {session_start:%Y-%m-%d %H:%M} UTC"
        + (f" to {session_end:%H:%M} UTC." if session_end else "."),
        "",
        f"{stats['kept']} turns from {stats['documents']} recording(s).",
    ]
    if stats["dropped_before_session"]:
        head.append(
            f"{stats['dropped_before_session']} turns recorded before the session started were "
            "left out — the recorder was running before the game began."
        )
    if stats["dropped_after_session"]:
        head.append(f"{stats['dropped_after_session']} turns after the session ended were left out.")
    head += ["", "---", ""]

    body = [f"**{speaker}:** {text}" for _, speaker, text in turns]
    return "\n".join(head) + "\n" + "\n\n".join(body) + "\n"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO, text=True, capture_output=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True, help="Archive folder name, e.g. 2026-08-31-1927")
    ap.add_argument("--started-at", required=True)
    ap.add_argument("--ended-at")
    ap.add_argument("--transcripts", required=True,
                    help="Newline-separated transcript paths (Transcripts' ${transcripts})")
    ap.add_argument("--branch")
    ap.add_argument("--dry-run", action="store_true", help="Write nothing; print what would happen")
    ap.add_argument("--no-pr", action="store_true", help="Write and commit locally, open no PR")
    args = ap.parse_args()

    session_start = parse_iso(args.started_at)
    session_end = parse_iso(args.ended_at) if args.ended_at else None
    paths = [Path(p.strip()) for p in args.transcripts.splitlines() if p.strip()]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"✗ missing transcript(s): {', '.join(p.name for p in missing)}", file=sys.stderr)
        return 1
    if not paths:
        print("✗ no transcripts given", file=sys.stderr)
        return 1

    try:
        turns, stats = collect_turns(paths, session_start, session_end)
    except Refusal as e:
        print(f"✗ refusing to import: {e}", file=sys.stderr)
        print("  Nothing was written. Trim the transcript by hand if you want this session "
              "published.", file=sys.stderr)
        return 2

    if not turns:
        print("✗ no turns fall inside the session window — nothing to publish", file=sys.stderr)
        return 3

    out_dir = REPO / "data" / "sessions" / args.slug
    doc = render(turns, session_start, session_end, stats, args.slug)

    print(f"  {stats['kept']} turns kept, {stats['dropped_before_session']} dropped from before "
          f"the session, {stats['dropped_after_session']} from after")
    print(f"  speakers: {', '.join(sorted({s for _, s, _ in turns}))}")
    if args.dry_run:
        print(f"  would write {out_dir / 'transcript.md'} ({len(doc)} bytes)")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "transcript.md").write_text(doc, encoding="utf-8")
    print(f"✓ wrote {out_dir / 'transcript.md'}")

    # The journal generator skips any session page whose frontmatter says
    # `generated: false` — it reads that as "a person wrote this, leave it
    # alone". A scheduling stub carries the same flag, so it silently blocks its
    # own session from ever being written. Clear it, but only when the stub has
    # no prose worth protecting.
    unblocked = unblock_stub(session_start.strftime("%Y-%m-%d"))
    if unblocked:
        print(f"✓ cleared the generated:false flag on {unblocked.name} so the journal can write it")

    if args.no_pr:
        return 0
    return open_pr(args.slug, args.branch or f"session/{args.slug}", stats)


def unblock_stub(date_str: str) -> Path | None:
    """Let the generator write a date's page, if only a stub stands there.

    Deliberately conservative: a page with real prose is left exactly as it is,
    because `generated: false` is also how a hand-written entry protects itself
    and overwriting one would be the worst thing this script could do.
    """
    sessions = REPO / "content" / "sessions"
    for path in sorted(sessions.glob(f"{date_str}*.md")):
        text = path.read_text(encoding="utf-8")
        end = text.find("\n---\n", 4)
        if end == -1:
            continue
        body = text[end + 5:].strip()
        # A stub is scheduling notes, not an account of a session. Anything
        # substantial is somebody's writing.
        if len(body) > 900:
            print(f"  leaving {path.name} alone — it already has prose ({len(body)} chars)")
            continue
        if "generated: false" not in text:
            continue
        path.write_text(text.replace("generated: false", "generated: true", 1), encoding="utf-8")
        return path
    return None


def open_pr(slug: str, branch: str, stats: dict) -> int:
    run(["git", "checkout", "-B", branch])
    run(["git", "add", "data/sessions", "content/sessions"])
    status = run(["git", "status", "--porcelain"])
    if not status.stdout.strip():
        print("  nothing changed — no pull request opened")
        return 0

    message = (
        f"Add session archive {slug}\n\n"
        f"Imported from Transcripts: {stats['kept']} turns across {stats['documents']} recording(s).\n"
        f"{stats['dropped_before_session']} turns recorded before the session started were excluded."
    )
    commit = run(["git", "commit", "-m", message])
    if commit.returncode != 0:
        print(f"✗ commit failed: {commit.stderr}", file=sys.stderr)
        return 1

    push = run(["git", "push", "-u", "origin", branch])
    if push.returncode != 0:
        print(f"✗ push failed: {push.stderr}", file=sys.stderr)
        return 1

    pr = run([
        "gh", "pr", "create",
        "--title", f"Session archive {slug}",
        "--body",
        "Imported from Transcripts by `scripts/import_transcripts_session.py`.\n\n"
        f"- {stats['kept']} turns across {stats['documents']} recording(s)\n"
        f"- {stats['dropped_before_session']} turns from before the session start were excluded\n\n"
        "Merging this to `main` triggers the campaign-journal workflow, which generates the "
        "narrative entry and deploys to Pages. **Read the transcript before merging** — this "
        "repository is public.",
    ])
    if pr.returncode != 0:
        print(f"✗ gh pr create failed: {pr.stderr}", file=sys.stderr)
        return 1
    print(f"✓ {pr.stdout.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
