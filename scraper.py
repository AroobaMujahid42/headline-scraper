import csv
import os
import argparse
import re
import json
import logging
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "HeadlineScraper/1.0 (learning project)"}
TIMEOUT = 10
MAX_RETRIES = 3
AGENT = "HeadlineScraper"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("logs/scraper.log", encoding="utf-8"),
              logging.StreamHandler()],
)

session = requests.Session()
session.headers.update(HEADERS)


# ---------- Step 3: fetching ----------
def fetch(url):
    """Return page text, or None if it fails."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if 400 <= r.status_code < 500:
                logging.error("FAILED %s -> HTTP %s (not retrying)", url, r.status_code)
                return None
            r.raise_for_status()
            logging.info("FETCHED %s (%s bytes)", url, len(r.text))
            return r.text
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
            logging.warning("RETRY %s/%s for %s: %s", attempt, MAX_RETRIES, url, e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    logging.error("FAILED %s after %s attempts", url, MAX_RETRIES)
    return None


# ---------- Step 4: robots.txt ----------
_robots_cache = {}


def get_robots(url):
    """Return a cached RobotFileParser for the site, or None if unreadable."""
    parts = urlparse(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots_cache:
        rp = RobotFileParser()
        try:
            r = session.get(root + "/robots.txt", timeout=TIMEOUT)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            elif 400 <= r.status_code < 500:
                rp.parse([])
            else:
                rp = None
        except requests.RequestException as e:
            logging.warning("Could not read robots.txt for %s: %s", root, e)
            rp = None
        _robots_cache[root] = rp
    return _robots_cache[root]


def allowed(url):
    """True if robots.txt allows us to fetch this URL."""
    rp = get_robots(url)
    if rp is None:
        logging.warning("SKIPPED %s (robots.txt unavailable)", url)
        return False
    ok = rp.can_fetch(AGENT, url)
    if not ok:
        logging.warning("SKIPPED %s (disallowed by robots.txt)", url)
    return ok


def crawl_delay(url, default=2):
    """Seconds to wait between requests to this site."""
    rp = get_robots(url)
    delay = rp.crawl_delay(AGENT) if rp else None
    return delay if delay else default


# ---------- Step 5: parsing ----------
def load_sources(path="sources.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse(source, text):
    """Extract headline dicts from a page using the source's selectors."""
    is_xml = source["url"].endswith((".xml", ".rss"))
    soup = BeautifulSoup(text, "xml" if is_xml else "lxml")
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    items = []
    for block in soup.select(source["item"]):
        title_el = block.select_one(source["title"])
        link_el = block.select_one(source["link"])
        if not title_el or not link_el:
            continue

        title = " ".join(title_el.get_text().split())

        # RSS <link> holds the URL as text; HTML uses the href attribute
        href = link_el.get("href") or link_el.get_text(strip=True)
        url = urljoin(source["url"], href)

        published = None
        if source.get("time"):
            time_el = block.select_one(source["time"])
            if time_el:
                published = time_el.get("datetime") or time_el.get_text(strip=True)

        if title and url.startswith("http"):
            items.append({
                "source": source["name"],
                "title": title,
                "url": url,
                "published": published,
                "scraped_at": scraped_at,
            })
    return items


def scrape_source(source):
    """Fetch and parse one source. Returns a list of headlines."""
    if not allowed(source["url"]):
        return []
    text = fetch(source["url"])
    if text is None:
        return []
    items = parse(source, text)
    if not items:
        logging.warning("0 headlines from %s (selectors may be outdated)", source["name"])
    else:
        logging.info("PARSED %s headlines from %s", len(items), source["name"])
    return items



# ---------- Step 6: clean and deduplicate ----------
ARTICLE_ID = re.compile(r"/(?:articles|videos|live)/([a-z0-9]+)")


def normalize_url(url):
    """Strip tracking parameters and unify the BBC domain."""
    parts = urlparse(url)
    netloc = parts.netloc.replace("bbc.co.uk", "bbc.com")
    return f"{parts.scheme}://{netloc}{parts.path}"


def story_key(item):
    """A key that is the same for the same story across sources."""
    m = ARTICLE_ID.search(item["url"])
    return m.group(1) if m else normalize_url(item["url"])


def is_real_story(item):
    """Drop promos such as the app page and iPlayer ads."""
    return bool(ARTICLE_ID.search(item["url"]))


