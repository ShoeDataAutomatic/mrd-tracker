"""
scrapers/primark.py — Primark scraper using route interception.

Key insight: Primark's React app requests products 24 at a time (rows=24).
By intercepting this outgoing request before it's sent and changing rows=24
to rows=500, the server returns all products in a single response. This
completely avoids the pagination problem — one intercepted request, one
response, all products.

If the server caps or rejects rows=500, we fall back to tall-viewport
scrolling to load as many products as possible.
"""

import re
import json
import time
import logging

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

        all_docs = []
        total    = None

        # First load to get total count and first batch
        total = self._load_batch(slug, url, 0, all_docs)
        if not all_docs:
            return []

        # Load remaining batches — one browser load per batch of rows_override
        start = self.rows_override
        while total and start < total:
            self._load_batch(slug, url, start, all_docs)
            start += self.rows_override
            time.sleep(1)  # Brief pause between loads

        self.log(f'Complete: {len(all_docs)} products for {slug}')

        products = []
        for rank, item in enumerate(all_docs, start=1):
            product = self._parse_product(item, category_path, rank)
            if product:
                products.append(product)
        return products

    def _load_batch(self, slug, url, start, all_docs):
        """
        Load the category page once in a fresh browser, intercept the first
        getPlpProducts request and force start=N, rows=rows_override.
        Appends results to all_docs. Returns total product count or None.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        total    = [None]
        fired    = [False]

        def on_route(route, request):
            if 'getPlpProducts' in request.url and not fired[0]:
                fired[0] = True
                new_url = re.sub(r'(%22rows%22%3A)\d+',  lambda m: f'{m.group(1)}{self.rows_override}', request.url)
                new_url = re.sub(r'(%22start%22%3A)\d+', lambda m: f'{m.group(1)}{start}',               new_url)
                self.log(f'Batch load: start={start}, rows={self.rows_override}')
                route.continue_(url=new_url)
            else:
                route.continue_()

        def on_response(response):
            if 'getPlpProducts' not in response.url:
                return
            try:
                data  = response.json()
                docs, num = self._extract_docs(data)
                if num:
                    total[0] = num
                if docs:
                    all_docs.extend(docs)
                    self.log(f'Batch start={start}: got {len(docs)} products ({len(all_docs)}/{total[0]})')
                else:
                    if 'errors' in data:
                        self.warn(f'API errors for start={start}: {data["errors"]}')
            except Exception as e:
                self.warn(f'Response parse error at start={start}: {e}')

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
                viewport={'width': 1280, 'height': 800},
            )
            ctx.add_init_script(
                'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            )
            page = ctx.new_page()
            page.route('https://api001-arh.primark.com/bff-cae-green*', on_route)
            page.on('response', on_response)

            try:
                page.goto(url, wait_until='networkidle', timeout=40000)
                if start == 0:
                    self._dismiss_cookie_banner(page)
                page.wait_for_timeout(3000)
            except Exception as e:
                self.warn(f'Browser load error at start={start}: {e}')
            finally:
                browser.close()

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
        d     = (data.get('data')                 or {})
        nav   = (d.get('categoryNavItem')         or {})
        props = (nav.get('props')                 or {})
        pd    = (props.get('productsData')        or {})
        resp  = (pd.get('response')               or {})
        return resp.get('docs') or [], resp.get('numFound')

    def _parse_product(self, item, category_path, rank):
        pid = str(item.get('pid', '')).strip()
        if not pid:
            return None
        url_slug = item.get('url', '').strip()
        if not url_slug:
            return None

        price_pence = item.get('price')
        price       = round(price_pence / 100.0, 2) if price_pence else None
        prev_pence  = item.get('pricePrevious')
        was_price   = round(prev_pence / 100.0, 2) if prev_pence else None
        is_markdown = bool(was_price and price and was_price > price)
        thumb       = item.get('thumb_image', '').strip()
        image_url   = f'{thumb}?w=400&fmt=auto' if thumb else None
        category    = category_path.split('/en-gb/c/')[-1].strip('/')

        return {
            'sku':             pid,
            'name':            self.clean_text(item.get('title', 'Unknown')),
            'url':             f'https://www.primark.com/en-gb/p/{url_slug}',
            'category':        category,
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
        """Test the multi-batch loader. Run with: python run.py --discover primark"""
        slug = 'women/shoes'
        url  = f'https://www.primark.com/en-gb/c/{slug}'

        print('\n=== Primark discovery mode (multi-batch) ===\n')
        print(f'Loading {slug} in batches of {self.rows_override}...\n')

        all_docs = []

        # Batch 1 — also gets the total count
        total = self._load_batch(slug, url, 0, all_docs)
        print(f'Batch 1 done: {len(all_docs)}/{total} products')

        if not all_docs:
            print('No products returned — check the scraper.')
            return

        # Remaining batches
        start = self.rows_override
        batch = 2
        while total and start < total:
            self._load_batch(slug, url, start, all_docs)
            print(f'Batch {batch} done: {len(all_docs)}/{total} products')
            start += self.rows_override
            batch += 1
            time.sleep(1)

        print(f'\nFinal result: {len(all_docs)}/{total} products loaded')
        if len(all_docs) >= total:
            print('SUCCESS — all products loaded.')
        else:
            print(f'WARNING — only got {len(all_docs)} of {total}.')

        if all_docs:
            print(f'\nSample product:\n{json.dumps(all_docs[0], indent=2)}')
