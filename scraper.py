"""
SmartBuy: Global Phone Prices & Reviews by Country
====================================================
Source: Kimovil.com  (single source — prices + reviews + all brands)

Strategy:
  1. Use Playwright (real Chromium) to bypass bot detection.
  2. Fetch Kimovil's XML sitemap → all phone slugs, no pagination.
  3. For each slug, navigate to /en/where-to-buy-and-price/{slug}
     → prices per country + user score + review count.
  4. Write rows to phones.csv.

One-time setup (after pip install):
  python -m playwright install chromium

Output columns:
  brand, model, variant, rating, num_reviews,
  country, price_usd, currency, price_local, phone_url
"""

import asyncio
import csv
import gzip
import logging
import random
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────────
OUT_FILE    = Path("phones.csv")
SLEEP_MIN   = 1500        # ms between navigations
SLEEP_MAX   = 3500
MAX_RETRIES = 3
RETRY_WAIT  = 30_000      # ms to pause on rate-limit signals

BASE        = "https://www.kimovil.com"
SITEMAP_IDX = f"{BASE}/en/sitemap.xml"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Playwright helpers ────────────────────────────────────────────────────────

async def _rand_sleep(page):
    """Human-paced delay between requests."""
    ms = random.randint(SLEEP_MIN, SLEEP_MAX)
    await page.wait_for_timeout(ms)


