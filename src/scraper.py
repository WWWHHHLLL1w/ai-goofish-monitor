"""Compatibility import for the supported lightweight scraper implementation."""
from src.scraper2 import LoginRequiredError, RiskControlError, scrape_xianyu

__all__ = ["LoginRequiredError", "RiskControlError", "scrape_xianyu"]
