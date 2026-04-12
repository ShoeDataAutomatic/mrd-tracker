"""
run.py — Main entry point for MRD Trend Tracker.

Usage:
  python run.py                  Start the scheduler (scrapes daily, sends weekly digest)
  python run.py --scrape         Run a single scrape now
  python run.py --score          Run scoring now (after a scrape)
  python run.py --email          Send the email digest now
  python run.py --sheets         Sync to Google Sheets now
  python run.py --dashboard      Start the web dashboard
  python run.py --discover       Print raw HTML for selector debugging
  python run.py --init           Initialise the database only
"""

import sys
import logging
import argparse
from datetime import datetime

# ---- Logging setup ----
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def run_scrape():
    """Scrape all enabled retailers and save to the database."""
    from config import RETAILERS
    from scrapers import get_scraper
    import database as db

    db.init_db()
    logger.info('=== Scrape run started ===')

    for key, config in RETAILERS.items():
        if not config.get('enabled'):
            continue

        logger.info(f'--- {config["name"]} ---')
        scraper  = get_scraper(key, config)
        products = scraper.scrape_all()

        if not products:
            logger.warning(f'No products returned for {key}.')
            continue

        for p in products:
            product_id = db.upsert_product(
                retailer  = key,
                sku       = p['sku'],
                name      = p['name'],
                url       = p['url'],
                category  = p.get('category'),
                image_url = p.get('image_url'),
            )
            db.save_snapshot(
                product_id      = product_id,
                price           = p.get('price'),
                rank            = p.get('rank'),
                review_count    = p.get('review_count'),
                sizes_available = p.get('sizes_available', []),
                sizes_oos       = p.get('sizes_oos', []),
                is_featured     = p.get('is_featured', False),
            )

        logger.info(f'Saved {len(products)} products for {key}.')

    logger.info('=== Scrape run complete ===')


def run_score():
    """Score all products based on today's snapshots."""
    import database as db
    import scorer
    db.init_db()
    logger.info('=== Scoring started ===')
    scorer.run_scoring()
    logger.info('=== Scoring complete ===')


def run_email():
    """Send the weekly email digest now."""
    from notifications.email_digest import send_digest
    logger.info('=== Sending email digest ===')
    send_digest()


def run_sheets():
    """Sync rankings to Google Sheets now."""
    from notifications.sheets import sync_to_sheets
    logger.info('=== Syncing to Google Sheets ===')
    sync_to_sheets()


def run_dashboard():
    """Start the Flask web dashboard."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'dashboard'))
    from dashboard.app import app
    from config import DASHBOARD_HOST, DASHBOARD_PORT
    logger.info(f'=== Dashboard starting at http://{DASHBOARD_HOST}:{DASHBOARD_PORT} ===')
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT)


def run_discover(retailer_key='primark'):
    """Print raw HTML for a retailer's category page to help tune selectors."""
    from config import RETAILERS
    from scrapers import get_scraper
    config = RETAILERS.get(retailer_key)
    if not config:
        logger.error(f'Unknown retailer: {retailer_key}')
        return
    scraper = get_scraper(retailer_key, config)
    if hasattr(scraper, 'discover'):
        scraper.discover()
    else:
        logger.error(f'Scraper for {retailer_key} does not support discovery mode.')


def run_refresh_images(retailer=None):
    """Re-scrape category pages to get fresh CDN image URLs."""
    from image_refresher import refresh_images, log_stale_summary
    import database as db
    db.init_db()
    logger.info('=== Image URL refresh started ===')
    log_stale_summary(retailer=retailer)
    refresh_images(retailer=retailer)
    logger.info('=== Image URL refresh complete ===')


def full_run():
    """Scrape, refresh images, score, send email digest, and sync sheets."""
    run_scrape()
    run_refresh_images()   # Refresh image URLs after scrape while pages are warm
    run_score()
    run_email()
    run_sheets()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def start_scheduler():
    """
    Start the daily scheduler.
    Scrapes and scores each day at SCRAPE_TIME.
    Sends the digest and syncs sheets on REPORT_DAY at REPORT_TIME.
    """
    try:
        import schedule
    except ImportError:
        logger.error('schedule not installed. Run: pip install schedule')
        sys.exit(1)

    import time
    from config import SCRAPE_TIME, REPORT_DAY, REPORT_TIME

    import database as db
    db.init_db()

    def daily_job():
        logger.info('=== Daily job triggered ===')
        run_scrape()
        run_refresh_images()   # Refresh image URLs immediately after scrape
        run_score()
        run_sheets()

    def weekly_job():
        logger.info('=== Weekly digest job triggered ===')
        run_email()

    schedule.every().day.at(SCRAPE_TIME).do(daily_job)
    getattr(schedule.every(), REPORT_DAY).at(REPORT_TIME).do(weekly_job)

    logger.info(f'Scheduler started. Scraping daily at {SCRAPE_TIME}.')
    logger.info(f'Email digest every {REPORT_DAY.title()} at {REPORT_TIME}.')
    logger.info('Press Ctrl+C to stop.\n')

    while True:
        schedule.run_pending()
        time.sleep(30)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='MRD Trend Tracker')
    parser.add_argument('--scrape',          action='store_true', help='Run scrape now')
    parser.add_argument('--refresh-images',  action='store_true', help='Refresh image URLs now')
    parser.add_argument('--score',           action='store_true', help='Run scoring now')
    parser.add_argument('--email',           action='store_true', help='Send email digest now')
    parser.add_argument('--sheets',          action='store_true', help='Sync to Google Sheets now')
    parser.add_argument('--dashboard',       action='store_true', help='Start the web dashboard')
    parser.add_argument('--discover',  nargs='?', const='primark', metavar='RETAILER',
                        help='Print raw HTML for selector debugging (default: primark)')
    parser.add_argument('--init',            action='store_true', help='Initialise the database only')
    args = parser.parse_args()

    if   args.init:                          import database as db; db.init_db()
    elif args.scrape:                        run_scrape()
    elif getattr(args, 'refresh_images'):    run_refresh_images()
    elif args.score:                         run_score()
    elif args.email:                         run_email()
    elif args.sheets:                        run_sheets()
    elif args.dashboard:                     run_dashboard()
    elif args.discover:                      run_discover(args.discover)
    else:                                    start_scheduler()
