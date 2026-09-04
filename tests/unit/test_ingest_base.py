"""parse_money / parse_ts / diff_rows / Upserter diff semantics (no Postgres)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ingest.base import Upserter, diff_rows, jsonable, parse_money, parse_ts


class TestParseMoney:
    def test_object_shape(self):
        assert parse_money({"value": "8517.26", "currency": "USD", "display": "$8517.26 USD"}) == (
            Decimal("8517.26"),
            "USD",
        )

    def test_string_with_currency(self):
        assert parse_money("517.26 USD") == (Decimal("517.26"), "USD")

    def test_bare_string(self):
        assert parse_money("12500.00") == (Decimal("12500.00"), "USD")

    def test_display_string_with_symbol(self):
        assert parse_money("$2500.00 USD") == (Decimal("2500.00"), "USD")
        assert parse_money("€1,250.5") == (Decimal("1250.50"), "EUR")

    def test_card_quantity_shape(self):
        assert parse_money({"quantity": "100000", "instrument_code_string": "USD"}) == (Decimal("100000.00"), "USD")

    def test_minor_units_shape(self):
        assert parse_money({"amount": 123456, "currency": "USD"}) == (Decimal("1234.56"), "USD")

    def test_none_and_numbers(self):
        assert parse_money(None) == (Decimal("0.00"), "USD")
        assert parse_money(9000) == (Decimal("9000.00"), "USD")
        assert parse_money(Decimal("2500")) == (Decimal("2500.00"), "USD")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_money("twelve dollars")
        with pytest.raises(ValueError):
            parse_money(True)


class TestParseTs:
    def test_iso_z(self):
        assert parse_ts("2026-09-09T12:00:00.000Z") == datetime(2026, 9, 9, 12, tzinfo=UTC)

    def test_naive_iso_is_utc(self):
        assert parse_ts("2026-07-16T04:43:49") == datetime(2026, 7, 16, 4, 43, 49, tzinfo=UTC)

    def test_epoch_millis_and_seconds(self):
        assert parse_ts(1775239049411).year == 2026
        assert parse_ts(1775239049411).month == 4
        assert parse_ts("1756800000.000100") == datetime.fromtimestamp(1756800000.0001, tz=UTC)

    def test_none(self):
        assert parse_ts(None) is None
        assert parse_ts("") is None


class TestDiff:
    def test_created_lists_every_non_null_field(self):
        d = diff_rows(None, {"a": 1, "b": None, "c": "x"}, ["a", "b", "c"])
        assert d == {"a": [None, 1], "c": [None, "x"]}

    def test_no_change_is_empty(self):
        old = {"amount": Decimal("6500.00"), "due_at": datetime(2026, 9, 9, 12, tzinfo=UTC), "labels": ["Bug"]}
        new = {"amount": Decimal("6500.0"), "due_at": "2026-09-09T12:00:00Z", "labels": ("Bug",)}
        # Equivalent values in different Python types do not register as changes.
        new["due_at"] = datetime(2026, 9, 9, 12, tzinfo=UTC)
        assert diff_rows(old, new, ["amount", "due_at", "labels"]) == {}

    def test_changed_field_reports_old_and_new(self):
        d = diff_rows(
            {"status": "Backlog", "assignee": None}, {"status": "Done", "assignee": "Alexey"}, ["status", "assignee"]
        )
        assert d == {"status": ["Backlog", "Done"], "assignee": [None, "Alexey"]}

    def test_jsonable(self):
        out = jsonable({"amt": Decimal("1.50"), "at": datetime(2026, 1, 1, tzinfo=UTC), "xs": (1, 2)})
        assert out == {"amt": "1.50", "at": "2026-01-01T00:00:00+00:00", "xs": [1, 2]}


class TestUpserterConfig:
    def test_compare_excludes_json_and_skipped(self):
        u = Upserter(
            "issues", "linear", ["id"], ["id", "title", "raw", "uuid"], json_cols=["raw"], skip_compare=["uuid"]
        )
        assert u.compare == ["id", "title"]

    def test_rejects_unsafe_identifiers(self):
        with pytest.raises(ValueError):
            Upserter("issues; drop table x", "linear", ["id"], ["id"])
        with pytest.raises(ValueError):
            Upserter("issues", "linear", ["id"], ["id", "bad col"])

    def test_composite_entity_id(self):
        u = Upserter("messages", "slack", ["source", "id"], ["source", "id", "text"])
        assert u._entity_id({"source": "slack", "id": "1.2"}) == "slack:1.2"
