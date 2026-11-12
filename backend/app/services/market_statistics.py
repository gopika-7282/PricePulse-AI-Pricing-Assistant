"""Deterministic descriptive statistics for validated comparable offers."""

from __future__ import annotations

from math import isfinite
import re
from difflib import SequenceMatcher
from datetime import datetime, timezone
from statistics import fmean, median, quantiles
from typing import Any, Iterable


def deduplicate_comparable_offers(offers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove repeat representations of the same same-marketplace offer.

    Identical canonical URLs are one offer. Without a URL, near-identical title,
    normalized pack quantity, and normalized price are treated as one listing.
    A distinct title or materially different price remains separate evidence.
    Excluded rows are retained as ``duplicate_offer`` observations for audit.
    """
    from app.services.competitor_service import normalize_competitor_url

    def freshness(offer: dict[str, Any]) -> float:
        scraped_at = offer.get("scraped_at")
        if not isinstance(scraped_at, datetime):
            return 0.0
        if scraped_at.tzinfo is None:
            scraped_at = scraped_at.replace(tzinfo=timezone.utc)
        return scraped_at.timestamp()

    unique: list[dict[str, Any]] = []
    seen: list[dict[str, Any]] = []
    for offer in sorted(offers, key=freshness, reverse=True):
        if offer.get("exclusion_reason"):
            unique.append(offer)
            continue
        marketplace = str(offer.get("marketplace") or "Unknown").casefold().strip()
        url = normalize_competitor_url(offer.get("product_url"))
        title = re.sub(r"[^a-z0-9]+", " ", str(offer.get("product_name") or "").casefold()).strip()
        duplicate = False
        for prior in seen:
            if marketplace != str(prior.get("marketplace") or "Unknown").casefold().strip():
                continue
            prior_url = normalize_competitor_url(prior.get("product_url"))
            same_url = bool(url and prior_url and url.casefold() == prior_url.casefold())
            prior_title = re.sub(r"[^a-z0-9]+", " ", str(prior.get("product_name") or "").casefold()).strip()
            title_similarity = SequenceMatcher(None, title, prior_title).ratio() if title and prior_title else 0
            same_pack = (offer.get("quantity_dimension") == prior.get("quantity_dimension")
                         and offer.get("normalized_quantity") is not None
                         and abs(float(offer["normalized_quantity"]) - float(prior["normalized_quantity"])) <= 1e-9)
            try:
                price, prior_price = float(offer["price"]), float(prior["price"])
                near_price = abs(price - prior_price) <= max(0.01, max(price, prior_price) * 0.01)
            except (TypeError, ValueError, KeyError):
                near_price = False
            if same_url or (same_pack and near_price and title_similarity >= 0.96):
                duplicate = True
                break
        if duplicate:
            unique.append({"marketplace": offer.get("marketplace"), "exclusion_reason": "duplicate_offer"})
        else:
            seen.append(offer)
            unique.append(offer)
    return unique


def calculate_market_statistics(offers: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize valid normalized offers without dropping price outliers.

    The typical range is the inclusive interquartile range (Q1–Q3). Outliers
    are flagged using Tukey's 1.5×IQR fences, but remain in the mean and in
    the observed minimum/maximum. Callers can pass excluded observations with
    an ``exclusion_reason``; those offers never enter price statistics.
    """
    included: list[dict[str, Any]] = []
    exclusions: dict[str, int] = {}
    for offer in offers:
        reason = offer.get("exclusion_reason")
        if reason:
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        try:
            price = float(offer.get("price"))
        except (TypeError, ValueError):
            exclusions["invalid_price"] = exclusions.get("invalid_price", 0) + 1
            continue
        if not isfinite(price) or price <= 0:
            exclusions["invalid_price"] = exclusions.get("invalid_price", 0) + 1
            continue
        included.append({**offer, "price": price})

    prices = sorted(offer["price"] for offer in included)
    count = len(prices)
    if count == 0:
        return {
            "comparable_offer_count": 0,
            "distinct_marketplace_count": 0,
            "marketplaces": [],
            "mean_price": None,
            "median_price": None,
            "typical_price_range_low": None,
            "typical_price_range_high": None,
            "typical_range_method": "Inclusive sample quartiles (Q1–Q3).",
            "observed_minimum_price": None,
            "observed_maximum_price": None,
            "outlier_count": 0,
            "outlier_offers": [],
            "outlier_method": "Tukey 1.5×IQR fences; flagged offers remain included.",
            "exclusion_count": sum(exclusions.values()),
            "exclusion_reasons": exclusions,
        }

    outlier_offers = []
    if count == 1:
        q1 = q3 = prices[0]
        outlier_count = 0
    else:
        q1, q3 = quantiles(prices, n=4, method="inclusive")[0::2]
        iqr = q3 - q1
        lower_fence, upper_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        for offer in included:
            price = offer["price"]
            if price < lower_fence or price > upper_fence:
                outlier_offers.append({"marketplace": offer.get("marketplace") or "Unknown",
                                       "product_name": offer.get("product_name"),
                                       "price": round(price, 2),
                                       "reason": "Outside Tukey 1.5×IQR fences; retained in the statistics."})
        outlier_count = len(outlier_offers)

    marketplace_names = {}
    for offer in included:
        name = str(offer.get("marketplace") or "Unknown").strip() or "Unknown"
        marketplace_names.setdefault(name.casefold(), name)
    marketplaces = sorted(marketplace_names.values(), key=str.casefold)
    return {
        "comparable_offer_count": count,
        "distinct_marketplace_count": len(marketplaces),
        "marketplaces": marketplaces,
        "mean_price": round(fmean(prices), 2),
        "median_price": round(median(prices), 2),
        "typical_price_range_low": round(q1, 2),
        "typical_price_range_high": round(q3, 2),
        "typical_range_method": "Inclusive sample quartiles (Q1–Q3).",
        "observed_minimum_price": round(prices[0], 2),
        "observed_maximum_price": round(prices[-1], 2),
        "outlier_count": outlier_count,
        "outlier_offers": outlier_offers,
        "outlier_method": "Tukey 1.5×IQR fences; flagged offers remain included.",
        "exclusion_count": sum(exclusions.values()),
        "exclusion_reasons": exclusions,
    }


def market_confidence(offer_count: int, marketplace_count: int, *, stale: bool = False) -> float:
    """Conservative confidence based only on comparable sample breadth."""
    if offer_count <= 0:
        return 0.20
    if offer_count == 1:
        confidence = 0.30
    elif offer_count == 2:
        confidence = 0.40
    elif marketplace_count >= 3:
        confidence = 0.80 if offer_count < 5 else 0.90
    elif marketplace_count >= 2:
        confidence = 0.65 if offer_count < 5 else 0.75
    else:
        confidence = 0.45
    return min(confidence, 0.55) if stale else confidence


def market_position(price: float, reference: float | None, typical_low: float | None,
                    typical_high: float | None) -> str | None:
    if reference is None:
        return None
    if typical_low is not None and price < typical_low:
        return "below_typical_range"
    if typical_high is not None and price > typical_high:
        return "above_typical_range"
    if abs(price - reference) <= 0.01:
        return "at_market_reference"
    return "within_typical_range"
