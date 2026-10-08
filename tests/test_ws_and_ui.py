import os

import pytest

from app.ws_listener import ws_url

from .conftest import episode

EP1, EP2 = episode(201, 1, 1), episode(202, 1, 2)


@pytest.fixture
def ws(spin, fake_plex, monkeypatch):
    monkeypatch.setattr(spin.waker, "submit", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(os, "getxattr", lambda p, a: b"disk2")
    fake_plex.items.update({"201": EP1, "202": EP2})
    fake_plex.leaves = [EP1, EP2]
    spin.db.set("debounce_seconds", 0)
    spin.db.set("next_on_play", False)
    return spin.listener


def note(offset, state="playing", rk="201"):
    return {"sessionKey": "7", "ratingKey": rk, "key": f"/library/metadata/{rk}",
            "state": state, "viewOffset": offset}


def test_ws_url():
    assert ws_url("http://10.0.0.5:32400/") == "ws://10.0.0.5:32400/:/websockets/notifications"
    assert ws_url("https://x.plex.direct:32400") == "wss://x.plex.direct:32400/:/websockets/notifications"


def test_ws_session_start_then_next_at_90_percent(ws, spin):
    ws.handle_notification(note(0))
    ws.handle_notification(note(500_000))
    assert [e["trigger"] for e in spin.db.recent_events()] == ["ws"]
    ws.handle_notification(note(910_000))
    ws.handle_notification(note(950_000))  # only once per session
    evs = spin.db.recent_events()
    assert [e["trigger"] for e in evs] == ["ws-next", "ws"]
    assert evs[0]["local_path"].endswith("S01E02.mkv")


def test_ws_ignores_prerolls_and_clears_on_stop(ws, spin):
    ws.handle_notification({"sessionKey": "7", "ratingKey": "1", "key": "/clip/preroll", "state": "playing"})
    assert spin.db.recent_events() == []
    ws.handle_notification(note(0))
    ws.handle_notification(note(0, state="stopped"))
    ws.handle_notification(note(0))
    assert [e["trigger"] for e in spin.db.recent_events()] == ["ws", "ws"]


# --- UI ----------------------------------------------------------------------

def test_first_run_goes_to_password_setup(client):
    r = client.get("/", follow_redirects=True)
    assert b"Set the UI password" in r.data


def test_password_setup_then_login_required(app, client, spin):
    r = client.post("/setup/password", data={"password": "hunter22", "confirm": "hunter22"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/setup/plex")
    assert client.get("/setup/plex").status_code == 200

    anon = app.test_client()
    assert "/login" in anon.get("/dashboard").headers["Location"]
    assert anon.post("/login", data={"password": "wrong"}).status_code == 200
    r = anon.post("/login?next=/settings", data={"password": "hunter22"})
    assert r.headers["Location"] == "/settings"
    # A second visitor can't overwrite the password without logging in.
    assert "/login" in app.test_client().post("/setup/password", data={"password": "x" * 8, "confirm": "x" * 8}).headers["Location"]


def test_logged_in_pages_render(client, spin):
    client.post("/setup/password", data={"password": "hunter22", "confirm": "hunter22"})
    spin.db.log_event(trigger="play", title="Film (2020)", disk="disk1", status="cold", read_ms=7400.0)
    for path in ("/dashboard", "/partials/events", "/partials/overview", "/settings",
                 "/test", "/setup/paths", "/setup/webhook", "/setup/webhook/status"):
        assert client.get(path).status_code == 200, path
    assert b"7.40s" in client.get("/partials/events").data
    assert spin.db.stats_since(0) == {"total": 1, "cold": 1, "avg_cold_ms": 7400.0}


def test_settings_save(client, spin):
    client.post("/setup/password", data={"password": "hunter22", "confirm": "hunter22"})
    client.post("/settings", data={"media.play": "on", "debounce_seconds": "30", "read_blocks": "99",
                                   "block_size": "5000", "allowed_users": "a, b", "next_episode_lookahead": "2",
                                   "next_episode_enabled": "on"})
    s = spin.db.settings()
    assert s["events"] == ["media.play"] and s["debounce_seconds"] == 30
    assert s["read_blocks"] == 16 and s["block_size"] == 4608
    assert s["allowed_users"] == ["a", "b"] and s["next_episode_lookahead"] == 2
    assert s["next_on_play"] is False and s["websocket_enabled"] is False


def test_healthz(client):
    j = client.get("/healthz").get_json()
    assert j["ok"] and j["mnt_user_present"] and j["websocket"] == "disabled"
