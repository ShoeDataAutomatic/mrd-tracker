"""
scrapers/primark.py — Primark scraper using capture-and-replay pagination.

Strategy:
  Phase 1 — Browser: load the category page once in a headless browser.
    The browser fires a real getPlpProducts GET request to Primark's API.
    We capture the request URL (which contains all auth/locale/query params)
    and the first 24 products from the response.

  Phase 2 — Direct HTTP: for every remaining page we decode the captured
    URL, update the `start` offset in the JSON `variables` parameter, and
    replay the GET request directly via the requests library.
    No browser needed — each page is a single HTTP call (~200 ms).

  This is reliable regardless of scroll mechanics or endpoint naming, and
  automatically picks up any endpoint change (blue/green) without config edits.
"""

import re
import json
import time
import logging
import requests as _http
from urllib.parse import urlparse, parse_qs, urlencode

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class PrimarkScraper(BaseScraper):

    def __init__(self, config):
        super().__init__(config)
        api            = config.get('api', {})
        self.page_size = api.get('page_size', 24)
        self.rows_override = api.get('rows_override', 500)

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def scrape_category(self, category_path):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error('Playwright not installed.')
            return []

        slug = category_path.split('/en-gb/c/')[-1].strip('/')
        url  = f'https://www.primark.com/en-gb/c/{slug}'

        # Derive top-level category label and subcategory from the slug.
        # e.g. 'women/shoes/heels'           → category='women', subcategory='heels'
        #      'kids/girls/girls-shoes/boots' → category='girls', subcategory='boots'
        #      'women/shoes'                  → category='women',  subcategory=None
        parts  = slug.split('/')
        gender = parts[0] if parts else 'unknown'
        if gender == 'kids' and len(parts) > 1:
            gender = parts[1]   # 'girls' or 'boys'
        category_label = gender   # 'women', 'men', 'girls', 'boys'

        _base_slugs = {'women/shoes', 'men/shoes', 'kids/girls/girls-shoes', 'kids/boys/boys-shoes'}
        subcategory = None if slug in _base_slugs else parts[-1].replace('-', ' ')

        all_docs = []
        total    = self._load_all_by_capture_and_replay(slug, url, all_docs)

        self.log(f'Complete: {len(all_docs)} products for {slug}')

        products = []
        for rank, item in enumerate(all_docs, start=1):
            product = self._parse_product(item, category_path, rank, category_label, subcategory)
            if product:
                products.append(product)
        return products

    def _load_all_by_capture_and_replay(self, slug, url, all_docs):
        """
        Phase 1: browser loads the page, captures the getPlpProducts request
                 URL and returns the first 24 products.
        Phase 2: direct HTTP GET calls (via requests) paginate through the
                 remaining products by incrementing the `start` variable.

        Returns the total product count reported by the API.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        captured  = [None]   # will hold {'url': ..., 'headers': ...}
        total     = [None]
        seen_pids = set()

        def on_route(route, request):
            if 'getPlpProducts' in request.url and captured[0] is None:
                captured[0] = {
                    'url':     request.url,
                    'headers': dict(request.headers),
                }
            route.continue_()

        def on_response(response):
            if 'getPlpProducts' not in response.url:
                return
            try:
                data = response.json()
                docs, num = self._extract_docs(data)
                if num and total[0] is None:
                    total[0] = num
                if docs:
                    new_docs = [d for d in docs if d.get('pid') not in seen_pids]
                    for d in new_docs:
                        seen_pids.add(d.get('pid'))
                    all_docs.extend(new_docs)
                    self.log(f'Batch +{len(new_docs)} products ({len(all_docs)}/{total[0]})')
            except Exception as e:
                self.warn(f'Response parse error: {e}')

        # ── Phase 1: browser ──────────────────────────────────────────────
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled'],
            )
            ctx = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1280, 'height': 900},
            )
            ctx.add_init_script(
                'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            )
            page = ctx.new_page()
            page.route('https://api001-arh.primark.com/*', on_route)
            page.on('response', on_response)
            try:
                page.goto(url, wait_until='networkidle', timeout=40000)
                self._dismiss_cookie_banner(page)
                page.wait_for_timeout(2000)
            except Exception as e:
                self.warn(f'Browser load error: {e}')
            finally:
                browser.close()

        if not captured[0] or not total[0]:
            return total[0]

        if len(all_docs) >= total[0]:
            return total[0]

        # ── Phase 2: direct HTTP for remaining pages ──────────────────────
        base_url = captured[0]['url']
        headers  = captured[0]['headers']

        parsed = urlparse(base_url)
        params = parse_qs(parsed.query, keep_blank_values=True)

        if 'variables' not in params:
            self.warn('Cannot find variables param in captured URL — skipping pagination')
            return total[0]

        while len(all_docs) < total[0]:
            try:
                variables = json.loads(params['variables'][0])
                variables['start'] = len(all_docs)
                variables['rows']  = min(100, total[0] - len(all_docs))

                new_params = {k: v[0] for k, v in params.items()}
                new_params['variables'] = json.dumps(variables, separators=(',', ':'))
                page_url = parsed._replace(query=urlencode(new_params)).geturl()

                resp = _http.get(page_url, headers=headers, timeout=20)
                resp.raise_for_status()
                data     = resp.json()
                docs, _  = self._extract_docs(data)

                new_docs = [d for d in (docs or []) if d.get('pid') not in seen_pids]
                if not new_docs:
                    self.warn(f'No new products at start={len(all_docs)} — stopping')
                    break
                for d in new_docs:
                    seen_pids.add(d.get('pid'))
                all_docs.extend(new_docs)
                self.log(f'Direct API +{len(new_docs)} products ({len(all_docs)}/{total[0]})')
                time.sleep(0.3)

            except Exception as e:
                self.warn(f'Direct API error at start={len(all_docs)}: {e}')
                break

        return total[0]


    def scrape_product(self, product_url):
        return None

    # -----------------------------------------------------------------------
    # Scroll fallback
    # -----------------------------------------------------------------------

    def _scroll_for_more(self, page, all_docs, total):
        no_new_streak = 0
        for attempt in range(25):
            if len(all_docs) >= total:
                break
            prev = len(all_docs)
            try:
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(600)
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(600)
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(600)
                page.keyboard.press('End')
                page.wait_for_timeout(2500)
            except Exception:
                break
            if len(all_docs) > prev:
                self.log(f'Scroll loaded {len(all_docs)-prev} more ({len(all_docs)}/{total})')
                no_new_streak = 0
            else:
                no_new_streak += 1
                if no_new_streak >= 3:
                    break

    # -----------------------------------------------------------------------
    # Parsing
    # -----------------------------------------------------------------------

    def _extract_docs(self, data):
        # Primary path — works for broad category pages (women/shoes, men/shoes, etc.)
        d     = (data.get('data')                 or {})
        nav   = (d.get('categoryNavItem')         or {})
        props = (nav.get('props')                 or {})
        pd    = (props.get('productsData')        or {})
        resp  = (pd.get('response')               or {})
        docs  = resp.get('docs')
        num   = resp.get('numFound')
        if docs:
            return docs, num

        # Fallback — recursively search the response tree for a docs list.
        # Handles subcategory pages where Primark uses a different JSON structure.
        docs, num = self._find_docs_recursive(data)
        if docs:
            self.log(f'Used recursive extraction — found {len(docs)} docs')
        return docs or [], num

    def _find_docs_recursive(self, obj, depth=0):
        """Walk the JSON tree looking for a non-empty docs list."""
        if depth > 10 or not isinstance(obj, dict):
            return None, None
        if 'docs' in obj and isinstance(obj.get('docs'), list) and obj['docs']:
            return obj['docs'], obj.get('numFound')
        for v in obj.values():
            if isinstance(v, dict):
                result = self._find_docs_recursive(v, depth + 1)
                if result[0]:
                    return result
        return None, None

    def _parse_product(self, item, category_path, rank, category_label=None, subcategory=None):
        pid = str(item.get('pid', '')).strip()
        if not pid:
            return None
        url_slug = item.get('url', '').strip()
        if not url_slug:
            return None

        # price      = full/original price (RRP)
        # sale_price = actual current selling price (lower than price when on sale)
        # pricePrevious = previous price (for recent adjustments, not always present)
        full_pence  = item.get('price')
        sale_pence  = item.get('sale_price') or full_pence
        prev_pence  = item.get('pricePrevious')

        # Current price is the actual selling price
        price = round(sale_pence / 100.0, 2) if sale_pence else None

        # Was-price: if sale_price < price (original), the full price is the was-price.
        # Otherwise fall back to pricePrevious for recent price drops.
        if full_pence and sale_pence and sale_pence < full_pence:
            was_price = round(full_pence / 100.0, 2)
        elif prev_pence and sale_pence and prev_pence > sale_pence:
            was_price = round(prev_pence / 100.0, 2)
        else:
            was_price = None

        is_markdown = bool(was_price and price and was_price > price)
        thumb       = item.get('thumb_image', '').strip()
        image_url   = f'{thumb}?w=600&fmt=auto' if thumb else None

        # Use provided label, otherwise derive from category_path
        if not category_label:
            slug = category_path.split('/en-gb/c/')[-1].strip('/')
            category_label = '/'.join(slug.split('/')[-2:]) if slug.count('/') >= 2 else slug

        return {
            'sku':             pid,
            'name':            self.clean_text(item.get('title', 'Unknown')),
            'url':             f'https://www.primark.com/en-gb/p/{url_slug}',
            'category':        category_label,
            'subcategory':     subcategory,
            'price':           price,
            'rank':            rank,
            'review_count':    None,
            'sizes_available': [],
            'sizes_oos':       [],
            'is_featured':     rank <= 4,
            'image_url':       image_url,
            'raw_data': {
                'description': item.get('description'),
                'color_count': item.get('colorCount'),
                'brand':       item.get('brand'),
                'was_price':   was_price,
                'is_markdown': is_markdown,
            },
        }

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _dismiss_cookie_banner(self, page):
        for sel in [
            'button[id*="accept"]',
            'button[data-testid*="accept"]',
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            '#onetrust-accept-btn-handler',
        ]:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(800)
                    self.log('Dismissed cookie banner.')
                    return
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Discovery mode
    # -----------------------------------------------------------------------

    def discover(self):
        """
        Capture the real getPlpProducts API request format.
        Run with: python run.py --discover primark
        Fetches ALL pages for a category and scans for markdown products.
        """
        import json as _json
        import requests as _http
        from urllib.parse import urlparse, parse_qs, urlencode
        from playwright.sync_api import sync_playwright

        slug = 'women/shoes/heels'
        url  = f'https://www.primark.com/en-gb/c/{slug}'

        print('\n=== Primark discovery: fetching ALL pages ===\n')
        print(f'URL: {url}\n')

        captured_req  = [None]
        all_docs      = []
        total         = [None]

        def on_route(route, request):
            if 'getPlpProducts' in request.url and captured_req[0] is None:
                captured_req[0] = {'url': request.url, 'headers': dict(request.headers)}
                print(f'Captured: {request.url[:200]}\n')
            route.continue_()

        def on_response(response):
            if 'getPlpProducts' not in response.url:
                return
            try:
                data = response.json()
                docs, num = self._extract_docs(data)
                if num and total[0] is None:
                    total[0] = num
                if docs:
                    all_docs.extend(docs)
                    print(f'Browser page: {len(docs)} products (total={num})')
            except Exception as e:
                print(f'Response parse error: {e}')

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                                        args=['--disable-blink-features=AutomationControlled'])
            ctx = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1280, 'height': 900},
            )
            ctx.add_init_script(
                'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            )
            page = ctx.new_page()
            page.route('https://api001-arh.primark.com/*', on_route)
            page.on('response', on_response)
            try:
                page.goto(url, wait_until='networkidle', timeout=40000)
                self._dismiss_cookie_banner(page)
                page.wait_for_timeout(3000)
            finally:
                browser.close()

        if not captured_req[0]:
            print('WARNING: No API request captured.')
            return

        # Replay remaining pages via direct HTTP
        base_url = captured_req[0]['url']
        headers  = captured_req[0]['headers']
        parsed   = urlparse(base_url)
        params   = parse_qs(parsed.query, keep_blank_values=True)

        while total[0] and len(all_docs) < total[0]:
            variables = _json.loads(params['variables'][0])
            variables['start'] = len(all_docs)
            variables['rows']  = min(100, total[0] - len(all_docs))
            new_params = {k: v[0] for k, v in params.items()}
            new_params['variables'] = _json.dumps(variables, separators=(',', ':'))
            page_url = parsed._replace(query=urlencode(new_params)).geturl()
            # Try both blue/green endpoints
            for ep in ('bff-cae-blue', 'bff-cae-green'):
                page_url2 = page_url.replace('bff-cae-blue', ep).replace('bff-cae-green', ep)
                try:
                    resp = _http.get(page_url2, headers=headers, timeout=20)
                    docs, num = self._extract_docs(resp.json())
                    if docs:
                        all_docs.extend(docs)
                        print(f'HTTP page (start={variables["start"]}): {len(docs)} products via {ep}')
                        break
                except Exception as e:
                    print(f'  {ep} failed: {e}')

        print(f'\nTotal fetched: {len(all_docs)}/{total[0]}')

        # Analyse price fields across all products
        print(f'\n=== Price field analysis across {len(all_docs)} products ===')
        for field in ('price', 'sale_price', 'pricePrevious', 'changePercent'):
            values = [d.get(field) for d in all_docs]
            non_null = [v for v in values if v not in (None, 0)]
            unique   = sorted(set(non_null))[:10]
            print(f'  {field}: {len(non_null)}/{len(all_docs)} non-null/zero — sample values: {unique}')

        # Markdown candidates
        marked_sp  = [d for d in all_docs if d.get('sale_price') and d.get('price') and d['sale_price'] < d['price']]
        marked_pp  = [d for d in all_docs if d.get('pricePrevious') and d.get('price') and d['pricePrevious'] > d['price']]
        marked_cp  = [d for d in all_docs if d.get('changePercent') and d['changePercent'] < 0]
        print(f'\n  sale_price < price:     {len(marked_sp)} products')
        print(f'  pricePrevious > price:  {len(marked_pp)} products')
        print(f'  changePercent < 0:      {len(marked_cp)} products')

        for label, group in [('sale_price<price', marked_sp), ('pricePrevious>price', marked_pp), ('changePercent<0', marked_cp)]:
            if group:
                d = group[0]
                print(f'\nSample [{label}]: {d.get("title")}')
                print(f'  price={d.get("price")}  sale_price={d.get("sale_price")}  pricePrevious={d.get("pricePrevious")}  changePercent={d.get("changePercent")}')
