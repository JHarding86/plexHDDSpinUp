"""Plex Media Server and plex.tv API helpers (JSON responses)."""
from urllib.parse import urlencode

import requests

PRODUCT = "Spinup"
PLEX_TV = "https://plex.tv"
TIMEOUT = 10


class PlexError(Exception):
    pass


def _headers(client_id, token=None):
    h = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Client-Identifier": client_id,
    }
    if token:
        h["X-Plex-Token"] = token
    return h


# --- plex.tv sign-in (PIN flow) ------------------------------------------

def create_pin(client_id):
    r = requests.post(f"{PLEX_TV}/api/v2/pins", params={"strong": "true"},
                      headers=_headers(client_id), timeout=TIMEOUT)
    r.raise_for_status()
    pin = r.json()
    return {"id": pin["id"], "code": pin["code"]}


def auth_url(client_id, code):
    params = {"clientID": client_id, "code": code, "context[device][product]": PRODUCT}
    return "https://app.plex.tv/auth#?" + urlencode(params)


def check_pin(client_id, pin_id):
    """Return the account token once the user has approved the PIN, else None."""
    r = requests.get(f"{PLEX_TV}/api/v2/pins/{pin_id}", headers=_headers(client_id), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("authToken") or None


def list_servers(client_id, account_token):
    """Servers on the account, with candidate connection URLs (LAN first)."""
    r = requests.get(f"{PLEX_TV}/api/v2/resources",
                     params={"includeHttps": 1, "includeRelay": 0},
                     headers=_headers(client_id, account_token), timeout=TIMEOUT)
    r.raise_for_status()
    servers = []
    for res in r.json():
        if "server" not in (res.get("provides") or ""):
            continue
        urls = []
        conns = sorted(res.get("connections", []), key=lambda c: not c.get("local"))
        for c in conns:
            if c.get("relay"):
                continue
            # Plain http://ip:port avoids plex.direct DNS-rebinding problems on LAN.
            if c.get("local") and c.get("address"):
                urls.append({"url": f"http://{c['address']}:{c.get('port', 32400)}", "local": True})
            urls.append({"url": c["uri"], "local": bool(c.get("local"))})
        seen, unique = set(), []
        for u in urls:
            if u["url"] not in seen:
                seen.add(u["url"])
                unique.append(u)
        servers.append({
            "name": res.get("name"),
            "token": res.get("accessToken") or account_token,
            "urls": unique,
        })
    return servers


# --- next-episode ordering (pure, unit-tested) -----------------------------

def _ep_key(item):
    return (int(item.get("parentIndex") or 0), int(item.get("index") or 0))


def pick_next(leaves, current, count=1):
    """Episodes after `current` in season/episode order.

    Specials (season 0) are skipped unless the current episode is itself a special.
    """
    cur_key = _ep_key(current)
    include_specials = cur_key[0] == 0
    eps = [e for e in leaves if include_specials or _ep_key(e)[0] != 0]
    eps.sort(key=_ep_key)
    keys = [str(e.get("ratingKey")) for e in eps]
    cur_rk = str(current.get("ratingKey"))
    if cur_rk in keys:
        after = eps[keys.index(cur_rk) + 1:]
    else:
        after = [e for e in eps if _ep_key(e) > cur_key]
    return after[:max(0, count)]


def files_of(item):
    """All distinct file paths on a metadata item (every version/part)."""
    out = []
    for media in item.get("Media", []) or []:
        for part in media.get("Part", []) or []:
            f = part.get("file")
            if f and f not in out:
                out.append(f)
    return out


def describe(item):
    if item.get("type") == "episode":
        return "{} S{:02d}E{:02d} · {}".format(
            item.get("grandparentTitle", "?"), int(item.get("parentIndex") or 0),
            int(item.get("index") or 0), item.get("title", "?"))
    year = item.get("year")
    return f"{item.get('title', '?')} ({year})" if year else item.get("title", "?")


# --- Plex Media Server ----------------------------------------------------

class PlexClient:
    def __init__(self, url, token, client_id="spinup"):
        if not url or not token:
            raise PlexError("Plex is not configured")
        self.url = url.rstrip("/")
        self.token = token
        self.client_id = client_id

    def _get(self, path, **params):
        try:
            r = requests.get(self.url + path, params=params,
                             headers=_headers(self.client_id, self.token), timeout=TIMEOUT)
        except requests.RequestException as e:
            raise PlexError(f"Cannot reach Plex at {self.url}: {e}") from e
        if r.status_code == 401:
            raise PlexError("Plex rejected the token (401)")
        if not r.ok:
            raise PlexError(f"Plex returned HTTP {r.status_code} for {path}")
        return r.json().get("MediaContainer", {})

    def identity(self):
        mc = self._get("/")
        return {"name": mc.get("friendlyName"), "version": mc.get("version")}

    def metadata(self, rating_key):
        items = self._get(f"/library/metadata/{rating_key}").get("Metadata") or []
        if not items:
            raise PlexError(f"No metadata for ratingKey {rating_key}")
        return items[0]

    def sections(self):
        out = []
        for d in self._get("/library/sections").get("Directory", []) or []:
            out.append({
                "id": str(d.get("key")),
                "title": d.get("title"),
                "type": d.get("type"),
                "locations": [loc.get("path") for loc in d.get("Location", []) or []],
            })
        return out

    def sample_item(self, section_id, section_type):
        """One playable item from a section (an episode for show libraries)."""
        params = {"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 1}
        if section_type == "show":
            params["type"] = 4
        items = self._get(f"/library/sections/{section_id}/all", **params).get("Metadata") or []
        return items[0] if items else None

    def next_episodes(self, episode, count=1):
        show = episode.get("grandparentRatingKey")
        if not show:
            return []
        leaves = self._get(f"/library/metadata/{show}/allLeaves").get("Metadata") or []
        return pick_next(leaves, episode, count)

    def search(self, query, limit=10):
        mc = self._get("/hubs/search", query=query, limit=limit)
        results = []
        for hub in mc.get("Hub", []) or []:
            for item in hub.get("Metadata", []) or []:
                if item.get("type") in ("movie", "episode"):
                    results.append(item)
        return results[:limit]


def client_from_settings(settings):
    return PlexClient(settings.get("plex_url"), settings.get("plex_token"), settings.get("plex_client_id") or "spinup")
