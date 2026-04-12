from scrapers.primark import PrimarkScraper

SCRAPER_MAP = {
    'primark': PrimarkScraper,
}

def get_scraper(retailer_key, config):
    cls = SCRAPER_MAP.get(retailer_key)
    if not cls:
        raise ValueError(f'No scraper registered for retailer: {retailer_key}')
    return cls(config)
