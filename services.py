import asyncio
import logging
import math
import secrets
import time
from datetime import datetime, timezone, timedelta

import aiohttp
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton as IB

from . import db
from .db import q, one, run, S, SI
from .ui import esc
from .config import TZ, TONCENTER_KEY, OWNER_ID
from .kb import K, B

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

def calc_uzs(stars: int, rate: int) -> int:
    """⭐ narxini so'mga aylantiradi (1000 so'mgacha yuqoriga yaxlitlanadi)."""
    return int(math.ceil(stars * max(1, rate) / 1000.0) * 1000)


def fmt_uzs(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " so'm"


async def new_order(uid: int, kind: str, stars: int, item_id: int = 0) -> int:
    rate = max(1, await SI("ton_rate", 200))
    ton = max(0.01, round(stars / rate, 3))
    uzs = calc_uzs(stars, await SI("uzs_rate", 230))
    code = secrets.token_hex(3).upper()
    oid, _ = await run("INSERT INTO orders(user_id,kind,item_id,stars,ton,status,code,ts,uzs) "
                       "VALUES(?,?,?,?,?,'pending',?,?,?)",
                       uid, kind, item_id, stars, ton, code, int(time.time()), uzs)
    return oid


def fmt_meta(meta: str) -> str:
    """Buyurtma ma'lumoti: 'ID|server' -> chiroyli ko'rinish."""
    if not meta:
        return ""
    pid, _, srv = meta.partition("|")
    return f"ID: <code>{esc(pid)}</code>" + (f" · Server: <code>{esc(srv)}</code>" if srv else "")


async def new_game_order(uid: int, pack_id: int, meta: str):
    p = await one("SELECT * FROM packs WHERE id=? AND active=1 AND price_uzs>0", pack_id)
    if not p:
        return None
    stars = max(1, math.ceil(p["price_uzs"] / max(1, await SI("uzs_rate", 230))))
    ton = max(0.01, round(stars / max(1, await SI("ton_rate", 200)), 3))
    oid, _ = await run("INSERT INTO orders(user_id,kind,item_id,stars,ton,status,code,ts,uzs,meta) "
                       "VALUES(?,'game',?,?,?,'pending',?,?,?,?)",
                       uid, pack_id, stars, ton, secrets.token_hex(3).upper(), int(time.time()), p["price_uzs"], meta)
    return oid


async def order_title(o) -> str:
    k = o["kind"]
    if k == "game":
        p = await one("SELECT p.title, g.name, g.emoji FROM packs p JOIN games g ON g.id=p.game_id WHERE p.id=?", o["item_id"])
        return f"{p['emoji']} {p['name']} — {p['title']}" if p else "🎮 O'yin donati"
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
    elif kind == "game":
        await deliver_game(bot, o)
    return True


async def deliver_game(bot: Bot, o):
    """O'yin donati qo'lda yetkaziladi: egasiga to'liq ma'lumot bilan xabar boradi."""
    title = await order_title(o)
    user = await one("SELECT * FROM users WHERE id=?", o["user_id"])
    who = f'<a href="tg://user?id={o["user_id"]}">{esc(user["name"] if user else str(o["user_id"]))}</a>'
    if user and user["username"]:
        who += f' (@{user["username"]})'
    await _dm(bot, o["user_id"],
              f"✅ To'lov qabul qilindi!\n🎮 {esc(title)}\n{fmt_meta(o['meta'])}\n\n"
              f"Buyurtma #{o['id']} tez orada yetkaziladi. Tayyor bo'lganda xabar beramiz.", K([HOME_B]))
    await _dm(bot, OWNER_ID,
              f"🎮 <b>Yangi donat buyurtma #{o['id']}</b>\n{esc(title)}\n{fmt_meta(o['meta'])}\n"
              f"Xaridor: {who}\nTo'lov: {o['method']} · {fmt_uzs(o['uzs'])}",
              K([B("✅ Yetkazildi", f"o:dlv:{o['id']}")]))


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
    if user and user["username"]:
        who += f' (@{user["username"]})'
    how = ""
    if it and it["kind"] == "stars":
        how = f"\n\n⭐ <b>{esc(it['payload'])} Stars</b> ni Fragment orqali shu foydalanuvchiga sovg'a qiling, so'ng «Yetkazildi» ni bosing."
    elif it and it["kind"] in ("premium", "gift"):
        how = ("\n\n⚠️ Avtomatik yetkazilmadi — botning Stars balansi yetarli emas bo'lishi mumkin. "
               "Qo'lda sovg'a qiling va «Yetkazildi» ni bosing.")
    elif it and it["kind"] == "nft":
        how = "\n\n🖼 NFT ni qo'lda o'tkazib bering, so'ng «Yetkazildi» ni bosing."
    await _dm(bot, OWNER_ID,
              f"🧾 <b>Qo'lda yetkazish kerak</b>\nBuyurtma #{o['id']} — {esc(it['title'] if it else '?')}\n"
              f"Xaridor: {who}\nTo'lov: {o['method']}{how}",
              K([B("✅ Yetkazildi", f"o:dlv:{o['id']}")]))


HOME_B = B("🏠 Menyu", "m:home")
