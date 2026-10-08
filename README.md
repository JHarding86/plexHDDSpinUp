# Spinup

Wakes the Unraid disk holding the movie or episode Plex is about to play, using the preroll window to hide HDD spin-up latency. It also pre-warms the next TV episode's disk.

It runs as a Docker container on the Unraid box and comes with a web UI for setup and monitoring.

```
Plex (Proxmox) ──webhook──▶ Spinup (Unraid :9876)
  media.play / resume  → look up the file via the Plex API → read a few random blocks from /mnt/diskN
  media.scrobble (ep)  → find the next episode → wake its disk too
```

## How it wakes the right disk

1. A Plex webhook (or the optional Plex websocket) reports a `ratingKey`.
2. Spinup asks Plex for that item's file path and rewrites it with your path mappings (Plex's `/data/movies/...` becomes `/mnt/user/movies/...`).
3. It asks Unraid's user-share filesystem which disk holds the file (the `system.LOCATION` xattr), so no other disk is touched.
4. It reads 3 × 4 KiB at **random offsets** from `/mnt/diskN/...` with `O_DIRECT`. That bypasses Unraid's RAM cache, so the read always has to go to the platter. If `O_DIRECT` isn't supported, it falls back to a buffered random-offset read.
5. It records how long the read took. Anything over 1 s means the disk was asleep and the preroll absorbed the spin-up.

**Next episode:** Plex sends `media.scrobble` when an episode is ~90% watched. Spinup then finds the next episode (rolling into the next season and skipping specials) and wakes its disk before autoplay starts. TV autoplay has no preroll, so this is where the warm-up matters most. It can also wake the next episode as soon as one starts.

## Install on Unraid

### 1. Let Unraid pull the image

The repo is private, so the GHCR image is private too. Pick one:

- **Make the package public.** The code isn't sensitive. Go to GitHub → your profile → Packages → `plexhddspinup` → Package settings → Change visibility → Public.
- **Or log Unraid in to GHCR once.** Create a classic PAT with only `read:packages`, then in the Unraid terminal run:
  ```sh
  docker login ghcr.io -u JHarding86
  ```

### 2. Add the container

Copy the template onto the flash drive, from the Unraid terminal:

```sh
curl -fsSL -H "Authorization: token <PAT with repo read>" \
  https://raw.githubusercontent.com/JHarding86/plexHDDSpinUp/main/unraid/spinup.xml \
  -o /boot/config/plugins/dockerMan/templates-user/my-spinup.xml
```

Alternatively, paste the file's contents there by hand. Then go to **Docker → Add Container → Template: spinup → Apply**.

The template maps:

| Container | Host | Why |
|---|---|---|
| `9876` | `9876` | Web UI + webhook |
| `/config` | `/mnt/user/appdata/spinup` | settings + activity log |
| `/mnt` | `/mnt` (**RO/Slave**) | read the exact `/mnt/diskN` holding a file |
| `/unraid` | `/var/local/emhttp` (RO) | dashboard disk spin state (optional) |

If you'd rather use Compose Manager, see [`docker-compose.yml`](docker-compose.yml).

### 3. Run the setup wizard

Open `http://<unraid-ip>:9876`:

1. Set a UI password.
2. **Sign in with Plex** and pick your server. Choose the plain `http://<lan-ip>:32400` address.
3. Choose which libraries to monitor.
4. Check the path mappings (suggested automatically). The check maps one real file per library and shows which disk it's on.
5. Copy the webhook URL into Plex → Settings → Webhooks (needs Plex Pass). Play something and the wizard confirms receipt.

Then use **Test wake** on a title whose disk is in standby. You should see a *cold* read of several seconds.

## Settings worth knowing

- **Plex websocket trigger:** fires the moment a session starts, needs no Plex Pass, and also handles next-episode warming at 90%. Safe to run together with webhooks, because wakes are debounced per disk.
- **Debounce (120 s):** a disk woken in the last N seconds isn't read again.
- **Filters:** limit wakes to particular Plex users or players.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
CONFIG_DIR=./config MNT_ROOT=/mnt flask --app 'app:create_app()' run -p 9876
```

CI (`.github/workflows/docker.yml`) runs the tests on every push and PR. Pushes to `main` publish `ghcr.io/jharding86/plexhddspinup:latest` and `:sha-<commit>`.
