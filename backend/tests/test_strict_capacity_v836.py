"""v8.3.6 STRICT capacity enforcement regression tests.

Policy (set in stone for this version):
  * `_effective_capacity(w)` MUST return `capacity_max` verbatim.
  * `reported_capacity` is telemetry-only — it is never used by the scheduler.
  * Distribution capacity sum is sum of `capacity_max` across online workers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import _effective_capacity  # noqa: E402


def test_ignores_reported_when_lower_than_admin():
    """Worker reported 30 but admin set 100 → must return 100 (no auto-shrink)."""
    w = {"capacity_max": 100, "reported_capacity": 30}
    assert _effective_capacity(w) == 100


def test_ignores_reported_when_higher_than_admin():
    """Worker reported 128 but admin set 80 → must return 80 (admin is law)."""
    w = {"capacity_max": 80, "reported_capacity": 128}
    assert _effective_capacity(w) == 80


def test_admin_cap_of_one_is_strict():
    """The smoking-gun user complaint: cap=1 means 1, not 70."""
    w = {"capacity_max": 1, "reported_capacity": 70}
    assert _effective_capacity(w) == 1


def test_missing_reported_returns_admin():
    w = {"capacity_max": 50}
    assert _effective_capacity(w) == 50


def test_zero_admin_cap_returns_zero():
    w = {"capacity_max": 0, "reported_capacity": 999}
    assert _effective_capacity(w) == 0


def test_default_when_no_cap_set():
    """Brand new worker without any cap → default 100 (existing behaviour)."""
    assert _effective_capacity({}) == 100


def test_garbage_reported_does_not_crash():
    w = {"capacity_max": 25, "reported_capacity": "bad"}
    # We don't even look at reported anymore — should be 25.
    assert _effective_capacity(w) == 25
