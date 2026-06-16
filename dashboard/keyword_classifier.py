"""
dashboard/keyword_classifier.py — Automated keyword classification.

After each scrape, brand-new product-name keywords (ones never classified
before, in any status) are detected and sent to the Claude API for
classification into Mitch's 7 categories (Colour / Material / Trim /
Pattern / Type / Fit / Brand), using a few real product names containing
the keyword as context.

Results are stored in the keyword_classifications table with
status='pending' — they do NOT affect the live dashboard filters until
Mitch approves them from the Keyword Analysis page's review queue
(see app.py's /api/keyword-review/* routes).

Run manually:
    python run.py --classify-keywords      Classify any new keywords now
    python run.py --seed-keywords           One-off: load the legacy hardcoded
                                             term sets into the DB as approved

Runs automatically as part of run.py's daily_job(), after the scrape.
"""

import os
import re
import json
import logging
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))   # project root, for `import database`
import database as db

logger = logging.getLogger(__name__)

_ATTR_KEYS = ['colour', 'material', 'trim', 'pattern', 'type', 'fit', 'brand']

# Kept identical to dashboard/app.py's _STOP_WORDS / _tokenise so that
# "new keyword" detection lines up exactly with what the dashboard displays.
# Duplicated here (rather than imported) to avoid a circular import between
# this module and app.py.
_STOP_WORDS = {
    'a', 'an', 'the', 'and', 'or', 'in', 'on', 'with', 'for', 'to', 'of', 'at', 'by',
    'from', 'up', 'as', 'is', 'it', 'its', 'be', 'are', 'was', 'were', 'has', 'have',
    'had', 'do', 'does', 'did', 'but', 'not', 'no', 'so', 'if', 'this', 'that',
    'these', 'those', 's', 'amp',
}


def _tokenise(name):
    """Single words only (no bigrams) — matches the keyword listing on the dashboard."""
    text  = name.lower().replace('-', ' ').replace("'s", '').replace("'", '')
    words = re.findall(r'[a-z]+', text)
    return [w for w in words if w not in _STOP_WORDS and len(w) > 2]


_CATEGORY_GUIDE = """You are classifying single-word keywords extracted from B2B footwear product names sold by high-volume, low-price retailers (e.g. Primark, New Look) into zero or more of 7 categories. A keyword can belong to more than one category, or to none.

- Colour: colours, colour-as-print descriptors (e.g. "leopard", "floral", "striped"), colour modifiers ("dark", "bright", "multi")
- Material: fabrics and surface materials (e.g. "suede", "canvas", "glitter", "faux", "mesh")
- Trim: decorative hardware or embellishments (e.g. "buckle", "studded", "tassel", "zip", "bow")
- Pattern: construction/silhouette descriptors (e.g. "platform", "pointed", "strappy", "chunky", "sling")
- Type: the kind of footwear itself (e.g. "trainer", "sandal", "boot", "loafer")
- Fit: fit or width descriptors (e.g. "wide", "fit")
- Brand: brand names or licensed characters (e.g. "disney", "minnie", "ipanema")

Respond with ONLY a JSON object mapping each input keyword (exactly as given) to an object of 7 booleans, e.g.:
{"glitter": {"colour": false, "material": true, "trim": false, "pattern": false, "type": false, "fit": false, "brand": false}}

If a keyword doesn't clearly belong to any category (common — e.g. generic words, retailer SKU fragments), set all 7 to false. No explanation, no markdown fences — just the JSON object."""

_MAX_KEYWORDS_PER_CALL    = 40
_MAX_EXAMPLES_PER_KEYWORD = 4


def _get_anthropic_client():
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        logger.error('[keyword-classifier] anthropic package not installed — run: pip install anthropic')
        return None
    return anthropic.Anthropic(api_key=api_key)


def find_new_keywords():
    """
    Tokenise every product name currently in the DB and return
    {keyword: [example product names]} for keywords that don't yet have a
    row in keyword_classifications (i.e. genuinely new). Single words only.
    """
    products = db.get_all_products(retailer=None)
    known    = db.get_known_keyword_set()

    examples = defaultdict(list)
    for p in products:
        name = (p.get('name') or '').strip()
        if not name:
            continue
        for tok in set(_tokenise(name)):
            if tok in known:
                continue
            if len(examples[tok]) < _MAX_EXAMPLES_PER_KEYWORD:
                examples[tok].append(name)
    return dict(examples)


