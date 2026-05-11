"""
SmartBuy: Global Phone Prices & Reviews by Country
====================================================
Scrapes all currently-buyable smartphones from two sources:
  • GSMArena  — full brand/model catalogue, specs, expert score, user rating,
                user opinion count, availability status, launch year
  • Kimovil   — retail prices per country (50+ countries)

Output: phones.csv
Columns: brand, model, availability, date_launched, gsmarena_score,
         user_rating, num_opinions, country, price_usd, currency,
         price_local, phone_url
"""

import csv
import logging
import random
import re
import time
from pathlib import Path

import cloudscraper
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────
# Set to a list of brand name strings to scrape only those brands,
# or leave as None to auto-discover ALL brands from GSMArena.
BRANDS: list | None = None

PHONES_PER_BRAND = None          # None = all phones per brand (recommended)
AVAILABLE_ONLY   = True          # True = skip Discontinued / Cancelled / Rumoured
OUT_FILE         = Path("phones.csv")
SLEEP_MIN        = 2.0           # seconds between requests (be polite)
SLEEP_MAX        = 4.0
SLEEP_BRAND      = 5.0           # extra pause between brands (avoids 429 bursts)
MAX_RETRIES      = 5
RETRY_429_WAIT   = 60            # seconds to wait after a 429 before retrying

GSMARENA_BASE = "https://www.gsmarena.com"
KIMOVIL_BASE  = "https://www.kimovil.com"

# Availability keywords that mean "on sale right now"
_AVAILABLE_KEYWORDS = {"available", "on sale", "launched"}
_SKIP_KEYWORDS      = {"discontinued", "cancelled", "canceled",
                       "rumoured", "rumored", "coming soon", "not released"}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── HTTP session ──────────────────────────────────────────────────────────────
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

SESSION = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False}
)
SESSION.headers.update({
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
})


def _sleep():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


def _get(url, referer=GSMARENA_BASE):
    """Fetch URL with retries; return BeautifulSoup or None."""
    SESSION.headers["User-Agent"] = random.choice(_USER_AGENTS)
    SESSION.headers["Referer"]    = referer
    for attempt in range(MAX_RETRIES):
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code == 429:
                wait = RETRY_429_WAIT * (attempt + 1)
                log.warning("  429 rate-limited on %s — waiting %ds before retry %d/%d",
                            url, wait, attempt+1, MAX_RETRIES)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except Exception as exc:
            log.warning("  attempt %d/%d failed for %s: %s", attempt+1, MAX_RETRIES, url, exc)
            time.sleep(min(30, 4 ** attempt))
    log.error("  gave up on %s", url)
    return None


# ── GSMArena: discover all brands ─────────────────────────────────────────────

def get_all_brands():
    """
    Scrape GSMArena makers.php3 and return every brand as
    {"name": str, "url": str}.  Single HTTP call covers all brands.
    """
    log.info("Fetching brand list from GSMArena...")
    soup = _get(f"{GSMARENA_BASE}/makers.php3")
    _sleep()
    if not soup:
        raise RuntimeError("Could not load GSMArena brand list.")

    brands = []
    seen   = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "-phones-" not in href:
            continue
        # GSMArena renders: <a href="samsung-phones-9.php">Samsung<span>1455 devices</span></a>
        # get_text() gives "Samsung1455 devices" — use the first bare text node instead.
        name = next((s.strip() for s in a.strings if s.strip() and not re.match(r'^\d+', s.strip())), "")
        if not name or name in seen:
            continue
        seen.add(name)
        url = href if href.startswith("http") else f"{GSMARENA_BASE}/{href}"
        brands.append({"name": name, "url": url})

    log.info("  Found %d brands", len(brands))
    return brands


# ── GSMArena: brand page -> phone URLs ────────────────────────────────────────

def get_phone_urls(brand, brand_url, limit):
    """
    Paginate GSMArena brand listing -> list of {brand, model, phone_url}.
    """
    phones   = []
    page_url = brand_url

    while page_url:
        soup = _get(page_url)
        _sleep()
        if not soup:
            break

        for li in soup.select("div.makers ul li"):
            a = li.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            if not re.search(r"[\w]+-[\w_]+_\d+\.php", href):
                continue

            strong = a.find("strong")
            model  = strong.get_text(strip=True) if strong else a.get_text(strip=True)
            if not model:
                continue

            phones.append({
                "brand"    : brand,
                "model"    : model,
                "phone_url": href if href.startswith("http") else f"{GSMARENA_BASE}/{href}",
            })

            if limit and len(phones) >= limit:
                return phones

        nxt = soup.select_one("a.pages-next") or soup.select_one("a[title='Next page']")
        if nxt and nxt.get("href"):
            h = nxt["href"]
            page_url = h if h.startswith("http") else f"{GSMARENA_BASE}/{h}"
        else:
            break

    log.info("  %s: %d phones found", brand, len(phones))
    return phones


