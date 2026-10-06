# Aura AI

## Start the development stack

Copy `.env.example` to `.env` and create the referenced Core and Authentik
secret files. For a disposable, loopback-only Docker Desktop login, run:

```sh
./deploy/compose/local-http.sh up
```

Open Aura at <http://aura.localhost:4200>. Sign in with the generated
`aura-owner` account; its password is stored in
`.secrets/authentik-owner-password`. Authentik is available at
<http://authentik.localhost:9000>.

This identity and its HTTP issuer are development-only and disposable. Stop
the stack with:

```sh
./deploy/compose/local-http.sh down
```

For an external Authentik instance with stable HTTPS configuration, start only
the normal stack:

```sh
docker compose up --build --detach --wait
```

Detailed deployment and verification instructions are in
[`docs/README.md`](docs/README.md).
