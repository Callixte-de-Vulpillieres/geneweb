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
  * a password credential and the matching realm role (geneweb-wizard/-friend);
  * ``geneweb_login`` = the exact legacy login, so a base can keep using it as
    its identity (``oidc_user_claim=geneweb_login``) and preserve ``manitou`` /
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
import re
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


def sanitize_username(login):
    return re.sub(r"\s+", ".", login.strip().lower())


def to_keycloak_user(u, email_domain):
    handle = sanitize_username(u["login"])
    attrs = {"geneweb_login": [u["login"]]}
    if u["person_key"]:
        attrs["geneweb_person_key"] = [u["person_key"]]
    return {
        "username": handle,
        "enabled": True,
        "email": f"{handle}@{email_domain}",
        "emailVerified": True,
        "firstName": u["display"] or u["login"],
        "lastName": "",
        "credentials": [
            {"type": "password", "value": u["password"], "temporary": False}
        ],
        "realmRoles": [u["role"]],
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
        "--email-domain", default="example.local", metavar="DOMAIN",
        help="domain for the synthesized user emails (default: example.local)",
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

    with open(args.template, encoding="utf-8") as fh:
        realm = json.load(fh)
    realm["users"] = [to_keycloak_user(u, args.email_domain) for u in users]

    text = json.dumps(realm, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
