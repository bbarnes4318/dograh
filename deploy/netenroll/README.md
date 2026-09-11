# Serving Dograh at `aivoice.netenroll.com`

Runbook for putting this Dograh install on the `netenroll.com` registrable domain so
the portal at `https://agents.netenroll.com` can sign users into the `/voice-agents`
iframe again.

## Why

The portal sets `dograh_auth_token` (JWT) and `dograh_auth_user` (JSON) and relies on
the browser sending them into the Dograh iframe. That works only while both sides share
one registrable domain:

- A cookie set by `agents.netenroll.com` **cannot** be scoped to `aivoice.hopwhistle.com`
  (RFC 6265 domain-match), so the browser silently drops it.
- `SameSite=Lax` cookies are **not** sent into a cross-site frame.

Serving Dograh at `aivoice.netenroll.com` makes the cookie first-party again.

Two things that look like fixes but are not, and are out of scope here:

- **A redirect** from `aivoice.netenroll.com` to `aivoice.hopwhistle.com` — the browser
  ends up on `hopwhistle.com` and the cookie still does not apply. The app must be
  *served* on the netenroll hostname.
- **`SameSite=None`** — that makes them third-party cookies: blocked by Safari and
  Firefox, partitioned by Chrome.

## BLOCKER: DNS points at the wrong server

`aivoice.netenroll.com` already has an explicit A record, but it resolves to a
**different machine** than the one serving the portal:

| Hostname                 | Resolves to      | Notes                          |
| ------------------------ | ---------------- | ------------------------------ |
| `aivoice.hopwhistle.com` | `178.156.223.97` | current Dograh box             |
| `agents.netenroll.com`   | `178.156.223.97` | same box — portal              |
| `netenroll.com`          | `178.156.198.66` | different box                  |
| `aivoice.netenroll.com`  | `178.156.198.66` | **wrong box** — must be `.97`  |

This is not a wildcard artifact: a random `*.netenroll.com` label returns NXDOMAIN, so
the record is explicit and deliberate.

**Nothing below will work until this A record is repointed to `178.156.223.97`.**
Certbot's HTTP-01 challenge validates against whatever the name resolves to, so
issuance fails while it points at `.66`. Repoint it, let TTL expire, and confirm:

```bash
dig +short aivoice.netenroll.com    # must print 178.156.223.97
```

## How this box actually serves Dograh (confirmed on the box)

Settled by `docker compose ps` + `ss -lntp` on hopwhistle-prod-ash:

- **Host nginx owns :80 and :443** (real `nginx` workers, not `docker-proxy`) and
  reverse-proxies the published container ports. Dograh's own `remote` compose
  profile is NOT in use — there is no `nginx_https` or `coturn` container.
- The live vhost is **`/etc/nginx/sites-available/aivoice`** (not a filename
  matching the hostname, which is why a `sites-available/aivoice.hopwhistle.com`
  search came up empty).
- **Ports are remapped from the upstream compose defaults.** MinIO is on
  **19000**, Postgres 15432, Redis 16379. Only api (8000) and ui (3010) match
  the stock file. `/opt/dograh/docker-compose.override.yaml` plus a customized
  `docker-compose.yaml` drive this.
- The API image is upstream `dograhai/dograh-api:latest` with **13 bind-mounted
  patch files** from `/opt/dograh-patches/` layered over it. The UI is a
  locally built `dograh-ui:voicestudio`, from the `hopwhistle/customizations`
  branch — not from `main`.

The vhost in this directory is derived from that live `aivoice` vhost: only the
hostname, certificate, framing policy and ACME location differ. Every proxy rule
is copied verbatim from the config already serving this app.

## What the earlier draft got wrong

The draft at `/etc/nginx/sites-available/aivoice.netenroll.com` on the box was
written from container ports rather than from the working vhost. Do not enable
it as-is:

1. **`location /api` must be `location /api/v1/`.** This breaks the exact thing
   the vhost exists to fix. Next.js serves `/api/auth/oss`, `/api/auth/session`,
   `/api/auth/logout` and `/api/config/*` itself on :3010 — the routes that read
   `dograh_auth_token` and hand the session to the browser. Sending all of
   `/api` to FastAPI 404s them and the iframe falls back to Dograh's login
   screen. The live `aivoice` vhost gets this right.
2. **No `/voice-audio/` location** — recording playback 404s.
3. **MinIO is on 19000 on this box, not 9000.** An earlier version of this file
   had 9000 copied from the stock compose; that would have broken recordings.
4. **No `sub_filter`** for stray `localhost:9000` MinIO URLs.

## Verified

This vhost was run against nginx with stub upstreams on this box's real port
layout (8000 / 3010 / 19000) and a self-signed certificate. Confirmed:
`nginx -t` clean; `/`, `/voice-agents`, `/api/auth/oss`, `/api/auth/session`,
`/api/config/version` route to the **UI**, `/api/v1/health` to the **API**,
`/voice-audio/...` to **MinIO**; the `frame-ancestors` header present on 200 and
on 502; and on :80 the ACME challenge served from the webroot while every other
path 301s to HTTPS.

## Step 1 — DNS

Repoint `aivoice.netenroll.com` to `178.156.223.97` and confirm with `dig` as above.

## Step 2 — install the vhost (architecture A)

```bash
install -m 0644 /opt/dograh/deploy/netenroll/aivoice.netenroll.com.conf \
  /etc/nginx/sites-available/aivoice.netenroll.com
ln -sf /etc/nginx/sites-available/aivoice.netenroll.com \
  /etc/nginx/sites-enabled/aivoice.netenroll.com
mkdir -p /var/www/hopwhistle
```

