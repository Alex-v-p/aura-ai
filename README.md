# Aura AI

## Start the development stack

Copy `.env.example` to `.env` and create the referenced Core and Authentik
secret files. For a disposable, loopback-only Docker Desktop login, run:

```sh
./deploy/compose/local-http.sh up
```

Open Aura at <http://aura.localhost:4200> and Observatory at
<http://observatory.localhost:4300>. Sign in with the generated `aura-owner`
account; its password is stored in `.secrets/authentik-owner-password`.
Authentik is available at <http://authentik.localhost:9000>.

The stack reserves `AURA_FRONTEND_SUBNET=10.254.241.0/24` for its browser-facing
network so Docker does not capture routes to a LAN Ollama provider. Override
that value in `.env` if the range conflicts with a trusted local network.

If Docker Desktop cannot route to a remote LAN Ollama host, set
`AURA_LOCAL_OLLAMA_RELAY_TARGET=192.168.0.40:11434` in `.env`. The launcher then
manages a transient user service bound only to `127.0.0.1:11435` and points Core
at the loopback relay; `down` and `reset` stop it. Leave the target empty when
direct routing works. The relay requires `systemd-run`, `systemctl`, and
`socat` on the host.

This identity and its HTTP issuer are development-only and disposable. Stop
the stack with:

```sh
./deploy/compose/local-http.sh down
```

`down` preserves the disposable Docker-managed volumes. To intentionally
discard the local Core, Authentik, and Observatory state and start fresh, run:

```sh
./deploy/compose/local-http.sh reset
./deploy/compose/local-http.sh up
```

For an external Authentik instance with stable HTTPS configuration, start only
the normal stack:

```sh
docker compose up --build --detach --wait
```

Detailed deployment and verification instructions are in
[`docs/README.md`](docs/README.md).