# ── GSMArena: phone detail page ───────────────────────────────────────────────

def get_phone_details(phone_url):
    """
    Scrape a GSMArena phone detail page.

    Returns dict with keys:
        availability   - "Available" | "Discontinued" | "Coming soon" | ""
        date_launched  - 4-digit year string, e.g. "2024"
        gsmarena_score - expert review score 0-100 (empty if unreviewed)
        user_rating    - crowd rating out of 10
        num_opinions   - number of user votes behind user_rating
    """
    result = {
        "availability"  : "",
        "date_launched" : "",
        "gsmarena_score": "",
        "user_rating"   : "",
        "num_opinions"  : "",
    }

    soup = _get(phone_url)
    _sleep()
    if not soup:
        return result

    # ── Specs table rows ──────────────────────────────────────────────────────
    for ttl in soup.select("td.ttl"):
        label = ttl.get_text(strip=True).lower()
        nfo   = ttl.find_next_sibling("td", class_="nfo")
        if not nfo:
            continue
        nfo_text = nfo.get_text(strip=True)

        if "announced" in label and not result["date_launched"]:
            m = re.search(r"\b(20\d{2}|19\d{2})\b", nfo_text)
            if m:
                result["date_launched"] = m.group()

        if "status" in label and not result["availability"]:
            lower = nfo_text.lower()
            if any(k in lower for k in _AVAILABLE_KEYWORDS):
                result["availability"] = "Available"
            else:
                result["availability"] = nfo_text.split(".")[0].strip()

    # ── Expert / GSMArena review score ────────────────────────────────────────
    for sel in [
        ".score-specs-review .score-total",
        ".review-score",
        "a.link-review span",
        "[class*='score-total']",
    ]:
        score_el = soup.select_one(sel)
        if score_el:
            m = re.search(r"\d+", score_el.get_text(strip=True))
            if m:
                result["gsmarena_score"] = m.group()
                break

    if not result["gsmarena_score"]:
        for meta in soup.find_all("meta"):
            if "score" in str(meta.get("property", "")).lower():
                m = re.search(r"\d+", meta.get("content", ""))
                if m:
                    result["gsmarena_score"] = m.group()
                    break

    # ── User rating (itemprop is the canonical selector on GSMArena) ──────────
    rating_el = soup.select_one("[itemprop='ratingValue']")
    if not rating_el:
        rating_el = (
            soup.select_one(".rating-link .link-spoilers")
            or soup.select_one(".opinion-score")
        )
    if rating_el:
        m = re.search(r"[\d.]+", rating_el.get_text(strip=True))
        if m:
            result["user_rating"] = m.group()

    # ── Number of user opinions behind the rating ─────────────────────────────
    count_el = soup.select_one("[itemprop='ratingCount']")
    if not count_el:
        count_el = (
            soup.select_one(".rating-count")
            or soup.select_one("[class*='opinion-count']")
        )
    if count_el:
        m = re.search(r"[\d,]+", count_el.get_text(strip=True))
        if m:
            result["num_opinions"] = m.group().replace(",", "")

    return result


# ── Kimovil: prices per country ───────────────────────────────────────────────

def _kimovil_slug(brand, model):
    """Convert 'Samsung Galaxy S25 Ultra' -> 'samsung-galaxy-s25-ultra'."""
    raw = f"{brand} {model}".lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return raw


