# Home Payouts: its own AI Voice workspace at `aivoice.homepayouts.com`

The Home Payouts portal (a real estate workspace in Hoppwhistle) embeds this
app at `/re/voice-agents`. It gets:

- **Its own agents.** A separate user and organization in this app, so its
  workflows, campaigns, calls and phone numbers are its own.
- **The same settings and rules** as the existing workspace, copied once by
  `scripts/clone_workspace.sql`. That covers AI models and voices, defaults,
  telephony accounts, integrations and credentials.
- **Its own look.** The portal loads the frame with `?brand=home-payouts`.
  The app then swaps to the Home Payouts logo, favicon, title and green and
  navy palette. See `ui/src/lib/portalBrand.ts` and the
  `[data-brand='home-payouts']` block in `ui/src/app/globals.css`.

The portal's sign-in cookie only reaches the frame if both are on one
registrable domain, so the app must be served at `aivoice.homepayouts.com`.
The netenroll runbook (`deploy/netenroll/README.md`) explains why. Everything
there applies here with the names changed.

## 1. DNS

Add an A record `aivoice.homepayouts.com` pointing to `178.156.223.97`, the
box that serves this app. Then check it:

```bash
dig +short aivoice.homepayouts.com    # 178.156.223.97
```

## 2. vhost and certificate (on the box)

```bash
install -m 0644 /opt/dograh/deploy/homepayouts/aivoice.homepayouts.com.conf /etc/nginx/sites-available/aivoice.homepayouts.com
certbot certonly --webroot -w /var/www/hopwhistle -d aivoice.homepayouts.com
ln -sf /etc/nginx/sites-available/aivoice.homepayouts.com /etc/nginx/sites-enabled/aivoice.homepayouts.com
nginx -t && systemctl reload nginx
```

If certbot can't reach the webroot because no vhost serves the name yet, run
`certbot certonly --nginx -d aivoice.homepayouts.com` instead.

`frame-ancestors` in the vhost allows only the Home Payouts portal hosts. Add a
host there if the portal is served on another name.

## 3. Rebuild the UI with the brand

The skin is in the UI image. Rebuild `ui` from this branch the same way the
running image is built, then recreate it:

```bash
cd /opt/dograh && docker compose build ui && docker compose up -d --force-recreate ui
```

Don't use `./remote_up.sh` on this box. It pulls upstream images.

## 4. Create the workspace (copies the settings)

```bash
cat /opt/dograh/scripts/clone_workspace.sql | docker exec -i <postgres container> psql -U <user> -d <db> -v ON_ERROR_STOP=1 -v source_org=1 -v source_user=1 -v provider_id=hoppwhistle-home-payouts -v email=voice@homepayouts.com
```

`source_org` and `source_user` are the existing workspace (the one the
insurance portal uses). The script prints `new_user_id`; you need it in step 5.
Running it a second time changes nothing.

## 5. Point Hoppwhistle at it

```bash
node dist/cli/re-workspace.js voice --tenant <home payouts slug> --dograh-user <new_user_id> --url https://aivoice.homepayouts.com --cookie-domain .homepayouts.com
```

Inside the Hoppwhistle API container, run it as
`docker exec hopwhistle-api node dist/cli/re-workspace.js ...`.

After that, **AI Voice** in the Home Payouts sidebar opens this app, already
signed in to the workspace's own account.
