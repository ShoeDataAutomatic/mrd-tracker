"""
database.py — SQLite setup and all data access functions.

Tables:
  products   — one row per unique product (retailer + SKU)
  snapshots  — one row per daily scrape result per product
  scores     — one row per product per day, calculated by scorer.py
"""

import sqlite3
import json
from datetime import datetime, date
from config import DATABASE_PATH


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')   # Better concurrent read performance
    return conn


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_db():
    """Create tables if they don't exist. Safe to run on every startup."""
    conn = get_connection()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            retailer           TEXT NOT NULL,
            sku                TEXT NOT NULL,
            name               TEXT,
            url                TEXT NOT NULL,
            category           TEXT,
            image_url          TEXT,
            image_refreshed_at TEXT,
            first_seen         TEXT NOT NULL,
            last_seen          TEXT,
            UNIQUE(retailer, sku)
        )
    ''')

    # Migration: add image_refreshed_at to existing databases that predate this column
    try:
        c.execute('ALTER TABLE products ADD COLUMN image_refreshed_at TEXT')
        conn.commit()
    except Exception:
        pass   # Column already exists — safe to ignore

    c.execute('''
        CREATE TABLE IF NOT EXISTS snapshots (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id        INTEGER NOT NULL,
            timestamp         TEXT NOT NULL,
            price             REAL,
            rank              INTEGER,     -- position on category page (1 = top)
            review_count      INTEGER,
            sizes_available   TEXT,        -- JSON array e.g. ["4","5","6"]
            sizes_oos         TEXT,        -- JSON array of out-of-stock sizes
            is_featured       INTEGER DEFAULT 0,
            raw_data          TEXT,        -- JSON blob for any extra fields
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS scores (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id  INTEGER NOT NULL,
            scored_date TEXT NOT NULL,
            score       REAL DEFAULT 0,
            signals     TEXT,             -- JSON object of signal breakdown
            UNIQUE(product_id, scored_date),
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    ''')

    # Index for fast score lookups
    c.execute('CREATE INDEX IF NOT EXISTS idx_scores_date ON scores(scored_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_product ON snapshots(product_id)')

    conn.commit()
    conn.close()
    print('[db] Initialised.')


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

def upsert_product(retailer, sku, name, url, category, image_url=None):
    """Insert or update a product. Returns the product's id."""
    conn = get_connection()
    c = conn.cursor()
    now = datetime.utcnow().isoformat()

    # Only update image_refreshed_at when we actually have a fresh image URL
    if image_url:
        c.execute('''
            INSERT INTO products (retailer, sku, name, url, category, image_url, image_refreshed_at, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(retailer, sku) DO UPDATE SET
                name               = excluded.name,
                image_url          = excluded.image_url,
                image_refreshed_at = excluded.image_refreshed_at,
                last_seen          = excluded.last_seen
        ''', (retailer, sku, name, url, category, image_url, now, now, now))
    else:
        c.execute('''
            INSERT INTO products (retailer, sku, name, url, category, image_url, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(retailer, sku) DO UPDATE SET
                name      = excluded.name,
                last_seen = excluded.last_seen
        ''', (retailer, sku, name, url, category, image_url, now, now))

    conn.commit()
    c.execute('SELECT id FROM products WHERE retailer=? AND sku=?', (retailer, sku))
    row = c.fetchone()
    conn.close()
    return row['id']


def update_product_image(retailer, sku, image_url):
    """
    Update only the image URL and refresh timestamp for a product.
    Used by the image refresher — does not touch any other fields.
    """
    if not image_url:
        return
    conn = get_connection()
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    c.execute('''
        UPDATE products
        SET image_url = ?, image_refreshed_at = ?
        WHERE retailer = ? AND sku = ?
    ''', (image_url, now, retailer, sku))
    conn.commit()
    conn.close()


def get_all_products(retailer=None):
    conn = get_connection()
    c = conn.cursor()
    if retailer:
        c.execute('SELECT * FROM products WHERE retailer=? ORDER BY last_seen DESC', (retailer,))
    else:
        c.execute('SELECT * FROM products ORDER BY last_seen DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_product(product_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM products WHERE id=?', (product_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_stale_image_products(retailer=None, stale_hours=20):
    """
    Return products whose image URL hasn't been refreshed within stale_hours.
    These are the candidates for the image refresh job.
    """
    conn = get_connection()
    c = conn.cursor()
    retailer_filter = 'AND retailer = ?' if retailer else ''
    params = [f'-{stale_hours} hours']
    if retailer:
        params.append(retailer)

    c.execute(f'''
        SELECT id, retailer, sku, url, image_url, image_refreshed_at
        FROM products
        WHERE (
            image_refreshed_at IS NULL
            OR image_refreshed_at < datetime('now', ?)
        )
        AND last_seen >= date('now', '-3 days')
        {retailer_filter}
        ORDER BY image_refreshed_at ASC
    ''', params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows
    conn = get_connection()
    c = conn.cursor()
    if retailer:
        c.execute('SELECT * FROM products WHERE retailer=? ORDER BY last_seen DESC', (retailer,))
    else:
        c.execute('SELECT * FROM products ORDER BY last_seen DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def mark_product_unseen(product_id):
    """Update last_seen so the scorer can detect disappearances."""
    # We deliberately do NOT update last_seen here —
    # a stale last_seen means the product wasn't found today.
    pass


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def save_snapshot(product_id, price=None, rank=None, review_count=None,
                  sizes_available=None, sizes_oos=None,
                  is_featured=False, raw_data=None):
    conn = get_connection()
    c = conn.cursor()
    now = datetime.utcnow().isoformat()

    c.execute('''
        INSERT INTO snapshots
            (product_id, timestamp, price, rank, review_count,
             sizes_available, sizes_oos, is_featured, raw_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        product_id, now, price, rank, review_count,
        json.dumps(sizes_available or []),
        json.dumps(sizes_oos or []),
        1 if is_featured else 0,
        json.dumps(raw_data) if raw_data else None,
    ))

    conn.commit()
    conn.close()


def get_snapshots(product_id, limit=10):
    """Return the N most recent snapshots for a product, newest first."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        SELECT * FROM snapshots
        WHERE product_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    ''', (product_id, limit))
    rows = []
    for r in c.fetchall():
        d = dict(r)
        d['sizes_available'] = json.loads(d['sizes_available'] or '[]')
        d['sizes_oos']       = json.loads(d['sizes_oos'] or '[]')
        rows.append(d)
    conn.close()
    return rows


def get_latest_snapshot(product_id):
    snaps = get_snapshots(product_id, limit=1)
    return snaps[0] if snaps else None


def get_previous_snapshot(product_id):
    snaps = get_snapshots(product_id, limit=2)
    return snaps[1] if len(snaps) >= 2 else None


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------

def save_score(product_id, score, signals):
    conn = get_connection()
    c = conn.cursor()
    today = date.today().isoformat()

    c.execute('''
        INSERT OR REPLACE INTO scores (product_id, scored_date, score, signals)
        VALUES (?, ?, ?, ?)
    ''', (product_id, today, score, json.dumps(signals)))

    conn.commit()
    conn.close()


def get_top_products(limit=50, days=30, retailer=None):
    """
    Return top N products ranked by cumulative score over the past `days` days.
    Each result includes the product fields plus total_score and latest snapshot data.
    """
    conn = get_connection()
    c = conn.cursor()

    retailer_filter = 'AND p.retailer = ?' if retailer else ''
    params = [f'-{days} days', limit]
    if retailer:
        params.insert(1, retailer)

    c.execute(f'''
        SELECT
            p.*,
            COALESCE(SUM(s.score), 0) AS total_score,
            sn.price         AS latest_price,
            sn.rank          AS latest_rank,
            sn.review_count  AS latest_reviews,
            sn.is_featured   AS latest_featured,
            sn.timestamp     AS latest_snapshot_time
        FROM products p
        LEFT JOIN scores s
            ON s.product_id = p.id
            AND s.scored_date >= date('now', ?)
        LEFT JOIN (
            SELECT product_id, price, rank, review_count, is_featured, timestamp,
                   ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY timestamp DESC) AS rn
            FROM snapshots
        ) sn ON sn.product_id = p.id AND sn.rn = 1
        WHERE 1=1 {retailer_filter}
        GROUP BY p.id
        ORDER BY total_score DESC
        LIMIT ?
    ''', params)

    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_score_history(product_id, days=30):
    """Return daily scores for a product over the past N days."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        SELECT scored_date, score, signals
        FROM scores
        WHERE product_id = ?
          AND scored_date >= date('now', ?)
        ORDER BY scored_date ASC
    ''', (product_id, f'-{days} days'))
    rows = []
    for r in c.fetchall():
        d = dict(r)
        d['signals'] = json.loads(d['signals'] or '{}')
        rows.append(d)
    conn.close()
    return rows
