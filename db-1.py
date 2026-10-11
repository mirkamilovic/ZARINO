import os
import time
import aiosqlite
from .config import DB_PATH

_db: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT, username TEXT, wallet TEXT DEFAULT '',
    balance INTEGER DEFAULT 0, credits INTEGER DEFAULT 0, vip_until INTEGER DEFAULT 0,
    ref_by INTEGER, ref_count INTEGER DEFAULT 0, joined INTEGER, banned INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY, title TEXT, username TEXT, group_id INTEGER, owner_id INTEGER
);
CREATE TABLE IF NOT EXISTS battles(
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, channel_id INTEGER, title TEXT,
    rules TEXT, points INTEGER DEFAULT 1, end_ts INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
    post_id INTEGER, created INTEGER
);
CREATE TABLE IF NOT EXISTS participants(
    battle_id INTEGER, user_id INTEGER, nick TEXT, points INTEGER DEFAULT 0,
    PRIMARY KEY(battle_id, user_id)
);
CREATE TABLE IF NOT EXISTS rounds(
    id INTEGER PRIMARY KEY AUTOINCREMENT, battle_id INTEGER, n INTEGER, points INTEGER,
    post_id INTEGER, group_id INTEGER, thread_id INTEGER, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS scored(
    round_id INTEGER, user_id INTEGER, PRIMARY KEY(round_id, user_id)
);
CREATE TABLE IF NOT EXISTS plog(
    id INTEGER PRIMARY KEY AUTOINCREMENT, battle_id INTEGER, user_id INTEGER,
    delta INTEGER, reason TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT, descr TEXT DEFAULT '',
    price_stars INTEGER, payload TEXT DEFAULT '', active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, item_id INTEGER DEFAULT 0,
    stars INTEGER, ton REAL, method TEXT DEFAULT '', status TEXT DEFAULT 'pending',
    code TEXT, charge TEXT, ts INTEGER, uzs INTEGER DEFAULT 0, meta TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS games(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, emoji TEXT DEFAULT '🎮',
    server INTEGER DEFAULT 0, id_label TEXT DEFAULT 'Player ID', active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS packs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER, title TEXT,
    price_uzs INTEGER DEFAULT 0, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS gids(
    user_id INTEGER, game_id INTEGER, pid TEXT, server TEXT DEFAULT '', PRIMARY KEY(user_id, game_id)
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_rounds_thread ON rounds(group_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_rounds_post ON rounds(post_id);
"""

DEFAULTS = {
    "brand": "Battle Hub", "battle_fee": 25, "vip_price": 500, "vip_days": 30,
    "ton_rate": 200, "markup": 15, "ref_per_credit": 3, "ton_address": "", "force_sub": "",
    "uzs_rate": 230, "card_number": "", "card_holder": "", "provider_token": "",
}


async def init():
    global _db
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(SCHEMA)
    # eski bazalar uchun migratsiya (Railway'dagi mavjud baza o'chib ketmaydi)
    cols = [r["name"] for r in await q("PRAGMA table_info(orders)")]
    if "uzs" not in cols:
        await _db.execute("ALTER TABLE orders ADD COLUMN uzs INTEGER DEFAULT 0")
    if "meta" not in cols:
        await _db.execute("ALTER TABLE orders ADD COLUMN meta TEXT DEFAULT ''")
    for k, v in DEFAULTS.items():
        await _db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, str(v)))
    await _db.commit()
    if not await one("SELECT 1 FROM games LIMIT 1"):
        # Paket nomlari tayyor, NARXLAR yo'q — owner botning ichidan narx qo'yguncha foydalanuvchiga ko'rinmaydi
        seed = [("PUBG Mobile", "🔫", 0, ["60 UC", "325 UC", "660 UC", "1800 UC", "3850 UC"]),
                ("Free Fire", "🔥", 0, ["100 Almaz", "310 Almaz", "520 Almaz", "1060 Almaz", "2180 Almaz"]),
                ("Mobile Legends", "⚔️", 1, ["86 Almaz", "172 Almaz", "257 Almaz", "344 Almaz", "706 Almaz"])]
        for name, emoji, server, titles in seed:
            gid, _ = await run("INSERT INTO games(name,emoji,server,id_label) VALUES(?,?,?,?)",
                               name, emoji, server, "User ID" if server else "Player ID")
            for t in titles:
                await run("INSERT INTO packs(game_id,title,price_uzs) VALUES(?,?,0)", gid, t)
    if not await one("SELECT 1 FROM items LIMIT 1"):
        for m, st in ((3, 1000), (6, 1500), (12, 2500)):
            await run("INSERT INTO items(kind,title,descr,price_stars,payload) VALUES('premium',?,?,?,?)",
                      f"💎 Telegram Premium — {m} oy", "Premium obuna to'g'ridan-to'g'ri hisobingizga sovg'a qilinadi",
                      int(st * 1.15), str(m))
        for n in (50, 100, 250, 500, 1000):
            await run("INSERT INTO items(kind,title,descr,price_stars,payload) VALUES('stars',?,?,?,?)",
                      f"⭐ {n} Stars", "TON orqali to'lanadi, owner tomonidan qo'lda yetkaziladi",
                      int(n * 1.3), str(n))


async def close():
    if _db:
        await _db.close()


async def q(sql, *a):
    async with _db.execute(sql, a) as c:
        return await c.fetchall()


async def one(sql, *a):
    rows = await q(sql, *a)
    return rows[0] if rows else None


async def run(sql, *a):
    async with _db.execute(sql, a) as c:
        res = (c.lastrowid, c.rowcount)
    await _db.commit()
    return res


async def S(key, default=""):
    row = await one("SELECT value FROM settings WHERE key=?", key)
    return row["value"] if row and row["value"] is not None else default


async def SI(key, default=0):
    try:
        return int(float(await S(key, str(default))))
    except ValueError:
        return default


async def set_s(key, value):
    await run("INSERT OR REPLACE INTO settings VALUES(?,?)", key, str(value))


async def upsert_user(u) -> bool:
    """Foydalanuvchini saqlaydi. Yangi bo'lsa True qaytaradi."""
    row = await one("SELECT id FROM users WHERE id=?", u.id)
    if row:
        await run("UPDATE users SET name=?, username=? WHERE id=?", u.full_name, u.username, u.id)
        return False
    await run("INSERT INTO users(id,name,username,joined) VALUES(?,?,?,?)",
              u.id, u.full_name, u.username, int(time.time()))
    return True
