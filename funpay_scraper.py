"""Snapshot FunPay WoW: Forever gold listings (https://funpay.com/en/chips/258/).

Each run fetches the public listing page once and appends every offer
(server, faction, seller, stock, price) to data/offers_YYYY-MM-DD.csv (UTC day).
Daily text files keep the git history small when this runs on GitHub Actions.
Use funpay_report.py to build the report.
"""
import csv
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://funpay.com/en/chips/258/"
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOG = HERE / "funpay_scraper.log"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
FIELDS = ["ts", "offer_id", "user_id", "seller", "server_id", "server", "side_id",
          "side", "online", "promo", "rating", "reviews", "seller_age", "stock", "price_eur"]


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def num(s):
    try:
        return float(s.replace(",", "").replace("\xa0", "").strip())
    except (ValueError, AttributeError):
        return None


def fetch():
    for attempt in range(3):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                r.encoding = "utf-8"
                return r.text
            log(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            log(f"request error: {e}")
        time.sleep(60 * (attempt + 1))
    raise RuntimeError("failed to fetch page after 3 attempts")


def parse(html, ts):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for a in soup.select("a.tc-item"):
        href = a.get("href", "")
        offer_id = href.split("id=")[-1] if "id=" in href else href
        stars = a.select_one(".rating-stars")
        rating = None
        if stars:
            for c in stars.get("class", []):
                if c.startswith("rating-") and c[7:].isdigit():
                    rating = int(c[7:])
        price_div = a.select_one(".tc-price div")
        price = num(price_div.contents[0]) if price_div and price_div.contents else None
        amt = a.select_one(".tc-amount")
        txt = lambda sel: (a.select_one(sel).get_text(strip=True) if a.select_one(sel) else None)
        rows.append({
            "ts": ts,
            "offer_id": offer_id,
            "user_id": offer_id.split("-")[0],
            "seller": txt(".media-user-name"),
            "server_id": a.get("data-server"),
            "server": txt(".tc-server"),
            "side_id": a.get("data-side"),
            "side": txt(".tc-side"),
            "online": 1 if a.get("data-online") == "1" else 0,
            "promo": 1 if "offer-promo" in a.get("class", []) else 0,
            "rating": rating,
            "reviews": int(num(txt(".rating-mini-count") or "") or 0),
            "seller_age": txt(".media-user-info"),
            "stock": num(amt.get("data-s") or amt.get_text()) if amt else None,
            "price_eur": price,
        })
    return rows


def save(rows, ts):
    DATA.mkdir(exist_ok=True)
    path = DATA / f"offers_{ts[:10]}.csv"
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def main():
    if "--now" not in sys.argv:
        time.sleep(random.uniform(0, 600))  # jitter so requests aren't on the exact hour
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = parse(fetch(), ts)
        if not rows:
            log("WARNING: 0 offers parsed - page layout may have changed")
            return 1
        save(rows, ts)
        log(f"saved {len(rows)} offers at {ts}")
    except Exception as e:
        log(f"ERROR: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
