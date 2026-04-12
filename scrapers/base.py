"""
scrapers/base.py — Abstract base class for all retailer scrapers.

To add a new retailer, create scrapers/retailername.py,
subclass BaseScraper, and implement scrape_category() and scrape_product().
"""

import re
import time
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BaseScraper(ABC):

    def __init__(self, config):
        """
        config: the retailer dict from config.RETAILERS
        """
        self.config    = config
        self.base_url  = config['base_url']
        self.selectors = config.get('selectors', {})
        self.results   = []   # Populated by scrape_all()

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def scrape_all(self):
        """
        Scrape every enabled category. Returns a list of product dicts.
        Each dict has keys: sku, name, url, category, price, rank,
        review_count, sizes_available, sizes_oos, is_featured, image_url.
        """
        self.results = []
        for category_path in self.config.get('categories', []):
            logger.info(f'[{self.config["name"]}] Scraping category: {category_path}')
            try:
                products = self.scrape_category(category_path)
                self.results.extend(products)
                logger.info(f'[{self.config["name"]}] Found {len(products)} products in {category_path}')
            except Exception as e:
                logger.error(f'[{self.config["name"]}] Failed on {category_path}: {e}')
            time.sleep(2)   # Polite delay between categories
        return self.results

    # -----------------------------------------------------------------------
    # To implement in subclasses
    # -----------------------------------------------------------------------

    @abstractmethod
    def scrape_category(self, category_path):
        """
        Scrape a single category page.
        Returns list of product dicts (see scrape_all docstring for keys).
        """
        pass

    @abstractmethod
    def scrape_product(self, product_url):
        """
        Scrape a single product detail page.
        Returns a dict with extra detail (sizes, reviews, etc.).
        Returns None if the page cannot be fetched.
        """
        pass

    # -----------------------------------------------------------------------
    # Shared utilities
    # -----------------------------------------------------------------------

    @staticmethod
    def extract_sku_from_url(url):
        """
        Try to extract a stable product identifier from a URL.
        Handles common patterns like /p/product-name-12345678 or ?id=12345.
        Falls back to the last URL segment if no number found.
        """
        # Pattern: last numeric sequence in the URL path
        matches = re.findall(r'\d{5,}', url)
        if matches:
            return matches[-1]
        # Fall back: last path segment
        return url.rstrip('/').split('/')[-1]

    @staticmethod
    def clean_price(raw_price):
        """Convert a price string like '£12.00' or '$9.99' to a float."""
        if not raw_price:
            return None
        cleaned = re.sub(r'[^\d.]', '', str(raw_price))
        try:
            return float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def clean_text(text):
        if not text:
            return ''
        return ' '.join(text.strip().split())

    def log(self, message):
        logger.info(f'[{self.config["name"]}] {message}')

    def warn(self, message):
        logger.warning(f'[{self.config["name"]}] {message}')
