"""Pure-Python API helper tests (no DB): money strings, cell sanitizer."""

from decimal import Decimal

from api.deps import compact_money, money, money_ccy, pill, sanitize_cell, status_pill


def test_money_two_decimals():
    assert money(Decimal("12500")) == "12500.00"
    assert money(None) == "0.00"
    assert money_ccy(Decimal("517.26")) == "517.26 USD"
    assert compact_money(2500) == "$2.5k"
    assert compact_money(0) == "$0"


def test_sanitize_allows_only_pill_and_link():
    assert sanitize_cell(pill("Paid", "paid")) == '<span class="status-pill status-paid">Paid</span>'
    good_link = '<a href="https://linear.app/x" target="_blank" rel="noopener">↗</a>'
    assert sanitize_cell(good_link) == good_link
    assert sanitize_cell("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert sanitize_cell('<span class="status-pill status-evil">x</span>').startswith("&lt;span")
    assert sanitize_cell('<a href="javascript:alert(1)" target="_blank" rel="noopener">↗</a>').startswith("&lt;a")
    assert sanitize_cell("<b>bold</b>") == "&lt;b&gt;bold&lt;/b&gt;"


def test_pill_escapes_text_and_clamps_class():
    assert pill("<x>", "nope") == '<span class="status-pill status-draft">&lt;x&gt;</span>'
    assert 'status-paid">READY' in status_pill("READY")
    assert 'status-unpaid">ERROR' in status_pill("ERROR")
