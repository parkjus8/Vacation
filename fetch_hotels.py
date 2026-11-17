#!/usr/bin/env python3
"""Daily hotel report.

Fetches Google Hotels prices through SerpApi, compares with the previous run,
and writes:
  - reports/<date>.md and reports/latest.md   (full Markdown tables)
  - reports/discord.json                      (messages for send_discord.py)
  - data/snapshots/<date>.json                (everything seen today)
  - data/state.json                           (what has already been shown on Discord)
"""

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).parent
SNAP_DIR = ROOT / "data" / "snapshots"
STATE_FILE = ROOT / "data" / "state.json"
REPORT_DIR = ROOT / "reports"
SERP_URL = "https://serpapi.com/search.json"

TEAL, GREEN = 0x0A6C74, 0x1B7A43


# ---------------------------------------------------------------- fetching

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
    if s.get("breakfast_included"):
        params["amenities"] = "9"  # Google Hotels amenity ID 9 = Free breakfast
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


def _num(d, field):
    v = (d or {}).get(field)
    return v if isinstance(v, (int, float)) else None


def normalize(cfg, window, props):
    """Return (kept, excluded). Excluded hotels carry the reason they were dropped."""
    a = cfg["anchor"]
    f = cfg.get("filters") or {}
    nights = (date.fromisoformat(window["check_out"]) - date.fromisoformat(window["check_in"])).days
    kept, excluded = {}, {}
    for p in props:
        name = p.get("name")
        if not name:
            continue
        rpn, tot = p.get("rate_per_night"), p.get("total_rate")
        nightly = _num(rpn, "extracted_lowest")
        total = _num(tot, "extracted_lowest")
        if nightly is None and total:
            nightly = round(total / nights)
        if total is None and nightly:
            total = nightly * nights
        if nightly is None:
            continue  # no price for these dates
        total_after = _num(tot, "extracted_after_taxes_fees")

        gps = p.get("gps_coordinates") or {}
        dist = None
        if gps.get("latitude") is not None:
            dist = haversine_km(a["lat"], a["lon"], gps["latitude"], gps["longitude"])

        sources = []
        for pr in p.get("prices") or []:
            n = _num(pr.get("rate_per_night"), "extracted_lowest")
            if pr.get("source") and n is not None:
                sources.append({"source": pr["source"], "nightly": n})
        sources.sort(key=lambda x: x["nightly"])

        h = {
            "window": window["label"],
            "name": name,
            "stars": p.get("extracted_hotel_class"),
            "rating": p.get("overall_rating"),
            "reviews": p.get("reviews"),
            "breakfast": any("breakfast" in str(x).lower() for x in (p.get("amenities") or [])),
            "amenities": [str(x) for x in (p.get("amenities") or [])],
            "nightly": nightly,
            "total": total,
            "total_after_taxes": total_after,
            "nights": nights,
            "distance_km": round(dist, 1) if dist is not None else None,
            "sources": sources,
            "check_in_time": p.get("check_in_time"),
            "link": p.get("link"),
        }

        reason = None
        if f.get("max_nightly") and nightly > f["max_nightly"]:
            reason = f"over your ${f['max_nightly']:,.0f} nightly cap"
        elif f.get("min_nightly") and nightly < f["min_nightly"]:
            reason = f"under your ${f['min_nightly']:,.0f} nightly floor"
        elif (p.get("reviews") or 0) < (f.get("min_reviews") or 0):
            reason = f"below your {f['min_reviews']} review minimum"
        elif dist is not None and dist > a["max_distance_km"]:
            reason = f"beyond {a['max_distance_km']} km from {a['name']}"

        target = excluded if reason else kept
        if reason:
            h["reason"] = reason
        if name not in target or nightly < target[name]["nightly"]:
            target[name] = h
    return list(kept.values()), list(excluded.values())


# ---------------------------------------------------------------- ranking

def value_score(h):
    if not h.get("rating") or not h["nightly"]:
        return 0
    return h["rating"] / (h["nightly"] / 100)


