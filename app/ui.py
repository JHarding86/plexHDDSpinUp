import functools
import os
import secrets
from datetime import datetime

from flask import (Blueprint, current_app, flash, make_response, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import plex as plexapi
from . import spinup, unraid
from .waker import map_path, resolve_disk, suggest_maps

bp = Blueprint("ui", __name__)

STEPS = [("password", "Password"), ("plex", "Connect Plex"), ("libraries", "Libraries"),
         ("paths", "Path mapping"), ("webhook", "Webhook")]


# --- helpers ----------------------------------------------------------------

def db():
    return spinup().db


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not db().get("ui_password_hash"):
            return redirect(url_for("ui.setup_step", step="password"))
        if not session.get("auth"):
            return redirect(url_for("ui.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def plex_client():
    return plexapi.client_from_settings(db().settings())


def webhook_url():
    return f"{request.host_url}webhook/{db().get('webhook_secret')}"


def _lines(value):
    return [v.strip() for v in value.replace("\n", ",").split(",") if v.strip()]


def _int(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.form.get(name, default))))
    except (TypeError, ValueError):
        return default


@bp.app_template_filter("ts")
def fmt_ts(ts):
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M:%S") if dt.date() == datetime.now().date() else dt.strftime("%b %d %H:%M")


@bp.app_context_processor
def inject():
    return {"steps": STEPS, "setup_complete": db().get("setup_complete"), "authed": session.get("auth")}


# --- auth -------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if check_password_hash(db().get("ui_password_hash") or "", request.form.get("password", "")):
            session["auth"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("ui.index"))
        flash("Wrong password.", "error")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("ui.login"))


@bp.get("/")
def index():
    if not db().get("setup_complete"):
        return redirect(url_for("ui.setup"))
    return redirect(url_for("ui.dashboard"))


# --- setup wizard -----------------------------------------------------------

@bp.get("/setup")
def setup():
    s = db().settings()
    if not s["ui_password_hash"]:
        step = "password"
    elif not s["plex_token"]:
        step = "plex"
    else:
        step = "libraries"
    return redirect(url_for("ui.setup_step", step=step))


@bp.route("/setup/password", methods=["GET", "POST"], endpoint="setup_password")
def setup_password():
    has_pw = bool(db().get("ui_password_hash"))
    if has_pw and not session.get("auth"):
        return redirect(url_for("ui.login", next=request.path))
    if request.method == "POST":
        pw, confirm = request.form.get("password", ""), request.form.get("confirm", "")
        if len(pw) < 6:
            flash("Use at least 6 characters.", "error")
        elif pw != confirm:
            flash("Passwords don't match.", "error")
        else:
            db().set("ui_password_hash", generate_password_hash(pw))
            session["auth"] = True
            session.permanent = True
            if db().get("setup_complete"):
                flash("Password changed.", "ok")
                return redirect(url_for("ui.settings"))
            return redirect(url_for("ui.setup_step", step="plex"))
    return render_template("setup_password.html", step="password", has_pw=has_pw)


@bp.get("/setup/<step>")
@login_required
def setup_step(step):
    if step == "password":
        return setup_password()
    s = db().settings()
    if step == "plex":
        return render_template("setup_plex.html", step=step, s=s)
    if step == "libraries":
        try:
            sections = [x for x in plex_client().sections() if x["type"] in ("movie", "show")]
            error = None
        except plexapi.PlexError as e:
            sections, error = [], str(e)
        selected = {str(i) for i in s["library_ids"]}
        return render_template("setup_libraries.html", step=step, sections=sections,
                               selected=selected, error=error)
    if step == "paths":
        return render_template("setup_paths.html", step=step, maps=db().path_maps(),
                               mnt_root=current_app.config["MNT_ROOT"])
    if step == "webhook":
        return render_template("setup_webhook.html", step=step, webhook_url=webhook_url(),
                               last=s.get("last_webhook"))
    return redirect(url_for("ui.setup"))


@bp.post("/plex/pin")
@login_required
def plex_pin():
    cid = db().get("plex_client_id")
    try:
        pin = plexapi.create_pin(cid)
    except Exception as e:  # noqa: BLE001 - surface any plex.tv failure in the UI
        return render_template("partials/plex_error.html", error=f"Could not reach plex.tv: {e}")
    return render_template("partials/plex_pin.html", pin=pin, auth_url=plexapi.auth_url(cid, pin["code"]))


@bp.get("/plex/pin/<int:pin_id>")
@login_required
def plex_pin_check(pin_id):
    cid = db().get("plex_client_id")
    try:
        token = plexapi.check_pin(cid, pin_id)
    except Exception as e:  # noqa: BLE001
        return _replace_auth_box(render_template("partials/plex_error.html", error=str(e)))
    if not token:
        return render_template("partials/plex_pin_waiting.html", pin_id=pin_id)
    db().set("plex_account_token", token)
    return _replace_auth_box(plex_servers())


