"""
backfill_newlook_subcategories.py

One-off script to re-derive subcategories for all New Look products
using the updated logic:
  1. Category sitemap  — skip if result is 'shoes'
  2. URL path parsing  — skip if result is 'shoes'
  3. Product name      — last resort

Run from the mrd-tracker directory:
    python backfill_newlook_subcategories.py

Add --dry-run to preview without writing.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from scrapers.newlook import NewLookScraper
from config import DATABASE_PATH
import database as db
import sqlite3

DRY_RUN = '--dry-run' in sys.argv

# ── Load category sitemap prefixes ───────────────────────────────────────────
print('Fetching New Look category sitemap…')
style_prefixes = NewLookScraper._fetch_style_categories()
print(f'  Loaded {len(style_prefixes)} style prefixes\n')

# ── Load all New Look products from DB ───────────────────────────────────────
products = db.get_all_products(retailer='newlook')
print(f'Found {len(products)} New Look products in DB\n')

# ── Re-derive subcategories ───────────────────────────────────────────────────
changes   = []
unchanged = []

for p in products:
    url      = p.get('url', '')
    name     = p.get('name', '')
    old_sub  = p.get('subcategory') or ''
    url_path = url.replace('https://www.newlook.com', '')

    gender = NewLookScraper._gender_from_path(url_path)

    # Method 1: category sitemap
    new_sub = NewLookScraper._style_from_category_prefix(url_path, style_prefixes)
    if not new_sub or new_sub == 'shoes':
        # Method 2: URL path
        new_sub = NewLookScraper._subcategory_from_path(url_path)
    if not new_sub or new_sub == 'shoes':
        # Method 3: product name (gender-aware)
        if gender == 'men':
            new_sub = NewLookScraper._style_from_name_mens(name)
        else:
            new_sub = NewLookScraper._style_from_name(name)

    if new_sub and new_sub != old_sub:
        changes.append({
            'id':      p['id'],
            'sku':     p.get('sku', ''),
            'name':    name,
            'old_sub': old_sub,
            'new_sub': new_sub,
        })
    else:
        unchanged.append(name)

# ── Report ────────────────────────────────────────────────────────────────────
print(f'=== Results {"(DRY RUN) " if DRY_RUN else ""}===')
print(f'  Will update : {len(changes)}')
print(f'  Unchanged   : {len(unchanged)}')

if changes:
    print(f'\nSample changes (first 20):')
    for c in changes[:20]:
        print(f'  [{c["sku"]}] {c["name"][:55]:<55}  {repr(c["old_sub"])} → {repr(c["new_sub"])}')
    if len(changes) > 20:
        print(f'  … and {len(changes) - 20} more')

# ── Write to DB ───────────────────────────────────────────────────────────────
if not DRY_RUN and changes:
    print(f'\nWriting {len(changes)} updates to DB…')
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    for row in changes:
        c.execute('UPDATE products SET subcategory=? WHERE id=?',
                  (row['new_sub'], row['id']))
    conn.commit()
    conn.close()
    print('Done.')
elif DRY_RUN:
    print('\n(Dry run — no changes written)')
