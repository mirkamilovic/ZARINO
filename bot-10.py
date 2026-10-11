"""
Battle Hub — Telegram batl boti (bitta fayl).  Mini App: index.html (shu fayl bilan bir papkada turishi kerak)

O'rnatish:   pip install "aiogram>=3.22" aiosqlite aiohttp Pillow python-dotenv
Sozlash:     shu papkada .env fayl yarating:
                 BOT_TOKEN=123456:ABC...      (@BotFather'dan)
                 OWNER_ID=123456789           (sizning Telegram ID'ingiz)
                 WEBAPP_URL=https://sizning-domen.uz   (ixtiyoriy, Mini App uchun ochiq HTTPS)
                 PORT=8080
                 DB_PATH=data/bot.db
                 TZ_OFFSET=5
                 TONCENTER_KEY=               (ixtiyoriy)
Ishga tushirish:   python bot.py

Muhim: bot kanalda ham, izoh (muhokama) guruhida ham admin bo'lishi kerak; @BotFather'da /setprivacy -> Disable.
"""
import asyncio
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from html import escape as esc
from urllib.parse import parse_qsl

import aiohttp
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, BaseMiddleware, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardMarkup as M,
    InlineKeyboardButton as IB, WebAppInfo, BotCommand, BotCommandScopeChat,
    BotCommandScopeAllPrivateChats, PreCheckoutQuery, LabeledPrice,
)
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont


##############################################################################
# app/config.py
##############################################################################
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
TZ = int(os.getenv("TZ_OFFSET", "5"))
TONCENTER_KEY = os.getenv("TONCENTER_KEY", "")


##############################################################################
# app/db.py
##############################################################################
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
    code TEXT, charge TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_rounds_thread ON rounds(group_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_rounds_post ON rounds(post_id);
"""

DEFAULTS = {
    "brand": "Battle Hub", "battle_fee": 25, "vip_price": 500, "vip_days": 30,
    "ton_rate": 200, "markup": 15, "ref_per_credit": 3, "ton_address": "", "force_sub": "",
}


async def db_init():
    global _db
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(SCHEMA)
    for k, v in DEFAULTS.items():
        await _db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, str(v)))
    await _db.commit()
    if not await one("SELECT 1 FROM items LIMIT 1"):
        for m, st in ((3, 1000), (6, 1500), (12, 2500)):
            await run("INSERT INTO items(kind,title,descr,price_stars,payload) VALUES('premium',?,?,?,?)",
                      f"💎 Telegram Premium — {m} oy", "Premium obuna to'g'ridan-to'g'ri hisobingizga sovg'a qilinadi",
                      int(st * 1.15), str(m))
        for n in (50, 100, 250, 500, 1000):
            await run("INSERT INTO items(kind,title,descr,price_stars,payload) VALUES('stars',?,?,?,?)",
                      f"⭐ {n} Stars", "TON orqali to'lanadi, owner tomonidan qo'lda yetkaziladi",
                      int(n * 1.3), str(n))


async def db_close():
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


##############################################################################
# app/assets.py
##############################################################################
DIR = "data/img"
W, H = 1280, 640

THEMES = {
    "home": ("Battle Hub", "Raqobat. Ball. G'alaba.", ((88, 28, 235), (236, 72, 153))),
    "battle": ("Batllar", "Qatnash va g'olib bo'l", ((14, 165, 233), (99, 102, 241))),
    "shop": ("Do'kon", "Stars - Premium - Giftlar - NFT", ((245, 158, 11), (239, 68, 68))),
    "wallet": ("Hamyon", "Balans va TON ulash", ((16, 185, 129), (6, 95, 70))),
    "profile": ("Profil", "Sening natijalaring", ((59, 130, 246), (139, 92, 246))),
    "owner": ("Owner panel", "Boshqaruv markazi", ((30, 41, 59), (100, 116, 139))),
    "vip": ("VIP", "Cheksiz batllar", ((234, 179, 8), (180, 83, 9))),
    "ref": ("Taklif", "Do'st chaqir - bonus ol", ((236, 72, 153), (244, 63, 94))),
    "top": ("Reyting", "Eng kuchlilar", ((168, 85, 247), (37, 99, 235))),
}

FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _font(size):
    for p in FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _make(key, title, sub, colors):
    c1, c2 = colors
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for x in range(W):
        d.line([(x, 0), (x, H)], fill=_lerp(c1, c2, x / W))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.ellipse((W - 520, -200, W + 120, 440), fill=(255, 255, 255, 38))
    od.ellipse((W - 300, 260, W + 200, 760), fill=(255, 255, 255, 26))
    od.ellipse((-160, H - 220, 380, H + 300), fill=(0, 0, 0, 40))
    od.rounded_rectangle((90, 200, 110, 440), 10, fill=(255, 255, 255, 220))
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(img)
    d.text((140, 190), title, font=_font(120), fill="white")
    d.text((144, 350), sub, font=_font(44), fill=(255, 255, 255))
    path = os.path.join(DIR, f"{key}.jpg")
    img.save(path, quality=90)


def make_banners():
    os.makedirs(DIR, exist_ok=True)
    for key, (t, s, c) in THEMES.items():
        if not os.path.exists(os.path.join(DIR, f"{key}.jpg")):
            _make(key, t, s, c)


##############################################################################
# app/ui.py
##############################################################################
log = logging.getLogger(__name__)


async def screen(ev, key, text, kb=None):
    """Bitta toza ekran: eski xabarni o'chirib, rasm bilan yangisini yuboradi."""
    bot = ev.bot
    if isinstance(ev, CallbackQuery):
        chat_id = ev.message.chat.id
        try:
            await ev.answer()
        except Exception:
            pass
        try:
            await ev.message.delete()
        except Exception:
            pass
    else:
        chat_id = ev.chat.id
    fid = await S(f"img_{key}")
    path = os.path.join(DIR, f"{key}.jpg")
    if len(text) <= 1000 and (fid or os.path.exists(path)):
        try:
            msg = await bot.send_photo(chat_id, fid or FSInputFile(path), caption=text, reply_markup=kb)
            if not fid:
                await set_s(f"img_{key}", msg.photo[-1].file_id)
            return msg
        except Exception as e:
            log.warning("rasm yuborilmadi (%s): %s", key, e)
            if fid:
                await set_s(f"img_{key}", "")
    return await bot.send_message(chat_id, text, reply_markup=kb)


##############################################################################
# app/kb.py
##############################################################################
def B(text, cb=None, url=None, web=None):
    if web:
        return IB(text=text, web_app=WebAppInfo(url=web))
    if url:
        return IB(text=text, url=url)
    return IB(text=text, callback_data=cb)


def K(*rows):
    return M(inline_keyboard=[list(r) for r in rows])


HOME = B("🏠 Menyu", "m:home")


def home_kb(uid):
    rows = [
        [B("🔥 Faol batllar", "m:battles"), B("🛍 Do'kon", "m:shop")],
        [B("⚔️ Batl yaratish", "m:new"), B("📂 Batllarim", "m:my")],
        [B("👛 Hamyon", "m:wallet"), B("👤 Profil", "m:profile")],
        [B("👑 VIP", "m:vip"), B("🎁 Do'st taklif qilish", "m:ref")],
    ]
    if WEBAPP_URL:
        rows.insert(0, [B("📱 Mini App ochish", web=WEBAPP_URL + "/app")])
    if uid == OWNER_ID:
        rows.append([B("🛠 Owner panel", "o:home")])
    return K(*rows)


def back(cb="m:home"):
    return K([B("⬅️ Orqaga", cb), HOME])


def cancel_kb():
    return K([B("✖️ Bekor qilish", "x:cancel")])


##############################################################################
# app/states.py
##############################################################################
class Link(StatesGroup):
    ch = State()


class NewB(StatesGroup):
    ch = State()
    title = State()
    rules = State()
    points = State()
    end = State()


class Join(StatesGroup):
    nick = State()


class Rnd(StatesGroup):
    text = State()
    points = State()


class Pts(StatesGroup):
    amount = State()


class Wal(StatesGroup):
    addr = State()


class OSet(StatesGroup):
    val = State()


class OImg(StatesGroup):
    photo = State()


class OItem(StatesGroup):
    kind = State()
    title = State()
    descr = State()
    price = State()
    payload = State()


class OBc(StatesGroup):
    msg = State()


##############################################################################
# app/services.py
##############################################################################
log = logging.getLogger(__name__)
_uname = None
MEDALS = ["🥇", "🥈", "🥉"]
PREMIUM_STARS = {3: 1000, 6: 1500, 12: 2500}


async def bot_username(bot: Bot) -> str:
    global _uname
    if not _uname:
        _uname = (await bot.me()).username
    return _uname


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=TZ))).strftime("%d.%m.%Y %H:%M")


def is_vip(u) -> bool:
    return bool(u and u["vip_until"] and u["vip_until"] > time.time())


# ------------------------------------------------------------------ batl

def mention(r) -> str:
    s = f'<a href="tg://user?id={r["user_id"]}">{esc(r["nick"])}</a>'
    if r["username"]:
        s += f' (@{r["username"]})'
    return s


async def leaderboard(bid: int):
    return await q(
        "SELECT p.user_id,p.nick,p.points,u.username FROM participants p "
        "LEFT JOIN users u ON u.id=p.user_id WHERE p.battle_id=? ORDER BY p.points DESC, p.nick", bid)


def lb_text(rows, limit=40) -> str:
    lines = []
    for i, r in enumerate(rows[:limit]):
        mark = MEDALS[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} {mention(r)} — <b>{r['points']}</b> ball")
    if len(rows) > limit:
        lines.append(f"… va yana {len(rows) - limit} ishtirokchi")
    return "\n".join(lines)


async def post_text(b) -> str:
    cnt = (await one("SELECT COUNT(*) c FROM participants WHERE battle_id=?", b["id"]))["c"]
    names = await q("SELECT nick FROM participants WHERE battle_id=? ORDER BY rowid LIMIT 40", b["id"])
    ended = b["status"] != "active"
    ends = "🏁 Yakunlangan" if ended else ("⏰ Tugash: " + fmt_ts(b["end_ts"]) if b["end_ts"] else "⏰ Tugash: yaratuvchi hal qiladi")
    lines = [f"⚔️ <b>{esc(b['title'])}</b>", "", "📜 <b>Qoidalar</b>", esc(b["rules"]), "",
             f"💎 Har bir raundda 1 ta izoh = <b>{b['points']} ball</b>",
             "💬 Bir foydalanuvchidan faqat 1 ta izoh hisoblanadi", ends, "", f"👥 <b>Ishtirokchilar ({cnt})</b>"]
    for i, n in enumerate(names, 1):
        lines.append(f"{i}. {esc(n['nick'])}")
    if cnt > 40:
        lines.append(f"… va yana {cnt - 40} ta")
    return "\n".join(lines)[:4000]


async def join_markup(bot, bid) -> InlineKeyboardMarkup:
    u = await bot_username(bot)
    return K([B("✅ Qatnashish", url=f"https://t.me/{u}?start=join_{bid}")])


