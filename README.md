# Vernyi Bears

Web shop for a personalised-gift business in Almaty: product catalog, order form with customer photos, and an admin panel. New orders are sent to a WhatsApp group.

This is the second version of the client's order system. The first one was built on Oracle APEX; this one is a rewrite in Python / Flask.

## Features

- Product catalog with categories and photos
- Order form: city, delivery method (inDrive, Yandex, pickup, own delivery), needed date, comment, and up to 10 photos per item for personalised gifts (extension and size checks)
- Admin panel: order list, statuses (new, discussing, working, done, cancelled), payment status, product and settings editing
- Excel export of orders (`/admin/export.xlsx`)
- Private link to an order's photos by random token (`/o/<token>`)
- New-order notification to a WhatsApp group through [Green API](https://green-api.com/) (optional)

## Stack

Python, Flask, SQLite, gunicorn, Pillow, openpyxl, requests.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 seed_catalog.py        # load the product catalog
python3 app.py                 # http://127.0.0.1:5000
```

The database file `shop.db` is created on first start.

## Configuration

All settings are environment variables:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session key. Set your own value in production |
| `ADMIN_PASSWORD` | Admin panel password. Set your own value in production |
| `SITE_URL` | Public address, used in links to order photos |
| `GREENAPI_ID`, `GREENAPI_TOKEN`, `WA_GROUP_ID` | Enable WhatsApp notifications (all three required) |

## Deploy

`deploy.sh` packs the project, uploads it to the server, installs requirements, restarts the `vernyibears` service and checks that the site answers. The orders database, uploads and `.env` on the server are kept.

```bash
DEPLOY_SERVER=user@host ./deploy.sh
```

## Status

Built and deployed for the client; the client has not started using it yet.
