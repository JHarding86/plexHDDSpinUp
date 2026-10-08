"""Optional second trigger: Plex's notification websocket.

Fires as soon as a session starts playing and needs no Plex Pass. It also
watches progress so the next episode can be warmed at ~90% even without the
media.scrobble webhook.
"""
import json
import logging
import threading

import websocket

from . import plex as plexapi
from .waker import PLAYABLE_TYPES

log = logging.getLogger(__name__)
NEXT_EPISODE_AT = 0.9


def ws_url(plex_url):
    base = plex_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + "/:/websockets/notifications"


class PlexWebsocketListener(threading.Thread):
    def __init__(self, db, waker):
        super().__init__(name="plex-ws", daemon=True)
        self.db = db
        self.waker = waker
        self.status = "disabled"
        self._sessions = {}
        self._sessions_lock = threading.Lock()
        self._poke = threading.Event()
        self._ws = None

    def restart(self):
        """Apply changed settings: drop the current connection and re-read them."""
        ws = self._ws
        if ws is not None:
            ws.close()
        self._poke.set()

    def run(self):
        backoff = 2
        while True:
            s = self.db.settings()
            if not (s["websocket_enabled"] and s["plex_url"] and s["plex_token"]):
                self.status = "disabled"
                self._poke.wait(10)
                self._poke.clear()
                continue
            self.status = "connecting"
            self._ws = websocket.WebSocketApp(
                ws_url(s["plex_url"]),
                header=[f"X-Plex-Token: {s['plex_token']}"],
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=lambda _ws, err: log.warning("plex websocket error: %s", err),
            )
            self._ws.run_forever(ping_interval=30, ping_timeout=10)
            self._ws = None
            if self.status == "connected":
                backoff = 2
            self.status = "reconnecting"
            self._poke.wait(backoff)
            self._poke.clear()
            backoff = min(backoff * 2, 60)

    def _on_open(self, _ws):
        self.status = "connected"
        log.info("plex websocket connected")

    def _on_message(self, _ws, message):
        try:
            data = json.loads(message).get("NotificationContainer", {})
        except (ValueError, AttributeError):
            return
        if data.get("type") != "playing":
            return
        for n in data.get("PlaySessionStateNotification", []) or []:
            self.handle_notification(n)

    def handle_notification(self, n):
        skey, rk, state = n.get("sessionKey"), n.get("ratingKey"), n.get("state")
        if not skey:
            return
        if state == "stopped":
            with self._sessions_lock:
                self._sessions.pop(skey, None)
            return
        if not rk or not str(n.get("key", "")).startswith("/library/metadata/"):
            return  # prerolls/trailers that aren't library items
        with self._sessions_lock:
            sess = self._sessions.get(skey)
            if sess is None or sess["rk"] != rk:
                self._sessions[skey] = {"rk": rk, "item": None, "next_done": False}
                start = True
            else:
                start = False
                item = sess["item"]
                due = (item is not None and item.get("type") == "episode" and not sess["next_done"]
                       and item.get("duration") and int(n.get("viewOffset") or 0)
                       >= NEXT_EPISODE_AT * int(item["duration"]))
                if due:
                    sess["next_done"] = True
        if start:
            self.waker.submit(self._start_session, skey, rk)
        elif due and self.db.get("next_episode_enabled"):
            self.waker.submit(self.waker.wake_next, item, "ws-next",
                              {"title": plexapi.describe(item)})

    def _start_session(self, skey, rk):
        s = self.db.settings()
        try:
            item = self.waker.plex().metadata(rk)
        except plexapi.PlexError as e:
            log.warning("websocket session %s: %s", skey, e)
            return
        if item.get("type") not in PLAYABLE_TYPES:
            return
        if s["library_ids"] and str(item.get("librarySectionID")) not in {str(i) for i in s["library_ids"]}:
            return
        with self._sessions_lock:
            sess = self._sessions.get(skey)
            if sess is not None and sess["rk"] == rk:
                sess["item"] = item
        ctx = {"title": plexapi.describe(item)}
        self.waker.wake_item(item, "ws", ctx)
        if item.get("type") == "episode" and s["next_episode_enabled"] and s["next_on_play"]:
            self.waker.wake_next(item, "ws-next-on-play", ctx)
