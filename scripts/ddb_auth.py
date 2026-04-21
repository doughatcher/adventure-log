#!/usr/bin/env python3
"""
ddb_auth.py — DnD Beyond authentication helper.

Opens a visible Chromium browser so you can log in to DnD Beyond manually.
After login, extracts the CobaltSession cookie and writes it to .env.

Usage:
    python scripts/ddb_auth.py

Requirements:
    pip install playwright
    playwright install chromium
"""
import asyncio
import os
import re
import sys
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

ENV_FILE = Path(__file__).parent.parent / ".env"
DDB_URL = "https://www.dndbeyond.com/login"


def _update_env(key: str, value: str) -> None:
    """Write or update a KEY=VALUE line in .env."""
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
    print(f"  ✓ {key} written to {ENV_FILE}")


async def main() -> None:
    print("DnD Beyond Auth Helper")
    print("=" * 40)
    print("A browser will open. Log in to DnD Beyond,")
    print("then return here and press Enter.")
    print()

    async with async_playwright() as p:
        # Visible browser — user must log in manually
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(DDB_URL)
        print(f"Browser opened: {DDB_URL}")
        print()
        input(">>> Log in to DnD Beyond in the browser, then press Enter here...")

        # Give it a moment for any post-login redirects/cookie writes
        await page.wait_for_timeout(2000)

        cookies = await context.cookies()
        cobalt = next((c for c in cookies if c["name"] == "CobaltSession"), None)

        await browser.close()

    if not cobalt:
        print()
        print("ERROR: CobaltSession cookie not found.")
        print("Make sure you're fully logged in to DnD Beyond before pressing Enter.")
        sys.exit(1)

    print()
    print(f"Found CobaltSession cookie (expires: {cobalt.get('expires', 'session')})")
    _update_env("DDB_COOKIE", cobalt["value"])
    print()
    print("Done. You can now run ddb_poll.py to start syncing character data.")


if __name__ == "__main__":
    asyncio.run(main())
