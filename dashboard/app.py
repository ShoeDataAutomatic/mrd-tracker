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
from flask import Flask, render_template, jsonify, request, Response, redirect
from config import DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_DEBUG, RETAILERS
import scorer
import database as db

# Subcategory filter → terms that should match in the product's subcategory field.
# Handles plural/singular and common variant names across retailers.
_SUBCAT_ALIASES = {
    'trainers':  ['trainers', 'trainer', 'sneaker', 'sneakers'],
    'boots':     ['boot', 'boots'],
    'sandals':   ['sandal', 'sandals'],
    'heels':     ['heels', 'block heels', 'kitten heels', 'cone heels',
                  'court shoes', 'stilettos', 'wedge heels', 'pumps'],
    'flats':     ['flats', 'ballet flats', 'flat shoes'],
    'loafers':   ['loafer', 'loafers'],
    'brogues':   ['brogue', 'brogues', 'oxford', 'oxfords', 'loafers and brogues'],
    'slippers':  ['slipper', 'slippers'],
    'clogs':     ['clog', 'clogs'],
    'mules':     ['mule', 'mules'],
    'wedges':    ['wedge', 'wedges', 'espadrille', 'espadrilles'],
    'sliders':   ['slider', 'sliders', 'flip flops', 'flip flop',
                  'sandals, sliders and flip-flops'],
    'platforms': ['platform', 'platforms'],
}

def _subcat_match(filter_val, product_sub):
    """Return True if any alias for filter_val matches product_sub.
    Single-word aliases use whole-word matching to avoid false positives
    (e.g. 'heel' matching 'heeled sandals', 'flat' matching 'platform').
    Multi-word phrase aliases use substring matching.
    """
    aliases = _SUBCAT_ALIASES.get(filter_val, [filter_val])
    sub_words = set(product_sub.split())
    for a in aliases:
        if ' ' in a:               # multi-word phrase
            if a in product_sub:
                return True
        else:                      # single word — whole-word only
            if a in sub_words:
                return True
    return False

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

# ---------------------------------------------------------------------------
# Smart attribute term sets  (colour / material / trim / pattern / type / fit / brand)
# Reviewed and verified by Mitch against production keyword data (single words only,
# matches the bigram-free keyword listing used elsewhere on this page).
# ---------------------------------------------------------------------------

_COLOUR_TERMS = frozenset([
    'abstract', 'animal', 'beige', 'black', 'blue', 'bright', 'brown', 'burgundy',
    'camel', 'chocolate', 'clear', 'colour', 'contrast', 'coral', 'cream', 'dark',
    'dot', 'floral', 'gingham', 'gold', 'green', 'grey', 'gum', 'khaki', 'leopard',
    'light', 'lilac', 'metallic', 'mink', 'mint', 'multi', 'multicolour', 'navy',
    'nude', 'oatmeal', 'off', 'orange', 'pale', 'pink', 'polka', 'print', 'printed',
    'red', 'rose', 'rust', 'seagrass', 'silver', 'snake', 'stone', 'stripe', 'striped',
    'tan', 'tone', 'tortoiseshell', 'white', 'yellow'
])

_MATERIAL_TERMS = frozenset([
    'anglaise', 'borg', 'broderie', 'brushed', 'canvas', 'corduroy', 'cork', 'cotton',
    'croc', 'crochet', 'crocodile', 'cut', 'denim', 'effect', 'embossed', 'fabric',
    'faux', 'fluffy', 'foam', 'fur', 'glitter', 'grosgrain', 'interwoven', 'jersey',
    'jute', 'knit', 'knitted', 'lace', 'laser', 'lasercut', 'lattice', 'leather',
    'lined', 'linen', 'lizard', 'look', 'macrame', 'mesh', 'nubuck', 'out', 'patent',
    'perspex', 'plush', 'premium', 'raffia', 'real', 'rubber', 'satin', 'scalloped',
    'shimmer', 'snakeskin', 'soft', 'suede', 'suedette', 'tapestry', 'textured',
    'tulle', 'velvet', 'weave', 'woven'
])

