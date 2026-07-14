#!/usr/bin/env python3
"""Build a Keycloak realm-import JSON from an AssoConnect CSV export.

The CSV is authoritative (one row per person). The GeneWeb wizard ``.auth``
file(s) are joined on the person key to recover the wizard *login* (absent from
the CSV) used as ``geneweb_login`` for wizards, so their wiznotes /
``superwizard`` / ``manitou`` keep working. Both the wizard and friend ``.auth``
files are also cross-checked against the CSV: every field present on both sides
(wizard name; AMI login, password and person key) is compared and any
divergence -- or an entry present in one source but not the other -- is warned.

Per row -> one Keycloak account:
  * username = ``identifiant AMI`` kept verbatim, numeric-suffixed on collision;
  * login by email is also possible (email is set; realm allows email login);
  * password = ``mot de passe AMI`` (treated as valid for preprod; force a
    reset for the real rollout);
  * roles: friend if AMI access is on, plus geneweb-wizard if ``Statut dans la
    base`` is ``Magicien`` (wizards also have a friend account -> one merged
    account with both roles);
  * geneweb_person_key from the Roglo columns (Prénom / N° d'occurence /
    Patronyme) as ``first_name.occ surname``;
  * geneweb_login = wizard login (joined from .auth) for wizards, else the AMI
    username;
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
    d = {}
    for e in entries:
        pk = e["person_key"]
        if not pk:
            print(
                f"warning: {kind} .auth entry {e['login']!r} has no person key; "
                "cannot cross-check it",
                file=sys.stderr,
            )
            continue
        if pk in d and d[pk]["login"] != e["login"]:
            print(
                f"warning: {kind} .auth has two logins for person {pk!r}: "
                f"{d[pk]['login']!r} and {e['login']!r}",
                file=sys.stderr,
            )
        d[pk] = e
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
        if not ami:
            print(
                f"warning: contact {col(row, 'contact_id')!r} "
                f"({col(row, 'email')!r}) has no AMI login; skipped",
                file=sys.stderr,
            )
            continue

        is_wizard = norm(col(row, "statut")) == "magicien"
        is_friend = norm(col(row, "ami_active")) == "oui" or bool(ami)
        roles = []
        if is_wizard:
            roles.append("geneweb-wizard")
        if is_friend or is_wizard:
            roles.append("geneweb-friend")

        pk = person_key(
            col(row, "roglo_firstname"), col(row, "occ"), col(row, "roglo_surname")
        )
        if is_wizard and not pk:
            print(f"warning: wizard {ami!r} has no person key in the CSV", file=sys.stderr)

        geneweb_login = ami
        if is_wizard:
            e = wiz_by_pk.get(pk)
            if e is None:
                print(
                    f"warning: wizard {ami!r} (person {pk!r}) has no matching "
                    "wizard .auth entry; using AMI login as geneweb_login",
                    file=sys.stderr,
                )
            else:
                wiz_used.add(pk)
                geneweb_login = e["login"]
                nm = col(row, "nom_magicien")
                if nm and e["name"] and nm != e["name"]:
                    print(
                        f"warning: wizard name differs for {pk!r}: CSV {nm!r} "
                        f"vs wizard .auth {e['name']!r}",
                        file=sys.stderr,
                    )

        if is_friend and (frd_by_pk or frd_by_login):
            e = frd_by_pk.get(pk)
            if e is not None:
                frd_used.add(e["person_key"])
            else:
                cands = frd_by_login.get(ami, [])
                if cands:
                    e = cands[0]
                    frd_used.add(e["person_key"])
                    print(
                        f"warning: person key differs for AMI login {ami!r}: CSV "
                        f"{pk!r} vs friend .auth {e['person_key']!r}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"warning: friend {ami!r} (person {pk!r}) not found in "
                        "friend .auth",
                        file=sys.stderr,
                    )
            if e is not None:
                if e["login"] != ami:
                    print(
                        f"warning: AMI login differs for person {pk!r}: CSV "
                        f"{ami!r} vs friend .auth {e['login']!r}",
                        file=sys.stderr,
                    )
                if e["password"] != col(row, "ami_pw"):
                    print(
                        f"warning: AMI password differs for {ami!r} (person {pk!r})",
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
                "ami": ami,
                "email": col(row, "email"),
                "password": col(row, "ami_pw"),
                "roles": roles,
                "enabled": enabled,
                "first": col(row, "prenom"),
                "last": col(row, "nom"),
                "attrs": attrs,
            }
        )

    for pk in set(wiz_by_pk) - wiz_used:
        print(
            f"warning: wizard .auth {wiz_by_pk[pk]['login']!r} (person {pk!r}) "
            "has no matching CSV row",
            file=sys.stderr,
        )
    for pk in set(frd_by_pk) - frd_used:
        print(
            f"warning: friend .auth {frd_by_pk[pk]['login']!r} (person {pk!r}) "
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
    logins = {a["ami"] for a in accounts}
    used = set()
    renamed = []
    for a in accounts:
        base = a["ami"]
        name = base
        if name in used:
            i = 2
            while f"{base}{i}" in used or f"{base}{i}" in logins:
                i += 1
            name = f"{base}{i}"
            renamed.append((a["email"], base, name))
        used.add(name)
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
        "credentials": [
            {"type": "password", "value": a["password"], "temporary": False}
        ],
        "realmRoles": a["roles"],
        "attributes": a["attrs"],
    }
    if a["email"]:
        u["email"] = a["email"]
        u["emailVerified"] = True
    return u


if __name__ == "__main__":
    main()
