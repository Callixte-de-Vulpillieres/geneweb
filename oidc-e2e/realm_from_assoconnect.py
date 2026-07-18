#!/usr/bin/env python3
"""Build a Keycloak realm-import JSON from an AssoConnect CSV export.

The CSV is authoritative (one row per person). The GeneWeb wizard ``.auth``
file(s) are joined on the person key to recover the wizard *login* (absent from
the CSV) used as ``geneweb_login`` for wizards, so their wiznotes /
``superwizard`` / ``manitou`` keep working. Both the wizard and friend ``.auth``
files are also cross-checked against the CSV: every field present on both sides
(wizard name; AMI login, password and person key) is compared and any
divergence -- or an entry present in one source but not the other -- is warned.

Per row -> one Keycloak account. The login/password come from the first source
that has them: the friend ``.auth`` entry, then the CSV (``identifiant AMI`` /
``mot de passe AMI``), then -- for wizards -- the wizard ``.auth`` entry; a row
with none of these is skipped with a warning.
  * username = the resolved login kept verbatim, numeric-suffixed on a
    case-insensitive collision (Keycloak lowercases usernames and has no
    case-sensitive mode, so logins differing only by case must be split);
  * login by email is also possible (email is set; realm allows email login);
  * password = the resolved password (treated as valid for preprod; force a
    reset for the real rollout); omitted when empty;
  * roles: friend if AMI access is on, plus geneweb-wizard if ``Statut dans la
    base`` is ``Magicien`` (wizards also have a friend account -> one merged
    account with both roles);
  * geneweb_person_key from the Roglo columns (Prénom / N° d'occurence /
    Patronyme) as ``first_name.occ surname``;
  * geneweb_login = wizard login (joined from .auth) for wizards, else the
    resolved login;
  * firstName/lastName/email, and every other column kept as a user attribute
    (passwords excluded);
  * enabled = false when deceased or missing consent (kept, not skipped).

Warnings (stderr): a Magicien with no matching .auth entry (and vice versa),
overlapping fields that disagree between the two sources, missing person key,
row without an AMI login, duplicate email. A rename log (--rename-log) lists
every account whose AMI login was suffixed: email, previous login, new login.
"""

import argparse
import csv
import json
import re
import sys
import unicodedata


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace("\u2019", "'").strip()


