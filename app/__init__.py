"""Spinup: wake the Unraid disk holding whatever Plex is about to play."""
import logging
import os
from types import SimpleNamespace

from flask import Flask, current_app

from .db import DB
from .waker import Waker
from .ws_listener import PlexWebsocketListener


def spinup():
    """Shared services for the running app (db, waker, websocket listener)."""
    return current_app.extensions["spinup"]


def create_app(overrides=None):
    cfg = {
        "CONFIG_DIR": os.environ.get("CONFIG_DIR", "/config"),
        "MNT_ROOT": os.environ.get("MNT_ROOT", "/mnt"),
        "DISKS_INI": os.environ.get("DISKS_INI", "/unraid/disks.ini"),
        "START_BACKGROUND": True,
    }
    cfg.update(overrides or {})

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    os.makedirs(cfg["CONFIG_DIR"], exist_ok=True)
    db = DB(os.path.join(cfg["CONFIG_DIR"], "spinup.db"))
    waker = Waker(db, mnt_root=cfg["MNT_ROOT"], **({"plex_factory": cfg["PLEX_FACTORY"]} if "PLEX_FACTORY" in cfg else {}))
    listener = PlexWebsocketListener(db, waker)
    if cfg["START_BACKGROUND"]:
        listener.start()

    app = Flask(__name__)
    app.config.update(cfg)
    app.secret_key = db.get("secret_key")
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)
    app.extensions["spinup"] = SimpleNamespace(db=db, waker=waker, listener=listener)

    from . import ui, webhook
    app.register_blueprint(webhook.bp)
    app.register_blueprint(ui.bp)
    return app
