#!/usr/bin/env python3
"""
VCTA Dashboard Sync
-------------------
Reads your team data from vcta.dk and uploads a snapshot to JSONBin.

Setup (one time):
  pip install requests

  1. Log in to vcta.dk in Chrome
  2. Open DevTools (F12) -> Application -> Cookies -> www.vcta.dk
  3. Copy the value of '.AspNetCore.Identity.Application'
  4. Create a file called cookies.txt next to this script and paste the value in

Running:
  python sync.py

Schedule (Windows Task Scheduler):
  Action: python C:/Dev/vcta/sync.py
  Trigger: every 30 minutes

The cookie typically lasts several weeks. If you get an auth error, repeat steps 2-4.
"""

import re
import sys
from datetime import datetime
from pathlib import Path
import requests

# -- Config --------------------------------------------------------------------
JSONBIN_MASTER_KEY = "$2a$10$T5Gr8moeChNgWFEpzpkbV.xklP2b25TzPXX2GFSIiSEZOZQGPr/62"
JSONBIN_BIN_ID     = "69fb04bbaaba882197795fd0"
COOKIES_FILE       = Path(__file__).parent / "cookies.txt"
# ------------------------------------------------------------------------------

VCTA_BASE = "https://www.vcta.dk"
VCTA_API  = f"{VCTA_BASE}/umbraco/surface/vctasurface/ToAPI?action=profile"


def load_identity_cookie():
    # When running in GitHub Actions the cookie is passed as an env variable
    import os
    env_value = os.environ.get("VCTA_COOKIE", "").strip()
    if env_value:
        return env_value

    # Locally, read from cookies.txt
    if not COOKIES_FILE.exists():
        sys.exit(
            "cookies.txt not found.\n"
            f"Create {COOKIES_FILE.resolve()} and paste your "
            ".AspNetCore.Identity.Application cookie value into it.\n"
            "Get it from: Chrome DevTools (F12) -> Application -> Cookies -> www.vcta.dk"
        )
    value = COOKIES_FILE.read_text(encoding="utf-8").strip()
    if not value:
        sys.exit("cookies.txt is empty — paste your .AspNetCore.Identity.Application value into it.")
    return value


def get_csrf_token(session):
    r = session.get(f"{VCTA_BASE}/Min-side", timeout=15)
    r.raise_for_status()

    # ASP.NET Core embeds the token in a hidden input or meta tag
    patterns = [
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
        r'value="([^"]+)"[^>]*name="__RequestVerificationToken"',
        r'<meta\s+name="csrf-token"\s+content="([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, r.text)
        if m:
            return m.group(1)

    return None


def fetch_vcta():
    identity = load_identity_cookie()

    session = requests.Session()
    session.cookies.set(
        ".AspNetCore.Identity.Application", identity,
        domain="www.vcta.dk", path="/"
    )

    print("  Getting CSRF token…")
    csrf = get_csrf_token(session)
    if not csrf:
        print("  Warning: could not find CSRF token — trying without it")

    r = session.post(
        VCTA_API,
        data="{}",
        headers={
            "content-type": "text/plain;charset=UTF-8",
            "x-csrf-token":  csrf or "",
            "Referer":       f"{VCTA_BASE}/Min-side",
        },
        timeout=15,
    )

    if r.status_code in (401, 403):
        sys.exit(
            "Authentication failed — your cookie has probably expired.\n"
            "Get a fresh .AspNetCore.Identity.Application value from Chrome DevTools "
            "and update cookies.txt."
        )
    r.raise_for_status()

    data = r.json()
    if not data.get("ownedTeams"):
        sys.exit("No owned teams found — are you a team captain on vcta.dk?")

    return data


def merge_owned_teams(owned_teams):
    """Merge all owned teams into a single member list and totals.
    If the same person (by email) appears in multiple teams their
    stats are summed and trophies are de-duplicated."""
    by_email = {}
    for team in owned_teams:
        for m in team["ps"]:
            email = m["e"]
            if email not in by_email:
                by_email[email] = dict(m)  # first occurrence wins, ignore duplicates

    members = list(by_email.values())
    return {
        "name":      owned_teams[0]["c"],   # use company name as the unified team name
        "company":   owned_teams[0]["c"],
        "seats":     sum(t["s"] for t in owned_teams),
        "totalDays": sum(m["d"]  for m in members),
        "totalKm":   round(sum(m["k"]  for m in members), 1),
        "playDays":  sum(m["pd"] for m in members),
        "playKm":    round(sum(m["pk"] for m in members), 1),
    }, members


def build_payload(raw):
    owned_teams = raw["ownedTeams"]
    campaign    = raw["campaigns"][0]

    team, all_members = merge_owned_teams(owned_teams)
    members = sorted(all_members, key=lambda m: (-m["d"], -m["k"]))

    return {
        "updated": datetime.now().isoformat(),
        "campaign": {
            "name":           campaign["name"],
            "startDataEntry": campaign["startDataEntry"],
            "endDataEntry":   campaign["endDataEntry"],
        },
        "team": team,
        "members": [
            {
                "name":      m["n"],
                "days":      m["d"],
                "km":        round(m["k"], 1),
                "playDays":  m["pd"],
                "playKm":    round(m["pk"], 1),
                "trophies":  m["tr"],
                "isCaptain": m.get("l", 0) == 1,
            }
            for m in members
        ],
    }


def push(payload):
    for attempt in range(1, 4):
        try:
            r = requests.put(
                f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}",
                json=payload,
                headers={
                    "Content-Type":     "application/json",
                    "X-Master-Key":     JSONBIN_MASTER_KEY,
                    "X-Bin-Versioning": "false",
                },
                timeout=45,
            )
            r.raise_for_status()
            return
        except requests.exceptions.Timeout:
            if attempt == 3:
                sys.exit("JSONBin timed out after 3 attempts — try again later.")
            print(f"  JSONBin timeout (attempt {attempt}/3), retrying…")


if __name__ == "__main__":
    print("Fetching VCTA data…")
    raw     = fetch_vcta()
    payload = build_payload(raw)

    t = payload["team"]
    print(f"  Team   : {t['name']} ({t['company']})")
    print(f"  Lodder : {t['totalDays']}  |  km: {t['totalKm']}")
    print(f"  Members: {len(payload['members'])}")

    print("Uploading to JSONBin…")
    push(payload)
    print(f"Done — updated at {payload['updated']}")