def crush(s):
    """GeneWeb-like key normalization for comparison/join: drop accents, case
    and all whitespace, so "Ansart de Lessan" == "AnsartdeLessan"."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(s.split()).lower()


def attr_key(header):
    k = re.sub(r"[^a-z0-9]+", "_", norm(header)).strip("_")
    return k


# logical field -> ("eq"|"has", needle(s)) resolved against the CSV header row
FIELDS = {
    "contact_id": ("has", ["id du contact"]),
    "nom": ("eq", "nom"),
    "prenom": ("eq", "prenom"),
    "email": ("eq", "email"),
    "roglo_surname": ("has", ["patronyme complet sur roglo"]),
    "roglo_firstname": ("has", ["prenom sur roglo"]),
    "occ": ("has", ["occurence"]),
    "statut": ("has", ["statut dans la base"]),
    "ami_id": ("has", ["identifiant ami"]),
    "ami_pw": ("has", ["mot de passe ami"]),
    "ami_active": ("has", ["acces ami active"]),
    "nom_magicien": ("has", ["nom magicien"]),
    "deceased": ("eq", "decede"),
    "consent_keep": ("has", ["autorise la conservation"]),
    "consent_access": ("has", ["droit d'acces"]),
    "consent_cgu": ("has", ["conditions generales"]),
}


def resolve_columns(fieldnames):
    norm_map = [(norm(h), h) for h in fieldnames]
    cols = {}
    for field, (mode, target) in FIELDS.items():
        found = None
        for nh, h in norm_map:
            if (mode == "eq" and nh == target) or (
                mode == "has" and all(n in nh for n in target)
            ):
                found = h
                break
        cols[field] = found
        if found is None:
            print(f"warning: CSV column for {field!r} not found", file=sys.stderr)
    return cols


def person_key(first, occ, surname):
    first, surname = first.strip(), surname.strip()
    occ = occ.strip() or "0"
    if not first or not surname:
        return ""
    return f"{first}.{occ} {surname}"


def auth_person_key(field4, name):
    raw = field4.strip()
    if not raw and "|" in name:
        return name.split("|", 1)[1].strip()
    parts = raw.split("/")
    if len(parts) >= 3:
        return f"{parts[0].strip()}.{(parts[-1].strip() or '0')} " + " ".join(
            p.strip() for p in parts[1:-1]
        )
    return raw


def parse_auth(path):
    """Parse a GeneWeb .auth file into a list of entries with all fields:
    login, password, name (raw field 3), person_key (from field 4 or name|key)."""
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split(":", 2)
            if len(parts) < 2 or not parts[0]:
                continue
            login, password = parts[0], parts[1]
            info = parts[2] if len(parts) == 3 else ""
            name, field4 = info, ""
            if ":" in info:
                name, rest = info.split(":", 1)
                field4 = rest.split(":", 1)[0]
            entries.append(
                {
                    "login": login,
                    "password": password,
                    "name": name,
                    "person_key": auth_person_key(field4, name),
                }
            )
    return entries


def index_by_pk(entries, kind):
    """Index entries by the crushed person key (space/accent/case insensitive)."""
    d = {}
    for e in entries:
        if not e["person_key"]:
            print(
                f"warning: {kind} .auth entry {e['login']!r} has no person key; "
                "cannot cross-check it",
                file=sys.stderr,
            )
            continue
        key = crush(e["person_key"])
        if key in d and d[key]["login"] != e["login"]:
            print(
                f"warning: {kind} .auth has two logins for person "
                f"{e['person_key']!r}: {d[key]['login']!r} and {e['login']!r}",
                file=sys.stderr,
            )
        d[key] = e
    return d


def main():
    ap = argparse.ArgumentParser(
        description="Generate a Keycloak realm JSON from an AssoConnect CSV."
    )
    ap.add_argument("--csv", required=True, metavar="FILE")
    ap.add_argument(
        "--wizard", action="append", default=[], metavar="FILE",
        help="GeneWeb wizard .auth file (repeatable): join for the wizard login "
        "and cross-check overlapping fields",
    )
    ap.add_argument(
        "--friend", action="append", default=[], metavar="FILE",
        help="GeneWeb friend .auth file (repeatable): cross-check AMI login, "
        "password and person key against the CSV",
    )
    ap.add_argument(
        "--template", default="oidc-e2e/keycloak-realm.json", metavar="FILE"
    )
    ap.add_argument("--out", metavar="FILE", help="realm JSON (default: stdout)")
    ap.add_argument(
        "--rename-log", metavar="FILE",
        help="CSV log of AMI logins that were suffixed (email, previous, new)",
    )
    args = ap.parse_args()

    wiz_entries = [e for p in args.wizard for e in parse_auth(p)]
    frd_entries = [e for p in args.friend for e in parse_auth(p)]
    wiz_by_pk = index_by_pk(wiz_entries, "wizard")
    frd_by_pk = index_by_pk(frd_entries, "friend")
    frd_by_login = {}
    for e in frd_entries:
        frd_by_login.setdefault(e["login"], []).append(e)
    wiz_used, frd_used = set(), set()

    with open(args.csv, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = resolve_columns(reader.fieldnames or [])
        rows = list(reader)

    def col(row, field):
        h = cols.get(field)
        return (row.get(h) or "").strip() if h else ""

    accounts = []
    for row in rows:
        ami = col(row, "ami_id")
        ami_pw = col(row, "ami_pw")
        is_wizard = norm(col(row, "statut")) == "magicien"

        pk = person_key(
            col(row, "roglo_firstname"), col(row, "occ"), col(row, "roglo_surname")
        )
        who = ami or col(row, "email") or col(row, "contact_id")
        if is_wizard and not pk:
            print(f"warning: wizard {who!r} has no person key in the CSV", file=sys.stderr)

        # locate the matching .auth entries (by crushed person key; friend also
        # by exact AMI login as a fallback).
        frd_e = frd_by_pk.get(crush(pk)) if pk else None
        if frd_e is None and ami:
            cands = frd_by_login.get(ami, [])
            if cands:
                frd_e = cands[0]
                if pk and crush(frd_e["person_key"]) != crush(pk):
                    print(
                        f"warning: person key differs for AMI login {ami!r}: CSV "
                        f"{pk!r} vs friend .auth {frd_e['person_key']!r}",
                        file=sys.stderr,
                    )
        if frd_e is not None:
            frd_used.add(crush(frd_e["person_key"]))

        wiz_e = wiz_by_pk.get(crush(pk)) if pk else None
        if wiz_e is not None:
            wiz_used.add(crush(pk))

        # resolve login/password: friend .auth -> CSV AMI -> wizard .auth -> none.
        if frd_e is not None:
            login, password = frd_e["login"], frd_e["password"]
            if ami and frd_e["login"] != ami:
                print(
                    f"warning: AMI login differs for person {pk!r}: CSV {ami!r} "
                    f"vs friend .auth {frd_e['login']!r}",
                    file=sys.stderr,
                )
            if ami_pw and frd_e["password"] != ami_pw:
                print(
                    f"warning: AMI password differs for {login!r} (person {pk!r})",
                    file=sys.stderr,
                )
        elif ami:
            login, password = ami, ami_pw
        elif is_wizard and wiz_e is not None:
            login, password = wiz_e["login"], wiz_e["password"]
        else:
            print(
                f"warning: contact {who!r} has no login in friend .auth, CSV or "
                "wizard .auth; skipped",
                file=sys.stderr,
            )
            continue

        is_friend = (
            norm(col(row, "ami_active")) == "oui" or bool(ami) or frd_e is not None
        )
        roles = []
        if is_wizard:
            roles.append("geneweb-wizard")
        if is_friend or is_wizard:
            roles.append("geneweb-friend")

        # geneweb_login (conf.user): wizard login for wizards, else the account login.
        geneweb_login = login
        if is_wizard:
            if wiz_e is not None:
                geneweb_login = wiz_e["login"]
                nm = col(row, "nom_magicien")
                if nm and wiz_e["name"] and crush(nm) != crush(wiz_e["name"]):
                    print(
                        f"warning: wizard name differs for {pk!r}: CSV {nm!r} "
                        f"vs wizard .auth {wiz_e['name']!r}",
                        file=sys.stderr,
                    )
            else:
                print(
                    f"warning: wizard {login!r} (person {pk!r}) has no matching "
                    f"wizard .auth entry; using {login!r} as geneweb_login",
                    file=sys.stderr,
                )

        if is_friend and frd_e is None and (frd_by_pk or frd_by_login):
            print(
                f"warning: friend {login!r} (person {pk!r}) not found in friend .auth",
                file=sys.stderr,
            )

        consent = all(
            norm(col(row, c)) == "oui"
            for c in ("consent_keep", "consent_access", "consent_cgu")
        )
        enabled = norm(col(row, "deceased")) != "oui" and consent

        attrs = {"geneweb_login": [geneweb_login]}
        if pk:
            attrs["geneweb_person_key"] = [pk]

        accounts.append(
            {
                "row": row,
                "login": login,
                "email": col(row, "email"),
                "password": password,
                "roles": roles,
                "enabled": enabled,
                "first": col(row, "prenom"),
                "last": col(row, "nom"),
                "attrs": attrs,
            }
        )

    for key in set(wiz_by_pk) - wiz_used:
        e = wiz_by_pk[key]
        print(
            f"warning: wizard .auth {e['login']!r} (person {e['person_key']!r}) "
            "has no matching CSV row",
            file=sys.stderr,
        )
    for key in set(frd_by_pk) - frd_used:
        e = frd_by_pk[key]
        print(
            f"warning: friend .auth {e['login']!r} (person {e['person_key']!r}) "
            "has no matching CSV row",
            file=sys.stderr,
        )

    _assign_usernames(accounts, args.rename_log)
    _fill_attributes(accounts, cols)
    _dedup_emails(accounts)

    with open(args.template, encoding="utf-8") as fh:
        realm = json.load(fh)
    realm["users"] = [_to_kc(a) for a in accounts]

    text = json.dumps(realm, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


def _assign_usernames(accounts, rename_log):
    # Keycloak lowercases usernames and enforces case-insensitive uniqueness
    # (the JPA store has no case-sensitive mode), so dedup on the lowercased
    # login: two logins differing only by case would collide on import. The
    # first keeps its verbatim login; later collisions get a numeric suffix.
    # geneweb_login is a separate claim kept in exact case, so GeneWeb still
    # gets the case-sensitive login.
    logins = {a["login"].lower() for a in accounts}
    used = set()
    renamed = []
    for a in accounts:
        base = a["login"]
        name = base
        if name.lower() in used:
            i = 2
            while f"{base}{i}".lower() in used or f"{base}{i}".lower() in logins:
                i += 1
            name = f"{base}{i}"
            renamed.append((a["email"], base, name))
        used.add(name.lower())
        a["username"] = name
    if rename_log and renamed:
        with open(rename_log, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["email", "previous_login", "new_login"])
            w.writerows(renamed)
    for email, prev, new in renamed:
        print(f"note: AMI login {prev!r} -> {new!r} ({email})", file=sys.stderr)


def _fill_attributes(accounts, cols):
    # keep every column as an attribute, except passwords and the columns
    # already mapped to reserved Keycloak fields (username/email/first/last).
    skip = {cols.get("nom"), cols.get("prenom"), cols.get("email"), cols.get("ami_id")}
    for a in accounts:
        for header, value in a["row"].items():
            if header is None or header in skip or not (value or "").strip():
                continue
            if "mot de passe" in norm(header):
                continue
            a["attrs"].setdefault(attr_key(header), [value.strip()])


def _dedup_emails(accounts):
    seen = {}
    for a in accounts:
        e = a["email"]
        if not e:
            continue
        if e.lower() in seen:
            print(
                f"warning: duplicate email {e!r} ({seen[e.lower()]!r} and "
                f"{a['username']!r}); dropping it from the second account",
                file=sys.stderr,
            )
            a["email"] = ""
        else:
            seen[e.lower()] = a["username"]


def _to_kc(a):
    u = {
        "username": a["username"],
        "enabled": a["enabled"],
        "firstName": a["first"],
        "lastName": a["last"],
        "realmRoles": a["roles"],
        "attributes": a["attrs"],
    }
    # Only set a password credential when we actually have one: an empty value
    # makes Keycloak's realm import fail with `argument "content" is null` and
    # rolls back every user. Passwordless accounts (wizards, blank AMI passwords)
    # are imported and get a password via reset.
    pw = (a["password"] or "").strip()
    if pw:
        u["credentials"] = [{"type": "password", "value": pw, "temporary": False}]
    if a["email"]:
        u["email"] = a["email"]
        u["emailVerified"] = True
    return u


if __name__ == "__main__":
    main()
