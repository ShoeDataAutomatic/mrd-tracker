"""
scrapers/primark.py — Primark SSR DOM scraper.

Strategy:
  Primark uses Next.js App Router with Server-Side Rendering (SSR) — products
  are embedded in the HTML at page-load time, not fetched via a separate API
  call.  The old getPlpProducts capture-and-replay approach no longer works.

  Phase 1 — Browser: load the category page in a real Chrome window,
    dismiss the cookie banner, then wait for product links to appear in the
    rendered DOM (selector: a[href*="/en-gb/p/"]).

  Phase 2 — Pagination: scroll to the bottom and click "Load N more" until
    the button disappears, then run a single JS extraction over the full page.

  Product IDs are derived from the URL slug — the trailing numeric segment:
    /en-gb/p/strappy-block-heel-sandals-black-991169064804  →  PID 991169064804
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
        self._px_blocked = False   # set True after first PX block; skips remaining categories

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def scrape_all(self):
        """Override to break immediately when PX blocks the first category."""
        self.results = []
        for category_path in self.config.get('categories', []):
            if self._px_blocked:
                self.warn('PX blocked — aborting remaining Primark categories')
                break
            logger.info(f'[{self.config["name"]}] Scraping category: {category_path}')
            try:
                products = self.scrape_category(category_path)
                self.results.extend(products)
                logger.info(f'[{self.config["name"]}] Found {len(products)} products in {category_path}')
            except Exception as e:
                logger.error(f'[{self.config["name"]}] Failed on {category_path}: {e}')
            if not self._px_blocked:
                time.sleep(2)
        return self.results

    def scrape_category(self, category_path):
        if self._px_blocked:
            self.warn(f'Skipping {category_path} — PX blocked on first category')
            return []

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
        self._load_all_from_dom(slug, url, all_docs)

        self.log(f'Complete: {len(all_docs)} products for {slug}')

        products = []
        for rank, item in enumerate(all_docs, start=1):
            product = self._parse_dom_product(item, category_path, rank, category_label, subcategory)
            if product:
                products.append(product)
        return products

    def _load_all_from_dom(self, slug, url, all_docs):
        """
        Extract all products from Primark's SSR HTML DOM.

        Primark switched to Next.js App Router (SSR) — products are rendered
        server-side in the initial HTML, not loaded via a getPlpProducts API
        call.  This method:
          1. Launches a real Chrome window (bypasses PerimeterX fingerprinting)
          2. Dismisses the cookie banner
          3. Waits for a[href*="/en-gb/p/"] links to appear
          4. Clicks "Load N more" until exhausted
          5. Runs a single JS extraction over the fully-loaded DOM
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return

        # JavaScript that walks every product link and gathers name / price / img
        # from the nearest ancestor that contains both fields.
        JS_EXTRACT = r'''
            () => {
                const results = [];
                const seen = new Set();
                document.querySelectorAll('a[href*="/en-gb/p/"]').forEach(a => {
                    const href = a.href;
                    if (!href || seen.has(href)) return;
                    seen.add(href);

                    let el = a.parentElement;
                    let name = null, price = null, img = null;

                    for (let i = 0; i < 12 && el; i++) {
                        if (!name) {
                            const nameEl = el.querySelector(
                                '[data-testautomation-id="product-name"],' +
                                '[class*="ProductName"],[class*="product-name"]'
                            );
                            if (nameEl) name = nameEl.innerText.trim();
                        }
                        if (!price) {
                            const priceEl = el.querySelector(
                                '[data-testautomation-id="price"],' +
                                '[data-testautomation-id*="Price"],' +
                                '[class*="Price"],[class*="price"]'
                            );
                            if (priceEl) price = priceEl.innerText.trim().replace(/\s+/g, ' ');
                        }
                        if (!img) {
                            const imgEl = el.querySelector(
                                'img[src*="primedia"],img[data-src*="primedia"],' +
                                'img[srcset*="primedia"]'
                            );
                            if (imgEl) {
                                const src = imgEl.src || imgEl.dataset.src || '';
                                if (src && !src.startsWith('data:')) img = src;
                            }
                        }
                        if (name && price) break;
                        el = el.parentElement;
                    }

                    if (name || price) {
                        results.push({ url: href, name: name, price: price, img: img });
                    }
                });
                return results;
            }
        '''

        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel='chrome',    # real installed Chrome — avoids headless fingerprint
                headless=False,      # visible mode: PX JS challenge completes properly
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
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(page)
            except Exception:
                pass

            try:
                # ── Warm up PX session on the homepage first ─────────────────
                # PX is more likely to trust a session that starts at the root
                # domain rather than landing directly on a deep category URL.
                self.log('Warming up PX session on homepage...')
                page.goto('https://www.primark.com', wait_until='domcontentloaded', timeout=20000)
                page.wait_for_timeout(3000)

                # Now navigate to the actual category page
                page.goto(url, wait_until='domcontentloaded', timeout=30000)

                # Cookie banner loads asynchronously — wait for it then dismiss
                try:
                    page.wait_for_selector(
                        '#onetrust-accept-btn-handler,'
                        'button:has-text("Accept all"),'
                        'button:has-text("Accept")',
                        timeout=8000,
                    )
                    self._dismiss_cookie_banner(page)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

                # Wait for product links — longer timeout to allow JS hydration
                try:
                    page.wait_for_selector('a[href*="/en-gb/p/"]', timeout=30000)
                except Exception:
                    # Log a page snippet so we know what PX is actually serving
                    try:
                        snippet = page.evaluate(
                            "document.body.innerText.replace(/\\s+/g,' ').slice(0,400)"
                        )
                        self.warn(f'Page body snippet: {snippet}')
                    except Exception:
                        pass
                    self.warn(f'No product links found on {url} — PX may be blocking')
                    self._px_blocked = True
                    browser.close()
                    return

                # ── Pagination: click "Load N more" until exhausted ──────────
                for page_num in range(20):   # safety cap — 20 × 24 = 480 products max
                    # Scroll to bottom so the "Load more" button is in view
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                    page.wait_for_timeout(1200)

                    more_btn = None
                    for sel in [
                        'button:has-text("Load")',
                        'button:has-text("Show more")',
                        '[data-testid*="load-more"]',
                        '[data-testautomation-id*="load-more"]',
                    ]:
                        try:
                            btn = page.query_selector(sel)
                            if btn and btn.is_visible():
                                more_btn = btn
                                break
                        except Exception:
                            pass

                    if not more_btn:
                        self.log(f'All products loaded after {page_num + 1} scroll(s)')
                        break

                    count_before = page.evaluate(
                        "document.querySelectorAll('a[href*=\"/en-gb/p/\"]').length"
                    )
                    more_btn.click()
                    self.log(f'Clicked "Load more" (batch {page_num + 2}), waiting...')

                    try:
                        page.wait_for_function(
                            f"document.querySelectorAll('a[href*=\"/en-gb/p/\"]').length > {count_before}",
                            timeout=12000,
                        )
                        page.wait_for_timeout(500)
                    except Exception:
                        self.log('No new products appeared — stopping pagination')
                        break

                # ── Final DOM extraction ─────────────────────────────────────
                products = page.evaluate(JS_EXTRACT)
                all_docs.extend(products)
                self.log(f'DOM extracted {len(products)} products from {url}')

            except Exception as e:
                self.warn(f'Browser load error: {e}')
                self._px_blocked = True
            finally:
                browser.close()


    def scrape_product(self, product_url):
        return None

    # -----------------------------------------------------------------------
    # Availability check (OOS detection via ColourAvailability API)
    # -----------------------------------------------------------------------

    def check_all_availability(self, products):
        """
        Check OOS status for all Primark products.

        Uses the NonStoreSpecificColourAvailability GraphQL API on
        api001-arh.primark.com/bff-cae-green.  Each product requires one call
        with variables {"locale":"en-gb","styleCode":"<9-digit code>"}, where
        the styleCode is the first 9 digits of the 12-digit trailing number in
        the product URL (e.g. .../p/name-991169064804 → styleCode "991169064").

        To bypass PerimeterX on the API subdomain we:
          1. Navigate to a product page on primark.com to capture the persisted
             query hash from the intercepted network response.
          2. Navigate to the API subdomain directly to establish a PX session
             cookie for that domain.
          3. Fire parallel fetch() calls (20 at a time via Promise.all) from
             within the primark.com page context with credentials: 'include' so
             the PX cookie is sent.

        On Railway (datacenter IP) the API subdomain still blocks with PX.
        We detect the HTML error body and bail after one warning.
        """
        import re as _re
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.warn('[Primark] Playwright not installed — skipping availability check')
            return

        from urllib.parse import urlparse as _up, parse_qs as _pqs, unquote as _uq

        def get_style_code(url):
            m = _re.search(r'-(\d{12})$', (url or '').rstrip('/'))
            return m.group(1)[:9] if m else None

        checkable = [p for p in products if get_style_code(p.get('url', ''))]
        if not checkable:
            self.log('[Primark] No valid product URLs for OOS check — skipping')
            return

        self.log(f'[Primark] OOS check: {len(checkable)}/{len(products)} products')

        # ── Phase 1: navigate to a product page, intercept the hash ──────────
        captured = {'extensions': None}

        def on_response(response):
            if 'NonStoreSpecificColourAvailability' not in response.url:
                return
            try:
                params  = _pqs(_up(response.url).query)
                ext_raw = params.get('extensions', [''])[0]
                if ext_raw and not captured['extensions']:
                    captured['extensions'] = _uq(ext_raw)
            except Exception:
                pass

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                channel='chrome',
                headless=False,
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
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(page)
            except Exception:
                pass
            page.on('response', on_response)

            self.log('[Primark] Loading product page to capture API hash...')
            try:
                page.goto(checkable[0]['url'], wait_until='domcontentloaded', timeout=15000)
                page.wait_for_timeout(3000)
            except Exception as e:
                self.warn(f'[Primark] Product page load error: {e}')

            if not captured['extensions']:
                self.warn(
                    '[Primark] OOS check: could not capture persisted query hash '
                    '— skipping.'
                )
                browser.close()
                return

            # ── Phase 2: warm up api subdomain so PX sets a cookie ───────────
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
                self.warn(f'[Primark] API subdomain warmup error (continuing): {e}')
            api_page.close()

            extensions_str = captured['extensions']
            self.log('[Primark] Hash captured. Fetching availability...')

            # ── Phase 3: parallel fetch per product, 20 at a time ────────────
            PARALLEL   = 20
            px_blocked = False
            avail_map  = {}   # styleCode -> bool (True = at least one size available)

            for i in range(0, len(checkable), PARALLEL):
                if px_blocked:
                    break

                batch       = checkable[i:i + PARALLEL]
                style_codes = [get_style_code(p['url']) for p in batch]
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
            const text = await resp.text();
            return {{sc, status: resp.status, body: text}};
        }} catch (e) {{
            return {{sc, status: 0, body: ''}};
        }}
    }}));
    return results;
}})()
"""
                try:
                    results = page.evaluate(js)
                except Exception as e:
                    self.warn(f'[Primark] Batch evaluate error at offset {i}: {e}')
                    continue

                for r in results:
                    sc   = r.get('sc', '')
                    body = r.get('body', '')
                    if not body:
                        continue
                    if '<!DOCTYPE' in body or 'px-captcha' in body:
                        px_blocked = True
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

                if px_blocked:
                    self.warn(
                        '[Primark] OOS check blocked by PerimeterX on '
                        'api001-arh.primark.com (datacenter IP rejected). '
                        'A residential proxy is required. Skipping.'
                    )
                    break

                if i % (PARALLEL * 5) == 0 and i > 0:
                    done = min(i + PARALLEL, len(checkable))
                    self.log(f'[Primark] OOS check: {done}/{len(checkable)} fetched')

            browser.close()

        if px_blocked:
            return

        # ── Phase 4: apply results ────────────────────────────────────────────
        oos_count = 0
        for prod in checkable:
            sc = get_style_code(prod['url'])
            if sc not in avail_map:
                continue
            is_oos = not avail_map[sc]
            prod.setdefault('raw_data', {})['is_oos'] = is_oos
            if is_oos:
                oos_count += 1

        self.log(f'[Primark] OOS check done: {oos_count}/{len(checkable)} confirmed OOS.')

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

    def _parse_dom_product(self, item, category_path, rank, category_label=None, subcategory=None):
        """Parse a product dict extracted from the SSR DOM by _load_all_from_dom."""
        url = (item.get('url') or '').strip()
        if not url:
            return None

        # PID = trailing numeric segment in the URL slug
        # e.g. /en-gb/p/strappy-block-heel-sandals-black-991169064804 → 991169064804
        m = re.search(r'-(\d+)$', url.rstrip('/'))
        if not m:
            return None
        pid = m.group(1)

        name = self.clean_text(item.get('name') or 'Unknown')

        # Price string: "£12.00" or "£8.00 £12.00" (sale then was-price).
        # Extract up to two numbers; first is current price, second is was-price.
        price_raw = (item.get('price') or '').strip()
        price_nums = re.findall(r'\d+\.?\d*', price_raw)
        price     = float(price_nums[0]) if price_nums else None
        was_price = float(price_nums[1]) if len(price_nums) >= 2 else None
        is_markdown = bool(was_price and price and was_price > price)

        image_url = item.get('img') or None

        if not category_label:
            slug = category_path.split('/en-gb/c/')[-1].strip('/')
            category_label = slug.split('/')[0]

        return {
            'sku':             pid,
            'name':            name,
            'url':             url,
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
                'colour':      None,
                'color_count': None,
                'brand':       'Primark',
                'was_price':   was_price,
                'is_markdown': is_markdown,
            },
        }

    def _parse_product(self, item, category_path, rank, category_label=None, subcategory=None):
        """Parse a product from the old GraphQL API format. Used by discover() only."""
        pid = str(item.get('pid', '')).strip()
        if not pid:
            return None
        url_slug = item.get('url', '').strip()
        if not url_slug:
            return None

        # price / sale_price are always equal in the API (sale_price is not useful).
        # pricePrevious = the was-price when pricePrevious > price; equals price otherwise.
        # changePercent = positive integer (e.g. 56 = 56% off) when discounted, else null.
        price_pence = item.get('price')
        prev_pence  = item.get('pricePrevious')

        price = round(price_pence / 100.0, 2) if price_pence else None

        # A product is on markdown when pricePrevious > price (the was-price is higher).
        if prev_pence and price_pence and prev_pence > price_pence:
            was_price = round(prev_pence / 100.0, 2)
        else:
            was_price = None

        is_markdown = bool(was_price and price and was_price > price)
        thumb       = item.get('thumb_image', '').strip()
        image_url   = f'{thumb}?w=600&fmt=auto' if thumb else None

        # The PLP feed doesn't list a colour name field on the product itself, but
        # the first (displayed) variant's sku_color holds the colour swatch shown
        # on the card (e.g. "tan", "chocolate"). Fold it into the name so the
        # keyword classifier (which only tokenises `name`) can pick it up.
        variants  = item.get('variants') or []
        colour    = (variants[0].get('sku_color') or '').strip().lower() if variants else ''
        # Size-level SKU IDs — used by check_all_availability() for OOS detection.
        # Stored at the product level (not in raw_data) so they're available during
        # the scrape but are not persisted to the DB snapshot.
        # Prefer variant-level skuIds (size-specific); fall back to masterSkuId
        # (product/colour level) which is consistently populated in PLP responses.
        size_skus = [str(v['skuId']) for v in variants if v.get('skuId')]
        if not size_skus and item.get('masterSkuId'):
            size_skus = [str(item['masterSkuId'])]
        title    = self.clean_text(item.get('title', 'Unknown'))
        name     = f'{title} {colour.title()}' if colour else title

        # Use provided label, otherwise derive from category_path
        if not category_label:
            slug = category_path.split('/en-gb/c/')[-1].strip('/')
            category_label = '/'.join(slug.split('/')[-2:]) if slug.count('/') >= 2 else slug

        return {
            'sku':             pid,
            'name':            name,
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
            'size_skus':       size_skus or None,   # ephemeral — used by check_all_availability, not stored
            'raw_data': {
                'description': item.get('description'),
                'colour':      colour or None,
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
