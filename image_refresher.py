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
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    to_download = []

    # One query upfront — avoid per-product round-trips
    existing_blob_ids = db.get_product_ids_with_blobs(retailer=key)

    for p in products:
        image_url = p.get('image_url')
        if image_url:
            product_id = db.update_product_image(
                retailer  = key,
                sku       = p['sku'],
                image_url = image_url,
            )
            if product_id and product_id not in existing_blob_ids:
                to_download.append((product_id, image_url))
            updated += 1
        else:
            missing += 1

    if missing:
        logger.warning(
            f'[image-refresh] {key}: {missing} products had no image URL in scrape results. '
            f'Their existing URLs were not changed.'
        )

    # Download and cache blobs for products that don't have one yet
    if to_download:
        logger.info(f'[image-refresh] {key}: downloading {len(to_download)} new image blobs…')
        blobs_saved = 0
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(_download_image, url): pid
                for pid, url in to_download
            }
            for future in as_completed(futures):
                pid = futures[future]
                data, content_type = future.result()
                if data:
                    db.save_image_blob(pid, data, content_type)
                    blobs_saved += 1
        logger.info(f'[image-refresh] {key}: cached {blobs_saved}/{len(to_download)} blobs.')

    return updated


# ---------------------------------------------------------------------------
# Image download helper
# ---------------------------------------------------------------------------

def _download_image(url):
    """Download an image URL. Returns (bytes, content_type) or (None, None)."""
    try:
        resp = requests.get(
            url, timeout=10,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; MRDTracker/1.0)'}
        )
        if resp.status_code == 200:
            ct = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
            return resp.content, ct
    except Exception as e:
        logger.debug(f'[image-refresh] Failed to download {url}: {e}')
    return None, None


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
