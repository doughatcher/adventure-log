#!/usr/bin/env python3
"""
ddb_poll.py — Poll DnD Beyond for live character stats and push to dnd-stage server.

Reads credentials from .env, fetches each character's HP/AC/conditions from the
DnD Beyond character API, then PATCHes the local dnd-stage server.

Runs as a loop (default: every 60s). Run with --once for a single pass.

Usage:
    python scripts/ddb_poll.py          # continuous loop
    python scripts/ddb_poll.py --once   # single pass and exit

Requirements:
    pip install httpx python-dotenv
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────

ENV_FILE = Path(__file__).parent.parent / ".env"
POLL_INTERVAL = int(os.environ.get("DDB_POLL_INTERVAL", "60"))  # seconds

# DnD Beyond API endpoints
AUTH_URL = "https://auth-service.dndbeyond.com/v1/cobalt-token"
CHAR_API_URL = "https://character-service.dndbeyond.com/character/v5/character/{char_id}"

# DnD Beyond character ID → dnd-stage slug mapping
# Populated from DDB_CHARACTER_IDS env var + name-to-slug logic
SLUG_OVERRIDES: dict[str, str] = {
    # If auto-slugging doesn't match, add: "character_id": "slug-in-dnd-stage"
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _name_to_slug(name: str) -> str:
    """Convert a character name to a dnd-stage URL slug."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    return slug


def _calc_hp(char_data: dict) -> tuple[int, int]:
    """Return (current_hp, max_hp) from DnD Beyond character JSON."""
    base = char_data.get("baseHitPoints", 0)
    bonus = char_data.get("bonusHitPoints") or 0
    override = char_data.get("overrideHitPoints")
    removed = char_data.get("removedHitPoints") or 0
    temp = char_data.get("temporaryHitPoints") or 0

    max_hp = override if override is not None else (base + bonus)
    current_hp = max(0, max_hp - removed) + temp
    return current_hp, max_hp


def _get_ac(char_data: dict) -> int | None:
    """Extract AC from character data. Returns None if not determinable."""
    # Try to find AC in stats — this is approximate without full rule resolution
    # The character-service API doesn't pre-calculate AC, but inventory items may help
    # Best we can do: look for equipped armor item bonuses
    # Return None to skip AC update if not available
    return None


def _get_conditions(char_data: dict) -> list[str]:
    """Return list of active condition names."""
    condition_ids = char_data.get("conditions") or []
    # DnD Beyond condition ID → name map (SRD conditions)
    CONDITION_NAMES = {
        1: "Blinded", 2: "Charmed", 3: "Deafened", 4: "Exhaustion",
        5: "Frightened", 6: "Grappled", 7: "Incapacitated", 8: "Invisible",
        9: "Paralyzed", 10: "Petrified", 11: "Poisoned", 12: "Prone",
        13: "Restrained", 14: "Stunned", 15: "Unconscious",
    }
    return [CONDITION_NAMES.get(c, f"condition_{c}") for c in condition_ids]


# ── DnD Beyond API ───────────────────────────────────────────────────────────

async def get_cobalt_token(client: httpx.AsyncClient, cookie: str) -> str:
    resp = await client.post(
        AUTH_URL,
        headers={"Cookie": f"CobaltSession={cookie}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["token"]


async def fetch_character(client: httpx.AsyncClient, char_id: str, token: str) -> dict | None:
    url = CHAR_API_URL.format(char_id=char_id)
    try:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response shape: {"data": {...}} or the dict directly
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data
    except httpx.HTTPStatusError as e:
        print(f"  WARNING: fetch_character({char_id}) failed {e.response.status_code}")
        return None
    except Exception as e:
        print(f"  WARNING: fetch_character({char_id}) error: {e}")
        return None


# ── dnd-stage API ─────────────────────────────────────────────────────────────

async def patch_character(client: httpx.AsyncClient, server_url: str, slug: str, payload: dict) -> bool:
    url = f"{server_url}/api/characters/{slug}"
    try:
        resp = await client.patch(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        print(f"  WARNING: patch {slug} failed {e.response.status_code}: {e.response.text[:100]}")
        return False
    except Exception as e:
        print(f"  WARNING: patch {slug} error: {e}")
        return False


# ── Poll loop ─────────────────────────────────────────────────────────────────

async def poll_once(server_url: str, cookie: str, char_ids: list[str]) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] Polling DnD Beyond for {len(char_ids)} character(s)...")

    async with httpx.AsyncClient() as client:
        # Refresh cobalt token each poll (tokens are short-lived)
        try:
            token = await get_cobalt_token(client, cookie)
        except httpx.HTTPStatusError as e:
            print(f"  ERROR: Auth failed ({e.response.status_code}). Run ddb_auth.py to refresh cookie.")
            return
        except Exception as e:
            print(f"  ERROR: Auth error: {e}")
            return

        tasks = [fetch_character(client, cid, token) for cid in char_ids]
        results = await asyncio.gather(*tasks)

        for char_id, char_data in zip(char_ids, results):
            if char_data is None:
                continue

            name: str = char_data.get("name") or char_data.get("characterName") or f"char_{char_id}"
            slug = SLUG_OVERRIDES.get(char_id) or _name_to_slug(name)

            current_hp, max_hp = _calc_hp(char_data)
            conditions = _get_conditions(char_data)
            ac = _get_ac(char_data)

            payload: dict = {
                "hp_current": current_hp,
                "hp_max": max_hp,
            }
            if conditions:
                payload["conditions"] = conditions
            if ac is not None:
                payload["ac"] = ac

            ok = await patch_character(client, server_url, slug, payload)
            status = "✓" if ok else "✗"
            cond_str = f"  [{', '.join(conditions)}]" if conditions else ""
            print(f"  {status} {name:30s} HP={current_hp}/{max_hp}{cond_str}")


async def main(once: bool = False) -> None:
    _load_env()

    cookie = os.environ.get("DDB_COOKIE", "")
    char_id_str = os.environ.get("DDB_CHARACTER_IDS", "")
    server_url = os.environ.get("DND_STAGE_URL", "http://localhost:3200")

    if not cookie:
        print("ERROR: DDB_COOKIE not set. Run scripts/ddb_auth.py first.")
        sys.exit(1)

    if not char_id_str:
        print("ERROR: DDB_CHARACTER_IDS not set. Run scripts/ddb_discover.py first.")
        sys.exit(1)

    char_ids = [cid.strip() for cid in char_id_str.split(",") if cid.strip()]
    print(f"dnd-stage DnD Beyond Poller")
    print(f"  Server:     {server_url}")
    print(f"  Characters: {len(char_ids)} ({char_id_str})")
    print(f"  Interval:   {POLL_INTERVAL}s")
    print()

    if once:
        await poll_once(server_url, cookie, char_ids)
        return

    while True:
        try:
            await poll_once(server_url, cookie, char_ids)
        except Exception as e:
            print(f"  ERROR: Unexpected error in poll: {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll DnD Beyond for character stats")
    parser.add_argument("--once", action="store_true", help="Run a single poll and exit")
    args = parser.parse_args()
    asyncio.run(main(once=args.once))
