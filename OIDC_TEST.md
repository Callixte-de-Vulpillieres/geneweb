# Testing OIDC (SSO) locally, end to end

This walks through a complete local OIDC login against a real identity provider
(Keycloak), so you can exercise wizard / friend / visitor access and logout.

Everything needed lives in this branch: the identity-provider realm, the TLS
front-end config and the base `.gwf` are under `oidc-e2e/`, and the database
itself is built from the tracked sample `test/galichet.gw` — no personal data
involved.

> **OIDC is UNIX-only** (it uses `/dev/urandom` for its CSPRNG and is refused on
> Windows), so run this on Linux or macOS.

## Why the extra moving parts

- The session cookie is `Secure` + `__Host-`, so gwd **must be reached over
  HTTPS** — we put **Caddy** in front of gwd to terminate TLS at
  `https://localhost`.
- `oidc_provider_url` **must be https in production** (the id_token signature is
  not verified locally; trust comes from the TLS connection to the token
  endpoint). For this local loop, a Keycloak dev server on `http://localhost`
  is acceptable.
- gwd shells out to **`curl`** for the discovery and token HTTP calls, so curl
  must be on `PATH`.

## Prerequisites

- A working GeneWeb build toolchain (`opam`, `dune`).
- `docker` (for Keycloak), `caddy` (TLS front end), `curl`.
- macOS + **Firefox** only: `certutil` so Caddy can trust its CA in Firefox —
  `brew install nss` (or use the Firefox workaround in Troubleshooting).

---

## 1. Build gwd and gwc

```sh
eval $(opam env)
dune build @install
```

The binaries are then under `_build/install/default/bin/` (`gwd`, `gwc`).

## 2. Build the sample base

The base is compiled from the tracked sample `test/galichet.gw` into
`oidc-e2e/bases/` (its OIDC-enabled `oidc-e2e/bases/galichet.gwf` is already in
this branch and is preserved by the build):

```sh
_build/install/default/bin/gwc -bd oidc-e2e/bases -f -gwo -o galichet test/galichet.gw
```

`oidc-e2e/bases/galichet.gwf` (the OIDC-relevant keys):

```
oidc_provider_url=http://localhost:8080/realms/geneweb
oidc_client_id=geneweb
oidc_client_secret=geneweb-secret
oidc_redirect_uri=https://localhost/galichet
oidc_user_claim=email
oidc_role_claim=realm_access.roles
oidc_wizard_role=geneweb-wizard
oidc_friend_role=geneweb-friend
oidc_person_key_claim=geneweb_person_key
oidc_provider_name=Keycloak
```

| Key | Value here | Notes |
|-----|-----------|-------|
| `oidc_provider_url` | Keycloak realm URL | `.well-known/openid-configuration` is fetched under it |
| `oidc_client_id` / `oidc_client_secret` | `geneweb` / `geneweb-secret` | must match the Keycloak client |
| `oidc_redirect_uri` | `https://localhost/galichet` | must match a client redirect URI exactly |
| `oidc_user_claim` | `email` | identity (falls back to `sub`) |
| `oidc_role_claim` | `realm_access.roles` | array claim inspected for roles |
| `oidc_wizard_role` / `oidc_friend_role` | `geneweb-wizard` / `geneweb-friend` | grant wizard / friend |
| `oidc_person_key_claim` | `geneweb_person_key` | claim carrying a GeneWeb person key (`first_name.occ surname`) to link the user to a database individual |
| `oidc_provider_name` | `Keycloak` | button reads "Connect with Keycloak" |

## 3. Identity provider (Keycloak)

`oidc-e2e/keycloak-realm.json` defines a realm with a confidential client (fixed
secret, PKCE S256), two users, and two mappers: one puts realm roles into the
id_token, the other exposes the user attribute `geneweb_person_key` as the claim
`geneweb_person_key`. `alice` carries that attribute set to `Jean Pierre.0
Galichet`, a person present in the sample base. Test users:

| User | Password | Role | Person key | Result |
|------|----------|------|-----------|--------|
| `alice` | `alice` | `geneweb-wizard` | `Jean Pierre.0 Galichet` | **wizard**, linked to that individual |
| `bob` | `bob` | `geneweb-friend` | _(none)_ | **friend** |
| _(any user with no matching role)_ | | | | **visitor** (no session, sees public data) |

## 4. TLS front end (Caddy)

`oidc-e2e/Caddyfile`:

```
localhost {
	reverse_proxy localhost:2317
}
```

---

## Run it (three terminals, from the repo root)

**Terminal 1 — Keycloak**

```sh
docker run --rm --name gw-kc -p 8080:8080 \
  -e KEYCLOAK_ADMIN=admin -e KEYCLOAK_ADMIN_PASSWORD=admin \
  -v "$PWD/oidc-e2e/keycloak-realm.json:/opt/keycloak/data/import/realm.json:ro" \
  quay.io/keycloak/keycloak:latest start-dev --import-realm
```

