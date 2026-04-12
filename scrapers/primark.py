"""
scrapers/primark.py — Primark scraper using Playwright + API response interception.

Primark's API requires session cookies set by their bot-protection layer on page load.
We use a real browser (Playwright) to load the category page, which sets the cookies
automatically, then intercept the JSON API responses as the page scrolls and loads
more products.

This avoids both HTML parsing (fragile) and direct API calls (blocked by 403).
The intercepted responses contain clean, structured product JSON — title, price,
image URL, SKU — with no HTML parsing required.

IF PRODUCTS STOP LOADING:
  Usually means the page structure changed or bot detection tightened.
  Run: python run.py --discover primark
  to open the browser visually and check what's happening.
"""

import json
import time
import logging

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class PrimarkScraper(BaseScraper):

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def scrape_category(self, category_path):
        """
        Load a Primark category page in a real browser, intercept all
        getPlpProducts API responses as the page scrolls, and return
        the full list of products as standard dicts.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error('Playwright not installed. Run: pip install playwright && playwright install chromium')
            return []

        slug = category_path.split('/en-gb/c/')[-1].strip('/')
        url  = f'https://www.primark.com/en-gb/c/{slug}'

        all_docs = []
        total    = None

        def on_response(response):
            nonlocal total
            if 'getPlpProducts' not in response.url:
                return
            try:
                data          = response.json()
                products_data = (
                    data
                    .get('data', {})
                    .get('categoryNavItem', {})
                    .get('props', {})
                    .get('productsData', {})
                    .get('response', {})
                )
                docs  = products_data.get('docs', [])
                total = products_data.get('numFound', total)
                all_docs.extend(docs)
                self.log(f'Intercepted {len(docs)} products (running total: {len(all_docs)} / {total})')
            except Exception as e:
                self.warn(f'Failed to parse intercepted response: {e}')

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
            page.on('response', on_response)

            try:
                self.log(f'Loading: {url}')
                page.goto(url, wait_until='networkidle', timeout=40000)
                self._dismiss_cookie_banner(page)
                page.wait_for_timeout(3000)

                # Scroll repeatedly to trigger the site's own pagination API calls.
                # Each scroll loads the next batch of 24 products.
                max_scrolls = 25      # Safety cap — 25 × 24 = 600 products max
                for i in range(max_scrolls):
                    if total and len(all_docs) >= total:
                        self.log(f'All {total} products loaded.')
                        break
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                    page.wait_for_timeout(1800)
                    self.log(f'Scroll {i + 1} complete ({len(all_docs)} products so far)')

            except Exception as e:
                self.warn(f'Error loading {url}: {e}')
            finally:
                browser.close()

        if not all_docs:
            self.warn(
                f'No products intercepted for {slug}. '
                'The page may not be loading products — run --discover primark to investigate.'
            )
            return []

        # Convert raw API dicts to our standard product format
        products = []
        for rank, item in enumerate(all_docs, start=1):
            product = self._parse_product(item, category_path, rank)
            if product:
                products.append(product)

        return products

    def scrape_product(self, product_url):
        """Primark does not expose size/stock data via their public site."""
        return None

    # -----------------------------------------------------------------------
    # Product parsing
    # -----------------------------------------------------------------------

    def _parse_product(self, item, category_path, rank):
        """Convert a raw API product dict to our standard format."""
        pid = str(item.get('pid', '')).strip()
        if not pid:
            return None

        url_slug = item.get('url', '').strip()
        if not url_slug:
            return None

        url = f'https://www.primark.com/en-gb/p/{url_slug}'

        # Price is in pence: 1000 = £10.00
        price_pence = item.get('price')
        price = round(price_pence / 100.0, 2) if price_pence else None

        # Image from Amplience CDN at 400px width
        thumb     = item.get('thumb_image', '').strip()
        image_url = f'{thumb}?w=400&fmt=auto' if thumb else None

        category = category_path.split('/en-gb/c/')[-1].strip('/')

        return {
            'sku':             pid,
            'name':            self.clean_text(item.get('title', 'Unknown')),
            'url':             url,
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
            },
        }

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _dismiss_cookie_banner(self, page):
        """Try common cookie accept button patterns."""
        selectors = [
            'button[id*="accept"]',
            'button[data-testid*="accept"]',
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("I accept")',
            '#onetrust-accept-btn-handler',
        ]
        for sel in selectors:
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
        Open a visible browser window and print intercepted product data.
        Useful for debugging — run with: python run.py --discover primark
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print('Playwright not installed.')
            return

        captured = []

        def on_response(response):
            if 'getPlpProducts' not in response.url:
                return
            try:
                data          = response.json()
                products_data = (
                    data
                    .get('data', {})
                    .get('categoryNavItem', {})
                    .get('props', {})
                    .get('productsData', {})
                    .get('response', {})
                )
                docs  = products_data.get('docs', [])
                total = products_data.get('numFound')
                captured.extend(docs)
                print(f'Intercepted {len(docs)} products (total available: {total})')
            except Exception as e:
                print(f'Parse error: {e}')

        print('\n=== Primark discovery mode (visible browser) ===\n')
        print('Opening browser — watch for products loading...\n')

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
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
            page.on('response', on_response)
            page.goto(
                'https://www.primark.com/en-gb/c/women/shoes',
                wait_until='networkidle',
                timeout=40000,
            )
            self._dismiss_cookie_banner(page)
            page.wait_for_timeout(3000)
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            page.wait_for_timeout(4000)
            browser.close()

        if captured:
            print(f'\nSuccess! Captured {len(captured)} products.')
            print('\nSample product:\n')
            print(json.dumps(captured[0], indent=2))
        else:
            print('\nNo products captured.')
            print('The page may be blocking the headless browser.')
            print('Check if shoe products were visible in the browser window when it opened.')
