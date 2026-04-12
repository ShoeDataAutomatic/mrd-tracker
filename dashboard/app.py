"""
dashboard/app.py — Flask web dashboard.

Run with:  python dashboard/app.py
Or via:    python run.py --dashboard

Then open: http://localhost:5000
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
from flask import Flask, render_template, jsonify, request
from config import DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_DEBUG, RETAILERS
import scorer
import database as db

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


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=DASHBOARD_DEBUG)
