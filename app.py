import io
import json
import os
import re
import logging
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote

import requests
from flask import (Flask, abort, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "shop.db")
UPLOAD_DIR = os.path.join(BASE, "static", "uploads")
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
ORDER_UPLOAD_DIR = os.path.join(UPLOAD_DIR, "orders")
os.makedirs(ORDER_UPLOAD_DIR, exist_ok=True)
PHOTO_EXT = ALLOWED_EXT | {"heic"}
PHOTO_RE = re.compile(r"^uploads/orders/[0-9a-f]{16}\.(?:" + "|".join(PHOTO_EXT) + ")$")
MAX_PHOTOS_PER_ITEM = 10
CITIES = ["Алматы", "Астана", "Шымкент", "Другое"]
DELIVERY = {"indrive": "inDrive", "yandex": "Яндекс", "pickup": "Самовывоз",
            "own": "Наша доставка (по тарифу Яндекса)"}
KZ_TZ = timezone(timedelta(hours=5))
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")   # public address, e.g. https://vernyibears.kz
STATUSES = {"new": "Новый", "discussing": "Обсуждаем", "working": "В работе",
            "done": "Завершён", "cancelled": "Отменён"}
PAYMENT = {"unpaid": "Не оплачен", "paid": "Оплачен"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def add_column(conn, table, col, ddl):
    """ALTER TABLE ... ADD COLUMN that tolerates another worker adding it first. -> True if added."""
    if col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e):
            return False
        raise


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        price INTEGER NOT NULL,
        category TEXT DEFAULT '',
        image TEXT DEFAULT '',
        personalizable INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        variants TEXT DEFAULT '[]',
        images TEXT DEFAULT '[]',
        sort INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        name TEXT, phone TEXT, address TEXT, comment TEXT,
        items TEXT, total INTEGER,
        status TEXT DEFAULT 'new'
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    for col, ddl in (("variants", "TEXT DEFAULT '[]'"), ("images", "TEXT DEFAULT '[]'"),
                     ("sort", "INTEGER DEFAULT 0")):
        add_column(conn, "products", col, ddl)
    for col in ("city", "delivery", "need_date", "token"):
        add_column(conn, "orders", col, "TEXT DEFAULT ''")
    add_column(conn, "orders", "source", "TEXT DEFAULT 'site'")
    add_column(conn, "orders", "number", "TEXT DEFAULT ''")
    for oid, created, phone in conn.execute(
            "SELECT id, created_at, phone FROM orders WHERE number IS NULL OR number=''").fetchall():
        try:
            day = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            day = datetime.now(KZ_TZ)
        while True:
            num = make_order_number(day, phone or "")
            if not conn.execute("SELECT 1 FROM orders WHERE number=?", (num,)).fetchone():
                break
        conn.execute("UPDATE orders SET number=? WHERE id=?", (num, oid))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_number ON orders(number) WHERE number != ''")
    if add_column(conn, "orders", "payment", "TEXT DEFAULT 'unpaid'"):
        conn.execute("UPDATE orders SET payment='paid', status='working' WHERE status='paid'")
    defaults = {
        "shop_name": "Vernyi Bears",
        "tagline": "Сладкие подарки с вашим фото",
        "whatsapp": "77005553398",
        "currency": "₸",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


VARIANT_SIZES = {"thumb": (480, 74), "full": (900, 78)}  # max side px, webp quality


def image_variant(rel, kind):
    """Static path -> path of a smaller webp copy (made on first use); original on any failure."""
    side, quality = VARIANT_SIZES[kind]
    out_rel = f"{os.path.splitext(rel)[0]}.{kind}.webp"
    src, out = (os.path.join(app.static_folder, r) for r in (rel, out_rel))
    try:
        if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(src):
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(src))
            im.thumbnail((side, side))
            tmp = f"{out}.{os.getpid()}.tmp"
            im.convert("RGB").save(tmp, "WEBP", quality=quality, method=6)
            os.replace(tmp, out)
        return out_rel
    except Exception:
        return rel


