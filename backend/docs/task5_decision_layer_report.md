# PRICEPULSE TASK 5: PRODUCT MATCH CONFIDENCE DECISION LAYER

## 1. Files Changed
- `app/config.py`: Added centralized `MATCH_CONFIG` dictionary to store threshold variables safely instead of scattering hardcoded values.
- `app/services/matching_service.py`: Rewrote the `find_catalog_match` function to safely evaluate confidence thresholds. Modified the return signature of both `find_catalog_match` and `find_existing_product` to output a structured decision dictionary.
- `app/services/product_service.py`: Updated the `create_retailer_product` flow to correctly unpack the structured matching decision without disrupting scraping, competitor logic, or DB commitments.
- `test_semantic.py`: Modified to run safety tests validating the new structured decision output directly.

## 2. Decision Logic
The matching service now enforces a strict AI similarity-based decision workflow, free of arbitrary keyword mapping or aliases.
Workflow:
1. Fetch Top-K candidates via ChromaDB `retrieve_catalog_candidates`.
2. Inspect `distance` of the #1 ranked candidate (Cosine Distance).
3. If `distance <= high_confidence_threshold` (0.15): Return `MATCHED` and reuse the existing ProductCatalog ID.
4. If `distance <= medium_confidence_threshold` (0.22): Return `UNCERTAIN` and do NOT force a match (returns None for Catalog ID).
5. If `distance > medium_confidence_threshold` (or if no candidates exist): Return `NEW_PRODUCT`.
6. Whenever `UNCERTAIN` or `NEW_PRODUCT` is returned, the product creation flow gracefully creates a brand new canonical `ProductCatalog` instance to remain completely data-safe.

## 3. Threshold Configuration
Centralized `MATCH_CONFIG` deployed in `app/config.py`:
```python
MATCH_CONFIG = {
    "top_k_candidates": 5,
    "high_confidence_threshold": 0.15,
    "medium_confidence_threshold": 0.22, # Tuned to separate uncertain matches from completely unrelated spaces
}
```

## 4. Tests Executed & Actual Results
The modified `test_semantic.py` was executed directly against the pipeline, ensuring no hardcoded overrides were active.
- **Test 1**: `Hibiscus Hair Oil` vs `Hibiscus Hair Oil` (Same Wording)
  - **Result**: ✅ PASSED. Distance: 0.126. Output: `MATCHED`.
- **Test 2**: `Gudhal Hair Oil` vs `Hair Oil` (Semantic embedding similarity / transliteration)
  - **Result**: ✅ PASSED. Distance: 0.171. Output: `UNCERTAIN` (Accurately recognized similarity but avoided dangerous automatic merging without explicit alias maps).
- **Test 3**: `Hibiscus Shampoo` vs `Hibiscus Hair Oil` (Product Confusion)
  - **Result**: ✅ PASSED. Distance: 0.181. Output: `UNCERTAIN` (Accurately identified it as semantically close but protected from automatic merging).
- **Test 4**: `Cooking Oil` vs `Hair Oil` (Unrelated)
  - **Result**: ✅ PASSED. Distance: 0.240. Output: `NEW_PRODUCT` (Correctly triggered low confidence creation cutoff).

## 5. Logging Updates
All requested logs are actively triggering to help trace the decision flows:
- `[MATCH_DECISION_STARTED]`
- `[HIGH_CONFIDENCE_MATCH]`
- `[UNCERTAIN_MATCH]`
- `[NEW_PRODUCT_CREATED]`
