# Relocating the temporary Authentik deployment

The `local-identity` Compose profile exists to bootstrap Aura development. It
is deliberately not an Aura application dependency: Core talks to the
configured OIDC discovery endpoint and never reads Authentik tables or calls
its private APIs. The profile is defined in the explicit
`deploy/compose/local-identity.yaml` override, so normal Aura Compose commands
do not interpolate or require Authentik inputs. Once a separately managed
Authentik instance is available, move the same provider rather than creating a
second Aura identity.

The initial local deployment must use stable HTTPS hostnames that are not
`localhost`, Compose service names, or container-only aliases. Configure
`AURA_OIDC_ISSUER` and `AUTHENTIK_HOST_BROWSER` to the same issuer host from
the first run, and set `AURA_PUBLIC_ORIGIN`/`AURA_OIDC_REDIRECT_URI` to the
stable Aura browser origin. Keep the OIDC audience and client ID stable too.
The identity override intentionally refuses to render without these values and
an operator-verified Authentik image; its startup guard rejects an image that
is not digest-qualified before Authentik starts.

## Before the move

1. Confirm that the external instance can serve the same stable issuer URL as
   `AURA_OIDC_ISSUER` (for example, through internal DNS or a reverse proxy).
2. Back up the local Authentik PostgreSQL database, `/media` data, imported
   blueprints, and the secret material used for the Authentik signing key and
   Aura client.
3. Record the Aura OIDC application/provider's client ID, client secret,
   redirect URI, signing configuration, and the configured owner subject. Do
   not put any of those values in this repository.
4. Stop new identity administration and take the database backup while the
   local profile is stopped or quiesced. Do not remove Core PostgreSQL or its
   encrypted state directory.

## Restore and switch

1. Restore the Authentik database and media into the separately managed
   instance, preserving the same issuer, signing material, client ID, client
   secret, and owner subject.
2. Configure the external Authentik application with the exact Aura callback:
   `https://<aura-public-host>/api/v1/auth/callback`.
3. Update only runtime deployment inputs (`AURA_OIDC_ISSUER`, audience/client
   values, redirect origin, and secret-file paths). Keep Core and browser API
   contracts unchanged.
4. Verify OIDC discovery and JWKS from the Aura/Core network, then log in and
   out through the BFF. Confirm that the returned `sub` is unchanged and that
   a non-owner subject still receives `403` from Core.
5. Start the normal Aura stack with only `compose.yaml` and run the smoke
   checks. Keep the encrypted local Authentik backup until the external
   deployment has passed the recovery test.
6. Remove the local identity profile only after the external login and logout
   path is healthy. Removing the profile must not remove Core data or require a
   Core migration.

## Rollback

Point `AURA_OIDC_ISSUER` and the other provider settings back to the local
instance, restore the local Authentik database/media if necessary, and start
the profile with both Compose files and the same secrets:

```sh
docker compose -f compose.yaml -f deploy/compose/local-identity.yaml \
  --profile local-identity up --build --detach --wait authentik-server authentik-worker
```

Do not relink a changed OIDC subject by email. If the issuer or subject cannot
be preserved, stop and plan an owner-approved identity migration instead.

## Required safeguards

- Keep Aura and Authentik PostgreSQL databases, roles, credentials, backups,
  and migration lifecycles separate.
- Keep OIDC tokens server-side in the Core session store; browser cookies are
  host-only, `HttpOnly`, `Secure`, and explicitly SameSite-configured.
- Keep all state directories on operator-supplied encrypted host storage.
- Never commit real endpoint names, client secrets, signing keys, passwords,
  certificates, or owner subjects.
