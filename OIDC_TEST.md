# Testing OIDC (SSO) locally, end to end

A complete local OIDC login against a real identity provider (Keycloak), so you
can exercise wizard / friend / visitor access, the person mapping, and logout.

> **You must run this from the `oidc-e2e-test` branch of the fork.** It ships the
> configs the commands below reference (`oidc-e2e/keycloak-realm.json`,
> `oidc-e2e/Caddyfile`, `oidc-e2e/bases/galichet.gwf`) and builds the database
> from the tracked sample `test/galichet.gw` — nothing needs to be created by
> hand.

Clone that branch directly:

```sh
git clone -b oidc-e2e-test https://github.com/Callixte-de-Vulpillieres/geneweb.git
cd geneweb
```

Or, if you already have a clone, add the fork and switch to it:

```sh
git remote add fork https://github.com/Callixte-de-Vulpillieres/geneweb.git
git fetch fork oidc-e2e-test
git switch oidc-e2e-test
```

> **OIDC is UNIX-only** (it uses `/dev/urandom`), so run this on Linux or macOS,
> and gwd must be reached over **HTTPS** (the session cookie is `Secure` /
> `__Host-`) — hence Caddy in front to terminate TLS at `https://localhost`.

## Prerequisites

- GeneWeb build toolchain (`opam`, `dune`), plus `docker`, `caddy`, `curl`.
- macOS + Firefox only: `brew install nss` so Caddy can trust its CA in Firefox
  (or use the Firefox workaround in Troubleshooting).

## 1. Build and prepare the base

```sh
eval $(opam env)
dune build @install
_build/install/default/bin/gwc -bd oidc-e2e/bases -f -gwo -o galichet test/galichet.gw
```

This builds `gwd`/`gwc` (under `_build/install/default/bin/`) and compiles the
sample base; the OIDC config `oidc-e2e/bases/galichet.gwf` already on the branch
is preserved.

## 2. Run it (three terminals, from the repo root)

```sh
# Terminal 1 — Keycloak (wait for "Imported realm geneweb")
docker run --rm --name gw-kc -p 8080:8080 \
  -e KEYCLOAK_ADMIN=admin -e KEYCLOAK_ADMIN_PASSWORD=admin \
  -v "$PWD/oidc-e2e/keycloak-realm.json:/opt/keycloak/data/import/realm.json:ro" \
  quay.io/keycloak/keycloak:latest start-dev --import-realm

# Terminal 2 — gwd
./_build/install/default/bin/gwd --hd hd --bd oidc-e2e/bases --port 2317

# Terminal 3 — Caddy (first run installs its local CA; may prompt for sudo)
sudo caddy run --config oidc-e2e/Caddyfile
```

Test users (defined in `oidc-e2e/keycloak-realm.json`). `galichet.gwf` uses
`oidc_user_claim=geneweb_login`, so the identity is the login carried by the
`geneweb_login` claim (not the email):

| User | Password | Role | Person key | Result |
|------|----------|------|-----------|--------|
| `alice` | `alice` | `geneweb-wizard` | `Paul.0 Galichet` | **wizard**, linked to that individual |
| `bob` | `bob` | `geneweb-friend` | _(none)_ | **friend** |
| _(any user with no matching role)_ | | | | **visitor** (no session, sees public data) |

## 3. Walk-through

1. Open **<https://localhost/galichet>** while logged out → the welcome page
   shows a **"Connect with Keycloak"** button.
2. Click it → the Keycloak login page.
3. Log in as **alice / alice** → back on the welcome page as a **wizard**
   (identity + a *disconnect* button). Because the id_token carries the person
   key, alice's name links to the individual **Paul Galichet** (`m=S&pn=...`);
   **bob** (no key) shows no such link.
4. Click **disconnect** (POST) → the session cookie is cleared and you are sent
   through the provider's logout.
5. Repeat with **bob / bob** for **friend** access, or a role-less user for
   **visitor**.

Error page: while logged out, open
`https://localhost/galichet?m=OIDC_CALLBACK&state=x&code=y` (no valid login
cookie) → the **"Authentication Error"** page.

## OIDC and legacy password auth together (optional)

Uncomment the `wizard_passwd` / `friend_passwd` lines at the bottom of
`oidc-e2e/bases/galichet.gwf` and restart gwd.

> When `oidc_provider_url` is set, the "empty password ⇒ everyone is wizard"
> default is disabled, so legacy access works only if you actually set a
> password.

Precedence per request (first match wins): URL token (`w=<token>`) → OIDC session
cookie → HTTP Basic header. To check:

- Logged out, the welcome page shows both a **Wizard** button (legacy) and the
  **Connect with Keycloak** button.
- **Wizard** + password `letmein` → wizard via Basic auth; the disconnect is the
  plain `w=` link, not the OIDC form.
- **Connect with Keycloak** as `alice` → wizard via SSO; disconnect is the OIDC
  logout form. With an OIDC session, a cached Basic header is ignored.
- `m=OIDC_LOGOUT` clears only the OIDC cookie; a cached Basic credential stays
  until the browser forgets it.

## Teardown

```sh
# Ctrl-C the three terminals, then:
docker rm -f gw-kc 2>/dev/null || true
rm -rf oidc-e2e/bases/galichet.gwb oidc-e2e/bases/galichet.lck oidc-e2e/bases/cnt
```

## Troubleshooting

- **Firefox "unknown issuer"** — Firefox uses its own trust store. Set
  `security.enterprise_roots.enabled = true` in `about:config` and restart, or
  `brew install nss` and re-run Caddy, or accept the warning (HTTPS still works).
  Chrome/Safari trust the macOS keychain already.
- **`OIDC not configured`** — `--bd` must point to `oidc-e2e/bases` and the file
  be `oidc-e2e/bases/galichet.gwf`.
- **redirect_uri mismatch** — the client `redirectUris` must match
  `oidc_redirect_uri` exactly (`https://localhost/galichet`).
- **Logged in but only visitor** — the role isn't in the id_token; confirm the
  realm imported and `oidc_role_claim=realm_access.roles`.
- **`Cannot bind … 2317`** — port in use; `pkill -f _build/install/default/bin/gwd`
  or pick another `--port` (update the Caddyfile / redirect URIs accordingly).
- **Non-privileged alternative to `:443`** — set the Caddyfile site to
  `https://localhost:8443` and change `oidc_redirect_uri` and the client's
  `redirectUris` to `https://localhost:8443/galichet` (re-import the realm).
