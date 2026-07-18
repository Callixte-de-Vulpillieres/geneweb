#!/usr/bin/env python3
"""Write the roglo realm's declarative user-profile config
(``attributes."kc.user.profile.config"``) into a realm template JSON, so the
imported attributes render with proper input types / validations / permissions
and grouping. Idempotent -- rerun to update.

Usage:
  python3 oidc-e2e/set_user_profile.py roglo-preprod-realm.template.json
"""

import json
import sys

BOTH = {"view": ["admin", "user"], "edit": ["admin", "user"]}
ADMIN = {"view": ["admin"], "edit": ["admin"]}
READONLY = {"view": ["admin", "user"], "edit": ["admin"]}  # user sees, admin edits


def a(name, label, perms, group=None, input_type=None, options=None, maxlen=255):
    d = {"name": name, "displayName": label, "permissions": perms, "multivalued": False}
    val = {}
    if options:
        val["options"] = {"options": options}
    if maxlen:
        val["length"] = {"max": maxlen}
    if val:
        d["validations"] = val
    if input_type:
        d["annotations"] = {"inputType": input_type}
    if group:
        d["group"] = group
    return d


PROFILE = {
    "attributes": [
        {"name": "username", "displayName": "${username}",
         "validations": {"length": {"min": 1, "max": 255}},
         "permissions": BOTH, "multivalued": False},
        {"name": "email", "displayName": "${email}",
         "validations": {"email": {}, "length": {"max": 255}},
         "permissions": BOTH, "multivalued": False},
        {"name": "firstName", "displayName": "${firstName}",
         "validations": {"length": {"max": 255}}, "permissions": BOTH},
        {"name": "lastName", "displayName": "${lastName}",
         "validations": {"length": {"max": 255}}, "permissions": BOTH},
        # contact
        a("phone_mobile", "Téléphone mobile", BOTH, "contact", "html5-tel", maxlen=30),
        a("phone_landline", "Téléphone fixe", BOTH, "contact", "html5-tel", maxlen=30),
        a("gender", "Sexe", BOTH, "contact", "select",
          options=["Masculin", "Féminin"], maxlen=None),
        a("address", "Adresse", BOTH, "contact", maxlen=255),
        a("address_complement", "Complément d'adresse", BOTH, "contact", maxlen=255),
        a("postal_code", "Code postal", BOTH, "contact", maxlen=20),
        a("city", "Ville", BOTH, "contact", maxlen=120),
        a("region", "Région", BOTH, "contact", maxlen=120),
        a("department", "Département", BOTH, "contact", maxlen=120),
        a("country", "Pays", BOTH, "contact", maxlen=120),
        a("website", "Site internet", BOTH, "contact", "html5-url", maxlen=255),
        a("birth_date", "Date de naissance", BOTH, "contact", "html5-date", maxlen=10),
        a("birth_place", "Lieu de naissance", BOTH, "contact", maxlen=255),
        # roglo membership / misc
        a("parrain", "Parrain / Magicien de référence", BOTH, "roglo", maxlen=255),
        a("annuaire_amis", "Annuaire des Amis", BOTH, "roglo", "select",
          options=["true", "false"], maxlen=None),
        a("genealogy_interests", "Centres d'intérêt généalogiques", BOTH, "roglo",
          "textarea", maxlen=4000),
        a("comments", "Commentaires", ADMIN, "roglo", "textarea", maxlen=4000),
        a("date_admission_ami", "Date d'admission ami", READONLY, "roglo",
          "html5-date", maxlen=10),
        a("date_admission_magicien", "Date d'admission magicien", READONLY, "roglo",
          "html5-date", maxlen=10),
        # internal identity keys -- admin only, never user-editable
        a("geneweb_login", "GeneWeb login", ADMIN, "roglo", maxlen=255),
        a("geneweb_person_key", "GeneWeb person key", ADMIN, "roglo", maxlen=255),
        a("assoconnect_contact_id", "AssoConnect contact id", ADMIN, "roglo", maxlen=64),
        a("assoconnect_app_id", "AssoConnect app id", ADMIN, "roglo", maxlen=64),
    ],
    "groups": [
        {"name": "contact", "displayHeader": "Coordonnées"},
        {"name": "roglo", "displayHeader": "Roglo"},
    ],
    "unmanagedAttributePolicy": "ENABLED",
}


def main():
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <realm-template.json>")
    path = sys.argv[1]
    realm = json.load(open(path, encoding="utf-8"))
    realm.setdefault("attributes", {})["kc.user.profile.config"] = json.dumps(
        PROFILE, ensure_ascii=False
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(realm, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"updated {path}: {len(PROFILE['attributes'])} profile attributes")


if __name__ == "__main__":
    main()
