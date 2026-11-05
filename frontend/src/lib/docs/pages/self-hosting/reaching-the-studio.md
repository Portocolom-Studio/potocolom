---
title: Reaching the studio from other machines
subtitle: Open the studio from a phone or laptop on your network.
tab: self-hosting
group: Install
order: 5
source: docs/self-hosting.md "Reaching the studio from other machines", "TLS and HSTS", "Accounts" ("First run"); deploy/compose/compose.yml
---

The compose file publishes port 8080 without an address, so Docker listens on every host interface. Another machine on your network can reach the studio at the address of the host. Before you open it up, set the public address, so that the browser and the API agree on the origin.

## Set the public address

`PUBLIC_URL` must match the origin that the browser sends, exactly. An origin is the scheme, the host and the port. Write it in this form:

- Lowercase scheme and host.
- The port only when it is not the default. Leave out `:80` on http and `:443` on https.
- No path.

When your host has an IPv6 address, the installer asks for a hostname instead of the literal address.

Set the address during the install. Replace `<origin>` with the address that other machines will use, for example `http://<host-address>:8080` on a LAN:

```bash
curl -fsSL https://github.com/Portocolom-Studio/potocolom/releases/latest/download/install.sh | POTOCOLOM_PUBLIC_URL=<origin> bash
```

To change the address on an install that already runs, set `PUBLIC_URL` in `deploy/compose/.env`. Then apply it from the install directory. Use `rocm` in place of `gpu` on an AMD host, and `api` on a host without a local worker:

```bash
env -u PUBLIC_URL docker compose --env-file deploy/compose/.env -f deploy/compose/compose.yml --profile gpu up -d
```

The `env -u PUBLIC_URL` part removes a `PUBLIC_URL` that is set in your shell. Without it, the shell value would replace the value in the env file.

## Open the studio

On the other machine, open the same origin in a browser. If sign-in fails, compare `PUBLIC_URL` with the address in the browser bar first. When the two differ, sign-in is refused.

## Security before you expose the studio

> [!WARNING]
> A new install stays unclaimed until someone spends the setup link and chooses. Keep that link private until it is spent. If the install has no accounts, every request resolves to one local user, and that user is an administrator. Anyone who can reach the API can do everything the install can do. Open the studio only on a network you trust, or choose accounts first. See [First run](/docs/self-hosting/first-run) and [Accounts](/docs/self-hosting/accounts).

The shipped compose profile serves plain HTTP. That suits a trusted home or office network. For anything beyond that, put an HTTPS reverse proxy in front of the API, and redirect HTTP to HTTPS before the service is exposed. Browsers honour the HSTS header only over HTTPS, so without TLS the header does nothing. See [TLS and HSTS](/docs/self-hosting/tls-and-hsts).