_TRIM_TERMS = frozenset([
    'applique', 'backless', 'bar', 'beaded', 'block', 'bow', 'braided', 'buckle',
    'buckled', 'chain', 'charm', 'charms', 'cowboy', 'crystal', 'cutout', 'diamant',
    'diamante', 'dorsay', 'elasticated', 'embellished', 'embroidered', 'eyelet',
    'fisherman', 'flower', 'fringe', 'fringed', 'gem', 'hardware', 'heart', 'hiking',
    'horsebit', 'knot', 'knotted', 'metal', 'multistrap', 'pearl', 'plaited', 'quilted',
    'ring', 'ruched', 'running', 'scallop', 'shell', 'snaffle', 'stacked', 'starfish',
    'stitch', 'studded', 'tassel', 'tie', 'toggle', 'trim', 'twist', 'twisted',
    'whipstitch', 'whipstitched', 'wrap', 'zip'
])

_PATTERN_TERMS = frozenset([
    'almond', 'ankle', 'asymmetric', 'back', 'ballerina', 'ballet', 'barely', 'cage',
    'caged', 'calf', 'chelsea', 'chunky', 'classic', 'closed', 'court', 'cross',
    'crossband', 'crossover', 'derby', 'double', 'espadrille', 'espadrilles',
    'fastening', 'flatform', 'footbed', 'gladiator', 'heeled', 'high', 'jane', 'jelly',
    'kitten', 'knee', 'loop', 'low', 'mary', 'mid', 'moulded', 'mule', 'mules', 'open',
    'over', 'panel', 'panelled', 'peep', 'penny', 'platform', 'point', 'pointed',
    'pool', 'post', 'pull', 'pumps', 'riding', 'round', 'side', 'single', 'slim',
    'sling', 'slingback', 'sock', 'square', 'stiletto', 'strap', 'strappy', 'tab',
    'thong', 'toe', 'top', 'touch', 'vamp', 'wedge', 'wedges', 'western'
])

_TYPE_TERMS = frozenset([
    'active', 'beach', 'boot', 'boots', 'canvas', 'clog', 'clogs', 'essential', 'flat',
    'flats', 'flip', 'flops', 'football', 'formal', 'heel', 'heels', 'leisure',
    'loafer', 'loafers', 'sandals', 'shoe', 'shoes', 'slider', 'sliders', 'slides',
    'slipers', 'slippers', 'sneakers', 'sports', 'sporty', 'trainer', 'trainers',
    'trekker'
])

_FIT_TERMS = frozenset([
    'fit', 'wide'
])

_BRAND_TERMS = frozenset([
    'cars', 'coleen', 'desire', 'disney', 'echevarr', 'edit', 'foamflex', 'ipanema',
    'jack', 'jones', 'london', 'man', 'marvel', 'mickey', 'minnie', 'mouse', 'ora',
    'paula', 'pixar', 'public', 'rebel', 'rita', 'south', 'spider', 'truffle'
])


_KW_ATTR_KEYS = ['colour', 'material', 'trim', 'pattern', 'type', 'fit', 'brand']

# In-memory cache of term sets, merged from the hardcoded sets above (the
# original baseline) plus any keywords Mitch has approved via the review
# queue (see /api/keyword-review/* routes below). Refreshed periodically so
# newly-approved keywords show up in the live filters without a restart, but
# without hitting the database on every single product classification.
_term_cache = {'ts': 0, 'sets': None}
_TERM_CACHE_TTL = 300   # seconds


def _get_term_sets():
    import time
    now = time.time()
    if _term_cache['sets'] is None or now - _term_cache['ts'] > _TERM_CACHE_TTL:
        sets = {
            'colour':   set(_COLOUR_TERMS),
            'material': set(_MATERIAL_TERMS),
            'trim':     set(_TRIM_TERMS),
            'pattern':  set(_PATTERN_TERMS),
            'type':     set(_TYPE_TERMS),
            'fit':      set(_FIT_TERMS),
            'brand':    set(_BRAND_TERMS),
        }
        try:
            for row in db.get_keyword_classifications(status='approved'):
                for key in _KW_ATTR_KEYS:
                    if row.get(key):
                        sets[key].add(row['keyword'])
        except Exception:
            pass   # DB unavailable — fall back to the hardcoded sets only
        _term_cache['sets'] = sets
        _term_cache['ts']   = now
    return _term_cache['sets']


