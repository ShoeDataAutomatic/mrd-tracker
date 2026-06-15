"""
debug_mens_subcats.py — run on Railway shell to diagnose men's subcategory issues.
    python debug_mens_subcats.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from scrapers.newlook import NewLookScraper
from config import DATABASE_PATH
import database as db
from collections import Counter

# ── Current state in DB ───────────────────────────────────────────────────────
products = db.get_all_products(retailer='newlook')
mens = [p for p in products if (p.get('category') or '').lower() == 'men']
print(f'Men\'s New Look products in DB: {len(mens)}\n')

current_subs = Counter(p.get('subcategory') or 'None' for p in mens)
print('=== Current subcategories in DB ===')
for sub, n in current_subs.most_common():
    print(f'  {repr(sub):40s} {n}')

# ── What the new logic would produce ─────────────────────────────────────────
print('\n=== Fetching category sitemap… ===')
style_prefixes = NewLookScraper._fetch_style_categories()
print(f'  {len(style_prefixes)} style prefixes loaded')

new_subs = Counter()
method_used = Counter()
samples = []

for p in mens:
    url      = p.get('url', '')
    name     = p.get('name', '')
    url_path = url.replace('https://www.newlook.com', '')

    gender  = NewLookScraper._gender_from_path(url_path)
    _generic = {'shoes', 'mens shoes', 'womens shoes', 'girls shoes', 'boys shoes'}

    new_sub = NewLookScraper._style_from_category_prefix(url_path, style_prefixes)
    if new_sub and new_sub.endswith(' boots'):
        new_sub = 'boots'
    method = 'sitemap'

    if not new_sub or new_sub in _generic:
        new_sub = NewLookScraper._subcategory_from_path(url_path)
        method = 'url_path'

    if not new_sub or new_sub in _generic:
        if gender == 'men':
            new_sub = NewLookScraper._style_from_name_mens(name)
        else:
            new_sub = NewLookScraper._style_from_name(name)
        method = 'name'

    new_subs[new_sub or 'None'] += 1
    method_used[method] += 1
    if new_sub != (p.get('subcategory') or ''):
        samples.append((name[:55], p.get('subcategory'), new_sub, method))

print(f'\n=== What new logic would assign ===')
for sub, n in new_subs.most_common():
    print(f'  {repr(sub):40s} {n}')

print(f'\n=== Method used ===')
for m, n in method_used.most_common():
    print(f'  {m}: {n}')

print(f'\n=== Sample changes (first 20) ===')
for name, old, new, method in samples[:20]:
    print(f'  {name:<55}  {repr(old)} → {repr(new)}  [{method}]')
print(f'  Total would change: {len(samples)}')