def _replace_auth_box(body):
    """Swap the whole sign-in box rather than just the polling element."""
    resp = make_response(body)
    resp.headers["HX-Retarget"] = "#plex-auth"
    resp.headers["HX-Reswap"] = "innerHTML"
    return resp


@bp.get("/plex/servers")
@login_required
def plex_servers():
    s = db().settings()
    try:
        servers = plexapi.list_servers(s["plex_client_id"], s["plex_account_token"])
    except Exception as e:  # noqa: BLE001
        return render_template("partials/plex_error.html", error=f"Could not list servers: {e}")
    return render_template("partials/plex_servers.html", servers=servers)


def _connect(url, token, name=None):
    try:
        ident = plexapi.PlexClient(url, token, db().get("plex_client_id")).identity()
    except plexapi.PlexError as e:
        flash(str(e), "error")
        return redirect(url_for("ui.setup_step", step="plex"))
    db().set("plex_url", url.rstrip("/"))
    db().set("plex_token", token)
    db().set("plex_server_name", name or ident.get("name") or "Plex")
    spinup().listener.restart()
    flash(f"Connected to {ident.get('name')} (Plex {ident.get('version')}).", "ok")
    return redirect(url_for("ui.setup_step", step="libraries"))


@bp.post("/plex/select")
@login_required
def plex_select():
    s = db().settings()
    name, url = request.form.get("server"), request.form.get("url", "")
    try:
        servers = plexapi.list_servers(s["plex_client_id"], s["plex_account_token"])
    except Exception as e:  # noqa: BLE001
        flash(f"Could not list servers: {e}", "error")
        return redirect(url_for("ui.setup_step", step="plex"))
    match = next((x for x in servers if x["name"] == name), None)
    if not match:
        flash("Server not found on your account.", "error")
        return redirect(url_for("ui.setup_step", step="plex"))
    return _connect(url, match["token"], name)


@bp.post("/plex/manual")
@login_required
def plex_manual():
    return _connect(request.form.get("url", "").strip(), request.form.get("token", "").strip())


@bp.post("/setup/libraries")
@login_required
def setup_libraries_save():
    chosen = request.form.getlist("library")
    total = request.form.get("total", type=int) or 0
    db().set("library_ids", [] if len(chosen) >= total else chosen)
    if not chosen:
        flash("No libraries selected; nothing will be woken.", "error")
    if not db().path_maps():
        try:
            locs = [loc for x in plex_client().sections() if not chosen or x["id"] in chosen
                    for loc in x["locations"]]
            for m in suggest_maps(locs, current_app.config["MNT_ROOT"]):
                db().add_path_map(m["plex_prefix"], m["local_prefix"])
        except plexapi.PlexError:
            pass
    return redirect(url_for("ui.setup_step", step="paths"))


@bp.post("/paths/add")
@login_required
def paths_add():
    plex_prefix = request.form.get("plex_prefix", "").strip()
    local_prefix = request.form.get("local_prefix", "").strip()
    if plex_prefix.startswith("/") and local_prefix.startswith("/"):
        db().add_path_map(plex_prefix, local_prefix)
    else:
        flash("Both paths must be absolute (start with /).", "error")
    return redirect(request.form.get("back") or url_for("ui.setup_step", step="paths"))


@bp.post("/paths/<int:map_id>/delete")
@login_required
def paths_delete(map_id):
    db().delete_path_map(map_id)
    return redirect(request.form.get("back") or url_for("ui.setup_step", step="paths"))


@bp.post("/paths/suggest")
@login_required
def paths_suggest():
    s = db().settings()
    try:
        locs = [loc for x in plex_client().sections()
                if not s["library_ids"] or x["id"] in {str(i) for i in s["library_ids"]}
                for loc in x["locations"]]
    except plexapi.PlexError as e:
        flash(str(e), "error")
        return redirect(url_for("ui.setup_step", step="paths"))
    existing = {(m["plex_prefix"], m["local_prefix"]) for m in db().path_maps()}
    added = 0
    for m in suggest_maps(locs, current_app.config["MNT_ROOT"]):
        if (m["plex_prefix"], m["local_prefix"]) not in existing:
            db().add_path_map(m["plex_prefix"], m["local_prefix"])
            added += 1
    flash(f"Added {added} suggested mapping(s)." if added else "No new mappings to suggest.", "ok")
    return redirect(url_for("ui.setup_step", step="paths"))


