#!/usr/bin/env bash
# Home Payouts AI Voice, end to end, on the production box (178.156.223.97).
# Safe to re-run. Stops at the first failure with "STOPPED: <reason>".
#
#   1 deploy the portal (main)            4 rebuild the voice UI with the brand
#   2 fetch this branch to $SRC           5 create the Home Payouts voice account
#   3 nginx vhost + certificate           6 connect the portal workspace to it
#
# The live voice app's checkout and compose files in /opt/dograh are only read,
# never changed. Run on the box:
#   curl -fsSL https://raw.githubusercontent.com/bbarnes4318/dograh/hopwhistle/customizations/deploy/homepayouts/setup.sh -o /tmp/hp.sh && bash /tmp/hp.sh
set -euo pipefail
step(){ printf '\n===== %s\n' "$*"; }
die(){ printf '\nSTOPPED: %s\n' "$*"; exit 1; }
HOST=aivoice.homepayouts.com
D=/opt/dograh; H=/opt/hopwhistle; SRC=/opt/dograh-homepayouts
REPO=https://github.com/bbarnes4318/dograh.git
HC="docker compose --env-file .env -f infra/docker/docker-compose.dev.yml"

step "1/6 Portal: deploy main (AI Voice page for Home Payouts)"
cd $H
git fetch origin && git checkout main && git pull origin main && git rev-parse --short HEAD
$HC build api web freeswitch
$HC up -d --force-recreate api web freeswitch
sleep 8
docker ps --filter name=hopwhistle-api --format '{{.Names}} {{.Status}}'

step "2/6 Voice app: fetch the hopwhistle/customizations code (into $SRC; $D is not touched)"
if [ -d $SRC/.git ]; then
  git -C $SRC fetch -q --depth 1 "$REPO" hopwhistle/customizations && git -C $SRC checkout -q -B hp FETCH_HEAD
else
  git clone -q --depth 1 --branch hopwhistle/customizations "$REPO" $SRC
fi
git -C $SRC log --oneline -1

step "3/6 nginx + certificate for $HOST"
mkdir -p /var/www/hopwhistle
install -m 0644 $SRC/deploy/homepayouts/aivoice.homepayouts.com.conf /etc/nginx/sites-available/$HOST
if [ ! -f /etc/letsencrypt/live/$HOST/fullchain.pem ]; then
  certbot certonly --nginx -d $HOST --non-interactive --agree-tos --register-unsafely-without-email \
    || certbot certonly --webroot -w /var/www/hopwhistle -d $HOST --non-interactive --agree-tos --register-unsafely-without-email \
    || die "certificate for $HOST was not issued"
fi
ln -sf /etc/nginx/sites-available/$HOST /etc/nginx/sites-enabled/$HOST
if ! nginx -t; then rm -f /etc/nginx/sites-enabled/$HOST; die "nginx config test failed; $HOST disabled again, nothing else changed in nginx"; fi
systemctl reload nginx

step "4/6 Voice app: rebuild the UI with the Home Payouts brand"
cd $D
UICID=$(docker compose ps -q ui 2>/dev/null | head -1)
[ -n "$UICID" ] || die "could not find the voice app ui container (docker compose ps ui in $D)"
IMG=$(docker inspect -f '{{.Config.Image}}' "$UICID")
echo "ui image: $IMG"
docker tag "$IMG" "${IMG%:*}:before-home-payouts" && echo "previous image kept as ${IMG%:*}:before-home-payouts"
docker build -t "$IMG" -f $SRC/ui/Dockerfile $SRC
docker compose up -d --no-deps --force-recreate ui

step "5/6 Voice app: create the Home Payouts account with the same settings"
PGC=$(docker compose ps -q postgres 2>/dev/null | head -1)
[ -n "$PGC" ] || die "could not find the voice app postgres container"
PGU=$(docker exec "$PGC" printenv POSTGRES_USER || echo postgres)
PGD=$(docker exec "$PGC" printenv POSTGRES_DB || echo "$PGU")
VQ(){ docker exec "$PGC" psql -U "$PGU" -d "$PGD" -tAX -v ON_ERROR_STOP=1 -c "$1"; }
SRC_USER=$(grep -E '^AIVOICE_DEFAULT_USER_ID=' $H/.env | tail -1 | cut -d= -f2- | tr -d "\"' " || true)
SRC_USER=${SRC_USER:-1}
SRC_ORG=$(VQ "SELECT COALESCE(selected_organization_id, (SELECT min(organization_id) FROM organization_users WHERE user_id=$SRC_USER)) FROM users WHERE id=$SRC_USER")
[ -n "$SRC_ORG" ] || die "voice user $SRC_USER (the insurance workspace) was not found"
echo "copying settings from voice user $SRC_USER / organization $SRC_ORG"
docker exec -i "$PGC" psql -U "$PGU" -d "$PGD" -v ON_ERROR_STOP=1 \
  -v source_org="$SRC_ORG" -v source_user="$SRC_USER" \
  -v provider_id=hoppwhistle-home-payouts -v email=voice@homepayouts.com < $SRC/scripts/clone_workspace.sql
NEW_USER=$(VQ "SELECT id FROM users WHERE provider_id='hoppwhistle-home-payouts'")
[ -n "$NEW_USER" ] || die "Home Payouts voice account was not created"
echo "Home Payouts voice user: $NEW_USER"

step "6/6 Portal: sign Home Payouts into its own voice account"
HQ(){ docker exec hopwhistle-postgres-dev psql -U callfabric -d callfabric -tAX -c "$1"; }
SLUG=$(HQ "SELECT slug FROM tenants WHERE vertical='REAL_ESTATE' AND \"brandTheme\"='home-payouts'")
[ "$(printf '%s\n' "$SLUG" | grep -c .)" = 1 ] || SLUG=$(HQ "SELECT slug FROM tenants WHERE vertical='REAL_ESTATE'")
[ "$(printf '%s\n' "$SLUG" | grep -c .)" = 1 ] || die "could not pick the Home Payouts workspace; real estate workspaces: $(echo $SLUG)"
API=$(docker ps --filter name=hopwhistle-api --format '{{.Names}}' | head -1)
docker exec "$API" node dist/cli/re-workspace.js voice --tenant "$SLUG" --dograh-user "$NEW_USER" --url https://$HOST --cookie-domain .homepayouts.com

step "Check"
curl -sS -o /dev/null -w "https://$HOST -> HTTP %{http_code}\n" https://$HOST/ || true
echo "DONE. Home Payouts -> AI Voice now opens its own branded voice workspace."
