#!/usr/bin/env python3
"""
ddb_discover.py — Discover DnD Beyond character IDs for the campaign.

Reads DDB_COOKIE from .env, authenticates, then fetches the campaign's
active character list and writes character IDs to .env as DDB_CHARACTER_IDS.

Run this once after ddb_auth.py, or any time the party roster changes.

Usage:
    python scripts/ddb_discover.py

Requirements:
    pip install httpx python-dotenv
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx python-dotenv")
    sys.exit(1)

# Load .env
ENV_FILE = Path(__file__).parent.parent / ".env"


def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _update_env(key: str, value: str) -> None:
    if ENV_FILE.exists():
        content = ENV_FILE.read_text()
    else:
        content = ""
    pattern = rf"^{re.escape(key)}=.*$"
    new_line = f"{key}={value}"
    if re.search(pattern, content, flags=re.MULTILINE):
        content = re.sub(pattern, new_line, content, flags=re.MULTILINE)
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        content += new_line + "\n"
    ENV_FILE.write_text(content)
    ENV_FILE.chmod(0o600)


async def get_cobalt_token(cookie: str) -> str:
    """Exchange CobaltSession cookie for a short-lived bearer token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://auth-service.dndbeyond.com/v1/cobalt-token",
            headers={"Cookie": f"CobaltSession={cookie}"},
        )
        resp.raise_for_status()
        return resp.json()["token"]


async def get_campaign_characters(token: str, campaign_id: str) -> list[dict]:
    """Fetch the list of active characters in the campaign."""
    url = f"https://api.dndbeyond.com/campaign/stt/active-short-characters/{campaign_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # Response shape: {"data": [...]} or list directly
        if isinstance(data, dict):
            return data.get("data", [])
        return data


async def main() -> None:
    _load_env()

    campaign_id = os.environ.get("DDB_CAMPAIGN_ID", "6805334")
    cookie = os.environ.get("DDB_COOKIE", "")

    if not cookie:
        print("ERROR: DDB_COOKIE not set. Run ddb_auth.py first.")
        sys.exit(1)

    print(f"Discovering characters for campaign {campaign_id}...")

    try:
        token = await get_cobalt_token(cookie)
        print("  ✓ Got cobalt token")
    except httpx.HTTPStatusError as e:
        print(f"ERROR: Auth failed ({e.response.status_code}). Run ddb_auth.py to refresh cookie.")
        sys.exit(1)

    try:
        characters = await get_campaign_characters(token, campaign_id)
    except httpx.HTTPStatusError as e:
        print(f"ERROR: Campaign fetch failed ({e.response.status_code}): {e.response.text[:200]}")
        sys.exit(1)

    if not characters:
        print("WARNING: No characters returned. Check campaign ID or auth.")
        sys.exit(1)

    print(f"\nFound {len(characters)} character(s):")
    ids = []
    for char in characters:
        char_id = str(char.get("id") or char.get("characterId") or "")
        name = char.get("name") or char.get("characterName") or "Unknown"
        print(f"  {name:30s}  id={char_id}")
        if char_id:
            ids.append(char_id)

    if not ids:
        print("ERROR: Could not extract character IDs from response.")
        print("Raw response sample:", json.dumps(characters[:1], indent=2))
        sys.exit(1)

    id_string = ",".join(ids)
    _update_env("DDB_CHARACTER_IDS", id_string)
    print(f"\n✓ DDB_CHARACTER_IDS={id_string}")
    print(f"  Written to {ENV_FILE}")
    print("\nYou can now run ddb_poll.py to start syncing.")


if __name__ == "__main__":
    asyncio.run(main())
