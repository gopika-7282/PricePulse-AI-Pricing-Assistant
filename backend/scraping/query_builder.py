"""Build short marketplace queries from product attributes."""
import re
from typing import Optional


def focused_product_query(product_name: str, category: Optional[str] = None, details: Optional[str] = None) -> str:
    name = re.sub(r"\b\d+\s*(?:ml|gm|g|kg|l|oz|pack)\b", "", product_name or "", flags=re.I)
    result = list(re.sub(r"['’]", "", name).split()[:5]) or (product_name or "").split()
    seen = {word.casefold() for word in result}
    if category:
        category_words = str(category).split()[:3]
        if category_words and str(category).casefold() not in " ".join(result).casefold():
            result.extend(category_words)
            seen.update(word.casefold() for word in category_words)
    for value, limit in ((details, 10),):
        if isinstance(value, (list, tuple)):
            value = " ".join(map(str, value))
        for word in re.findall(r"[\w-]+", str(value or "")):
            if len(word) < 3 or word.casefold() in {"and", "for", "the", "with", "all", "our", "pure"}:
                continue
            if word.casefold() not in seen and len(result) < limit:
                result.append(word)
                seen.add(word.casefold())
    return " ".join(result)
