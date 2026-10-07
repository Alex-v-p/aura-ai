"""Create the disposable local Aura owner and emit its OIDC subject.

This file is executed by Authentik's supported ``ak shell`` command, so the
Authentik Django application is already configured.  It deliberately emits
only the subject line consumed by local-http.sh; credentials never appear in
stdout or logs.
"""

from __future__ import annotations

import os
from pathlib import Path

from authentik.core.models import User
from authentik.common.oauth.constants import SubModes
from authentik.providers.oauth2.models import OAuth2Provider


def required_text(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value or any(character in value for character in "\r\n"):
        raise SystemExit(f"{name} must be a non-empty single-line value")
    return value


def main() -> None:
    username = required_text("AUTHENTIK_OWNER_USERNAME", "aura-owner")
    display_name = required_text("AUTHENTIK_OWNER_NAME", "Aura Owner")
    password_file = os.environ.get(
        "AUTHENTIK_OWNER_PASSWORD_FILE", "/run/secrets/authentik-owner-password"
    )
    try:
        password = Path(password_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit("the local owner password secret is unavailable") from exc
    if not password:
        raise SystemExit("the local owner password secret is empty")

    users = User.objects.filter(username=username)
    if users.count() > 1:
        raise SystemExit("multiple Authentik users match the configured owner username")
    user = users.first()
    if user is None:
        user = User(username=username, name=display_name, is_active=True)
        user.set_password(password)
        user.save()
    else:
        # Reconcile only safe identity attributes.  Never rotate an existing
        # password during repeated startup.
        if not user.check_password(password):
            raise SystemExit("the existing Aura owner password does not match the configured secret")
        if user.is_staff or user.is_superuser:
            raise SystemExit("the existing Aura owner must be a normal non-staff user")
        changed = []
        if user.name != display_name:
            user.name = display_name
            changed.append("name")
        if not user.is_active:
            user.is_active = True
            changed.append("is_active")
        if changed:
            user.save(update_fields=changed)

    try:
        provider = OAuth2Provider.objects.get(name="Aura Web OIDC")
        if provider.sub_mode != SubModes.HASHED_USER_ID:
            raise SystemExit("Aura Web OIDC provider is not configured for hashed user subjects")
        subject = str(user.uid).strip()
    except OAuth2Provider.DoesNotExist as exc:
        raise SystemExit("Aura Web OIDC provider was not created by the blueprint") from exc
    if not subject or any(character in subject for character in "\r\n"):
        raise SystemExit("Authentik returned an invalid Aura owner subject")

    print(f"AURA_OWNER_SUBJECT={subject}")


main()
