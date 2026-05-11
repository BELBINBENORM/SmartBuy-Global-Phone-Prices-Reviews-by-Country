# 📱 SmartBuy: Global Phone Prices & Reviews by Country

[![Update Kaggle Dataset](https://github.com/YOUR_GITHUB_USERNAME/smartbuy-global-phone-prices-reviews/actions/workflows/update-dataset.yml/badge.svg)](https://github.com/YOUR_GITHUB_USERNAME/smartbuy-global-phone-prices-reviews/actions/workflows/update-dataset.yml)

Retail prices across 50+ countries × expert & user review scores — for **every currently-buyable smartphone**, per model, per brand. **Auto-updated monthly via GitHub Actions** and pushed directly to Kaggle.

Only phones with an **Available** status on GSMArena are included — discontinued and cancelled models are skipped so the dataset reflects what you can actually buy today.

---

## 📦 Output

| File | Description |
|------|-------------|
| `phones.csv` | One row per phone × country — prices, review scores, opinion count, and launch year |

### Columns

| Column | Source | Description |
|--------|--------|-------------|
| `brand` | GSMArena | Phone manufacturer (e.g. Samsung, Apple) |
| `model` | GSMArena | Phone model name (e.g. Galaxy S25 Ultra) |
| `availability` | GSMArena | Status: Available / Discontinued / Coming soon |
| `date_launched` | GSMArena | Year the phone was announced |
| `gsmarena_score` | GSMArena | Expert review score 0–100 (empty if unreviewed) |
| `user_rating` | GSMArena | User rating out of 10 |
| `num_opinions` | GSMArena | Number of user votes behind `user_rating` |
| `country` | Kimovil | Country where price was scraped |
| `price_usd` | Kimovil | Retail price in USD |
| `currency` | Kimovil | Local currency code |
| `price_local` | Kimovil | Retail price in local currency |
| `phone_url` | GSMArena | Source detail page URL |

---

## 📡 Data Sources

- **GSMArena** — full phone catalogue, specs (launch year, status), expert scores, user ratings + vote counts
- **Kimovil** — retail prices per country (50+ countries) via `/en/where-to-buy-and-price/`

---

## 🚀 Setup

### 1. Add Kaggle secrets to GitHub

**Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|--------|-------|
| `KAGGLE_USERNAME` | Your Kaggle username |
| `KAGGLE_KEY` | Your Kaggle API key (kaggle.com → Account → API) |

### 2. Update `dataset-metadata.json`

Replace `YOUR_KAGGLE_USERNAME` with your real Kaggle username.

### 3. First run

**Actions → Update Kaggle Dataset → Run workflow**

The dataset is created on Kaggle automatically. Every 1st of the month, a new version is published with fresh data.

### Run locally

```bash
pip install -r requirements.txt
python scraper.py
```

---

## ⚙️ Configuration

Edit the top of `scraper.py`:

```python
# None = auto-discover ALL brands from GSMArena (recommended)
# Or set a list to scrape only specific brands:
# BRANDS = ["Samsung", "Apple", "Google"]
BRANDS           = None

PHONES_PER_BRAND = None     # None = all phones (recommended); int to limit per brand
AVAILABLE_ONLY   = True     # True = skip Discontinued / Cancelled phones
SLEEP_MIN        = 1.5      # seconds between requests
```

---

## 📄 License

CC BY-NC-SA 4.0 — data sourced from publicly available pages on GSMArena and Kimovil.
