"""
scorer.py — Change detection and product scoring engine.

Called after each scrape run. For each product:
  1. Compare latest snapshot to the previous one
  2. Detect signal events (rank change, new arrival, etc.)
  3. Calculate today's score and write it to the scores table
"""

import logging
from datetime import datetime, date, timedelta

import database as db
from config import SCORING, ROLLING_WINDOW_DAYS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_scoring(retailer=None):
    """
    Score all products (optionally filtered by retailer).
    Should be called once after each scrape run completes.
    """
    products = db.get_all_products(retailer=retailer)
    if not products:
        logger.info('[scorer] No products to score.')
        return

    scored   = 0
    skipped  = 0
    today    = date.today().isoformat()

    for product in products:
        pid   = product['id']
        score, signals = _score_product(product)

        if score is not None:
            db.save_score(pid, score, signals)
            scored += 1
        else:
            skipped += 1

    logger.info(f'[scorer] Done. Scored: {scored}, skipped (no snapshots): {skipped}')


# ---------------------------------------------------------------------------
# Per-product scoring
# ---------------------------------------------------------------------------

def _score_product(product):
    """
    Return (score, signals_dict) for today, or (None, {}) if no data.
    """
    pid      = product['id']
    latest   = db.get_latest_snapshot(pid)
    previous = db.get_previous_snapshot(pid)

    if not latest:
        return None, {}

    signals = {}
    score   = 0.0

    today     = date.today()
    first_seen = _parse_date(product.get('first_seen', ''))
    last_seen  = _parse_date(product.get('last_seen', ''))

    # ------------------------------------------------------------------
    # 1. New arrival
    # ------------------------------------------------------------------
    if first_seen and (today - first_seen).days <= 1:
        signals['new_arrival'] = SCORING['new_arrival']
        score += SCORING['new_arrival']

    # ------------------------------------------------------------------
    # 2. Long runner
    #    Primark cycles their online catalog quickly, so a product that
    #    has been present for 14+ days is likely a sustained performer.
    # ------------------------------------------------------------------
    if first_seen and (today - first_seen).days >= 14:
        signals['long_runner'] = SCORING['long_runner']
        score += SCORING['long_runner']

    # ------------------------------------------------------------------
    # 3. Featured placement
    # ------------------------------------------------------------------
    if latest.get('is_featured'):
        signals['featured'] = SCORING['featured']
        score += SCORING['featured']

    # ------------------------------------------------------------------
    # 4. Product removed (last_seen is not today)
    # ------------------------------------------------------------------
    if last_seen and (today - last_seen).days >= 1:
        signals['product_removed'] = SCORING['product_removed']
        score += SCORING['product_removed']

    # ------------------------------------------------------------------
    # Signals that require a previous snapshot for comparison
    # ------------------------------------------------------------------
    if previous:

        # 5. Rank improvement
        prev_rank = previous.get('rank')
        curr_rank = latest.get('rank')
        if prev_rank and curr_rank and curr_rank < prev_rank:
            improvement = prev_rank - curr_rank
            rank_score  = (improvement // 5) * SCORING['rank_improvement']
            if rank_score > 0:
                signals['rank_improvement'] = rank_score
                score += rank_score

        # 6. Size sell-through (e-com retailers only; Primark usually has empty arrays)
        prev_available = set(previous.get('sizes_available') or [])
        curr_available = set(latest.get('sizes_available') or [])
        sizes_gone     = prev_available - curr_available
        if sizes_gone:
            oos_score = len(sizes_gone) * SCORING['size_sold_out']
            signals['sizes_sold_out'] = {'sizes': list(sizes_gone), 'score': oos_score}
            score += oos_score

        # 7. Restock event (size present again after being OOS)
        prev_oos = set(previous.get('sizes_oos') or [])
        curr_oos = set(latest.get('sizes_oos') or [])
        restocked = prev_oos - curr_oos
        if restocked:
            rs_score = SCORING['restock_event']
            signals['restock_event'] = {'sizes': list(restocked), 'score': rs_score}
            score += rs_score

        # 8. Review velocity
        prev_reviews = previous.get('review_count') or 0
        curr_reviews = latest.get('review_count') or 0
        if prev_reviews and curr_reviews > prev_reviews:
            growth_pct = (curr_reviews - prev_reviews) / prev_reviews
            if growth_pct >= 0.10:   # 10% growth in review count
                signals['review_velocity'] = SCORING['review_velocity']
                score += SCORING['review_velocity']

        # 9. Price markdown
        prev_price = previous.get('price')
        curr_price = latest.get('price')
        if prev_price and curr_price and curr_price < prev_price:
            signals['price_markdown'] = SCORING['price_markdown']
            score += SCORING['price_markdown']

    return round(score, 2), signals


# ---------------------------------------------------------------------------
# Rankings summary
# ---------------------------------------------------------------------------

def get_rankings(limit=50, days=None, retailer=None):
    """
    Return ranked products with their cumulative scores.
    Wrapper around database.get_top_products() with signal decoding.
    """
    days = days or ROLLING_WINDOW_DAYS
    rows = db.get_top_products(limit=limit, days=days, retailer=retailer)

    for row in rows:
        pid         = row['id']
        first_seen  = _parse_date(row.get('first_seen', ''))
        today       = date.today()

        row['days_tracked']  = (today - first_seen).days if first_seen else 0
        row['score_history'] = db.get_score_history(pid, days=days)

        # Build a human-readable signal summary for the most recent score
        history = row['score_history']
        if history:
            latest_signals = history[-1].get('signals', {})
            row['signal_tags'] = _signals_to_tags(latest_signals)
        else:
            row['signal_tags'] = []

    return rows


def _signals_to_tags(signals):
    """Convert a signals dict to a list of human-readable tag strings."""
    tags = []
    if 'new_arrival'      in signals: tags.append('New arrival')
    if 'long_runner'      in signals: tags.append('Long runner')
    if 'featured'         in signals: tags.append('Featured')
    if 'rank_improvement' in signals: tags.append('Rising')
    if 'restock_event'    in signals: tags.append('Restocked')
    if 'sizes_sold_out'   in signals: tags.append('Selling through')
    if 'review_velocity'  in signals: tags.append('Review spike')
    if 'price_markdown'   in signals: tags.append('Marked down')
    if 'product_removed'  in signals: tags.append('Removed')
    return tags


def _parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).date()
    except (ValueError, TypeError):
        return None