def pick_top(hotels, n, min_rating):
    """Top n by value (rating per dollar). The cheapest hotel is always included."""
    ranked = sorted((h for h in hotels if (h["rating"] or 0) >= min_rating), key=value_score, reverse=True)
    top = ranked[:n]
    if hotels:
        cheapest = min(hotels, key=lambda h: h["nightly"])
        if cheapest["name"] not in {h["name"] for h in top}:
            top = top[: n - 1] + [cheapest] if len(top) >= n else top + [cheapest]
    return top


# ---------------------------------------------------------------- persistence

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def load_previous(today_str):
    snaps = sorted(f for f in SNAP_DIR.glob("*.json") if f.stem < today_str)
    if not snaps:
        return None
    return json.loads(snaps[-1].read_text())


def key(h):
    return f"{h['window']}|{h['name']}"


# ---------------------------------------------------------------- explanations

def money(x):
    return f"${x:,.0f}" if x is not None else "n/a"


def explain_added(h, prev_kept, prev_excluded):
    p = prev_kept.get(h["name"])
    if p is None and h["name"] in prev_excluded:
        return f"back within your filters (was {prev_excluded[h['name']]['reason']}), now {money(h['nightly'])}/night"
    if p is None:
        return f"newly listed at {money(h['nightly'])}/night"
    if h["nightly"] < p["nightly"]:
        return f"price fell {money(p['nightly'])} to {money(h['nightly'])}/night"
    if h["nightly"] > p["nightly"]:
        return f"moved up as options ahead of it got pricier or sold out (now {money(h['nightly'])}, was {money(p['nightly'])})"
    return f"moved up the value ranking at {money(h['nightly'])}/night"


def explain_removed(name, yday, today_kept, today_excluded):
    if name in today_excluded:
        return f"{today_excluded[name]['reason']} (now {money(today_excluded[name]['nightly'])}/night)"
    h = today_kept.get(name)
    if h is None:
        return "no longer listed for these dates (likely sold out)"
    if h["nightly"] > yday["nightly"]:
        return f"price rose {money(yday['nightly'])} to {money(h['nightly'])}/night, so cheaper options outranked it"
    return f"still listed at {money(h['nightly'])}/night but outranked by better-value options"


# ---------------------------------------------------------------- Discord payload

def truncate(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def hotel_embed(rank, h, drop, max_amen=6):
    stars = f"{h['stars']}★" if h.get("stars") else "?★"
    rating = f"{h['rating']} ({h['reviews']:,})" if h.get("rating") and h.get("reviews") else (str(h["rating"]) if h.get("rating") else "no rating")
    dist = f"{h['distance_km']} km to SoFi" if h.get("distance_km") is not None else "distance unknown"
    bf = "breakfast included" if h.get("breakfast") else "no breakfast listed"
    total = h["total_after_taxes"] or h["total"]
    tax = " incl. taxes" if h.get("total_after_taxes") else ""
    lines = [f"{stars} · {rating} · {dist} · {bf}",
             f"**{money(h['nightly'])}/night · {money(total)} total{tax}** ({h['nights']} nights)"]
    if drop:
        lines.insert(0, f"🔻 **PRICE DROP** {money(drop['old'])} → {money(h['nightly'])} "
                        f"(−{money(drop['old'] - h['nightly'])}) since you last saw it")
    if h.get("sources"):
        best = h["sources"][0]["nightly"]
        parts = [(f"**{s['source']} {money(s['nightly'])}**" if s["nightly"] == best else f"{s['source']} {money(s['nightly'])}")
                 for s in h["sources"][:5]]
        lines.append("Listed on: " + " · ".join(parts))
    if h.get("amenities"):
        lines.append("Amenities: " + ", ".join(h["amenities"][:max_amen]))
    if h.get("check_in_time"):
        lines.append(f"Check-in {h['check_in_time']}")
    embed = {
        "title": truncate(f"{rank}. {h['name']}", 250),
        "color": GREEN if drop else TEAL,
        "description": "\n".join(lines),
    }
    if h.get("link"):
        embed["url"] = h["link"]
    return embed


def embed_chars(e):
    return len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))


