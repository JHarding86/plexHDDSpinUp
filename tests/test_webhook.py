import json
import os

import pytest

from .conftest import episode

MOVIE = {
    "ratingKey": "500", "type": "movie", "title": "Film", "year": 2020, "librarySectionID": 1,
    "Media": [{"Part": [{"file": "/data/movies/Film (2020)/Film.mkv"}]}],
}
EP1, EP2 = episode(201, 1, 1), episode(202, 1, 2)


@pytest.fixture(autouse=True)
def sync_and_xattr(spin, fake_plex, monkeypatch):
    """Run wakes inline, give Plex some library items and fake Unraid's location xattr."""
    monkeypatch.setattr(spin.waker, "submit", lambda fn, *a, **kw: fn(*a, **kw))
    fake_plex.items.update({"500": MOVIE, "201": EP1, "202": EP2})
    fake_plex.leaves = [EP1, EP2]

    def getxattr(path, attr):
        return b"disk1" if "/movies/" in path else b"disk2"
    monkeypatch.setattr(os, "getxattr", getxattr)


def post(client, spin, payload, secret=None):
    secret = secret or spin.db.get("webhook_secret")
    return client.post(f"/webhook/{secret}", data={"payload": json.dumps(payload)})


def play(md, event="media.play", user="james", player="Living Room"):
    return {"event": event, "Account": {"title": user}, "Player": {"title": player},
            "Metadata": {k: v for k, v in md.items() if k != "Media"}}


def test_wrong_secret_is_404(client, spin):
    assert post(client, spin, play(MOVIE), secret="nope").status_code == 404
    assert spin.db.recent_events() == []


def test_movie_play_wakes_its_disk(client, spin, mnt):
    r = post(client, spin, play(MOVIE))
    assert r.status_code == 200
    [ev] = spin.db.recent_events()
    assert ev["trigger"] == "play" and ev["disk"] == "disk1"
    assert ev["status"] in ("cold", "warm")
    assert ev["local_path"] == str(mnt / "user/movies/Film (2020)/Film.mkv")
    assert ev["user"] == "james" and ev["title"] == "Film (2020)"
    assert spin.db.get("last_webhook")["event"] == "media.play"


def test_second_play_on_same_disk_is_debounced(client, spin):
    post(client, spin, play(MOVIE))
    post(client, spin, play(MOVIE))
    assert [e["status"] for e in spin.db.recent_events()][0] == "debounced"


def test_episode_play_also_warms_next_episode(client, spin):
    spin.db.set("debounce_seconds", 0)
    post(client, spin, play(EP1))
    evs = {e["trigger"]: e for e in spin.db.recent_events()}
    assert evs["play"]["title"].startswith("Show S01E01")
    assert evs["next-on-play"]["title"].startswith("Show S01E02")


def test_scrobble_wakes_next_episode_only(client, spin):
    post(client, spin, play(EP1, event="media.scrobble"))
    [ev] = spin.db.recent_events()
    assert ev["trigger"] == "scrobble-next"
    assert ev["local_path"].endswith("S01E02.mkv") and ev["disk"] == "disk2"


def test_scrobble_on_last_episode_is_skipped(client, spin):
    post(client, spin, play(EP2, event="media.scrobble"))
    [ev] = spin.db.recent_events()
    assert ev["status"] == "skipped"


def test_next_episode_can_be_disabled(client, spin):
    spin.db.set("next_episode_enabled", False)
    post(client, spin, play(EP1, event="media.scrobble"))
    assert spin.db.recent_events() == []


@pytest.mark.parametrize("change,payload", [
    (("allowed_users", ["someone"]), play(MOVIE)),
    (("allowed_players", ["Bedroom"]), play(MOVIE)),
    (("library_ids", ["2"]), play(MOVIE)),
    (None, play(MOVIE, event="media.pause")),
    (None, {"event": "media.play", "Metadata": {"type": "track", "ratingKey": "9"}}),
])
def test_filters(client, spin, change, payload):
    if change:
        spin.db.set(*change)
    post(client, spin, payload)
    assert spin.db.recent_events() == []


def test_missing_mapping_reports_notfound(client, spin):
    for m in spin.db.path_maps():
        spin.db.delete_path_map(m["id"])
    post(client, spin, play(MOVIE))
    [ev] = spin.db.recent_events()
    assert ev["status"] == "notfound"


def test_plex_lookup_failure_logged(client, spin, fake_plex):
    del fake_plex.items["500"]
    post(client, spin, play(MOVIE))
    [ev] = spin.db.recent_events()
    assert ev["status"] == "error" and "500" in ev["message"]


def test_bad_payload(client, spin):
    secret = spin.db.get("webhook_secret")
    assert client.post(f"/webhook/{secret}", data={"payload": "{not json"}).status_code == 400