async def update_post(bot: Bot, bid: int):
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not b or not b["post_id"]:
        return
    markup = await join_markup(bot, bid) if b["status"] == "active" else None
    try:
        await bot.edit_message_text(await post_text(b), chat_id=b["channel_id"], message_id=b["post_id"],
                                    reply_markup=markup)
    except Exception as e:
        log.debug("post yangilanmadi: %s", e)


async def finish_battle(bot: Bot, bid: int) -> bool:
    _, rc = await run("UPDATE battles SET status='ended' WHERE id=? AND status='active'", bid)
    if not rc:
        return False
    await run("UPDATE rounds SET active=0 WHERE battle_id=?", bid)
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    rows = await leaderboard(bid)
    text = (f"🏁 <b>{esc(b['title'])}</b> — batl yakunlandi!\n\n🏆 <b>Natijalar</b>\n\n"
            + (lb_text(rows) or "Ishtirokchilar bo'lmadi"))
    try:
        await bot.send_message(b["channel_id"], text)
    except Exception as e:
        log.warning("natija kanalga yuborilmadi: %s", e)
        try:
            await bot.send_message(b["owner_id"], "⚠️ Natijani kanalga yuborib bo'lmadi, botning huquqlarini tekshiring.\n\n" + text)
        except Exception:
            pass
    await update_post(bot, bid)
    try:
        await bot.send_message(b["owner_id"], f"✅ «{esc(b['title'])}» batli yakunlandi, natijalar kanalga yuborildi.")
    except Exception:
        pass
    return True


async def ticker(bot: Bot):
    while True:
        await asyncio.sleep(20)
        try:
            rows = await q("SELECT id FROM battles WHERE status='active' AND end_ts>0 AND end_ts<=?", int(time.time()))
            for r in rows:
                await finish_battle(bot, r["id"])
        except Exception:
            log.exception("ticker xatosi")


# ------------------------------------------------------------------ buyurtma / to'lov

async def new_order(uid: int, kind: str, stars: int, item_id: int = 0) -> int:
    rate = max(1, await SI("ton_rate", 200))
    ton = max(0.01, round(stars / rate, 3))
    code = secrets.token_hex(3).upper()
    oid, _ = await run("INSERT INTO orders(user_id,kind,item_id,stars,ton,status,code,ts) VALUES(?,?,?,?,?,'pending',?,?)",
                       uid, kind, item_id, stars, ton, code, int(time.time()))
    return oid


async def order_title(o) -> str:
    k = o["kind"]
    if k == "topup":
        return f"💰 Balansni to'ldirish — {o['stars']}⭐"
    if k == "fee":
        return "⚔️ Batl yaratish krediti"
    if k == "vip":
        return "👑 VIP obuna"
    it = await one("SELECT * FROM items WHERE id=?", o["item_id"])
    return it["title"] if it else "Buyurtma"


async def ton_paid(o) -> bool:
    addr = await S("ton_address")
    if not addr:
        return False
    headers = {"X-API-Key": TONCENTER_KEY} if TONCENTER_KEY else {}
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get("https://toncenter.com/api/v2/getTransactions",
                             params={"address": addr, "limit": 50}) as r:
                d = await r.json()
        need = int(o["ton"] * 1e9 * 0.98)
        for t in d.get("result", []):
            m = t.get("in_msg") or {}
            if (m.get("message") or "").strip() == o["code"] and int(m.get("value") or 0) >= need:
                return True
    except Exception as e:
        log.warning("TON tekshiruvi xatosi: %s", e)
    return False


async def _dm(bot, uid, text, kb=None):
    try:
        await bot.send_message(uid, text, reply_markup=kb)
    except Exception:
        pass


async def fulfill(bot: Bot, oid: int, method: str, charge: str | None = None) -> bool:
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    if not o or o["status"] != "pending":
        return False
    _, rc = await run("UPDATE orders SET status='paid',method=?,charge=? WHERE id=? AND status='pending'",
                      method, charge, oid)
    if not rc:
        return False
    uid, kind = o["user_id"], o["kind"]
    if kind == "topup":
        await run("UPDATE users SET balance=balance+? WHERE id=?", o["stars"], uid)
        await run("UPDATE orders SET status='delivered' WHERE id=?", oid)
        await _dm(bot, uid, f"✅ Balans <b>{o['stars']}⭐</b> ga to'ldirildi.", K([B("👛 Hamyon", "m:wallet")]))
    elif kind == "fee":
        await run("UPDATE users SET credits=credits+1 WHERE id=?", uid)
        await run("UPDATE orders SET status='delivered' WHERE id=?", oid)
        await _dm(bot, uid, "✅ To'lov qabul qilindi! Batl yaratish krediti qo'shildi.",
                  K([B("⚔️ Batl yaratish", "m:new")]))
    elif kind == "vip":
        days = await SI("vip_days", 30)
        u = await one("SELECT vip_until FROM users WHERE id=?", uid)
        start = max(int(time.time()), u["vip_until"] or 0)
        until = start + days * 86400
        await run("UPDATE users SET vip_until=? WHERE id=?", until, uid)
        await run("UPDATE orders SET status='delivered' WHERE id=?", oid)
        await _dm(bot, uid, f"👑 VIP faollashtirildi! Amal qilish muddati: <b>{fmt_ts(until)}</b>",
                  K([B("⚔️ Batl yaratish", "m:new")]))
    elif kind == "item":
        await deliver_item(bot, o)
    return True


async def deliver_item(bot: Bot, o):
    it = await one("SELECT * FROM items WHERE id=?", o["item_id"])
    uid = o["user_id"]
    ok = False
    if it:
        try:
            if it["kind"] == "gift":
                await bot.send_gift(user_id=uid, gift_id=it["payload"])
                ok = True
            elif it["kind"] == "premium":
                m = int(it["payload"])
                await bot.gift_premium_subscription(user_id=uid, month_count=m, star_count=PREMIUM_STARS[m])
                ok = True
        except Exception as e:
            log.warning("avto-yetkazish bo'lmadi (buyurtma %s): %s", o["id"], e)
    if ok:
        await run("UPDATE orders SET status='delivered' WHERE id=?", o["id"])
        await _dm(bot, uid, f"🎉 <b>{esc(it['title'])}</b> yetkazib berildi!", K([HOME_B]))
        return
    await _dm(bot, uid, f"✅ To'lov qabul qilindi. Buyurtma #{o['id']} tez orada yetkaziladi.", K([HOME_B]))
    user = await one("SELECT * FROM users WHERE id=?", uid)
    who = f'<a href="tg://user?id={uid}">{esc(user["name"] if user else str(uid))}</a>'
    await _dm(bot, OWNER_ID,
              f"🧾 <b>Qo'lda yetkazish kerak</b>\nBuyurtma #{o['id']} — {esc(it['title'] if it else '?')}\n"
              f"Xaridor: {who}\nTo'lov: {o['method']}",
              K([B("✅ Yetkazildi", f"o:dlv:{o['id']}")]))


HOME_B = B("🏠 Menyu", "m:home")


##############################################################################
# app/web.py
##############################################################################
log = logging.getLogger(__name__)
ROOT = os.path.dirname(os.path.abspath(__file__))


def check_init(init_data: str):
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True))
        h = d.pop("hash")
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(d.get("auth_date", 0)) > 86400:
            return None
        return json.loads(d["user"])
    except Exception:
        return None


def auth(handler):
    async def wrapper(req: web.Request):
        u = check_init(req.headers.get("X-Init-Data", ""))
        if not u:
            return web.json_response({"error": "Avtorizatsiya xatosi"}, status=401)
        row = await one("SELECT banned FROM users WHERE id=?", u["id"])
        if not row:
            # foydalanuvchi botni hali ishga tushirmagan bo'lsa ham profil yaratamiz
            await run("INSERT OR IGNORE INTO users(id,name,username,joined) VALUES(?,?,?,?)",
                      u["id"], (u.get("first_name", "") + " " + u.get("last_name", "")).strip(), u.get("username"), int(time.time()))
        elif row["banned"]:
            return web.json_response({"error": "Bloklangansiz"}, status=403)
        req["uid"] = u["id"]
        return await handler(req)
    return wrapper


@auth
async def api_state(req: web.Request):
    uid = req["uid"]
    u = await one("SELECT * FROM users WHERE id=?", uid)
    battles = []
    for b in await q("SELECT b.id,b.title,b.points,b.end_ts,(SELECT COUNT(*) FROM participants WHERE battle_id=b.id) n "
                     "FROM battles b WHERE b.status='active' ORDER BY b.id DESC LIMIT 20"):
        top = [{"nick": r["nick"], "points": r["points"]} for r in (await leaderboard(b["id"]))[:5]]
        battles.append({"id": b["id"], "title": b["title"], "points": b["points"], "n": b["n"],
                        "ends": fmt_ts(b["end_ts"]) if b["end_ts"] else "", "top": top})
    items = [dict(id=i["id"], kind=i["kind"], title=i["title"], descr=i["descr"], stars=i["price_stars"])
             for i in await q("SELECT * FROM items WHERE active=1 ORDER BY kind, price_stars")]
    return web.json_response({
        "brand": await S("brand"), "bot": await bot_username(req.app["bot"]), "ton_rate": await SI("ton_rate", 200),
        "user": {"id": uid, "name": u["name"], "balance": u["balance"], "credits": u["credits"],
                 "vip": fmt_ts(u["vip_until"]) if is_vip(u) else "", "wallet": u["wallet"]},
        "battles": battles, "items": items})


async def _invoice(req, order_id):
    bot: Bot = req.app["bot"]
    o = await one("SELECT * FROM orders WHERE id=?", order_id)
    title = (await order_title(o))[:32]
    link = await bot.create_invoice_link(title=title, description=f"Buyurtma #{order_id}", payload=f"o:{order_id}",
                                         currency="XTR", prices=[LabeledPrice(label=title, amount=o["stars"])])
    return web.json_response({"link": link})


@auth
async def api_buy(req: web.Request):
    body = await req.json()
    it = await one("SELECT * FROM items WHERE id=? AND active=1", int(body.get("item_id", 0)))
    if not it:
        return web.json_response({"error": "Mahsulot topilmadi"})
    if it["kind"] == "stars":
        u = await bot_username(req.app["bot"])
        return web.json_response({"bot": f"https://t.me/{u}?start=buy_{it['id']}"})
    return await _invoice(req, await new_order(req["uid"], "item", it["price_stars"], it["id"]))


@auth
async def api_topup(req: web.Request):
    body = await req.json()
    amount = int(body.get("stars", 0))
    if not 10 <= amount <= 10000:
        return web.json_response({"error": "Miqdor 10 dan 10000 gacha bo'lsin"})
    return await _invoice(req, await new_order(req["uid"], "topup", amount))


@auth
async def api_wallet(req: web.Request):
    body = await req.json()
    addr = str(body.get("address", "")).strip()[:80]
    await run("UPDATE users SET wallet=? WHERE id=?", addr, req["uid"])
    return web.json_response({"ok": True})


