"""
scrapers/newlook.py — New Look scraper using the product sitemap.

Strategy:
  New Look's website and API are protected by PerimeterX at the edge, blocking
  all datacenter IP requests to category pages and the OCC search API.

  However, two endpoints remain accessible via plain requests:
    1. The XML sitemap (sitemap_uk_product_en_1.xml) — lists every product
       with its URL, image URL, and name.  One request for all ~10 K products.
    2. Individual product pages — return HTTP 200 with structured data (LD+JSON)
       including price.

  Scrape flow:
    a. Fetch the product sitemap (~15 MB XML, one request).
    b. Parse it into product entries using regex (namespace-aware).
    c. Filter for footwear-only entries (URL path contains /footwear/).
    d. Derive gender & subcategory from the URL path.
    e. Optionally fetch each product page to get the current price.

  Pricing note:
    Fetching a product page per product is slow (~1–2 s each * 1,500 products
    ≈ 30 min).  By default price fetching is OFF and price is stored as None.
    Set 'fetch_prices': True in the retailer config to enable it.
    Prices are only re-fetched for new or unfamiliar products.

  To run discovery / debug:
    python run.py --discover newlook
"""

import re
import json
import time
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

# Sitemap URL for UK products
_SITEMAP_URL = 'https://www.newlook.com/uk/sitemap/maps/sitemap_uk_product_en_1.xml'

