#!/usr/bin/env python3
"""Roger Store 🌿 — Telegram-магазин графики для эндуро-мотоциклов.

Запуск:  .venv/bin/python bot.py
Токен и админы берутся из roger-store/.env (см. .env.example).
"""
import asyncio
import html
import json
import logging
import os
import re
import sys
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

import db

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


# .env магазина важнее; если ADMIN_IDS не задан, берём OWNER_IDS из .env соседнего бота
_env = {**load_env(os.path.join(HERE, "..", ".env")), **load_env(os.path.join(HERE, ".env"))}
TOKEN = os.environ.get("SHOP_TOKEN") or _env.get("SHOP_TOKEN", "")
ADMINS = {int(x) for x in re.findall(r"\d+", _env.get("ADMIN_IDS") or _env.get("OWNER_IDS", ""))}

# ---------------------------------------------------------------- оформление
BRANDS = ["KTM", "Husqvarna", "Yamaha", "Honda", "Kawasaki", "Beta", "GasGas",
          "Suzuki", "Sherco", "TM"]
STATUSES = ["Новый", "Ожидает оплаты", "Оплачен", "В обработке", "Отправлен", "Завершён"]
ST_EMOJI = {"Новый": "🆕", "Ожидает оплаты": "⏳", "Оплачен": "💚",
            "В обработке": "🛠", "Отправлен": "🚚", "Завершён": "🏁"}
EARLY = ("Новый", "Ожидает оплаты")   # статусы, когда ещё можно платить
PAGE = 6

BTN_CAT, BTN_CART, BTN_ORDERS = "🛍 Каталог", "🛒 Корзина", "📦 Мои заказы"
BTN_SEARCH, BTN_HELP, BTN_ADMIN = "🔍 Поиск", "💬 Помощь", "🛠 Админ-панель"
BTN_CANCEL = "❌ Отмена"
MENU = {BTN_CAT, BTN_CART, BTN_ORDERS, BTN_SEARCH, BTN_HELP, BTN_ADMIN, BTN_CANCEL}

WELCOME = (
    "🌿🏍 <b>Roger Store</b>\n\n"
    "Привет, {name}! Тут только графика для эндуро-мотоциклов: "
    "full kits, номерные таблички, защита рамы, форков и маятника — "
    "под вашу модель и год. 🔥\n\n"
    "Выбирай раздел в меню 👇")


def esc(s):
    return html.escape(str(s or ""))


