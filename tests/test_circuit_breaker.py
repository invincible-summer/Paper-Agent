"""Tests for the per-tool circuit breaker (DESIGN D-068, agent-develop V4).

Uses a fresh breaker instance (not the module global) so tests are isolated.
"""

from core.circuit_breaker import CircuitBreaker
from core.tool_protocol import ErrorCode


def _breaker():
    b = CircuitBreaker(threshold=3, cooldown_s=300.0)
    return b


def test_closed_below_threshold_allows_call():
    b = _breaker()
    b.record_failure("search")
    b.record_failure("search")
    assert b.guard("search") is None  # 2 fails, still closed


def test_opens_at_threshold():
    b = _breaker()
    for _ in range(3):
        b.record_failure("search")
    g = b.guard("search")
    assert g is not None
    assert g.is_error
    assert g.error_code == ErrorCode.CIRCUIT_OPEN


def test_independent_per_tool():
    b = _breaker()
    for _ in range(3):
        b.record_failure("search")
    # graph breaker unaffected
    assert b.guard("graph") is None
    assert b.guard("search") is not None


def test_success_resets_failure_count():
    b = _breaker()
    b.record_failure("search")
    b.record_failure("search")
    b.record_success("search")
    # failure count reset; need 3 more to open
    b.record_failure("search")
    b.record_failure("search")
    assert b.guard("search") is None


def test_half_open_after_cooldown_allows_one_trial():
    b = CircuitBreaker(threshold=1, cooldown_s=0.05)
    b.record_failure("search")  # 1 -> open
    assert b.guard("search") is not None
    import time
    time.sleep(0.06)
    # cooldown elapsed -> half-open, allow a trial
    assert b.guard("search") is None
    # that trial failing re-opens
    b.record_failure("search")
    assert b.guard("search") is not None