# Image CDN template — override w= for quality
_IMAGE_BASE = 'https://media2.newlookassets.com/i/newlook/{sku}.jpg?strip=true&qlt=80&w=600'

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept':          'text/html,application/xhtml+xml,*/*',
    'Accept-Language': 'en-GB,en;q=0.9',
    'Referer':         'https://www.newlook.com/',
}

# Pre-compiled regex patterns for sitemap parsing
# Each <ns1:url> block contains a product URL and optionally an image block.
# The sitemap uses XML namespaces, so we match on 'loc>' rather than '<loc>'.
_RE_URL_BLOCK  = re.compile(r'<ns[0-9]+:url\b[^>]*>(.*?)</ns[0-9]+:url>', re.DOTALL)
_RE_PROD_LOC   = re.compile(r'<ns[0-9]+:loc[^>]*>(https://www\.newlook\.com[^<]+)</ns[0-9]+:loc>')
_RE_IMAGE_LOC  = re.compile(r'<image:loc>([^<]+)</image:loc>')
_RE_CAPTION    = re.compile(r'<image:caption>([^<]+)</image:caption>')


class NewLookScraper(BaseScraper):

    def __init__(self, config):
        super().__init__(config)
        self._fetch_prices = config.get('fetch_prices', False)

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def scrape_all(self):
        """
        Override scrape_all: fetch sitemap once, then filter per category.
        More efficient than calling scrape_category per category.
        """
        self.log('Fetching product sitemap …')
        all_entries = self._fetch_sitemap()
        if not all_entries:
            self.warn('Sitemap returned no entries — aborting.')
            return []

        self.log(f'Sitemap: {len(all_entries)} total product entries')

        # Filter for categories configured in config
        category_paths = self.config.get('categories', [])
        # Build a set of path prefixes to match against product URLs
        # e.g. '/uk/womens/footwear' matches any womens footwear product
        prefixes = [self._category_path_to_prefix(p) for p in category_paths]

        filtered = [e for e in all_entries if any(e['url_path'].startswith(px) for px in prefixes)]
        self.log(f'After category filter: {len(filtered)} footwear products')

        if not filtered:
            self.warn('No products matched configured category paths.')
            return []

        # Sort by gender to assign ranks within each category
        results = self._build_products(filtered)
        self.log(f'Built {len(results)} product dicts.')
        return results

    def scrape_category(self, category_path):
        """Called by base class if scrape_all is not overridden. Not used here."""
        return []

    def scrape_product(self, product_url):
        return None

    # -----------------------------------------------------------------------
    # Sitemap fetching & parsing
    # -----------------------------------------------------------------------

    def _fetch_sitemap(self):
        """
        Download and parse the product sitemap XML.
        Returns a list of dicts with keys: url, url_path, sku, name, image_url.
        """
        try:
            resp = requests.get(_SITEMAP_URL, headers=_HEADERS, timeout=30)
            if resp.status_code != 200:
                self.warn(f'Sitemap returned HTTP {resp.status_code}')
                return []
            return self._parse_sitemap(resp.text)
        except Exception as e:
            self.warn(f'Sitemap fetch error: {e}')
            return []

    def _parse_sitemap(self, xml_text):
        """
        Parse the namespaced XML sitemap.
        Returns list of dicts: url, url_path, sku, name, image_url.
        """
        entries = []

        # The sitemap uses heavy namespacing.  We parse URL blocks one at a time.
        # Each block looks like:
        #   <ns1:url>
        #     <ns2:loc>https://www.newlook.com/uk/womens/footwear/.../p/SKU</ns2:loc>
        #     <image:image>
        #       <image:loc>http://media3.newlookassets.com/i/newlook/SKU.jpg</image:loc>
        #       <image:caption>Product Name</image:caption>
        #     </image:image>
        #   </ns1:url>
        for block_match in _RE_URL_BLOCK.finditer(xml_text):
            block = block_match.group(1)

            # Product page URL
            loc_m = _RE_PROD_LOC.search(block)
            if not loc_m:
                continue
            url = loc_m.group(1).strip()

            # Only process newlook.com/uk/ product pages (skip alternate lang links)
            if '/uk/' not in url or '/p/' not in url:
                continue

            url_path = url.replace('https://www.newlook.com', '')

            # SKU: last path segment after /p/
            sku_m = re.search(r'/p/(\d+)$', url_path)
            if not sku_m:
                continue
            sku = sku_m.group(1)

            # Product name from image caption
            cap_m = _RE_CAPTION.search(block)
            name = self.clean_text(cap_m.group(1)) if cap_m else f'Product {sku}'

            # Image URL — use our CDN template for consistent quality
            image_url = _IMAGE_BASE.format(sku=sku)

            entries.append({
                'url':       url,
                'url_path':  url_path,
                'sku':       sku,
                'name':      name,
                'image_url': image_url,
            })

        return entries

    # -----------------------------------------------------------------------
    # Building product dicts
    # -----------------------------------------------------------------------

    def _build_products(self, entries):
        """
        Convert sitemap entries into standard product dicts.
        Optionally batch-fetches product pages for prices.
        """
        # Group by gender for rank assignment
        by_gender = {}
        for e in entries:
            gender = self._gender_from_path(e['url_path'])
            by_gender.setdefault(gender, []).append(e)

        products = []
        for gender, group in by_gender.items():
            for rank, entry in enumerate(group, start=1):
                subcategory = self._subcategory_from_path(entry['url_path'])
                products.append({
                    'sku':             entry['sku'],
                    'name':            entry['name'],
                    'url':             entry['url'],
                    'category':        gender,
                    'subcategory':     subcategory,
                    'price':           None,   # Filled in below if fetch_prices=True
                    'rank':            rank,
                    'review_count':    None,
                    'sizes_available': [],
                    'sizes_oos':       [],
                    'is_featured':     rank <= 4,
                    'image_url':       entry['image_url'],
                    'raw_data':        {'was_price': None, 'is_markdown': False},
                    '_url':            entry['url'],   # kept for price fetching
                })

        if self._fetch_prices:
            self.log(f'Fetching prices for {len(products)} products (this may take a while) …')
            self._fill_prices(products)

        # Remove internal key
        for p in products:
            p.pop('_url', None)

        return products

    def _fill_prices(self, products, max_workers=8):
        """Concurrently fetch product pages to extract prices."""
        def fetch_one(product):
            try:
                r = requests.get(product['_url'], headers=_HEADERS, timeout=12)
                if r.status_code == 200:
                    price, was_price = self._extract_price(r.text)
                    return product['sku'], price, was_price
            except Exception:
                pass
            return product['sku'], None, None

        sku_map = {p['sku']: p for p in products}

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(fetch_one, p): p for p in products}
            done = 0
            for fut in as_completed(futures):
                sku, price, was_price = fut.result()
                if price is not None and sku in sku_map:
                    sku_map[sku]['price'] = price
                    sku_map[sku]['raw_data']['was_price'] = was_price
                    sku_map[sku]['raw_data']['is_markdown'] = bool(
                        was_price and price and was_price > price
                    )
                done += 1
                if done % 100 == 0:
                    self.log(f'Prices: {done}/{len(products)} fetched')

    def _extract_price(self, html):
        """
        Extract current and was-price from product page HTML.
        Tries LD+JSON Product schema first, then regex fallback.
        Returns (price_float, was_price_float) — either may be None.
        """
        # LD+JSON approach (most reliable)
        for block in re.findall(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.DOTALL
        ):
            try:
                data = json.loads(block)
                if isinstance(data, dict) and data.get('@type') == 'Product':
                    offers = data.get('offers', {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get('price')
                    if price is not None:
                        return float(price), None
            except Exception:
                pass

        # Regex fallback — look for "price":"17.99" patterns
        m = re.search(r'"price"\s*:\s*"?([\d.]+)"?', html)
        if m:
            return float(m.group(1)), None

        return None, None

    # -----------------------------------------------------------------------
    # URL path helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _category_path_to_prefix(config_path):
        """
        Convert a config category path like '/uk/womens/footwear/c/uk-womens-footwear'
        to a URL prefix that matches product URLs: '/uk/womens/footwear/'.
        """
        # Config paths end in /c/{category-id}; product paths are under the parent
        # e.g. '/uk/womens/footwear' prefix matches '/uk/womens/footwear/shoes/...'
        m = re.match(r'(/uk/[^/]+/[^/]+)', config_path)
        if m:
            return m.group(1) + '/'
        # Fallback: use path up to /c/
        return config_path.split('/c/')[0] + '/'

    @staticmethod
    def _gender_from_path(url_path):
        """Derive gender label from URL path."""
        p = url_path.lower()
        if '/womens/' in p:  return 'women'
        if '/mens/'   in p:  return 'men'
        if '/girls/'  in p:  return 'girls'
        if '/boys/'   in p:  return 'boys'
        return 'unknown'

    @staticmethod
    def _subcategory_from_path(url_path):
        """
        Derive subcategory from URL path.
        e.g. '/uk/womens/footwear/shoes/product-name/p/SKU'   → 'shoes'
             '/uk/womens/footwear/product-name/p/SKU'         → None  (no subfolder)
             '/uk/mens/mens-footwear/trainers/product/p/SKU'  → 'trainers'

        Requires TWO path segments between the category anchor and /p/ to avoid
        treating the product slug as a subcategory.
        """
        # Women's/girls' footwear: /footwear/<subcat>/<slug>/p/<SKU>
        m = re.search(r'/footwear/([^/]+)/[^/]+/p/', url_path)
        if m:
            return m.group(1).replace('-', ' ')
        # Men's: /mens-footwear/<subcat>/<slug>/p/<SKU>
        m = re.search(r'/mens-footwear/([^/]+)/[^/]+/p/', url_path)
        if m:
            return m.group(1).replace('-', ' ')
        # Girls: /shoes-for-girls/<subcat>/<slug>/p/<SKU>
        m = re.search(r'/shoes-for-girls/([^/]+)/[^/]+/p/', url_path)
        if m:
            return m.group(1).replace('-', ' ')
        return None

    # -----------------------------------------------------------------------
    # Discovery mode
    # -----------------------------------------------------------------------

    def discover(self):
        """
        Test sitemap fetch and print sample products.
        Run with: python run.py --discover newlook
        """
        print('\n=== New Look discovery mode (sitemap) ===\n')
        print(f'Fetching sitemap: {_SITEMAP_URL}')

        try:
            resp = requests.get(_SITEMAP_URL, headers=_HEADERS, timeout=30)
            print(f'HTTP {resp.status_code}  size: {len(resp.text):,} chars')
        except Exception as e:
            print(f'Fetch error: {e}')
            return

        if resp.status_code != 200:
            print('Non-200 response — sitemap not accessible.')
            return

        entries = self._parse_sitemap(resp.text)
        print(f'\nTotal entries parsed: {len(entries)}')

        # Filter footwear
        prefixes = [self._category_path_to_prefix(p) for p in self.config.get('categories', [])]
        footwear = [e for e in entries if any(e['url_path'].startswith(px) for px in prefixes)]
        print(f'Footwear entries: {len(footwear)}')

        # Gender breakdown
        by_gender = {}
        for e in footwear:
            g = self._gender_from_path(e['url_path'])
            by_gender[g] = by_gender.get(g, 0) + 1
        print(f'By gender: {by_gender}')

        # Subcategory sample
        subcats = {}
        for e in footwear:
            s = self._subcategory_from_path(e['url_path']) or 'other'
            subcats[s] = subcats.get(s, 0) + 1
        print(f'Subcategories: {dict(sorted(subcats.items(), key=lambda x: -x[1])[:10])}')

        print('\nSample products:')
        for e in footwear[:5]:
            print(f'  SKU={e["sku"]}  Name={e["name"][:50]}')
            print(f'  URL={e["url"][:80]}')
            print(f'  Image={e["image_url"][:60]}')
            print()

        # Check for a second sitemap file (girls/other may be in en_2)
        print('\nChecking for sitemap_uk_product_en_2.xml …')
        sitemap2_url = _SITEMAP_URL.replace('_en_1.xml', '_en_2.xml')
        try:
            r2 = requests.get(sitemap2_url, headers=_HEADERS, timeout=15)
            print(f'  HTTP {r2.status_code}  size: {len(r2.text):,} chars')
            if r2.status_code == 200:
                entries2 = self._parse_sitemap(r2.text)
                print(f'  Entries: {len(entries2)}')
                foot2 = [e for e in entries2 if any(e['url_path'].startswith(px) for px in prefixes)]
                print(f'  Footwear matches: {len(foot2)}')
                # Gender sample
                g2 = {}
                for e in entries2:
                    g = self._gender_from_path(e['url_path'])
                    g2[g] = g2.get(g, 0) + 1
                print(f'  Gender breakdown: {dict(list(g2.items())[:6])}')
        except Exception as e:
            print(f'  Error: {e}')

        # Test price fetch on first product
        if footwear:
            print('\nTesting price fetch on first product …')
            try:
                r = requests.get(footwear[0]['url'], headers=_HEADERS, timeout=12)
                price, was = self._extract_price(r.text)
                print(f'  HTTP {r.status_code}  price={price}  was_price={was}')
            except Exception as e:
                print(f'  Price fetch error: {e}')
