"""
dashboard/app.py — Flask web dashboard.

Run with:  python dashboard/app.py
Or via:    python run.py --dashboard

Then open: http://localhost:5000
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import re
import json
from collections import Counter
from flask import Flask, render_template, jsonify, request
from config import DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_DEBUG, RETAILERS
import scorer
import database as db

# ---------------------------------------------------------------------------
# Keyword helpers
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    'a','an','the','and','or','in','on','with','for','to','of','at','by',
    'from','up','as','is','it','its','be','are','was','were','has','have',
    'had','do','does','did','but','not','no','so','if','this','that',
    'these','those','s','amp',
}

def _tokenise(name):
    """Return unigrams + bigrams from a product name, stop-words removed."""
    text  = name.lower().replace('-', ' ').replace("'s", '').replace("'", '')
    words = re.findall(r'[a-z]+', text)
    words = [w for w in words if w not in _STOP_WORDS and len(w) > 2]
    tokens = list(words)
    for i in range(len(words) - 1):
        tokens.append(f'{words[i]} {words[i+1]}')
    return tokens

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    retailer_list = [
        {'key': k, 'name': v['name']}
        for k, v in RETAILERS.items()
        if v.get('enabled')
    ]
    return render_template('index.html', retailers=retailer_list)


# ---------------------------------------------------------------------------
# API endpoints (called by the dashboard JS)
# ---------------------------------------------------------------------------

@app.route('/api/rankings')
def api_rankings():
    retailer = request.args.get('retailer') or None
    days     = int(request.args.get('days', 30))
    limit    = request.args.get('limit', '50')
    limit    = 9999 if limit == '9999' else int(limit)
    products = scorer.get_rankings(limit=limit, days=days, retailer=retailer)

    # Serialise score_history as a simple list of [date, score] for sparklines
    for p in products:
        p['score_series'] = [
            {'date': h['scored_date'], 'score': h['score']}
            for h in p.get('score_history', [])
        ]
        del p['score_history']   # Keep the response lean
        # image_url is already on the product row from the DB join

    return jsonify(products)


@app.route('/api/product/<int:product_id>')
def api_product(product_id):
    product  = db.get_product(product_id)
    if not product:
        return jsonify({'error': 'Not found'}), 404

    snapshots = db.get_snapshots(product_id, limit=30)
    history   = db.get_score_history(product_id, days=60)

    return jsonify({
        'product':   product,
        'snapshots': snapshots,
        'history':   history,
    })


@app.route('/api/stats')
def api_stats():
    from datetime import date, timedelta

    retailer      = request.args.get('retailer') or None
    products      = db.get_all_products(retailer=retailer)
    top           = scorer.get_rankings(limit=1, retailer=retailer)

    today         = date.today()
    week_ago      = (today - timedelta(days=7)).isoformat()
    two_weeks_ago = (today - timedelta(days=14)).isoformat()

    new_this_week = sum(
        1 for p in products
        if p.get('first_seen', '') >= week_ago
    )

    # Fastest rising: product with the biggest average daily score improvement
    # comparing the last 7 days to the 7 days before that.
    rankings = scorer.get_rankings(limit=9999, days=14, retailer=retailer)
    fastest_rising       = None
    fastest_rising_delta = None
    best_delta           = 0

    for p in rankings:
        history = p.get('score_history') or []
        recent  = [h['score'] for h in history if h.get('scored_date', '') >= week_ago]
        prev    = [h['score'] for h in history
                   if two_weeks_ago <= h.get('scored_date', '') < week_ago]
        if recent and prev:
            recent_avg = sum(recent) / len(recent)
            prev_avg   = sum(prev)   / len(prev)
            if prev_avg > 0:
                delta = round(((recent_avg - prev_avg) / prev_avg) * 100)
                if delta > best_delta:
                    best_delta           = delta
                    fastest_rising       = p['name']
                    fastest_rising_delta = delta

    return jsonify({
        'total_products':       len(products),
        'new_this_week':        new_this_week,
        'top_product':          top[0]['name'] if top else '—',
        'top_score':            top[0]['total_score'] if top else 0,
        'fastest_rising':       fastest_rising,
        'fastest_rising_delta': fastest_rising_delta,
    })


@app.route('/api/keywords')
def api_keywords():
    from datetime import date, timedelta
    comparison  = request.args.get('comparison', 'week')
    retailer    = request.args.get('retailer') or None
    category    = (request.args.get('category')    or '').lower() or None
    subcategory = (request.args.get('subcategory') or '').lower() or None
    max_age     = request.args.get('max_age') or None   # max product age in days

    spans = {'week': 7, 'month': 30, 'quarter': 90}
    n          = spans.get(comparison, 7)
    today      = date.today()
    curr_start = (today - timedelta(days=n)).isoformat()
    prev_start = (today - timedelta(days=n * 2)).isoformat()
    age_cutoff = (today - timedelta(days=int(max_age))).isoformat() if max_age else None

    products     = db.get_all_products(retailer=retailer)
    curr_counter = Counter()
    prev_counter = Counter()
    curr_total   = 0
    prev_total   = 0

    for p in products:
        if category and not (p.get('category') or '').lower().startswith(category):
            continue
        if subcategory:
            prod_sub = (p.get('subcategory') or '').lower().replace('-', ' ')
            if subcategory not in prod_sub:
                continue
        if age_cutoff and (p.get('first_seen') or '') < age_cutoff:
            continue
        name      = (p.get('name') or '').strip()
        last_seen = p.get('last_seen') or ''
        if not name:
            continue
        tokens = _tokenise(name)
        if last_seen >= curr_start:
            curr_counter.update(tokens)
            curr_total += 1
        elif last_seen >= prev_start:
            prev_counter.update(tokens)
            prev_total += 1

    result = []
    for kw, curr in curr_counter.items():
        if curr < 2:
            continue
        prev  = prev_counter.get(kw, 0)
        delta = round(((curr - prev) / prev) * 100) if prev > 0 else None
        result.append({'keyword': kw, 'count': curr, 'prev_count': prev, 'delta': delta})

    result.sort(key=lambda x: x['count'], reverse=True)

    top            = result[0] if result else None
    rising         = [r for r in result if r['delta'] is not None and r['delta'] > 0]
    fastest_rising = max(rising, key=lambda x: x['delta']) if rising else None

    return jsonify({
        'keywords':       result[:200],
        'unique_count':   len(result),
        'products_total': curr_total,
        'top_keyword':    top,
        'fastest_rising': fastest_rising,
    })


@app.route('/api/keywords/products')
def api_keyword_products():
    keyword  = (request.args.get('q') or '').lower().strip()
    retailer = request.args.get('retailer') or None
    category = (request.args.get('category') or '').lower() or None
    if not keyword:
        return jsonify([])
    # Use get_top_products so each product includes total_score and latest_price
    products = db.get_top_products(limit=9999, days=30, retailer=retailer)
    matching = [
        p for p in products
        if keyword in (p.get('name') or '').lower()
        and (not category or (p.get('category') or '').lower().startswith(category))
    ]
    for p in matching:
        p.pop('latest_raw_data', None)  # strip heavy field
    matching.sort(key=lambda p: p.get('last_seen') or '', reverse=True)
    return jsonify(matching)


@app.route('/api/success')
def api_success():
    from datetime import date
    from collections import Counter

    retailer = request.args.get('retailer') or None
    category = (request.args.get('category') or '').lower() or None
    min_days = int(request.args.get('min_days', 30))

    DEMAND_SIGNALS = {'featured', 'rank_improvement', 'sizes_sold_out', 'restock_event', 'review_velocity'}

    SIGNAL_FREQ_CONFIG = [
        ('long_runner',      'Long runner (30+ days)'),
        ('featured',         'Featured placement'),
        ('rank_improvement', 'Rank improvement'),
        ('sizes_sold_out',   'Selling through (sizes OOS)'),
        ('restock_event',    'Restock event'),
        ('review_velocity',  'Review velocity spike'),
    ]

    products = db.get_all_products(retailer=retailer)
    today    = date.today()
    results  = []

    for product in products:
        pid = product['id']

        if category and not (product.get('category') or '').lower().startswith(category):
            continue

        history = db.get_score_history(pid, days=365)
        if not history:
            continue

        days_tracked = len(history)
        if days_tracked < min_days:
            continue

        scores     = [h['score'] for h in history]
        peak_score = max(scores)
        cumulative = sum(scores)

        # Collect all signal names across full history
        all_signal_names = set()
        has_demand       = False
        for h in history:
            for sig in (h.get('signals') or {}):
                all_signal_names.add(sig)
                if sig in DEMAND_SIGNALS:
                    has_demand = True

        if not has_demand:
            continue

        # Trajectory classification
        peak_idx      = scores.index(peak_score)
        peak_fraction = peak_idx / max(days_tracked - 1, 1)
        if peak_fraction <= 0.30:
            trajectory = 'early_spike'
        elif peak_fraction >= 0.65:
            trajectory = 'steady_climber'
        else:
            trajectory = 'mid_life_peak'

        # Current signal tags (latest score)
        latest_signals = history[-1].get('signals', {})
        signal_tags    = scorer._signals_to_tags(latest_signals)

        results.append({
            'id':               pid,
            'name':             product.get('name'),
            'retailer':         product.get('retailer'),
            'category':         product.get('category'),
            'subcategory':      product.get('subcategory'),
            'url':              product.get('url'),
            'image_url':        product.get('image_url'),
            'days_tracked':     days_tracked,
            'peak_score':       round(peak_score),
            'peak_day':         peak_idx + 1,
            'cumulative':       round(cumulative),
            'trajectory':       trajectory,
            'signal_tags':      signal_tags,
            'all_signal_names': sorted(all_signal_names),
            'score_series':     [{'date': h['scored_date'], 'score': h['score']} for h in history],
        })

    results.sort(key=lambda x: x['peak_score'], reverse=True)

    # Retain only the top 20% by peak score (self-calibrates with catalogue size).
    # Require a hard floor of at least 5 products so the view is never empty.
    top_n = max(5, round(len(results) * 0.20))
    results = results[:top_n]

    total = len(results)

    # Summary stats
    avg_days = round(sum(r['days_tracked'] for r in results) / total) if total else 0
    avg_peak = round(sum(r['peak_score']   for r in results) / total) if total else 0

    # Top signal by frequency
    sig_counter = Counter()
    for r in results:
        for sig in r['all_signal_names']:
            sig_counter[sig] += 1

    sig_label_map = dict(SIGNAL_FREQ_CONFIG)
    top_sig_key   = sig_counter.most_common(1)[0][0] if sig_counter else None
    top_sig_pct   = round((sig_counter[top_sig_key] / total) * 100) if top_sig_key and total else 0

    # Signal frequency list (in fixed order)
    signal_freq = [
        {
            'key':   key,
            'label': label,
            'count': sig_counter.get(key, 0),
        }
        for key, label in SIGNAL_FREQ_CONFIG
        if sig_counter.get(key, 0) > 0
    ]

    return jsonify({
        'summary': {
            'total':             total,
            'avg_days':          avg_days,
            'avg_peak':          avg_peak,
            'top_signal_label':  sig_label_map.get(top_sig_key, '—') if top_sig_key else '—',
            'top_signal_pct':    top_sig_pct,
        },
        'signal_freq': signal_freq,
        'products':    results,
    })


@app.route('/api/markdown')
def api_markdown():
    from collections import Counter
    retailer = request.args.get('retailer') or None
    category = (request.args.get('category') or '').lower() or None

    products = scorer.get_removed_analysis(retailer=retailer)

    if category:
        products = [p for p in products if (p.get('category') or '').lower().startswith(category)]

    total     = len(products)
    poor      = sum(1 for p in products if p['reason'] == 'poor_seller')
    season    = sum(1 for p in products if p['reason'] == 'end_of_season')
    completed = sum(1 for p in products if p['reason'] == 'completed_run')

    # Plain-English insights
    insights = []
    if poor > 0:
        insights.append(f"{poor} poor seller{'s' if poor != 1 else ''} removed — worth reviewing these design decisions")
    if completed > 0:
        insights.append(f"{completed} style{'s' if completed != 1 else ''} completed a strong run — consider reordering or similar designs")
    sub_counts = Counter(p.get('subcategory') for p in products if p.get('subcategory'))
    if sub_counts:
        top_sub, top_n = sub_counts.most_common(1)[0]
        insights.append(f"{top_sub.title()} has the most removals ({top_n} products)")
    high_completed = [p for p in products if p['reason'] == 'completed_run' and p['peak_score'] > 50]
    if high_completed:
        insights.append(f"{len(high_completed)} high-scoring style{'s' if len(high_completed) != 1 else ''} removed after strong performance — confirmed strong consumer demand")

    return jsonify({
        'summary':  {'total': total, 'poor_seller': poor, 'end_of_season': season, 'completed_run': completed},
        'insights': insights,
        'products': products,
    })


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=DASHBOARD_DEBUG)
