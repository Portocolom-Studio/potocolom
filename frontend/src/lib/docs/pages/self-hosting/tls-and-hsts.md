---
title: TLS and HSTS
subtitle: Serve the studio over HTTPS, and know how the HSTS header is sent.
tab: self-hosting
group: Run it
order: 8
source: docs/self-hosting.md "TLS and HSTS"
---

TLS (Transport Layer Security) encrypts the connection between a browser and the server. The API does not terminate TLS itself. For any install that other people reach over the internet, put an HTTPS reverse proxy or load balancer in front of the API.

HSTS (HTTP Strict Transport Security) is a response header. It tells a browser to use HTTPS for this host from then on.

## HSTS has no switch

The API sends the header on every HTTP response:

```text
Strict-Transport-Security: max-age=31536000
```

The source defines no setting that turns HSTS on or off. The header comes from [backend/app/security.py](https://github.com/Portocolom-Studio/potocolom/blob/main/backend/app/security.py), and the static site headers in [frontend/static/_headers](https://github.com/Portocolom-Studio/potocolom/blob/main/frontend/static/_headers) use the same value. HSTS takes effect for a browser once the studio is served over HTTPS.

The header does not include `includeSubDomains` or `preload`. Self-hosted operators may serve unrelated subdomains from the same host, and this header does not pin those subdomains to HTTPS.

> [!WARNING]
> Browsers honor HSTS only over HTTPS. The header does not provide TLS, and it does not upgrade a first connection that uses plain HTTP. Without TLS in front of the API, browsers never see a secure context, and HSTS has no effect.

## Local and LAN installs

The shipped localhost and LAN compose profile serves plain HTTP for development, at `http://localhost:8080`. This is intentional.

For anything beyond a trusted LAN, put an HTTPS reverse proxy in front of the API.

## Public deployments

Any public deployment must do all of these before it is exposed:

1. Terminate TLS in front of the API, with a reverse proxy, a load balancer, or similar.
2. Redirect HTTP to HTTPS.
3. Restrict direct access to port 8080, so clients reach the API only through the proxy. The Compose file publishes `8080:8080` on every interface, and the API serves plain HTTP on that port. A client that reaches port 8080 skips the proxy, including for sign-in. If the proxy runs on the same host, publish the port on loopback by changing the line to `"127.0.0.1:8080:8080"` in `deploy/compose/compose.yml`. Otherwise, restrict port 8080 with Docker-aware network rules. Host firewall rules may not filter Docker-published ports, depending on the firewall and Docker configuration.

Then set the public address to the `https://` origin that browsers use. The install reads this value from `POTOCOLOM_PUBLIC_URL` at install time, or from `PUBLIC_URL` in `deploy/compose/.env`. It must match the origin that the browser sends exactly: lowercase scheme and host, the port only when it is not the default, and no path. See [Reaching the studio from other machines](/docs/self-hosting/reaching-the-studio) for how to apply the change.

Remote GPU workers need the same HTTPS setup. Configure the proxy before you attach a worker to an API host. See [An API host with remote GPU workers](/docs/self-hosting/remote-gpu-workers).

## Settings

| Name                   | Where                 | Value                                                               |
| ---------------------- | --------------------- | ------------------------------------------------------------------- |
| `POTOCOLOM_PUBLIC_URL` | Install time          | The public origin. The installer stores it as `PUBLIC_URL`.         |
| `PUBLIC_URL`           | `deploy/compose/.env` | The origin that browsers use, such as `https://studio.example.com`. |

The source names no setting for TLS or HSTS. The proxy is the only place where TLS is configured.
