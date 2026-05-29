from datetime import date

import pytest

from trend.roll import (
    ContractInfo,
    Severity,
    evaluate,
    evaluate_all,
    format_warnings,
    needs_action,
    next_expiry,
    parse_ib_expiry,
)


def _info(symbol: str, days_out: int, today: date) -> ContractInfo:
    return ContractInfo(
        symbol=symbol,
        contract_label=f"{symbol}TEST",
        last_trade_date=date.fromordinal(today.toordinal() + days_out),
    )


def test_evaluate_far_from_expiry_is_ok():
    today = date(2026, 5, 24)
    info = _info("MES", days_out=60, today=today)
    w = evaluate(info, today)
    assert w.severity is Severity.OK
    assert w.days_until_expiry == 60


def test_evaluate_inside_warn_window():
    today = date(2026, 5, 24)
    info = _info("MES", days_out=10, today=today)
    w = evaluate(info, today, warn_days=14, roll_days=7)
    assert w.severity is Severity.WARN


def test_evaluate_inside_roll_window():
    today = date(2026, 5, 24)
    info = _info("MES", days_out=5, today=today)
    w = evaluate(info, today, warn_days=14, roll_days=7)
    assert w.severity is Severity.ROLL_NOW


def test_evaluate_already_expired():
    today = date(2026, 5, 24)
    info = _info("MES", days_out=-2, today=today)
    w = evaluate(info, today)
    assert w.severity is Severity.ROLL_NOW
    assert w.days_until_expiry == -2


def test_evaluate_all_with_commodity_thresholds():
    today = date(2026, 5, 24)
    infos = [
        _info("MES", days_out=15, today=today),  # financial, 15d out → OK
        _info("MCL", days_out=15, today=today),  # commodity, 15d out → ROLL_NOW (20d threshold)
        _info("MGC", days_out=25, today=today),  # commodity, 25d out → WARN (30d threshold)
    ]
    warnings = evaluate_all(
        infos, today,
        commodity_symbols={"MCL", "MGC"},
        warn_days=14, roll_days=7,
        commodity_warn_days=30, commodity_roll_days=20,
    )
    by_sym = {w.symbol: w for w in warnings}
    assert by_sym["MES"].severity is Severity.OK         # 15 > 14
    assert by_sym["MCL"].severity is Severity.ROLL_NOW   # 15 ≤ 20
    assert by_sym["MGC"].severity is Severity.WARN       # 20 < 25 ≤ 30


def test_format_warnings_renders():
    today = date(2026, 5, 24)
    warnings = [evaluate(_info("MES", days_out=20, today=today), today)]
    text = format_warnings(warnings)
    assert "MES" in text
    assert "20" in text


def test_needs_action_filters_ok():
    today = date(2026, 5, 24)
    warnings = [
        evaluate(_info("MES", days_out=60, today=today), today),  # OK
        evaluate(_info("MNQ", days_out=10, today=today), today),  # WARN
        evaluate(_info("MGC", days_out=3, today=today), today),   # ROLL_NOW
    ]
    actionable = needs_action(warnings)
    assert len(actionable) == 2
    assert all(w.severity is not Severity.OK for w in actionable)


def test_parse_ib_expiry_full_date():
    assert parse_ib_expiry("20260618") == date(2026, 6, 18)


def test_parse_ib_expiry_month_only_is_first():
    assert parse_ib_expiry("202606") == date(2026, 6, 1)


def test_parse_ib_expiry_rejects_garbage():
    with pytest.raises(ValueError):
        parse_ib_expiry("2026")


def test_next_expiry_picks_successor():
    expiries = ["20260618", "20260918", "20261218", "20260318"]
    assert next_expiry(expiries, "20260618") == "20260918"


def test_next_expiry_none_when_current_is_latest():
    expiries = ["20260318", "20260618"]
    assert next_expiry(expiries, "20260618") is None


def test_next_expiry_handles_mixed_formats():
    # A YYYYMM entry sorts as the start of that month, before YYYYMMDD of same month.
    expiries = ["202609", "20260618"]
    assert next_expiry(expiries, "20260618") == "202609"