async def web_img(req: web.Request):
    key = req.match_info["key"]
    if not key.isalnum():
        raise web.HTTPNotFound()
    fid = await S(f"img_{key}")
    if fid:
        try:
            buf = io.BytesIO()
            await req.app["bot"].download(fid, destination=buf)
            return web.Response(body=buf.getvalue(), content_type="image/jpeg", headers={"Cache-Control": "max-age=60"})
        except Exception:
            pass
    path = os.path.join(DIR, f"{key}.jpg")
    if os.path.exists(path):
        return web.FileResponse(path)
    raise web.HTTPNotFound()


async def web_index(req):
    return web.FileResponse(os.path.join(ROOT, "index.html"), headers={"Cache-Control": "no-cache"})


async def web_manifest(req):
    base = WEBAPP_URL or f"{req.scheme}://{req.host}"
    return web.json_response({"url": base, "name": await S("brand"), "iconUrl": f"{base}/img/home"})


async def start_web(bot: Bot):
    app = web.Application()
    app["bot"] = bot
    app.add_routes([
        web.get("/app", web_index), web.get("/health", lambda r: web.Response(text="ok")),
        web.get("/tonconnect-manifest.json", web_manifest), web.get("/img/{key}", web_img),
        web.get("/api/state", api_state), web.post("/api/buy", api_buy),
        web.post("/api/topup", api_topup), web.post("/api/wallet", api_wallet),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    log.info("Mini App serveri: %s:%s", WEB_HOST, WEB_PORT)
    return runner


##############################################################################
# app/handlers/pay.py
##############################################################################
r_pay = Router()


async def checkout(ev, oid: int):
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    u = await one("SELECT balance FROM users WHERE id=?", o["user_id"])
    it = await one("SELECT kind FROM items WHERE id=?", o["item_id"]) if o["item_id"] else None
    rows = []
    if o["kind"] != "topup":
        rows.append([B(f"💰 Balansdan to'lash ({u['balance']}⭐)", f"pay:bal:{oid}")])
    if not (it and it["kind"] == "stars"):
        rows.append([B(f"⭐ Stars bilan to'lash ({o['stars']}⭐)", f"pay:xtr:{oid}")])
    if await S("ton_address"):
        rows.append([B(f"💠 TON bilan to'lash ({o['ton']} TON)", f"pay:ton:{oid}")])
    rows.append([B("✖️ Bekor qilish", "x:cancel")])
    text = (f"🧾 <b>Buyurtma #{oid}</b>\n\n{esc(await order_title(o))}\n"
            f"💳 Narx: <b>{o['stars']}⭐</b> ≈ <b>{o['ton']} TON</b>\n\nTo'lov usulini tanlang 👇")
    await screen(ev, "shop", text, K(*rows))


async def _mine(c: CallbackQuery, oid: int):
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    if not o or o["user_id"] != c.from_user.id or o["status"] != "pending":
        await c.answer("Buyurtma topilmadi yoki allaqachon to'langan", show_alert=True)
        return None
    return o


@r_pay.callback_query(F.data.startswith("pay:bal:"))
async def pay_balance(c: CallbackQuery):
    o = await _mine(c, int(c.data.split(":")[2]))
    if not o or o["kind"] == "topup":
        return
    _, rc = await run("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", o["stars"], o["user_id"], o["stars"])
    if not rc:
        return await c.answer("Balans yetarli emas. Avval hamyonni to'ldiring.", show_alert=True)
    await fulfill(c.bot, o["id"], "balance")
    await c.answer("✅ To'landi")
    try:
        await c.message.delete()
    except Exception:
        pass


@r_pay.callback_query(F.data.startswith("pay:xtr:"))
async def pay_stars(c: CallbackQuery):
    o = await _mine(c, int(c.data.split(":")[2]))
    if not o:
        return
    title = (await order_title(o))[:32]
    await c.bot.send_invoice(
        chat_id=c.from_user.id, title=title, description=f"Buyurtma #{o['id']}",
        payload=f"o:{o['id']}", currency="XTR", prices=[LabeledPrice(label=title, amount=o["stars"])])
    await c.answer()


@r_pay.callback_query(F.data.startswith("pay:ton:"))
async def pay_ton(c: CallbackQuery):
    o = await _mine(c, int(c.data.split(":")[2]))
    if not o:
        return
    addr = await S("ton_address")
    await run("UPDATE orders SET method='ton' WHERE id=?", o["id"])
    text = (f"💠 <b>TON orqali to'lov</b> — buyurtma #{o['id']}\n\n"
            f"1️⃣ Manzil:\n<code>{esc(addr)}</code>\n"
            f"2️⃣ Miqdor: <code>{o['ton']}</code> TON\n"
            f"3️⃣ Izoh (comment) <b>albatta</b> yozing:\n<code>{o['code']}</code>\n\n"
            "To'lagach «Tekshirish» tugmasini bosing. Izohsiz to'lov avtomatik topilmaydi.")
    await screen(c, "wallet", text, K([B("✅ To'lovni tekshirish", f"pay:chk:{o['id']}")], [B("✖️ Bekor", "x:cancel")]))


@r_pay.callback_query(F.data.startswith("pay:chk:"))
async def pay_check(c: CallbackQuery):
    o = await _mine(c, int(c.data.split(":")[2]))
    if not o:
        return
    if await ton_paid(o):
        await fulfill(c.bot, o["id"], "ton")
        await c.answer("✅ To'lov topildi")
        try:
            await c.message.delete()
        except Exception:
            pass
    else:
        await c.answer("Hali to'lov topilmadi. 1-2 daqiqadan so'ng qayta tekshiring.", show_alert=True)


@r_pay.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = False
    if q.invoice_payload.startswith("o:"):
        o = await one("SELECT status,user_id FROM orders WHERE id=?", int(q.invoice_payload[2:]))
        ok = bool(o and o["status"] == "pending" and o["user_id"] == q.from_user.id)
    await q.answer(ok=ok, error_message=None if ok else "Buyurtma topilmadi yoki allaqachon to'langan")


@r_pay.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    if sp.invoice_payload.startswith("o:"):
        await fulfill(m.bot, int(sp.invoice_payload[2:]), "stars", sp.telegram_payment_charge_id)


##############################################################################
# app/handlers/user.py
##############################################################################
r_user = Router()
r_user.message.filter(F.chat.type == "private")

ADDR_RE = re.compile(r"^(?:[EUk0]Q[A-Za-z0-9_-]{46}|-?\d:[0-9a-fA-F]{64})$")


# ------------------------------------------------------------------ bosh menyu

@r_user.message(Command("menu"))
@r_user.callback_query(F.data == "m:home")
async def home(ev, state: FSMContext):
    await state.clear()
    u = ev.from_user
    brand = await S("brand")
    text = (f"<b>{esc(brand)}</b>\n\n👋 Salom, <b>{esc(u.first_name)}</b>!\n\n"
            "⚔️ Batllarda qatnash, izoh yoz va ball to'pla\n"
            "🛍 Stars, Premium, Giftlar va NFT xarid qil\n"
            "👛 TON hamyoningni ulab, balansni to'ldir\n\n"
            "Quyidagi tugmalardan birini tanlang 👇")
    await screen(ev, "home", text, home_kb(u.id))


@r_user.callback_query(F.data == "x:cancel")
async def cancel(c: CallbackQuery, state: FSMContext):
    await home(c, state)


@r_user.message(Command("help"))
async def help_cmd(m: Message):
    text = ("<b>Yordam</b>\n\n"
            "<b>Foydalanuvchi:</b> /menu /battles /shop /wallet /profile\n"
            "<b>Batl yaratuvchi:</b> /newbattle /mybattles /linkchannel\n\n"
            "1️⃣ Batl postidagi «Qatnashish» tugmasini bosing\n"
            "2️⃣ Nik nomingizni kiriting\n"
            "3️⃣ Kanaldagi raund postlariga izoh yozing — har bir raundda 1 ta izoh = ball.")
    await screen(m, "home", text, back())


@r_user.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext, is_new: bool = False):
    await state.clear()
    arg = command.args or ""
    if arg.startswith("ref_") and is_new:
        await _ref(m, arg[4:])
    if arg.startswith("join_") and arg[5:].isdigit():
        return await start_join(m, state, int(arg[5:]))
    if arg.startswith("buy_") and arg[4:].isdigit():
        return await show_item(m, int(arg[4:]))
    await home(m, state)


async def _ref(m: Message, raw: str):
    if not raw.isdigit() or int(raw) == m.from_user.id:
        return
    rid = int(raw)
    if not await one("SELECT 1 FROM users WHERE id=?", rid):
        return
    _, rc = await run("UPDATE users SET ref_by=? WHERE id=? AND ref_by IS NULL", rid, m.from_user.id)
    if not rc:
        return
    await run("UPDATE users SET ref_count=ref_count+1 WHERE id=?", rid)
    r = await one("SELECT ref_count FROM users WHERE id=?", rid)
    per = await SI("ref_per_credit", 3)
    if per and r["ref_count"] % per == 0:
        await run("UPDATE users SET credits=credits+1 WHERE id=?", rid)
        try:
            await m.bot.send_message(rid, f"🎁 {per} ta do'stingiz qo'shildi — sizga 1 ta batl krediti sovg'a qilindi!")
        except Exception:
            pass


# ------------------------------------------------------------------ batllar ro'yxati

@r_user.message(Command("battles"))
@r_user.callback_query(F.data == "m:battles")
async def battles(ev, state: FSMContext):
    await state.clear()
    rows = await q("SELECT b.id,b.title,(SELECT COUNT(*) FROM participants WHERE battle_id=b.id) n "
                   "FROM battles b WHERE b.status='active' ORDER BY b.id DESC LIMIT 15")
    if not rows:
        return await screen(ev, "battle", "🔥 <b>Faol batllar</b>\n\nHozircha faol batl yo'q.\nO'zingiz yarating — «⚔️ Batl yaratish»!",
                            K([B("⚔️ Batl yaratish", "m:new")], [HOME]))
    kb = [[B(f"⚔️ {r['title'][:30]} · 👥 {r['n']}", f"v:{r['id']}")] for r in rows] + [[HOME]]
    await screen(ev, "battle", "🔥 <b>Faol batllar</b>\n\nBirini tanlang 👇", K(*kb))


@r_user.callback_query(F.data.startswith("v:"))
async def view_battle(c: CallbackQuery):
    bid = int(c.data[2:])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not b:
        return await c.answer("Batl topilmadi", show_alert=True)
    rows = await leaderboard(bid)
    ch = await one("SELECT username FROM channels WHERE id=?", b["channel_id"])
    ends = "yakunlangan" if b["status"] != "active" else (fmt_ts(b["end_ts"]) if b["end_ts"] else "yaratuvchi hal qiladi")
    text = (f"⚔️ <b>{esc(b['title'])}</b>\n⏰ Tugash: {ends}\n💎 Raund uchun: {b['points']} ball\n\n"
            f"🏆 <b>Reyting</b>\n{lb_text(rows, 10) or 'Hali ishtirokchi yo‘q'}")
    kb = []
    if b["status"] == "active":
        kb.append([B("✅ Qatnashish", f"j:{bid}")])
    if ch and ch["username"]:
        kb.append([B("📢 Kanalga o'tish", url=f"https://t.me/{ch['username']}")])
    kb.append([B("⬅️ Orqaga", "m:battles"), HOME])
    await screen(c, "top", text, K(*kb))


# ------------------------------------------------------------------ qatnashish

@r_user.callback_query(F.data.startswith("j:"))
async def join_cb(c: CallbackQuery, state: FSMContext):
    await start_join(c, state, int(c.data[2:]))


async def start_join(ev, state: FSMContext, bid: int):
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not b or b["status"] != "active":
        return await screen(ev, "battle", "😔 Bu batl yakunlangan yoki topilmadi.", K([B("🔥 Faol batllar", "m:battles")], [HOME]))
    p = await one("SELECT nick FROM participants WHERE battle_id=? AND user_id=?", bid, ev.from_user.id)
    await state.set_state(Join.nick)
    await state.update_data(bid=bid)
    extra = f"\n\nSiz allaqachon «{esc(p['nick'])}» nomi bilan ro'yxatdasiz. Yangi nom yuborsangiz o'zgaradi." if p else ""
    await screen(ev, "battle",
                 f"✍️ <b>{esc(b['title'])}</b>\n\nBatlda ko'rinadigan <b>nik nomingizni</b> yuboring (2–24 belgi).{extra}",
                 K([B("👤 Ismimni ishlatish", "jn:use")], [B("✖️ Bekor qilish", "x:cancel")]))


async def _register(ev, state: FSMContext, nick: str):
    d = await state.get_data()
    bid = d.get("bid")
    b = await one("SELECT * FROM battles WHERE id=?", bid) if bid else None
    await state.clear()
    if not b or b["status"] != "active":
        return await screen(ev, "battle", "😔 Batl yakunlangan.", K([HOME]))
    nick = nick.strip()[:24]
    await run("INSERT INTO participants(battle_id,user_id,nick) VALUES(?,?,?) "
              "ON CONFLICT(battle_id,user_id) DO UPDATE SET nick=excluded.nick", bid, ev.from_user.id, nick)
    await update_post(ev.bot, bid)
    await screen(ev, "battle",
                 f"✅ <b>Ro'yxatdan o'tdingiz!</b>\n\n⚔️ {esc(b['title'])}\n👤 Nik: <b>{esc(nick)}</b>\n\n"
                 f"Endi kanaldagi raund postlariga izoh yozing — har bir raundda 1 ta izoh = <b>{b['points']} ball</b>.",
                 K([B("🏆 Reyting", f"v:{bid}")], [HOME]))


@r_user.callback_query(Join.nick, F.data == "jn:use")
async def join_use(c: CallbackQuery, state: FSMContext):
    await _register(c, state, c.from_user.full_name if len(c.from_user.full_name) >= 2 else f"user{c.from_user.id}")


@r_user.message(Join.nick, F.text)
async def join_nick(m: Message, state: FSMContext):
    if not 2 <= len(m.text.strip()) <= 24:
        return await m.answer("⚠️ Nik 2 dan 24 belgigacha bo'lishi kerak. Qayta yuboring.", reply_markup=cancel_kb())
    await _register(m, state, m.text)


# ------------------------------------------------------------------ profil / referal / VIP

@r_user.message(Command("profile"))
@r_user.callback_query(F.data == "m:profile")
async def profile(ev, state: FSMContext):
    await state.clear()
    uid = ev.from_user.id
    u = await one("SELECT * FROM users WHERE id=?", uid)
    st = await one("SELECT COUNT(*) n, COALESCE(SUM(points),0) s FROM participants WHERE user_id=?", uid)
    vip = f"👑 VIP: {fmt_ts(u['vip_until'])} gacha" if is_vip(u) else "👑 VIP: yo'q"
    text = (f"👤 <b>{esc(u['name'])}</b>\n🆔 <code>{uid}</code>\n\n"
            f"💰 Balans: <b>{u['balance']}⭐</b>\n🎟 Batl kreditlari: <b>{u['credits']}</b>\n{vip}\n\n"
            f"⚔️ Qatnashgan batllar: <b>{st['n']}</b>\n💎 Jami ball: <b>{st['s']}</b>\n"
            f"👥 Taklif qilinganlar: <b>{u['ref_count']}</b>\n"
            f"👛 Hamyon: <code>{esc(u['wallet']) if u['wallet'] else 'ulanmagan'}</code>")
    await screen(ev, "profile", text, K([B("👛 Hamyon", "m:wallet"), B("👑 VIP", "m:vip")], [HOME]))


@r_user.callback_query(F.data == "m:ref")
async def ref(c: CallbackQuery):
    uname = await bot_username(c.bot)
    link = f"https://t.me/{uname}?start=ref_{c.from_user.id}"
    per = await SI("ref_per_credit", 3)
    u = await one("SELECT ref_count FROM users WHERE id=?", c.from_user.id)
    text = (f"🎁 <b>Do'st taklif qiling</b>\n\nHar <b>{per}</b> ta do'stingiz uchun <b>1 ta bepul batl krediti</b> olasiz.\n\n"
            f"👥 Taklif qilganlar: <b>{u['ref_count']}</b>\n🔗 Havolangiz:\n<code>{link}</code>")
    await screen(c, "ref", text, K([B("📤 Ulashish", url=f"https://t.me/share/url?url={link}&text=Batllarda%20qatnash!")], [HOME]))


@r_user.callback_query(F.data == "m:vip")
async def vip(c: CallbackQuery):
    price, days = await SI("vip_price", 500), await SI("vip_days", 30)
    u = await one("SELECT * FROM users WHERE id=?", c.from_user.id)
    now = f"\n\n✅ Faol: {fmt_ts(u['vip_until'])} gacha" if is_vip(u) else ""
    text = (f"👑 <b>VIP obuna</b>\n\n• Cheksiz batl yaratish (kredit kerak emas)\n• {days} kun amal qiladi\n"
            f"• Narxi: <b>{price}⭐</b>{now}")
    await screen(c, "vip", text, K([B(f"👑 VIP olish — {price}⭐", "vip:buy")], [HOME]))


@r_user.callback_query(F.data == "vip:buy")
async def vip_buy(c: CallbackQuery):
    await checkout(c, await new_order(c.from_user.id, "vip", await SI("vip_price", 500)))


# ------------------------------------------------------------------ hamyon

@r_user.message(Command("wallet"))
@r_user.callback_query(F.data == "m:wallet")
async def wallet(ev, state: FSMContext):
    await state.clear()
    u = await one("SELECT * FROM users WHERE id=?", ev.from_user.id)
    addr = f"<code>{esc(u['wallet'])}</code>" if u["wallet"] else "ulanmagan"
    text = (f"👛 <b>Hamyon</b>\n\n💰 Bot balansi: <b>{u['balance']}⭐</b>\n💠 TON hamyon: {addr}\n\n"
            "Balans bilan batl krediti, VIP va do'kon xaridlarini to'lash mumkin.")
    rows = [[B("➕ Balansni to'ldirish", "w:top")]]
    if WEBAPP_URL:
        rows.append([B("🔗 TON hamyonni ulash", web=WEBAPP_URL + "/app?tab=wallet")])
    rows.append([B("✍️ Manzilni qo'lda kiritish", "w:set")])
    if u["wallet"]:
        rows.append([B("🗑 Hamyonni uzish", "w:del")])
    rows.append([HOME])
    await screen(ev, "wallet", text, K(*rows))


@r_user.callback_query(F.data == "w:top")
async def topup(c: CallbackQuery):
    amounts = (50, 100, 250, 500, 1000, 2500)
    rows = [[B(f"{a}⭐", f"w:t:{a}") for a in amounts[i:i + 3]] for i in range(0, len(amounts), 3)]
    rows.append([B("⬅️ Orqaga", "m:wallet")])
    await screen(c, "wallet", "➕ <b>Balansni to'ldirish</b>\n\nMiqdorni tanlang:", K(*rows))


@r_user.callback_query(F.data.startswith("w:t:"))
async def topup_go(c: CallbackQuery):
    await checkout(c, await new_order(c.from_user.id, "topup", int(c.data.split(":")[2])))


@r_user.callback_query(F.data == "w:set")
async def wallet_set(c: CallbackQuery, state: FSMContext):
    await state.set_state(Wal.addr)
    await screen(c, "wallet", "✍️ TON hamyon manzilingizni yuboring (UQ… yoki EQ… bilan boshlanadi).", cancel_kb())


@r_user.message(Wal.addr, F.text)
async def wallet_addr(m: Message, state: FSMContext):
    a = m.text.strip()
    if not ADDR_RE.match(a):
        return await m.answer("⚠️ Manzil noto'g'ri ko'rinadi. Qayta yuboring.", reply_markup=cancel_kb())
    await run("UPDATE users SET wallet=? WHERE id=?", a, m.from_user.id)
    await wallet(m, state)


@r_user.callback_query(F.data == "w:del")
async def wallet_del(c: CallbackQuery, state: FSMContext):
    await run("UPDATE users SET wallet='' WHERE id=?", c.from_user.id)
    await wallet(c, state)


##############################################################################
# app/handlers/shop.py
##############################################################################
r_shop = Router()
r_shop.message.filter(F.chat.type == "private")

KINDS = [("premium", "💎 Premium"), ("stars", "⭐ Stars"), ("gift", "🎁 Giftlar"), ("nft", "🖼 NFT")]


def price_label(it) -> str:
    return f"{it['price_stars']}⭐"


@r_shop.message(Command("shop"))
@r_shop.callback_query(F.data == "m:shop")
async def shop(ev, state: FSMContext):
    await state.clear()
    rows = [[B(KINDS[0][1], "s:k:premium"), B(KINDS[1][1], "s:k:stars")],
            [B(KINDS[2][1], "s:k:gift"), B(KINDS[3][1], "s:k:nft")], [HOME]]
    await screen(ev, "shop", "🛍 <b>Do'kon</b>\n\nStars, Premium, Giftlar va NFT — bitta joyda.\nBo'limni tanlang 👇", K(*rows))


@r_shop.callback_query(F.data.startswith("s:k:"))
async def category(c: CallbackQuery):
    kind = c.data.split(":")[2]
    items = await q("SELECT * FROM items WHERE kind=? AND active=1 ORDER BY price_stars LIMIT 40", kind)
    title = dict(KINDS).get(kind, "Do'kon")
    if not items:
        return await screen(c, "shop", f"{title}\n\nHozircha mahsulot yo'q.", K([B("⬅️ Orqaga", "m:shop")]))
    rows = [[B(f"{i['title']} — {price_label(i)}", f"s:i:{i['id']}")] for i in items]
    rows.append([B("⬅️ Orqaga", "m:shop")])
    await screen(c, "shop", f"<b>{title}</b>\n\nMahsulotni tanlang 👇", K(*rows))


async def show_item(ev, iid: int):
    it = await one("SELECT * FROM items WHERE id=? AND active=1", iid)
    if not it:
        return await screen(ev, "shop", "Mahsulot topilmadi.", K([B("🛍 Do'kon", "m:shop")]))
    text = f"<b>{esc(it['title'])}</b>\n\n{esc(it['descr'])}\n\n💳 Narx: <b>{it['price_stars']}⭐</b>"
    if it["kind"] == "nft" and it["payload"].startswith("http"):
        text += f"\n🔗 <a href=\"{esc(it['payload'])}\">NFT haqida</a>"
    await screen(ev, "shop", text, K([B("🛒 Sotib olish", f"s:b:{iid}")], [B("⬅️ Orqaga", f"s:k:{it['kind']}")]))


@r_shop.callback_query(F.data.startswith("s:i:"))
async def item(c: CallbackQuery):
    await show_item(c, int(c.data.split(":")[2]))


@r_shop.callback_query(F.data.startswith("s:b:"))
async def buy(c: CallbackQuery):
    iid = int(c.data.split(":")[2])
    it = await one("SELECT * FROM items WHERE id=? AND active=1", iid)
    if not it:
        return await c.answer("Mahsulot topilmadi", show_alert=True)
    await checkout(c, await new_order(c.from_user.id, "item", it["price_stars"], iid))


##############################################################################
# app/handlers/battle.py
##############################################################################
r_battle = Router()
r_battle.message.filter(F.chat.type == "private")

USER_CMDS = [("start", "Boshlash"), ("menu", "Asosiy menyu"), ("battles", "Faol batllar"),
             ("shop", "Do'kon"), ("wallet", "Hamyon"), ("profile", "Profil"), ("help", "Yordam")]
ORG_CMDS = USER_CMDS + [("newbattle", "Batl yaratish"), ("mybattles", "Mening batllarim"),
                        ("linkchannel", "Kanal ulash")]
OWNER_CMDS = ORG_CMDS + [("owner", "Owner panel"), ("give", "Balans berish"),
                         ("ban", "Bloklash"), ("unban", "Blokdan chiqarish")]


async def set_scope_commands(bot, uid: int, cmds):
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in cmds],
                                  scope=BotCommandScopeChat(chat_id=uid))
    except Exception:
        pass


