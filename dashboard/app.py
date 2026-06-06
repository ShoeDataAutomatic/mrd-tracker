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
    retailer = request.args.get('retailer') or None
    products = db.get_all_products(retailer=retailer)
    top      = scorer.get_rankings(limit=1, retailer=retailer)

    from datetime import date, timedelta
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    new_today = sum(
        1 for p in products
        if p.get('first_seen', '') >= today
    )

    return jsonify({
        'total_products': len(products),
        'new_today':      new_today,
        'top_product':    top[0]['name'] if top else '—',
        'top_score':      top[0]['total_score'] if top else 0,
    })


@app.route('/api/keywords')
def api_keywords():
    from datetime import date, timedelta
    comparison = request.args.get('comparison', 'week')
    retailer   = request.args.get('retailer') or None
    category   = (request.args.get('category') or '').lower() or None

    spans = {'week': 7, 'month': 30, 'quarter': 90}
    n          = spans.get(comparison, 7)
    today      = date.today()
    curr_start = (today - timedelta(days=n)).isoformat()
    prev_start = (today - timedelta(days=n * 2)).isoformat()

    products     = db.get_all_products(retailer=retailer)
    curr_counter = Counter()
    prev_counter = Counter()
    curr_total   = 0
    prev_total   = 0

    for p in products:
        if category and not (p.get('category') or '').lower().startswith(category):
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
    products = db.get_all_products(retailer=retailer)
    matching = [
        p for p in products
        if keyword in (p.get('name') or '').lower()
        and (not category or (p.get('category') or '').lower().startswith(category))
    ]
    matching.sort(key=lambda p: p.get('last_seen') or '', reverse=True)
    return jsonify(matching)


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
