"""
Tests for the User identity data class.

Locks the contract between `claims["sub"]` / `claims["username"]` and
the rest of the agent stack. If this class grows validation later, those
test changes will surface here.
"""

from user import User


def test_C1_user_stores_id_and_name():
    u = User(id="user-abc", name="alice")
    assert u.id == "user-abc"
    assert u.name == "alice"


def test_C2_user_accepts_any_string_no_validation():
    """No validation today. Locked so any future 'add validation' change
    must update this test, surfacing the behavior change in review."""
    u = User(id="", name="")
    assert u.id == ""
    assert u.name == ""
