"""Tests for calculate_logs_only_quality_score."""

from graph.confidence_calculator import ConfidenceCalculator


def test_logs_only_full_score():
    calc = ConfidenceCalculator()
    score = calc.calculate_logs_only_quality_score(
        log_count=5000,
        error_rate_node=0.0,
        is_inferred=False,
        call_count=200,
        confidence=0.8,
    )
    assert score >= 90


def test_logs_only_error_penalty():
    calc = ConfidenceCalculator()
    score = calc.calculate_logs_only_quality_score(
        log_count=5000,
        error_rate_node=0.5,
        is_inferred=False,
        call_count=200,
        confidence=0.8,
    )
    assert score <= 80
    assert score >= 60


def test_logs_only_inferred_penalty():
    calc = ConfidenceCalculator()
    score = calc.calculate_logs_only_quality_score(
        log_count=5000,
        error_rate_node=0.0,
        is_inferred=True,
        call_count=None,
        confidence=0.5,
    )
    assert score <= 95
    assert score >= 85


def test_logs_only_low_confidence():
    calc = ConfidenceCalculator()
    score = calc.calculate_logs_only_quality_score(
        log_count=5000,
        error_rate_node=0.0,
        is_inferred=False,
        call_count=200,
        confidence=0.2,
    )
    assert score == 95.0


def test_logs_only_low_volume():
    calc = ConfidenceCalculator()
    score = calc.calculate_logs_only_quality_score(
        log_count=50,
        error_rate_node=0.0,
        is_inferred=False,
        call_count=None,
        confidence=0.8,
    )
    # log_count=50 < 100 → -10 → 90
    assert score == 90.0