def own(b, uid) -> bool:
    return bool(b) and (b["owner_id"] == uid or uid == OWNER_ID)


# ------------------------------------------------------------------ kanal ulash

@r_battle.message(Command("linkchannel"))
@r_battle.callback_query(F.data == "ch:link")
async def link_start(ev, state: FSMContext):
    await state.set_state(Link.ch)
    me = await ev.bot.me()
    text = ("📢 <b>Kanal ulash</b>\n\nShartlar:\n"
            f"1️⃣ @{me.username} ni kanalga <b>admin</b> qiling (post yuborish va tahrirlash huquqi bilan)\n"
            "2️⃣ Kanalga <b>muhokama (izoh) guruhi</b> ulang\n"
            f"3️⃣ @{me.username} ni shu guruhga ham <b>admin</b> qiling — aks holda izohlarni ko'ra olmaydi\n\n"
            "Keyin kanal @username'ini yoki kanaldan biror postni forward qilib yuboring 👇")
    await screen(ev, "battle", text, cancel_kb())


@r_battle.message(Link.ch)
async def link_ch(m: Message, state: FSMContext):
    bot = m.bot
    try:
        fo = m.forward_origin
        if fo and fo.type == "channel":
            chat = await bot.get_chat(fo.chat.id)
        elif m.text:
            t = m.text.strip().replace("https://t.me/", "").replace("t.me/", "").lstrip("@")
            chat = await bot.get_chat("@" + t)
        else:
            return await m.answer("Kanal @username'ini yoki forward qilingan postni yuboring.", reply_markup=cancel_kb())
    except Exception:
        return await m.answer("❌ Kanal topilmadi. @username'ni tekshiring yoki bot kanalda borligiga ishonch hosil qiling.",
                              reply_markup=cancel_kb())
    if chat.type != "channel":
        return await m.answer("❌ Bu kanal emas. Kanalni yuboring.", reply_markup=cancel_kb())
    me = await bot.me()
    try:
        bm = await bot.get_chat_member(chat.id, me.id)
        um = await bot.get_chat_member(chat.id, m.from_user.id)
    except Exception:
        return await m.answer("❌ Bot kanalda admin emas. Avval botni admin qiling.", reply_markup=cancel_kb())
    if bm.status != "administrator" or not getattr(bm, "can_post_messages", False):
        return await m.answer("❌ Botga kanalda <b>post yuborish</b> huquqi bering.", reply_markup=cancel_kb())
    if um.status not in ("administrator", "creator") and m.from_user.id != OWNER_ID:
        return await m.answer("❌ Siz bu kanalning admini emassiz.", reply_markup=cancel_kb())
    gid = chat.linked_chat_id
    if not gid:
        return await m.answer("❌ Kanalga izoh (muhokama) guruhi ulanmagan. Kanal sozlamalaridan «Discussion» ulang.",
                              reply_markup=cancel_kb())
    try:
        gm = await bot.get_chat_member(gid, me.id)
        ok = gm.status == "administrator"
    except Exception:
        ok = False
    if not ok:
        return await m.answer("❌ Botni izoh guruhida ham <b>admin</b> qiling, so'ng qayta yuboring.", reply_markup=cancel_kb())
    await run("INSERT INTO channels(id,title,username,group_id,owner_id) VALUES(?,?,?,?,?) "
              "ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=excluded.username, "
              "group_id=excluded.group_id, owner_id=excluded.owner_id",
              chat.id, chat.title, chat.username, gid, m.from_user.id)
    await state.clear()
    if m.from_user.id != OWNER_ID:
        await set_scope_commands(bot, m.from_user.id, ORG_CMDS)
    await screen(m, "battle", f"✅ <b>{esc(chat.title)}</b> kanali ulandi!",
                 K([B("⚔️ Batl yaratish", "m:new")], [HOME]))


