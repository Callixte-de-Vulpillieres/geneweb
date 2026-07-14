#!/usr/bin/env python3
"""Build a Keycloak realm-import JSON from GeneWeb legacy auth files.

GeneWeb's wizard/friend password files (referenced by ``wizard_passwd_file`` /
``friend_passwd_file`` in a ``.gwf``) have one entry per line:

    username:password:name:key:info

GeneWeb keeps everything after the 2nd ``:`` as one info field, then reads:
  * ``name``  - display name; a ``/`` is a wiznotes sort separator, and older
                files may pack the person key here as ``name|first_name.occ surname``.
  * ``key``   - the person key as ``firstname/surname/occ`` (Roglo style).
  * ``info``  - free-form (e.g. a date); unused by GeneWeb.

This turns each entry into a Keycloak user so the same people can sign in over
OIDC. It loads an existing realm JSON as a template
(``oidc-e2e/keycloak-realm.json`` by default) and only replaces its ``users``
array, so the client, roles and protocol mappers stay in sync. Each user gets:
  * the legacy login as its Keycloak username, kept verbatim (case, spaces,
    special characters). Legacy auth lets different people share a login
    (disambiguated by password/person), which Keycloak forbids, so distinct
    people sharing a login are suffixed: philippe, philippe2, philippe3 ...
  * a password credential and the matching realm role (geneweb-wizard/-friend);
  * ``geneweb_login`` = the original login (no suffix), so a base uses it as
    identity (``oidc_user_claim=geneweb_login``) and preserves ``manitou`` /
    ``supervisor`` / ``superwizard`` matching;
  * ``geneweb_person_key`` = the key converted to ``first_name.occ surname``
    (resolved by GeneWeb's dot-key parser), when the entry carries one;
  * the display name, emitted as the ``name`` claim.

Example:
    ./realm_from_auth.py --wizard bases/mybase.wzd --friend bases/mybase.frd \\
        > my-realm.json
"""

import argparse
import json
import sys


def clean_display(name):
    """Drop the first '/' wiznotes sort separator, like gen_match_auth_file."""
    return name.replace("/", "", 1).strip()


def person_key_of_field(raw):
    """Convert a ``firstname/surname/occ`` key to ``first_name.occ surname``."""
    raw = raw.strip()
    if not raw:
        return ""
    parts = raw.split("/")
    if len(parts) == 1:
        # already "first_name.occ surname" (the name|key convention)
        return raw
    fn = parts[0].strip()
    occ = (parts[-1].strip() or "0") if len(parts) >= 3 else "0"
    sn = " ".join(p.strip() for p in parts[1:-1]) if len(parts) >= 3 else parts[1].strip()
    return f"{fn}.{occ} {sn}"


def parse_auth_file(path, role):
    """Parse a GeneWeb auth file, returning a list of user dicts."""
    users = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            # GeneWeb split: first ':' ends user, second ':' ends password.
            parts = line.split(":", 2)
            if len(parts) < 2 or not parts[0]:
                continue
            login, password = parts[0], parts[1]
            au_info = parts[2] if len(parts) == 3 else ""
            # au_info = name : key : info
            name, key = au_info, ""
            if ":" in au_info:
                name, rest = au_info.split(":", 1)
                key = rest.split(":", 1)[0]
            person_key = ""
            if "|" in name:  # legacy name|first_name.occ surname
                name, pk = name.split("|", 1)
                person_key = pk.strip()
            if key.strip():  # explicit firstname/surname/occ field wins
                person_key = person_key_of_field(key)
            users.append(
                {
                    "login": login,
                    "password": password,
                    "role": role,
                    "display": clean_display(name),
                    "person_key": person_key,
                }
            )
    return users


def merge_accounts(users):
    """Merge rows that are the same account -- same login, password and person
    key (the same person listed in both the wizard and friend files) -- unioning
    their roles. Different people who happen to share a login stay separate;
    legacy auth distinguishes them by password/person, not by the login alone."""
    by_id = {}
    order = []
    for u in users:
        key = (u["login"], u["password"], u["person_key"])
        if key not in by_id:
            by_id[key] = {**u, "roles": [u["role"]]}
            order.append(key)
        else:
            m = by_id[key]
            if u["role"] not in m["roles"]:
                m["roles"].append(u["role"])
            if not m["display"] and u["display"]:
                m["display"] = u["display"]
    return [by_id[k] for k in order]


def assign_usernames(accounts):
    """Set each account's Keycloak username to its login kept verbatim (case,
    spaces, special characters). When distinct accounts share a login, keep the
    first as-is and suffix the rest (philippe, philippe2, philippe3 ...),
    avoiding collisions with any other real login."""
    logins = {a["login"] for a in accounts}
    used = set()
    for a in accounts:
        base = a["login"]
        name = base
        if name in used:
            i = 2
            while f"{base}{i}" in used or f"{base}{i}" in logins:
                i += 1
            name = f"{base}{i}"
        used.add(name)
        a["username"] = name
    return accounts


def to_keycloak_user(u):
    # geneweb_login stays the original login (no suffix) for GeneWeb's identity;
    # only the Keycloak username may be suffixed to stay unique.
    attrs = {"geneweb_login": [u["login"]]}
    if u["person_key"]:
        attrs["geneweb_person_key"] = [u["person_key"]]
    return {
        "username": u["username"],
        "enabled": True,
        "firstName": u["display"] or u["login"],
        "lastName": "",
        "credentials": [
            {"type": "password", "value": u["password"], "temporary": False}
        ],
        "realmRoles": u["roles"],
        "attributes": attrs,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Generate a Keycloak realm JSON from GeneWeb auth files."
    )
    parser.add_argument(
        "--wizard", action="append", default=[], metavar="FILE",
        help="legacy auth file whose users become wizards (repeatable)",
    )
    parser.add_argument(
        "--friend", action="append", default=[], metavar="FILE",
        help="legacy auth file whose users become friends (repeatable)",
    )
    parser.add_argument(
        "--template", default="oidc-e2e/keycloak-realm.json", metavar="FILE",
        help="realm JSON to use as a base (default: oidc-e2e/keycloak-realm.json)",
    )
    parser.add_argument(
        "--out", metavar="FILE", help="output file (default: stdout)"
    )
    args = parser.parse_args()

    if not args.wizard and not args.friend:
        parser.error("provide at least one --wizard or --friend file")

    users = []
    for path in args.wizard:
        users += parse_auth_file(path, "geneweb-wizard")
    for path in args.friend:
        users += parse_auth_file(path, "geneweb-friend")

    if not users:
        print("warning: no users parsed from the given files", file=sys.stderr)

    users = merge_accounts(users)
    users = assign_usernames(users)
    for u in users:
        if u["username"] != u["login"]:
            print(
                f"note: login {u['login']!r} is shared; person "
                f"{u['person_key'] or '?'} gets username {u['username']!r}",
                file=sys.stderr,
            )
    kc_users = [to_keycloak_user(u) for u in users]

    with open(args.template, encoding="utf-8") as fh:
        realm = json.load(fh)
    realm["users"] = kc_users

    text = json.dumps(realm, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
