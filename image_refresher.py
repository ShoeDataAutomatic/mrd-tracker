"""
image_refresher.py — Daily image URL refresh.

Retail CDNs sometimes sign image URLs with expiring tokens. This module
re-scrapes category pages once per day (after the main scrape) to capture
fresh image URLs for all active products, without creating new snapshots
or triggering re-scoring.

It uses the existing scraper infrastructure but only calls
db.update_product_image() — no snapshot, no score, no side effects.

Schedule:
  Runs automatically as part of the daily job in run.py.
  Can also be triggered manually: python run.py --refresh-images
"""

import logging
from config import RETAILERS
from scrapers import get_scraper
import database as db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def refresh_images(retailer=None):
    """
    Re-scrape category pages for all enabled retailers (or a single one)
    and update image URLs in the database.

    Returns the total number of image URLs updated.
    """
    target = {
        k: v for k, v in RETAILERS.items()
        if v.get('enabled') and (retailer is None or k == retailer)
    }

    if not target:
        logger.warning('[image-refresh] No matching enabled retailers found.')
        return 0

    total_updated = 0

    for key, config in target.items():
        logger.info(f'[image-refresh] Starting refresh for {config["name"]}')
        updated = _refresh_retailer(key, config)
        total_updated += updated
        logger.info(f'[image-refresh] {config["name"]}: {updated} image URLs refreshed.')

    logger.info(f'[image-refresh] Complete. Total refreshed: {total_updated}')
    return total_updated


# ---------------------------------------------------------------------------
# Per-retailer refresh
# ---------------------------------------------------------------------------

def _refresh_retailer(key, config):
    """
    Scrape category pages for one retailer and update image URLs.
    Returns the count of products updated.
    """
    try:
        scraper  = get_scraper(key, config)
        products = scraper.scrape_all()
    except Exception as e:
        logger.error(f'[image-refresh] Scrape failed for {key}: {e}')
        return 0

    if not products:
        logger.warning(f'[image-refresh] No products returned for {key}.')
        return 0

    updated = 0
    missing = 0

    for p in products:
        image_url = p.get('image_url')
        if image_url:
            db.update_product_image(
                retailer  = key,
                sku       = p['sku'],
                image_url = image_url,
            )
            updated += 1
        else:
            missing += 1

    if missing:
        logger.warning(
            f'[image-refresh] {key}: {missing} products had no image URL in scrape results. '
            f'Their existing URLs were not changed.'
        )

    return updated


# ---------------------------------------------------------------------------
# Stale-check utility
# ---------------------------------------------------------------------------

def log_stale_summary(retailer=None, stale_hours=20):
    """
    Log a summary of how many products have stale image URLs.
    Useful for diagnostics — call before refresh_images() to see what needs updating.
    """
    stale = db.get_stale_image_products(retailer=retailer, stale_hours=stale_hours)
    if stale:
        logger.info(
            f'[image-refresh] {len(stale)} products have image URLs older than '
            f'{stale_hours}h and will be refreshed.'
        )
    else:
        logger.info('[image-refresh] All image URLs are fresh — nothing to refresh.')
    return len(stale)
