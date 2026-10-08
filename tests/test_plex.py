from app.plex import describe, files_of, pick_next

from .conftest import episode

LEAVES = [episode(100 + s * 10 + e, s, e) for s, e in
          [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (0, 1), (0, 2)]]


def keys(eps):
    return [(e["parentIndex"], e["index"]) for e in eps]


def test_next_within_season():
    assert keys(pick_next(LEAVES, episode(111, 1, 1))) == [(1, 2)]


def test_next_rolls_over_to_next_season():
    assert keys(pick_next(LEAVES, episode(113, 1, 3))) == [(2, 1)]


def test_lookahead_and_unsorted_input():
    assert keys(pick_next(list(reversed(LEAVES)), episode(112, 1, 2), 3)) == [(1, 3), (2, 1), (2, 2)]


def test_last_episode_has_no_next():
    assert pick_next(LEAVES, episode(122, 2, 2)) == []


def test_specials_skipped_unless_watching_one():
    assert (0, 1) not in keys(pick_next(LEAVES, episode(111, 1, 1), 10))
    assert keys(pick_next(LEAVES, episode(101, 0, 1))) == [(0, 2)]


def test_current_missing_from_leaves_uses_position():
    assert keys(pick_next(LEAVES, episode(999, 1, 2))) == [(1, 3)]


def test_files_and_describe():
    item = {"type": "movie", "title": "Film", "year": 2020,
            "Media": [{"Part": [{"file": "/a.mkv"}, {"file": "/b.mkv"}]}, {"Part": [{"file": "/a.mkv"}]}]}
    assert files_of(item) == ["/a.mkv", "/b.mkv"]
    assert describe(item) == "Film (2020)"
    assert describe(episode(1, 2, 3, title="Pilot")) == "Show S02E03 · Pilot"