def product_view(row):
    """Row -> dict with parsed variants/images and a display price."""
    p = dict(row)
    p["variants"] = json.loads(p.get("variants") or "[]")
    p["images"] = json.loads(p.get("images") or "[]") or ([p["image"]] if p["image"] else [])
    p["thumbs"] = [image_variant(i, "thumb") for i in p["images"]]
    p["fulls"] = [image_variant(i, "full") for i in p["images"]]
    prices = [v["price"] for v in p["variants"]] or ([p["price"]] if p["price"] else [])
    p["price_from"] = min(prices) if prices else 0
    p["has_range"] = len(set(prices)) > 1
    return p


def parse_variants(text):
    """'Название — 12 500 | 3' per line -> [{'name', 'price', 'photo'?}]; 3 = photo number."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*(.+?)\s*[—–-]\s*([\d\s]+?)\s*(?:тг|₸)?\s*(?:\|\s*(\d+))?\s*$", line)
        if m:
            v = {"name": m.group(1).strip(), "price": int(re.sub(r"\s", "", m.group(2)))}
            if m.group(3):
                v["photo"] = int(m.group(3))
            out.append(v)
    return out


def variants_text(variants):
    return "\n".join(f"{v['name']} — {v['price']}" + (f" | {v['photo']}" if v.get("photo") else "")
                     for v in variants)


def settings():
    return {r["key"]: r["value"] for r in db().execute("SELECT * FROM settings")}


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


# ---------- WhatsApp group notification (Green API) ----------
# Set GREENAPI_ID, GREENAPI_TOKEN and WA_GROUP_ID (e.g. 120363...@g.us) to enable.

log = logging.getLogger("shop")
GREEN_URL = os.environ.get("GREENAPI_URL", "https://api.green-api.com")


def group_enabled():
    return all(os.environ.get(k) for k in ("GREENAPI_ID", "GREENAPI_TOKEN", "WA_GROUP_ID"))


def _green(method, **kw):
    url = f"{GREEN_URL}/waInstance{os.environ['GREENAPI_ID']}/{method}/{os.environ['GREENAPI_TOKEN']}"
    r = requests.post(url, timeout=30, **kw)
    r.raise_for_status()
    return r


def send_to_group(order_id, text, photos):
    """Order text first, then every attached photo. Never raises: the order is already saved."""
    chat = os.environ["WA_GROUP_ID"]
    try:
        _green("sendMessage", json={"chatId": chat, "message": text})
        for n, path in enumerate(photos, 1):
            full = os.path.join(UPLOAD_DIR, path[len("uploads/"):])
            with open(full, "rb") as fh:
                _green("sendFileByUpload", data={"chatId": chat, "caption": f"Фото {n}/{len(photos)} к заявке №{order_id}"},
                       files={"file": (os.path.basename(full), fh)})
    except Exception:
        log.exception("WhatsApp group notification failed for order %s", order_id)


def make_order_number(when, phone):
    """DDMMYY-<last 4 phone digits>-<4 random digits>, e.g. 291026-2233-4817."""
    tail = "".join(c for c in phone if c.isdigit())[-4:].rjust(4, "0")
    return f"{when:%d%m%y}-{tail}-{secrets.randbelow(10000):04d}"


def valid_photos(paths):
    out = []
    for path in (paths or [])[:MAX_PHOTOS_PER_ITEM]:
        if (isinstance(path, str) and PHOTO_RE.match(path)
                and os.path.exists(os.path.join(UPLOAD_DIR, path[len("uploads/"):]))):
            out.append(path)
    return out


def build_item(entry, allow_free=False):
    """Cart entry -> order item (price taken from the catalog), or None if invalid.
    allow_free: admin may add a free-form line {name, price}."""
    try:
        qty = max(1, min(int(entry.get("qty") or 1), 99))
    except (TypeError, ValueError):
        qty = 1
    custom = (entry.get("custom") or "").strip()[:300]
    photos = valid_photos(entry.get("photos"))
    if allow_free and not entry.get("id"):
        name = (entry.get("name") or "").strip()[:120]
        if not name:
            return None
        try:
            price = max(0, int(entry.get("price") or 0))
        except (TypeError, ValueError):
            price = 0
        return {"name": name, "price": price, "qty": qty, "photos": photos, "custom": custom}
    row = db().execute("SELECT * FROM products WHERE id=? AND active=1", (entry.get("id"),)).fetchone()
    if not row:
        return None
    p = product_view(row)
    name, price = p["name"], p["price"]
    if p["variants"]:
        idx = entry.get("variant")
        if not isinstance(idx, int) or not 0 <= idx < len(p["variants"]):
            return None
        name += f" — {p['variants'][idx]['name']}"
        price = p["variants"][idx]["price"]
    if not p["personalizable"]:
        photos, custom = [], ""
    return {"name": name, "price": price, "qty": qty, "photos": photos, "custom": custom}


def clean_city(data):
    """-> (city, error)"""
    city = (data.get("city") or "").strip()
    if city not in CITIES:
        return None, "Выберите город"
    if city == "Другое":
        other = (data.get("city_other") or "").strip()[:80]
        if not other:
            return None, "Укажите ваш город"
        city = f"Другое ({other})"
    return city, None


def insert_order(now, name, phone, city, address, delivery, need_date, comment, items, total,
                 status="new", payment="unpaid", source="site"):
    """-> (id, number, token); retries on the (unlikely) repeat of a random order number."""
    token = secrets.token_hex(8)
    for _ in range(20):
        number = make_order_number(now, phone)
        try:
            cur = db().execute(
                "INSERT INTO orders (number, created_at, name, phone, city, address, delivery, need_date, "
                "comment, items, total, token, status, payment, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (number, now.strftime("%Y-%m-%d %H:%M:%S"), name, phone, city, address, delivery,
                 need_date.isoformat(), comment, json.dumps(items, ensure_ascii=False), total, token,
                 status, payment, source))
        except sqlite3.IntegrityError:
            continue
        db().commit()
        return cur.lastrowid, number, token
    return None


def money(n, cur):
    return f"{n:,}".replace(",", " ") + f" {cur}"


def order_template(o, items, cur):
    """The fixed order card the team receives: number, date, city, order, address, delivery,
    needed-by date, amount to pay, photos."""
    lines = []
    for i in items:
        line = f"• {i['name']} × {i['qty']}" + (
            f" — {money(i['price'] * i['qty'], cur)}" if i["price"] else " — цена по запросу")
        if i["custom"]:
            line += f"\n   ✍️ {i['custom']}"
        lines.append(line)
    pay = money(o["total"], cur) if o["total"] else "уточним"
    if o["total"] and any(not i["price"] for i in items):
        pay += " + позиции по запросу"
    if o["delivery_key"] != "pickup":
        pay += " (без доставки)"
    n_photos = sum(len(i["photos"]) for i in items)
    if not n_photos:
        photos = "нет"
    elif SITE_URL:
        photos = f"{n_photos} шт. — {SITE_URL}/o/{o['token']}"
    else:
        photos = f"{n_photos} шт. (смотрите в админке, заявка №{o['number']})"
    out = [
        f"🧾 Заявка №{o['number']}",
        f"📅 Дата заказа: {o['created'].strftime('%d.%m.%Y %H:%M')}",
        f"🏙 Город: {o['city']}",
        "🎁 Заказ:", *lines,
        f"📍 Адрес: {o['address']}",
        f"🚚 Доставка: {o['delivery']}",
        f"🗓 Нужно на: {o['need_date'].strftime('%d.%m.%Y')}",
        f"💰 Сумма на оплату: {pay}",
        f"📷 Фото: {photos}",
        "",
        f"👤 {o['name']}, {o['phone']}",
    ]
    if o["comment"]:
        out.append(f"💬 {o['comment']}")
    return "\n".join(out)


@app.context_processor
def inject_asset_version():
    """Cache-buster for style.css: nginx caches /static/ for days."""
    try:
        v = int(os.path.getmtime(os.path.join(BASE, "static", "style.css")))
    except OSError:
        v = 0
    return {"asset_v": v}


@app.template_filter("dt")
def fmt_dt(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return s or ""


@app.template_filter("dmy")
def fmt_date(s):
    try:
        return date.fromisoformat(s).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return s or ""


# ---------- storefront ----------

@app.route("/")
def index():
    rows = db().execute("SELECT * FROM products WHERE active=1 ORDER BY sort, id").fetchall()
    return render_template("store.html", products=[product_view(r) for r in rows],
                           s=settings())


@app.post("/api/upload")
def upload_photo():
    f = request.files.get("photo")
    ext = f.filename.rsplit(".", 1)[-1].lower() if f and "." in (f.filename or "") else ""
    if ext not in PHOTO_EXT:
        return jsonify(error="Допустимы фото: jpg, png, webp, heic"), 400
    name = f"{secrets.token_hex(8)}.{ext}"
    f.save(os.path.join(ORDER_UPLOAD_DIR, name))
    return jsonify(path=f"uploads/orders/{name}")


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Файл слишком большой (максимум 8 МБ)"), 413


@app.post("/api/order")
def create_order():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    cart = data.get("items") or []
    if not name or not phone or not cart:
        return jsonify(error="Заполните имя, телефон и добавьте товары"), 400
    if sum(c.isdigit() for c in phone) < 7:
        return jsonify(error="Укажите телефон полностью"), 400

    city, err = clean_city(data)
    if err:
        return jsonify(error=err), 400
    delivery_key = data.get("delivery")
    if delivery_key not in DELIVERY:
        return jsonify(error="Выберите способ доставки"), 400
    try:
        need_date = date.fromisoformat(data.get("need_date") or "")
    except ValueError:
        return jsonify(error="Укажите, на какую дату нужен заказ"), 400
    now = datetime.now(KZ_TZ)
    if need_date < now.date():
        return jsonify(error="Дата не может быть в прошлом"), 400
    address = (data.get("address") or "").strip()[:300]
    if delivery_key == "pickup":
        address = address or "Самовывоз"
    elif not address:
        return jsonify(error="Укажите точный адрес"), 400

    items = [it for it in (build_item(e) for e in cart) if it]
    total = sum(i["price"] * i["qty"] for i in items)
    if not items:
        return jsonify(error="Товары недоступны"), 400

    comment = (data.get("comment") or "").strip()[:500]
    created = insert_order(now, name, phone, city, address, DELIVERY[delivery_key], need_date,
                           comment, items, total)
    if not created:
        return jsonify(error="Не удалось создать заказ, попробуйте ещё раз"), 500
    oid, number, token = created

    s = settings()
    text = order_template(dict(id=oid, number=number, created=now, city=city, address=address,
                               delivery=DELIVERY[delivery_key], delivery_key=delivery_key,
                               need_date=need_date, total=total, token=token, name=name,
                               phone=phone, comment=comment), items, s["currency"])
    wa = f"https://wa.me/{s['whatsapp']}?text={quote(text)}"

    if group_enabled():
        digits = "".join(c for c in phone if c.isdigit())
        photos = [p for i in items for p in i["photos"]]
        threading.Thread(target=send_to_group,
                         args=(number, f"{text}\nЧат с клиентом: https://wa.me/{digits}", photos),
                         daemon=True).start()
    return jsonify(ok=True, order_id=oid, number=number, whatsapp_url=wa)


@app.route("/o/<token>")
def order_photos(token):
    row = db().execute("SELECT number, items FROM orders WHERE token=? AND token!=''", (token,)).fetchone()
    if not row:
        abort(404)
    photos = [p for i in json.loads(row["items"]) for p in i.get("photos", [])]
    return render_template("order_photos.html", number=row["number"], photos=photos, s=settings())


# ---------- admin ----------

@app.route("/admin/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["admin"] = True
            return redirect(url_for("admin"))
        error = "Неверный пароль"
    return render_template("login.html", error=error)


@app.route("/admin/export.xlsx")
@admin_required
def export_xlsx():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    cur_sym = settings()["currency"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Заказы"
    head = ["Номер заказа", "Дата заказа", "Город", "Заказ", "Адрес", "Доставка", "Нужно на",
            f"Сумма, {cur_sym}", "Фото", "Имя", "Телефон", "Комментарий", "Статус", "Оплата"]
    ws.append(head)
    for r in db().execute("SELECT * FROM orders ORDER BY id"):
        items = json.loads(r["items"])
        text = "\n".join(
            f"{i['name']} × {i['qty']}" + (f" ({i['custom']})" if i.get("custom") else "") for i in items)
        n_photos = sum(len(i.get("photos", [])) for i in items)
        photos = (f"{SITE_URL}/o/{r['token']}" if SITE_URL and n_photos and r["token"]
                  else (f"{n_photos} шт." if n_photos else ""))
        try:
            need = date.fromisoformat(r["need_date"])
        except (TypeError, ValueError):
            need = r["need_date"]
        try:
            created = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            created = r["created_at"]
        ws.append([r["number"], created, r["city"], text, r["address"], r["delivery"], need,
                   r["total"], photos, r["name"], r["phone"], r["comment"],
                   STATUSES.get(r["status"], r["status"]), PAYMENT.get(r["payment"], r["payment"])])
    for c in ws[1]:
        c.font = Font(bold=True)
    for col, w in zip("ABCDEFGHIJKLMN", (18, 17, 16, 46, 36, 30, 12, 12, 38, 18, 18, 30, 12, 12)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        row[1].number_format = "DD.MM.YYYY HH:MM"
        row[6].number_format = "DD.MM.YYYY"
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"zakazy-{datetime.now(KZ_TZ):%Y-%m-%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def load_orders(where="", args=()):
    out = []
    for o in db().execute(f"SELECT * FROM orders {where} ORDER BY id DESC", args):
        d = dict(o)
        d["items"] = json.loads(d["items"])
        out.append(d)
    return out


@app.get("/admin/orders/poll")
@admin_required
def orders_poll():
    """Orders newer than ?after=<id> as ready table rows, plus the current status of every order."""
    after = request.args.get("after", type=int) or 0
    new = load_orders("WHERE id > ?", (after,))
    s = settings()
    html = "".join(render_template("_order_row.html", o=o, s=s, statuses=STATUSES, payments=PAYMENT)
                   for o in new)
    states = {r["id"]: [r["status"], r["payment"]]
              for r in db().execute("SELECT id, status, payment FROM orders")}
    return jsonify(html=html, states=states,
                   new=[{"id": o["id"], "number": o["number"], "name": o["name"], "total": o["total"]}
                        for o in new])


@app.route("/admin")
@admin_required
def admin():
    orders = load_orders()
    products = [product_view(r) for r in
                db().execute("SELECT * FROM products ORDER BY sort, id").fetchall()]
    return render_template("admin.html", orders=orders, products=products,
                           variants_text=variants_text,
                           s=settings(), statuses=STATUSES, payments=PAYMENT,
                           catalog=catalog_options(products), cities=CITIES, delivery=DELIVERY,
                           default_pw=ADMIN_PASSWORD == "admin123")


def catalog_options(products):
    out = []
    for p in products:
        if not p["active"]:
            continue
        if p["variants"]:
            out += [dict(id=p["id"], variant=i, label=f"{p['name']} — {v['name']}", price=v["price"])
                    for i, v in enumerate(p["variants"])]
        else:
            out.append(dict(id=p["id"], variant=None, label=p["name"], price=p["price"]))
    return out


def save_image(file):
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXT:
        abort(400, "Допустимы только jpg, png, webp, gif")
    name = f"{secrets.token_hex(8)}_{secure_filename(file.filename)}"
    file.save(os.path.join(UPLOAD_DIR, name))
    return f"uploads/{name}"


@app.post("/admin/product")
@admin_required
def save_product():
    f = request.form
    pid = f.get("id")
    variants = parse_variants(f.get("variants", ""))
    price = int(f.get("price") or 0)
    if variants:
        price = min(v["price"] for v in variants)
    new_images = [p for p in (save_image(x) for x in request.files.getlist("image")) if p]
    vals = (f["name"].strip(), f.get("description", "").strip(), price,
            f.get("category", "").strip(), 1 if f.get("personalizable") else 0,
            1 if f.get("active") else 0, json.dumps(variants, ensure_ascii=False))
    if pid:
        row = db().execute("SELECT images FROM products WHERE id=?", (pid,)).fetchone()
        images = [i for i in json.loads(row["images"] or "[]") if i not in f.getlist("remove_img")]
        images += new_images
        db().execute("UPDATE products SET name=?, description=?, price=?, category=?, "
                     "personalizable=?, active=?, variants=?, images=?, image=? WHERE id=?",
                     (*vals, json.dumps(images), images[0] if images else "", pid))
    else:
        db().execute("INSERT INTO products (name, description, price, category, "
                     "personalizable, active, variants, images, image, sort) VALUES (?,?,?,?,?,?,?,?,?,"
                     "(SELECT COALESCE(MAX(sort), 0) + 1 FROM products))",
                     (*vals, json.dumps(new_images), new_images[0] if new_images else ""))
    db().commit()
    return redirect(url_for("admin") + "#products")


@app.post("/admin/product/<int:pid>/move")
@admin_required
def move_product(pid):
    step = -1 if request.form.get("dir") == "up" else 1
    ids = [r["id"] for r in db().execute("SELECT id FROM products ORDER BY sort, id")]
    if pid in ids:
        i, j = ids.index(pid), ids.index(pid) + step
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        db().executemany("UPDATE products SET sort=? WHERE id=?", [(n, x) for n, x in enumerate(ids, 1)])
        db().commit()
    return redirect(url_for("admin") + "#products")


@app.post("/admin/product/<int:pid>/delete")
@admin_required
def delete_product(pid):
    db().execute("DELETE FROM products WHERE id=?", (pid,))
    db().commit()
    return redirect(url_for("admin") + "#products")


@app.post("/admin/order/create")
@admin_required
def admin_create_order():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:120]
    phone = (data.get("phone") or "").strip()[:40]
    if not name or sum(c.isdigit() for c in phone) < 7:
        return jsonify(error="Укажите имя и телефон полностью"), 400
    city, err = clean_city(data)
    if err:
        return jsonify(error=err), 400
    delivery_key = data.get("delivery")
    if delivery_key not in DELIVERY:
        return jsonify(error="Выберите способ доставки"), 400
    try:
        need_date = date.fromisoformat(data.get("need_date") or "")
    except ValueError:
        return jsonify(error="Укажите дату"), 400
    address = (data.get("address") or "").strip()[:300]
    if delivery_key == "pickup":
        address = address or "Самовывоз"
    elif not address:
        return jsonify(error="Укажите точный адрес"), 400
    items = [it for it in (build_item(e, allow_free=True) for e in data.get("items") or []) if it]
    if not items:
        return jsonify(error="Добавьте хотя бы одну позицию"), 400
    try:
        total = max(0, int(data.get("total")))
    except (TypeError, ValueError):
        total = sum(i["price"] * i["qty"] for i in items)
    status = data.get("status") if data.get("status") in STATUSES else "new"
    payment = data.get("payment") if data.get("payment") in PAYMENT else "unpaid"
    created = insert_order(datetime.now(KZ_TZ), name, phone, city, address, DELIVERY[delivery_key],
                           need_date, (data.get("comment") or "").strip()[:500], items, total,
                           status=status, payment=payment, source="manual")
    if not created:
        return jsonify(error="Не удалось создать заказ, попробуйте ещё раз"), 500
    return jsonify(ok=True, number=created[1])


@app.post("/admin/order/<int:oid>/delete")
@admin_required
def delete_order(oid):
    row = db().execute("SELECT items FROM orders WHERE id=?", (oid,)).fetchone()
    if row:
        for item in json.loads(row["items"]):
            for path in item.get("photos", []):
                if PHOTO_RE.match(path):
                    try:
                        os.remove(os.path.join(UPLOAD_DIR, path[len("uploads/"):]))
                    except OSError:
                        pass
        db().execute("DELETE FROM orders WHERE id=?", (oid,))
        db().commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("admin") + "#orders")


@app.post("/admin/order/<int:oid>/status")
@admin_required
def order_status(oid):
    status, payment = request.form.get("status"), request.form.get("payment")
    if status in STATUSES:
        db().execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    if payment in PAYMENT:
        db().execute("UPDATE orders SET payment=? WHERE id=?", (payment, oid))
    db().commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("admin") + "#orders")


@app.post("/admin/settings")
@admin_required
def save_settings():
    for k in ("shop_name", "tagline", "whatsapp", "currency"):
        v = request.form.get(k, "").strip()
        if k == "whatsapp":
            v = "".join(c for c in v if c.isdigit())
        db().execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, v))
    db().commit()
    return redirect(url_for("admin") + "#settings")


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
