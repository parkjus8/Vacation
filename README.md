# Daily hotel report: SoFi Stadium, March 2027

Runs daily on GitHub Actions, pulls Google Hotels prices via SerpApi, compares with
the previous day, and commits a Markdown report to `reports/`.

## Setup
1. Create a GitHub repo and push these files.
2. Get a SerpApi key (serpapi.com, free tier is 250 searches/month).
3. Repo Settings > Secrets and variables > Actions > New secret: `SERPAPI_KEY`.
4. Actions tab > "Daily hotel report" > Run workflow to test.

Each run uses (date windows x max_pages) searches. The defaults use 2 a day, about 60 a month.

## Customize
Edit `config.yaml`: dates, guests, star filter, distance, ranking. Add more stay windows
to compare alternatives.

## Local test
    pip install -r requirements.txt
    SERPAPI_KEY=xxx python fetch_hotels.py
