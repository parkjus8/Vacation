# Daily hotel report: SoFi Stadium, March 2027

Runs daily on GitHub Actions. It pulls Google Hotels prices via SerpApi, compares them with the
previous day, saves a Markdown report to `reports/`, and posts a digest to Discord.

## Setup
1. Get a SerpApi key (serpapi.com, free tier is 250 searches/month).
2. In Discord: channel settings > Integrations > Webhooks > New Webhook > Copy Webhook URL.
3. In this repo: Settings > Secrets and variables > Actions > New repository secret. Add:
   - `SERPAPI_KEY`: your SerpApi key
   - `DISCORD_WEBHOOK_URL`: the webhook URL (treat it like a password; never commit it)
4. Actions tab > "Daily hotel report" > Run workflow to test.

Each run uses (date windows x max_pages) searches. The defaults use 2 a day, about 60 a month.

## What Discord gets
1. A summary: hotels added to or dropped from the list since yesterday (with the reason), and
   price drops on hotels you have already seen in an earlier post.
2. The top hotels (8 by default): best value by rating per dollar, always including the cheapest.
   Each shows nightly price, total, breakfast, stars, rating, distance to SoFi, amenities and
   which booking sites list it, with price drops highlighted in green.

## Files
- `config.yaml`: all settings (edit by hand, or use the settings dashboard).
- `fetch_hotels.py`: fetch, compare, write report and Discord payload.
- `send_discord.py`: posts `reports/discord.json` to the webhook.
- `data/snapshots/`: one JSON per day. `data/state.json`: what has been shown on Discord.

## Local test (no API key needed)
    pip install -r requirements.txt
    python fetch_hotels.py --mock props.json --date 2027-01-01   # props.json = a JSON list of SerpApi "properties"