@bp.get("/paths/check")
@login_required
def paths_check():
    """For each monitored library, map one real file and confirm the container sees it."""
    s = db().settings()
    rows, error = [], None
    try:
        client = plex_client()
        wanted = {str(i) for i in s["library_ids"]}
        for sec in client.sections():
            if sec["type"] not in ("movie", "show") or (wanted and sec["id"] not in wanted):
                continue
            item = client.sample_item(sec["id"], sec["type"])
            files = plexapi.files_of(item) if item else []
            if not files:
                rows.append({"library": sec["title"], "ok": None, "plex_path": "(library is empty)"})
                continue
            local = map_path(files[0], db().path_maps())
            ok = os.path.isfile(local)
            disk = resolve_disk(local, current_app.config["MNT_ROOT"])[0] if ok else None
            rows.append({"library": sec["title"], "ok": ok, "plex_path": files[0], "local": local, "disk": disk})
    except plexapi.PlexError as e:
        error = str(e)
    return render_template("partials/paths_check.html", rows=rows, error=error)


@bp.get("/setup/webhook/status")
@login_required
def webhook_status():
    return render_template("partials/webhook_status.html", last=db().get("last_webhook"))


@bp.post("/setup/finish")
@login_required
def setup_finish():
    db().set("setup_complete", True)
    return redirect(url_for("ui.dashboard"))


# --- dashboard --------------------------------------------------------------

@bp.get("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", s=db().settings())


@bp.get("/partials/events")
@login_required
def partial_events():
    return render_template("partials/events.html", events=db().recent_events(100))


@bp.get("/partials/overview")
@login_required
def partial_overview():
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    stats = db().stats_since(midnight)
    disks = unraid.read_disks(current_app.config["DISKS_INI"])
    return render_template("partials/overview.html", stats=stats, disks=disks,
                           ws_status=spinup().listener.status, s=db().settings())


# --- test wake --------------------------------------------------------------

@bp.get("/test")
@login_required
def test_wake():
    return render_template("test_wake.html")


@bp.get("/test/search")
@login_required
def test_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return ""
    try:
        items = plex_client().search(q)
    except plexapi.PlexError as e:
        return render_template("partials/plex_error.html", error=str(e))
    return render_template("partials/search_results.html", items=items, describe=plexapi.describe)


@bp.post("/test/<rating_key>")
@login_required
def test_wake_run(rating_key):
    rows = spinup().waker.wake_rating_key(rating_key, "test", {"user": "test"}, force=True)
    return render_template("partials/wake_result.html", rows=rows)


@bp.post("/test/<rating_key>/next")
@login_required
def test_wake_next(rating_key):
    rows = spinup().waker.wake_next(rating_key, "test-next", {"user": "test"})
    return render_template("partials/wake_result.html", rows=rows)


# --- settings ---------------------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    d = db()
    if request.method == "POST":
        d.set("events", [e for e in ("media.play", "media.resume") if request.form.get(e)])
        d.set("allowed_users", _lines(request.form.get("allowed_users", "")))
        d.set("allowed_players", _lines(request.form.get("allowed_players", "")))
        d.set("debounce_seconds", _int("debounce_seconds", 120, 0, 3600))
        d.set("read_blocks", _int("read_blocks", 3, 1, 16))
        d.set("block_size", _int("block_size", 4096, 512, 1048576) // 512 * 512)
        d.set("cold_threshold_ms", _int("cold_threshold_ms", 1000, 50, 60000))
        d.set("next_episode_enabled", bool(request.form.get("next_episode_enabled")))
        d.set("next_on_play", bool(request.form.get("next_on_play")))
        d.set("next_episode_lookahead", _int("next_episode_lookahead", 1, 1, 5))
        ws_before = d.get("websocket_enabled")
        d.set("websocket_enabled", bool(request.form.get("websocket_enabled")))
        if ws_before != d.get("websocket_enabled"):
            spinup().listener.restart()
        flash("Settings saved.", "ok")
        return redirect(url_for("ui.settings"))
    return render_template("settings.html", s=d.settings(), maps=d.path_maps(), webhook_url=webhook_url())


@bp.post("/settings/regenerate-secret")
@login_required
def regenerate_secret():
    db().set("webhook_secret", secrets.token_urlsafe(18))
    flash("New webhook URL generated. Update it in Plex → Settings → Webhooks.", "ok")
    return redirect(url_for("ui.settings"))


@bp.post("/settings/reset-plex")
@login_required
def reset_plex():
    for k in ("plex_url", "plex_token", "plex_server_name", "plex_account_token"):
        db().set(k, "")
    spinup().listener.restart()
    return redirect(url_for("ui.setup_step", step="plex"))