# ------------------------------------------------------------------ batl yaratish

async def needs_credit(uid: int) -> bool:
    if uid == OWNER_ID or await SI("battle_fee", 25) <= 0:
        return False
    u = await one("SELECT credits, vip_until FROM users WHERE id=?", uid)
    return not is_vip(u) and u["credits"] < 1


@r_battle.message(Command("newbattle"))
@r_battle.callback_query(F.data == "m:new")
async def new_battle(ev, state: FSMContext):
    await state.clear()
    uid = ev.from_user.id
    chs = await q("SELECT * FROM channels WHERE owner_id=?", uid)
    if not chs:
        return await link_start(ev, state)
    if await needs_credit(uid):
        return await checkout(ev, await new_order(uid, "fee", await SI("battle_fee", 25)))
    rows = [[B(f"📢 {c['title'][:30]}", f"nb:ch:{c['id']}")] for c in chs]
    rows += [[B("➕ Kanal ulash", "ch:link")], [HOME]]
    await state.set_state(NewB.ch)
    await screen(ev, "battle", "⚔️ <b>Yangi batl</b>\n\n1/5 — Qaysi kanalda o'tkazamiz?", K(*rows))


@r_battle.callback_query(NewB.ch, F.data.startswith("nb:ch:"))
async def nb_channel(c: CallbackQuery, state: FSMContext):
    await state.update_data(ch=int(c.data.split(":")[2]))
    await state.set_state(NewB.title)
    await screen(c, "battle", "⚔️ <b>Yangi batl</b>\n\n2/5 — Batl <b>nomini</b> yuboring (masalan: «Eng zo'r she'r»).", cancel_kb())


@r_battle.message(NewB.title, F.text)
async def nb_title(m: Message, state: FSMContext):
    if len(m.text) > 80:
        return await m.answer("⚠️ Nom 80 belgidan oshmasin.", reply_markup=cancel_kb())
    await state.update_data(title=m.text.strip())
    await state.set_state(NewB.rules)
    await screen(m, "battle", "⚔️ <b>Yangi batl</b>\n\n3/5 — <b>Qoidalarni</b> yozing (nima qilinadi, qanday g'olib aniqlanadi).", cancel_kb())


def _points_kb(prefix, extra=None):
    rows = [[B(str(n), f"{prefix}:{n}") for n in (1, 2, 3, 5, 10)]]
    if extra:
        rows.append(extra)
    rows.append([B("✖️ Bekor qilish", "x:cancel")])
    return K(*rows)


@r_battle.message(NewB.rules, F.text)
async def nb_rules(m: Message, state: FSMContext):
    if len(m.text) > 1500:
        return await m.answer("⚠️ Qoidalar 1500 belgidan oshmasin.", reply_markup=cancel_kb())
    await state.update_data(rules=m.text.strip())
    await state.set_state(NewB.points)
    await screen(m, "battle", "⚔️ <b>Yangi batl</b>\n\n4/5 — Har bir raundda 1 ta izoh uchun nechta <b>ball</b> berilsin?\n(Tugmani bosing yoki son yuboring)",
                 _points_kb("nb:pts"))


async def _ask_end(ev, state):
    await state.set_state(NewB.end)
    rows = [[B("✋ Qo'lda yakunlayman", "nb:end:0")],
            [B("1 soat", "nb:end:1"), B("6 soat", "nb:end:6"), B("24 soat", "nb:end:24")],
            [B("3 kun", "nb:end:72"), B("7 kun", "nb:end:168")], [B("✖️ Bekor qilish", "x:cancel")]]
    await screen(ev, "battle", "⚔️ <b>Yangi batl</b>\n\n5/5 — Batl qachon tugasin?", K(*rows))


@r_battle.callback_query(NewB.points, F.data.startswith("nb:pts:"))
async def nb_points_cb(c: CallbackQuery, state: FSMContext):
    await state.update_data(points=int(c.data.split(":")[2]))
    await _ask_end(c, state)