async def _fetch_text(page, url, wait_selector=None):
    """
    Navigate to *url* in the existing page and return its full HTML.
    Retries up to MAX_RETRIES times on transient failures.
    Returns None if every attempt fails.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)

            # Optional: wait for a key element to confirm the page rendered
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=10_000)
                except PWTimeout:
                    pass  # selector missing is fine — we'll handle it downstream

            # Basic bot-wall detection
            body_text = await page.inner_text("body")
            if len(body_text.strip()) < 200:
                log.warning("Suspiciously short body on attempt %d — %s", attempt, url)
                await page.wait_for_timeout(RETRY_WAIT)
                continue

            return await page.content()

        except PWTimeout:
            log.warning("Timeout attempt %d/%d — %s", attempt, MAX_RETRIES, url)
            await page.wait_for_timeout(10_000 * attempt)
        except Exception as exc:
            log.warning("Error attempt %d/%d — %s: %s", attempt, MAX_RETRIES, url, exc)
            await page.wait_for_timeout(10_000 * attempt)

    log.error("Gave up: %s", url)
    return None


async def _fetch_bytes(context, url):
    """
    Download binary content (e.g. gzipped sitemaps) via a new browser page
    by intercepting the response body.  Falls back to None on failure.
    """
    page = await context.new_page()
    body = None
    try:
        resp = await page.goto(url, wait_until="commit", timeout=30_000)
        if resp and resp.ok:
            body = await resp.body()
    except Exception as exc:
        log.warning("Binary fetch failed for %s: %s", url, exc)
    finally:
        await page.close()
    return body


# ── STEP 1: Collect phone slugs from sitemap ──────────────────────────────────

def _parse_sitemap_xml(xml_bytes_or_str):
    """Return all <loc> URLs from a sitemap XML blob."""
    urls = []
    if isinstance(xml_bytes_or_str, str):
        xml_bytes_or_str = xml_bytes_or_str.encode()
    try:
        root = ET.fromstring(xml_bytes_or_str)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//sm:loc", ns):
            if loc.text:
                urls.append(loc.text.strip())
    except ET.ParseError as e:
        log.error("XML parse error: %s", e)
    return urls


async def get_all_price_slugs(context, page=None):
    """
    Walk Kimovil's sitemap tree (up to 5 levels deep) and return every phone slug.

    Kimovil uses a 3-level sitemap hierarchy:
      /sitemap.xml
        → /en/sitemaps/sitemap.en.xml          (language index)
          → sitemap-datasheets-smartphones-KFC-index.en.xml  (category index)
            → sitemap-datasheets-smartphones-KFC-0.en.xml    (actual phone URLs)

    Phone pages appear as /en/find-{slug} or /en/frequencies/{slug}.
    The slug is shared with /en/where-to-buy-and-price/{slug}.

    Falls back to scraping the Kimovil search/listing API if the sitemap
    tree yields nothing.
    """
    # ── slug extraction helpers ───────────────────────────────────────────────
    _PHONE_MARKERS = ("/where-to-buy-and-price/", "/frequencies/", "/compare/")
    _FIND_RE  = re.compile(r"/find-([a-z0-9][a-z0-9-]{3,})(?:[/?#]|$)")
    _SLUG_VAL = re.compile(r"^[a-z0-9][a-z0-9-]{3,}$")

    def _slug_from_url(u: str):
        for marker in _PHONE_MARKERS:
            if marker in u:
                s = u.rstrip("/").split("/")[-1].split("?")[0]
                if _SLUG_VAL.match(s):
                    return s
        m = _FIND_RE.search(u)
        return m.group(1) if m else None

    seen_slugs:    set[str] = set()
    visited_maps:  set[str] = set()
    price_slugs:   list[str] = []

    # ── recursive sitemap walker ──────────────────────────────────────────────
    async def _walk(url: str, depth: int = 0):
        if url in visited_maps or depth > 5:
            return
        visited_maps.add(url)

        content = await _fetch_bytes(context, url)
        if not content:
            log.warning("  [depth %d] Empty response: %s", depth, url)
            return
        if url.endswith(".gz"):
            try:
                content = gzip.decompress(content)
            except Exception:
                pass

        child_urls = _parse_sitemap_xml(content)
        log.info("  [depth %d] %s → %d URLs", depth, url.split("/")[-1], len(child_urls))
        if child_urls:
            log.info("    sample: %s", child_urls[:2])

        sub_sitemaps  = []
        phone_hits    = 0
        for u in child_urls:
            slug = _slug_from_url(u)
            if slug:
                if slug not in seen_slugs:
                    seen_slugs.add(slug)
                    price_slugs.append(slug)
                    phone_hits += 1
            elif ".xml" in u and u not in visited_maps:
                sub_sitemaps.append(u)

        if phone_hits:
            log.info("    → %d new phone slugs (total %d)", phone_hits, len(price_slugs))

        # Recurse into sub-sitemaps.
        # At depth 0 (root sitemap) fetch ALL children — we need to find the
        # right language branch.  At depth ≥ 1, restrict to English to avoid
        # fetching identical slugs 9 times (once per language).
        for sm in sub_sitemaps:
            if depth == 0 or "/en/" in sm or ".en." in sm:
                await _walk(sm, depth + 1)
                if len(price_slugs) >= 500:
                    log.info("  500+ slugs collected — stopping early.")
                    return

    # ── kick off from root ────────────────────────────────────────────────────
    log.info("Fetching root sitemap: %s", SITEMAP_IDX)
    raw = await _fetch_bytes(context, SITEMAP_IDX)
    if not raw:
        log.info("Trying alternate root sitemap…")
        raw = await _fetch_bytes(context, f"{BASE}/sitemap.xml")
    if not raw:
        raise RuntimeError("Could not fetch Kimovil sitemap.")

    root_urls = _parse_sitemap_xml(raw)
    log.info("Root sitemap: %d entries", len(root_urls))

    # Root is an index of language indexes — walk them (English first)
    root_maps = [u for u in root_urls if ".xml" in u]
    root_maps.sort(key=lambda u: (0 if ".en." in u else 1))
    for sm in root_maps:
        await _walk(sm, depth=1)
        if len(price_slugs) >= 500:
            break

    # ── fallback: Kimovil's JSON search API ──────────────────────────────────
    if not price_slugs:
        log.warning("Sitemap walk yielded 0 slugs — trying JSON search API fallback.")
        if page is None:
            _pg = await context.new_page()
            _close = True
        else:
            _pg = page
            _close = False
        try:
            price_slugs = await _scrape_via_api(_pg, seen_slugs)
        finally:
            if _close:
                await _pg.close()

    log.info("Total unique phone slugs: %d", len(price_slugs))
    return price_slugs


async def _scrape_via_api(page, seen_slugs: set) -> list:
    """
    Fallback: use Kimovil's internal search/datasheet JSON endpoint to
    enumerate phone slugs page by page.
    Kimovil loads phones via XHR — intercept the response to harvest slugs.
    """
    slugs: list[str] = []
    _FIND_RE = re.compile(r"/find-([a-z0-9][a-z0-9-]{3,})(?:[/?#]|$)")
    _SLUG_VAL = re.compile(r"^[a-z0-9][a-z0-9-]{3,}$")
    harvested_urls: list[str] = []

    def _on_response(response):
        """Capture any JSON response that looks like a phone listing."""
        ct = response.headers.get("content-type", "")
        if "json" in ct and "kimovil" in response.url:
            harvested_urls.append(response.url)

    page.on("response", _on_response)

    try:
        # Load the listing page and scroll to trigger lazy loading
        log.info("  Loading listing page to capture XHR endpoints…")
        try:
            await page.goto(f"{BASE}/en/phones-list.html",
                            wait_until="networkidle", timeout=60_000)
        except Exception:
            await page.goto(f"{BASE}/en/phones-list.html",
                            wait_until="domcontentloaded", timeout=45_000)

        await page.wait_for_timeout(3000)

        # Harvest slugs from any phone links already rendered
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            m = _FIND_RE.search(a["href"])
            if m:
                slug = m.group(1)
                if slug not in seen_slugs and _SLUG_VAL.match(slug):
                    seen_slugs.add(slug)
                    slugs.append(slug)

        log.info("  Harvested %d slugs from rendered listing HTML", len(slugs))

        # Log any captured XHR URLs for future debugging
        if harvested_urls:
            log.info("  Captured XHR URLs: %s", harvested_urls[:5])

    except Exception as exc:
        log.warning("  Listing/API fallback error: %s", exc)
    finally:
        page.remove_listener("response", _on_response)

    return slugs


# ── STEP 2: Parse each price page ─────────────────────────────────────────────

def _parse_price_html(html, slug, url):
    """
    Parse the fully-rendered HTML of a price page.
    Returns a result dict or None.
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── Brand / Model ──────────────────────────────────────────────────────────
    brand = model = variant = ""
    h1 = soup.find("h1")
    if h1:
        raw = h1.get_text(strip=True)
        raw = re.sub(r"\s*[-–|]?\s*(price|where to buy).*", "", raw, flags=re.I).strip()
        parts = raw.split(None, 1)
        brand = parts[0] if parts else ""
        model = parts[1] if len(parts) > 1 else ""

    h2 = soup.find("h2")
    if h2:
        v = h2.get_text(strip=True)
        if re.search(r"\d+\s*(GB|TB|MB)", v, re.I):
            variant = v

    # ── Rating ─────────────────────────────────────────────────────────────────
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

    # ── Prices by country ──────────────────────────────────────────────────────
    prices, seen_ctries = [], set()

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

        price_local = price_usd = currency = ""
        for cell in cells[1:]:
            m_price = re.search(r"[\d,]+\.?\d*", cell)
            m_curr  = re.search(r"\b([A-Z]{3})\b|([€£$¥₹₩₺₴₦])", cell)
            if m_price:
                price_local = m_price.group().replace(",", "")
                if m_curr:
                    currency = next(g for g in m_curr.groups() if g)
                break

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

    if not prices:
        log.warning("No prices found for %s", slug)

    return {
        "brand":       brand,
        "model":       model,
        "variant":     variant,
        "rating":      rating,
        "num_reviews": num_reviews,
        "prices":      prices,
        "phone_url":   url,
    }