def _classify_name(name):
    """Return (colours, materials, trims, patterns, types, fits, brands) -
    sets of matching attribute terms found in a product name. Term sets are
    the hardcoded baseline merged with any Mitch-approved additions from the
    keyword review queue."""
    tokens = set(_tokenise(name))   # unigrams + bigrams, stop-words removed
    term_sets = _get_term_sets()
    colours, materials, trims, patterns, types, fits, brands = (
        set(), set(), set(), set(), set(), set(), set()
    )
    for key, bucket in (
        ('colour', colours), ('material', materials),
        ('trim', trims), ('pattern', patterns),
        ('type', types), ('fit', fits), ('brand', brands),
    ):
        for t in term_sets[key]:
            if t in tokens:
                bucket.add(t)
    return colours, materials, trims, patterns, types, fits, brands

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
    retailer   = request.args.get('retailer') or None
    days       = int(request.args.get('days', 30))
    limit      = request.args.get('limit', '50')
    limit      = 9999 if limit == '9999' else int(limit)
    start_date = request.args.get('start_date') or None
    end_date   = request.args.get('end_date')   or None
    products   = scorer.get_rankings(limit=limit, days=days, retailer=retailer,
                                     start_date=start_date, end_date=end_date)

    # Serialise score_history as a simple list of [date, score] for sparklines
    for p in products:
        p['score_series'] = [
            {'date': h['scored_date'], 'score': h['score']}
            for h in p.get('score_history', [])
        ]
        del p['score_history']   # Keep the response lean
        # image_url is already on the product row from the DB join

    return jsonify(products)


