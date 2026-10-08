import os

from app import waker
from app.waker import map_path, random_offsets, read_random_blocks, resolve_disk, suggest_maps

MAPS = [
    {"plex_prefix": "/data", "local_prefix": "/mnt/user/data"},
    {"plex_prefix": "/data/tv/", "local_prefix": "/mnt/user/tv"},
]


def test_map_path_longest_prefix_wins():
    assert map_path("/data/tv/Show/e1.mkv", MAPS) == "/mnt/user/tv/Show/e1.mkv"
    assert map_path("/data/movies/m.mkv", MAPS) == "/mnt/user/data/movies/m.mkv"


def test_map_path_respects_component_boundaries():
    assert map_path("/data/tv2/x.mkv", MAPS) == "/mnt/user/data/tv2/x.mkv"
    assert map_path("/other/x.mkv", MAPS) == "/other/x.mkv"


def test_suggest_maps_matches_trailing_components(mnt):
    out = suggest_maps(["/media/library/tv", "/nope/nothing", str(mnt / "user/movies")], str(mnt))
    assert out == [{"plex_prefix": "/media/library/tv", "local_prefix": str(mnt / "user/tv")}]


def _fake_location(name):
    def getxattr(path, attr):
        assert attr == "system.LOCATION"
        return name.encode() + b"\x00"
    return getxattr


def test_resolve_disk_uses_shfs_location_xattr(mnt, monkeypatch):
    monkeypatch.setattr(os, "getxattr", _fake_location("disk2"))
    disk, path = resolve_disk(str(mnt / "user/tv/Show/S01E01.mkv"), str(mnt))
    assert disk == "disk2"
    assert path == str(mnt / "disk2/tv/Show/S01E01.mkv")


def test_resolve_disk_without_xattr_reads_through_user_share(mnt, monkeypatch):
    def nope(path, attr):
        raise OSError(61, "No data available")
    monkeypatch.setattr(os, "getxattr", nope)
    local = str(mnt / "user/tv/Show/S01E01.mkv")
    assert resolve_disk(local, str(mnt)) == (None, local)


def test_resolve_disk_direct_paths(mnt):
    p = str(mnt / "disk1/movies/Film (2020)/Film.mkv")
    assert resolve_disk(p, str(mnt)) == ("disk1", p)
    assert resolve_disk("/elsewhere/file.mkv", str(mnt)) == (None, "/elsewhere/file.mkv")


def test_random_offsets_aligned_and_in_range():
    size, bs = 10 * 1024 * 1024 + 123, 4096
    offs = random_offsets(size, 5, bs)
    assert len(offs) == 5
    assert all(o % bs == 0 and 0 <= o <= size - bs for o in offs)
    assert random_offsets(100, 3, bs) == [0]


def test_read_random_blocks_reads_real_file(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(os.urandom(256 * 1024))
    ms, mode = read_random_blocks(str(f), 3, 4096)
    assert ms >= 0 and mode in ("direct", "buffered")


def test_read_falls_back_when_o_direct_unsupported(tmp_path, monkeypatch):
    f = tmp_path / "f.bin"
    f.write_bytes(os.urandom(64 * 1024))
    real = waker._read_blocks

    def no_direct(path, offsets, bs, direct):
        if direct:
            raise OSError(22, "Invalid argument")
        return real(path, offsets, bs, direct)
    monkeypatch.setattr(waker, "_read_blocks", no_direct)
    assert read_random_blocks(str(f), 2, 4096)[1] == "buffered"
