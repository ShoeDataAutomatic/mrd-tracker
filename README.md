# MRD Trend Tracker

Automated footwear trend intelligence. Monitors retailer websites daily,
detects signals of strong-selling styles, and surfaces ranked product lists
via a web dashboard, weekly email digest, and Google Sheets.

---

## What it tracks

| Signal | Description |
|--------|-------------|
| New arrival | Product appeared for the first time |
| Long runner | Still present after 14+ days (Primark cycles fast) |
| Featured | Placed in top-4 / promoted position on category page |
| Rising | Moved up in category page rank |
| Restocked | Went OOS then came back (e-com retailers) |
| Selling through | Sizes dropping out of stock |
| Review spike | Review count grew >10% since last check |
| Marked down | Price reduced — potential slow-mover signal |
| Removed | No longer visible on the site |

> **Primark note:** Primark does not support online purchasing in most
> markets, so size/stock signals are not available. The most reliable
> signals for Primark are: **New arrival**, **Long runner**, **Featured**,
> and **Rising**. A product that remains prominently placed on Primark's
> website for 2+ weeks is almost certainly a sustained seller.

---

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Initialise the database

```bash
python run.py --init
```

### 3. Tune the Primark selectors

Primark's website is a React app. Before the first real scrape, run
discovery mode to inspect the live page HTML and confirm the CSS selectors
in `config.py` are correct:

```bash
python run.py --discover primark > primark_page.html
```

Open `primark_page.html` in a browser, inspect the product grid, and update
`RETAILERS['primark']['selectors']` in `config.py` if needed.

Key selectors to verify:
- `product_card` — the repeating element for each product in the grid
- `product_name` — the product title inside the card
- `product_price` — the price element
- `product_link` — the `<a>` tag linking to the product page

### 4. Run your first scrape

```bash
python run.py --scrape
python run.py --score
```

### 5. Open the dashboard

```bash
python run.py --dashboard
```

Then go to http://localhost:5000 in your browser.

---

## Running automatically (daily)

### Option A — Start the built-in scheduler

```bash
python run.py
```

This runs in the foreground. It will scrape at the time set in `config.py`
(`SCRAPE_TIME`, default 07:30) and send the email digest each Monday.

To keep it running permanently on a server, use `screen`, `tmux`, or a
process manager like `pm2` or `supervisor`.

### Option B — Use system cron

Add to your crontab (`crontab -e`):

```
# Scrape and score daily at 7:30am
30 7 * * * cd /path/to/mrd-tracker && python run.py --scrape && python run.py --score && python run.py --sheets

# Send email digest every Monday at 8:30am
30 8 * * 1 cd /path/to/mrd-tracker && python run.py --email
```

---

## Email digest setup

1. Set `EMAIL['enabled'] = True` in `config.py`
2. Fill in your SMTP credentials
3. For Gmail: use an **App Password** (not your Gmail password)
   - Go to myaccount.google.com → Security → App passwords

Test it manually:
```bash
python run.py --email
```

---

## Google Sheets setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → Enable **Google Sheets API** and **Google Drive API**
3. Create a **Service Account** → Download the JSON key
4. Save the key as `credentials.json` in the project root
5. Create a Google Sheet named exactly as set in `SHEETS['spreadsheet_name']`
6. Share the sheet with the service account email (found in credentials.json)
7. Set `SHEETS['enabled'] = True` in `config.py`

Test it:
```bash
python run.py --sheets
```

---

## Adding a new retailer

1. Create `scrapers/newretailer.py` — subclass `BaseScraper`, implement
   `scrape_category()` and `scrape_product()`
2. Add an entry to `RETAILERS` in `config.py` with the retailer's base URL,
   category paths, and CSS selectors
3. Register the scraper in `scrapers/__init__.py`

---

## Configuration reference (`config.py`)

| Setting | Default | Description |
|---------|---------|-------------|
| `SCRAPE_TIME` | `'07:30'` | Daily scrape time (24hr) |
| `REPORT_DAY` | `'monday'` | Day for weekly email digest |
| `ROLLING_WINDOW_DAYS` | `30` | Days to sum scores over |
| `DASHBOARD_PORT` | `5000` | Web dashboard port |
| `SCORING` | (see file) | Signal point values |

---

## File structure

```
mrd-tracker/
├── config.py                  All settings
├── database.py                SQLite setup and queries
├── scorer.py                  Change detection and scoring
├── run.py                     CLI entry point and scheduler
├── requirements.txt
├── scrapers/
│   ├── base.py                Abstract base scraper
│   └── primark.py             Primark-specific scraper
├── notifications/
│   ├── email_digest.py        Weekly HTML email
│   └── sheets.py              Google Sheets sync
└── dashboard/
    ├── app.py                 Flask app
    └── templates/
        └── index.html         Dashboard UI
```
