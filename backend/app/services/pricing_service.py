from sqlalchemy.orm import Session
from app.models.retailer_product import RetailerProduct
from app.models.competitor_product import CompetitorProduct
from app.models.price_analysis import PriceAnalysis
from app.models.recommendation import Recommendation
from typing import Optional

def calculate_market_average(db: Session, catalog_product_id: int) -> dict:
    competitors = db.query(CompetitorProduct).filter(CompetitorProduct.catalog_product_id == catalog_product_id).all()
    if not competitors:
        return {
            "average": 0.0,
            "minimum": 0.0,
            "maximum": 0.0,
            "count": 0
        }
    
    prices = [c.price for c in competitors]
    return {
        "average": sum(prices) / len(prices),
        "minimum": min(prices),
        "maximum": max(prices),
        "count": len(prices)
    }

def calculate_price_analysis(db: Session, retailer_product_id: int) -> PriceAnalysis:
    retailer_prod = db.query(RetailerProduct).filter(RetailerProduct.id == retailer_product_id).first()
    if not retailer_prod:
        raise ValueError("Retailer product not found")
        
    market_stats = calculate_market_average(db, retailer_prod.catalog_product_id)
    
    diff_percentage = 0.0
    if market_stats["average"] > 0:
        diff_percentage = ((market_stats["average"] - retailer_prod.cost_price) / retailer_prod.cost_price) * 100
    
    summary = f"Analyzed {market_stats['count']} competitor prices. Market average is {market_stats['average']}."
    
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
    min_selling_price = cost_price * (1 + (min_profit_margin / 100.0))
    
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