def clean(items):
    """Filter promos, normalize URLs, and remove duplicates."""
    best = {}  # key -> item
    order = []
    for it in items:
        if not is_real_story(it):
            continue
        it = {**it, "url": normalize_url(it["url"])}
        key = story_key(it)
        if key not in best:
            best[key] = it
            order.append(key)
        else:
            # Keep the first copy, but fill in a missing time from later ones
            old = best[key]
            if not old["published"] and it["published"]:
                old["published"] = it["published"]
                old["source"] = it["source"]
    return [best[k] for k in order]

# ---------- Step 7: time normalizing and keyword filter ----------
REL_TIME = re.compile(r"(\d+)\s*(min|hr|hour|day)s?\s*ago", re.I)


def to_iso(published, scraped_at):
    """Convert RSS dates or 'N hrs ago' into an ISO timestamp, or None."""
    if not published:
        return None
    if re.search(r"just now", published, re.I):
        return scraped_at
    # RSS style: "Wed, 30 Sep 2026 05:02:50 GMT" (fix "07GMT" and "Thu,17")
    fixed = re.sub(r"(\d)GMT", r"\1 GMT", published)
    fixed = re.sub(r"^(\w{3}),(\S)", r"\1, \2", fixed)
    try:
        return parsedate_to_datetime(fixed).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass
    # Relative style: "3 hrs ago" (approximate, based on scrape time)
    m = REL_TIME.search(published)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"min": timedelta(minutes=n), "hr": timedelta(hours=n),
                 "hour": timedelta(hours=n), "day": timedelta(days=n)}[unit]
        base = datetime.fromisoformat(scraped_at)
        return (base - delta).isoformat(timespec="seconds")
    return None


def add_iso_times(items):
    for it in items:
        it["published_iso"] = to_iso(it["published"], it["scraped_at"])
    return items


def matches(item, keywords):
    """True if no keywords are given, or any keyword appears as a whole word."""
    if not keywords:
        return True
    for k in keywords:
        if re.search(rf"\b{re.escape(k)}\b", item["title"], re.I):
            return True
    return False


def parse_args():
    p = argparse.ArgumentParser(description="Headline scraper")
    p.add_argument("--keyword", "-k", action="append", default=[],
                   help="filter by keyword (repeat for several)")
    p.add_argument("--limit", type=int, default=0,
                   help="max headlines to show (0 = all)")
    p.add_argument("--format", choices=["json", "csv", "both", "none"],
                   default="both", help="output file format (default: both)")
    p.add_argument("--source", "-s", action="append", default=[],
                   help="only run sources whose name contains this text (repeatable)")
    p.add_argument("--delay", type=float, default=None,
                   help="seconds to wait between sources (default: site crawl delay or 2)")
    return p.parse_args()

# ---------- Step 8: saving ----------
FIELDS = ["source", "title", "url", "published", "published_iso", "scraped_at"]


def save_json(items, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def save_csv(items, path):
    # utf-8-sig so Excel displays non-English text correctly
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)


def save(items, fmt, outdir="output"):
    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    base = os.path.join(outdir, f"headlines_{stamp}")
    saved = []
    if fmt in ("json", "both"):
        save_json(items, base + ".json")
        saved.append(base + ".json")
    if fmt in ("csv", "both"):
        save_csv(items, base + ".csv")
        saved.append(base + ".csv")
    for p in saved:
        logging.info("SAVED %s items to %s", len(items), p)


def pick_sources(sources, names):
    """Keep sources whose name contains any of the given texts."""
    if not names:
        return sources
    wanted = [n.lower() for n in names]
    return [s for s in sources if any(w in s["name"].lower() for w in wanted)]

if __name__ == "__main__":
    args = parse_args()

    sources = pick_sources(load_sources(), args.source)
    if not sources:
        raise SystemExit("No sources matched --source")

    all_items = []
    for src in sources:
        all_items.extend(scrape_source(src))
        time.sleep(args.delay if args.delay is not None else crawl_delay(src["url"]))

    raw_count = len(all_items)
    all_items = add_iso_times(clean(all_items))
    logging.info("CLEANED %s -> %s headlines", raw_count, len(all_items))

    all_items = [it for it in all_items if matches(it, args.keyword)]
    if args.keyword:
        logging.info("KEYWORD %s -> %s matches", args.keyword, len(all_items))
    if args.limit:
        all_items = all_items[:args.limit]

    for it in all_items:
        print(f"[{it['source']}] {it['title']}")
        print(f"    {it['url']}  |  {it['published_iso'] or it['published']}")
    print(f"\nTotal: {len(all_items)} headlines")

    if args.format != "none" and all_items:
        save(all_items, args.format)

    by_source = {}
    for it in all_items:
        by_source[it["source"]] = by_source.get(it["source"], 0) + 1
    print("\nSummary:")
    for name, n in by_source.items():
        print(f"  {name}: {n}")
       