def _classify_batch_llm(client, model, batch_keywords, examples_map):
    """One Claude API call for a batch of keywords. Returns {keyword: {colour: bool, ...}}."""
    lines = []
    for kw in batch_keywords:
        ex = examples_map.get(kw, [])
        ex_str = '; '.join(ex[:3]) if ex else '(no example available)'
        lines.append(f'- "{kw}" — seen in: {ex_str}')

    prompt = _CATEGORY_GUIDE + '\n\nClassify these keywords:\n' + '\n'.join(lines)

    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{'role': 'user', 'content': prompt}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r'^```(?:json)?|```$', '', text, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.error(f'[keyword-classifier] Could not parse LLM response as JSON: {text[:300]}')
        return {}

    result = {}
    for kw in batch_keywords:
        attrs = parsed.get(kw) or {}
        result[kw] = {k: bool(attrs.get(k)) for k in _ATTR_KEYS}
    return result


def run_keyword_classification_job():
    """
    Find new keywords, classify them via the Claude API, and queue the
    suggestions for Mitch's review. Safe to call repeatedly — keywords
    already in the table (pending, approved, or rejected) are never
    reprocessed or overwritten.
    """
    db.init_db()
    new_kw_examples = find_new_keywords()
    if not new_kw_examples:
        logger.info('[keyword-classifier] No new keywords found.')
        return {'new_keywords': 0, 'queued': 0}

    client = _get_anthropic_client()
    if not client:
        logger.warning(
            f'[keyword-classifier] {len(new_kw_examples)} new keyword(s) found but '
            'ANTHROPIC_API_KEY is not set (or the anthropic package is missing) — '
            'skipping LLM classification this run.'
        )
        return {'new_keywords': len(new_kw_examples), 'queued': 0}

    model = os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6')
    keywords = sorted(new_kw_examples.keys())
    logger.info(f'[keyword-classifier] {len(keywords)} new keyword(s) found — classifying via {model}...')

    queued = 0
    for i in range(0, len(keywords), _MAX_KEYWORDS_PER_CALL):
        batch = keywords[i:i + _MAX_KEYWORDS_PER_CALL]
        try:
            results = _classify_batch_llm(client, model, batch, new_kw_examples)
        except Exception as e:
            logger.error(f'[keyword-classifier] LLM call failed for batch starting at index {i}: {e}')
            continue
        for kw in batch:
            attrs = results.get(kw, {k: False for k in _ATTR_KEYS})
            db.add_keyword_classification(
                kw, attrs, status='pending', source='llm',
                examples=new_kw_examples.get(kw, []),
            )
            queued += 1

    logger.info(f'[keyword-classifier] Queued {queued} keyword(s) for review.')
    return {'new_keywords': len(keywords), 'queued': queued}


def seed_legacy_classifications():
    """
    One-off migration: load Mitch's manually-reviewed term sets (currently
    hardcoded in dashboard/app.py) into the keyword_classifications table as
    approved/manual rows, so the database becomes the single source of truth
    going forward without losing any of his existing review work.

    Safe to re-run — uses INSERT OR IGNORE under the hood (via
    db.add_keyword_classification), so it never overwrites a row that
    already exists.
    """
    db.init_db()
    sys.path.insert(0, os.path.dirname(__file__))   # dashboard dir, for `import app`
    import app as app_module   # noqa: imported lazily to avoid a circular import with app.py

    term_sets = {
        'colour':   app_module._COLOUR_TERMS,
        'material': app_module._MATERIAL_TERMS,
        'trim':     app_module._TRIM_TERMS,
        'pattern':  app_module._PATTERN_TERMS,
        'type':     app_module._TYPE_TERMS,
        'fit':      app_module._FIT_TERMS,
        'brand':    app_module._BRAND_TERMS,
    }
    all_keywords = set()
    for terms in term_sets.values():
        all_keywords |= set(terms)

    count = 0
    for kw in sorted(all_keywords):
        attrs = {key: kw in terms for key, terms in term_sets.items()}
        db.add_keyword_classification(kw, attrs, status='approved', source='manual', examples=[])
        count += 1

    logger.info(f'[keyword-classifier] Seeded {count} legacy keyword classification(s) into the database.')
    return count
