import logging

import pytest

from scraping.base_scraper import BaseScraper


@pytest.mark.parametrize("platform", ["Amazon", "Myntra", "Meesho", "Flipkart"])
def test_diagnostic_logs_safe_url_parts_and_classified_status(caplog, platform):
    scraper = BaseScraper()
    requested = "https://user:password@shop.example/search?q=private-product&token=private-token"
    final = "https://shop.example/security/check?session=private-session"

    with caplog.at_level(logging.INFO, logger="scraping.base_scraper"):
        scraper.log_diagnostic(
            platform, "final", requested_url=requested, final_url=final,
            challenge=True, challenge_reason="html_signal",
            product_card_count=12, normalized_product_count=0,
            final_status="BLOCKED", reason_category="challenge_detected",
        )

    line = next(line for line in caplog.text.splitlines() if "[SCRAPER_DIAGNOSTIC]" in line)
    assert f"platform={platform}" in line
    assert "requested_host=shop.example" in line
    assert "final_host=shop.example" in line
    assert "final_path=/security/check" in line
    assert "final_status=BLOCKED" in line
    assert "reason_category=challenge_detected" in line
    assert "challenge=true" in line
    assert "password" not in line
    assert "private-product" not in line
    assert "private-token" not in line
    assert "private-session" not in line
    assert "?" not in line


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("SUCCESS", "success"),
        ("BLOCKED", "challenge_detected"),
        ("TIMEOUT", "timeout"),
        ("NETWORK_ERROR", "network_failure"),
        ("PARSE_ERROR", "parser_failure"),
        ("EMPTY", "no_products"),
        ("FAILED", "scraper_failure"),
    ],
)
def test_diagnostic_reason_categories_preserve_existing_statuses(status, reason):
    expected_status = "PARSE_ERROR" if status == "FAILED" else status
    assert BaseScraper.classify_status(status) == expected_status
    assert BaseScraper.result_reason_category(status) == reason


def test_challenge_reason_is_category_only():
    assert BaseScraper.challenge_reason_category(
        True, "captcha page", "https://amazon.example/errors/validateCaptcha",
        url_markers=("errors/validateCaptcha",), html_markers=("captcha",),
    ) == "url_pattern"
    assert BaseScraper.challenge_reason_category(
        True, "captcha page", "https://amazon.example/search",
        url_markers=("errors/validateCaptcha",), html_markers=("captcha",),
    ) == "html_signal"
    assert BaseScraper.challenge_reason_category(False, "", "") == "not_detected"