@r_battle.message(NewB.points, F.text)
async def nb_points_msg(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not 1 <= int(m.text) <= 1000:
        return await m.answer("⚠️ 1 dan 1000 gacha son yuboring.", reply_markup=cancel_kb())
    await state.update_data(points=int(m.text))
    await _ask_end(m, state)


@r_battle.callback_query(NewB.end, F.data.startswith("nb:end:"))
async def nb_end(c: CallbackQuery, state: FSMContext):
    hours = int(c.data.split(":")[2])
    await state.update_data(hours=hours)
    d = await state.get_data()
    ends = fmt_ts(int(time.time()) + hours * 3600) if hours else "yaratuvchi hal qiladi"
    text = (f"✅ <b>Tasdiqlang</b>\n\n⚔️ {esc(d['title'])}\n📜 {esc(d['rules'])}\n"
            f"💎 Raund uchun: {d['points']} ball\n⏰ Tugash: {ends}")
    await screen(c, "battle", text, K([B("🚀 E'lon qilish", "nb:go")], [B("✖️ Bekor qilish", "x:cancel")]))


@r_battle.callback_query(NewB.end, F.data == "nb:go")
async def nb_go(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    uid = c.from_user.id
    ch = await one("SELECT * FROM channels WHERE id=?", d.get("ch"))
    if not ch:
        return await c.answer("Kanal topilmadi", show_alert=True)
    u = await one("SELECT * FROM users WHERE id=?", uid)
    if await SI("battle_fee", 25) > 0 and uid != OWNER_ID and not is_vip(u):
        _, rc = await run("UPDATE users SET credits=credits-1 WHERE id=? AND credits>0", uid)
        if not rc:
            await state.clear()
            return await checkout(c, await new_order(uid, "fee", await SI("battle_fee", 25)))
    end_ts = int(time.time()) + d["hours"] * 3600 if d["hours"] else 0
    bid, _ = await run("INSERT INTO battles(owner_id,channel_id,title,rules,points,end_ts,created) VALUES(?,?,?,?,?,?,?)",
                       uid, ch["id"], d["title"], d["rules"], d["points"], end_ts, int(time.time()))
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    try:
        msg = await c.bot.send_message(ch["id"], await post_text(b), reply_markup=await join_markup(c.bot, bid))
    except Exception as e:
        await run("DELETE FROM battles WHERE id=?", bid)
        if await SI("battle_fee", 25) > 0 and uid != OWNER_ID and not is_vip(u):
            await run("UPDATE users SET credits=credits+1 WHERE id=?", uid)
        await state.clear()
        return await screen(c, "battle", f"❌ Kanalga post yuborib bo'lmadi: {esc(str(e))}\nKredit qaytarildi.", K([HOME]))
    await run("UPDATE battles SET post_id=? WHERE id=?", msg.message_id, bid)
    await state.clear()
    await manage(c, state, bid, prefix="🚀 Batl e'lon qilindi!\n\n")


# ------------------------------------------------------------------ batllarim / boshqaruv

@r_battle.message(Command("mybattles"))
@r_battle.callback_query(F.data == "m:my")
async def my_battles(ev, state: FSMContext):
    await state.clear()
    rows = await q("SELECT id,title,status FROM battles WHERE owner_id=? ORDER BY id DESC LIMIT 20", ev.from_user.id)
    if not rows:
        return await screen(ev, "battle", "📂 Sizda hali batl yo'q.", K([B("⚔️ Batl yaratish", "m:new")], [HOME]))
    kb = [[B(f"{'🟢' if r['status'] == 'active' else '🏁'} {r['title'][:32]}", f"b:{r['id']}")] for r in rows]
    await screen(ev, "battle", "📂 <b>Mening batllarim</b>", K(*kb, [HOME]))


async def manage(ev, state, bid: int, prefix: str = ""):
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not own(b, ev.from_user.id):
        return await ev.answer("Ruxsat yo'q", show_alert=True) if isinstance(ev, CallbackQuery) else None
    n = (await one("SELECT COUNT(*) c FROM participants WHERE battle_id=?", bid))["c"]
    r = (await one("SELECT COUNT(*) c FROM rounds WHERE battle_id=?", bid))["c"]
    ends = "yakunlangan" if b["status"] != "active" else (fmt_ts(b["end_ts"]) if b["end_ts"] else "qo'lda")
    text = (f"{prefix}⚔️ <b>{esc(b['title'])}</b>\n\n👥 Ishtirokchilar: {n}\n🔄 Raundlar: {r}\n"
            f"💎 Raund uchun: {b['points']} ball\n⏰ Tugash: {ends}")
    rows = []
    if b["status"] == "active":
        rows += [[B("➕ Yangi raund (post)", f"b:r:{bid}")],
                 [B("🏆 Reyting", f"b:lb:{bid}"), B("➕ Ball berish", f"b:p:{bid}")],
                 [B("🏁 Yakunlash", f"b:f:{bid}")]]
    else:
        rows.append([B("🏆 Natijalar", f"b:lb:{bid}")])
    rows.append([B("⬅️ Orqaga", "m:my"), HOME])
    await screen(ev, "battle", text, K(*rows))


@r_battle.callback_query(F.data.regexp(r"^b:\d+$"))
async def manage_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await manage(c, state, int(c.data[2:]))


@r_battle.callback_query(F.data.startswith("b:lb:"))
async def lb(c: CallbackQuery):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not own(b, c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    rows = await leaderboard(bid)
    await screen(c, "top", f"🏆 <b>{esc(b['title'])}</b>\n\n{lb_text(rows) or 'Hali ishtirokchi yo‘q'}",
                 K([B("⬅️ Orqaga", f"b:{bid}")]))


# ------------------------------------------------------------------ raund

@r_battle.callback_query(F.data.startswith("b:r:"))
async def rnd_start(c: CallbackQuery, state: FSMContext):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not own(b, c.from_user.id) or b["status"] != "active":
        return await c.answer("Ruxsat yo'q yoki batl yakunlangan", show_alert=True)
    await state.set_state(Rnd.text)
    await state.update_data(bid=bid)
    await screen(c, "battle", "➕ <b>Yangi raund</b>\n\nKanalga chiqadigan postni yuboring: <b>matn</b> yoki <b>rasm + izoh</b> (izoh 1000 belgigacha).",
                 cancel_kb())


@r_battle.message(Rnd.text, F.text | F.photo)
async def rnd_text(m: Message, state: FSMContext):
    text = m.html_text or ""
    if m.photo and len(text) > 800:
        return await m.answer("⚠️ Rasm izohi 800 belgidan oshmasin.", reply_markup=cancel_kb())
    if len(text) > 3000:
        return await m.answer("⚠️ Matn juda uzun.", reply_markup=cancel_kb())
    await state.update_data(text=text, photo=m.photo[-1].file_id if m.photo else None)
    await state.set_state(Rnd.points)
    await screen(m, "battle", "Bu raund uchun nechta ball?",
                 _points_kb("rd:pts", [B("📌 Batl standarti", "rd:pts:0")]))


@r_battle.callback_query(Rnd.points, F.data.startswith("rd:pts:"))
async def rnd_go(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    b = await one("SELECT * FROM battles WHERE id=?", d["bid"])
    if not own(b, c.from_user.id) or b["status"] != "active":
        await state.clear()
        return await c.answer("Batl yakunlangan", show_alert=True)
    pts = int(c.data.split(":")[2]) or b["points"]
    n = (await one("SELECT COUNT(*) c FROM rounds WHERE battle_id=?", b["id"]))["c"] + 1
    body = (f"🔥 <b>{n}-raund</b> · {esc(b['title'])}\n\n{d['text']}\n\n"
            f"💬 Izohda 1 ta xabar yozing — <b>+{pts} ball</b>\n"
            "ℹ️ Faqat ro'yxatdan o'tganlar. Bir raundda faqat 1 ta izoh hisoblanadi.")
    markup = await join_markup(c.bot, b["id"])
    try:
        if d.get("photo"):
            msg = await c.bot.send_photo(b["channel_id"], d["photo"], caption=body[:1024], reply_markup=markup)
        else:
            msg = await c.bot.send_message(b["channel_id"], body[:4096], reply_markup=markup)
    except Exception as e:
        await state.clear()
        return await screen(c, "battle", f"❌ Post yuborilmadi: {esc(str(e))}", K([B("⬅️ Orqaga", f"b:{b['id']}")]))
    await run("INSERT INTO rounds(battle_id,n,points,post_id) VALUES(?,?,?,?)", b["id"], n, pts, msg.message_id)
    await state.clear()
    await manage(c, state, b["id"], prefix=f"✅ {n}-raund kanalga joylandi!\n\n")


# ------------------------------------------------------------------ qo'lda ball

@r_battle.callback_query(F.data.startswith("b:p:"))
async def pts_list(c: CallbackQuery):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not own(b, c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    rows = (await leaderboard(bid))[:40]
    if not rows:
        return await c.answer("Hali ishtirokchi yo'q", show_alert=True)
    kb = [[B(f"{r['nick'][:18]} ({r['points']})", f"b:pu:{bid}:{r['user_id']}") for r in rows[i:i + 2]]
          for i in range(0, len(rows), 2)]
    kb.append([B("⬅️ Orqaga", f"b:{bid}")])
    await screen(c, "battle", "➕ <b>Ball berish</b>\n\nIshtirokchini tanlang (eng yaxshi 40 ta ko'rsatilgan):", K(*kb))


@r_battle.callback_query(F.data.startswith("b:pu:"))
async def pts_pick(c: CallbackQuery, state: FSMContext):
    _, _, bid, uid = c.data.split(":")
    b = await one("SELECT * FROM battles WHERE id=?", int(bid))
    if not own(b, c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await state.set_state(Pts.amount)
    await state.update_data(bid=int(bid), uid=int(uid))
    await screen(c, "battle", "Necha ball qo'shilsin? Son yuboring (ayirish uchun manfiy son, masalan <code>-3</code>).", cancel_kb())


@r_battle.message(Pts.amount, F.text)
async def pts_amount(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.lstrip("-").isdigit() or abs(int(t)) > 10000 or int(t) == 0:
        return await m.answer("⚠️ Noldan farqli butun son yuboring.", reply_markup=cancel_kb())
    d = await state.get_data()
    b = await one("SELECT * FROM battles WHERE id=?", d["bid"])
    if not own(b, m.from_user.id):
        return await state.clear()
    delta = int(t)
    _, rc = await run("UPDATE participants SET points=points+? WHERE battle_id=? AND user_id=?", delta, d["bid"], d["uid"])
    if rc:
        await run("INSERT INTO plog(battle_id,user_id,delta,reason,ts) VALUES(?,?,?,?,?)",
                  d["bid"], d["uid"], delta, "manual", int(time.time()))
        try:
            await m.bot.send_message(d["uid"], f"{'➕' if delta > 0 else '➖'} «{esc(b['title'])}» batlida sizga <b>{delta:+d}</b> ball berildi.")
        except Exception:
            pass
    await state.clear()
    await manage(m, state, d["bid"], prefix=f"✅ {delta:+d} ball qo'llandi.\n\n")


# ------------------------------------------------------------------ yakunlash

@r_battle.callback_query(F.data.startswith("b:f:"))
async def fin_ask(c: CallbackQuery):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not own(b, c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await screen(c, "battle", f"🏁 <b>{esc(b['title'])}</b> batlini yakunlaymizmi?\nNatijalar kanalga e'lon qilinadi.",
                 K([B("✅ Ha, yakunlash", f"b:ff:{bid}")], [B("⬅️ Yo'q", f"b:{bid}")]))


@r_battle.callback_query(F.data.startswith("b:ff:"))
async def fin_go(c: CallbackQuery, state: FSMContext):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not own(b, c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await finish_battle(c.bot, bid)
    await manage(c, state, bid, prefix="🏁 Batl yakunlandi!\n\n")


##############################################################################
# app/handlers/comments.py
##############################################################################
r_comments = Router()


@r_comments.message(F.chat.type.in_({"group", "supergroup"}))
async def on_group(m: Message):
    # 1) kanal posti guruhga avtomatik ko'chirilganda: raund <-> guruh xabarini bog'laymiz
    fo = m.forward_origin
    if m.is_automatic_forward and fo and fo.type == "channel":
        await run("UPDATE rounds SET group_id=?, thread_id=? WHERE post_id=? "
                  "AND battle_id IN (SELECT id FROM battles WHERE channel_id=?)",
                  m.chat.id, m.message_id, fo.message_id, fo.chat.id)
        return

    u = m.from_user
    if not u or u.is_bot or m.sender_chat:
        return

    # 2) izoh qaysi post ostida yozilgan
    tid = m.message_thread_id
    rt = m.reply_to_message
    if tid is None and rt:
        tid = rt.message_thread_id or rt.message_id
    if tid is None:
        return

    r = await one(
        "SELECT r.id, r.points, r.battle_id FROM rounds r JOIN battles b ON b.id=r.battle_id "
        "WHERE r.group_id=? AND r.thread_id=? AND r.active=1 AND b.status='active' "
        "AND (b.end_ts=0 OR b.end_ts>?)", m.chat.id, tid, int(time.time()))
    if not r:
        return
    if not await one("SELECT 1 FROM participants WHERE battle_id=? AND user_id=?", r["battle_id"], u.id):
        return

    # 3) bir foydalanuvchi — bir raundda bitta ball (INSERT OR IGNORE kafolatlaydi)
    _, rc = await run("INSERT OR IGNORE INTO scored(round_id,user_id) VALUES(?,?)", r["id"], u.id)
    if not rc:
        return
    await run("UPDATE participants SET points=points+? WHERE battle_id=? AND user_id=?",
              r["points"], r["battle_id"], u.id)
    await run("INSERT INTO plog(battle_id,user_id,delta,reason,ts) VALUES(?,?,?,?,?)",
              r["battle_id"], u.id, r["points"], f"round:{r['id']}", int(time.time()))


##############################################################################
# app/handlers/owner.py
##############################################################################
r_owner = Router()
r_owner.message.filter(F.chat.type == "private", F.from_user.id == OWNER_ID)
r_owner.callback_query.filter(F.from_user.id == OWNER_ID)

SETTINGS = {
    "brand": "Bot nomi",
    "battle_fee": "Batl yaratish narxi (⭐, 0 = bepul)",
    "vip_price": "VIP narxi (⭐)",
    "vip_days": "VIP muddati (kun)",
    "ton_rate": "Kurs: 1 TON = necha ⭐",
    "markup": "Gift importidagi ustama (%)",
    "ref_per_credit": "Nechta do'st = 1 batl krediti (0 = o'chiq)",
    "ton_address": "TON qabul qilish manzili (to'lovlar uchun)",
    "force_sub": "Majburiy obuna kanali (@kanal, bo'sh = o'chiq)",
}
NUMERIC = {"battle_fee", "vip_price", "vip_days", "ton_rate", "markup", "ref_per_credit"}
IMAGES = {"home": "Bosh menyu", "battle": "Batllar", "shop": "Do'kon", "wallet": "Hamyon",
          "profile": "Profil", "owner": "Owner panel", "vip": "VIP", "ref": "Taklif", "top": "Reyting"}
KIND_NAMES = {"premium": "💎 Premium", "stars": "⭐ Stars", "gift": "🎁 Gift", "nft": "🖼 NFT"}


@r_owner.message(Command("owner"))
@r_owner.callback_query(F.data == "o:home")
async def panel(ev, state: FSMContext):
    await state.clear()
    rows = [[B("📊 Statistika", "o:stats"), B("🧾 Buyurtmalar", "o:ord")],
            [B("⚙️ Sozlamalar / narxlar", "o:set"), B("🛍 Do'kon boshqaruvi", "o:shop")],
            [B("🖼 Rasmlar", "o:img"), B("📣 Xabar yuborish", "o:bc")],
            [B("🏠 Menyu", "m:home")]]
    await screen(ev, "owner", "🛠 <b>Owner panel</b>\n\nBarcha narxlar, rasmlar va do'kon shu yerdan boshqariladi.", K(*rows))


@r_owner.callback_query(F.data == "o:stats")
async def stats(c: CallbackQuery):
    day = int(time.time()) - 86400
    users = (await one("SELECT COUNT(*) c FROM users"))["c"]
    new = (await one("SELECT COUNT(*) c FROM users WHERE joined>?", day))["c"]
    ba = (await one("SELECT COUNT(*) c FROM battles WHERE status='active'"))["c"]
    bt = (await one("SELECT COUNT(*) c FROM battles"))["c"]
    pt = (await one("SELECT COUNT(*) c FROM participants"))["c"]
    stars = (await one("SELECT COALESCE(SUM(stars),0) s FROM orders WHERE method='stars' AND status IN ('paid','delivered')"))["s"]
    ton = (await one("SELECT COALESCE(SUM(ton),0) s FROM orders WHERE method='ton' AND status IN ('paid','delivered')"))["s"]
    pend = (await one("SELECT COUNT(*) c FROM orders WHERE status='paid'"))["c"]
    bal = ""
    try:
        sb = await c.bot.get_my_star_balance()
        bal = f"\n🌟 Botning Stars balansi: <b>{sb.amount}</b>"
    except Exception:
        pass
    text = (f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: <b>{users}</b> (24 soatda +{new})\n"
            f"⚔️ Batllar: <b>{ba}</b> faol / {bt} jami\n🙋 Ishtirokchilar: <b>{pt}</b>\n\n"
            f"💰 Tushum: <b>{stars}⭐</b> + <b>{round(ton, 3)} TON</b>\n📦 Yetkazilmagan buyurtmalar: <b>{pend}</b>{bal}\n\n"
            "ℹ️ Stars'ni yechish: Telegram → bot sozlamalari → Balance (Fragment orqali).")
    await screen(c, "owner", text, K([B("⬅️ Orqaga", "o:home")]))


# ------------------------------------------------------------------ sozlamalar

@r_owner.callback_query(F.data == "o:set")
async def settings(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = []
    for k, label in SETTINGS.items():
        v = await S(k)
        rows.append([B(f"{label}: {v[:18] or '—'}", f"o:s:{k}")])
    rows.append([B("⬅️ Orqaga", "o:home")])
    await screen(c, "owner", "⚙️ <b>Sozlamalar</b>\nO'zgartirish uchun bosing 👇", K(*rows))


@r_owner.callback_query(F.data.startswith("o:s:"))
async def setting_pick(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    if key not in SETTINGS:
        return await c.answer()
    await state.set_state(OSet.val)
    await state.update_data(key=key)
    await screen(c, "owner", f"✏️ <b>{SETTINGS[key]}</b>\nHozirgi qiymat: <code>{esc(await S(key)) or '—'}</code>\n\nYangi qiymatni yuboring"
                 + (" (bo'sh qiymat uchun <code>-</code>)" if key in ("force_sub", "ton_address") else "") + ":", cancel_kb())


@r_owner.message(OSet.val, F.text)
async def setting_val(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    v = m.text.strip()
    if v == "-" and key in ("force_sub", "ton_address"):
        v = ""
    if key in NUMERIC and (not v.isdigit() or (key == "ton_rate" and int(v) < 1)):
        return await m.answer("⚠️ Musbat butun son yuboring.", reply_markup=cancel_kb())
    if key == "force_sub" and v and not v.startswith("@"):
        return await m.answer("⚠️ Kanalni @username ko'rinishida yuboring (bot u kanalda admin bo'lishi kerak).", reply_markup=cancel_kb())
    await set_s(key, v)
    await state.clear()
    await m.answer("✅ Saqlandi", reply_markup=K([B("⚙️ Sozlamalar", "o:set")]))


# ------------------------------------------------------------------ rasmlar

@r_owner.callback_query(F.data == "o:img")
async def images(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[B(f"🖼 {name}", f"o:i:{k}")] for k, name in IMAGES.items()] + [[B("⬅️ Orqaga", "o:home")]]
    await screen(c, "owner", "🖼 <b>Rasmlar</b>\nQaysi bo'limning rasmini almashtiramiz?\n(Mini App ham shu rasmlardan foydalanadi)", K(*rows))


@r_owner.callback_query(F.data.startswith("o:i:"))
async def image_pick(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(OImg.photo)
    await state.update_data(key=key)
    await screen(c, "owner", f"🖼 <b>{IMAGES.get(key, key)}</b>\n\nYangi rasmni yuboring (tavsiya: 1280×640, gorizontal).",
                 K([B("♻️ Standartga qaytarish", f"o:ir:{key}")], [B("✖️ Bekor qilish", "x:cancel")]))


@r_owner.message(OImg.photo, F.photo)
async def image_set(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    await set_s(f"img_{key}", m.photo[-1].file_id)
    await state.clear()
    await m.answer("✅ Rasm almashtirildi", reply_markup=K([B("🖼 Rasmlar", "o:img")]))


@r_owner.callback_query(F.data.startswith("o:ir:"))
async def image_reset(c: CallbackQuery, state: FSMContext):
    await set_s(f"img_{c.data.split(':')[2]}", "")
    await state.clear()
    await images(c, state)


# ------------------------------------------------------------------ do'kon

@r_owner.callback_query(F.data == "o:shop")
async def shop_admin(c: CallbackQuery, state: FSMContext):
    await state.clear()
    items = await q("SELECT * FROM items ORDER BY kind, price_stars")
    rows = [[B(f"{'✅' if i['active'] else '🚫'} {i['title'][:28]} · {i['price_stars']}⭐", f"o:it:{i['id']}")] for i in items[:40]]
    rows += [[B("➕ Mahsulot qo'shish", "o:ia"), B("🎁 Giftlarni import", "o:gi")], [B("⬅️ Orqaga", "o:home")]]
    await screen(c, "owner", "🛍 <b>Do'kon boshqaruvi</b>", K(*rows))


@r_owner.callback_query(F.data.startswith("o:it:"))
async def item_view(c: CallbackQuery):
    iid = int(c.data.split(":")[2])
    i = await one("SELECT * FROM items WHERE id=?", iid)
    if not i:
        return await c.answer("Topilmadi")
    text = (f"<b>{esc(i['title'])}</b>\nTuri: {KIND_NAMES.get(i['kind'], i['kind'])}\nNarx: {i['price_stars']}⭐\n"
            f"Payload: <code>{esc(i['payload'])}</code>\nHolat: {'faol' if i['active'] else 'o‘chirilgan'}")
    await screen(c, "owner", text, K([B("🔁 Yoq/o'chir", f"o:itt:{iid}"), B("🗑 O'chirish", f"o:itd:{iid}")],
                                      [B("✏️ Narxni o'zgartirish", f"o:itp:{iid}")], [B("⬅️ Orqaga", "o:shop")]))


@r_owner.callback_query(F.data.startswith("o:itt:"))
async def item_toggle(c: CallbackQuery, state: FSMContext):
    await run("UPDATE items SET active=1-active WHERE id=?", int(c.data.split(":")[2]))
    await shop_admin(c, state)


@r_owner.callback_query(F.data.startswith("o:itd:"))
async def item_delete(c: CallbackQuery, state: FSMContext):
    await run("DELETE FROM items WHERE id=?", int(c.data.split(":")[2]))
    await shop_admin(c, state)


@r_owner.callback_query(F.data.startswith("o:itp:"))
async def item_price(c: CallbackQuery, state: FSMContext):
    await state.set_state(OItem.price)
    await state.update_data(edit=int(c.data.split(":")[2]))
    await screen(c, "owner", "Yangi narxni ⭐ da yuboring:", cancel_kb())


@r_owner.callback_query(F.data == "o:ia")
async def item_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(OItem.kind)
    rows = [[B(n, f"o:ik:{k}")] for k, n in KIND_NAMES.items()] + [[B("✖️ Bekor qilish", "x:cancel")]]
    await screen(c, "owner", "Mahsulot turini tanlang:", K(*rows))


@r_owner.callback_query(OItem.kind, F.data.startswith("o:ik:"))
async def item_kind(c: CallbackQuery, state: FSMContext):
    await state.update_data(kind=c.data.split(":")[2])
    await state.set_state(OItem.title)
    await screen(c, "owner", "Mahsulot <b>nomini</b> yuboring:", cancel_kb())


@r_owner.message(OItem.title, F.text)
async def item_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:60])
    await state.set_state(OItem.descr)
    await m.answer("Qisqa <b>tavsif</b> yuboring (yoki <code>-</code>):", reply_markup=cancel_kb())


@r_owner.message(OItem.descr, F.text)
async def item_descr(m: Message, state: FSMContext):
    await state.update_data(descr="" if m.text.strip() == "-" else m.text.strip()[:300])
    await state.set_state(OItem.price)
    await m.answer("Narxini ⭐ da yuboring (TON'ga kurs bo'yicha avto hisoblanadi):", reply_markup=cancel_kb())


@r_owner.message(OItem.price, F.text)
async def item_price_in(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or int(m.text) < 1:
        return await m.answer("⚠️ Musbat butun son yuboring.", reply_markup=cancel_kb())
    d = await state.get_data()
    if d.get("edit"):
        await run("UPDATE items SET price_stars=? WHERE id=?", int(m.text), d["edit"])
        await state.clear()
        return await m.answer("✅ Narx yangilandi", reply_markup=K([B("🛍 Do'kon", "o:shop")]))
    await state.update_data(price=int(m.text))
    await state.set_state(OItem.payload)
    hint = {"premium": "oylar soni: 3, 6 yoki 12", "gift": "gift ID (import qilish osonroq)",
            "stars": "Stars miqdori (masalan 100)", "nft": "NFT havolasi (https://...)"}[d["kind"]]
    await m.answer(f"Qo'shimcha ma'lumot — {hint}:", reply_markup=cancel_kb())


@r_owner.message(OItem.payload, F.text)
async def item_payload(m: Message, state: FSMContext):
    d = await state.get_data()
    p = m.text.strip()
    if d["kind"] == "premium" and p not in ("3", "6", "12"):
        return await m.answer("⚠️ Faqat 3, 6 yoki 12.", reply_markup=cancel_kb())
    await run("INSERT INTO items(kind,title,descr,price_stars,payload) VALUES(?,?,?,?,?)",
              d["kind"], d["title"], d["descr"], d["price"], p)
    await state.clear()
    await m.answer("✅ Mahsulot qo'shildi", reply_markup=K([B("🛍 Do'kon", "o:shop")]))


@r_owner.callback_query(F.data == "o:gi")
async def gifts_import(c: CallbackQuery, state: FSMContext):
    markup = await SI("markup", 15)
    try:
        gifts = (await c.bot.get_available_gifts()).gifts
    except Exception as e:
        return await c.answer(f"Giftlarni olib bo'lmadi: {e}"[:190], show_alert=True)
    added = 0
    for g in gifts:
        if await one("SELECT 1 FROM items WHERE kind='gift' AND payload=?", g.id):
            continue
        price = math.ceil(g.star_count * (1 + markup / 100))
        await run("INSERT INTO items(kind,title,descr,price_stars,payload) VALUES('gift',?,?,?,?)",
                  f"{g.sticker.emoji or '🎁'} Gift ({g.star_count}⭐)", "Avtomatik yetkaziladi (botda Stars balansi bo'lsa)", price, g.id)
        added += 1
    await c.answer(f"✅ {added} ta yangi gift qo'shildi (ustama {markup}%)", show_alert=True)
    await shop_admin(c, state)


# ------------------------------------------------------------------ buyurtmalar

@r_owner.callback_query(F.data == "o:ord")
async def orders(c: CallbackQuery):
    rows = await q("SELECT * FROM orders WHERE status IN ('pending','paid') AND kind='item' ORDER BY id DESC LIMIT 15")
    kb = [[B(f"#{o['id']} · {o['status']} · {o['stars']}⭐ · {o['method'] or '-'}", f"o:od:{o['id']}")] for o in rows]
    kb.append([B("⬅️ Orqaga", "o:home")])
    await screen(c, "owner", "🧾 <b>Buyurtmalar</b> (kutilayotgan / yetkazilmagan)" + ("" if rows else "\n\nHammasi joyida ✅"), K(*kb))


@r_owner.callback_query(F.data.startswith("o:od:"))
async def order_view(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    it = await one("SELECT title FROM items WHERE id=?", o["item_id"]) if o else None
    if not o:
        return await c.answer("Topilmadi")
    text = (f"🧾 <b>Buyurtma #{oid}</b>\n{esc(it['title']) if it else '?'}\nXaridor: <a href=\"tg://user?id={o['user_id']}\">{o['user_id']}</a>\n"
            f"Holat: {o['status']} · {o['method'] or '-'}\nNarx: {o['stars']}⭐ / {o['ton']} TON\nKod: <code>{o['code']}</code>\nVaqt: {fmt_ts(o['ts'])}")
    rows = []
    if o["status"] == "pending":
        rows.append([B("✅ To'langan deb tasdiqlash", f"o:cf:{oid}")])
    if o["status"] == "paid":
        rows.append([B("📦 Yetkazildi", f"o:dlv:{oid}")])
    rows.append([B("⬅️ Orqaga", "o:ord")])
    await screen(c, "owner", text, K(*rows))


@r_owner.callback_query(F.data.startswith("o:cf:"))
async def order_confirm(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    await fulfill(c.bot, oid, "manual")
    await c.answer("Tasdiqlandi")
    await orders(c)


@r_owner.callback_query(F.data.startswith("o:dlv:"))
async def order_deliver(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    if o and o["status"] == "paid":
        await run("UPDATE orders SET status='delivered' WHERE id=?", oid)
        try:
            await c.bot.send_message(o["user_id"], f"🎉 Buyurtma #{oid} yetkazib berildi! Rahmat.")
        except Exception:
            pass
    await c.answer("✅ Belgilandi", show_alert=True)
    try:
        await c.message.delete()
    except Exception:
        pass


# ------------------------------------------------------------------ xabar yuborish

@r_owner.callback_query(F.data == "o:bc")
async def bc_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(OBc.msg)
    await screen(c, "owner", "📣 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring (matn, rasm, tugma — hammasi nusxalanadi):", cancel_kb())


@r_owner.message(OBc.msg)
async def bc_send(m: Message, state: FSMContext):
    await state.clear()
    ids = [r["id"] for r in await q("SELECT id FROM users WHERE banned=0")]
    await m.answer(f"📤 {len(ids)} ta foydalanuvchiga yuborish boshlandi…")
    asyncio.create_task(_broadcast(m.bot, m.chat.id, m.message_id, ids))


async def _broadcast(bot, from_chat, mid, ids):
    ok = fail = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, from_chat, mid)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await bot.send_message(from_chat, f"✅ Yuborildi: {ok}\n❌ Yetmadi: {fail}")


# ------------------------------------------------------------------ buyruqlar

@r_owner.message(Command("give"))
async def give(m: Message, command: CommandObject):
    a = (command.args or "").split()
    if len(a) != 2 or not a[0].isdigit() or not a[1].lstrip("-").isdigit():
        return await m.answer("Foydalanish: /give <user_id> <stars>")
    _, rc = await run("UPDATE users SET balance=balance+? WHERE id=?", int(a[1]), int(a[0]))
    await m.answer("✅ Bajarildi" if rc else "❌ Foydalanuvchi topilmadi")


@r_owner.message(Command("ban", "unban"))
async def ban(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        return await m.answer(f"Foydalanish: /{command.command} <user_id>")
    _, rc = await run("UPDATE users SET banned=? WHERE id=?", 1 if command.command == "ban" else 0, int(arg))
    await m.answer("✅ Bajarildi" if rc else "❌ Foydalanuvchi topilmadi")


##############################################################################
# app/main.py
##############################################################################
class Gate(BaseMiddleware):
    """Foydalanuvchini saqlaydi, bloklanganlarni to'xtatadi, majburiy obunani tekshiradi."""

    async def __call__(self, handler, event, data):
        u = event.from_user
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        if not u or u.is_bot or not chat or chat.type != "private":
            return await handler(event, data)
        data["is_new"] = await upsert_user(u)
        row = await one("SELECT banned FROM users WHERE id=?", u.id)
        if row and row["banned"]:
            return
        fs = await S("force_sub")
        if fs and u.id != OWNER_ID and not (isinstance(event, CallbackQuery) and event.data == "sub:check"):
            try:
                ok = (await event.bot.get_chat_member(fs, u.id)).status not in ("left", "kicked")
            except Exception:
                ok = True  # bot kanalda bo'lmasa foydalanuvchini to'smaymiz
            if not ok:
                kb = K([B("📢 Kanalga obuna bo'lish", url=f"https://t.me/{fs.lstrip('@')}")],
                       [B("✅ Tekshirish", "sub:check")])
                await screen(event, "home", "🔒 Botdan foydalanish uchun avval kanalimizga obuna bo'ling:", kb)
                return
        return await handler(event, data)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db_init()
    make_banners()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Gate())
    dp.callback_query.outer_middleware(Gate())

    @dp.callback_query(lambda c: c.data == "sub:check")
    async def sub_check(c: CallbackQuery):
        await c.answer("✅ Rahmat!")
        try:
            await c.message.delete()
        except Exception:
            pass
        await bot.send_message(c.from_user.id, "Obuna tasdiqlandi ✅ /menu ni bosing 👇")

    dp.include_routers(r_comments, r_owner, r_user, r_battle, r_shop, r_pay)

    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in USER_CMDS],
                              scope=BotCommandScopeAllPrivateChats())
    await set_scope_commands(bot, OWNER_ID, OWNER_CMDS)
    for r in await q("SELECT DISTINCT owner_id FROM channels"):
        if r["owner_id"] != OWNER_ID:
            await set_scope_commands(bot, r["owner_id"], ORG_CMDS)

    runner = await start_web(bot)
    asyncio.create_task(ticker(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        await db_close()


if __name__ == "__main__":
    asyncio.run(main())