The ACME webroot is `/var/www/hopwhistle` — the same one the portal's vhosts
already use on this box.

The vhost references a certificate that does not exist yet, so `nginx -t` fails until
Step 3. Issue the certificate first with `--nginx`, or temporarily comment out the
`listen 443` server, reload, issue via `--webroot`, then restore it.

## Step 3 — certificate

```bash
certbot certonly --webroot -w /var/www/hopwhistle -d aivoice.netenroll.com
nginx -t && systemctl reload nginx
```

`aivoice.hopwhistle.com` keeps its own certificate and vhost; nothing here touches it.

## Step 4 — Dograh's own configuration

All of this lives in **`/opt/dograh/.env`**. Dograh derives the per-subsystem URLs from
two canonical keys (`api/constants.py`):

```ini
PUBLIC_HOST=aivoice.netenroll.com
PUBLIC_BASE_URL=https://aivoice.netenroll.com
```

`BACKEND_API_ENDPOINT`, `MINIO_PUBLIC_ENDPOINT` and `TURN_HOST` each derive from these
when unset. If a previous install pinned any of them to the hopwhistle host, delete the
keys so they re-derive — `scripts/setup_custom_domain.sh` does exactly this:

```bash
# in /opt/dograh/.env — remove if present
BACKEND_API_ENDPOINT=
MINIO_PUBLIC_ENDPOINT=
TURN_HOST=
BACKEND_URL=
```

Restart the stack so the API picks up the new environment:

```bash
cd /opt/dograh && docker compose up -d --force-recreate api ui
```

**Do not use `./remote_up.sh` on this box.** It runs `up -d --pull always`,
which would replace the locally built `dograh-ui:voicestudio` image with
upstream's `dograhai/dograh-ui:latest` and take the Fish Voice Studio UI with
it. It also starts the `remote` profile (nginx + coturn containers), and that
nginx would fight host nginx for :80/:443.

### CORS

**Nothing to add.** This install runs `DEPLOYMENT_MODE=oss` (the default), and in OSS
mode `api/app.py` sets `allow_origins=["*"]` with `allow_credentials=False` and ignores
`CORS_ALLOWED_ORIGINS` entirely. That variable only takes effect when
`DEPLOYMENT_MODE != "oss"`, where it becomes a required explicit allowlist.

CORS is also not what governs this scenario. The iframe *navigates* to
`aivoice.netenroll.com`; the page inside it then makes **same-origin** requests to its
own backend. No cross-origin request is involved, so no CORS header would help.

### Permitted embedding origins

**Dograh sets no framing headers at all** — there is no `X-Frame-Options` and no
`Content-Security-Policy` anywhere in `api/`, `ui/`, or the nginx templates. So the
iframe is permitted by default, and the `frame-ancestors` directive in the vhost is a
*tightening* (restricting embedding to the portal and Dograh itself) rather than
something required to make embedding work. It is the only place embedding is governed;
there is no application-level setting for it.

### Cookie domain

Confirmed: **nothing in Dograh pins a cookie domain.** Dograh only ever *reads*
`dograh_auth_token`. The single place it writes cookies is `ui/src/app/api/auth/logout/route.ts`,
which sets `path: '/'` with no `domain` attribute. A cookie the portal issues with
`Domain=.netenroll.com` is accepted unchanged, and `OSS_JWT_SECRET`, the HS256 payload
shape in `api/utils/auth.py`, and the shared-workspace user id are all untouched by this
change.

## Verification

```bash
# served directly over TLS, not a redirect — expect 200, not 301/302
curl -sI https://aivoice.netenroll.com/ | head -1

# framing policy present
curl -sI https://aivoice.netenroll.com/ | grep -i content-security-policy

# the UI's own auth route must be answered by Next.js, not the backend.
# 401 = correct (no cookie sent). 404 = /api is misrouted to FastAPI.
curl -s -o /dev/null -w '%{http_code}\n' https://aivoice.netenroll.com/api/auth/oss

# backend reachable under its real prefix
curl -s https://aivoice.netenroll.com/api/v1/health | head -c 200

# old hostname still up
curl -sI https://aivoice.hopwhistle.com/ | head -1
```

Then load `/voice-agents` in the portal and confirm the iframe shows the user's calls
rather than Dograh's login form.

## What was actually tested

The vhost in this directory was run against nginx 1.24 with stub upstreams standing in
for the UI, API and MinIO, and a self-signed certificate. Confirmed:

- `nginx -t` passes with no warnings.
- `/`, `/voice-agents`, `/api/auth/oss`, `/api/auth/session`, `/api/config/version` all
  route to the **UI**; `/api/v1/health` routes to the **API**; `/voice-audio/...` routes
  to **MinIO**. This is the correction that matters most — see item 1 above.
- `Content-Security-Policy: frame-ancestors 'self' https://agents.netenroll.com` is
  present on a 200 **and** on a 502, confirming `always` covers error responses.
- On port 80, `/.well-known/acme-challenge/<token>` is served from the webroot while
  every other path 301s to HTTPS — so issuance and renewal both work.

Not tested, because it needs the real box: TLS with the real certificate, WebSocket
upgrade against real Dograh, and end-to-end cookie flow from the portal.

## Unrelated exposure worth noting

`docker-compose.yaml` publishes `5432` (Postgres), `6379` (Redis), `8000` (api) and
`3010` (ui) on **all** interfaces, not loopback. Only MinIO is bound to `127.0.0.1`.
On a public-IP box that means the database and the API are reachable from the internet,
bypassing nginx and TLS. Pre-existing and not caused by this change, but worth a
firewall rule or a `127.0.0.1:` prefix on those port mappings.
