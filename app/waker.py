"""Turn Plex play events into a small read on the one disk holding the file."""
import errno
import logging
import mmap
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import plex as plexapi

log = logging.getLogger(__name__)

# Entries under /mnt that are not a single physical disk or pool.
NON_DISK_MOUNTS = {"user", "user0", "disks", "remotes", "addons", "rootshare"}
PLAYABLE_TYPES = ("movie", "episode")


# --- path handling ----------------------------------------------------------

def _norm(p):
    return p.rstrip("/") or "/"


def map_path(plex_path, path_maps):
    """Rewrite a Plex-side path using the longest matching prefix map."""
    best = None
    for m in path_maps:
        prefix = _norm(m["plex_prefix"])
        if plex_path == prefix or plex_path.startswith(prefix + "/"):
            if best is None or len(prefix) > len(_norm(best["plex_prefix"])):
                best = m
    if best is None:
        return plex_path
    rest = plex_path[len(_norm(best["plex_prefix"])):]
    return _norm(best["local_prefix"]) + rest


def suggest_maps(locations, mnt_root="/mnt"):
    """Guess Plex-path → /mnt/user maps by matching the longest trailing path.

    e.g. Plex's /data/media/movies → /mnt/user/media/movies if that share exists.
    """
    out = []
    user_root = os.path.join(_norm(mnt_root), "user")
    for loc in locations:
        parts = [p for p in loc.split("/") if p]
        for k in range(len(parts), 0, -1):
            cand = os.path.join(user_root, *parts[-k:])
            if os.path.isdir(cand):
                if _norm(loc) != cand:
                    out.append({"plex_prefix": _norm(loc), "local_prefix": cand})
                break
    return out


def resolve_disk(local_path, mnt_root="/mnt"):
    """Return (disk_name, path_to_read).

    For /mnt/user paths, Unraid's shfs reports the owning disk through the
    `system.LOCATION` xattr, so we can read /mnt/diskN directly (bypassing the
    FUSE layer) without probing, and possibly waking, any other disk.
    """
    root = _norm(mnt_root)
    if not local_path.startswith(root + "/"):
        return None, local_path
    head, _, rest = local_path[len(root) + 1:].partition("/")
    if head not in ("user", "user0"):
        return (head if head not in NON_DISK_MOUNTS else None), local_path
    try:
        loc = os.getxattr(local_path, "system.LOCATION").decode(errors="replace").strip("\x00 \n")
    except OSError:
        loc = ""
    name = loc.split(",")[0].strip() if loc else ""
    if name:
        direct = os.path.join(root, name, rest)
        if os.path.isfile(direct):
            return name, direct
        return name, local_path
    return None, local_path


# --- the read itself --------------------------------------------------------

def random_offsets(size, count, block_size):
    if size <= block_size:
        return [0]
    hi = (size - block_size) // block_size
    return sorted(random.randint(0, hi) * block_size for _ in range(max(1, count)))


def _read_blocks(path, offsets, block_size, direct):
    flags = os.O_RDONLY | (getattr(os, "O_DIRECT", 0) if direct else 0)
    fd = os.open(path, flags)
    try:
        buf = mmap.mmap(-1, block_size)  # page-aligned, as O_DIRECT requires
        try:
            for off in offsets:
                if not direct:
                    try:
                        os.posix_fadvise(fd, off, block_size, os.POSIX_FADV_DONTNEED)
                    except (OSError, AttributeError):
                        pass
                os.preadv(fd, [buf], off)
        finally:
            buf.close()
    finally:
        os.close(fd)


def read_random_blocks(path, count=3, block_size=4096):
    """Read `count` blocks at random offsets, bypassing caches where possible.

    Random offsets in a multi-GB file almost never hit Unraid's page cache, and
    O_DIRECT skips it outright, so the read has to touch the platter.
    Returns (elapsed_ms, mode).
    """
    size = os.stat(path).st_size
    offsets = random_offsets(size, count, block_size)
    start = time.monotonic()
    try:
        _read_blocks(path, offsets, block_size, direct=True)
        mode = "direct"
    except OSError as e:
        if e.errno not in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP):
            raise
        _read_blocks(path, offsets, block_size, direct=False)
        mode = "buffered"
    return (time.monotonic() - start) * 1000.0, mode


# --- orchestration ----------------------------------------------------------

