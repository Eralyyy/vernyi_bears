"""Replace the products table with the catalog taken from the WhatsApp Business screenshots.

Run once:  python3 seed_catalog.py
"""
import glob
import json
import re
import sqlite3

import app

GIFT = "Именной Kinder с вашим фото — в подарок."

PRODUCTS = [
    dict(slug="giant", category="Букеты и торты", name="WOW конфета ГИГАНТ — тренд 2026",
         description="В каждой конфете — именной Kinder с вашим фото в подарок.\n"
                     "Возможен индивидуальный заказ: сделаем конфету на любую сумму и размер.\n"
                     "Стоимость обсудим в WhatsApp.",
         variants=[]),
    dict(slug="cake", category="Букеты и торты", name="Торт из Kinder шоколадок",
         description="Именной Kinder с вашим фото — в подарок к каждому торту.",
         variants=[("Круглый Kinder-торт с Teddy", 25500, 2),
                   ("2-ярусный Kinder-торт из 88 шоколадок", 32500),
                   ("Круглый 2-ярусный торт с конфетами Roshen", 37500),
                   ("3-ярусный торт из 111 шоколадок Kinder", 42500, 1),
                   ("Kinder-торт + клубника в шоколаде (необычное комбо)", 31500)]),
    dict(slug="bouquet", category="Букеты и торты", name="Букет из Kinder, 61 шт",
         description="Подберём на любую сумму и размер.\nПодарочное оформление именного киндера с фото — в подарок.",
         price=55000, variants=[]),
    dict(slug="boxes", category="Боксы", name="Боксы со сладостями",
         description="К каждому боксу — именной Kinder с вашим фото в подарок.",
         variants=[("Бокс-шкатулка", 32500), ("Premium бокс", 55000),
                   ("Двойное сердечко", 27500), ("Сладкое сердечко", 21500),
                   ("Мини сердечко", 18500),
                   ("Бокс Махаббат (+5 жвачек Love is в подарок)", 32500),
                   ("Сладости + 5 жвачек Love is", 21500),
                   ("VIP бокс — 10 именных Kinder с фото", 45000),
                   ("Трансформер", 32500), ("3-ярусный Premium бокс", 49000)]),
    dict(slug="berry", category="Клубника в шоколаде", name="Клубника в бельгийском шоколаде",
         description="Свежая клубника в бельгийском шоколаде. " + GIFT +
                     "\nВыберите вариант — оформим заказ быстро.",
         variants=[("25 шт, бокс", 23500), ("Mini корзина", 17500), ("I Love You", 31500),
                   ("Premium сердце", 45000), ("Двойное сердце", 32500),
                   ("Mini сердце", 17500), ("Premium Мама", 45000),
                   ("Premium Ана", 35000), ("16 шт, бокс", 14500)]),
    dict(slug="kinder", category="Именные подарки", name="Именной Kinder с вашим фото",
         description="АКЦИЯ 1+1: за эту цену вы получаете 2 киндера.\nОформление в подарок.",
         price=7990, variants=[]),
    dict(slug="merci", category="Именные подарки", name="Merci с вашим фото",
         description="Шоколадный набор Merci с вашим фото на упаковке.",
         price=8500, variants=[]),
    dict(slug="gum", category="Именные подарки", name="Жвачки Love is с вашим фото",
         description="Идеальный милый подарок для любимых.",
         variants=[("5 шт", 2500), ("10 шт", 4500), ("50 шт", 25000), ("100 шт", 35000)]),
    dict(slug="lamp", category="Именные подарки", name="Подсветка-ночник с вашим фото",
         description="6 режимов света. " + GIFT + "\nСтильный и тёплый подарок для любого повода.",
         price=14500, variants=[]),
]


def photos(slug):
    files = glob.glob(f"static/catalog/{slug}-*.jpg")
    files.sort(key=lambda f: int(re.search(r"-(\d+)\.jpg$", f).group(1)))
    return [f.removeprefix("static/") for f in files]


conn = sqlite3.connect(app.DB_PATH)
conn.execute("DELETE FROM products")
conn.execute("DELETE FROM sqlite_sequence WHERE name='products'")
for sort, p in enumerate(PRODUCTS):
    variants = []
    for n, pr, *ph in p["variants"]:
        v = {"name": n, "price": pr}
        if ph:
            v["photo"] = ph[0]
        variants.append(v)
    if p["slug"] in ("boxes", "berry"):          # variant order == photo order in the catalog
        for i, v in enumerate(variants, 1):
            v["photo"] = i
    price = min(v["price"] for v in variants) if variants else p.get("price", 0)
    imgs = photos(p["slug"])
    conn.execute(
        "INSERT INTO products (name, description, price, category, image, personalizable, "
        "active, variants, images, sort) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (p["name"], p["description"], price, p["category"], imgs[0] if imgs else "", 1, 1,
         json.dumps(variants, ensure_ascii=False), json.dumps(imgs), sort))
for k, v in {"shop_name": "Vernyi Bears", "tagline": "Сладкие подарки с вашим фото",
             "whatsapp": "77005553398", "currency": "₸"}.items():
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, v))
conn.commit()
print(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], "products loaded")
