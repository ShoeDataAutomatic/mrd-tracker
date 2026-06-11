"""
scrapers/newlook.py — New Look scraper.

Strategy:
  1. Use Playwright to load the first category page and intercept all JSON
     responses until one containing a product list is found.
  2. Extract the base API URL and response schema from that first intercept.
  3. Fetch all remaining pages directly via requests (no browser needed) —
     much faster than re-launching a browser per page.
  4. Fall back to HTML pagination scraping if no API is discovered.

New Look runs on SAP Hybris + AngularJS.  The product search API is typically
at a path like /uk/products/search or /uk/category/{id}/products, but the
exact URL is discovered at runtime by intercepting the first live request.
"""

import re
import json
import time
import logging
import requests

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

# Substrings that indicate a JSON response contains product data
_PRODUCT_KEYS = ('products', 'pagination', 'totalNumberOfResults', 'numFound')

# Image CDN — quality raised from default 50 → 80
_IMAGE_BASE = 'https://media2.newlookassets.com/i/newlook/{sku}.jpg?strip=true&qlt=80&w=600'

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept':          'application/json, text/plain, */*',
    'Accept-Language': 'en-GB,en;q=0.9',
    'Referer':         'https://www.newlook.com/',
}


class NewLookScraper(BaseScraper):

    def __init__(self, config):
        super().__init__(config)
        self._api_url_template = None   # Set after first-page discovery

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def scrape_category(self, category_path):
        """
        Scrape a New Look category.  category_path is the full URL path, e.g.
        '/uk/womens/footwear/c/uk-womens-footwear'.
        """
        url = f'https://www.newlook.com{category_path}'
        gender, subcategory_hint = self._parse_category_path(category_path)

        self.log(f'Loading {url} …')

        # Step 1 — launch browser, intercept first product API response
        api_template, first_page_data = self._discover_api(url)

        if not api_template:
            self.warn('Could not discover API endpoint — falling back to HTML pagination.')
            return self._scrape_html_pages(url, gender, subcategory_hint)

        self._api_url_template = api_template
        self.log(f'Discovered API: {api_template}')

        # Step 2 — parse first page (already in hand)
        products_raw, total = self._extract_products_and_total(first_page_data)
        if not products_raw:
            self.warn('First page returned no products.')
            return []

        page_size = len(products_raw)
        total_pages = max(1, -(-total // page_size)) if total else 1   # ceiling division
        self.log(f'Total: {total} products across ~{total_pages} pages (page size {page_size})')

        all_raw = list(products_raw)

        # Step 3 — fetch remaining pages directly (no browser)
        for page_num in range(1, total_pages):
            time.sleep(0.5)
            page_url = api_template.format(page=page_num, page_size=page_size)
            try:
                resp = requests.get(page_url, headers=_HEADERS, timeout=15)
                if resp.status_code != 200:
                    self.warn(f'Page {page_num} returned HTTP {resp.status_code}')
                    break
                data = resp.json()
                items, _ = self._extract_products_and_total(data)
                if not items:
                    self.log(f'Page {page_num}: empty — stopping.')
                    break
                all_raw.extend(items)
                self.log(f'Page {page_num}: +{len(items)} ({len(all_raw)}/{total})')
            except Exception as e:
                self.warn(f'Page {page_num} fetch error: {e}')
                break

        self.log(f'Collected {len(all_raw)} raw products for {category_path}')

        # Step 4 — parse into standard product dicts
        products = []
        for rank, item in enumerate(all_raw, start=1):
            parsed = self._parse_product(item, gender, subcategory_hint, rank)
            if parsed:
                products.append(parsed)

        self.log(f'Parsed {len(products)} products.')
        return products

    def scrape_product(self, product_url):
        return None   # Detail pages not needed for trend tracking

    # -----------------------------------------------------------------------
    # API discovery via Playwright
    # -----------------------------------------------------------------------

    def _discover_api(self, url):
        """
        Open the category page in a headless browser.  Intercept every JSON
        response.  Return (api_url_template, response_data) for the first
        response that looks like a product listing.

        api_url_template uses Python .format() placeholders:
          {page}      → current page number (0-indexed)
          {page_size} → number of results per page
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.warn('Playwright not installed.')
            return None, None

        captured_url  = [None]
        captured_data = [None]

        def on_response(response):
            if captured_url[0]:
                return   # Already found — stop processing
            if not response.url.startswith('http'):
                return
            # Skip obvious static assets
            if any(response.url.endswith(ext) for ext in
                   ('.js', '.css', '.png', '.jpg', '.svg', '.woff', '.ico', '.gif', '.webp')):
                return
            try:
                ct = response.headers.get('content-type', '')
                if 'json' not in ct:
                    return
                body = response.text()
                # Quick check before full parse
                if not any(k in body for k in _PRODUCT_KEYS):
                    return
                data = json.loads(body)
                items, total = self._extract_products_and_total(data)
                if items and total:
                    raw_url = response.url
                    template = self._build_api_template(raw_url)
                    if template:
                        captured_url[0]  = template
                        captured_data[0] = data
                        self.log(f'Intercepted product API: {raw_url}')
            except Exception:
                pass

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                    ],
                )
                ctx = browser.new_context(
                    user_agent=_HEADERS['User-Agent'],
                    viewport={'width': 1280, 'height': 900},
                    locale='en-GB',
                    timezone_id='Europe/London',
                )
                ctx.add_init_script(
                    'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
                )
                page = ctx.new_page()
                page.on('response', on_response)
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=40000)
                    self._dismiss_cookie_banner(page)
                    # Scroll to trigger product loading
                    page.wait_for_timeout(3000)
                    page.evaluate('window.scrollTo(0, 600)')
                    page.wait_for_timeout(2000)
                    page.evaluate('window.scrollTo(0, 1200)')
                    page.wait_for_timeout(3000)
                except Exception as e:
                    self.warn(f'Browser load error: {e}')
                finally:
                    browser.close()
        except Exception as e:
            self.warn(f'Playwright error: {e}')

        return captured_url[0], captured_data[0]

    def _build_api_template(self, raw_url):
        """
        Replace the page/offset/currentPage and pageSize parameters in a raw
        API URL with {page} and {page_size} placeholders so we can paginate.
        Returns None if we can't identify pagination params.
        """
        # Common SAP Hybris OCC / Accelerator pagination param names
        url = raw_url

        # Replace currentPage=N
        if re.search(r'[?&]currentPage=\d+', url):
            url = re.sub(r'(currentPage=)\d+',  r'\g<1>{page}',      url)
            url = re.sub(r'(pageSize=)\d+',     r'\g<1>{page_size}', url)
            return url

        # Replace page=N
        if re.search(r'[?&]page=\d+', url):
            url = re.sub(r'((?<=[?&])page=)\d+', r'\g<1>{page}',      url)
            url = re.sub(r'(pageSize=)\d+',       r'\g<1>{page_size}', url)
            return url

        # Replace start=N (offset-based pagination)
        if re.search(r'[?&]start=\d+', url):
            # Convert to page-based by rebuilding; store as-is and handle separately
            url = re.sub(r'(start=)\d+', r'\g<1>{page}', url)
            url = re.sub(r'(sz=)\d+',    r'\g<1>{page_size}', url)
            return url

        self.warn(f'Could not identify pagination params in: {raw_url}')
        return None

    # -----------------------------------------------------------------------
    # Data extraction
    # -----------------------------------------------------------------------

    def _extract_products_and_total(self, data):
        """
        Attempt to extract a products list and total count from a JSON response.
        Handles SAP Hybris OCC format and common Accelerator variants.
        Returns (products_list, total_count).
        """
        if not isinstance(data, dict):
            return [], 0

        # SAP Hybris OCC: { products: [...], pagination: { totalNumberOfResults: N } }
        products = data.get('products')
        if isinstance(products, list) and products:
            total = (
                (data.get('pagination') or {}).get('totalNumberOfResults')
                or data.get('totalNumberOfResults')
                or len(products)
            )
            return products, int(total)

        # Older Hybris Accelerator JSON: { results: [...], totalCount: N }
        results = data.get('results')
        if isinstance(results, list) and results:
            total = data.get('totalCount') or data.get('total') or len(results)
            return results, int(total)

        # Search deeper one level
        for v in data.values():
            if isinstance(v, dict):
                items, total = self._extract_products_and_total(v)
                if items:
                    return items, total

        return [], 0

    def _parse_product(self, item, gender, subcategory_hint, rank):
        """
        Convert a raw product dict from the API into our standard schema.
        Handles both SAP OCC and Accelerator JSON shapes.
        """
        # SKU / code
        sku = str(item.get('code') or item.get('id') or '').strip()
        if not sku:
            return None

        # Name
        name = self.clean_text(item.get('name') or item.get('title') or '')
        if not name:
            return None

        # URL
        raw_url = item.get('url') or item.get('productUrl') or ''
        if raw_url.startswith('/'):
            product_url = f'https://www.newlook.com{raw_url}'
        elif raw_url.startswith('http'):
            product_url = raw_url
        else:
            product_url = f'https://www.newlook.com/uk/p/{sku}'

        # Subcategory — derive from product URL path
        # e.g. .../womens/footwear/shoes/...  →  'shoes'
        #      .../womens/footwear/sandals/... →  'sandals'
        subcategory = subcategory_hint
        url_path_match = re.search(r'/footwear/([^/]+)/', product_url)
        if url_path_match:
            subcategory = url_path_match.group(1).replace('-', ' ')

        # Price
        price = None
        was_price = None
        price_obj = item.get('price') or {}
        if isinstance(price_obj, dict):
            price = price_obj.get('value')
            if price is None:
                price = self.clean_price(price_obj.get('formattedValue'))
        elif isinstance(price_obj, (int, float)):
            price = float(price_obj)
        else:
            price = self.clean_price(str(price_obj))

        prev_price_obj = item.get('previousPrice') or item.get('wasPrice') or {}
        if isinstance(prev_price_obj, dict):
            was_price = prev_price_obj.get('value')
            if was_price is None:
                was_price = self.clean_price(prev_price_obj.get('formattedValue'))

        is_markdown = bool(was_price and price and was_price > price)

        # Image — construct directly from SKU (more reliable than parsing images array)
        image_url = _IMAGE_BASE.format(sku=sku)

        # Review count
        review_count = item.get('numberOfReviews') or item.get('reviewCount') or None

        # Featured — New Look doesn't expose this directly; use top-4 heuristic
        is_featured = rank <= 4

        return {
            'sku':             sku,
            'name':            name,
            'url':             product_url,
            'category':        gender,
            'subcategory':     subcategory,
            'price':           price,
            'rank':            rank,
            'review_count':    review_count,
            'sizes_available': [],
            'sizes_oos':       [],
            'is_featured':     is_featured,
            'image_url':       image_url,
            'raw_data': {
                'was_price':   was_price,
                'is_markdown': is_markdown,
                'brand':       item.get('manufacturer') or item.get('brand'),
            },
        }

    # -----------------------------------------------------------------------
    # HTML pagination fallback
    # -----------------------------------------------------------------------

    def _scrape_html_pages(self, base_url, gender, subcategory_hint):
        """
        Fallback: scrape products from SSR'd HTML by iterating ?page=N.
        Each page SSRs ~3 products.  Stops when a page returns no new products.
        Significantly slower and less complete than API mode.
        """
        self.warn('Using HTML fallback — coverage will be limited to SSR\'d products only.')
        seen_skus = set()
        all_products = []
        page = 0
        empty_streak = 0

        while empty_streak < 3:
            url = f'{base_url}?page={page}' if page else base_url
            try:
                resp = requests.get(url, headers={**_HEADERS, 'Accept': 'text/html'}, timeout=15)
                if resp.status_code != 200:
                    break
                items = self._parse_html_products(resp.text, gender, subcategory_hint, len(all_products))
                new = [p for p in items if p['sku'] not in seen_skus]
                if not new:
                    empty_streak += 1
                else:
                    empty_streak = 0
                    seen_skus.update(p['sku'] for p in new)
                    all_products.extend(new)
                    self.log(f'HTML page {page}: +{len(new)} ({len(all_products)} total)')
            except Exception as e:
                self.warn(f'HTML page {page} error: {e}')
                break
            page += 1
            time.sleep(1)

        return all_products

    def _parse_html_products(self, html, gender, subcategory_hint, rank_offset):
        """
        Extract SSR'd product data from New Look category page HTML.
        Parses the product list items rendered server-side.
        Regex-based; tolerates the limited SSR payload.
        """
        products = []
        # Match product URLs: /uk/.../p/{SKU}
        pattern = re.compile(
            r'href="(https://www\.newlook\.com/uk/[^"]+?/p/(\d{9,}))"[^>]*>\s*'
            r'\[?([^\]\[]+?)\]?\s*\(',
            re.DOTALL,
        )
        img_pattern = re.compile(
            r'newlookassets\.com/i/newlook/(\d{9,})\.jpg'
        )
        price_pattern = re.compile(r'£([\d.]+)')

        found_skus = {}
        for m in img_pattern.finditer(html):
            found_skus[m.group(1)] = None   # Placeholder

        # Try to pair SKUs with names and prices from surrounding context
        blocks = re.split(r'<li[^>]*>', html)
        for block in blocks:
            img_m = img_pattern.search(block)
            if not img_m:
                continue
            sku = img_m.group(1)
            if sku in {p['sku'] for p in products}:
                continue

            # Extract product URL
            url_m = re.search(rf'href="([^"]+/p/{sku})"', block)
            product_url = url_m.group(1) if url_m else f'https://www.newlook.com/uk/p/{sku}'

            # Extract name (text of the anchor tag for the product)
            name_m = re.search(rf'\[([^\[\]]+?)\]\({re.escape(product_url)}\)', block)
            name = self.clean_text(name_m.group(1)) if name_m else f'Product {sku}'

            # Subcategory from URL
            subcategory = subcategory_hint
            sub_m = re.search(r'/footwear/([^/]+)/', product_url)
            if sub_m:
                subcategory = sub_m.group(1).replace('-', ' ')

            # Price
            prices = price_pattern.findall(block)
            price = float(prices[0]) if prices else None

            products.append({
                'sku':             sku,
                'name':            name,
                'url':             product_url,
                'category':        gender,
                'subcategory':     subcategory,
                'price':           price,
                'rank':            rank_offset + len(products) + 1,
                'review_count':    None,
                'sizes_available': [],
                'sizes_oos':       [],
                'is_featured':     False,
                'image_url':       _IMAGE_BASE.format(sku=sku),
                'raw_data':        {'was_price': None, 'is_markdown': False},
            })

        return products

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _parse_category_path(self, path):
        """
        Derive gender and subcategory hint from the category URL path.
        e.g. '/uk/womens/footwear/c/uk-womens-footwear'       → ('women', None)
             '/uk/mens/mens-footwear/c/uk-mens-footwear'      → ('men',   None)
             '/uk/girls/shoes-for-girls/c/uk-teens-footwear'  → ('girls', None)
        """
        path_lower = path.lower()
        if 'women' in path_lower:
            gender = 'women'
        elif 'mens' in path_lower or '/men/' in path_lower:
            gender = 'men'
        elif 'girl' in path_lower or 'teen' in path_lower:
            gender = 'girls'
        elif 'boy' in path_lower:
            gender = 'boys'
        else:
            gender = 'unknown'
        return gender, None

    def _dismiss_cookie_banner(self, page):
        for sel in [
            '#onetrust-accept-btn-handler',
            'button[id*="accept"]',
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
            'button:has-text("Accept")',
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
        Print all intercepted network responses from the first category page.
        Run with: python run.py --discover newlook
        Reveals the exact API endpoint URL and response schema.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print('Playwright not installed.')
            return

        url = 'https://www.newlook.com/uk/womens/footwear/c/uk-womens-footwear'
        print(f'\n=== New Look discovery mode ===\nLoading {url}\n')

        captured = []

        def on_response(response):
            # Skip obvious static assets by extension
            if any(response.url.endswith(ext) for ext in
                   ('.png', '.jpg', '.svg', '.woff', '.woff2', '.ico', '.gif', '.webp', '.ttf')):
                return
            # Skip tracking / analytics domains
            skip_domains = ('google', 'facebook', 'doubleclick', 'analytics', 'hotjar',
                            'optimizely', 'segment', 'adobe', 'omniture', 'qualtrics')
            if any(d in response.url for d in skip_domains):
                return
            try:
                ct = response.headers.get('content-type', '')
                body = response.text()
                if not body or len(body) < 20:
                    return
                entry = {
                    'url':    response.url,
                    'status': response.status,
                    'ct':     ct,
                    'body':   body[:2000],
                    'is_product': any(k in body for k in _PRODUCT_KEYS),
                }
                captured.append(entry)
            except Exception:
                pass

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                ],
            )
            ctx = browser.new_context(
                user_agent=_HEADERS['User-Agent'],
                viewport={'width': 1280, 'height': 900},
                locale='en-GB',
                timezone_id='Europe/London',
            )
            ctx.add_init_script(
                'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            )
            page = ctx.new_page()
            page.on('response', on_response)
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=40000)
                self._dismiss_cookie_banner(page)
                # Scroll to trigger lazy-loading of products
                page.wait_for_timeout(3000)
                page.evaluate('window.scrollTo(0, 600)')
                page.wait_for_timeout(2000)
                page.evaluate('window.scrollTo(0, 1200)')
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f'Browser error: {e}')
            finally:
                browser.close()

        print(f'Captured {len(captured)} responses total.\n')

        # First show any product data
        product_responses = [r for r in captured if r['is_product']]
        other_json = [r for r in captured if not r['is_product'] and 'json' in r['ct'].lower()]
        other = [r for r in captured if not r['is_product'] and 'json' not in r['ct'].lower()]

        if product_responses:
            print('=' * 60)
            print(f'PRODUCT DATA FOUND in {len(product_responses)} response(s):')
            print('=' * 60)
            for r in product_responses:
                print(f'\n[{r["status"]}] {r["url"]}')
                print(f'Content-Type: {r["ct"]}')
                print(r['body'][:2000])
                print()
        else:
            print('NO PRODUCT DATA DETECTED in any response.\n')

        if other_json:
            print('-' * 60)
            print(f'Other JSON responses ({len(other_json)}):')
            print('-' * 60)
            for r in other_json:
                print(f'\n[{r["status"]}] {r["url"]}')
                print(f'Content-Type: {r["ct"]}')
                print(r['body'][:500])
                print()

        if other:
            print('-' * 60)
            print(f'Non-JSON responses ({len(other)}) — URLs only:')
            for r in other:
                print(f'  [{r["status"]}] {r["ct"][:40]:40s}  {r["url"][:100]}')
