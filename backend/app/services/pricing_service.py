from sqlalchemy.orm import Session
from app.models.retailer_product import RetailerProduct
from app.models.competitor_product import CompetitorProduct
from app.models.price_analysis import PriceAnalysis
from app.models.recommendation import Recommendation
from app.services.market_statistics import calculate_market_statistics
from typing import Optional
import logging
import math

logger = logging.getLogger(__name__)


def minimum_viable_selling_price(cost_price: float, minimum_profit_margin: float) -> float:
    """Single pricing rule for the retailer's non-negotiable minimum margin."""
    cost = float(cost_price)
    margin = float(minimum_profit_margin)
    if cost <= 0:
        raise ValueError("Cost price must be greater than zero")
    if margin < 0:
        raise ValueError("Minimum profit margin cannot be negative")
    return cost * (1 + margin / 100.0)

def calculate_market_average(db: Session, catalog_product_id: int, retailer_product: RetailerProduct | None = None) -> dict:
    all_competitors = db.query(CompetitorProduct).filter(
        CompetitorProduct.catalog_product_id == catalog_product_id,
        CompetitorProduct.observation_source == "SCRAPE",
        CompetitorProduct.scraped_at.isnot(None),
    ).all()

    logger.info(f"[PRICING_INPUT_UPDATED] catalog_product_id={catalog_product_id} raw_competitor_count={len(all_competitors)}")

    if retailer_product is None:
        offers = [{"exclusion_reason": "target_quantity_unavailable"} for _ in all_competitors]
    else:
        from app.services.relevance_filter_service import classify_candidate, strip_match_marker
        from app.utils.quantity import listing_quantity_is_uncertain, price_at_target_pack, strip_quantity_marker

        offers = []
        for competitor in all_competitors:
            try:
                raw_price = float(competitor.price)
            except (TypeError, ValueError):
                raw_price = float("nan")
            if not math.isfinite(raw_price) or raw_price <= 0:
                offers.append({"exclusion_reason": "invalid_price"})
                continue
            quality, _ = classify_candidate(
                retailer_product.product_name, competitor.product_name or "",
                retailer_product.product_details or "",
                strip_match_marker(strip_quantity_marker(competitor.product_details)) or "",
            )
            if quality not in {"exact", "alternative"}:
                offers.append({"exclusion_reason": "incompatible_product" if quality == "rejected" else "uncertain_product_match"})
                continue
            if listing_quantity_is_uncertain(competitor):
                offers.append({"exclusion_reason": "quantity_uncertain"})
                continue
            if not retailer_product.quantity_value or not retailer_product.quantity_unit:
                offers.append({"exclusion_reason": "target_quantity_unavailable"})
                continue
            equivalent = price_at_target_pack(
                raw_price, retailer_product.quantity_value, retailer_product.quantity_unit,
                competitor.quantity_value, competitor.quantity_unit, competitor.pack_count,
                competitor.total_quantity, competitor.total_quantity_unit,
            )
            if equivalent is None or not math.isfinite(equivalent) or equivalent <= 0:
                unit = competitor.total_quantity_unit or competitor.quantity_unit
                reason = "incompatible_quantity_dimension" if unit else "quantity_unavailable"
                offers.append({"exclusion_reason": reason})
                continue
            offers.append({"price": equivalent, "marketplace": competitor.platform_name})

    stats = calculate_market_statistics(offers)
    logger.info(f"[PRICING_ANALYSIS_COMPLETED] catalog_product_id={catalog_product_id} comparable_count={stats['comparable_offer_count']}")
    return {
        "average": stats["mean_price"] or 0.0,
        "median": stats["median_price"],
        "typical_range_low": stats["typical_price_range_low"],
        "typical_range_high": stats["typical_price_range_high"],
        "minimum": stats["observed_minimum_price"] or 0.0,
        "maximum": stats["observed_maximum_price"] or 0.0,
        "count": stats["comparable_offer_count"],
        "distinct_marketplace_count": stats["distinct_marketplace_count"],
        "outlier_count": stats["outlier_count"],
        "exclusion_count": stats["exclusion_count"],
        "exclusion_reasons": stats["exclusion_reasons"],
    }

