from scrapers.primark import PrimarkScraper
from scrapers.newlook import NewLookScraper

SCRAPER_MAP = {
    'primark': PrimarkScraper,
    'newlook': NewLookScraper,
}

def get_scraper(retailer_key, config):
    cls = SCRAPER_MAP.get(retailer_key)
    if not cls:
        raise ValueError(f'No scraper registered for retailer: {retailer_key}')
    return cls(config)