def fit_embeds(embeds, limit=5800):
    """Discord allows 6000 characters across all embeds in a message. Trim amenities until it fits."""
    for max_amen in (6, 4, 2, 0):
        if sum(embed_chars(e) for e in embeds) <= limit:
            return embeds
        for e in embeds:
            lines = [l for l in e["description"].split("\n")]
            for i, l in enumerate(lines):
                if l.startswith("Amenities: "):
                    items = l[len("Amenities: "):].split(", ")[:max_amen]
                    lines[i] = "Amenities: " + ", ".join(items) if items else ""
            e["description"] = "\n".join(x for x in lines if x)
    return embeds


def build_messages(cfg, window, top, changes, seen_drops, today_str, first_run):
    n = len(top)
    drop_by_name = {d["name"]: d for d in seen_drops}
    summary = [f"**{window['label']}**: {window['check_in']} to {window['check_out']}, "
               f"{cfg['search']['adults']} guests, showing {n} hotels (best value, cheapest always included)."]
    if first_run:
        summary.append("\nFirst report. Added/dropped changes and price drops will show from tomorrow.")
    else:
        def block(title, items):
            if not items:
                return [f"\n**{title}**", "None"]
            return [f"\n**{title}** ({len(items)})"] + [f"• **{nm}**: {why}" for nm, why in items]
        summary += block("Added to the list", changes["added"])
        summary += block("Dropped from the list", changes["removed"])
        summary.append("\n**Price drops on hotels you've seen before**")
        if seen_drops:
            for d in sorted(seen_drops, key=lambda d: d["old"] - d["now"], reverse=True):
                pct = round(100 * (d["old"] - d["now"]) / d["old"])
                where = "in today's list" if d["in_top"] else "not in today's top list"
                summary.append(f"• **{d['name']}**: {money(d['old'])} → {money(d['now'])}/night (−{pct}%), {where}")
        else:
            summary.append("None")
    summary_embed = {"title": f"SoFi hotels, {today_str}", "description": truncate("\n".join(summary), 4000), "color": TEAL}
    hotel_embeds = [hotel_embed(i + 1, h, drop_by_name.get(h["name"])) for i, h in enumerate(top)]
    hotel_embeds = fit_embeds(hotel_embeds)
    if hotel_embeds:
        hotel_embeds[-1]["footer"] = {"text": "Prices from Google Hotels via SerpApi. Bold = cheapest listing site."}
    return [{"embeds": [summary_embed]}, {"embeds": hotel_embeds}]


# ---------------------------------------------------------------- Markdown report

def delta_str(h, prev_kept):
    p = prev_kept.get(h["name"])
    if not p:
        return "new"
    d = h["nightly"] - p["nightly"]
    return "-" if d == 0 else f"{'▼' if d < 0 else '▲'} {money(abs(d))}"


