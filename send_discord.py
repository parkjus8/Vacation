#!/usr/bin/env python3
"""Post reports/discord.json to a Discord webhook (URL comes from DISCORD_WEBHOOK_URL)."""

import json
import os
import sys
import time
from pathlib import Path

import requests

PAYLOAD = Path(__file__).parent / "reports" / "discord.json"


def post(url, body):
    for attempt in range(2):
        r = requests.post(url, json=body, timeout=30)
        if r.status_code == 429 and attempt == 0:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        return r
    return r


def main():
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("DISCORD_WEBHOOK_URL not set; skipping Discord.")
        return
    messages = json.loads(PAYLOAD.read_text())
    for i, msg in enumerate(messages, 1):
        if not msg["embeds"]:
            continue
        r = post(url, {"username": "Hotel Watch", **msg})
        if r.status_code >= 300:
            # Never print the URL: it is a secret.
            sys.exit(f"Discord rejected message {i}: HTTP {r.status_code} {r.text[:300]}")
        time.sleep(1.2)  # stay well under webhook rate limits
    print(f"Sent {len(messages)} Discord messages.")


if __name__ == "__main__":
    main()