Wait for `Imported realm geneweb` and `Keycloak ... started`.
(Admin console, if needed: <http://localhost:8080>, admin / admin.)

**Terminal 2 — gwd**

```sh
eval $(opam env)
./_build/install/default/bin/gwd --hd hd --bd oidc-e2e/bases --port 2317
```

**Terminal 3 — Caddy**

```sh
sudo caddy run --config oidc-e2e/Caddyfile
```

The first Caddy run installs its local CA into the OS trust store (it may prompt
for your password).

---

## Walk-through

1. Open **<https://localhost/galichet>** while logged out → the welcome page
   shows a **"Connect with Keycloak"** button.
2. Click it → you're redirected to the Keycloak login page.
3. Log in as **alice / alice** → back on the welcome page, now authenticated as a
   **wizard** (identity + a *disconnect* button are shown). Because the id_token
   carries `geneweb_person_key`, the identity is linked to the individual **Jean
   Pierre Galichet**: the name shown on the welcome page links to that person's
   record (`m=S&pn=...`). Logging in as **bob** (no person key) shows no such
   link.
4. Click **disconnect** (a POST form) → the session cookie is cleared and you are
   sent through the provider's logout.
5. Repeat with **bob / bob** to see **friend** access, or a role-less user for
   **visitor** behaviour.

Error page: while logged out, open
`https://localhost/galichet?m=OIDC_CALLBACK&state=x&code=y` (no valid login
cookie) → the generic **"Authentication Error"** page.

## OIDC and legacy password auth together

OIDC and GeneWeb's built-in password auth (HTTP Basic, `wizard_passwd` /
`friend_passwd`) can be enabled on the same base at once. Uncomment the password
lines at the bottom of `oidc-e2e/bases/galichet.gwf`:

```
wizard_passwd=letmein
friend_passwd=letmein-friend
```

Restart gwd after editing the `.gwf`.

> When `oidc_provider_url` is set, the "empty password ⇒ everyone is wizard"
> default is disabled. So legacy access only works if you actually set
> `wizard_passwd` / `friend_passwd` (or their `_file` variants). With OIDC
> configured and no password set, the password path grants nothing (visitor).

Precedence for a single request (first match wins):

1. a URL token session (`w=<token>`),
2. else a valid OIDC session cookie,
3. else the HTTP Basic `Authorization` header (`wizard_passwd` / `friend_passwd`).

What to verify:

| Step | Expectation |
|------|-------------|
| Logged out, open `https://localhost/galichet` | Both a **Wizard** button (legacy) and a **Connect with Keycloak** button (OIDC) are shown |
| Click **Wizard**, then at the browser prompt enter any username and the password `letmein` | Wizard via legacy Basic auth; the *disconnect* control is the plain `w=` link, **not** the OIDC logout form |
| Instead click **Connect with Keycloak** and log in as `alice` | Wizard via SSO; the *disconnect* control is the **OIDC logout** form |
| Holding an OIDC session, also send a Basic header (e.g. reuse a tab that cached one) | OIDC wins; the Basic header is ignored until the OIDC cookie is cleared |
| Click the OIDC **disconnect** (`m=OIDC_LOGOUT`) | Clears only the OIDC cookie; a cached Basic credential is unaffected — the browser keeps resending it, so you may still be wizard via legacy until the browser forgets it |

Friend works the same way: set `friend_passwd` and reach it with
`https://localhost/galichet?w=f` (there is no dedicated friend button on the
welcome page).

## Teardown

```sh
# Ctrl-C the three terminals, then:
docker rm -f gw-kc 2>/dev/null || true
# remove the generated database (config files under oidc-e2e/ are tracked):
rm -rf oidc-e2e/bases/galichet.gwb oidc-e2e/bases/galichet.lck oidc-e2e/bases/cnt
```

---

## Troubleshooting

- **Firefox "unknown issuer" (`SEC_ERROR_UNKNOWN_ISSUER`)** — Firefox uses its
  own trust store, not the macOS keychain where Caddy installed its CA. Either:
  - `about:config` → set `security.enterprise_roots.enabled` = `true` → restart
    Firefox (makes Firefox use the OS trust store); **or**
  - `brew install nss` then re-run `sudo caddy run …` (Caddy then installs its CA
    into Firefox too); **or**
  - accept the warning (*Advanced → Accept the Risk and Continue*) — HTTPS still
    works, so the `Secure`/`__Host-` cookie is still set.
  Chrome/Safari trust the macOS keychain, so they work without this.
- **`OIDC not configured`** — the `.gwf` didn't load; check `--bd` points to
  `oidc-e2e/bases` and the file is `oidc-e2e/bases/galichet.gwf`.
- **`OIDC is available only on UNIX`** — you're on Windows; OIDC is Unix-only.
- **redirect_uri mismatch (Keycloak error)** — the client `redirectUris` must
  match `oidc_redirect_uri` exactly (`https://localhost/galichet`).
- **Logged in but only visitor** — the role isn't reaching the id_token; confirm
  the realm imported with the "realm roles in id token" mapper, and that
  `oidc_role_claim=realm_access.roles`.
- **gwd: `Cannot bind … 2317`** — the port is in use (often a stale gwd); free it
  with `pkill -f _build/install/default/bin/gwd` or pick another `--port` (and
  update the Caddyfile / redirect URIs accordingly).
- **`iss` / issuer error from gwd** — the token's `iss` must equal
  `oidc_provider_url`; keep them identical (e.g. both
  `http://localhost:8080/realms/geneweb`).
- **No `curl`** — install it; gwd logs "curl binary was not found in PATH" at
  config load if it's missing.
- **Non-privileged alternative to `:443`** — set the Caddyfile site to
  `https://localhost:8443`, and change `oidc_redirect_uri` and the client's
  `redirectUris` to `https://localhost:8443/galichet` (re-import the realm).
