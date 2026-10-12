"""Exercise the real Steam client and dead confirmation with offline HTTP responses."""
import json
from types import SimpleNamespace

import pytest
import requests

from config import Config
from core.api_client import SteamApiClient
from core.dead_store import DeadStore
from server import _confirm_dead


WORKSHOP_ID = "130928055"
MIRROR_SHELL = {"id": WORKSHOP_ID, "title": "", "is_fallback": True}


@pytest.fixture
def clients(tmp_path):
    key_path = tmp_path / "steam-key.json"
    key_path.write_text(json.dumps({"key": "TEST_KEY"}), encoding="utf-8")
    cfg = Config(data_dir=str(tmp_path), steam_api_key_path=str(key_path))
    return SimpleNamespace(steam=SteamApiClient(cfg)), DeadStore(cfg)


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode("utf-8")
    return result


@pytest.mark.parametrize("failure", [
    "timeout", "connection", "http_503", "invalid_json",
    "service_error", "item_service_error", "missing_details",
])
def test_steam_request_failure_does_not_persist_dead_map(clients, monkeypatch, failure):
    api, dead = clients

    def post(*args, **kwargs):
        if failure == "timeout":
            raise requests.Timeout("temporary timeout")
        if failure == "connection":
            raise requests.ConnectionError("temporary connection failure")
        if failure == "http_503":
            return response({}, status=503)
        if failure == "invalid_json":
            result = response({})
            result._content = b"not json"
            return result
        if failure == "service_error":
            return response({"response": {"result": 2}})
        if failure == "item_service_error":
            return response({"response": {
                "result": 1, "resultcount": 1,
                "publishedfiledetails": [{"publishedfileid": WORKSHOP_ID, "result": 2}],
            }})
        return response({"response": {"result": 1}})

    monkeypatch.setattr(api.steam.session, "post", post)
    assert _confirm_dead(api, dead, WORKSHOP_ID, MIRROR_SHELL) is None
    assert dead.is_dead(WORKSHOP_ID) is None
    assert not dead.path.exists()


@pytest.mark.parametrize("item,expected_dead", [
    ({"publishedfileid": WORKSHOP_ID, "result": 1, "title": "Real Map"}, False),
    ({"publishedfileid": WORKSHOP_ID, "result": 9}, True),
    ({"publishedfileid": WORKSHOP_ID, "result": 15}, True),
    ({"publishedfileid": WORKSHOP_ID, "result": 1, "title": ""}, True),
])
def test_successful_steam_confirmation_preserves_valid_and_missing_maps(
    clients, monkeypatch, item, expected_dead,
):
    api, dead = clients
    monkeypatch.setattr(api.steam.session, "post", lambda *args, **kwargs: response({
        "response": {"result": 1, "resultcount": 1, "publishedfiledetails": [item]},
    }))
    record = _confirm_dead(api, dead, WORKSHOP_ID, MIRROR_SHELL)
    assert (record is not None) is expected_dead
    assert (DeadStore(api.steam.cfg).is_dead(WORKSHOP_ID) is not None) is expected_dead
