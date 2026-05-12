"""
SmartBuy: Global Phone Prices & Reviews by Country
====================================================
Source: Kimovil.com  (single source — prices + reviews + all brands)

Strategy (no more GSMArena):
  1. Fetch Kimovil's XML sitemap  → all phone URLs instantly, no pagination,
     no rate-limiting (sitemaps are meant for crawlers)
  2. For each phone slug, GET /en/where-to-buy-and-price/{slug}
     → prices per country + user score + review count
  3. Write rows to phones.csv

Output columns:
  brand, model, variant, rating, num_reviews,
  country, price_usd, currency, price_local, phone_url
"""

import csv
import gzip
import logging
import random
import re
import time
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────
OUT_FILE    = Path("phones.csv")
SLEEP_MIN   = 1.5        # seconds between price-page requests
SLEEP_MAX   = 3.5
MAX_RETRIES = 4
RETRY_WAIT  = 30         # seconds to pause on 429

BASE        = "https://www.kimovil.com"
SITEMAP_IDX = f"{BASE}/en/sitemap.xml"   # or sitemap-index.xml

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── HTTP session ──────────────────────────────────────────────────────────────
_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

SESSION = requests.Session()
SESSION.headers.update({
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         BASE,
})


def _sleep():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


def _get_raw(url, binary=False):
    """Fetch URL; return response text (or bytes if binary=True), or None."""
    SESSION.headers["User-Agent"] = random.choice(_UAS)
    for attempt in range(MAX_RETRIES):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 429:
                wait = RETRY_WAIT * (attempt + 1)
                log.warning("429 on %s — sleeping %ds (attempt %d/%d)",
                            url, wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.content if binary else r.text
        except Exception as exc:
            log.warning("attempt %d/%d failed — %s: %s", attempt + 1, MAX_RETRIES, url, exc)
            time.sleep(10 * (attempt + 1))
    log.error("gave up: %s", url)
    return None


def _get_soup(url):
    html = _get_raw(url)
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")
    # Sanity check: if page looks like a bot-block, return None
    text = soup.get_text()
    if len(text.strip()) < 200:
        log.warning("Suspiciously short page — possible bot block: %s", url)
        log.warning("  Content preview: %s", text[:200])
        return None
    return soup


# ── STEP 1: Collect all phone slugs from sitemap ──────────────────────────────

def _parse_sitemap_xml(xml_text):
    """Extract all <loc> URLs from a sitemap XML string."""
    urls = []
    try:
        root = ET.fromstring(xml_text)
        ns   = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//sm:loc", ns):
            if loc.text:
                urls.append(loc.text.strip())
    except ET.ParseError as e:
        log.error("XML parse error: %s", e)
    return urls


def get_all_price_slugs():
    """
    Walk Kimovil's sitemap(s) and return all slugs for
    /en/where-to-buy-and-price/ pages (one per phone model/variant).
    """
    log.info("Fetching sitemap index: %s", SITEMAP_IDX)
    xml = _get_raw(SITEMAP_IDX)
    if not xml:
        # Try alternate name
        log.info("Trying alternate sitemap URL...")
        xml = _get_raw(f"{BASE}/sitemap.xml")
    if not xml:
        raise RuntimeError("Could not fetch Kimovil sitemap.")

    # Could be a sitemap index (points to sub-sitemaps) or a direct sitemap
    all_urls = _parse_sitemap_xml(xml)
    log.info("  sitemap index returned %d URLs", len(all_urls))

    price_slugs = []

    def _extract_from_url_list(urls):
        for u in urls:
            if "/where-to-buy-and-price/" in u:
                slug = u.rstrip("/").split("/")[-1]
                price_slugs.append(slug)

    # If index contains sub-sitemap URLs, fetch each one
    sub_sitemaps = [u for u in all_urls if "sitemap" in u.lower() and u.endswith(".xml")]
    direct_price = [u for u in all_urls if "/where-to-buy-and-price/" in u]

    if direct_price:
        log.info("  Direct price pages in top-level sitemap: %d", len(direct_price))
        _extract_from_url_list(direct_price)

    if sub_sitemaps:
        log.info("  Sub-sitemaps to fetch: %d", len(sub_sitemaps))
        for sm_url in sub_sitemaps:
            log.info("    Fetching sub-sitemap: %s", sm_url)
            content = _get_raw(sm_url, binary=True)
            if not content:
                continue
            # Handle gzipped sitemaps (.xml.gz)
            if sm_url.endswith(".gz"):
                try:
                    content = gzip.decompress(content)
                except Exception:
                    pass
            sub_urls = _parse_sitemap_xml(content.decode("utf-8", errors="replace"))
            _extract_from_url_list(sub_urls)
            _sleep()

    # Deduplicate
    seen = set()
    deduped = []
    for s in price_slugs:
        if s not in seen:
            seen.add(s)
            deduped.append(s)

    log.info("Total unique phone slugs: %d", len(deduped))
    return deduped


# ── STEP 2: Scrape each price page ───────────────────────────────────────────

def parse_price_page(slug):
    """
    Scrape /en/where-to-buy-and-price/{slug}.
    Returns dict:
      brand, model, variant, rating, num_reviews,
      prices: [{country, price_usd, currency, price_local}]
    """
    url  = f"{BASE}/en/where-to-buy-and-price/{slug}"
    soup = _get_soup(url)
    if not soup:
        return None

    # ── Brand / Model ─────────────────────────────────────────────────────────
    brand = model = variant = ""

    h1 = soup.find("h1")
    if h1:
        raw = h1.get_text(strip=True)
        # Strip "price and where to buy" suffix
        raw = re.sub(r"\s*[-–|]?\s*(price|where to buy).*", "", raw, flags=re.I).strip()
        # First word = brand, rest = model
        parts = raw.split(None, 1)
        brand = parts[0] if parts else ""
        model = parts[1] if len(parts) > 1 else ""

    # Variant: storage/RAM sometimes in h2 or sub-heading
    h2 = soup.find("h2")
    if h2:
        v = h2.get_text(strip=True)
        if re.search(r"\d+\s*(GB|TB|MB)", v, re.I):
            variant = v

    # ── Rating ────────────────────────────────────────────────────────────────
    rating = num_reviews = ""
    for sel in ["[itemprop='ratingValue']", ".rating-value", ".score-value",
                "[class*='rating'] [class*='value']", ".dxrating"]:
        el = soup.select_one(sel)
        if el:
            m = re.search(r"[\d.]+", el.get_text())
            if m:
                rating = m.group()
                break

    for sel in ["[itemprop='ratingCount']", ".rating-count", ".reviews-count",
                "[class*='votes']", "[class*='review-count']"]:
        el = soup.select_one(sel)
        if el:
            m = re.search(r"\d+", el.get_text())
            if m:
                num_reviews = m.group()
                break

    # ── Prices by country ─────────────────────────────────────────────────────
    prices      = []
    seen_ctries = set()

    # Try multiple table/row selectors
    rows = (soup.select("table tr")
            or soup.select("[class*='price'] tr")
            or soup.select("[class*='country'] tr"))

    for tr in rows:
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue

        country = cells[0].strip()
        if (not country
                or country.lower() in ("country", "where", "location", "flag", "")
                or country in seen_ctries):
            continue

        # Scan cells for a price value
        price_local = price_usd = currency = ""
        for cell in cells[1:]:
            m_price = re.search(r"[\d,]+\.?\d*", cell)
            m_curr  = re.search(r"\b([A-Z]{3})\b|([€£$¥₹₩₺₴₦])", cell)
            if m_price:
                price_local = m_price.group().replace(",", "")
                if m_curr:
                    currency = next(g for g in m_curr.groups() if g)
                break

        # Last numeric cell often = USD equivalent
        for cell in reversed(cells[1:]):
            m = re.search(r"[\d,]+", cell)
            if m:
                price_usd = m.group().replace(",", "")
                break

        if price_local and country:
            seen_ctries.add(country)
            prices.append({
                "country":     country,
                "price_usd":   price_usd,
                "currency":    currency,
                "price_local": price_local,
            })

    # If no table rows matched, log a snippet for debugging
    if not prices:
        log.warning("  No prices found for %s — page snippet:", slug)
        log.warning("  %s", soup.get_text()[:300].replace('\n', ' '))

    return {
        "brand":       brand,
        "model":       model,
        "variant":     variant,
        "rating":      rating,
        "num_reviews": num_reviews,
        "prices":      prices,
        "phone_url":   url,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    fieldnames = [
        "brand", "model", "variant", "rating", "num_reviews",
        "country", "price_usd", "currency", "price_local", "phone_url",
    ]

    # Resume: skip already-written (phone_url, country) pairs
    done        = set()
    write_header = not OUT_FILE.exists()
    if OUT_FILE.exists():
        with open(OUT_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add((row.get("phone_url", ""), row.get("country", "")))
        log.info("Resuming — %d rows already saved", len(done))

    out_fh = open(OUT_FILE, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_fh, fieldnames=fieldnames)
    if write_header:
        writer.writeheader()

    try:
        slugs = get_all_price_slugs()
        if not slugs:
            log.error("No slugs found — check sitemap URLs above.")
            return

        for i, slug in enumerate(slugs, 1):
            log.info("[%d/%d] %s", i, len(slugs), slug)
            data = parse_price_page(slug)
            _sleep()

            if not data:
                continue

            rows_to_write = data["prices"] or [
                {"country": "", "price_usd": "", "currency": "", "price_local": ""}
            ]

            for pr in rows_to_write:
                key = (data["phone_url"], pr["country"])
                if key in done:
                    continue
                done.add(key)
                writer.writerow({
                    "brand":       data["brand"],
                    "model":       data["model"],
                    "variant":     data["variant"],
                    "rating":      data["rating"],
                    "num_reviews": data["num_reviews"],
                    "country":     pr["country"],
                    "price_usd":   pr["price_usd"],
                    "currency":    pr["currency"],
                    "price_local": pr["price_local"],
                    "phone_url":   data["phone_url"],
                })
            out_fh.flush()

    finally:
        out_fh.close()

    log.info("Done. Output: %s", OUT_FILE)


if __name__ == "__main__":
    main()