def calculate_price_analysis(db: Session, retailer_product_id: int) -> PriceAnalysis:
    retailer_prod = db.query(RetailerProduct).filter(RetailerProduct.id == retailer_product_id).first()
    if not retailer_prod:
        raise ValueError("Retailer product not found")
    if retailer_prod.cost_price <= 0:
        raise ValueError("Cost price must be greater than zero")
        
    market_stats = calculate_market_average(db, retailer_prod.catalog_product_id, retailer_prod)
    
    diff_percentage = 0.0
    if market_stats["average"] > 0:
        diff_percentage = ((market_stats["average"] - retailer_prod.cost_price) / retailer_prod.cost_price) * 100
    
    pack_basis = (f"normalized to {retailer_prod.quantity_value:g} {retailer_prod.quantity_unit} per pack. "
                  if retailer_prod.quantity_value and retailer_prod.quantity_unit else "No target pack quantity was available. ")
    summary = (f"Analyzed {market_stats['count']} quantity-comparable competitor prices, {pack_basis}"
               f"Mean is {market_stats['average']:.2f}; median is {market_stats['median'] if market_stats['median'] is not None else 'unavailable'}; "
               f"typical range is {market_stats['typical_range_low']}–{market_stats['typical_range_high']}. "
               f"{market_stats['outlier_count']} outlier(s) flagged and retained; {market_stats['exclusion_count']} offer(s) excluded.")
    
    analysis = PriceAnalysis(
        retailer_product_id=retailer_product_id,
        average_market_price=market_stats["average"],
        minimum_market_price=market_stats["minimum"],
        maximum_market_price=market_stats["maximum"],
        competitor_count=market_stats["count"],
        price_difference_percentage=diff_percentage,
        analysis_summary=summary
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)
    return analysis

def generate_recommendation(db: Session, retailer_product_id: int) -> Recommendation:
    retailer_product = db.query(RetailerProduct).filter(RetailerProduct.id == retailer_product_id).first()
    if not retailer_product:
        raise ValueError("Retailer product not found")
        
    analysis = db.query(PriceAnalysis).filter(PriceAnalysis.retailer_product_id == retailer_product_id).order_by(PriceAnalysis.id.desc()).first()
    if not analysis:
        # Generate analysis if not already present
        analysis = calculate_price_analysis(db, retailer_product_id)
        
    cost_price = retailer_product.cost_price
    min_profit_margin = retailer_product.minimum_profit_margin
    min_selling_price = minimum_viable_selling_price(cost_price, min_profit_margin)
    
    market_avg = analysis.average_market_price or 0.0
    min_market = analysis.minimum_market_price or 0.0
    max_market = analysis.maximum_market_price or 0.0
    
    # Calculate recommended price
    recommended_price = market_avg if market_avg > min_selling_price else min_selling_price
        
    expected_profit = recommended_price - cost_price
    profit_percentage = (expected_profit / cost_price) * 100 if cost_price > 0 else 0
    
    reasoning = f"Competitor prices range between ₹{min_market} and ₹{max_market}. The recommended price maintains retailer profit margin while remaining competitive."
    
    rec = Recommendation(
        retailer_product_id=retailer_product_id,
        recommended_price=recommended_price,
        expected_profit=expected_profit,
        profit_percentage=profit_percentage,
        reasoning=reasoning,
        confidence_score=0.90,
        accepted_by_user=False
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    
    return rec

def get_price_analysis(db: Session, product_id: int, user_id: int) -> Optional[PriceAnalysis]:
    return db.query(PriceAnalysis).join(
        RetailerProduct, PriceAnalysis.retailer_product_id == RetailerProduct.id
    ).filter(
        PriceAnalysis.retailer_product_id == product_id,
        RetailerProduct.user_id == user_id
    ).order_by(PriceAnalysis.id.desc()).first()
