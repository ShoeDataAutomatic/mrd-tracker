# =============================================================================
# MRD Trend Tracker — Configuration
# =============================================================================
# Edit this file to add retailers, adjust scoring weights, and configure
# email / Google Sheets output.

# -----------------------------------------------------------------------------
# Retailers
# -----------------------------------------------------------------------------
# Each retailer entry defines the base URL, footwear category paths, and
# the CSS selectors used to parse their pages.
#
# IMPORTANT: CSS selectors may need updating if the retailer redesigns their
# site. Run `python run.py --discover primark` to print raw page HTML for
# inspection and selector tuning.
# -----------------------------------------------------------------------------

RETAILERS = {
    'primark': {
        'name':     'Primark',
        'base_url': 'https://www.primark.com',
        'region':   'en-gb',
        'categories': [
            '/en-gb/c/women/shoes',
            '/en-gb/c/men/shoes',
            '/en-gb/c/kids/shoes',
        ],
        'enabled': True,

        # Primark uses a persisted GraphQL API — no browser scraping needed.
        # If products stop appearing, the query_hash has likely changed.
        # Re-run the network capture script (see README) and update the hash below.
        'api': {
            'endpoint':   'https://api001-arh.primark.com/bff-cae-green',
            'query_hash': '3cd7900849277a5c5a81004be10d0a0a073b05fd2200c8b6c52aa3b20d86c744',
            'page_size':  24,
        },
    },
    # Add more retailers here following the same pattern, e.g.:
    # 'asos': { ... },
    # 'hm': { ... },
}

# -----------------------------------------------------------------------------
# Scoring weights
# -----------------------------------------------------------------------------
# Adjust these to tune what signals matter most in your rankings.
# Scores accumulate over the rolling window (ROLLING_WINDOW_DAYS).
# -----------------------------------------------------------------------------

SCORING = {
    'new_arrival':        4,    # Product seen for the first time
    'rank_improvement':   2,    # Moved up in category page position (per 5 places)
    'long_runner':        3,    # Still present after 14+ days (Primark cycles fast)
    'featured':           5,    # Appeared in featured/promoted slot
    'restock_event':      5,    # Restocked after being OOS (e-com retailers only)
    'size_sold_out':      3,    # Per size that went OOS since last check
    'review_velocity':    2,    # Review count grew by >10% since last check
    'price_markdown':    -4,    # Price dropped (potential slow mover clearance)
    'product_removed':   -3,    # Disappeared from the site entirely
}

ROLLING_WINDOW_DAYS = 30   # Days to sum scores over for rankings

# -----------------------------------------------------------------------------
# Scrape schedule
# -----------------------------------------------------------------------------

SCRAPE_TIME = '07:30'       # Daily scrape time (24hr, server local time)
REPORT_DAY  = 'monday'      # Day to send weekly email digest
REPORT_TIME = '08:30'       # Time to send weekly email digest

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------

DATABASE_PATH = 'mrd_tracker.db'

# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------

DASHBOARD_HOST = '0.0.0.0'
DASHBOARD_PORT = 5000
DASHBOARD_DEBUG = False      # Set True during development only

# -----------------------------------------------------------------------------
# Email digest
# -----------------------------------------------------------------------------
# Sensitive values are read from environment variables so they are never
# hardcoded in the repo. Set them as GitHub Actions secrets and Railway
# environment variables (see README for instructions).
#
# To use locally, either set the env vars in your shell or create a .env
# file and run: pip install python-dotenv, then add the two lines below
# to the top of run.py:
#   from dotenv import load_dotenv; load_dotenv()
# -----------------------------------------------------------------------------

import os

EMAIL = {
    'enabled':      bool(os.environ.get('EMAIL_USERNAME')),
    'smtp_host':    os.environ.get('EMAIL_SMTP_HOST', 'smtp.gmail.com'),
    'smtp_port':    int(os.environ.get('EMAIL_SMTP_PORT', 587)),
    'use_tls':      True,
    'username':     os.environ.get('EMAIL_USERNAME', ''),
    'password':     os.environ.get('EMAIL_PASSWORD', ''),
    'from_address': os.environ.get('EMAIL_FROM', ''),
    'to_addresses': [a.strip() for a in os.environ.get('EMAIL_TO', '').split(',') if a.strip()],
    'top_n':        20,
}

# -----------------------------------------------------------------------------
# Google Sheets
# -----------------------------------------------------------------------------
# GOOGLE_CREDENTIALS_JSON should contain the full contents of your service
# account JSON file as a single-line string (set as a GitHub/Railway secret).
# The credentials are written to a temp file at runtime.
# -----------------------------------------------------------------------------

import json, tempfile

_gcp_json = os.environ.get('GOOGLE_CREDENTIALS_JSON', '')
_creds_file = 'credentials.json'   # Fallback for local use

if _gcp_json:
    try:
        _tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        _tmp.write(_gcp_json)
        _tmp.flush()
        _creds_file = _tmp.name
    except Exception:
        pass

SHEETS = {
    'enabled':          bool(_gcp_json) or os.path.exists('credentials.json'),
    'credentials_file': _creds_file,
    'spreadsheet_name': os.environ.get('SHEETS_NAME', 'MRD Trend Tracker'),
    'worksheet_name':   'Rankings',
    'top_n':            50,
}
