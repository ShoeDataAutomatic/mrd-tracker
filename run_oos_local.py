"""
run_oos_local.py — Local OOS + price checker for New Look and Primark.

Fetches the product list from Railway, checks availability from this machine
(residential IP bypasses PerimeterX), and posts results back via the API.

Setup:
  1. Copy oos_config.env.example to oos_config.env and fill in RAILWAY_URL + OOS_API_KEY
  2. Run: python run_oos_local.py
  3. Or schedule via Windows Task Scheduler (see README or the schedule script)

New Look:  requests.get() per product page → schema.org availability + price
           Runs 10 threads in parallel.

Primark:   Playwright browser → NonStoreSpecificColourAvailability API
           20 parallel fetch()s per browser evaluate call.
"""

import os
import re
import sys
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────

_CFG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'oos_config.env')
if os.path.exists(_CFG_FILE):
    with open(_CFG_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

RAILWAY_URL = os.environ.get('RAILWAY_URL', 'https://web-production-c511e.up.railway.app').rstrip('/')
OOS_API_KEY = os.environ.get('OOS_API_KEY', '')

if not OOS_API_KEY:
    print('ERROR: OOS_API_KEY not set. Add it to oos_config.env.')
    sys.exit(1)

_API_HEADERS = {'Authorization': f'Bearer {OOS_API_KEY}', 'Content-Type': 'application/json'}

_NL_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept':          'text/html,application/xhtml+xml,*/*',
    'Accept-Language': 'en-GB,en;q=0.9',
}

_PX_MARKERS = ('px-captcha', 'PerimeterX', '_pxParam', 'Access to this page has been denied')


# ── Railway API helpers ─────────────────────────────────────────────────────

def get_products(retailer):
    r = requests.get(
        f'{RAILWAY_URL}/api/oos/products',
        params={'retailer': retailer},
        headers=_API_HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()['products']


def post_updates(updates, batch_size=500):
    """POST results back in batches to avoid request-size limits."""
    total_updated = 0
    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        r = requests.post(
            f'{RAILWAY_URL}/api/oos/update',
            json={'updates': batch},
            headers=_API_HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        total_updated += r.json().get('updated', 0)
    return total_updated


# ── New Look ────────────────────────────────────────────────────────────────

def _check_newlook_one(product):
    """Returns dict with is_oos and price, or None on failure/block."""
    url = product.get('url', '')
    if not url:
        return None
    try:
        r = requests.get(url, headers=_NL_HEADERS, timeout=12)
        if r.status_code != 200:
            return None
        if any(m in r.text for m in _PX_MARKERS):
            return None

        avail_m = re.search(r'"availability"\s*:\s*"(https://schema\.org/[^"]+)"', r.text)
        is_oos  = bool(avail_m and 'OutOfStock' in avail_m.group(1))

        price_m = re.search(r'"price"\s*:\s*"?([\d.]+)"?', r.text)
        price   = float(price_m.group(1)) if price_m else None

        return {'retailer': 'newlook', 'sku': product['sku'], 'is_oos': is_oos, 'price': price}
    except Exception:
        return None


def run_newlook_oos():
    print(f'\n=== New Look OOS + price check  [{datetime.now():%H:%M:%S}] ===')
    products = get_products('newlook')
    print(f'  {len(products)} products to check')

    updates  = []
    ok       = 0
    blocked  = 0
    t0       = time.time()

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_check_newlook_one, p): p for p in products}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                updates.append(result)
                ok += 1
            else:
                blocked += 1
            if i % 200 == 0 or i == len(products):
                elapsed = time.time() - t0
                print(f'  {i}/{len(products)}  ok={ok}  blocked/failed={blocked}  {elapsed:.0f}s')

    oos_count = sum(1 for u in updates if u['is_oos'])
    print(f'  Results: {ok} fetched, {blocked} blocked, {oos_count} OOS')

    if updates:
        n = post_updates(updates)
        print(f'  Posted {n}/{len(updates)} updates to Railway')


# ── Primark ─────────────────────────────────────────────────────────────────

def _get_style_code(url):
    m = re.search(r'-(\d{12})$', (url or '').rstrip('/'))
    return m.group(1)[:9] if m else None


def run_primark_oos():
    print(f'\n=== Primark OOS check  [{datetime.now():%H:%M:%S}] ===')

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('  ERROR: Playwright not installed. Run: pip install playwright && playwright install chromium')
        return

    from urllib.parse import urlparse, parse_qs, unquote

    products  = get_products('primark')
    checkable = [p for p in products if _get_style_code(p.get('url', ''))]
    print(f'  {len(checkable)}/{len(products)} products have valid style codes')

    if not checkable:
        return

    captured = {'extensions': None}

    def on_response(response):
        if 'NonStoreSpecificColourAvailability' not in response.url:
            return
        try:
            params  = parse_qs(urlparse(response.url).query)
            ext_raw = params.get('extensions', [''])[0]
            if ext_raw and not captured['extensions']:
                captured['extensions'] = unquote(ext_raw)
        except Exception:
            pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled'],
        )
        ctx = browser.new_context(
            user_agent=_NL_HEADERS['User-Agent'],
            viewport={'width': 1280, 'height': 900},
        )
        ctx.add_init_script(
            'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
        )
        page = ctx.new_page()
        page.on('response', on_response)

        # Navigate to first product page to capture persisted query hash
        first_url = checkable[0]['url']
        print(f'  Loading product page for hash capture...')
        try:
            page.goto(first_url, wait_until='networkidle', timeout=45000)
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f'  Page load error: {e}')

        if not captured['extensions']:
            print('  Could not capture API hash — aborting Primark OOS check')
            browser.close()
            return

        # Warm up api001-arh.primark.com so PX sets a cookie for that domain
        print('  Warming up API subdomain...')
        api_page = ctx.new_page()
        try:
            api_page.goto(
                'https://api001-arh.primark.com/bff-cae-green'
                '?operationName=NonStoreSpecificColourAvailability'
                '&variables=%7B%22locale%22%3A%22en-gb%22%2C%22styleCode%22%3A%22%22%7D',
                wait_until='networkidle',
                timeout=20000,
            )
            api_page.wait_for_timeout(2000)
        except Exception as e:
            print(f'  API warmup error (continuing): {e}')
        api_page.close()

        extensions_str = captured['extensions']
        PARALLEL       = 20
        avail_map      = {}   # styleCode -> bool (True = at least one size in stock)
        t0             = time.time()

        for i in range(0, len(checkable), PARALLEL):
            batch       = checkable[i:i + PARALLEL]
            style_codes = [_get_style_code(p['url']) for p in batch]
            sc_js       = json.dumps(style_codes)

            js = f"""
(async () => {{
    const styleCodes = {sc_js};
    const ext = {json.dumps(extensions_str)};
    const results = await Promise.all(styleCodes.map(async (sc) => {{
        const url = new URL('https://api001-arh.primark.com/bff-cae-green');
        url.searchParams.set('operationName', 'NonStoreSpecificColourAvailability');
        url.searchParams.set('variables', JSON.stringify({{locale: 'en-gb', styleCode: sc}}));
        url.searchParams.set('extensions', ext);
        try {{
            const resp = await fetch(url.toString(), {{
                headers: {{'accept': 'application/json', 'accept-language': 'en-GB,en;q=0.9'}},
                credentials: 'include'
            }});
            return {{sc, body: await resp.text()}};
        }} catch (e) {{
            return {{sc, body: ''}};
        }}
    }}));
    return results;
}})()
"""
            try:
                results = page.evaluate(js)
            except Exception as e:
                print(f'  Batch error at {i}: {e}')
                continue

            px_hit = False
            for r in results:
                sc   = r.get('sc', '')
                body = r.get('body', '')
                if not body:
                    continue
                if '<!DOCTYPE' in body or 'px-captcha' in body:
                    px_hit = True
                    break
                try:
                    data  = json.loads(body)
                    inv   = ((data.get('data') or {})
                             .get('nonStoreColorSelectorInventory') or {})
                    sizes = inv.get('skuColorAvailability') or []
                    if sizes:
                        avail_map[sc] = any(s.get('isAvailable') for s in sizes)
                except Exception:
                    pass

            if px_hit:
                print('  Blocked by PerimeterX — stopping Primark OOS check')
                break

            done = min(i + PARALLEL, len(checkable))
            if done % 100 == 0 or done == len(checkable):
                print(f'  {done}/{len(checkable)} checked  {time.time() - t0:.0f}s')

        browser.close()

    updates   = []
    oos_count = 0
    for p in checkable:
        sc = _get_style_code(p['url'])
        if sc not in avail_map:
            continue
        is_oos = not avail_map[sc]
        updates.append({'retailer': 'primark', 'sku': p['sku'], 'is_oos': is_oos})
        if is_oos:
            oos_count += 1

    print(f'  Results: {len(updates)} checked, {oos_count} OOS, {len(checkable) - len(updates)} no data')
    if updates:
        n = post_updates(updates)
        print(f'  Posted {n}/{len(updates)} updates to Railway')


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Local OOS checker')
    parser.add_argument('--retailer', choices=['newlook', 'primark', 'both'], default='both')
    args = parser.parse_args()

    if args.retailer in ('newlook', 'both'):
        run_newlook_oos()
    if args.retailer in ('primark', 'both'):
        run_primark_oos()

    print(f'\nDone [{datetime.now():%H:%M:%S}]')
