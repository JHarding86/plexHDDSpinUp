import os

import pytest

from app import create_app
from app.plex import PlexError


def episode(rk, season, index, show="10", path=None, title=None):
    return {
        "ratingKey": str(rk), "type": "episode", "grandparentRatingKey": show,
        "grandparentTitle": "Show", "parentIndex": season, "index": index,
        "title": title or f"Ep {season}x{index}", "librarySectionID": 2, "duration": 1_000_000,
        "Media": [{"Part": [{"file": path or f"/data/tv/Show/S{season:02d}E{index:02d}.mkv"}]}],
    }


class FakePlex:
    """Stands in for PlexClient; `items` maps ratingKey -> metadata."""

    def __init__(self, items=None, leaves=None):
        self.items = items or {}
        self.leaves = leaves or []

    def metadata(self, rk):
        if str(rk) not in self.items:
            raise PlexError(f"No metadata for ratingKey {rk}")
        return self.items[str(rk)]

    def next_episodes(self, ep, count=1):
        from app.plex import pick_next
        return pick_next(self.leaves, ep, count)


@pytest.fixture
def mnt(tmp_path):
    """Fake Unraid /mnt: a file on disk2, visible via /mnt/user too."""
    root = tmp_path / "mnt"
    for d in ("user/tv/Show", "disk1/tv/Show", "disk2/tv/Show", "user/movies/Film (2020)", "disk1/movies/Film (2020)"):
        (root / d).mkdir(parents=True)
    for rel in ("tv/Show/S01E01.mkv", "tv/Show/S01E02.mkv"):
        data = os.urandom(64 * 1024)
        (root / "user" / rel).write_bytes(data)
        (root / "disk2" / rel).write_bytes(data)
    data = os.urandom(64 * 1024)
    (root / "user/movies/Film (2020)/Film.mkv").write_bytes(data)
    (root / "disk1/movies/Film (2020)/Film.mkv").write_bytes(data)
    return root


@pytest.fixture
def fake_plex():
    return FakePlex()


@pytest.fixture
def app(tmp_path, mnt, fake_plex):
    app = create_app({
        "CONFIG_DIR": str(tmp_path / "config"),
        "MNT_ROOT": str(mnt),
        "DISKS_INI": str(tmp_path / "emhttp" / "disks.ini"),
        "START_BACKGROUND": False,
        "PLEX_FACTORY": lambda settings: fake_plex,
    })
    app.config["TESTING"] = True
    st = app.extensions["spinup"]
    st.db.add_path_map("/data/tv", str(mnt / "user/tv"))
    st.db.add_path_map("/data/movies", str(mnt / "user/movies"))
    return app


@pytest.fixture
def spin(app):
    return app.extensions["spinup"]


@pytest.fixture
def client(app):
    return app.test_client()