class Waker:
    def __init__(self, db, mnt_root="/mnt", plex_factory=plexapi.client_from_settings, max_workers=4):
        self.db = db
        self.mnt_root = mnt_root
        self.plex_factory = plex_factory
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wake")
        self._claims = {}
        self._lock = threading.Lock()

    def submit(self, fn, *args, **kwargs):
        fut = self.executor.submit(fn, *args, **kwargs)
        fut.add_done_callback(self._report_failure)
        return fut

    @staticmethod
    def _report_failure(fut):
        exc = fut.exception()
        if exc:
            log.error("wake task failed", exc_info=exc)

    def plex(self):
        return self.plex_factory(self.db.settings())

    # --- debounce ---------------------------------------------------------
    def _claim(self, key, window):
        """Reserve a wake for `key` unless one happened within `window` seconds."""
        now = time.monotonic()
        with self._lock:
            last = self._claims.get(key)
            if last is not None and now - last < window:
                return False
            self._claims[key] = now
            return True

    def _release(self, key):
        with self._lock:
            self._claims.pop(key, None)

    # --- entry points -----------------------------------------------------
    def handle_webhook(self, payload):
        """Filter a Plex webhook and schedule wakes. Returns a short description."""
        s = self.db.settings()
        event = payload.get("event", "")
        md = payload.get("Metadata") or {}
        kind = md.get("type")
        if kind not in PLAYABLE_TYPES:
            return f"ignored {event} for type {kind!r}"
        if s["library_ids"] and str(md.get("librarySectionID")) not in {str(i) for i in s["library_ids"]}:
            return "ignored: library not monitored"
        user = (payload.get("Account") or {}).get("title")
        player = (payload.get("Player") or {}).get("title")
        if s["allowed_users"] and user not in s["allowed_users"]:
            return f"ignored: user {user!r} not allowed"
        if s["allowed_players"] and player not in s["allowed_players"]:
            return f"ignored: player {player!r} not allowed"

        rating_key = md.get("ratingKey")
        if not rating_key:
            return "ignored: no ratingKey"
        ctx = {"user": user, "player": player, "title": plexapi.describe(md)}
        is_episode = kind == "episode"
        scheduled = []
        if event in s["events"]:
            self.submit(self.wake_rating_key, rating_key, "play", ctx)
            scheduled.append("current")
            if is_episode and s["next_episode_enabled"] and s["next_on_play"]:
                self.submit(self.wake_next, rating_key, "next-on-play", ctx)
                scheduled.append("next")
        if event == "media.scrobble" and is_episode and s["next_episode_enabled"]:
            self.submit(self.wake_next, rating_key, "scrobble-next", ctx)
            scheduled.append("next")
        return f"{event}: woke {', '.join(scheduled)}" if scheduled else f"ignored event {event}"

    def wake_rating_key(self, rating_key, trigger, ctx, force=False):
        try:
            item = self.plex().metadata(rating_key)
        except plexapi.PlexError as e:
            return [self._log(trigger, ctx, status="error", message=str(e))]
        return self.wake_item(item, trigger, ctx, force=force)

    def wake_next(self, rating_key_or_item, trigger, ctx):
        s = self.db.settings()
        try:
            client = self.plex()
            item = (rating_key_or_item if isinstance(rating_key_or_item, dict)
                    else client.metadata(rating_key_or_item))
            nexts = client.next_episodes(item, s["next_episode_lookahead"])
        except plexapi.PlexError as e:
            return [self._log(trigger, ctx, status="error", message=str(e))]
        if not nexts:
            return [self._log(trigger, ctx, status="skipped", message="no next episode")]
        results = []
        for nx in nexts:
            results += self.wake_item(nx, trigger, dict(ctx, title=plexapi.describe(nx)))
        return results

    def wake_item(self, item, trigger, ctx, force=False):
        files = plexapi.files_of(item)
        ctx = dict(ctx, title=ctx.get("title") or plexapi.describe(item))
        if not files:
            return [self._log(trigger, ctx, status="error", message="Plex returned no file path")]
        return [self.wake_file(f, trigger, ctx, force=force) for f in files]

    def wake_file(self, plex_path, trigger, ctx, force=False):
        s = self.db.settings()
        local = map_path(plex_path, self.db.path_maps())
        base = dict(ctx, plex_path=plex_path, local_path=local)
        if not os.path.isfile(local):
            return self._log(trigger, base, status="notfound",
                             message="File not visible in container. Check path mappings.")
        disk, read_path = resolve_disk(local, self.mnt_root)
        base["disk"] = disk
        key = disk or local
        if not force and not self._claim(key, s["debounce_seconds"]):
            return self._log(trigger, base, status="debounced",
                             message=f"{disk or 'file'} woken < {s['debounce_seconds']}s ago")
        try:
            ms, mode = read_random_blocks(read_path, s["read_blocks"], s["block_size"])
        except OSError as e:
            self._release(key)
            return self._log(trigger, base, status="error", message=f"read failed: {e}")
        if force:
            self._claim(key, 0)
        status = "cold" if ms >= s["cold_threshold_ms"] else "warm"
        msg = f"{mode} read of {s['read_blocks']}×{s['block_size']}B"
        return self._log(trigger, base, status=status, read_ms=round(ms, 1), message=msg)

    def _log(self, trigger, fields, **extra):
        row = {
            "trigger": trigger,
            "user": fields.get("user"),
            "player": fields.get("player"),
            "title": fields.get("title"),
            "plex_path": fields.get("plex_path"),
            "local_path": fields.get("local_path"),
            "disk": fields.get("disk"),
            "status": extra.get("status"),
            "read_ms": extra.get("read_ms"),
            "message": extra.get("message"),
        }
        self.db.log_event(**row)
        log.info("[%s] %s %s %s %s", trigger, row["status"], row["disk"] or "-", row["title"], row["message"] or "")
        return row
