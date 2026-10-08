# Plan: Spinup — Plex disk-waker for Unraid, with web UI

## Context
Plex runs in a Proxmox VM and media lives on an Unraid array split across many drives. When a movie starts, the preroll plays while the movie's drive is still asleep, then playback stalls during spin-up. Goal: a small Dockerized Flask app **on the Unraid box** (LAN-only) that receives Plex webhooks, resolves the exact file, and wakes **only the drive holding it** during the preroll. It also pre-warms the **next TV episode's** drive. A web UI handles setup and shows activity. `/home/james/projects/plexHDDSpinUp` is currently empty and not a git repo.

Decisions made: run on Unraid · LAN-only (simple UI password, no TLS) · private GitHub repo `JHarding86/plexHDDSpinUp` · image built by GitHub Actions → `ghcr.io/jharding86/plexhddspinup` · include all nice-to-haves, next-episode first.

## Step 0 — Git + remote (first thing on approval)
1. `git init -b main` in the project dir. Add `README.md` (short description), `.gitignore` (Python, `config/`, `.venv`) and `docs/PLAN.md` (a copy of this plan).
2. Initial commit (with the Co-Authored-By trailer), then `gh repo create JHarding86/plexHDDSpinUp --private --source . --push`.
3. Create a worktree/branch `feat/spinup-v1` for the implementation and open a draft PR at the end. Main only receives the scaffold commit the user explicitly asked for.

## Architecture
```
Plex ──POST /webhook/<secret>──▶ spinup (Unraid, host port 9876)
  media.play/resume  → resolve ratingKey → wake current file
  media.scrobble(ep) → find next episode  → wake its file
Plex websocket (optional 2nd trigger) ──▶ same pipeline, debounced
```
Wake pipeline: Plex `/library/metadata/<key>` → `Part@file` → prefix path map (Plex VM path → `/mnt/user/...`) → **resolve physical disk** via shfs's `system.LOCATION` xattr on the `/mnt/user` path (no probing of other disks, which could wake them) → read 2–3 × 4 KiB blocks at **random offsets with `O_DIRECT`** on the `/mnt/diskN` path. This bypasses Unraid's page cache, so the read is guaranteed to hit the platter. It falls back to buffered random-offset reads via `/mnt/user` if `O_DIRECT` fails. Each read is timed: over ~1s means the disk was asleep.

Debounce: skip any disk woken within the last N seconds (default 120). The webhook returns 200 immediately and the work runs on a `ThreadPoolExecutor`.

## Next-episode warming (priority nice-to-have)
- On `media.play` of an episode: also wake the next episode right away **if** it's on a different disk (cheap, covers binge-starts).
- On `media.scrobble` of an episode (~90% watched): resolve the next episode and wake its disk. This is the key one, because Unraid's spin-down timer may have parked the disk since play started, and TV autoplay has no preroll buffer.
- Next-episode lookup: from the episode's metadata take `grandparentRatingKey`, `parentIndex`, `index` → `GET /library/metadata/<show>/allLeaves` → the first item ordered after (season, episode), skipping specials (season 0) unless the current one is a special.
- Setting: lookahead count (default 1; allow 2 for heavy bingers).

## Other nice-to-haves
- **Disk status panel:** mount Unraid's `/var/local/emhttp` directory read-only (a single-file bind goes stale when Unraid rewrites `disks.ini`) and parse each disk's `spundown`, `name` and `device` to show live spin state on the dashboard, plus which disk each event hit.
- **Plex websocket trigger** (`ws://<plex>:32400/:/websockets/notifications`, `websocket-client`): `PlaySessionStateNotification` state=playing → same pipeline. It's often faster than webhooks and works without Plex Pass. Toggle in settings. It runs as one background thread started once in the app factory (safe because gunicorn runs a single worker).
- **Unraid template:** `unraid/spinup.xml` for "Add Container" (port, `/config`, `/mnt` ro-slave, disks.ini, PUID 99/PGID 100), plus a README section on installing it.

## UI (Flask + Jinja + HTMX + Pico.css, no JS build)
- **Setup wizard:** (1) Sign in with Plex (plex.tv PIN flow → choose server from `/api/v2/resources`; manual URL+token fallback; Test connection) → (2) choose libraries (`/library/sections`) → (3) path mapping, auto-suggested from library `Location` paths vs `/mnt/user`, live-validated ("found 1,284 files") → (4) webhook URL + copy button + Plex Settings→Webhooks steps + "waiting for first event…" indicator.
- **Dashboard:** live event feed (time, user, player, title, trigger [play/scrobble-next/ws/test], disk, read ms, cold/warm), today's stats, and the disk status panel. Polled by HTMX every 3s.
- **Test wake:** search the Plex library → wake that title's disk → show result.
- **Settings:** events, user/player allow-lists, debounce, read count/size, next-episode on/off + lookahead, websocket on/off, UI password, regenerate webhook secret.
- **Auth:** single password (hashed in SQLite) set in the wizard. The webhook is protected by a random secret in its path.

## Files (new repo `spinup/` layout at repo root)
- `app/__init__.py` — app factory, DB init, starts executor + optional websocket thread
- `app/db.py` — SQLite (`/config/spinup.db`): `settings` k/v, `path_maps`, `events` (30-day trim)
- `app/plex.py` — PIN auth, resources, sections, metadata→file paths, next-episode lookup, search
- `app/waker.py` — path mapping, disk resolution, O_DIRECT random reads, timing, debounce
- `app/unraid.py` — disks.ini parser
- `app/ws_listener.py` — Plex websocket client with reconnect/backoff
- `app/webhook.py`, `app/ui.py` — blueprints; `app/templates/`, `app/static/`
- `tests/` — pytest: sample Plex payloads (play/scrobble/resume), path mapping, next-episode ordering (incl. season rollover, specials), disk resolution against a fake `/mnt/diskN` tree, disks.ini parsing, webhook secret rejection
- `Dockerfile` (python:3.12-slim, gunicorn `-w 1 --threads 8`, PUID/PGID entrypoint, `/healthz` HEALTHCHECK), `docker-compose.yml` (example), `requirements.txt`
- `.github/workflows/docker.yml` — test → build → push `ghcr.io/jharding86/plexhddspinup:latest` + sha tag on main
- `unraid/spinup.xml`, `README.md`

## Container on Unraid
Volumes: `/mnt/user/appdata/spinup:/config` · `/mnt:/mnt:ro,rslave` (rslave so disk mounts appear even if the array starts after the container) · `/var/local/emhttp:/unraid:ro`. Port 9876. Env: `PUID=99 PGID=100 TZ`. GHCR package is private by default. The README covers either making the package public (code isn't sensitive) or running a one-time `docker login ghcr.io` on Unraid with a read-only PAT.

## Verification
1. `pytest` passes locally and in Actions. `docker build` + `docker run` locally, with a fake `/mnt/disk1..3` tree and a sample disks.ini → wizard loads and `/healthz` is OK.
2. Post captured sample Plex payloads with `curl -F payload=@play.json` → event appears on the dashboard with the correct disk.
3. On Unraid: install from the template, run the wizard against the real Plex, use Test wake on a title whose disk is spun down → dashboard shows a cold read (~5–10s) and the disk panel shows it spinning.
4. Real run: start a movie with preroll. Event timestamps should confirm `media.play` fires at preroll start, and the movie starts with no stall. Watch a TV episode past 90% → "scrobble-next" event wakes the next episode's disk.
5. Push the branch, CI publishes the image, open a draft PR.
