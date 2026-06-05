"""
scorer.py — Change detection and product scoring engine.

Called after each scrape run. For each product:
  1. Compare latest snapshot to the previous one
  2. Detect signal events (rank change, new arrival, etc.)
  3. Calculate today's score and write it to the scores table
"""

import json
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
    if first_seen and (today - first_seen).days <= 7:
        signals['new_arrival'] = SCORING['new_arrival']
        score += SCORING['new_arrival']

    # ------------------------------------------------------------------
    # 2. Long runner
    #    Primark cycles their online catalog quickly, so a product that
    #    has been present for 30+ days is likely a sustained performer.
    #    Not awarded if the product has since been removed.
    # ------------------------------------------------------------------
    is_removed = last_seen and (today - last_seen).days >= 1
    if first_seen and (today - first_seen).days >= 30 and not is_removed:
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
    if is_removed:
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
        # Detect via snapshot price comparison OR via the scraper's raw_data flag
        # (catches products that arrived already marked down)
        prev_price = previous.get('price')
        curr_price = latest.get('price')
        try:
            raw = json.loads(latest.get('raw_data') or '{}')
        except Exception:
            raw = {}
        is_markdown_flag = raw.get('is_markdown', False)
        was_price        = raw.get('was_price')

        price_dropped = prev_price and curr_price and curr_price < prev_price
        if price_dropped or is_markdown_flag:
            signals['price_markdown'] = {
                'score':     SCORING['price_markdown'],
                'was_price': was_price or (prev_price if price_dropped else None),
            }
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

        # Extract was_price from the latest snapshot's raw_data blob
        try:
            raw = json.loads(row.get('latest_raw_data') or '{}')
            row['was_price']   = raw.get('was_price')
            row['is_markdown'] = raw.get('is_markdown', False)
        except Exception:
            row['was_price']   = None
            row['is_markdown'] = False
        row.pop('latest_raw_data', None)  # Keep response lean

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


def get_markdown_analysis(retailer=None):
    """
    Return all currently marked-down products with a classification of WHY
    they are marked down: poor_seller, end_of_season, or size_clearance.
    """
    today    = date.today()
    products = db.get_all_products(retailer=retailer)
    results  = []

    SEASONAL_SUBCATS = {
        'boots', 'sandals', 'flip flops and sliders', 'sandals and sliders',
        'sandals and flipflops', 'sandals and flip flops', 'slippers', 'clogs',
    }

    for product in products:
        pid      = product['id']
        history  = db.get_score_history(pid, days=180)
        if not history:
            continue

        # Only process products with an active price_markdown signal
        latest_signals = history[-1].get('signals', {})
        if 'price_markdown' not in latest_signals:
            continue

        latest_snap = db.get_latest_snapshot(pid)
        if not latest_snap:
            continue

        try:
            raw       = json.loads(latest_snap.get('raw_data') or '{}')
            was_price = raw.get('was_price')
        except Exception:
            was_price = None

        curr_price = latest_snap.get('price')
        if not was_price or not curr_price or was_price <= curr_price:
            continue

        markdown_depth_pct = round((1 - curr_price / was_price) * 100)

        # Score series analysis
        scores    = [h['score'] for h in history]
        peak      = max(scores) if scores else 0
        avg       = sum(scores) / len(scores) if scores else 0

        # Day within product life when markdown first appeared
        first_seen    = _parse_date(product.get('first_seen', ''))
        markdown_day  = None
        for h in history:
            if 'price_markdown' in h.get('signals', {}):
                md_date = _parse_date(h['scored_date'])
                if md_date and first_seen:
                    markdown_day = (md_date - first_seen).days
                break

        # Collect all historical signal tags
        all_tags = set()
        for h in history:
            all_tags.update(_signals_to_tags(h.get('signals', {})))

        was_featured    = 'Featured'     in all_tags
        was_rising      = 'Rising'       in all_tags
        was_long_runner = 'Long runner'  in all_tags
        age             = (today - first_seen).days if first_seen else 0
        sub             = (product.get('subcategory') or '').lower()

        # ── Classification scoring ──────────────────────────────────────
        poor_score    = 0
        season_score  = 0
        clear_score   = 0

        # Poor seller
        if avg < 15:                     poor_score += 4
        elif avg < 30:                   poor_score += 2
        if peak < 20:                    poor_score += 3
        elif peak < 40:                  poor_score += 1
        if not was_featured:             poor_score += 2
        if not was_rising:               poor_score += 1
        if markdown_depth_pct > 30:      poor_score += 1

        # End of season
        if age > 120:                    season_score += 3
        elif age > 90:                   season_score += 2
        elif age > 60:                   season_score += 1
        if sub in SEASONAL_SUBCATS:      season_score += 3
        if peak > 25:                    season_score += 1   # was selling reasonably

        # Size clearance
        if peak > 60:                    clear_score += 4
        elif peak > 40:                  clear_score += 3
        elif peak > 20:                  clear_score += 1
        if was_featured:                 clear_score += 2
        if was_rising:                   clear_score += 2
        if was_long_runner:              clear_score += 1

        scores_map = {
            'poor_seller':   poor_score,
            'end_of_season': season_score,
            'size_clearance': clear_score,
        }
        reason        = max(scores_map, key=scores_map.get)
        sorted_vals   = sorted(scores_map.values(), reverse=True)
        gap           = sorted_vals[0] - sorted_vals[1]
        confidence    = 'high' if gap >= 3 else 'medium' if gap >= 1 else 'low'

        results.append({
            'id':                 pid,
            'name':               product.get('name'),
            'retailer':           product.get('retailer'),
            'category':           product.get('category'),
            'subcategory':        product.get('subcategory'),
            'url':                product.get('url'),
            'image_url':          product.get('image_url'),
            'latest_price':       curr_price,
            'was_price':          was_price,
            'markdown_depth_pct': markdown_depth_pct,
            'markdown_day':       markdown_day,
            'age':                age,
            'reason':             reason,
            'confidence':         confidence,
            'peak_score':         round(peak),
            'avg_score':          round(avg, 1),
            'score_series':       [{'date': h['scored_date'], 'score': h['score']} for h in history[-14:]],
            'was_featured':       was_featured,
            'was_rising':         was_rising,
            'was_long_runner':    was_long_runner,
            'signal_history':     sorted(all_tags),
        })

    results.sort(key=lambda x: x['markdown_depth_pct'], reverse=True)
    return results


def _parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).date()
    except (ValueError, TypeError):
        return None
