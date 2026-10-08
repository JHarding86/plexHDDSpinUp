import hmac
import json
import logging
import os
import time

from flask import Blueprint, abort, current_app, jsonify, request

from . import spinup

log = logging.getLogger(__name__)
bp = Blueprint("webhook", __name__)


@bp.post("/webhook/<secret>")
def plex_webhook(secret):
    st = spinup()
    if not hmac.compare_digest(secret, st.db.get("webhook_secret") or ""):
        abort(404)
    # Plex sends multipart/form-data with the JSON in a "payload" field.
    raw = request.form.get("payload")
    try:
        data = json.loads(raw) if raw is not None else request.get_json(force=True, silent=False)
    except (ValueError, TypeError):
        return jsonify(error="unparseable payload"), 400
    if not isinstance(data, dict):
        return jsonify(error="unparseable payload"), 400
    st.db.set("last_webhook", {"ts": time.time(), "event": data.get("event")})
    result = st.waker.handle_webhook(data)
    log.info("webhook %s", result)
    return jsonify(result=result)


@bp.get("/healthz")
def healthz():
    st = spinup()
    mnt = current_app.config["MNT_ROOT"]
    return jsonify(
        ok=True,
        setup_complete=bool(st.db.get("setup_complete")),
        mnt_user_present=os.path.isdir(os.path.join(mnt, "user")),
        websocket=st.listener.status,
    )
