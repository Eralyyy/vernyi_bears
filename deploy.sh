#!/usr/bin/env bash
# Update the live site. Code, templates, styles and catalog photos are replaced;
# the orders database, client photos, product uploads and .env on the server are kept.
#   ./deploy.sh
set -euo pipefail
SERVER=root@142.93.163.218
cd "$(dirname "$0")"
COPYFILE_DISABLE=1 tar czf /tmp/vb-update.tgz \
  --exclude=__pycache__ --exclude='скриншоты' --exclude=shop.db --exclude=.env \
  --exclude='static/uploads' --exclude='*.log' .
scp -q /tmp/vb-update.tgz "$SERVER:/tmp/vb-update.tgz"
ssh "$SERVER" 'set -e
  cd /opt/vernyibears
  tar xzf /tmp/vb-update.tgz && rm /tmp/vb-update.tgz
  ./venv/bin/pip install -q -r requirements.txt
  chown -R vernyibears:vernyibears /opt/vernyibears
  systemctl restart vernyibears
  for i in 1 2 3 4 5 6 7 8; do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/ || true)
    [ "$code" = 200 ] && break; sleep 1
  done
  echo "site: $code"
  systemctl is-active vernyibears'