def render_markdown(cfg, hotels_by_window, prev_by_window, today_str):
    top_n = cfg["report"]["top_n"]
    min_rating = cfg["report"]["min_rating"]
    lines = [f"# Hotels near SoFi Stadium: {today_str}", "",
             f"{cfg['search']['adults']} guests, {'/'.join(map(str, cfg['search']['hotel_class']))}-star, "
             f"within {cfg['anchor']['max_distance_km']} km of {cfg['anchor']['name']}. "
             f"Prices per night in {cfg['search']['currency']}; change is vs. the previous run.", ""]
    for w in cfg["date_windows"]:
        hs = hotels_by_window.get(w["label"], [])
        prev_kept = prev_by_window.get(w["label"], {})
        lines.append(f"## {w['label']} ({w['check_in']} to {w['check_out']})")
        if not hs:
            lines += ["", "No priced results returned.", ""]
            continue
        cheapest = sorted(hs, key=lambda h: h["nightly"])[:top_n]
        best_value = sorted([h for h in hs if (h["rating"] or 0) >= min_rating], key=value_score, reverse=True)[:top_n]
        for title, rows in (("Cheapest", cheapest), ("Best value (rating per dollar)", best_value)):
            lines += ["", f"### {title}", "",
                      "| Hotel | Stars | Rating | Km | Breakfast | Nightly | Total | Change |",
                      "|---|---|---|---|---|---|---|---|"]
            for h in rows:
                nm = f"[{h['name']}]({h['link']})" if h.get("link") else h["name"]
                lines.append(f"| {nm} | {h['stars'] or '?'} | {h['rating'] or '?'} ({h['reviews'] or 0}) | "
                             f"{h['distance_km'] if h['distance_km'] is not None else '?'} | "
                             f"{'yes' if h.get('breakfast') else '-'} | {money(h['nightly'])} | "
                             f"{money(h['total'])} | {delta_str(h, prev_kept)} |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--mock", help="JSON file of SerpApi 'properties' (offline test)")
    ap.add_argument("--date", help="Override today's date, YYYY-MM-DD (testing)")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    # One snapshot per run (not per day), so each run compares with the previous run.
    now = datetime.now(timezone.utc)
    today_str = args.date or now.strftime("%Y-%m-%dT%H%M")
    tz = ZoneInfo((cfg.get("report") or {}).get("timezone", "America/Toronto"))
    display = datetime.now(tz).strftime("%b %d, %I:%M %p").replace(" 0", " ")
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key and not args.mock:
        sys.exit("SERPAPI_KEY is not set")

    dcfg = cfg.get("discord") or {}
    top_n = int(dcfg.get("top_n", 8))
    min_drop = float(dcfg.get("min_drop_usd", 5))

    prev_snap = load_previous(today_str)
    state = load_json(STATE_FILE, {"shown": {}, "last_top": {}})

    all_kept, all_excluded, hotels_by_window, prev_by_window = [], [], {}, {}
    messages = []
    for w in cfg["date_windows"]:
        label = w["label"]
        props = json.loads(Path(args.mock).read_text()) if args.mock else fetch_window(cfg, w, api_key)
        kept, excluded = normalize(cfg, w, props)
        all_kept += kept
        all_excluded += excluded
        hotels_by_window[label] = kept

        prev_kept = {h["name"]: h for h in (prev_snap or {}).get("hotels", []) if h["window"] == label}
        prev_excl = {h["name"]: h for h in (prev_snap or {}).get("excluded", []) if h["window"] == label}
        prev_by_window[label] = prev_kept
        today_kept = {h["name"]: h for h in kept}
        today_excl = {h["name"]: h for h in excluded}

        top = pick_top(kept, top_n, cfg["report"]["min_rating"])
        top_names = {h["name"] for h in top}

        # Day-over-day changes in the top list, with reasons
        yday_top = {t["name"]: t for t in state["last_top"].get(label, [])}
        first_run = label not in state["last_top"]
        changes = {
            "added": [(h["name"], explain_added(h, prev_kept, prev_excl)) for h in top if h["name"] not in yday_top],
            "removed": [(nm, explain_removed(nm, t, today_kept, today_excl)) for nm, t in yday_top.items() if nm not in top_names],
        }

        # Price drops on hotels already shown on Discord (compared with the price when last shown)
        shown = state["shown"]
        seen_drops = []
        for h in kept:
            rec = shown.get(f"{label}|{h['name']}")
            if rec and rec["price"] - h["nightly"] >= min_drop:
                seen_drops.append({"name": h["name"], "old": rec["price"], "now": h["nightly"], "in_top": h["name"] in top_names})

        messages += build_messages(cfg, w, top, changes, seen_drops, display, first_run)

        # Update state: everything mentioned today (top list and drop callouts) is now "seen"
        mentioned = {h["name"]: h for h in top}
        for d in seen_drops:
            mentioned[d["name"]] = today_kept[d["name"]]
        for nm, h in mentioned.items():
            shown[f"{label}|{nm}"] = {"price": h["nightly"], "date": today_str}
        state["last_top"][label] = [{"name": h["name"], "nightly": h["nightly"]} for h in top]

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (SNAP_DIR / f"{today_str}.json").write_text(json.dumps({"date": today_str, "hotels": all_kept, "excluded": all_excluded}, indent=1))
    STATE_FILE.write_text(json.dumps(state, indent=1))
    md = render_markdown(cfg, hotels_by_window, prev_by_window, display)
    (REPORT_DIR / f"{today_str}.md").write_text(md)
    (REPORT_DIR / "latest.md").write_text(md)
    (REPORT_DIR / "discord.json").write_text(json.dumps(messages, indent=1))
    print(md)


if __name__ == "__main__":
    main()