async def scrape_price_page(page, slug):
    url  = f"{BASE}/en/where-to-buy-and-price/{slug}"
    html = await _fetch_text(page, url, wait_selector="table, [class*='price']")
    if not html:
        return None
    return _parse_price_html(html, slug, url)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    fieldnames = [
        "brand", "model", "variant", "rating", "num_reviews",
        "country", "price_usd", "currency", "price_local", "phone_url",
    ]

    # Resume support
    done         = set()
    write_header = not OUT_FILE.exists()
    if OUT_FILE.exists():
        with open(OUT_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add((row.get("phone_url", ""), row.get("country", "")))
        log.info("Resuming — %d rows already saved", len(done))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",  # hide automation flag
            ],
        )

        # One persistent browser context mimics a real user profile
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            java_script_enabled=True,
        )

        # Mask Playwright's navigator.webdriver fingerprint
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
        """)

        # Reuse a single page for all requests (keeps cookies/session alive)
        page = await context.new_page()

        try:
            # Pass the page so the fallback listing-scraper can reuse it
            slugs = await get_all_price_slugs(context, page)
            if not slugs:
                log.error("No slugs found — check sitemap URLs.")
                return

            out_fh = open(OUT_FILE, "a", newline="", encoding="utf-8")
            writer = csv.DictWriter(out_fh, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()

            try:
                for i, slug in enumerate(slugs, 1):
                    log.info("[%d/%d] %s", i, len(slugs), slug)
                    data = await scrape_price_page(page, slug)
                    await _rand_sleep(page)

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

        finally:
            await page.close()
            await context.close()
            await browser.close()

    log.info("Done. Output: %s", OUT_FILE)


if __name__ == "__main__":
    asyncio.run(main())
