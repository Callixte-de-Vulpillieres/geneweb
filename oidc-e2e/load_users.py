#!/usr/bin/env python3
"""Load users into a Keycloak realm over the Admin REST API.

Keycloak's startup / directory realm-import creates every user inside one
growing Hibernate session, so it degrades to O(n^2) and runs as a single
all-or-nothing transaction (it times out and rolls back on large realms).
This loader instead creates each user with its own request -> its own short
transaction: linear time, resumable, and parallelizable. Argon2 hashing is
kept (Keycloak hashes the plaintext ``credentials`` value per request).

Workflow for a large realm:
  1. Import only the realm structure at startup (roles, client, mappers) --
     i.e. the ``<realm>-realm.json`` written by realm_from_assoconnect.py
     --out-dir, with no users.
  2. Run this loader against the running server to create the users:

       python3 oidc-e2e/load_users.py \
         --server https://auth.roglo.eu --realm roglo \
         --admin-user admin --admin-pass "$KC_ADMIN_PASSWORD" \
         --dir ./import --workers 8

Users are read from the ``<realm>-users-*.json`` chunk files (--dir) or from a
single realm/users JSON (--users-json, uses its top-level ``users``). Realm
roles listed on each user (``realmRoles``) are assigned after creation. Users
that already exist (HTTP 409) are skipped, so the loader can be re-run to
resume. A CSV error log (--error-log) records any user that failed.
"""

import argparse
import concurrent.futures as futures
import csv
import glob
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class Client:
    """Minimal Keycloak Admin API client with token auto-refresh."""

    def __init__(self, server, realm, admin_user, admin_pass, admin_realm, admin_client):
        self.base = server.rstrip("/")
        self.realm = realm
        self._cred = (admin_user, admin_pass, admin_realm, admin_client)
        self._lock = threading.Lock()
        self._token = None
        self._exp = 0.0

    def _fetch_token(self):
        user, pw, realm, client = self._cred
        data = urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": client,
                "username": user,
                "password": pw,
            }
        ).encode()
        url = f"{self.base}/realms/{realm}/protocol/openid-connect/token"
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
            body = json.load(r)
        self._token = body["access_token"]
        self._exp = time.time() + body.get("expires_in", 60) - 15

    def token(self):
        with self._lock:
            if not self._token or time.time() >= self._exp:
                self._fetch_token()
            return self._token

    def request(self, method, path, body=None):
        """Return (status, headers, parsed_json_or_None). Retries on 5xx/401."""
        url = f"{self.base}/admin/realms/{self.realm}{path}"
        payload = json.dumps(body).encode() if body is not None else None
        for attempt in range(5):
            req = urllib.request.Request(url, data=payload, method=method)
            req.add_header("Authorization", f"Bearer {self.token()}")
            if payload is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    txt = r.read().decode() or "null"
                    return r.status, dict(r.headers), json.loads(txt)
            except urllib.error.HTTPError as e:
                if e.code == 401:  # token likely expired mid-flight
                    with self._lock:
                        self._token = None
                    continue
                if e.code >= 500 and attempt < 4:
                    time.sleep(2**attempt)
                    continue
                return e.code, dict(e.headers), None
            except urllib.error.URLError:
                if attempt < 4:
                    time.sleep(2**attempt)
                    continue
                raise
        return 0, {}, None

    def realm_roles(self):
        _, _, roles = self.request("GET", "/roles")
        return {r["name"]: {"id": r["id"], "name": r["name"]} for r in (roles or [])}


def load_users(source_files):
    users = []
    for path in source_files:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        users.extend(doc.get("users", []) if isinstance(doc, dict) else doc)
    return users


def create_user(client, roles_by_name, user):
    """Create one user and assign its realm roles. Returns (status, detail)."""
    rep = dict(user)
    wanted = rep.pop("realmRoles", []) or []
    status, headers, _ = client.request("POST", "/users", rep)
    if status == 409:
        return "exists", None
    if status != 201:
        return "error", f"create HTTP {status}"

    loc = headers.get("Location", "")
    uid = loc.rstrip("/").rsplit("/", 1)[-1] if loc else None
    if uid and wanted:
        reps = [roles_by_name[n] for n in wanted if n in roles_by_name]
        missing = [n for n in wanted if n not in roles_by_name]
        if missing:
            return "error", f"unknown realm role(s): {','.join(missing)}"
        st, _, _ = client.request("POST", f"/users/{uid}/role-mappings/realm", reps)
        if st not in (204, 201):
            return "error", f"role-mapping HTTP {st}"
    return "created", None


def main():
    ap = argparse.ArgumentParser(description="Load users into Keycloak via the Admin API.")
    ap.add_argument("--server", required=True, help="e.g. https://auth.roglo.eu")
    ap.add_argument("--realm", required=True)
    ap.add_argument("--admin-user", required=True)
    ap.add_argument(
        "--admin-pass", default=os.environ.get("KC_ADMIN_PASSWORD"),
        help="admin password (or set KC_ADMIN_PASSWORD)",
    )
    ap.add_argument("--admin-realm", default="master")
    ap.add_argument("--admin-client", default="admin-cli")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", metavar="DIR", help="dir with <realm>-users-*.json chunk files")
    src.add_argument("--users-json", nargs="+", metavar="FILE", help="realm/users JSON file(s)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--error-log", metavar="FILE", help="CSV of users that failed")
    args = ap.parse_args()

    if not args.admin_pass:
        ap.error("no admin password (pass --admin-pass or set KC_ADMIN_PASSWORD)")

    if args.dir:
        files = sorted(
            glob.glob(os.path.join(args.dir, f"{args.realm}-users-*.json")),
            key=lambda p: int(p.rsplit("-", 1)[-1].split(".")[0]),
        )
        if not files:
            ap.error(f"no {args.realm}-users-*.json files in {args.dir}")
    else:
        files = args.users_json

    users = load_users(files)
    client = Client(
        args.server, args.realm, args.admin_user, args.admin_pass,
        args.admin_realm, args.admin_client,
    )
    roles_by_name = client.realm_roles()
    print(f"loading {len(users)} users into realm {args.realm!r} "
          f"({len(roles_by_name)} realm roles) with {args.workers} workers",
          file=sys.stderr)

    counts = {"created": 0, "exists": 0, "error": 0}
    errors = []
    lock = threading.Lock()
    done = 0

    def work(u):
        nonlocal done
        try:
            outcome, detail = create_user(client, roles_by_name, u)
        except Exception as e:  # network etc.
            outcome, detail = "error", repr(e)
        with lock:
            counts[outcome] += 1
            if outcome == "error":
                errors.append((u.get("username", ""), u.get("email", ""), detail))
            done_local = done = done + 1
        if done_local % 500 == 0:
            print(f"  {done_local}/{len(users)} "
                  f"(created={counts['created']} exists={counts['exists']} "
                  f"error={counts['error']})", file=sys.stderr)
        return outcome

    with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, users))

    print(f"done: created={counts['created']} exists={counts['exists']} "
          f"error={counts['error']}", file=sys.stderr)
    if errors and args.error_log:
        with open(args.error_log, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["username", "email", "error"])
            w.writerows(errors)
        print(f"wrote {len(errors)} failures to {args.error_log}", file=sys.stderr)
    sys.exit(1 if counts["error"] else 0)


if __name__ == "__main__":
    main()