def money(n):
    return f"{int(n):,}".replace(",", " ") + " ₽"


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_kb(uid):
    rows = [[KeyboardButton(text=BTN_CAT), KeyboardButton(text=BTN_CART)],
            [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_SEARCH)],
            [KeyboardButton(text=BTN_HELP)]]
    if uid in ADMINS:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_kb(*extra):
    rows = [[KeyboardButton(text=t)] for t in extra] + [[KeyboardButton(text=BTN_CANCEL)]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def edit(c: CallbackQuery, text, markup=None):
    """Правит сообщение; если это фото или правка невозможна — шлёт новое."""
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        try:
            await c.message.delete()
        except TelegramBadRequest:
            pass
        await c.message.answer(text, reply_markup=markup)


async def respond(obj, text, markup=None):
    if isinstance(obj, CallbackQuery):
        await edit(obj, text, markup)
    else:
        await obj.answer(text, reply_markup=markup)


def pager(prefix, page, total):
    row = []
    if page > 0:
        row.append(btn("◀️", f"{prefix}:{page - 1}"))
    if (page + 1) * PAGE < total:
        row.append(btn("▶️", f"{prefix}:{page + 1}"))
    return row


# ---------------------------------------------------------------- товары
def product_brands(p):
    ms = [m.strip() for m in (p["models"] or "").split(",") if m.strip()]
    return ms or BRANDS


def get_products(cid=0, brand=None, query=None, hidden=False):
    sql = "SELECT * FROM products"
    where, args = [], []
    if not hidden:
        where.append("active=1")
    if cid:
        where.append("category_id=?")
        args.append(cid)
    rows = db.q(sql + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC", args)
    if brand:   # фильтр в Python: SQLite lower() не знает кириллицу
        b = brand.lower()
        rows = [r for r in rows
                if not r["models"].strip() or b in [m.strip().lower() for m in r["models"].split(",")]]
    if query:
        w = query.lower().split()
        rows = [r for r in rows if all(x in (r["title"] + " " + r["description"]).lower() for x in w)]
    return rows


def product_text(p):
    return (f"<b>{esc(p['title'])}</b>\n\n{esc(p['description'])}\n\n"
            f"🏍 Подходит: {esc(p['models'] or 'все популярные модели')}\n"
            f"💰 <b>{money(p['price'])}</b>")


async def send_product(bot: Bot, chat_id, pid):
    p = db.one("SELECT * FROM products WHERE id=? AND active=1", (pid,))
    if not p:
        await bot.send_message(chat_id, "😕 Товар недоступен.")
        return
    photos = json.loads(p["photos"] or "[]")
    try:
        if len(photos) == 1:
            await bot.send_photo(chat_id, photos[0])
        elif photos:
            await bot.send_media_group(chat_id, [InputMediaPhoto(media=f) for f in photos[:10]])
    except TelegramBadRequest:
        pass
    await bot.send_message(chat_id, product_text(p), reply_markup=kb([
        [btn("🛒 В корзину", f"a:{pid}")],
        [btn("◀️ Каталог", "cat"), btn("🛒 Корзина", "cart")]]))


# ---------------------------------------------------------------- корзина и заказы
def cart_rows(uid):
    return db.q("SELECT c.id, c.qty, c.brand, c.year, p.title, p.price FROM cart c "
                "JOIN products p ON p.id=c.product_id WHERE c.user_id=? AND p.active=1 "
                "ORDER BY c.id", (uid,))


def cart_view(uid):
    rows = cart_rows(uid)
    if not rows:
        return "🛒 Корзина пуста.\nЗагляни в каталог 🌿", kb([[btn("🛍 В каталог", "cat")]])
    lines, total, keys = ["🛒 <b>Твоя корзина</b>\n"], 0, []
    for i, r in enumerate(rows, 1):
        s = r["price"] * r["qty"]
        total += s
        lines.append(f"{i}. <b>{esc(r['title'])}</b>\n    🏍 {esc(r['brand'])} {esc(r['year'])} · "
                     f"{r['qty']} × {money(r['price'])} = {money(s)}")
        keys.append([btn(f"{i}.", "noop"), btn("➖", f"cq:{r['id']}:-1"),
                     btn(f"{r['qty']} шт", "noop"), btn("➕", f"cq:{r['id']}:1"),
                     btn("❌", f"cd:{r['id']}")])
    lines.append(f"\n💰 <b>Итого: {money(total)}</b>")
    keys.append([btn("✅ Оформить заказ", "checkout")])
    keys.append([btn("🗑 Очистить", "cclear"), btn("🛍 Ещё товары", "cat")])
    return "\n".join(lines), kb(keys)


def order_text(oid, admin=False):
    o = db.one("SELECT * FROM orders WHERE id=?", (oid,))
    items = db.q("SELECT * FROM order_items WHERE order_id=?", (oid,))
    lines = [f"🧾 <b>Заказ #{oid}</b> · {ST_EMOJI.get(o['status'], '')} {o['status']}",
             f"📅 {datetime.fromtimestamp(o['created']):%d.%m.%Y %H:%M}\n"]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {esc(it['title'])} — {esc(it['brand'])} {esc(it['year'])} × {it['qty']} "
                     f"= {money(it['price'] * it['qty'])}")
    lines.append(f"\n💰 <b>Итого: {money(o['total'])}</b>")
    if o["paid_claimed"]:
        lines.append("💳 Клиент отметил оплату")
    lines.append(f"\n👤 {esc(o['name'])}\n📞 {esc(o['phone'])}\n📍 {esc(o['address'])}")
    if o["comment"]:
        lines.append(f"💬 {esc(o['comment'])}")
    if admin:
        u = db.one("SELECT * FROM users WHERE id=?", (o["user_id"],))
        tag = f"@{u['username']}" if u and u["username"] else "без username"
        lines.append(f"🔗 {tag} · <code>{o['user_id']}</code>")
    return "\n".join(lines)


def requisites_text():
    rows = db.q("SELECT * FROM requisites WHERE active=1 ORDER BY id")
    if not rows:
        return "💳 Реквизиты пока не добавлены — напиши менеджеру в разделе «Помощь»."
    out = ["💳 <b>Реквизиты для перевода</b>"]
    for r in rows:
        out.append(f"\n🏦 {esc(r['bank'])}\n💳 <code>{esc(r['number'])}</code>\n👤 {esc(r['holder'])}"
                   + (f"\nℹ️ {esc(r['comment'])}" if r["comment"] else ""))
    return "\n".join(out)


async def notify_admins(bot: Bot, text, markup=None):
    for a in ADMINS:
        try:
            await bot.send_message(a, text, reply_markup=markup)
        except Exception as e:  # админ мог не запускать бота
            logging.warning("не удалось уведомить %s: %s", a, e)


def admin_order_kb(oid, uid):
    st = [btn(f"{ST_EMOJI[s]} {s}", f"x:os:{oid}:{i}") for i, s in enumerate(STATUSES)]
    return kb([st[0:2], st[2:4], st[4:6],
               [btn("✉️ Написать клиенту", f"x:rp:{uid}")],
               [btn("📦 Заказы", "x:ol:-"), btn("🏠 Панель", "x:home")]])


# ---------------------------------------------------------------- состояния
class Checkout(StatesGroup):
    name = State()
    phone = State()
    address = State()
    comment = State()


class Support(StatesGroup):
    msg = State()


class Search(StatesGroup):
    q = State()


class Reply(StatesGroup):
    msg = State()


class NewProduct(StatesGroup):
    title = State()
    description = State()
    price = State()
    models = State()
    photos = State()


class EditProduct(StatesGroup):
    value = State()
    photos = State()


class CatName(StatesGroup):
    value = State()


class NewReq(StatesGroup):
    bank = State()
    number = State()
    holder = State()
    comment = State()


class EditReq(StatesGroup):
    value = State()


class Broadcast(StatesGroup):
    msg = State()


router = Router()
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMINS))
admin.callback_query.filter(F.from_user.id.in_(ADMINS))
fallback = Router()

# ================================================================ ПОКУПАТЕЛЬ
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    db.touch_user(m.from_user)
    await m.answer(WELCOME.format(name=esc(m.from_user.first_name)),
                   reply_markup=main_kb(m.from_user.id))


@router.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено 🌿", reply_markup=main_kb(m.from_user.id))


@router.message(F.text.in_(MENU))
async def menu(m: Message, state: FSMContext):
    await state.clear()
    db.touch_user(m.from_user)
    t = m.text
    if t == BTN_CAT:
        await show_categories(m)
    elif t == BTN_CART:
        text, markup = cart_view(m.from_user.id)
        await m.answer(text, reply_markup=markup)
    elif t == BTN_ORDERS:
        await show_orders(m)
    elif t == BTN_SEARCH:
        await state.set_state(Search.q)
        await m.answer("🔍 Что ищем? Напиши слово из названия (например: <i>KTM</i> или <i>номерные</i>).",
                       reply_markup=cancel_kb())
    elif t == BTN_HELP:
        await m.answer(
            "💬 <b>Помощь</b>\n\n🌿 Выбери товар → марку и год мотоцикла → корзина → оформление.\n"
            "💳 Оплата переводом на реквизиты, после перевода нажми «Я оплатил».\n"
            "🚚 Статусы заказа приходят сюда автоматически.\n\nОстались вопросы? Напиши менеджеру 👇",
            reply_markup=kb([[btn("✉️ Связаться с менеджером", "support")]]))
    elif t == BTN_ADMIN and m.from_user.id in ADMINS:
        await admin_home(m)
    else:
        await m.answer("Отменено 🌿", reply_markup=main_kb(m.from_user.id))


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


