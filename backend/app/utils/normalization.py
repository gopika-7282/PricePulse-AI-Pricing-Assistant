"""Small, dependency-free normalization helpers."""
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    value = value.replace("'", "")
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def normalize_competitor_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    tracking = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "tag", "qid", "sid"}
    query = urlencode([(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if key.lower() not in tracking])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))
