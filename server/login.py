"""One-time Schwab OAuth login: `python -m server.login`.

Schwab requires an https callback, so rather than standing up a TLS listener this
opens the consent page, lets the browser fail to load the redirect, and asks you to
paste the resulting address-bar URL back in. Re-run it once a week -- Schwab expires
refresh tokens after 7 days no matter what.
"""

from __future__ import annotations

import asyncio
import sys
import webbrowser
from datetime import datetime

from .config import SETTINGS
from .schwab import SchwabAuthError, SchwabClient, authorization_url, extract_code


async def run() -> int:
    if not SETTINGS.configured:
        print("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set.")
        print("Copy .env.example to .env and fill in your app credentials.")
        return 1

    url = authorization_url()
    print("\n1. Opening the Schwab consent page in your browser:\n")
    print(f"   {url}\n")
    print("   (If it doesn't open, copy the URL above.)")
    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover - headless environments
        pass

    print("\n2. Log in and approve access. Your browser will then try to load")
    print(f"   {SETTINGS.redirect_uri}... and show an error page. That is expected.")
    print("\n3. Copy the FULL URL out of the address bar and paste it here.\n")

    redirected = input("Redirected URL: ").strip()
    if not redirected:
        print("Nothing pasted; aborting.")
        return 1

    client = SchwabClient()
    try:
        code = extract_code(redirected)
        await client.exchange_code(code)
    except SchwabAuthError as exc:
        print(f"\nLogin failed: {exc}")
        return 1
    finally:
        await client.aclose()

    expires = client.tokens.status()["refresh_expires_at"]
    when = datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M")
    print(f"\nSaved tokens to {SETTINGS.token_path}")
    print(f"Refresh token valid until {when} -- re-run this command before then.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
