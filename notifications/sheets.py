"""
notifications/sheets.py — Sync rankings to a Google Sheet.

Setup:
  1. Go to console.cloud.google.com
  2. Create a project, enable Google Sheets API and Google Drive API
  3. Create a Service Account, download the JSON key file
  4. Save the key file as credentials.json in the project root
  5. Share your Google Sheet with the service account email address
  6. Set SHEETS['enabled'] = True and fill in SHEETS['spreadsheet_name'] in config.py

Dependencies:
  pip install gspread google-auth
"""

import logging
from datetime import date

from config import SHEETS, ROLLING_WINDOW_DAYS
import scorer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def sync_to_sheets(retailer=None):
    if not SHEETS.get('enabled'):
        logger.info('[sheets] Google Sheets sync disabled in config.')
        return

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        logger.error('[sheets] gspread not installed. Run: pip install gspread google-auth')
        return

    creds_file = SHEETS.get('credentials_file', 'credentials.json')
    try:
        creds = Credentials.from_service_account_file(
            creds_file,
            scopes=[
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive',
            ],
        )
        client = gspread.authorize(creds)
    except Exception as e:
        logger.error(f'[sheets] Auth failed: {e}')
        return

    try:
        spreadsheet = client.open(SHEETS['spreadsheet_name'])
    except gspread.SpreadsheetNotFound:
        logger.error(
            f'[sheets] Spreadsheet "{SHEETS["spreadsheet_name"]}" not found. '
            'Make sure it exists and is shared with the service account.'
        )
        return

    worksheet_name = SHEETS.get('worksheet_name', 'Rankings')
    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=200, cols=12)
        logger.info(f'[sheets] Created worksheet: {worksheet_name}')

    top_n    = SHEETS.get('top_n', 50)
    products = scorer.get_rankings(limit=top_n, retailer=retailer)

    if not products:
        logger.info('[sheets] No products to sync.')
        return

    rows = _build_rows(products)

    # Clear and rewrite
    worksheet.clear()
    worksheet.update('A1', rows, value_input_option='RAW')

    logger.info(f'[sheets] Synced {len(products)} products to "{SHEETS["spreadsheet_name"]}" / "{worksheet_name}"')


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------

def _build_rows(products):
    today    = date.today().strftime('%d %b %Y')
    header   = [
        'Rank',
        'Image',
        'Product name',
        'Category',
        'Retailer',
        'Score (30d)',
        'Latest price',
        'Page rank',
        'Days tracked',
        'Signals',
        'URL',
        f'Updated {today}',
    ]
    rows = [header]

    for i, p in enumerate(products, start=1):
        price_str  = f'£{p["latest_price"]:.2f}' if p.get('latest_price') else ''
        rank_str   = str(p['latest_rank']) if p.get('latest_rank') else ''
        tags_str   = ', '.join(p.get('signal_tags', []))

        # Use Google Sheets IMAGE() formula to render the product photo inline.
        # The cell needs to have row height increased manually (~80px) to see it.
        image_formula = f'=IMAGE("{p["image_url"]}", 4, 60, 60)' if p.get('image_url') else ''

        rows.append([
            i,
            image_formula,
            p.get('name', ''),
            p.get('category', '').replace('-', ' ').title(),
            p.get('retailer', '').title(),
            round(p.get('total_score', 0), 1),
            price_str,
            rank_str,
            p.get('days_tracked', 0),
            tags_str,
            p.get('url', ''),
            '',
        ])

    return rows
