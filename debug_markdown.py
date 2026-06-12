"""
Run this locally to diagnose markdown detection:
    python debug_markdown.py
"""
import sqlite3, json, os

DB_PATH = 'mrd_tracker.db'

if not os.path.exists(DB_PATH):
    print(f"Database not found at {DB_PATH}")
    print("Run from the mrd-tracker directory.")
    exit(1)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Last scrape time
c.execute("SELECT MAX(timestamp) FROM snapshots")
print(f"Last snapshot: {c.fetchone()[0]}\n")

# All retailers
c.execute("SELECT retailer, COUNT(*) FROM products GROUP BY retailer")
for r in c.fetchall():
    print(f"Products - {r[0]}: {r[1]}")

# Primark: check latest snapshot raw_data for markdown flags
c.execute("""
    SELECT p.name, p.category, sn.price, sn.raw_data, sn.timestamp
    FROM products p
    JOIN (
        SELECT product_id, price, raw_data, timestamp,
               ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY timestamp DESC) AS rn
        FROM snapshots
    ) sn ON sn.product_id = p.id AND sn.rn = 1
    WHERE p.retailer = 'primark'
    ORDER BY sn.timestamp DESC
""")
rows = c.fetchall()
print(f"\nPrimark products with a snapshot: {len(rows)}")
print(f"Sample snapshot time: {rows[0]['timestamp'][:16] if rows else 'none'}\n")

flagged   = []  # is_markdown=True in raw_data
missed    = []  # was_price > price but not flagged
no_was    = []  # was_price stored but equals price (noise)

for r in rows:
    try:
        raw = json.loads(r['raw_data'] or '{}')
    except Exception:
        raw = {}

    is_md  = raw.get('is_markdown', False)
    was    = raw.get('was_price')
    curr   = r['price']

    if is_md:
        flagged.append((r['name'], r['category'], curr, was))
    elif was and curr and float(was) > float(curr):
        missed.append((r['name'], r['category'], curr, was))

print(f"=== is_markdown=True in DB ({len(flagged)}) ===")
for name, cat, curr, was in flagged:
    print(f"  {name} | cat={cat} | £{curr:.2f} was £{was}")

print(f"\n=== was_price > price but NOT flagged ({len(missed)}) ===")
for name, cat, curr, was in missed:
    print(f"  {name} | cat={cat} | £{curr:.2f} was £{was}")

if not flagged and not missed:
    print("  (none — all prices equal pricePrevious)")

# Check if raw_data even contains 'was_price' key
c.execute("""
    SELECT sn.raw_data FROM products p
    JOIN (
        SELECT product_id, raw_data,
               ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY timestamp DESC) AS rn
        FROM snapshots
    ) sn ON sn.product_id = p.id AND sn.rn = 1
    WHERE p.retailer = 'primark'
    LIMIT 5
""")
print("\n=== Sample raw_data keys (first 5 Primark products) ===")
for r in c.fetchall():
    try:
        d = json.loads(r[0] or '{}')
        print(f"  keys={list(d.keys())}  is_markdown={d.get('is_markdown')}  was_price={d.get('was_price')}")
    except Exception as e:
        print(f"  parse error: {e}")

conn.close()