@app.route('/api/image/<int:product_id>')
def api_image(product_id):
    data, content_type = db.get_image_blob(product_id)
    if data:
        return Response(data, mimetype=content_type or 'image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    # Blob not cached yet — redirect to CDN URL as fallback
    product = db.get_product(product_id)
    if product and product.get('image_url'):
        return redirect(product['image_url'])
    return '', 404


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

    retailer   = request.args.get('retailer') or None
    start_date = request.args.get('start_date') or None
    end_date   = request.args.get('end_date')   or None

    products      = db.get_all_products(retailer=retailer)
    top           = scorer.get_rankings(limit=1, retailer=retailer,
                                        start_date=start_date, end_date=end_date)

    today         = date.today()
    week_ago      = (today - timedelta(days=7)).isoformat()
    two_weeks_ago = (today - timedelta(days=14)).isoformat()

    new_this_week = sum(
        1 for p in products
        if p.get('first_seen', '') >= week_ago
    )

    # Fastest rising: biggest average score improvement week-on-week within range
    rankings = scorer.get_rankings(limit=9999, days=14, retailer=retailer,
                                   start_date=start_date, end_date=end_date)
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
    def _split(v): return [x.strip() for x in v.split(',') if x.strip()]

    comparison  = request.args.get('comparison', 'month')
    retailers   = _split((request.args.get('retailer')    or '').lower())
    categories  = _split((request.args.get('category')    or '').lower())
    subcats     = _split((request.args.get('subcategory') or '').lower())
    max_age     = request.args.get('max_age') or None
    start_date  = request.args.get('start_date') or None
    end_date    = request.args.get('end_date')   or None

    spans = {'week': 7, 'month': 30, 'quarter': 90}
    n     = spans.get(comparison, 30)
    today = date.today()

    if start_date and end_date:
        from datetime import date as date_cls
        d1 = date_cls.fromisoformat(start_date)
        d2 = date_cls.fromisoformat(end_date)
        range_days = max((d2 - d1).days, 1)
        curr_start = start_date
        curr_end   = end_date
        prev_end   = (d1 - timedelta(days=1)).isoformat()
        prev_start = (d1 - timedelta(days=range_days)).isoformat()
    else:
        curr_start = (today - timedelta(days=n)).isoformat()
        curr_end   = today.isoformat()
        prev_start = (today - timedelta(days=n * 2)).isoformat()
        prev_end   = (today - timedelta(days=n)).isoformat()

    age_cutoff = (today - timedelta(days=int(max_age))).isoformat() if max_age else None

    products     = db.get_all_products(retailer=None)
    curr_counter = Counter()
    prev_counter = Counter()
    curr_total   = 0
    prev_total   = 0

    for p in products:
        if retailers and p.get('retailer', '').lower() not in retailers:
            continue
        if categories and not any((p.get('category') or '').lower().startswith(c) for c in categories):
            continue
        if subcats:
            prod_sub = (p.get('subcategory') or '').lower().replace('-', ' ')
            if not any(_subcat_match(s, prod_sub) for s in subcats):
                continue
        if age_cutoff and (p.get('first_seen') or '') < age_cutoff:
            continue
        name      = (p.get('name') or '').strip()
        last_seen = p.get('last_seen') or ''
        if not name:
            continue
        tokens    = _tokenise(name)
        last_date = last_seen[:10]   # normalise datetime -> date for comparison
        if curr_start <= last_date <= curr_end:
            curr_counter.update(tokens)
            curr_total += 1
        elif prev_start <= last_date <= prev_end:
            prev_counter.update(tokens)
            prev_total += 1

    result = []
    for kw, curr in curr_counter.items():
        if curr < 2:
            continue
        if ' ' in kw:          # skip bigrams — single words only
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
    def _split(v): return [x.strip() for x in v.split(',') if x.strip()]

    include_raw = (request.args.get('include') or '').lower().strip()
    exclude_raw = (request.args.get('exclude') or '').lower().strip()
    single_q    = (request.args.get('q') or '').lower().strip()
    retailers   = _split((request.args.get('retailer')    or '').lower())
    categories  = _split((request.args.get('category')    or '').lower())
    subcats     = _split((request.args.get('subcategory') or '').lower())
    included   = _split(include_raw) if include_raw else []
    excluded   = _split(exclude_raw) if exclude_raw else []
    colours_f   = _split((request.args.get('colours')   or '').lower())
    materials_f = _split((request.args.get('materials') or '').lower())
    trims_f     = _split((request.args.get('trims')     or '').lower())
    patterns_f  = _split((request.args.get('patterns')  or '').lower())
    types_f     = _split((request.args.get('types')     or '').lower())
    fits_f      = _split((request.args.get('fits')      or '').lower())
    brands_f    = _split((request.args.get('brands')    or '').lower())
    if single_q and not included:
        included = [single_q]
    products = db.get_top_products(limit=9999, days=30, retailer=None)
    def matches(p):
        name = (p.get('name') or '').lower()
        cat  = (p.get('category') or '').lower()
        sub  = (p.get('subcategory') or '').lower()
        if retailers and p.get('retailer', '').lower() not in retailers:
            return False
        if categories and not any(cat.startswith(c) for c in categories):
            return False
        if subcats and not any(_subcat_match(s, sub) for s in subcats):
            return False
        if included and not all(kw in name for kw in included):
            return False
        if excluded and any(kw in name for kw in excluded):
            return False
        if colours_f or materials_f or trims_f or patterns_f or types_f or fits_f or brands_f:
            c, m, tr, pa, ty, fi, br = _classify_name(name)
            if colours_f   and not any(t in c  for t in colours_f):   return False
            if materials_f and not any(t in m  for t in materials_f): return False
            if trims_f     and not any(t in tr for t in trims_f):     return False
            if patterns_f  and not any(t in pa for t in patterns_f):  return False
            if types_f     and not any(t in ty for t in types_f):     return False
            if fits_f      and not any(t in fi for t in fits_f):      return False
            if brands_f    and not any(t in br for t in brands_f):    return False
        return True
    matching = [p for p in products if matches(p)]
    for p in matching:
        p.pop('latest_raw_data', None)
    matching.sort(key=lambda p: p.get('last_seen') or '', reverse=True)
    return jsonify(matching)



@app.route('/api/keywords/attributes')
def api_keyword_attributes():
    def _split(v): return [x.strip() for x in v.split(',') if x.strip()]
    include_raw = (request.args.get('include') or '').lower().strip()
    retailers   = _split((request.args.get('retailer')    or '').lower())
    categories  = _split((request.args.get('category')    or '').lower())
    subcats     = _split((request.args.get('subcategory') or '').lower())
    included    = _split(include_raw) if include_raw else []
    products    = db.get_top_products(limit=9999, days=30, retailer=None)
    colour_counts   = Counter()
    material_counts = Counter()
    trim_counts     = Counter()
    pattern_counts  = Counter()
    type_counts     = Counter()
    fit_counts      = Counter()
    brand_counts    = Counter()
    for p in products:
        name = (p.get('name') or '').lower()
        if retailers and p.get('retailer', '').lower() not in retailers:
            continue
        cat = (p.get('category') or '').lower()
        if categories and not any(cat.startswith(c) for c in categories):
            continue
        sub = (p.get('subcategory') or '').lower()
        if subcats and not any(_subcat_match(s, sub) for s in subcats):
            continue
        if included and not all(kw in name for kw in included):
            continue
        c, m, tr, pa, ty, fi, br = _classify_name(name)
        colour_counts.update(c)
        material_counts.update(m)
        trim_counts.update(tr)
        pattern_counts.update(pa)
        type_counts.update(ty)
        fit_counts.update(fi)
        brand_counts.update(br)
    def to_list(counter):
        return [{'term': k, 'count': v} for k, v in counter.most_common()]
    return jsonify({
        'colours':   to_list(colour_counts),
        'materials': to_list(material_counts),
        'trims':     to_list(trim_counts),
        'patterns':  to_list(pattern_counts),
        'types':     to_list(type_counts),
        'fits':      to_list(fit_counts),
        'brands':    to_list(brand_counts),
    })

@app.route('/export/keyword-classifications')
def export_keyword_classifications():
    import csv, io
    products = db.get_all_products(retailer=None)
    all_tokens = Counter()
    for p in products:
        name = (p.get('name') or '').strip()
        if name:
            all_tokens.update(_tokenise(name))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Keyword', 'Count', 'Colour', 'Material', 'Trim', 'Pattern', 'Type', 'Fit', 'Brand'])
    for kw, cnt in sorted(all_tokens.items(), key=lambda x: -x[1]):
        if ' ' in kw:          # skip bigrams — single words only, matches /api/keywords
            continue
        c, m, tr, pa, ty, fi, br = _classify_name(kw)
        writer.writerow([
            kw, cnt,
            'Y' if c else '', 'Y' if m else '', 'Y' if tr else '',
            'Y' if pa else '', 'Y' if ty else '', 'Y' if fi else '', 'Y' if br else '',
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="keyword_classifications.csv"'}
    )


@app.route('/api/keyword-review/pending')
def api_keyword_review_pending():
    """List keywords the LLM has suggested classifications for, awaiting Mitch's review."""
    rows = db.get_keyword_classifications(status='pending')
    return jsonify(rows)


@app.route('/api/keyword-review/decide', methods=['POST'])
def api_keyword_review_decide():
    """
    Approve or reject a pending keyword suggestion.
    Body: {"keyword": "glitter", "decision": "approved"|"rejected", "attrs": {"colour": false, ...}}
    'attrs' is optional — only needed if Mitch corrected the suggested categories before approving.
    """
    data     = request.get_json(silent=True) or {}
    keyword  = (data.get('keyword') or '').strip()
    decision = (data.get('decision') or '').strip()
    attrs    = data.get('attrs')
    if not keyword or decision not in ('approved', 'rejected'):
        return jsonify({'error': 'keyword and a valid decision (approved/rejected) are required'}), 400
    db.review_keyword_classification(keyword, decision, attrs=attrs)
    return jsonify({'ok': True})


@app.route('/api/keyword-review/run-now', methods=['POST'])
def api_keyword_review_run_now():
    """Manually trigger the new-keyword classification job (normally runs after each scrape)."""
    from dashboard import keyword_classifier
    try:
        result = keyword_classifier.run_keyword_classification_job()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(result)


@app.route('/api/success')
def api_success():
    from datetime import date
    from collections import Counter

    retailer   = request.args.get('retailer') or None
    category   = (request.args.get('category') or '').lower() or None
    min_days   = int(request.args.get('min_days', 3))
    start_date = request.args.get('start_date') or None
    end_date   = request.args.get('end_date')   or None

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

        history = db.get_score_history(pid, days=365, start_date=start_date, end_date=end_date)
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
    retailer   = request.args.get('retailer') or None
    category   = (request.args.get('category') or '').lower() or None
    start_date = request.args.get('start_date') or None
    end_date   = request.args.get('end_date')   or None

    products = scorer.get_removed_analysis(retailer=retailer,
                                           start_date=start_date, end_date=end_date)

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
