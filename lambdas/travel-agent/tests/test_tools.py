"""
Tests for the local (non-user-scoped) tools.

get_user_location resolves an IP via ip-api.com. get_todays_date returns
the system date. Tests mock the network and freeze time.
"""

import json
from unittest.mock import MagicMock, patch
import pytest


def _fake_urlopen_response(payload: dict):
    """Return an object shaped like the urlopen return: .read() yields bytes."""
    fake = MagicMock()
    fake.read.return_value = json.dumps(payload).encode("utf-8")
    return fake


def test_D1_get_user_location_formats_city_region_country():
    import tools
    payload = {"city": "Seattle", "region": "Washington", "country": "United States"}
    with patch.object(tools.request, "urlopen", return_value=_fake_urlopen_response(payload)):
        result = tools.get_user_location("203.0.113.42")
    assert result == "Seattle Washington, United States"


def test_D2_get_user_location_propagates_json_decode_error():
    import tools
    fake = MagicMock()
    fake.read.return_value = b"not json"
    with patch.object(tools.request, "urlopen", return_value=fake):
        with pytest.raises(json.JSONDecodeError):
            tools.get_user_location("203.0.113.42")


def test_D3_get_user_location_propagates_missing_field():
    import tools
    payload = {"city": "Seattle"}  # region + country missing
    with patch.object(tools.request, "urlopen", return_value=_fake_urlopen_response(payload)):
        with pytest.raises(KeyError):
            tools.get_user_location("203.0.113.42")


def test_D4_get_todays_date_returns_iso_yyyy_mm_dd(monkeypatch):
    import tools
    from datetime import datetime

    class FrozenDatetime(datetime):
        @classmethod
        def today(cls):
            return cls(2026, 5, 19)

    monkeypatch.setattr(tools, "datetime", FrozenDatetime)
    assert tools.get_todays_date() == "2026-05-19"
