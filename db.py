"""SQLite-хранилище Roger Store: товары, категории, корзина, заказы, реквизиты."""
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SHOP_DB", os.path.join(HERE, "shop.db"))

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys=ON")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT, username TEXT, joined INTEGER);
CREATE TABLE IF NOT EXISTS categories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  title TEXT NOT NULL, description TEXT DEFAULT '', price INTEGER NOT NULL,
  models TEXT DEFAULT '',          -- марки через запятую, пусто = подходит всем
  photos TEXT DEFAULT '[]',        -- JSON-список file_id
  active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS cart(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  brand TEXT, year TEXT, qty INTEGER DEFAULT 1,
  UNIQUE(user_id, product_id, brand, year));
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  name TEXT, phone TEXT, address TEXT, comment TEXT,
  total INTEGER, status TEXT, paid_claimed INTEGER DEFAULT 0, created INTEGER);
CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  title TEXT, brand TEXT, year TEXT, price INTEGER, qty INTEGER);
CREATE TABLE IF NOT EXISTS requisites(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bank TEXT, number TEXT, holder TEXT, comment TEXT DEFAULT '', active INTEGER DEFAULT 1);
"""

DEFAULT_CATEGORIES = ["Full Graphics Kit", "Number Plates", "Frame Protection",
                      "Fork Guards", "Swingarm", "Custom"]


def init():
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
        conn.executemany("INSERT INTO categories(name) VALUES(?)",
                         [(c,) for c in DEFAULT_CATEGORIES])
    conn.commit()


def q(sql, args=()):
    return conn.execute(sql, args).fetchall()


def one(sql, args=()):
    return conn.execute(sql, args).fetchone()


def run(sql, args=()):
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.lastrowid


def touch_user(u):
    conn.execute(
        "INSERT INTO users(id,name,username,joined) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
        (u.id, u.full_name, u.username, int(time.time())))
    conn.commit()
