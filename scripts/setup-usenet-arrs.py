#!/usr/bin/env python3
"""One-shot setup of Radarr and Sonarr for regular Usenet downloads.

  python3 scripts/setup-usenet-arrs.py            dry run (the default): reads, prints what it would change
  python3 scripts/setup-usenet-arrs.py --apply    makes the changes

What it does, in each app, idempotently (a second run changes nothing):
  1. reads the existing download clients, remote path mappings and root folders and prints the layout
     (read-only);
  2. adds the SABnzbd download client with the app's own category and, with WATCH_HOLD=on, recent and older
     priority Paused (Radarr recentMoviePriority/olderMoviePriority, Sonarr recentTvPriority/olderTvPriority;
     the Paused value is read from the app's own field options): every grab arrives paused and arr-webhook's
     watch hold classifies it before anything downloads. With WATCH_HOLD not on, nothing is set Paused, and a
     Paused priority left by an earlier run is put back to Default;
  3. adds the Newznab indexer(s) from NEWZNAB_INDEXERS, or, when Prowlarr is in use (PROWLARR_URL and
     PROWLARR_API_KEY set, or the app's indexers look Prowlarr-synced), adds them to Prowlarr instead and
     leaves the apps' indexer lists to Prowlarr's sync;
  4. adds a remote path mapping only when SABNZBD_ARR_VISIBLE_DIR (where the app sees SABnzbd's complete
     folder) differs from SABNZBD_COMPLETE_DIR (where SABnzbd writes it);
  5. sets the default delay profile: preferred protocol Usenet, Usenet delay 0, torrent delay 0. The custom
     format score and quality still decide what is grabbed; the preferred protocol only breaks ties.

Settings come from the environment, then from .env (--env-file): RADARR_URL / SONARR_URL (or
TIERING_RADARR_URL / TIERING_SONARR_URL), RADARR_API_KEY, SONARR_API_KEY, SABNZBD_URL, SABNZBD_API_KEY,
SABNZBD_CATEGORY_RADARR / _SONARR, WATCH_HOLD, NEWZNAB_INDEXERS, PROWLARR_URL, PROWLARR_API_KEY.
Keys and passwords are never printed: output names what changes, not the values.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

APPS = {
    "Radarr": {"url": ("RADARR_URL", "TIERING_RADARR_URL"), "key": "RADARR_API_KEY", "cat_env": "SABNZBD_CATEGORY_RADARR",
               "cat_default": "radarr", "cat_field": "movieCategory",
               "prio_fields": ("recentMoviePriority", "olderMoviePriority")},
    "Sonarr": {"url": ("SONARR_URL", "TIERING_SONARR_URL"), "key": "SONARR_API_KEY", "cat_env": "SABNZBD_CATEGORY_SONARR",
               "cat_default": "sonarr", "cat_field": "tvCategory",
               "prio_fields": ("recentTvPriority", "olderTvPriority")},
}
MASK = "********"


class ApiError(Exception):
    """A call failed. The message never carries the URL or key."""


class Api:
    def __init__(self, base, key, prefix="/api/v3", timeout=30):
        self.base, self._key, self.prefix, self.timeout = base.rstrip("/"), key, prefix, timeout

    def __repr__(self):
        return "Api(<url>, <key>)"

    def _do(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + self.prefix + path, data=data, method=method,
                                     headers={"X-Api-Key": self._key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise ApiError(f"{method} {path} -> HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(f"{method} {path} failed: {type(e).__name__}") from None
        return json.loads(raw) if raw else None

    def get(self, path):
        return self._do("GET", path)

    def post(self, path, body):
        return self._do("POST", path, body)

    def put(self, path, body):
        return self._do("PUT", path, body)


def load_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env[k.strip()] = v
    except OSError:
        pass
    return env


def fields_map(item):
    return {f.get("name"): f.get("value") for f in item.get("fields") or []}


def set_fields(body, values):
    """Set field values by name in a schema/resource body. Returns the names the schema does not have."""
    have = {f.get("name"): f for f in body.get("fields") or []}
    missing = [k for k in values if k not in have]
    for k, v in values.items():
        if k in have:
            have[k]["value"] = v
    return missing


class Out:
    def __init__(self, apply):
        self.apply, self.changes = apply, 0

    def say(self, app, msg):
        print(f"[{app}] {msg}")

    def act(self, app, what, fn):
        self.changes += 1
        if not self.apply:
            self.say(app, f"would {what}")
            return None
        r = fn()
        self.say(app, f"did: {what}")
        return r


def base_of(url):
    p = urllib.parse.urlparse(url)
    return p.scheme, p.hostname or "", p.port, p.path.rstrip("/")


def norm_url(u):
    return (u or "").strip().rstrip("/").lower()


# -- 1. layout (read-only) --------------------------------------------------------------------------------
def report_layout(api, app, out):
    clients = api.get("/downloadclient") or []
    for c in clients:
        f = fields_map(c)
        cat = f.get("movieCategory") or f.get("tvCategory") or f.get("category")
        out.say(app, f"client: {c.get('implementation')} category={cat!r} enabled={c.get('enable')} "
                     f"protocol={c.get('protocol')}")
    maps = api.get("/remotepathmapping") or []
    for m in maps:
        out.say(app, f"remote path mapping: {m.get('remotePath')} -> {m.get('localPath')}")
    roots = api.get("/rootfolder") or []
    for r in roots:
        out.say(app, f"root folder: {r.get('path')}")
    return {"clients": clients, "maps": maps, "roots": roots}


# -- 2. SABnzbd client --------------------------------------------------------------------------------------
def option_value(field, name, fallback):
    """The value of the select option called `name` in a schema field (read from the app, not guessed)."""
    for o in field.get("selectOptions") or []:
        if str(o.get("name") or "").lower() == name.lower():
            return o.get("value")
    return fallback


def priority_wants(app, fields, hold_on):
    """{field: value} for the client's recent/older priority. With the watch hold on, every grab is added to
    SABnzbd Paused, so nothing downloads before arr-webhook has classified it (watch_hold.classify_usenet); the
    value comes from the field's own options (-2 only when the app lists none). With it off, a priority that
    is Paused is put back to Default, so no job is left waiting for a hold that is not running. Fields the app
    does not have are left out."""
    out = {}
    for name in APPS[app]["prio_fields"]:
        f = next((x for x in fields or [] if x.get("name") == name), None)
        if f is None:
            continue
        paused, default = option_value(f, "Paused", -2), option_value(f, "Default", -100)
        if hold_on:
            out[name] = paused
        elif f.get("value") == paused:
            out[name] = default
    return out


def ensure_sab_client(api, app, out, env, layout):
    cfg = APPS[app]
    sab_url = env.get("SABNZBD_URL") or ""
    if not sab_url or not env.get("SABNZBD_API_KEY"):
        out.say(app, "SABNZBD_URL / SABNZBD_API_KEY not set: download client skipped")
        return
    scheme, host, port, urlbase = base_of(sab_url)
    cat = env.get(cfg["cat_env"]) or cfg["cat_default"]
    want = {"host": host, "port": port or (443 if scheme == "https" else 80), "useSsl": scheme == "https",
            "urlBase": urlbase, cfg["cat_field"]: cat}
    # match by host (and port): AltMount also offers a SABnzbd-type API, so "the first SABnzbd client"
    # could be the wrong one
    sab_type = [c for c in layout["clients"] if str(c.get("implementation")).lower() == "sabnzbd"]
    existing = [c for c in sab_type if str(fields_map(c).get("host") or "").lower() == str(host).lower()
                and str(fields_map(c).get("port") or "") == str(want["port"])]
    hold_on = str(env.get("WATCH_HOLD") or "").strip().lower() == "on"
    if not hold_on:
        out.say(app, "WATCH_HOLD is not on: grabs are not added Paused (set WATCH_HOLD=on in .env and run again)")
    if existing:
        c = existing[0]
        want.update(priority_wants(app, c.get("fields"), hold_on))
        have = fields_map(c)
        if all(have.get(k) == v or (k == "urlBase" and not have.get(k) and not v) for k, v in want.items()) \
                and c.get("enable"):
            out.say(app, "SABnzbd client already set")
            return
        body = json.loads(json.dumps(c))
        miss = set_fields(body, {**want, "apiKey": env["SABNZBD_API_KEY"]})
        if miss:
            out.say(app, f"SABnzbd client has no field(s) {miss}: left alone")
            return
        body["enable"] = True
        out.act(app, "update the SABnzbd download client", lambda: api.put(f"/downloadclient/{c['id']}", body))
        return
    schemas = [s for s in api.get("/downloadclient/schema") or [] if str(s.get("implementation")).lower() == "sabnzbd"]
    if not schemas:
        out.say(app, "no SABnzbd schema in this app: skipped")
        return
    body = json.loads(json.dumps(schemas[0]))
    want.update(priority_wants(app, body.get("fields"), hold_on))
    miss = set_fields(body, {**want, "apiKey": env["SABNZBD_API_KEY"]})
    if miss:
        out.say(app, f"SABnzbd schema has no field(s) {miss}: skipped")
        return
    taken = {str(c.get("name")) for c in layout["clients"]}
    name = "SABnzbd" if "SABnzbd" not in taken else "SABnzbd (usenet)"
    body.update({"name": name, "enable": True, "priority": 1, "removeCompletedDownloads": True,
                 "removeFailedDownloads": True, "tags": []})
    body.pop("id", None)
    out.act(app, f"add the SABnzbd download client (category {cat!r})", lambda: api.post("/downloadclient", body))


# -- 3. indexers --------------------------------------------------------------------------------------------
def parse_indexers(raw):
    try:
        v = json.loads(raw or "[]")
    except ValueError:
        return []
    return [d for d in v if isinstance(d, dict) and d.get("url") and d.get("apikey")] if isinstance(v, list) else []


def split_api(url):
    """('https://host', '/api') from an indexer URL that may or may not end in /api."""
    u = url.strip().rstrip("/")
    if u.lower().endswith("/api"):
        return u[:-4], "/api"
    return u, "/api"


def ensure_indexers(api, app, out, env, prowlarr_mode):
    """Add the Newznab indexers to `api` (an app, or Prowlarr). Idempotent by base URL."""
    wanted = parse_indexers(env.get("NEWZNAB_INDEXERS"))
    if not wanted:
        out.say(app, "NEWZNAB_INDEXERS empty or unreadable: indexers skipped")
        return
    existing = api.get("/indexer") or []
    have = {norm_url(fields_map(i).get("baseUrl")) for i in existing if str(i.get("implementation")).lower() == "newznab"}
    schema = None
    for ix in wanted:
        base, api_path = split_api(ix["url"])
        if norm_url(base) in have:
            out.say(app, f"indexer {ix.get('name') or 'unnamed'!r} already there")
            continue
        if schema is None:
            schema = [s for s in api.get("/indexer/schema") or [] if str(s.get("implementation")).lower() == "newznab"]
        if not schema:
            out.say(app, "no Newznab schema: indexers skipped")
            return
        generic = [s for s in schema if str(s.get("name") or s.get("implementationName") or "").lower() == "newznab"]
        body = json.loads(json.dumps((generic or schema)[0]))
        miss = set_fields(body, {"baseUrl": base, "apiPath": api_path, "apiKey": ix["apikey"]})
        if miss:
            out.say(app, f"Newznab schema has no field(s) {miss}: skipped")
            return
        body.pop("id", None)
        body["name"] = ix.get("name") or "Newznab"
        body["tags"] = []
        if prowlarr_mode:
            body.update({"enable": True, "appProfileId": 1, "priority": 25})
        else:
            body.update({"enableRss": True, "enableAutomaticSearch": True, "enableInteractiveSearch": True,
                         "priority": 25})
        out.act(app, f"add the indexer {body['name']!r}", lambda b=body: api.post("/indexer", b))


def prowlarr_state(env, layout_indexers):
    """('configured' | 'looks_synced' | 'none')."""
    if env.get("PROWLARR_URL"):
        return "configured" if env.get("PROWLARR_API_KEY") else "no_key"
    for i in layout_indexers:
        b = norm_url(fields_map(i).get("baseUrl"))
        if "prowlarr" in b or ":9696" in b:
            return "looks_synced"
    return "none"


# -- 4. path mapping ----------------------------------------------------------------------------------------
def ensure_path_mapping(api, app, out, env, layout):
    sab_dir = (env.get("SABNZBD_COMPLETE_DIR") or "").rstrip("/")
    arr_dir = (env.get("SABNZBD_ARR_VISIBLE_DIR") or "").rstrip("/")
    if not sab_dir or not arr_dir or sab_dir == arr_dir:
        out.say(app, "no remote path mapping needed (SABnzbd's complete folder is at the same path)")
        return
    host = base_of(env.get("SABNZBD_URL") or "")[1]
    for m in layout["maps"]:
        if str(m.get("remotePath")).rstrip("/") == sab_dir and str(m.get("localPath")).rstrip("/") == arr_dir:
            out.say(app, "remote path mapping already set")
            return
    out.act(app, "add the remote path mapping for SABnzbd's complete folder",
            lambda: api.post("/remotepathmapping", {"host": host, "remotePath": sab_dir + "/", "localPath": arr_dir + "/"}))


# -- 5. delay profile ---------------------------------------------------------------------------------------
def ensure_delay_profile(api, app, out):
    profiles = api.get("/delayprofile") or []
    default = next((p for p in profiles if not p.get("tags")), None)
    if default is None:
        out.say(app, "no default delay profile found: skipped")
        return
    want = {"preferredProtocol": "usenet", "usenetDelay": 0, "torrentDelay": 0, "enableUsenet": True, "enableTorrent": True}
    if all(default.get(k) == v for k, v in want.items()):
        out.say(app, "delay profile already set")
        return
    body = {**default, **want}
    out.act(app, "set the default delay profile (Usenet preferred, delays 0)",
            lambda: api.put(f"/delayprofile/{default['id']}", body))


def setup_app(app, api, env, out, prowlarr=None):
    layout = report_layout(api, app, out)
    ensure_sab_client(api, app, out, env, layout)
    state = prowlarr_state(env, api.get("/indexer") or [])
    if state == "configured" and prowlarr is not None:
        out.say(app, "Prowlarr in use: indexers are added there (once), not here")
    elif state in ("configured", "no_key", "looks_synced"):
        out.say(app, f"Prowlarr {state}: direct indexer add skipped; set PROWLARR_URL and PROWLARR_API_KEY to add through it")
    else:
        ensure_indexers(api, app, out, env, prowlarr_mode=False)
    ensure_path_mapping(api, app, out, env, layout)
    ensure_delay_profile(api, app, out)


def main(argv=None, env=None, make_api=Api):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    ap.add_argument("--env-file", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
    a = ap.parse_args(argv)
    if env is None:
        env = {**load_env(a.env_file), **os.environ}
    out = Out(a.apply)
    print("mode:", "APPLY" if a.apply else "dry run (nothing is changed; pass --apply)")
    prowlarr = None
    if env.get("PROWLARR_URL") and env.get("PROWLARR_API_KEY"):
        prowlarr = make_api(env["PROWLARR_URL"], env["PROWLARR_API_KEY"], prefix="/api/v1")
        try:
            ensure_indexers(prowlarr, "Prowlarr", out, env, prowlarr_mode=True)
        except ApiError as e:
            print(f"[Prowlarr] {e}")
    failed = 0
    for app, cfg in APPS.items():
        url = next((env[k] for k in cfg["url"] if env.get(k)), "")
        key = env.get(cfg["key"], "")
        if not url or not key:
            print(f"[{app}] URL or API key not set: skipped")
            continue
        try:
            setup_app(app, make_api(url, key), env, out, prowlarr)
        except ApiError as e:
            failed += 1
            print(f"[{app}] stopped: {e}")
    print(f"{out.changes} change(s) {'made' if a.apply else 'planned'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
