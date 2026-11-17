#!/usr/bin/env python3
"""Daily hotel report: fetch prices (SerpApi Google Hotels), compare to the
previous snapshot, and write a Markdown report."""

import argparse
import json
import math
import os
import sys
from datetime import date
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
SNAP_DIR = ROOT / "data" / "snapshots"
REPORT_DIR = ROOT / "reports"
SERP_URL = "https://serpapi.com/search.json"


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_window(cfg, window, api_key):
    """Return raw 'properties' for one stay window, following pagination."""
    s = cfg["search"]
    params = {
        "engine": "google_hotels",
        "q": s["location_query"],
        "check_in_date": window["check_in"],
        "check_out_date": window["check_out"],
        "adults": s["adults"],
        "currency": s["currency"],
        "hotel_class": ",".join(str(c) for c in s["hotel_class"]),
        "sort_by": 3,  # lowest price
        "api_key": api_key,
    }
    props = []
    for _ in range(s.get("max_pages", 1)):
        resp = requests.get(SERP_URL, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"SerpApi error: {data['error']}")
        props.extend(data.get("properties", []))
        token = data.get("serpapi_pagination", {}).get("next_page_token")
        if not token:
            break
        params["next_page_token"] = token
    return props


def normalize(cfg, window, props):
    a = cfg["anchor"]
    nights = (date.fromisoformat(window["check_out"]) - date.fromisoformat(window["check_in"])).days
    out = []
    for p in props:
        gps = p.get("gps_coordinates") or {}
        nightly = (p.get("rate_per_night") or {}).get("extracted_lowest")
        total = (p.get("total_rate") or {}).get("extracted_lowest")
        if nightly is None and total:
            nightly = round(total / nights)
        if total is None and nightly:
            total = nightly * nights
        if nightly is None:
            continue  # no price available for these dates
        dist = None
        if gps.get("latitude") is not None:
            dist = haversine_km(a["lat"], a["lon"], gps["latitude"], gps["longitude"])
            if dist > a["max_distance_km"]:
                continue
        out.append({
            "window": window["label"],
            "name": p.get("name"),
            "stars": p.get("extracted_hotel_class"),
            "rating": p.get("overall_rating"),
            "reviews": p.get("reviews"),
            "nightly": nightly,
            "total": total,
            "distance_km": round(dist, 1) if dist is not None else None,
            "link": p.get("link"),
        })
    return out


def key(h):
    return f"{h['window']}|{h['name']}"


def load_previous(today_str):
    snaps = sorted(f for f in SNAP_DIR.glob("*.json") if f.stem < today_str)
    if not snaps:
        return {}
    data = json.loads(snaps[-1].read_text())
    return {key(h): h for h in data["hotels"]}


def value_score(h):
    if not h.get("rating") or not h["nightly"]:
        return 0
    return h["rating"] / (h["nightly"] / 100)


def money(x):
    return f"${x:,.0f}" if x is not None else "n/a"


def delta_str(h, prev):
    p = prev.get(key(h))
    if not p:
        return "new"
    d = h["nightly"] - p["nightly"]
    if d == 0:
        return "-"
    return f"{'▼' if d < 0 else '▲'} {money(abs(d))}"


def render(cfg, hotels, prev, today_str):
    top_n = cfg["report"]["top_n"]
    min_rating = cfg["report"]["min_rating"]
    lines = [f"# Hotels near SoFi Stadium: {today_str}", ""]
    lines.append(
        f"{cfg['search']['adults']} guests, {'/'.join(map(str, cfg['search']['hotel_class']))}-star, "
        f"within {cfg['anchor']['max_distance_km']} km of {cfg['anchor']['name']}. "
        "Source: Google Hotels via SerpApi. Prices are per night in "
        f"{cfg['search']['currency']}; change is vs. the previous run.\n"
    )
    for w in cfg["date_windows"]:
        hs = [h for h in hotels if h["window"] == w["label"]]
        lines.append(f"## {w['label']} ({w['check_in']} to {w['check_out']})")
        if not hs:
            lines += ["", "No priced results returned.", ""]
            continue
        cheapest = sorted(hs, key=lambda h: h["nightly"])[:top_n]
        ranked = [h for h in hs if (h["rating"] or 0) >= min_rating]
        best_value = sorted(ranked, key=value_score, reverse=True)[:top_n]

        for title, rows in (("Cheapest", cheapest), ("Best value (rating per dollar)", best_value)):
            lines += ["", f"### {title}", "",
                      "| Hotel | Stars | Rating | Km to SoFi | Nightly | Total | Change |",
                      "|---|---|---|---|---|---|---|"]
            for h in rows:
                name = f"[{h['name']}]({h['link']})" if h.get("link") else h["name"]
                lines.append(
                    f"| {name} | {h['stars'] or '?'} | {h['rating'] or '?'} ({h['reviews'] or 0}) | "
                    f"{h['distance_km'] if h['distance_km'] is not None else '?'} | "
                    f"{money(h['nightly'])} | {money(h['total'])} | {delta_str(h, prev)} |"
                )
        drops = [h for h in hs if key(h) in prev and h["nightly"] < prev[key(h)]["nightly"]]
        if drops:
            lines += ["", "### Price drops since last run", ""]
            for h in sorted(drops, key=lambda h: h["nightly"] - prev[key(h)]["nightly"]):
                lines.append(f"- {h['name']}: {money(prev[key(h)]['nightly'])} to {money(h['nightly'])} per night")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--mock", help="Path to a JSON file of SerpApi 'properties' (offline test)")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    today_str = date.today().isoformat()
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key and not args.mock:
        sys.exit("SERPAPI_KEY is not set")

    hotels = []
    for w in cfg["date_windows"]:
        props = json.loads(Path(args.mock).read_text()) if args.mock else fetch_window(cfg, w, api_key)
        hotels.extend(normalize(cfg, w, props))

    prev = load_previous(today_str)
    report = render(cfg, hotels, prev, today_str)

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (SNAP_DIR / f"{today_str}.json").write_text(json.dumps({"date": today_str, "hotels": hotels}, indent=1))
    (REPORT_DIR / f"{today_str}.md").write_text(report)
    (REPORT_DIR / "latest.md").write_text(report)
    print(report)


if __name__ == "__main__":
    main()
