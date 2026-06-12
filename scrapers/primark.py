"""
scrapers/primark.py — Primark scraper using scroll-based product loading.

Strategy:
  Primark's React app loads products 24 at a time as the user scrolls.
  We open the category page in a headless browser, capture each
  getPlpProducts API response, then scroll to the bottom repeatedly until
  all products are loaded.  This is more reliable than URL interception
  because it works regardless of how Primark encodes its request parameters.
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
        total    = self._load_all_by_scroll(slug, url, all_docs)

        self.log(f'Complete: {len(all_docs)} products for {slug}')

        products = []
        for rank, item in enumerate(all_docs, start=1):
            product = self._parse_product(item, category_path, rank, category_label, subcategory)
            if product:
                products.append(product)
        return products

    def _load_all_by_scroll(self, slug, url, all_docs):
        """
        Open the category page once, capture every getPlpProducts response,
        and scroll down until all products are loaded.

        Returns the total product count reported by the API (or None on error).
        No URL interception — works regardless of how Primark encodes its params.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        total    = [None]
        seen_pids = set()

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
                    self.log(
                        f'Scroll batch: +{len(new_docs)} new products ({len(all_docs)}/{total[0]})'
                    )
            except Exception as e:
                self.warn(f'Response parse error: {e}')

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
            page.on('response', on_response)

            try:
                page.goto(url, wait_until='networkidle', timeout=40000)
                self._dismiss_cookie_banner(page)
                page.wait_for_timeout(2000)

                # Scroll to bottom repeatedly until all products are loaded
                no_new_streak = 0
                for _ in range(60):  # safety cap: max 60 scroll attempts
                    if total[0] and len(all_docs) >= total[0]:
                        break
                    prev = len(all_docs)
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                    page.wait_for_timeout(1800)
                    if len(all_docs) == prev:
                        no_new_streak += 1
                        if no_new_streak >= 3:
                            break
                    else:
                        no_new_streak = 0

            except Exception as e:
                self.warn(f'Browser load error: {e}')
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

        price_pence = item.get('price')
        price       = round(price_pence / 100.0, 2) if price_pence else None
        prev_pence  = item.get('pricePrevious')
        was_price   = round(prev_pence / 100.0, 2) if prev_pence else None
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
        """Test the multi-batch loader. Run with: python run.py --discover primark"""
        slug = 'women/shoes/heels'
        url  = f'https://www.primark.com/en-gb/c/{slug}'

        print('\n=== Primark discovery mode (scroll) ===\n')
        print(f'Loading {slug} via scroll...\n')

        all_docs = []
        total = self._load_all_by_scroll(slug, url, all_docs)
        print(f'\nFinal result: {len(all_docs)}/{total} products loaded')
        if total and len(all_docs) >= total:
            print('SUCCESS — all products loaded.')
        else:
            print(f'WARNING — only got {len(all_docs)} of {total}.')

        if all_docs:
            print(f'\nSample product:\n{json.dumps(all_docs[0], indent=2)}')