# ---- каталог
async def show_categories(obj):
    cats = db.q("SELECT c.id, c.name, COUNT(p.id) n FROM categories c "
                "LEFT JOIN products p ON p.category_id=c.id AND p.active=1 GROUP BY c.id ORDER BY c.id")
    if not db.one("SELECT 1 FROM products WHERE active=1"):
        await respond(obj, "🌿 Каталог пока пуст — скоро добавим новинки!")
        return
    rows = [[btn(f"🌿 {c['name']} ({c['n']})", f"lst:{c['id']}:-:0")] for c in cats if c["n"]]
    rows.append([btn("📦 Весь каталог", "lst:0:-:0"), btn("🏍 По марке", "fb:0")])
    await respond(obj, "🛍 <b>Каталог Roger Store</b>\nВыбери категорию:", kb(rows))


@router.callback_query(F.data == "cat")
async def cb_cat(c: CallbackQuery):
    await show_categories(c)
    await c.answer()


@router.callback_query(F.data.startswith("fb:"))
async def cb_brands(c: CallbackQuery):
    cid = c.data.split(":")[1]
    rows = [[btn(b, f"lst:{cid}:{i}:0") for i, b in enumerate(BRANDS) if i // 2 == k]
            for k in range((len(BRANDS) + 1) // 2)]
    rows.append([btn("◀️ Назад", "cat")])
    await edit(c, "🏍 <b>Выбери марку мотоцикла:</b>", kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("lst:"))
async def cb_list(c: CallbackQuery):
    _, cid, bi, page = c.data.split(":")
    cid, page = int(cid), int(page)
    brand = BRANDS[int(bi)] if bi != "-" else None
    items = get_products(cid, brand)
    cat = db.one("SELECT name FROM categories WHERE id=?", (cid,))
    title = "🌿 <b>" + (cat["name"] if cat else "Весь каталог") + "</b>" + (f" · {brand}" if brand else "")
    if not items:
        await edit(c, f"{title}\n\nПока ничего нет 😕",
                   kb([[btn("🏍 Другая марка", f"fb:{cid}"), btn("◀️ Каталог", "cat")]]))
        return await c.answer()
    rows = [[btn(f"{p['title']} — {money(p['price'])}", f"p:{p['id']}")]
            for p in items[page * PAGE:(page + 1) * PAGE]]
    nav = pager(f"lst:{cid}:{bi}", page, len(items))
    if nav:
        rows.append(nav)
    rows.append([btn("🏍 Марка", f"fb:{cid}"), btn("◀️ Каталог", "cat")])
    await edit(c, f"{title}\nНайдено: {len(items)}", kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("p:"))
async def cb_product(c: CallbackQuery):
    try:
        await c.message.delete()
    except TelegramBadRequest:
        pass
    await send_product(c.bot, c.message.chat.id, int(c.data.split(":")[1]))
    await c.answer()


# ---- поиск
@router.message(Search.q, F.text)
async def search_q(m: Message, state: FSMContext):
    await state.clear()
    items = get_products(query=m.text)[:20]
    await m.answer("Готово 🌿" if items else "Ничего не нашёл 😕", reply_markup=main_kb(m.from_user.id))
    if items:
        await m.answer(f"🔍 Найдено: {len(items)}", reply_markup=kb(
            [[btn(f"{p['title']} — {money(p['price'])}", f"p:{p['id']}")] for p in items]))


# ---- добавление в корзину: марка → год
@router.callback_query(F.data.startswith("a:"))
async def cb_add(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = db.one("SELECT * FROM products WHERE id=? AND active=1", (pid,))
    if not p:
        return await c.answer("Товар недоступен", show_alert=True)
    brands = product_brands(p)
    rows = [[btn(b, f"ab:{pid}:{i}") for i, b in enumerate(brands) if i // 2 == k]
            for k in range((len(brands) + 1) // 2)]
    await c.message.answer("🏍 <b>Для какой марки?</b>", reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("ab:"))
async def cb_brand(c: CallbackQuery):
    _, pid, i = c.data.split(":")
    years = list(range(datetime.now().year + 1, 2009, -1))
    rows = [[btn(str(y), f"ay:{pid}:{i}:{y}") for y in years[k:k + 4]] for k in range(0, len(years), 4)]
    await edit(c, "📅 <b>Год выпуска мотоцикла?</b>", kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("ay:"))
async def cb_year(c: CallbackQuery):
    _, pid, i, year = c.data.split(":")
    p = db.one("SELECT * FROM products WHERE id=? AND active=1", (int(pid),))
    if not p:
        return await c.answer("Товар недоступен", show_alert=True)
    brand = product_brands(p)[int(i)]
    db.run("INSERT INTO cart(user_id,product_id,brand,year,qty) VALUES(?,?,?,?,1) "
           "ON CONFLICT(user_id,product_id,brand,year) DO UPDATE SET qty=qty+1",
           (c.from_user.id, p["id"], brand, year))
    await edit(c, f"✅ Добавлено: <b>{esc(p['title'])}</b>\n🏍 {esc(brand)} {year}",
               kb([[btn("🛒 Корзина", "cart")], [btn("🛍 Продолжить покупки", "cat")]]))
    await c.answer()


# ---- корзина
@router.callback_query(F.data == "cart")
async def cb_cart(c: CallbackQuery):
    text, markup = cart_view(c.from_user.id)
    await edit(c, text, markup)
    await c.answer()


@router.callback_query(F.data.startswith("cq:"))
async def cb_qty(c: CallbackQuery):
    _, cid, d = c.data.split(":")
    row = db.one("SELECT qty FROM cart WHERE id=? AND user_id=?", (int(cid), c.from_user.id))
    if row:
        if row["qty"] + int(d) <= 0:
            db.run("DELETE FROM cart WHERE id=?", (int(cid),))
        else:
            db.run("UPDATE cart SET qty=qty+? WHERE id=?", (int(d), int(cid)))
    text, markup = cart_view(c.from_user.id)
    await edit(c, text, markup)
    await c.answer()


@router.callback_query(F.data.startswith("cd:"))
async def cb_del(c: CallbackQuery):
    db.run("DELETE FROM cart WHERE id=? AND user_id=?", (int(c.data.split(":")[1]), c.from_user.id))
    text, markup = cart_view(c.from_user.id)
    await edit(c, text, markup)
    await c.answer()


@router.callback_query(F.data == "cclear")
async def cb_clear(c: CallbackQuery):
    db.run("DELETE FROM cart WHERE user_id=?", (c.from_user.id,))
    text, markup = cart_view(c.from_user.id)
    await edit(c, text, markup)
    await c.answer()


# ---- оформление заказа
@router.callback_query(F.data == "checkout")
async def cb_checkout(c: CallbackQuery, state: FSMContext):
    if not cart_rows(c.from_user.id):
        return await c.answer("Корзина пуста", show_alert=True)
    await state.set_state(Checkout.name)
    await c.message.answer("📝 <b>Оформление заказа</b>\n\nКак тебя зовут? Напиши ФИО.",
                           reply_markup=cancel_kb())
    await c.answer()


@router.message(Checkout.name, F.text)
async def co_name(m: Message, state: FSMContext):
    if len(m.text.strip()) < 2:
        return await m.answer("Напиши ФИО полностью 🌿")
    await state.update_data(name=m.text.strip())
    await state.set_state(Checkout.phone)
    await m.answer("📞 Номер телефона? Нажми кнопку или напиши вручную.", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)],
                  [KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True))


@router.message(Checkout.phone, F.contact | F.text)
async def co_phone(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else m.text.strip()
    if len(re.sub(r"\D", "", phone)) < 6:
        return await m.answer("Не похоже на номер 🤔 Напиши ещё раз.")
    await state.update_data(phone=phone)
    await state.set_state(Checkout.address)
    await m.answer("📍 Адрес доставки или способ получения?\n"
                   "Напиши город, улицу, дом — или выбери вариант ниже.",
                   reply_markup=cancel_kb("📦 Самовывоз", "🚚 Транспортная компания (укажу в комментарии)"))


@router.message(Checkout.address, F.text)
async def co_address(m: Message, state: FSMContext):
    await state.update_data(address=m.text.strip())
    await state.set_state(Checkout.comment)
    await m.answer("💬 Комментарий к заказу (цвет, номер, пожелания)?", reply_markup=cancel_kb("➡️ Без комментария"))


@router.message(Checkout.comment, F.text)
async def co_comment(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    uid = m.from_user.id
    rows = cart_rows(uid)
    if not rows:
        return await m.answer("Корзина опустела 🤔", reply_markup=main_kb(uid))
    comment = "" if m.text.startswith("➡️") else m.text.strip()
    total = sum(r["price"] * r["qty"] for r in rows)
    oid = db.run("INSERT INTO orders(user_id,name,phone,address,comment,total,status,created) "
                 "VALUES(?,?,?,?,?,?,?,?)",
                 (uid, d["name"], d["phone"], d["address"], comment, total, "Новый",
                  int(datetime.now().timestamp())))
    for r in rows:
        db.run("INSERT INTO order_items(order_id,title,brand,year,price,qty) VALUES(?,?,?,?,?,?)",
               (oid, r["title"], r["brand"], r["year"], r["price"], r["qty"]))
    db.run("DELETE FROM cart WHERE user_id=?", (uid,))
    await m.answer(f"🎉 Заказ <b>#{oid}</b> оформлен!", reply_markup=main_kb(uid))
    await m.answer(f"{order_text(oid)}\n\n{requisites_text()}\n\n"
                   f"Переведи <b>{money(total)}</b> и нажми «Я оплатил» 👇",
                   reply_markup=kb([[btn("✅ Я оплатил", f"paid:{oid}")]]))
    await notify_admins(m.bot, "🔔 <b>Новый заказ!</b>\n\n" + order_text(oid, True),
                        admin_order_kb(oid, uid))


# ---- мои заказы
async def show_orders(obj):
    uid = obj.from_user.id
    rows = db.q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,))
    if not rows:
        return await respond(obj, "📦 Заказов пока нет. Загляни в каталог 🌿")
    await respond(obj, "📦 <b>Твои заказы</b>", kb(
        [[btn(f"#{o['id']} · {ST_EMOJI[o['status']]} {o['status']} · {money(o['total'])}", f"o:{o['id']}")]
         for o in rows]))


@router.callback_query(F.data.startswith("o:"))
async def cb_order(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = db.one("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, c.from_user.id))
    if not o:
        return await c.answer("Заказ не найден", show_alert=True)
    text, rows = order_text(oid), []
    if o["status"] in EARLY:
        if o["paid_claimed"]:
            text += "\n\n⏳ Оплата на проверке у менеджера."
        else:
            text += "\n\n" + requisites_text()
            rows.append([btn("✅ Я оплатил", f"paid:{oid}")])
    rows.append([btn("◀️ Мои заказы", "orders")])
    await edit(c, text, kb(rows))
    await c.answer()


@router.callback_query(F.data == "orders")
async def cb_orders(c: CallbackQuery):
    await show_orders(c)
    await c.answer()


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = db.one("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, c.from_user.id))
    if not o or o["status"] not in EARLY:
        return await c.answer("Заказ не найден", show_alert=True)
    if o["paid_claimed"]:
        return await c.answer("Оплата уже отправлена на проверку ⏳", show_alert=True)
    db.run("UPDATE orders SET paid_claimed=1, status='Ожидает оплаты' WHERE id=?", (oid,))
    await edit(c, order_text(oid) + "\n\n⏳ Спасибо! Оплата отправлена менеджеру на проверку, "
                                    "мы сообщим о статусе здесь.")
    await notify_admins(c.bot, "💰 <b>Клиент нажал «Я оплатил»</b> — проверь поступление!\n\n"
                        + order_text(oid, True),
                        kb([[btn("💚 Оплата получена", f"x:os:{oid}:2")],
                            [btn("📋 Открыть заказ", f"x:o:{oid}")]]))
    await c.answer()


# ---- связь с менеджером
@router.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery, state: FSMContext):
    await state.set_state(Support.msg)
    await c.message.answer("✉️ Напиши сообщение — я передам его менеджеру. Можно прикрепить фото.",
                           reply_markup=cancel_kb())
    await c.answer()


@router.message(Support.msg)
async def support_msg(m: Message, state: FSMContext):
    await state.clear()
    u = m.from_user
    head = (f"✉️ <b>Сообщение клиента</b>\n{esc(u.full_name)}"
            + (f" @{u.username}" if u.username else "") + f" · <code>{u.id}</code>")
    for a in ADMINS:
        try:
            await m.bot.send_message(a, head, reply_markup=kb([[btn("↩️ Ответить", f"x:rp:{u.id}")]]))
            await m.copy_to(a)
        except Exception as e:
            logging.warning("support → %s: %s", a, e)
    await m.answer("✅ Отправлено! Менеджер ответит здесь, в боте.", reply_markup=main_kb(u.id))


# ================================================================ ВЛАДЕЛЕЦ
async def admin_home(obj):
    users = db.one("SELECT COUNT(*) n FROM users")["n"]
    orders = db.one("SELECT COUNT(*) n FROM orders")["n"]
    new = db.one("SELECT COUNT(*) n FROM orders WHERE status IN ('Новый','Ожидает оплаты')")["n"]
    await respond(obj, f"🛠 <b>Панель Roger Store</b>\n\n👥 Пользователей: {users}\n"
                       f"📦 Заказов: {orders} (ждут внимания: {new})", kb([
        [btn("📦 Заказы", "x:ol:-"), btn("🛍 Товары", "x:pl:0")],
        [btn("🗂 Категории", "x:cl"), btn("💳 Реквизиты", "x:rl")],
        [btn("📣 Рассылка", "x:bc")]]))


@admin.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await admin_home(m)


@admin.callback_query(F.data == "x:home")
async def x_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await admin_home(c)
    await c.answer()


# ---- заказы
@admin.callback_query(F.data.startswith("x:ol:"))
async def x_orders(c: CallbackQuery):
    f = c.data.split(":")[2]
    rows = db.q("SELECT * FROM orders " + ("" if f == "-" else "WHERE status=? ")
                + "ORDER BY id DESC LIMIT 25", () if f == "-" else (STATUSES[int(f)],))
    flt = [btn("Все", "x:ol:-")] + [btn(ST_EMOJI[s], f"x:ol:{i}") for i, s in enumerate(STATUSES)]
    keys = [[btn(f"#{o['id']} {ST_EMOJI[o['status']]} {o['name']} · {money(o['total'])}", f"x:o:{o['id']}")]
            for o in rows] + [flt, [btn("🏠 Панель", "x:home")]]
    await edit(c, "📦 <b>Заказы</b>" + (f" · {STATUSES[int(f)]}" if f != "-" else "")
               + ("" if rows else "\n\nПусто."), kb(keys))
    await c.answer()


@admin.callback_query(F.data.startswith("x:o:"))
async def x_order(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    o = db.one("SELECT user_id FROM orders WHERE id=?", (oid,))
    if not o:
        return await c.answer("Нет такого заказа", show_alert=True)
    await edit(c, order_text(oid, True), admin_order_kb(oid, o["user_id"]))
    await c.answer()


@admin.callback_query(F.data.startswith("x:os:"))
async def x_status(c: CallbackQuery):
    _, _, oid, si = c.data.split(":")
    oid, status = int(oid), STATUSES[int(si)]
    o = db.one("SELECT user_id FROM orders WHERE id=?", (oid,))
    if not o:
        return await c.answer("Нет такого заказа", show_alert=True)
    db.run("UPDATE orders SET status=? WHERE id=?", (status, oid))
    await edit(c, order_text(oid, True), admin_order_kb(oid, o["user_id"]))
    try:
        await c.bot.send_message(o["user_id"], f"📦 Заказ <b>#{oid}</b>: статус изменён\n"
                                               f"{ST_EMOJI[status]} <b>{status}</b>")
    except Exception:
        pass
    await c.answer("Статус обновлён, клиент уведомлён")


# ---- ответ клиенту
@admin.callback_query(F.data.startswith("x:rp:"))
async def x_reply(c: CallbackQuery, state: FSMContext):
    await state.set_state(Reply.msg)
    await state.update_data(uid=int(c.data.split(":")[2]))
    await c.message.answer("✍️ Напиши ответ клиенту (/cancel — отмена):")
    await c.answer()


@admin.message(Reply.msg)
async def x_reply_send(m: Message, state: FSMContext):
    uid = (await state.get_data())["uid"]
    await state.clear()
    try:
        await m.bot.send_message(uid, "💬 <b>Ответ менеджера Roger Store:</b>")
        await m.copy_to(uid)
        await m.answer("✅ Отправлено клиенту")
    except Exception:
        await m.answer("😕 Не удалось отправить (клиент мог заблокировать бота)")


# ---- товары
async def plist_view(c: CallbackQuery, page=0):
    items = get_products(hidden=True)
    rows = [[btn(f"{'' if p['active'] else '🙈 '}{p['title']} — {money(p['price'])}", f"x:p:{p['id']}")]
            for p in items[page * PAGE:(page + 1) * PAGE]]
    nav = pager("x:pl", page, len(items))
    if nav:
        rows.append(nav)
    rows += [[btn("➕ Добавить товар", "x:pnew")], [btn("🏠 Панель", "x:home")]]
    await edit(c, f"🛍 <b>Товары</b> ({len(items)})", kb(rows))


@admin.callback_query(F.data.startswith("x:pl:"))
async def x_plist(c: CallbackQuery):
    await plist_view(c, int(c.data.split(":")[2]))
    await c.answer()


def admin_product_view(pid):
    p = db.one("SELECT * FROM products WHERE id=?", (pid,))
    cat = db.one("SELECT name FROM categories WHERE id=?", (p["category_id"],)) if p["category_id"] else None
    text = (f"🛍 <b>#{pid} {esc(p['title'])}</b>\n\n{esc(p['description'])}\n\n"
            f"🗂 {esc(cat['name']) if cat else 'без категории'}\n💰 {money(p['price'])}\n"
            f"🏍 {esc(p['models'] or 'все модели')}\n🖼 Фото: {len(json.loads(p['photos'] or '[]'))}\n"
            f"👁 {'виден' if p['active'] else 'скрыт'}")
    return text, kb([
        [btn("✏️ Название", f"x:pe:{pid}:title"), btn("📝 Описание", f"x:pe:{pid}:description")],
        [btn("💰 Цена", f"x:pe:{pid}:price"), btn("🏍 Модели", f"x:pe:{pid}:models")],
        [btn("🗂 Категория", f"x:pc:{pid}"), btn("🖼 Фото", f"x:pph:{pid}")],
        [btn("🙈 Скрыть/показать", f"x:pt:{pid}"), btn("🗑 Удалить", f"x:pd:{pid}")],
        [btn("◀️ К товарам", "x:pl:0")]])


@admin.callback_query(F.data.startswith("x:p:"))
async def x_product(c: CallbackQuery):
    text, markup = admin_product_view(int(c.data.split(":")[2]))
    await edit(c, text, markup)
    await c.answer()


@admin.callback_query(F.data.startswith("x:pt:"))
async def x_ptoggle(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    db.run("UPDATE products SET active=1-active WHERE id=?", (pid,))
    text, markup = admin_product_view(pid)
    await edit(c, text, markup)
    await c.answer()


@admin.callback_query(F.data.startswith("x:pd:"))
async def x_pdelask(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await edit(c, "🗑 Удалить товар насовсем? Из корзин он тоже пропадёт.",
               kb([[btn("Да, удалить", f"x:pdy:{pid}"), btn("Нет", f"x:p:{pid}")]]))
    await c.answer()


@admin.callback_query(F.data.startswith("x:pdy:"))
async def x_pdel(c: CallbackQuery):
    db.run("DELETE FROM products WHERE id=?", (int(c.data.split(":")[2]),))
    await c.answer("Удалено")
    await plist_view(c)


def parse_models(text):
    if text.strip().lower() in ("все", "-", "all", "любые", "любая"):
        return ""
    canon = {b.lower(): b for b in BRANDS}
    parts = [x.strip() for x in re.split(r"[,;\n]", text) if x.strip()]
    return ",".join(canon.get(x.lower(), x) for x in parts)


PROMPTS = {"title": "✏️ Новое название:", "description": "📝 Новое описание:",
           "price": "💰 Новая цена (число, ₽):",
           "models": "🏍 Совместимые марки через запятую (KTM, Yamaha) или «все»:"}


@admin.callback_query(F.data.startswith("x:pe:"))
async def x_pedit(c: CallbackQuery, state: FSMContext):
    _, _, pid, field = c.data.split(":")
    await state.set_state(EditProduct.value)
    await state.update_data(pid=int(pid), field=field)
    await c.message.answer(PROMPTS[field] + "\n(/cancel — отмена)")
    await c.answer()


@admin.message(EditProduct.value, F.text)
async def x_pedit_val(m: Message, state: FSMContext):
    d = await state.get_data()
    f, text = d["field"], m.text.strip()
    if f == "price":
        if not text.isdigit():
            return await m.answer("Нужно целое число, например 4500")
        val = int(text)
    elif f == "models":
        val = parse_models(text)
    else:
        val = text
    db.run(f"UPDATE products SET {f}=? WHERE id=?", (val, d["pid"]))   # f из белого списка PROMPTS
    await state.clear()
    t, markup = admin_product_view(d["pid"])
    await m.answer("✅ Сохранено\n\n" + t, reply_markup=markup)


@admin.callback_query(F.data.startswith("x:pc:"))
async def x_pcat(c: CallbackQuery):
    pid = c.data.split(":")[2]
    cats = db.q("SELECT * FROM categories ORDER BY id")
    await edit(c, "🗂 Выбери категорию:", kb(
        [[btn(k["name"], f"x:pcs:{pid}:{k['id']}")] for k in cats] + [[btn("◀️ Назад", f"x:p:{pid}")]]))
    await c.answer()


@admin.callback_query(F.data.startswith("x:pcs:"))
async def x_pcat_set(c: CallbackQuery):
    _, _, pid, cid = c.data.split(":")
    db.run("UPDATE products SET category_id=? WHERE id=?", (int(cid), int(pid)))
    text, markup = admin_product_view(int(pid))
    await edit(c, text, markup)
    await c.answer("Категория изменена")


# замена фото существующего товара
@admin.callback_query(F.data.startswith("x:pph:"))
async def x_pphotos(c: CallbackQuery, state: FSMContext):
    await state.set_state(EditProduct.photos)
    await state.update_data(pid=int(c.data.split(":")[2]), photos=[])
    await c.message.answer("🖼 Пришли новые фото (по одному или несколько). Старые будут заменены.\n"
                           "Когда закончишь — «Готово».", reply_markup=kb([[btn("✅ Готово", "x:pdone")]]))
    await c.answer()


# добавление нового товара
@admin.callback_query(F.data == "x:pnew")
async def x_pnew(c: CallbackQuery, state: FSMContext):
    cats = db.q("SELECT * FROM categories ORDER BY id")
    if not cats:
        return await c.answer("Сначала создай категорию", show_alert=True)
    await state.clear()
    await edit(c, "➕ <b>Новый товар</b>\n\nВ какую категорию?",
               kb([[btn(k["name"], f"x:nc:{k['id']}")] for k in cats] + [[btn("❌ Отмена", "x:home")]]))
    await c.answer()


@admin.callback_query(F.data.startswith("x:nc:"))
async def x_ncat(c: CallbackQuery, state: FSMContext):
    await state.set_state(NewProduct.title)
    await state.update_data(cid=int(c.data.split(":")[2]), photos=[])
    await c.message.answer("✏️ Название товара? (/cancel — отмена)")
    await c.answer()


@admin.message(NewProduct.title, F.text)
async def np_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(NewProduct.description)
    await m.answer("📝 Подробное описание:")


@admin.message(NewProduct.description, F.text)
async def np_desc(m: Message, state: FSMContext):
    await state.update_data(description=m.text.strip())
    await state.set_state(NewProduct.price)
    await m.answer("💰 Цена в ₽ (число):")


@admin.message(NewProduct.price, F.text)
async def np_price(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("Нужно целое число, например 4500")
    await state.update_data(price=int(m.text.strip()))
    await state.set_state(NewProduct.models)
    await m.answer("🏍 Совместимые марки через запятую или «все».\nИзвестные: " + ", ".join(BRANDS))


@admin.message(NewProduct.models, F.text)
async def np_models(m: Message, state: FSMContext):
    await state.update_data(models=parse_models(m.text))
    await state.set_state(NewProduct.photos)
    await m.answer("🖼 Пришли фото товара (можно несколько). Потом нажми «Готово».",
                   reply_markup=kb([[btn("✅ Готово", "x:pdone")]]))


@admin.message(StateFilter(NewProduct.photos, EditProduct.photos), F.photo)
async def np_photo(m: Message, state: FSMContext):
    photos = (await state.get_data()).get("photos", [])
    photos.append(m.photo[-1].file_id)
    await state.update_data(photos=photos)
    await m.answer(f"📷 Принято фото: {len(photos)}", reply_markup=kb([[btn("✅ Готово", "x:pdone")]]))


@admin.callback_query(F.data == "x:pdone")
async def x_pdone(c: CallbackQuery, state: FSMContext):
    cur, d = await state.get_state(), await state.get_data()
    if cur == NewProduct.photos.state:
        pid = db.run("INSERT INTO products(category_id,title,description,price,models,photos) "
                     "VALUES(?,?,?,?,?,?)",
                     (d["cid"], d["title"], d["description"], d["price"], d["models"],
                      json.dumps(d["photos"])))
    elif cur == EditProduct.photos.state:
        pid = d["pid"]
        db.run("UPDATE products SET photos=? WHERE id=?", (json.dumps(d["photos"]), pid))
    else:
        return await c.answer("Нечего сохранять", show_alert=True)
    await state.clear()
    text, markup = admin_product_view(pid)
    await c.message.answer("✅ Готово!\n\n" + text, reply_markup=markup)
    await c.answer()


# ---- категории
async def cats_view(c: CallbackQuery):
    cats = db.q("SELECT c.id, c.name, COUNT(p.id) n FROM categories c "
                "LEFT JOIN products p ON p.category_id=c.id GROUP BY c.id ORDER BY c.id")
    await edit(c, "🗂 <b>Категории</b>", kb(
        [[btn(f"{k['name']} ({k['n']})", f"x:c:{k['id']}")] for k in cats]
        + [[btn("➕ Добавить", "x:cnew")], [btn("🏠 Панель", "x:home")]]))


@admin.callback_query(F.data == "x:cl")
async def x_cats(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await cats_view(c)
    await c.answer()


@admin.callback_query(F.data.startswith("x:c:"))
async def x_cat(c: CallbackQuery):
    cid = c.data.split(":")[2]
    k = db.one("SELECT name FROM categories WHERE id=?", (int(cid),))
    await edit(c, f"🗂 <b>{esc(k['name'])}</b>", kb([
        [btn("✏️ Переименовать", f"x:cr:{cid}"), btn("🗑 Удалить", f"x:cd:{cid}")],
        [btn("◀️ Категории", "x:cl")]]))
    await c.answer()


@admin.callback_query(F.data == "x:cnew")
async def x_cnew(c: CallbackQuery, state: FSMContext):
    await state.set_state(CatName.value)
    await state.update_data(cid=0)
    await c.message.answer("➕ Название новой категории: (/cancel — отмена)")
    await c.answer()


@admin.callback_query(F.data.startswith("x:cr:"))
async def x_crename(c: CallbackQuery, state: FSMContext):
    await state.set_state(CatName.value)
    await state.update_data(cid=int(c.data.split(":")[2]))
    await c.message.answer("✏️ Новое название категории: (/cancel — отмена)")
    await c.answer()


@admin.message(CatName.value, F.text)
async def x_cname(m: Message, state: FSMContext):
    cid = (await state.get_data())["cid"]
    name = m.text.strip()
    try:
        if cid:
            db.run("UPDATE categories SET name=? WHERE id=?", (name, cid))
        else:
            db.run("INSERT INTO categories(name) VALUES(?)", (name,))
    except Exception:
        return await m.answer("Такая категория уже есть, придумай другое название")
    await state.clear()
    await m.answer("✅ Готово", reply_markup=kb([[btn("🗂 Категории", "x:cl")]]))


@admin.callback_query(F.data.startswith("x:cd:"))
async def x_cdel(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    if db.one("SELECT 1 FROM products WHERE category_id=?", (cid,)):
        return await c.answer("В категории есть товары — сначала перенеси или удали их", show_alert=True)
    db.run("DELETE FROM categories WHERE id=?", (cid,))
    await c.answer("Удалено")
    await cats_view(c)


# ---- реквизиты
@admin.callback_query(F.data == "x:rl")
async def x_reqs(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = db.q("SELECT * FROM requisites ORDER BY id")
    await edit(c, "💳 <b>Реквизиты для оплаты</b>\nКлиенты видят все включённые (🟢).", kb(
        [[btn(f"{'🟢' if r['active'] else '⚪'} {r['bank']} · {r['number']}", f"x:r:{r['id']}")] for r in rows]
        + [[btn("➕ Добавить реквизиты", "x:rnew")], [btn("🏠 Панель", "x:home")]]))
    await c.answer()


def req_view(rid):
    r = db.one("SELECT * FROM requisites WHERE id=?", (rid,))
    text = (f"💳 <b>Реквизиты #{rid}</b> {'🟢 включены' if r['active'] else '⚪ выключены'}\n\n"
            f"🏦 {esc(r['bank'])}\n💳 <code>{esc(r['number'])}</code>\n👤 {esc(r['holder'])}\n"
            f"ℹ️ {esc(r['comment']) or '—'}")
    return text, kb([
        [btn("🏦 Банк", f"x:re:{rid}:bank"), btn("💳 Номер", f"x:re:{rid}:number")],
        [btn("👤 Получатель", f"x:re:{rid}:holder"), btn("ℹ️ Комментарий", f"x:re:{rid}:comment")],
        [btn("🔁 Вкл/выкл", f"x:rt:{rid}"), btn("🗑 Удалить", f"x:rd:{rid}")],
        [btn("◀️ Реквизиты", "x:rl")]])


@admin.callback_query(F.data.startswith("x:r:"))
async def x_req(c: CallbackQuery):
    text, markup = req_view(int(c.data.split(":")[2]))
    await edit(c, text, markup)
    await c.answer()


@admin.callback_query(F.data.startswith("x:rt:"))
async def x_rtoggle(c: CallbackQuery):
    rid = int(c.data.split(":")[2])
    db.run("UPDATE requisites SET active=1-active WHERE id=?", (rid,))
    text, markup = req_view(rid)
    await edit(c, text, markup)
    await c.answer()


@admin.callback_query(F.data.startswith("x:rd:"))
async def x_rdel(c: CallbackQuery, state: FSMContext):
    db.run("DELETE FROM requisites WHERE id=?", (int(c.data.split(":")[2]),))
    await x_reqs(c, state)


@admin.callback_query(F.data == "x:rnew")
async def x_rnew(c: CallbackQuery, state: FSMContext):
    await state.set_state(NewReq.bank)
    await c.message.answer("🏦 Название банка? (/cancel — отмена)")
    await c.answer()


@admin.message(NewReq.bank, F.text)
async def nr_bank(m: Message, state: FSMContext):
    await state.update_data(bank=m.text.strip())
    await state.set_state(NewReq.number)
    await m.answer("💳 Номер карты / счёта:")


@admin.message(NewReq.number, F.text)
async def nr_number(m: Message, state: FSMContext):
    await state.update_data(number=m.text.strip())
    await state.set_state(NewReq.holder)
    await m.answer("👤 ФИО получателя:")


@admin.message(NewReq.holder, F.text)
async def nr_holder(m: Message, state: FSMContext):
    await state.update_data(holder=m.text.strip())
    await state.set_state(NewReq.comment)
    await m.answer("ℹ️ Комментарий для клиента (или «-», если не нужен):")


@admin.message(NewReq.comment, F.text)
async def nr_comment(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    c = "" if m.text.strip() == "-" else m.text.strip()
    rid = db.run("INSERT INTO requisites(bank,number,holder,comment) VALUES(?,?,?,?)",
                 (d["bank"], d["number"], d["holder"], c))
    text, markup = req_view(rid)
    await m.answer("✅ Реквизиты добавлены\n\n" + text, reply_markup=markup)


@admin.callback_query(F.data.startswith("x:re:"))
async def x_redit(c: CallbackQuery, state: FSMContext):
    _, _, rid, field = c.data.split(":")
    await state.set_state(EditReq.value)
    await state.update_data(rid=int(rid), field=field)
    await c.message.answer("✏️ Новое значение (для комментария «-» очистит): (/cancel — отмена)")
    await c.answer()


@admin.message(EditReq.value, F.text)
async def er_value(m: Message, state: FSMContext):
    d = await state.get_data()
    if d["field"] not in ("bank", "number", "holder", "comment"):
        return await state.clear()
    val = "" if m.text.strip() == "-" and d["field"] == "comment" else m.text.strip()
    db.run(f"UPDATE requisites SET {d['field']}=? WHERE id=?", (val, d["rid"]))
    await state.clear()
    text, markup = req_view(d["rid"])
    await m.answer("✅ Сохранено\n\n" + text, reply_markup=markup)


# ---- рассылка
@admin.callback_query(F.data == "x:bc")
async def x_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.msg)
    n = db.one("SELECT COUNT(*) n FROM users")["n"]
    await c.message.answer(f"📣 Пришли сообщение для рассылки ({n} получателей): текст или фото с подписью. "
                           "(/cancel — отмена)")
    await c.answer()


@admin.message(Broadcast.msg)
async def x_bc_msg(m: Message, state: FSMContext):
    await state.update_data(mid=m.message_id)
    await m.answer("👆 Так увидят клиенты. Отправляем всем?",
                   reply_markup=kb([[btn("🚀 Отправить", "x:bcy"), btn("❌ Отмена", "x:home")]]))


@admin.callback_query(F.data == "x:bcy")
async def x_bc_go(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "mid" not in d:
        return await c.answer("Сначала пришли сообщение", show_alert=True)
    await state.clear()
    await c.answer("Отправляю…")
    ok = bad = 0
    for u in db.q("SELECT id FROM users"):
        try:
            await c.bot.copy_message(u["id"], c.message.chat.id, d["mid"])
            ok += 1
        except Exception:
            bad += 1
        await asyncio.sleep(0.05)
    await c.message.answer(f"📣 Рассылка завершена: доставлено {ok}, не доставлено {bad}")


# ================================================================ прочее
@fallback.message(StateFilter(None))
async def unknown(m: Message):
    await m.answer("Выбери раздел в меню 👇 🌿", reply_markup=main_kb(m.from_user.id))


async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        sys.exit("Нет токена: впиши SHOP_TOKEN=... в roger-store/.env (см. .env.example)")
    if not ADMINS:
        logging.warning("ADMIN_IDS не задан — админ-панель никому не доступна")
    db.init()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(router, admin, fallback)
    from aiogram.types import BotCommand
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню"),
                               BotCommand(command="cancel", description="Отмена действия")])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
