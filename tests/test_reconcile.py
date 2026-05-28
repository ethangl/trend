from trend.reconcile import Severity, compute, format_report, recommended_action


def test_all_match_is_ok():
    report = compute({"MES": 1, "MGC": 0}, {"MES": 1, "MGC": 0})
    assert report.overall is Severity.OK
    assert all(d.severity is Severity.OK for d in report.deltas)


def test_mismatch_with_threshold_zero_is_halt():
    report = compute({"MES": 5}, {"MES": 3}, halt_threshold=0)
    assert report.overall is Severity.HALT
    [d] = report.deltas
    assert d.symbol == "MES"
    assert d.delta == 2
    assert d.severity is Severity.HALT


def test_mismatch_within_threshold_is_warn_not_halt():
    report = compute({"MES": 5}, {"MES": 4}, halt_threshold=1)
    assert report.overall is Severity.WARN
    [d] = report.deltas
    assert d.delta == 1
    assert d.severity is Severity.WARN


def test_symbol_missing_from_one_side_treated_as_zero():
    report = compute({"MES": 3}, {"MNQ": 2}, halt_threshold=0)
    assert report.overall is Severity.HALT
    mes = next(d for d in report.deltas if d.symbol == "MES")
    mnq = next(d for d in report.deltas if d.symbol == "MNQ")
    assert mes.expected == 3 and mes.actual == 0 and mes.delta == 3
    assert mnq.expected == 0 and mnq.actual == 2 and mnq.delta == -2


def test_overall_promotes_to_worst_severity():
    # One OK, one WARN, one HALT → overall HALT
    report = compute(
        expected={"A": 1, "B": 5, "C": 10},
        actual={"A": 1, "B": 4, "C": 0},
        halt_threshold=1,
    )
    assert report.overall is Severity.HALT


def test_format_report_is_human_readable():
    report = compute({"MES": 3, "MGC": 1}, {"MES": 0, "MGC": 1})
    text = format_report(report)
    assert "MES" in text
    assert "+3" in text  # expected
    assert "+0" in text  # actual
    assert "HALT" in text.upper()


def test_recommended_action_first_run_scenario():
    # Cells expect positions, IBKR is flat — classic first-run.
    report = compute(
        expected={"MES": 2, "MNQ": -1},
        actual={"MES": 0, "MNQ": 0},
        halt_threshold=0,
    )
    action = recommended_action(report)
    assert "first-run" in action.lower() or "first" in action.lower()
    assert "MES" in action
    assert "MNQ" in action


def test_recommended_action_stale_ibkr_position():
    # Cells flat, IBKR has stale long.
    report = compute(
        expected={"MES": 0},
        actual={"MES": 3},
        halt_threshold=0,
    )
    action = recommended_action(report)
    assert "stale" in action.lower() or "manual" in action.lower()


def test_recommended_action_clean_is_noop():
    report = compute({"MES": 1}, {"MES": 1})
    action = recommended_action(report)
    assert "no action" in action.lower() or "match" in action.lower()