def get_kimovil_prices(brand, model):
    """
    Scrape Kimovil for retail prices across countries.
    Returns: [{country, price_usd, currency, price_local}]

    IMPORTANT: uses /en/where-to-buy-and-price/ — the actual price listing page.
    The old /en/frequency-checker/ URL is a radio-band compatibility checker
    and does NOT contain retail prices.
    """
    slug = _kimovil_slug(brand, model)
    url  = f"{KIMOVIL_BASE}/en/where-to-buy-and-price/{slug}"
    soup = _get(url, referer=KIMOVIL_BASE)
    _sleep()
    if not soup:
        return []

    rows = []
    for tr in soup.select("table.freq-table tr, .price-table tr, "
                          ".prices-by-country tr, table tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        country   = cells[0].get_text(strip=True)
        price_raw = cells[-1].get_text(strip=True)

        if not country or country.lower() in ("country", "region", ""):
            continue

        currency_sym  = re.search(r"[A-Z]{3}|[$€£¥₹₩]", price_raw)
        price_numeric = re.search(r"[\d,]+\.?\d*", price_raw)

        if not price_numeric:
            continue

        price_local = price_numeric.group().replace(",", "")
        currency    = currency_sym.group() if currency_sym else "?"

        usd_cells = tr.select("td.usd, td.price-usd")
        price_usd = ""
        if usd_cells:
            m = re.search(r"[\d,]+", usd_cells[0].get_text())
            if m:
                price_usd = m.group().replace(",", "")

        rows.append({
            "country"    : country,
            "price_usd"  : price_usd,
            "currency"   : currency,
            "price_local": price_local,
        })

    # Deduplicate by country (keep first hit)
    seen  = set()
    dedup = []
    for r in rows:
        if r["country"] not in seen:
            seen.add(r["country"])
            dedup.append(r)
    return dedup


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    fieldnames = [
        "brand", "model", "availability", "date_launched",
        "gsmarena_score", "user_rating", "num_opinions",
        "country", "price_usd", "currency", "price_local",
        "phone_url",
    ]

    # Resume support: skip already-written (brand, model, country) combos
    done = set()
    write_header = not OUT_FILE.exists()
    if OUT_FILE.exists():
        with open(OUT_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add((row["brand"], row["model"], row["country"]))
        log.info("Resuming -- %d combos already written", len(done))

    out_fh = open(OUT_FILE, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_fh, fieldnames=fieldnames)
    if write_header:
        writer.writeheader()

    # ── Brand list: auto-discover or use override ─────────────────────────────
    if BRANDS is None:
        brand_list = get_all_brands()           # all brands from GSMArena
    else:
        makers_soup = _get(f"{GSMARENA_BASE}/makers.php3")
        _sleep()
        url_map = {}
        if makers_soup:
            for a in makers_soup.find_all("a", href=True):
                if "-phones-" not in a["href"]:
                    continue
                name = next((s.strip() for s in a.strings if s.strip() and not re.match(r'^\d+', s.strip())), "")
                href = a["href"]
                url_map[name.lower()] = (
                    href if href.startswith("http") else f"{GSMARENA_BASE}/{href}"
                )
        brand_list = []
        for b in BRANDS:
            url = url_map.get(b.lower())
            if url:
                brand_list.append({"name": b, "url": url})
            else:
                log.warning("Brand not found on GSMArena: %s", b)

    try:
        for brand_info in brand_list:
            brand     = brand_info["name"]
            brand_url = brand_info["url"]
            log.info("== %s ==", brand)
            time.sleep(SLEEP_BRAND)   # pause between brands to avoid rate-limiting

            phones = get_phone_urls(brand, brand_url, PHONES_PER_BRAND)
            if not phones:
                log.warning("  No phones found for %s", brand)
                continue

            for i, phone in enumerate(phones, 1):
                b, m = phone["brand"], phone["model"]
                log.info("  [%d/%d] %s %s", i, len(phones), b, m)

                details = get_phone_details(phone["phone_url"])

                # ── Availability filter ───────────────────────────────────────
                if AVAILABLE_ONLY:
                    status = details["availability"].lower()
                    if status and not any(k in status for k in _AVAILABLE_KEYWORDS):
                        log.info("    -> skipping (%s)", details["availability"])
                        continue

                prices = get_kimovil_prices(b, m)
                if not prices:
                    prices = [{"country": "", "price_usd": "",
                               "currency": "", "price_local": ""}]

                for price_row in prices:
                    key = (b, m, price_row["country"])
                    if key in done:
                        continue
                    done.add(key)

                    writer.writerow({
                        "brand"         : b,
                        "model"         : m,
                        "availability"  : details["availability"],
                        "date_launched" : details["date_launched"],
                        "gsmarena_score": details["gsmarena_score"],
                        "user_rating"   : details["user_rating"],
                        "num_opinions"  : details["num_opinions"],
                        "country"       : price_row["country"],
                        "price_usd"     : price_row["price_usd"],
                        "currency"      : price_row["currency"],
                        "price_local"   : price_row["price_local"],
                        "phone_url"     : phone["phone_url"],
                    })
                out_fh.flush()

    finally:
        out_fh.close()

    log.info("Done. Output: %s", OUT_FILE)


if __name__ == "__main__":
    main()
