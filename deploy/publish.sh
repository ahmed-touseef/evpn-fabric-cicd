#!/usr/bin/env bash
# Publishes the status page at https://cicd.touseefahmed.com. Run it on the server, once the deploy
# agent has deployed the commit that brought this file:
#   sudo bash /srv/fabric/deploy/publish.sh
# It installs the status collector and the nightly game day (systemd timers), an Nginx site with its
# security settings and a Let's Encrypt certificate, then starts the first game day. Safe to run
# again, for example after a change to the units in deploy/. Other sites on this Nginx are not
# touched: if Nginx rejects the new files, every file is put back and Nginx keeps running as it was.
set -euo pipefail
DOMAIN=cicd.touseefahmed.com
SITE=/etc/nginx/sites-available/$DOMAIN
SNIP=/etc/nginx/snippets/fabric-security.conf
LIMITS=/etc/nginx/conf.d/fabric-limits.conf
STATUS=/var/lib/fabric-status/status.json

cd "$(dirname "$0")/.."
if [ "$(id -u)" != 0 ]; then echo "Run it as root: sudo bash $0"; exit 1; fi
if [ "$PWD" != /srv/fabric ]; then echo "Run the copy in /srv/fabric: the page is served from there"; exit 1; fi

echo "== 1. DNS =="
dns=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1 {print $1}')
if [ -z "$dns" ]; then
  echo "  $DOMAIN has no DNS record yet. Add the A record, wait a few minutes, then run this again."
  exit 1
fi
if ! hostname -I | tr ' ' '\n' | grep -qx "$dns"; then
  echo "  $DOMAIN points to $dns, which is not this server. Fix the A record first."
  exit 1
fi
echo "  $DOMAIN -> $dns (this server)"

echo "== 2. Status collector and nightly game day =="
install -m 644 deploy/fabric-*.service deploy/fabric-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q --now fabric-status.timer fabric-gameday.timer
if ! systemctl start fabric-status.service || [ ! -s "$STATUS" ]; then      # the first collection, now
  echo "  The collector did not write $STATUS. Its log:"
  journalctl -u fabric-status -n 20 --no-pager
  exit 1
fi
python3 -c "import json, sys; d = json.load(open(sys.argv[1])); print(f\"  collector ok: {len(d['routers'])} routers, {len(d['reach'])} server pairs, {len(d['problems'])} problems, {d['took_s']} s\")" "$STATUS"
echo "  next game day: $(systemctl show fabric-gameday.timer -P NextElapseUSecRealtime)"
if ! runuser -u www-data -- test -r /srv/fabric/deploy/web/index.html; then
  chmod o+x /srv /srv/fabric /srv/fabric/deploy                 # Nginx may pass through, nothing more
  chmod -R o+rX /srv/fabric/deploy/web
fi
for f in /srv/fabric/deploy/web/index.html "$STATUS"; do
  if ! runuser -u www-data -- test -r "$f"; then
    echo "  Nginx (user www-data) cannot read $f: check the permissions of its folders"
    exit 1
  fi
done
echo "  Nginx can read the page and status.json"

echo "== 3. Nginx site and its security settings =="
BAK=$(mktemp -d)
trap 'rm -rf "$BAK"' EXIT
for f in "$SITE" "$SNIP" "$LIMITS"; do
  if [ -f "$f" ]; then cp -a "$f" "$BAK/"; fi
done
new_site=0
install -d -m 755 /etc/nginx/snippets /etc/nginx/conf.d
install -m 644 deploy/nginx/security.conf "$SNIP"
install -m 644 deploy/nginx/limits.conf "$LIMITS"
if [ ! -f "$SITE" ]; then                                  # an existing copy already has certbot's HTTPS
  install -m 644 deploy/nginx/site.conf "$SITE"
  new_site=1
fi
ln -sf "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
if ! nginx -t 2>"$BAK/test.log"; then
  for f in "$SITE" "$SNIP" "$LIMITS"; do
    if [ -f "$BAK/$(basename "$f")" ]; then cp -a "$BAK/$(basename "$f")" "$f"; else rm -f "$f"; fi
  done
  if [ "$new_site" = 1 ]; then rm -f "/etc/nginx/sites-enabled/$DOMAIN"; fi
  echo "  Nginx rejected the new files, so every file was put back and Nginx runs as before:"
  sed 's/^/    /' "$BAK/test.log"
  exit 1
fi
systemctl reload nginx
echo "  config valid, Nginx reloaded"

echo "== 4. HTTPS certificate =="
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect --keep-until-expiring

echo "== 5. Check from outside =="
sleep 2                                                    # let the old Nginx workers finish
url=https://$DOMAIN
code() { curl -sS -o /dev/null -w "%{http_code}" "$@"; }
hdr=$(curl -sS -D - -o /dev/null "$url/" | tr -d '\r')
for h in Strict-Transport-Security Content-Security-Policy X-Content-Type-Options X-Frame-Options Referrer-Policy; do
  if grep -qi "^$h:" <<<"$hdr"; then echo "  ok       $h"; else echo "  MISSING  $h"; fi
done
echo "  GET  /             -> $(code "$url/")   (want 200)"
echo "  GET  /status.json  -> $(code "$url/status.json")   (want 200)"
echo "  POST /             -> $(code -X POST "$url/")   (want 405)"
echo "  GET  /.git/config  -> $(code "$url/.git/config")   (want 403 or 404)"
echo "  http:// redirect   -> $(code "http://$DOMAIN/")   (want 301)"
curl -sS "$url/status.json" | python3 -c "import json, sys, time; d = json.load(sys.stdin); print('  status.json written', int(time.time() - d['generated']), 's ago, running', d['running']['sha'][:7])"

echo "== 6. First game day =="
systemctl start --no-block fabric-gameday.service
echo "  started: for about four minutes a spine freezes, cables drop and a border goes down, on purpose."
echo "  Watch it on $url, the numbers appear when it ends. Its log: journalctl -u fabric-gameday -f"
echo "Done: open $url"
