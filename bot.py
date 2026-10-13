"""
Battle Hub — Telegram batl boti (bitta fayl, Mini App ham ichida)

O'rnatish:   pip install -r requirements.txt   (aiogram>=3.22, aiohttp, Pillow, python-dotenv)
Sozlash:     shu papkada .env fayl yarating:
                 BOT_TOKEN=123456:ABC...      (@BotFather'dan)
                 OWNER_ID=123456789           (sizning Telegram ID'ingiz)
                 WEBAPP_URL=https://sizning-domen.uz   (ixtiyoriy, Mini App uchun ochiq HTTPS)
                 PORT=8080
                 DB_PATH=data/bot.db
                 TZ_OFFSET=5
                 TONCENTER_KEY=               (ixtiyoriy)
                 ORDER_TTL_MIN=60             (buyurtma to'lov muddati, daqiqa)
                 MAX_DELIVERY_RETRIES=6       (avto-yetkazishning maksimal urinishi)
Ishga tushirish:   python bot.py

Eslatma: battle/ papkasi kerak emas, hammasi shu faylning ichida.
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
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from aiohttp import web
from aiogram import Bot, Dispatcher, BaseMiddleware, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter,
                                TelegramServerError)
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

from types import SimpleNamespace
from aiogram.types import InputMediaPhoto, MessageReactionUpdated


##############################################################################
# battle paketi (avval alohida battle/ papkasida edi; endi shu faylning ichida)
##############################################################################
SOURCES = ("reaction", "comment", "star", "manual", "bonus", "penalty", "legacy")

BATTLE_M5 = [
    "ALTER TABLE battles ADD COLUMN btype TEXT NOT NULL DEFAULT 'vote'",
    "ALTER TABLE battles ADD COLUMN state TEXT NOT NULL DEFAULT 'ACTIVE'",
    "ALTER TABLE battles ADD COLUMN finished_at INTEGER",
    "ALTER TABLE battles ADD COLUMN reward TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE participants ADD COLUMN joined_at INTEGER",
    "ALTER TABLE participants ADD COLUMN meta TEXT NOT NULL DEFAULT ''",   # NFT/avatar va h.k. (JSON)
    "ALTER TABLE rounds ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE rounds ADD COLUMN start_at INTEGER",
    "ALTER TABLE rounds ADD COLUMN end_at INTEGER",
    """CREATE TABLE IF NOT EXISTS battle_settings(
        battle_id INTEGER PRIMARY KEY REFERENCES battles(id),
        reaction_score INTEGER NOT NULL DEFAULT 1, comment_score INTEGER NOT NULL DEFAULT 3,
        star_score INTEGER NOT NULL DEFAULT 1, participant_limit INTEGER NOT NULL DEFAULT 0,
        comments_per_round INTEGER NOT NULL DEFAULT 1, allow_negative INTEGER NOT NULL DEFAULT 0,
        reaction_weights TEXT NOT NULL DEFAULT '{}', auto_finish INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS battle_score_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, battle_id INTEGER NOT NULL REFERENCES battles(id),
        round_id INTEGER, user_id INTEGER NOT NULL, source TEXT NOT NULL, amount INTEGER NOT NULL,
        idem_key TEXT NOT NULL, actor_id INTEGER, reason TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
        UNIQUE(battle_id, idem_key))""",
    "CREATE INDEX IF NOT EXISTS ix_bse_user ON battle_score_events(battle_id, user_id, source)",
    """CREATE TABLE IF NOT EXISTS battle_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, battle_id INTEGER, round_id INTEGER, user_id INTEGER,
        action TEXT NOT NULL, amount INTEGER, source TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_bev_battle ON battle_events(battle_id, id)",
    """CREATE TABLE IF NOT EXISTS battle_winners(
        battle_id INTEGER NOT NULL REFERENCES battles(id), rank INTEGER NOT NULL, user_id INTEGER NOT NULL,
        score INTEGER NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(battle_id, rank))""",
    """CREATE TABLE IF NOT EXISTS contest_draws(
        battle_id INTEGER PRIMARY KEY REFERENCES battles(id), seed TEXT NOT NULL, candidates_hash TEXT NOT NULL,
        candidate_count INTEGER NOT NULL, winner_id INTEGER NOT NULL, selected_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS battle_feedback(
        id INTEGER PRIMARY KEY AUTOINCREMENT, battle_id INTEGER NOT NULL REFERENCES battles(id), user_id INTEGER NOT NULL,
        text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'visible', created_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_bfb ON battle_feedback(battle_id, user_id, created_at)",
    """CREATE TABLE IF NOT EXISTS nft_entries(
        battle_id INTEGER NOT NULL REFERENCES battles(id), user_id INTEGER NOT NULL, name TEXT NOT NULL,
        collection TEXT NOT NULL DEFAULT '', rarity TEXT NOT NULL DEFAULT '', image_file_id TEXT NOT NULL DEFAULT '',
        meta TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL, PRIMARY KEY(battle_id, user_id))""",
    "CREATE INDEX IF NOT EXISTS ix_part_rank ON participants(battle_id, points DESC)",
    "UPDATE battles SET state = CASE status WHEN 'active' THEN 'ACTIVE' ELSE 'FINISHED' END",
    "UPDATE participants SET joined_at = COALESCE(joined_at, 0)",
    "INSERT OR IGNORE INTO battle_settings(battle_id, comment_score) SELECT id, points FROM battles",
    # eski ballar «legacy» manbasi bilan hisobga olinadi: breakdown va keshning yig'indisi mos bo'lsin
    """INSERT OR IGNORE INTO battle_score_events(battle_id,user_id,source,amount,idem_key,reason,created_at)
        SELECT battle_id,user_id,'legacy',points,'legacy:'||battle_id||':'||user_id,'migration',strftime('%s','now')
        FROM participants WHERE points<>0""",
]


BATTLE_M6 = [
    """CREATE TABLE IF NOT EXISTS battle_admins(
        battle_id INTEGER NOT NULL REFERENCES battles(id), user_id INTEGER NOT NULL, role TEXT NOT NULL,
        added_by INTEGER, created_at INTEGER NOT NULL, PRIMARY KEY(battle_id, user_id))""",
    "CREATE INDEX IF NOT EXISTS ix_badm_user ON battle_admins(user_id)",
]


def _make_texts():
    """battle/texts.py"""
    TEXTS = {"uz": {
        "no_battle": "❌ Battle topilmadi.",
        "ended": "❌ Bu Battle allaqachon yakunlangan.",
        "denied": "❌ Sizda bu amalni bajarish huquqi yo'q.",
        "joined_already": "⚠️ Siz allaqachon bu Battle'da qatnashgansiz.",
        "post_fail": "❌ Kanalga post yuborib bo'lmadi. Bot kanalda admin ekanligini va post yuborish huquqi borligini tekshiring.",
        "state.ACTIVE": "🟢 FAOL", "state.REGISTRATION": "📝 RO'YXAT", "state.PAUSED": "⏸ PAUZADA", "state.DRAFT": "✏️ QORALAMA",
        "state.FINISHING": "⏳ YAKUNLANMOQDA", "state.FINISHED": "🏁 YAKUNLANGAN", "state.SETTLED": "🏁 YAKUNLANGAN",
        "state.CANCELLED": "❌ BEKOR QILINGAN", "state.ARCHIVED": "🗄 ARXIV",
        "head.vote": "🔥 OVOZ BATTLE", "head.nft": "🖼 NFT BATTLE", "head.contest": "🎲 RANDOM CONTEST",
        "src.reaction": "❤️ Reaction", "src.comment": "💬 Izoh", "src.star": "⭐ Stars", "src.manual": "➕ Qo'lda",
        "src.bonus": "🎁 Bonus", "src.penalty": "➖ Jarima", "src.legacy": "📦 Avvalgi",
    }}


    def T(key: str, lang: str = "uz", **kw) -> str:
        s = TEXTS.get(lang, TEXTS["uz"]).get(key) or TEXTS["uz"].get(key, key)
        return s.format(**kw) if kw else s
    return SimpleNamespace(**{k: v for k, v in locals().items() if not k.startswith('_')})


def _make_engine():
    """battle/engine.py"""
    import hashlib
    import json
    import secrets
    import time


    ACTIVE_LIKE = ("REGISTRATION", "ACTIVE")
    TRANSITIONS = {
        "DRAFT": {"REGISTRATION", "ACTIVE", "CANCELLED"},
        "REGISTRATION": {"ACTIVE", "CANCELLED"},
        "ACTIVE": {"PAUSED", "FINISHING", "FINISHED", "CANCELLED"},
        "PAUSED": {"ACTIVE", "FINISHED", "CANCELLED"},
        "FINISHING": {"FINISHED"},
        "FINISHED": {"SETTLED", "ARCHIVED"},
        "SETTLED": {"ARCHIVED"},
        "CANCELLED": {"ARCHIVED"},
        "ARCHIVED": set(),
    }


    class BattleError(Exception):
        """Foydalanuvchiga ko'rsatsa bo'ladigan aniq xato."""


    def _legacy(state: str) -> str:
        return "active" if state in ACTIVE_LIKE or state == "PAUSED" else "ended"


    async def log_event(t, action, *, battle_id=None, round_id=None, user_id=None, amount=None, source="", meta="", ts=None):
        if not isinstance(meta, str):
            meta = json.dumps(meta, ensure_ascii=False, default=str)
        await t.execute("INSERT INTO battle_events(battle_id,round_id,user_id,action,amount,source,metadata,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        battle_id, round_id, user_id, action, amount, source, meta[:2000], ts or int(time.time()))


    async def transition(t, bid: int, to: str, actor: int | None = None, now: int | None = None) -> bool:
        """Holat o'tishi. Ruxsat etilmagan o'tish -> BattleError. Bir vaqtda ikki jarayon: faqat bittasi muvaffaq (UPDATE ... WHERE state=old)."""
        b = await t.one("SELECT state FROM battles WHERE id=?", bid)
        if not b:
            raise BattleError("❌ Battle topilmadi.")
        cur = b["state"]
        if cur == to:
            return False
        if to not in TRANSITIONS.get(cur, ()):
            raise BattleError(f"❌ Battle holatini {cur} dan {to} ga o'zgartirib bo'lmaydi.")
        ts = now or int(time.time())
        _, rc = await t.execute(
            "UPDATE battles SET state=?, status=?, finished_at=CASE WHEN ? IN ('FINISHED','CANCELLED') THEN ? ELSE finished_at END "
            "WHERE id=? AND state=?", to, _legacy(to), to, ts, bid, cur)
        if rc and to in ("FINISHED", "CANCELLED"):
            await t.execute("UPDATE rounds SET active=0, status='closed', end_at=? WHERE battle_id=? AND active=1", ts, bid)
        if rc:
            await log_event(t, f"battle.{to.lower()}", battle_id=bid, user_id=actor, ts=ts)
        return bool(rc)


    async def get_settings(t, bid: int) -> dict:
        r = await t.one("SELECT * FROM battle_settings WHERE battle_id=?", bid)
        if not r:
            await t.execute("INSERT OR IGNORE INTO battle_settings(battle_id) VALUES(?)", bid)
            r = await t.one("SELECT * FROM battle_settings WHERE battle_id=?", bid)
        d = dict(r)
        d["reaction_weights"] = json.loads(d.get("reaction_weights") or "{}")
        return d


    async def join(t, bid: int, uid: int, nick: str, now: int | None = None) -> str:
        """'joined' | 'exists'. FINISHED/PAUSED/limit -> BattleError."""
        b = await t.one("SELECT state FROM battles WHERE id=?", bid)
        if not b:
            raise BattleError("❌ Battle topilmadi.")
        if b["state"] not in ACTIVE_LIKE:
            raise BattleError("❌ Bu Battle allaqachon yakunlangan yoki hozir qatnashish yopiq.")
        s = await get_settings(t, bid)
        if s["participant_limit"]:
            n = (await t.one("SELECT COUNT(*) c FROM participants WHERE battle_id=?", bid))["c"]
            if n >= s["participant_limit"] and not await t.one("SELECT 1 FROM participants WHERE battle_id=? AND user_id=?", bid, uid):
                raise BattleError("❌ Ishtirokchilar limiti to'lgan.")
        ts = now or int(time.time())
        _, rc = await t.execute("INSERT OR IGNORE INTO participants(battle_id,user_id,nick,joined_at) VALUES(?,?,?,?)",
                                bid, uid, nick[:24], ts)
        if not rc:
            return "exists"
        await log_event(t, "battle.joined", battle_id=bid, user_id=uid, ts=ts)
        return "joined"


    async def add_score(t, bid: int, uid: int, source: str, amount: int, idem: str, *, round_id=None,
                        actor: int | None = None, reason: str = "", now: int | None = None) -> bool:
        """Ball qo'shadi. True = yangi yozuv, False = takror (idem_key allaqachon bor). Kesh (participants.points) shu tranzaksiyada yangilanadi."""
        if source not in SOURCES:
            raise BattleError("❌ Noma'lum ball manbasi.")
        if not isinstance(amount, int) or amount == 0:
            raise BattleError("❌ Ball noldan farqli butun son bo'lishi kerak.")
        b = await t.one("SELECT state FROM battles WHERE id=?", bid)
        if not b:
            raise BattleError("❌ Battle topilmadi.")
        if b["state"] != "ACTIVE":
            raise BattleError("❌ Bu Battle faol emas — ball qo'shib bo'lmaydi.")
        if not await t.one("SELECT 1 FROM participants WHERE battle_id=? AND user_id=?", bid, uid):
            raise BattleError("❌ Foydalanuvchi bu Battle'da qatnashmaydi.")
        if amount < 0:
            s = await get_settings(t, bid)
            if source not in ("penalty", "manual") or (source == "manual" and not s["allow_negative"]):
                raise BattleError("❌ Manfiy ball uchun ruxsat yo'q.")
        if round_id is not None:
            r = await t.one("SELECT active FROM rounds WHERE id=? AND battle_id=?", round_id, bid)
            if not r or not r["active"]:
                raise BattleError("❌ Raund yopilgan.")
        ts = now or int(time.time())
        _, rc = await t.execute(
            "INSERT OR IGNORE INTO battle_score_events(battle_id,round_id,user_id,source,amount,idem_key,actor_id,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", bid, round_id, uid, source, amount, idem, actor, reason[:200], ts)
        if not rc:
            return False
        await t.execute("UPDATE participants SET points=points+? WHERE battle_id=? AND user_id=?", amount, bid, uid)
        await log_event(t, f"battle.{source}_scored", battle_id=bid, round_id=round_id, user_id=uid, amount=amount, source=source,
                        meta={"actor": actor, "reason": reason}, ts=ts)
        return True


    async def standings(t, bid: int, limit: int = 10):
        return await t.query("SELECT user_id, nick, points FROM participants WHERE battle_id=? ORDER BY points DESC, joined_at, user_id LIMIT ?",
                             bid, limit)


    async def rank_of(t, bid: int, uid: int):
        me = await t.one("SELECT points, joined_at FROM participants WHERE battle_id=? AND user_id=?", bid, uid)
        if not me:
            return None
        r = await t.one("SELECT COUNT(*) c FROM participants WHERE battle_id=? AND (points>? OR (points=? AND (joined_at<? OR "
                        "(joined_at=? AND user_id<?))))", bid, me["points"], me["points"], me["joined_at"], me["joined_at"], uid)
        return r["c"] + 1


    async def breakdown(t, bid: int) -> dict:
        rows = await t.query("SELECT source, COALESCE(SUM(amount),0) s, COUNT(*) n FROM battle_score_events WHERE battle_id=? GROUP BY source", bid)
        out = {s: {"sum": 0, "n": 0} for s in SOURCES}
        for r in rows:
            out[r["source"]] = {"sum": r["s"], "n": r["n"]}
        out["participants"] = (await t.one("SELECT COUNT(*) c FROM participants WHERE battle_id=?", bid))["c"]
        out["total"] = (await t.one("SELECT COALESCE(SUM(points),0) s FROM participants WHERE battle_id=?", bid))["s"]
        return out


    async def finish(t, bid: int, actor=None, now=None) -> bool:
        b = await t.one("SELECT state FROM battles WHERE id=?", bid)
        if not b:
            raise BattleError("❌ Battle topilmadi.")
        if b["state"] == "PAUSED":
            await transition(t, bid, "ACTIVE", actor, now)
        return await transition(t, bid, "FINISHED", actor, now)


    async def settle(t, bid: int, actor=None, now=None):
        """FINISHED -> SETTLED. Birinchi 3 o'rinni bir marta yozadi. Takror chaqiruv None qaytaradi (reward ikki marta berilmaydi)."""
        ts = now or int(time.time())
        b = await t.one("SELECT state, btype FROM battles WHERE id=?", bid)
        if not b:
            raise BattleError("❌ Battle topilmadi.")
        if b["state"] != "FINISHED":
            return None
        if not await transition(t, bid, "SETTLED", actor, ts):
            return None
        top = await standings(t, bid, 3)
        for i, r in enumerate(top, 1):
            await t.execute("INSERT OR IGNORE INTO battle_winners(battle_id,rank,user_id,score,created_at) VALUES(?,?,?,?,?)",
                            bid, i, r["user_id"], r["points"], ts)
        await log_event(t, "battle.reward_granted", battle_id=bid, user_id=top[0]["user_id"] if top else None, ts=ts)
        return [dict(r) for r in top]


    async def draw_winner(t, bid: int, actor=None, now=None, seed: str | None = None) -> dict:
        """Random Contest: g'olibni tekshiriladigan tarzda tanlaydi. Nomzodlar user_id bo'yicha tartiblanadi,
        seed (CSPRNG) saqlanadi: sha256(seed:nomzodlar) % n. Ikkinchi chaqiruv mavjud g'olibni qaytaradi."""
        ts = now or int(time.time())
        b = await t.one("SELECT state, btype FROM battles WHERE id=?", bid)
        if not b:
            raise BattleError("❌ Contest topilmadi.")
        if b["btype"] != "contest":
            raise BattleError("❌ Bu Random Contest emas.")
        ex = await t.one("SELECT * FROM contest_draws WHERE battle_id=?", bid)
        if ex:
            return {**dict(ex), "repeated": True}
        if b["state"] not in ("FINISHED", "ACTIVE", "PAUSED"):
            raise BattleError("❌ Contest holati g'olib tanlashga mos emas.")
        cands = [r["user_id"] for r in await t.query("SELECT user_id FROM participants WHERE battle_id=? ORDER BY user_id", bid)]
        if not cands:
            raise BattleError("❌ Ishtirokchilar yo'q — g'olib tanlab bo'lmaydi.")
        seed = seed or secrets.token_hex(16)
        csv = ",".join(map(str, cands))
        chash = hashlib.sha256(csv.encode()).hexdigest()
        idx = int(hashlib.sha256(f"{seed}:{csv}".encode()).hexdigest(), 16) % len(cands)
        winner = cands[idx]
        _, rc = await t.execute("INSERT OR IGNORE INTO contest_draws(battle_id,seed,candidates_hash,candidate_count,winner_id,selected_at) "
                                "VALUES(?,?,?,?,?,?)", bid, seed, chash, len(cands), winner, ts)
        if not rc:
            ex = await t.one("SELECT * FROM contest_draws WHERE battle_id=?", bid)
            return {**dict(ex), "repeated": True}
        await log_event(t, "contest.winner_selected", battle_id=bid, user_id=winner, meta={"seed": seed, "n": len(cands)}, ts=ts)
        if b["state"] != "FINISHED":
            await finish(t, bid, actor, ts)
        return {"battle_id": bid, "seed": seed, "candidates_hash": chash, "candidate_count": len(cands),
                "winner_id": winner, "selected_at": ts, "repeated": False}
    return SimpleNamespace(**{k: v for k, v in locals().items() if not k.startswith('_')})


def _make_roles():
    """battle/roles.py"""
    BattleError, log_event = BE.BattleError, BE.log_event
    import time


    ROLES = ("manager", "moderator")
    MAX_ADMINS = 10
    ALL_PERMS = {"view", "score", "round", "state", "settings", "feedback", "cancel", "roles"}
    PERMS = {
        "owner": ALL_PERMS,
        "manager": {"view", "score", "round", "state", "settings", "feedback"},
        "moderator": {"view", "feedback"},
    }
    LABEL = {"owner": "👑 Egasi", "manager": "🛡 Admin", "moderator": "🧹 Moderator"}


    async def role_of(t, b, uid: int, global_owner: int | None = None):
        """'owner' | 'manager' | 'moderator' | None."""
        if not b or uid is None:
            return None
        if b["owner_id"] == uid or (global_owner is not None and uid == global_owner):
            return "owner"
        r = await t.one("SELECT role FROM battle_admins WHERE battle_id=? AND user_id=?", b["id"], uid)
        return r["role"] if r else None


    async def can(t, b, uid: int, perm: str, global_owner: int | None = None) -> bool:
        role = await role_of(t, b, uid, global_owner)
        return bool(role) and perm in PERMS.get(role, ())


    async def admins(t, bid: int):
        return await t.query("SELECT a.user_id, a.role, u.name, u.username FROM battle_admins a LEFT JOIN users u ON u.id=a.user_id "
                             "WHERE a.battle_id=? ORDER BY a.role, a.created_at", bid)


    async def admin_ids(t, bid: int) -> list[int]:
        return [r["user_id"] for r in await t.query("SELECT user_id FROM battle_admins WHERE battle_id=?", bid)]


    async def grant(t, bid: int, uid: int, role: str, actor: int, now: int | None = None) -> str:
        """'added' | 'changed' | 'same'. Egaga rol berib bo'lmaydi; maksimum MAX_ADMINS."""
        if role not in ROLES:
            raise BattleError("❌ Noma'lum rol.")
        b = await t.one("SELECT owner_id FROM battles WHERE id=?", bid)
        if not b:
            raise BattleError("❌ Battle topilmadi.")
        if uid == b["owner_id"]:
            raise BattleError("❌ Battle egasiga rol berib bo'lmaydi.")
        ex = await t.one("SELECT role FROM battle_admins WHERE battle_id=? AND user_id=?", bid, uid)
        if ex and ex["role"] == role:
            return "same"
        if not ex and (await t.one("SELECT COUNT(*) c FROM battle_admins WHERE battle_id=?", bid))["c"] >= MAX_ADMINS:
            raise BattleError(f"❌ Adminlar soni {MAX_ADMINS} tadan oshmasligi kerak.")
        ts = now or int(time.time())
        await t.execute("INSERT INTO battle_admins(battle_id,user_id,role,added_by,created_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(battle_id,user_id) DO UPDATE SET role=excluded.role, added_by=excluded.added_by", bid, uid, role, actor, ts)
        await log_event(t, "battle.role_granted", battle_id=bid, user_id=uid, source=role, meta={"actor": actor}, ts=ts)
        return "changed" if ex else "added"


    async def revoke(t, bid: int, uid: int, actor: int, now: int | None = None) -> bool:
        _, rc = await t.execute("DELETE FROM battle_admins WHERE battle_id=? AND user_id=?", bid, uid)
        if rc:
            await log_event(t, "battle.role_revoked", battle_id=bid, user_id=uid, meta={"actor": actor}, ts=now)
        return bool(rc)
    return SimpleNamespace(**{k: v for k, v in locals().items() if not k.startswith('_')})


def _make_views():
    """battle/views.py"""
    T = BT.T
    import json
    import time
    from html import escape as esc


    BAR = "━━━━━━━━━━━━━━"
    MEDALS = ("🥇", "🥈", "🥉")


    def num(n) -> str:
        return f"{int(n):,}"


    def left(end_ts: int, now: int | None = None) -> str:
        sec = (end_ts or 0) - (now or int(time.time()))
        if sec <= 0:
            return "tugamoqda"
        d, r = divmod(sec, 86400)
        h, r = divmod(r, 3600)
        m = r // 60
        return (f"{d} kun " if d else "") + f"{h:02d}:{m:02d}"


    async def top_rows(t, bid: int, limit: int = 10):
        return await t.query(
            "SELECT p.user_id, COALESCE(NULLIF(n.name,''), p.nick) label, p.nick, p.points, u.username, n.collection, n.rarity "
            "FROM participants p LEFT JOIN users u ON u.id=p.user_id LEFT JOIN nft_entries n ON n.battle_id=p.battle_id AND n.user_id=p.user_id "
            "WHERE p.battle_id=? ORDER BY p.points DESC, p.joined_at, p.user_id LIMIT ?", bid, limit)


    def who(r) -> str:
        s = f'<a href="tg://user?id={r["user_id"]}">{esc(r["label"])}</a>'
        if r["username"]:
            s += f' (@{r["username"]})'
        if r["collection"]:
            s += f' · {esc(r["collection"])}' + (f' [{esc(r["rarity"])}]' if r["rarity"] else "")
        return s


    async def post_card(t, b, fmt_ts) -> str:
        bid, bt = b["id"], b["btype"]
        bd = await BE.breakdown(t, bid)
        L = [BAR, f"<b>{T('head.' + bt)}</b>   {T('state.' + b['state'])}", "", f"🏆 <b>{esc(b['title'])}</b>", "",
             "📌 <b>Qoidalar:</b>", esc(b["rules"])]
        if b["reward"]:
            L += ["", f"🎁 <b>Mukofot:</b> {esc(b['reward'])}"]
        st = await BE.get_settings(t, bid)
        L += ["", f"👥 Ishtirokchilar: <b>{bd['participants']}</b>" + (f" / {st['participant_limit']}" if st["participant_limit"] else "")]
        if bt == "contest":
            d = await t.one("SELECT winner_id FROM contest_draws WHERE battle_id=?", bid)
            if d:
                w = await t.one("SELECT nick FROM participants WHERE battle_id=? AND user_id=?", bid, d["winner_id"])
                L.append(f"🥇 G'olib: <a href=\"tg://user?id={d['winner_id']}\">{esc(w['nick'] if w else str(d['winner_id']))}</a>")
        else:
            top = await top_rows(t, bid, 3)
            if top:
                L += ["", "🏆 <b>Hozirgi TOP:</b>"] + [f"{MEDALS[i]} {esc(r['label'])} — <b>{num(r['points'])}</b>" for i, r in enumerate(top)]
            L += ["", f"📊 Umumiy ball: <b>{num(bd['total'])}</b>  (💬 {bd['comment']['n']} · ❤️ {bd['reaction']['n']} · ⭐ {bd['star']['sum']})"]
        if b["state"] in ("ACTIVE", "PAUSED", "REGISTRATION"):
            L.append("⏰ Tugash: " + (f"{fmt_ts(b['end_ts'])}  (⏳ {left(b['end_ts'])})" if b["end_ts"] else "yaratuvchi hal qiladi"))
        elif b["finished_at"]:
            L.append(f"🏁 Yakunlangan: {fmt_ts(b['finished_at'])}")
        L.append(BAR)
        return "\n".join(L)[:4000]


    async def dash_text(t, b, fmt_ts) -> str:
        bid = b["id"]
        bd = await BE.breakdown(t, bid)
        top = await top_rows(t, bid, 1)
        st = await BE.get_settings(t, bid)
        L = [f"⚔️ <b>{esc(b['title'])}</b>   <i>{T('head.' + b['btype'])}</i>", "",
             f"👥 Participants: <b>{bd['participants']}</b>"]
        if b["btype"] != "contest":
            L += [f"🏆 Top: {who(top[0]) if top else '—'}", f"💎 Total score: <b>{num(bd['total'])}</b>",
                  f"💬 Comments: {num(bd['comment']['n'])} ({num(bd['comment']['sum'])})",
                  f"❤️ Reactions: {num(bd['reaction']['n'])} ({num(bd['reaction']['sum'])})",
                  f"⭐ Stars: {num(bd['star']['n'])} marta ({num(bd['star']['sum'])} ball)",
                  f"➕ Manual: {num(bd['manual']['sum'] + bd['bonus']['sum'])} · ➖ Penalty: {num(bd['penalty']['sum'])}"]
        rc = (await t.one("SELECT COUNT(*) c FROM rounds WHERE battle_id=?", bid))["c"]
        if b["btype"] != "contest":
            L.append(f"🔄 Raundlar: {rc}")
        if b["state"] in ("ACTIVE", "PAUSED"):
            L.append("⏰ Remaining: " + (f"<b>{left(b['end_ts'])}</b> ({fmt_ts(b['end_ts'])})" if b["end_ts"] else "qo'lda"))
        if b["reward"]:
            L.append(f"🎁 Mukofot: {esc(b['reward'])}")
        L.append(f"Status: {T('state.' + b['state'])}")
        return "\n".join(L)


    async def top10_text(t, b, uid: int | None = None) -> str:
        rows = await top_rows(t, b["id"], 10)
        L = [f"📊 <b>TOP 10</b> — {esc(b['title'])}", ""]
        for i, r in enumerate(rows):
            mark = MEDALS[i] if i < 3 else f"{i + 1}."
            L.append(f"{mark} {who(r)} — <b>{num(r['points'])}</b>")
        if not rows:
            L.append("Hali ishtirokchi yo'q")
        if uid:
            rk = await BE.rank_of(t, b["id"], uid)
            if rk:
                L += ["", f"📍 Sizning o'rningiz: <b>#{rk}</b>"]
        return "\n".join(L)


    def top10_alert(rows, rk=None) -> str:
        """Kanal tugmasi uchun (Telegram alert ≤ 200 belgi)."""
        parts = [f"{i + 1}. {r['label'][:11]} {r['points']}" for i, r in enumerate(rows)]
        s = "\n".join(parts)
        if rk:
            s += f"\n📍 #{rk}"
        while len(s) > 195 and parts:
            parts.pop()
            s = "\n".join(parts)
        return s or "Hali ishtirokchi yo'q"


    async def results_text(t, b, uid, fmt_ts) -> str:
        bid = b["id"]
        bd = await BE.breakdown(t, bid)
        rows = await top_rows(t, bid, 10)
        done = b["state"] in ("FINISHED", "SETTLED")
        L = ["🏆 <b>BATTLE YAKUNLANDI</b>" if done else "🏆 <b>BATTLE NATIJALARI</b>", f"<i>{esc(b['title'])}</i>", ""]
        if done and rows:
            L += ["🥇 <b>G'OLIB:</b>", f"👤 {who(rows[0])}", f"💎 <b>{num(rows[0]['points'])} BALL</b>", "🎉 Tabriklaymiz!", ""]
            if b["reward"]:
                L += [f"🎁 Mukofot: {esc(b['reward'])}", ""]
        for i, r in enumerate(rows):
            mark = MEDALS[i] if i < 3 else f"{i + 1}."
            L.append(f"{mark} {who(r)}\n     💎 {num(r['points'])} ball")
        L += ["", BAR, f"👥 Participantlar: {bd['participants']}", f"💬 Comments: {num(bd['comment']['n'])}",
              f"❤️ Reactions: {num(bd['reaction']['n'])}", f"⭐ Stars: {num(bd['star']['sum'])}"]
        end = b["finished_at"] or int(time.time())
        hrs = max(0, (end - (b["created"] or end)) // 3600)
        L.append(f"⏱ Battle davomiyligi: {hrs} soat")
        if uid:
            rk = await BE.rank_of(t, bid, uid)
            if rk:
                L += ["", f"📍 Sizning o'rningiz: <b>#{rk}</b>"]
        return "\n".join(L)[:4000]


    async def me_text(t, b, uid) -> str:
        bid = b["id"]
        p = await t.one("SELECT nick, points, joined_at FROM participants WHERE battle_id=? AND user_id=?", bid, uid)
        if not p:
            return "❌ Siz bu Battle'da qatnashmaysiz."
        rows = await t.query("SELECT source, SUM(amount) s, COUNT(*) n FROM battle_score_events WHERE battle_id=? AND user_id=? GROUP BY source", bid, uid)
        L = [f"👤 <b>{esc(p['nick'])}</b> — {esc(b['title'])}", f"📍 O'rin: <b>#{await BE.rank_of(t, bid, uid)}</b>",
             f"💎 Jami: <b>{num(p['points'])}</b>", "", "<b>Ball manbalari:</b>"]
        for r in rows:
            L.append(f"{T('src.' + r['source'])}: {num(r['s'])} ({r['n']})")
        if not rows:
            L.append("Hali ball yo'q")
        return "\n".join(L)


    def contest_result(b, draw, name, n) -> str:
        return (f"🎲 <b>RANDOM CONTEST YAKUNLANDI</b>\n<i>{esc(b['title'])}</i>\n\n🥇 <b>G'OLIB:</b> "
                f"<a href=\"tg://user?id={draw['winner_id']}\">{esc(name)}</a>\n🎉 Tabriklaymiz!\n"
                + (f"🎁 Mukofot: {esc(b['reward'])}\n" if b["reward"] else "")
                + f"\n👥 Ishtirokchilar: {n}\n🔐 Tekshirish: <code>sha256(seed:id'lar) mod {draw['candidate_count']}</code>\n"
                  f"Seed: <code>{draw['seed']}</code>\nRo'yxat hash: <code>{draw['candidates_hash'][:16]}…</code>")


    async def audit_text(t, bid: int, fmt_ts) -> str:
        rows = await t.query("SELECT * FROM battle_events WHERE battle_id=? ORDER BY id DESC LIMIT 15", bid)
        L = ["📜 <b>Audit (oxirgi 15)</b>", ""]
        for r in rows:
            L.append(f"{fmt_ts(r['created_at'])} · {r['action']}" + (f" · u{r['user_id']}" if r["user_id"] else "")
                     + (f" · {r['amount']:+d}" if r["amount"] is not None else ""))
        if not rows:
            L.append("Hozircha yozuv yo'q")
        return "\n".join(L)[:3900]
    return SimpleNamespace(**{k: v for k, v in locals().items() if not k.startswith('_')})


def _make_handlers():
    """battle/handlers.py"""
    V, T = BV, BT.T
    import json
    import re
    import time

    from aiogram import F, Router
    from aiogram.filters import Command
    from aiogram.types import CallbackQuery, InputMediaPhoto, Message, MessageReactionUpdated


    DUR = ((1, "1 soat"), (3, "3 soat"), (6, "6 soat"), (12, "12 soat"), (24, "24 soat"), (48, "48 soat"), (168, "7 kun"))
    FIELDS = {  # kalit: (yorliq, min, max)
        "reaction_score": ("👍 Reaction balli", 0, 1000), "comment_score": ("💬 Izoh balli", 0, 1000),
        "star_score": ("⭐ Har 1 Star = ball", 0, 1000), "participant_limit": ("👥 Ishtirokchi limiti (0=cheksiz)", 0, 100000),
        "comments_per_round": ("🛡 Raundda maks. izoh", 1, 20),
    }


    def build(g):
        r = Router()
        r.message.filter(F.chat.type == "private")
        K, B, HOME, screen, cancel_kb = g["K"], g["B"], g["HOME"], g["screen"], g["cancel_kb"]
        q, one, run, tx, esc, fmt_ts = g["q"], g["one"], g["run"], g["tx"], g["esc"], g["fmt_ts"]
        can, manage, RO = g["can"], g["manage"], g["RO"]
        BSet, CNew, NftE, FB, RoleAdd = g["BSet"], g["CNew"], g["NftE"], g["FB"], g["RoleAdd"]

        async def battle_for(c, bid, perm="view"):
            b = await one("SELECT * FROM battles WHERE id=?", bid)
            if not b:
                await c.answer(T("no_battle"), show_alert=True)
                return None
            if not await can(b, c.from_user.id, perm):
                await c.answer(T("denied"), show_alert=True)
                return None
            return b

        # ---------------------------------------------------------------- markaz
        @r.callback_query(F.data == "bx:home")
        @r.message(Command("battle"))
        async def hub(ev, state):
            await state.clear()
            rows = [[B("➕ Battle yaratish", "m:new"), B("🎲 Random Contest", "bx:cn")],
                    [B("🖼 NFT Battle", "bx:nb"), B("🔥 Faol Battlelar", "m:battles")],
                    [B("🏆 Mening Battlelarim", "m:my"), B("📜 Tarix", "bx:hist")],
                    [B("📊 Reyting", "bx:rank:a")], [HOME]]
            await screen(ev, "battle", "⚔️ <b>BATTLE MARKAZI</b>\n\nHammasi bir joyda 👇", K(*rows))

        @r.callback_query(F.data == "bx:nb")
        async def nft_start(c: CallbackQuery, state):
            await g["new_battle"](c, state)
            if await state.get_state() == "NewB:ch":
                await state.update_data(btype="nft")

        # ---------------------------------------------------------------- umumiy reyting
        PERIODS = {"a": ("🏆 All Time", 0), "d": ("📅 Kunlik", 86400), "w": ("📅 Haftalik", 7 * 86400), "m": ("📅 Oylik", 30 * 86400)}

        @r.callback_query(F.data.regexp(r"^bx:rank:[adwm]$"))
        async def rank(c: CallbackQuery):
            key = c.data.split(":")[2]
            label, span = PERIODS[key]
            if span:
                since = int(time.time()) - span
                src = ("SELECT user_id, SUM(amount) s FROM battle_score_events WHERE created_at>=? AND source<>'legacy' GROUP BY user_id",
                       (since,))
            else:
                src = ("SELECT user_id, SUM(points) s FROM participants GROUP BY user_id", ())
            rows = await q(f"SELECT x.user_id, x.s, u.name, u.username FROM ({src[0]}) x LEFT JOIN users u ON u.id=x.user_id "
                           "WHERE x.s>0 ORDER BY x.s DESC LIMIT 10", *src[1])
            L = [f"<b>{label}</b>", ""]
            for i, x in enumerate(rows):
                mark = V.MEDALS[i] if i < 3 else f"{i + 1}."
                L.append(f"{mark} {esc(x['name'] or str(x['user_id']))}" + (f" (@{x['username']})" if x["username"] else "")
                         + f" — <b>{V.num(x['s'])}</b>")
            if not rows:
                L.append("Hali natija yo'q")
            me = await one(f"SELECT s FROM ({src[0]}) WHERE user_id=?", *src[1], c.from_user.id)
            if me and me["s"] > 0:
                pos = (await one(f"SELECT COUNT(*) c FROM ({src[0]}) WHERE s>?", *src[1], me["s"]))["c"] + 1
                L += ["", f"📍 Sizning o'rningiz: <b>#{pos}</b> ({V.num(me['s'])})"]
            kb = [[B(("• " if k == key else "") + v[0], f"bx:rank:{k}") for k, v in PERIODS.items()], [B("⬅️ Orqaga", "bx:home"), HOME]]
            await screen(c, "top", "\n".join(L), K(*kb))

        # ---------------------------------------------------------------- tarix
        @r.callback_query(F.data == "bx:hist")
        async def history(c: CallbackQuery):
            uid = c.from_user.id
            part = await q("SELECT b.id,b.title,b.state,b.created,p.points,"
                           "(SELECT COUNT(*)+1 FROM participants p2 WHERE p2.battle_id=p.battle_id AND p2.points>p.points) rk "
                           "FROM participants p JOIN battles b ON b.id=p.battle_id WHERE p.user_id=? ORDER BY b.id DESC LIMIT 10", uid)
            mine = await q("SELECT id,title,state,created FROM battles WHERE owner_id=? ORDER BY id DESC LIMIT 8", uid)
            L = ["📜 <b>Battle tarixi</b>", "", "<b>Qatnashganlarim:</b>"]
            for x in part:
                win = "🥇 " if x["state"] in ("FINISHED", "SETTLED") and x["rk"] == 1 else ""
                L.append(f"{win}{esc(x['title'][:26])} · {T('state.' + x['state'])} · {fmt_ts(x['created'])[:10]} · 💎 {x['points']} · #{x['rk']}")
            if not part:
                L.append("—")
            L += ["", "<b>Yaratganlarim:</b>"] + [f"{esc(x['title'][:30])} · {T('state.' + x['state'])} · {fmt_ts(x['created'])[:10]}" for x in mine]
            if not mine:
                L.append("—")
            await screen(c, "profile", "\n".join(L)[:3900], K([B("⬅️ Orqaga", "bx:home"), HOME]))

        # ---------------------------------------------------------------- natijalar / top10 / mening natijam
        async def _view(c, bid, kind):
            b = await one("SELECT * FROM battles WHERE id=?", bid)
            if not b:
                return await c.answer(T("no_battle"), show_alert=True)
            uid = c.from_user.id
            if kind == "res":
                text = await V.results_text(RO, b, uid, fmt_ts)
            elif kind == "top":
                text = await V.top10_text(RO, b, uid)
            else:
                text = await V.me_text(RO, b, uid)
            kb = [[B("🏆 Natijalar", f"bx:res:{bid}"), B("📊 TOP 10", f"bx:top:{bid}")], [B("👤 Mening natijam", f"bx:me:{bid}")]]
            if b["btype"] == "nft":
                kb.append([B("🖼 NFT'larni ko'rish", f"bx:nv:{bid}")])
            if b["state"] == "ACTIVE":
                kb.append([B("⭐ Qo'llab-quvvatlash", f"bx:sp:{bid}"), B("💬 Fikr bildirish", f"bx:fb:{bid}")])
            kb.append([B("⬅️ Orqaga", f"v:{bid}"), HOME])
            await screen(c, "top", text, K(*kb))

        @r.callback_query(F.data.regexp(r"^bx:(res|top|me):\d+$"))
        async def view_cb(c: CallbackQuery):
            _, kind, bid = c.data.split(":")
            await _view(c, int(bid), kind)

        @r.callback_query(F.data.regexp(r"^bx:t:\d+$"))
        async def top_alert(c: CallbackQuery):
            """Kanal postidagi «TOP 10» tugmasi (ixcham alert)."""
            bid = int(c.data.split(":")[2])
            rows = await V.top_rows(RO, bid, 10)
            rk = await BE.rank_of(RO, bid, c.from_user.id)
            await c.answer(V.top10_alert(rows, rk), show_alert=True)

        @r.callback_query(F.data.regexp(r"^bx:nv:\d+$"))
        async def nft_view(c: CallbackQuery):
            bid = int(c.data.split(":")[2])
            rows = await q("SELECT n.*, p.points FROM nft_entries n JOIN participants p ON p.battle_id=n.battle_id AND p.user_id=n.user_id "
                           "WHERE n.battle_id=? AND n.image_file_id<>'' ORDER BY p.points DESC LIMIT 5", bid)
            if not rows:
                return await c.answer("Hali NFT yuborilmagan", show_alert=True)
            media = [InputMediaPhoto(media=x["image_file_id"], caption=f"{i + 1}. {x['name']} · {x['collection']} {x['rarity']} · 💎 {x['points']}"[:200])
                     for i, x in enumerate(rows)]
            await c.answer()
            await c.bot.send_media_group(c.from_user.id, media)

        # ---------------------------------------------------------------- dashboard / audit
        @r.callback_query(F.data.regexp(r"^bx:dash:\d+$"))
        async def dash(c: CallbackQuery, state):
            bid = int(c.data.split(":")[2])
            if await battle_for(c, bid, "view"):
                await manage(c, state, bid)

        @r.callback_query(F.data.regexp(r"^bx:au:\d+$"))
        async def audit_view(c: CallbackQuery):
            bid = int(c.data.split(":")[2])
            if await battle_for(c, bid, "view"):
                await screen(c, "owner", await V.audit_text(RO, bid, fmt_ts), K([B("⬅️ Orqaga", f"b:{bid}")]))

        # ---------------------------------------------------------------- pauza / bekor qilish
        @r.callback_query(F.data.regexp(r"^bx:pz:\d+$"))
        async def pause(c: CallbackQuery, state):
            bid = int(c.data.split(":")[2])
            b = await battle_for(c, bid, "state")
            if not b:
                return
            try:
                async with tx() as t:
                    await BE.transition(t, bid, "PAUSED" if b["state"] == "ACTIVE" else "ACTIVE", c.from_user.id)
            except BE.BattleError as e:
                return await c.answer(str(e), show_alert=True)
            await g["schedule_post_update"](c.bot, bid, 0.5)
            await manage(c, state, bid, prefix="✅ Holat yangilandi.\n\n")

        @r.callback_query(F.data.regexp(r"^bx:cx:\d+$"))
        async def cancel_ask(c: CallbackQuery):
            bid = int(c.data.split(":")[2])
            b = await battle_for(c, bid, "cancel")
            if b:
                await screen(c, "battle", f"❌ <b>{esc(b['title'])}</b> bekor qilinsinmi?\nBall berilmaydi, g'olib e'lon qilinmaydi.",
                             K([B("✅ Ha, bekor qilish", f"bx:cxx:{bid}")], [B("⬅️ Yo'q", f"b:{bid}")]))

        @r.callback_query(F.data.regexp(r"^bx:cxx:\d+$"))
        async def cancel_do(c: CallbackQuery, state):
            bid = int(c.data.split(":")[2])
            b = await battle_for(c, bid, "cancel")
            if not b:
                return
            try:
                async with tx() as t:
                    await BE.transition(t, bid, "CANCELLED", c.from_user.id)
            except BE.BattleError as e:
                return await c.answer(str(e), show_alert=True)
            await g["schedule_post_update"](c.bot, bid, 0.5)
            await manage(c, state, bid, prefix="❌ Battle bekor qilindi.\n\n")

        # ---------------------------------------------------------------- sozlamalar
        async def settings_screen(c, bid, prefix=""):
            b = await one("SELECT * FROM battles WHERE id=?", bid)
            async with tx() as t:
                s = await BE.get_settings(t, bid)
            w = ", ".join(f"{k}={v}" for k, v in s["reaction_weights"].items()) or "barcha emoji = Reaction balli"
            text = (f"{prefix}⚙️ <b>Sozlamalar</b> — {esc(b['title'])}\n\n⏱ Tugash: "
                    f"{fmt_ts(b['end_ts']) if b['end_ts'] else 'qo‘lda'}\n"
                    + "\n".join(f"{lab}: <b>{s[k]}</b>" for k, (lab, _, _) in FIELDS.items())
                    + f"\n👍 Reaction vaznlari: <code>{esc(w)}</code>\n🎁 Mukofot: {esc(b['reward']) or '—'}\n"
                    f"➖ Manfiy ball: {'ruxsat' if s['allow_negative'] else 'yopiq'}\n\n"
                    "Har bir sozlama faqat shu Battle'ga tegishli.")
            rows = [[B(lab.split(" (")[0], f"bx:sf:{bid}:{k}")] for k, (lab, _, _) in FIELDS.items()]
            rows += [[B("👍 Reaction vaznlari", f"bx:sw:{bid}"), B("🎁 Mukofot", f"bx:sr:{bid}")],
                     [B("➖ Manfiy ball: " + ("yopish" if s["allow_negative"] else "ruxsat"), f"bx:sn:{bid}")],
                     [B("⏱ Muddat", f"bx:sdm:{bid}")], [B("⬅️ Orqaga", f"b:{bid}")]]
            await screen(c, "owner", text, K(*rows))

        async def editable(c, bid):
            b = await battle_for(c, bid, "settings")
            if b and b["state"] not in ("ACTIVE", "PAUSED", "REGISTRATION", "DRAFT"):
                await c.answer(T("ended"), show_alert=True)
                return None
            return b

        @r.callback_query(F.data.regexp(r"^bx:set:\d+$"))
        async def settings(c: CallbackQuery, state):
            await state.clear()
            bid = int(c.data.split(":")[2])
            if await battle_for(c, bid, "settings"):
                await settings_screen(c, bid)

        @r.callback_query(F.data.regexp(r"^bx:s[fwr]:\d+(:\w+)?$"))
        async def setting_pick(c: CallbackQuery, state):
            parts = c.data.split(":")
            kind, bid = parts[1][1], int(parts[2])
            if not await editable(c, bid):
                return
            key = {"f": parts[3] if len(parts) > 3 else "", "w": "reaction_weights", "r": "reward"}[kind]
            if kind == "f" and key not in FIELDS:
                return await c.answer()
            await state.set_state(BSet.val)
            await state.update_data(bid=bid, key=key)
            hint = {"reaction_weights": "Format: <code>👍=1, ❤️=2, 🔥=3</code> (o'chirish: <code>-</code>)",
                    "reward": "Mukofot matnini yuboring (o'chirish: <code>-</code>)"}.get(
                key, f"Butun son yuboring ({FIELDS[key][1]}–{FIELDS[key][2]})" if key in FIELDS else "")
            await screen(c, "owner", f"✏️ <b>{FIELDS.get(key, (key,))[0]}</b>\n\n{hint}", cancel_kb())

        @r.message(BSet.val, F.text)
        async def setting_val(m: Message, state):
            d = await state.get_data()
            bid, key, v = d["bid"], d["key"], m.text.strip()
            b = await one("SELECT * FROM battles WHERE id=?", bid)
            if not b or not await can(b, m.from_user.id, "settings") or b["state"] not in ("ACTIVE", "PAUSED", "REGISTRATION", "DRAFT"):
                await state.clear()
                return await m.answer(T("denied"))
            if key == "reward":
                await run("UPDATE battles SET reward=? WHERE id=?", "" if v == "-" else v[:100], bid)
            elif key == "reaction_weights":
                w = {}
                if v != "-":
                    for part in re.split(r"[,;\n]+", v):
                        mm = re.match(r"^\s*(\S+?)\s*=\s*(-?\d{1,4})\s*$", part)
                        if not mm:
                            return await m.answer("⚠️ Format noto'g'ri. Masalan: <code>👍=1, ❤️=2, 🔥=3</code>", reply_markup=cancel_kb())
                        w[mm.group(1)] = int(mm.group(2))
                await run("UPDATE battle_settings SET reaction_weights=? WHERE battle_id=?", json.dumps(w, ensure_ascii=False), bid)
            else:
                lab, lo, hi = FIELDS[key]
                if not v.isdigit() or not lo <= int(v) <= hi:
                    return await m.answer(f"⚠️ {lo} dan {hi} gacha butun son yuboring.", reply_markup=cancel_kb())
                await run(f"UPDATE battle_settings SET {key}=? WHERE battle_id=?", int(v), bid)    # key — FIELDS dan, foydalanuvchi kiritmaydi
            await state.clear()
            await BE_log(bid, m.from_user.id, "battle.settings_changed", key)
            await m.answer("✅ Saqlandi", reply_markup=K([B("⚙️ Sozlamalar", f"bx:set:{bid}")]))
            await g["schedule_post_update"](m.bot, bid, 1)

        async def BE_log(bid, uid, action, meta=""):
            async with tx() as t:
                await BE.log_event(t, action, battle_id=bid, user_id=uid, meta=meta)

        @r.callback_query(F.data.regexp(r"^bx:sn:\d+$"))
        async def toggle_neg(c: CallbackQuery):
            bid = int(c.data.split(":")[2])
            if await editable(c, bid):
                await run("UPDATE battle_settings SET allow_negative=1-allow_negative WHERE battle_id=?", bid)
                await settings_screen(c, bid)

        @r.callback_query(F.data.regexp(r"^bx:sdm:\d+$"))
        async def dur_menu(c: CallbackQuery):
            bid = int(c.data.split(":")[2])
            if not await editable(c, bid):
                return
            rows = [[B(n, f"bx:sd:{bid}:{h}") for h, n in DUR[i:i + 3]] for i in range(0, len(DUR), 3)]
            rows.append([B("✋ Qo'lda yakunlayman", f"bx:sd:{bid}:0")])
            rows.append([B("⬅️ Orqaga", f"bx:set:{bid}")])
            await screen(c, "owner", "⏱ Hozirdan boshlab qancha vaqt qolsin?", K(*rows))

        @r.callback_query(F.data.regexp(r"^bx:sd:\d+:\d+$"))
        async def dur_set(c: CallbackQuery):
            _, _, bid, h = c.data.split(":")
            if await editable(c, int(bid)):
                await run("UPDATE battles SET end_ts=? WHERE id=?", int(time.time()) + int(h) * 3600 if int(h) else 0, int(bid))
                await g["schedule_post_update"](c.bot, int(bid), 1)
                await settings_screen(c, int(bid), "✅ Muddat yangilandi.\n\n")

        # ---------------------------------------------------------------- Random Contest yaratish
        @r.callback_query(F.data == "bx:cn")
        async def contest_new(c: CallbackQuery, state):
            await state.clear()
            chs = await q("SELECT * FROM channels WHERE owner_id=?", c.from_user.id)
            if not chs:
                return await g["link_start"](c, state)
            rows = [[B(f"📢 {x['title'][:30]}", f"bx:cc:{x['id']}")] for x in chs] + [[B("➕ Kanal ulash", "ch:link")], [HOME]]
            await state.set_state(CNew.ch)
            await screen(c, "battle", "🎲 <b>Random Contest</b>\n\n1/6 — Qaysi kanalda?", K(*rows))

        @r.callback_query(CNew.ch, F.data.startswith("bx:cc:"))
        async def contest_ch(c: CallbackQuery, state):
            ch = await one("SELECT * FROM channels WHERE id=? AND owner_id=?", int(c.data.split(":")[2]), c.from_user.id)
            if not ch:
                return await c.answer(T("denied"), show_alert=True)
            await state.update_data(ch=ch["id"])
            await state.set_state(CNew.title)
            await screen(c, "battle", "🎲 <b>Random Contest</b>\n\n2/6 — <b>Nomini</b> yuboring:", cancel_kb())

        @r.message(CNew.title, F.text)
        async def contest_title(m: Message, state):
            if not 2 <= len(m.text.strip()) <= 80:
                return await m.answer("⚠️ Nom 2–80 belgi bo'lsin.", reply_markup=cancel_kb())
            await state.update_data(title=m.text.strip())
            await state.set_state(CNew.desc)
            await m.answer("3/6 — <b>Tavsif</b> (shartlar) yuboring:", reply_markup=cancel_kb())

        @r.message(CNew.desc, F.text)
        async def contest_desc(m: Message, state):
            if len(m.text) > 1500:
                return await m.answer("⚠️ Tavsif 1500 belgidan oshmasin.", reply_markup=cancel_kb())
            await state.update_data(desc=m.text.strip())
            await state.set_state(CNew.dur)
            rows = [[B(n, f"bx:cd:{h}") for h, n in DUR[i:i + 3]] for i in range(0, len(DUR), 3)]
            rows += [[B("✋ Qo'lda yopaman", "bx:cd:0")], [B("✖️ Bekor qilish", "x:cancel")]]
            await m.answer("4/6 — Contest qancha davom etsin?", reply_markup=K(*rows))

        @r.callback_query(CNew.dur, F.data.regexp(r"^bx:cd:\d+$"))
        async def contest_dur(c: CallbackQuery, state):
            await state.update_data(hours=int(c.data.split(":")[2]))
            await state.set_state(CNew.limit)
            rows = [[B(str(n) if n else "♾ Cheksiz", f"bx:cl:{n}") for n in (0, 50, 100, 500)], [B("✖️ Bekor qilish", "x:cancel")]]
            await screen(c, "battle", "5/6 — Ishtirokchilar limiti?", K(*rows))

        @r.callback_query(CNew.limit, F.data.regexp(r"^bx:cl:\d+$"))
        async def contest_limit(c: CallbackQuery, state):
            await state.update_data(limit=int(c.data.split(":")[2]))
            await state.set_state(CNew.reward)
            await screen(c, "battle", "6/6 — <b>Mukofot</b> nima? (yo'q bo'lsa <code>-</code>)", cancel_kb())

        @r.message(CNew.reward, F.text)
        async def contest_reward(m: Message, state):
            rw = "" if m.text.strip() == "-" else m.text.strip()[:100]
            await state.update_data(reward=rw)
            d = await state.get_data()
            ends = fmt_ts(int(time.time()) + d["hours"] * 3600) if d["hours"] else "qo'lda"
            await m.answer(f"✅ <b>Tasdiqlang</b>\n\n🎲 {esc(d['title'])}\n📝 {esc(d['desc'])}\n⏰ {ends}\n"
                           f"👥 Limit: {d['limit'] or 'cheksiz'}\n🎁 {esc(rw) or '—'}",
                           reply_markup=K([B("🚀 E'lon qilish", "bx:cgo")], [B("✖️ Bekor qilish", "x:cancel")]))

        @r.callback_query(CNew.reward, F.data == "bx:cgo")
        async def contest_go(c: CallbackQuery, state):
            d = await state.get_data()
            uid = c.from_user.id
            ch = await one("SELECT * FROM channels WHERE id=? AND owner_id=?", d.get("ch"), uid)
            if not ch:
                return await c.answer(T("denied"), show_alert=True)
            ts = int(time.time())
            async with tx() as t:
                bid, _ = await t.execute("INSERT INTO battles(owner_id,channel_id,title,rules,points,end_ts,created,btype,reward,state,status) "
                                         "VALUES(?,?,?,?,0,?,?,'contest',?,'ACTIVE','active')", uid, ch["id"], d["title"], d["desc"],
                                         ts + d["hours"] * 3600 if d["hours"] else 0, ts, d["reward"])
                await t.execute("INSERT OR IGNORE INTO battle_settings(battle_id,participant_limit) VALUES(?,?)", bid, d["limit"])
                await BE.log_event(t, "contest.created", battle_id=bid, user_id=uid)
            await state.clear()
            try:
                b = await one("SELECT * FROM battles WHERE id=?", bid)
                msg = await c.bot.send_message(ch["id"], await g["post_text"](b), reply_markup=await g["join_markup"](c.bot, bid))
            except Exception:
                async with tx() as t:
                    await BE.transition(t, bid, "CANCELLED", uid)
                return await screen(c, "battle", T("post_fail"), K([HOME]))
            await run("UPDATE battles SET post_id=? WHERE id=?", msg.message_id, bid)
            await manage(c, state, bid, prefix="🚀 Contest e'lon qilindi!\n\n")

        # ---------------------------------------------------------------- NFT entry
        @r.message(NftE.photo, F.photo)
        async def nft_photo(m: Message, state):
            d = await state.get_data()
            bid = d.get("bid")
            parts = [x.strip() for x in (m.caption or "").split("|")]
            name = parts[0][:60] if parts and parts[0] else ""
            if not name:
                return await m.answer("⚠️ Izohda NFT nomini yozing: <code>Nomi | Kolleksiya | Noyoblik</code>", reply_markup=cancel_kb())
            async with tx() as t:
                b = await t.one("SELECT state, btype FROM battles WHERE id=?", bid)
                if not b or b["btype"] != "nft" or b["state"] not in ("REGISTRATION", "ACTIVE"):
                    await state.clear()
                    return await m.answer(T("ended"))
                if not await t.one("SELECT 1 FROM participants WHERE battle_id=? AND user_id=?", bid, m.from_user.id):
                    await state.clear()
                    return await m.answer("❌ Avval Battle'ga qo'shiling.")
                await t.execute("INSERT INTO nft_entries(battle_id,user_id,name,collection,rarity,image_file_id,meta,created_at) VALUES(?,?,?,?,?,?,?,?) "
                                "ON CONFLICT(battle_id,user_id) DO UPDATE SET name=excluded.name, collection=excluded.collection, "
                                "rarity=excluded.rarity, image_file_id=excluded.image_file_id",
                                bid, m.from_user.id, name, parts[1][:60] if len(parts) > 1 else "", parts[2][:30] if len(parts) > 2 else "",
                                m.photo[-1].file_id, json.dumps({"src": "upload"}), int(time.time()))
                await BE.log_event(t, "battle.nft_submitted", battle_id=bid, user_id=m.from_user.id, meta=name)
            await state.clear()
            await m.answer(f"✅ NFT <b>{esc(name)}</b> qabul qilindi.", reply_markup=K([B("🏆 Natijalar", f"bx:res:{bid}")], [HOME]))
            await g["schedule_post_update"](m.bot, bid)

        @r.message(NftE.photo)
        async def nft_wrong(m: Message):
            await m.answer("🖼 NFT <b>rasmini</b> yuboring (izohda: <code>Nomi | Kolleksiya | Noyoblik</code>).", reply_markup=cancel_kb())

        # ---------------------------------------------------------------- Stars bilan qo'llab-quvvatlash
        @r.callback_query(F.data.regexp(r"^bx:sp:\d+$"))
        async def sup_list(c: CallbackQuery):
            bid = int(c.data.split(":")[2])
            b = await one("SELECT * FROM battles WHERE id=?", bid)
            if not b or b["state"] != "ACTIVE" or b["btype"] == "contest":
                return await c.answer(T("ended"), show_alert=True)
            rows = [x for x in await V.top_rows(RO, bid, 10) if x["user_id"] != c.from_user.id]
            if not rows:
                return await c.answer("Hozircha qo'llab-quvvatlash uchun ishtirokchi yo'q", show_alert=True)
            kb = [[B(f"{x['label'][:20]} · {x['points']}", f"bx:su:{bid}:{x['user_id']}")] for x in rows] + [[B("⬅️ Orqaga", f"bx:top:{bid}")]]
            await screen(c, "top", "⭐ <b>Kimni qo'llab-quvvatlaysiz?</b>\nTelegram Stars to'lovi tasdiqlangach unga ball qo'shiladi.", K(*kb))

        @r.callback_query(F.data.regexp(r"^bx:su:\d+:\d+$"))
        async def sup_amount(c: CallbackQuery):
            _, _, bid, tuid = c.data.split(":")
            kb = [[B(f"⭐ {a}", f"bx:sa:{bid}:{tuid}:{a}") for a in (10, 25, 50)], [B(f"⭐ {a}", f"bx:sa:{bid}:{tuid}:{a}") for a in (100, 250)],
                  [B("⬅️ Orqaga", f"bx:sp:{bid}")]]
            await screen(c, "wallet", "Nechta Stars?", K(*kb))

        @r.callback_query(F.data.regexp(r"^bx:sa:\d+:\d+:\d+$"))
        async def sup_go(c: CallbackQuery):
            _, _, bid, tuid, amt = c.data.split(":")
            if int(tuid) == c.from_user.id:
                return await c.answer("O'zingizni qo'llab-quvvatlay olmaysiz", show_alert=True)
            o = await g["create_order"](c.from_user.id, "battle_star", stars=int(amt), meta=f"{bid}|{tuid}")
            if not o:
                return await c.answer("❌ Hozir qo'llab-quvvatlab bo'lmaydi (Battle faol emas yoki ishtirokchi topilmadi).", show_alert=True)
            await g["checkout"](c, o)

        # ---------------------------------------------------------------- fikr bildirish
        @r.callback_query(F.data.regexp(r"^bx:fb:\d+$"))
        async def fb_start_cb(c: CallbackQuery, state):
            await fb_begin(c, state, int(c.data.split(":")[2]))

        async def fb_begin(ev, state, bid):
            b = await one("SELECT title,state FROM battles WHERE id=?", bid)
            if not b or b["state"] not in ("REGISTRATION", "ACTIVE", "PAUSED"):
                return await screen(ev, "battle", T("ended"), K([HOME]))
            await state.set_state(FB.text)
            await state.update_data(bid=bid)
            await screen(ev, "battle", f"💬 <b>Fikr bildirish</b> — {esc(b['title'])}\n\nFikringizni yozing (3–500 belgi):", cancel_kb())
        g["fb_begin"] = fb_begin

        @r.message(FB.text, F.text)
        async def fb_text(m: Message, state):
            d = await state.get_data()
            bid, text, uid = d["bid"], m.text.strip(), m.from_user.id
            if not 3 <= len(text) <= 500:
                return await m.answer("⚠️ Fikr 3–500 belgi bo'lsin.", reply_markup=cancel_kb())
            ts = int(time.time())
            async with tx() as t:
                b = await t.one("SELECT owner_id,title,state FROM battles WHERE id=?", bid)
                if not b or b["state"] not in ("REGISTRATION", "ACTIVE", "PAUSED"):
                    await state.clear()
                    return await m.answer(T("ended"))
                last = await t.one("SELECT MAX(created_at) ts, COUNT(*) n FROM battle_feedback WHERE battle_id=? AND user_id=?", bid, uid)
                if last["ts"] and ts - last["ts"] < 60:
                    return await m.answer(f"⏳ Iltimos, {60 - (ts - last['ts'])} soniyadan so'ng yozing (flood himoyasi).")
                if last["n"] >= 5:
                    await state.clear()
                    return await m.answer("⚠️ Bu Battle uchun fikrlar limiti (5 ta) tugagan.")
                if await t.one("SELECT 1 FROM battle_feedback WHERE battle_id=? AND user_id=? AND text=?", bid, uid, text):
                    return await m.answer("⚠️ Bu fikr allaqachon yuborilgan.")
                fid, _ = await t.execute("INSERT INTO battle_feedback(battle_id,user_id,text,created_at) VALUES(?,?,?,?)", bid, uid, text, ts)
                await BE.log_event(t, "battle.feedback", battle_id=bid, user_id=uid)
            await state.clear()
            await m.answer("✅ Fikringiz qabul qilindi. Rahmat!", reply_markup=K([HOME]))
            for rid in {b["owner_id"], *await BR.admin_ids(RO, bid)}:
                try:
                    await m.bot.send_message(rid, f"💬 <b>Yangi fikr</b> — {esc(b['title'])}\n{await g['who'](uid)}:\n\n{esc(text)}",
                                             reply_markup=K([B("🙈 Yashirish", f"bx:fh:{fid}")]))
                except Exception:
                    pass

        @r.callback_query(F.data.regexp(r"^bx:fh:\d+$"))
        async def fb_hide(c: CallbackQuery):
            f = await one("SELECT f.id, f.battle_id FROM battle_feedback f WHERE f.id=?", int(c.data.split(":")[2]))
            b = await one("SELECT * FROM battles WHERE id=?", f["battle_id"]) if f else None
            if not f or not await can(b, c.from_user.id, "feedback"):
                return await c.answer(T("denied"), show_alert=True)
            async with tx() as t:
                await BE.log_event(t, "battle.feedback_hidden", battle_id=b["id"], user_id=c.from_user.id, meta=str(f["id"]))
            await run("UPDATE battle_feedback SET status='hidden' WHERE id=?", f["id"])
            await c.answer("🙈 Yashirildi")
            try:
                await c.message.edit_text((c.message.text or "") + "\n\n🙈 yashirilgan")
            except Exception:
                pass

        # ---------------------------------------------------------------- rollar (faqat Battle egasi)
        async def roles_screen(c, bid, prefix=""):
            b = await one("SELECT title FROM battles WHERE id=?", bid)
            rows_a = await BR.admins(RO, bid)
            L = [f"{prefix}👥 <b>Adminlar va rollar</b> — {esc(b['title'])}", "",
                 "🛡 <b>Admin</b>: ball, raund, pauza, yakunlash, sozlamalar, fikr moderatsiyasi",
                 "🧹 <b>Moderator</b>: statistikani ko'rish va fikrlarni yashirish", ""]
            for x in rows_a:
                L.append(f"{BR.LABEL[x['role']]} — {esc(x['name'] or str(x['user_id']))}" + (f" (@{x['username']})" if x["username"] else ""))
            if not rows_a:
                L.append("Hozircha adminlar yo'q")
            kb = [[B(f"🗑 {x['role']}: {(x['name'] or str(x['user_id']))[:18]}", f"bx:rd:{bid}:{x['user_id']}")] for x in rows_a]
            kb.append([B("➕ Admin qo'shish", f"bx:ra:{bid}:manager"), B("➕ Moderator qo'shish", f"bx:ra:{bid}:moderator")])
            kb.append([B("⬅️ Orqaga", f"b:{bid}")])
            await screen(c, "owner", "\n".join(L), K(*kb))

        @r.callback_query(F.data.regexp(r"^bx:rl:\d+$"))
        async def roles_menu(c: CallbackQuery, state):
            await state.clear()
            bid = int(c.data.split(":")[2])
            if await battle_for(c, bid, "roles"):
                await roles_screen(c, bid)

        @r.callback_query(F.data.regexp(r"^bx:ra:\d+:(manager|moderator)$"))
        async def role_add_ask(c: CallbackQuery, state):
            _, _, bid, role = c.data.split(":")
            if not await battle_for(c, int(bid), "roles"):
                return
            await state.set_state(RoleAdd.who)
            await state.update_data(bid=int(bid), role=role)
            await screen(c, "owner", f"➕ <b>{BR.LABEL[role]}</b> qo'shish\n\nFoydalanuvchining <b>Telegram ID</b> sini, <b>@username</b> ini yuboring "
                         "yoki uning xabarini forward qiling.\nU botni kamida bir marta ishga tushirgan bo'lishi kerak.", cancel_kb())

        @r.message(RoleAdd.who)
        async def role_add_do(m: Message, state):
            d = await state.get_data()
            bid, role = d["bid"], d["role"]
            b = await one("SELECT * FROM battles WHERE id=?", bid)
            if not b or not await can(b, m.from_user.id, "roles"):
                await state.clear()
                return await m.answer(T("denied"))
            uid = None
            fo = m.forward_origin
            if fo is not None and getattr(fo, "type", "") == "user":
                uid = fo.sender_user.id
            elif m.text:
                v = m.text.strip()
                if v.lstrip("-").isdigit():
                    uid = int(v)
                elif v.startswith("@"):
                    row = await one("SELECT id FROM users WHERE LOWER(username)=LOWER(?)", v[1:])
                    uid = row["id"] if row else None
            if uid is None or not await one("SELECT 1 FROM users WHERE id=?", uid):
                return await m.answer("❌ Foydalanuvchi topilmadi. U botni ishga tushirganiga ishonch hosil qiling va ID/@username ni qayta yuboring.",
                                      reply_markup=cancel_kb())
            try:
                async with tx() as t:
                    res = await BR.grant(t, bid, uid, role, m.from_user.id)
            except BE.BattleError as e:
                return await m.answer(str(e), reply_markup=cancel_kb())
            await state.clear()
            if res != "same":
                try:
                    await m.bot.send_message(uid, f"{BR.LABEL[role]} roli berildi: «{esc(b['title'])}» Battle'i. Boshqarish: 🏆 Mening Battlelarim.")
                except Exception:
                    pass
            await m.answer("✅ Rol berildi" if res != "same" else "ℹ️ Bu foydalanuvchi allaqachon shu rolda", reply_markup=K([B("👥 Rollar", f"bx:rl:{bid}")]))

        @r.callback_query(F.data.regexp(r"^bx:rd:\d+:\d+$"))
        async def role_remove(c: CallbackQuery):
            _, _, bid, uid = c.data.split(":")
            if not await battle_for(c, int(bid), "roles"):
                return
            async with tx() as t:
                ok = await BR.revoke(t, int(bid), int(uid), c.from_user.id)
            await roles_screen(c, int(bid), "✅ Rol olib tashlandi.\n\n" if ok else "")

        # ---------------------------------------------------------------- reaction ball
        @r.message_reaction()
        async def on_reaction(ev: MessageReactionUpdated):
            """Faqat izoh guruhidagi raund postiga qo'yilgan, foydalanuvchi aniqlanadigan reaksiyalar hisoblanadi
            (kanal reaksiyalari Telegram'da anonim). Bir foydalanuvchi + raund + emoji = bir marta (idem kalit)."""
            u = ev.user
            if not u or u.is_bot:
                return
            new = {x.emoji for x in ev.new_reaction if getattr(x, "emoji", None)}
            old = {x.emoji for x in ev.old_reaction if getattr(x, "emoji", None)}
            added = new - old
            if not added:
                return
            rd = await one("SELECT r.id rid, r.battle_id bid FROM rounds r JOIN battles b ON b.id=r.battle_id "
                           "WHERE r.group_id=? AND r.thread_id=? AND r.active=1 AND b.state='ACTIVE'", ev.chat.id, ev.message_id)
            if not rd:
                return
            hit = False
            async with tx() as t:
                s = await BE.get_settings(t, rd["bid"])
                w = s["reaction_weights"]
                for e in sorted(added):
                    amt = w.get(e, 0) if w else s["reaction_score"]
                    if not amt:
                        continue
                    try:
                        hit |= await BE.add_score(t, rd["bid"], u.id, "reaction", int(amt), f"re:{rd['rid']}:{u.id}:{e}", round_id=rd["rid"])
                    except BE.BattleError:
                        break
            if hit:
                await g["schedule_post_update"](ev.bot, rd["bid"])

        return r
    return SimpleNamespace(**{k: v for k, v in locals().items() if not k.startswith('_')})


BT = _make_texts()
BE = _make_engine()
BR = _make_roles()
BV = _make_views()
build_battle_router = _make_handlers().build


INDEX_HTML = r'''<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Battle Hub</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://unpkg.com/@tonconnect/ui@2.0.9/dist/tonconnect-ui.min.js"></script>
<style>
:root{--bg:#0b0b14;--card:rgba(255,255,255,.06);--line:rgba(255,255,255,.1);--tx:#f4f4fb;--mut:#9a9ab5;
--a1:#7c3aed;--a2:#ec4899;--ok:#22c55e}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;
padding:0 16px calc(84px + env(safe-area-inset-bottom))}
.hero{margin:14px 0;border-radius:22px;overflow:hidden;position:relative;aspect-ratio:2/1;background:#222 center/cover}
.hero:after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,transparent 40%,rgba(0,0,0,.65))}
.hero h1{position:absolute;left:16px;bottom:12px;margin:0;font-size:22px;z-index:1}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px;margin:10px 0;backdrop-filter:blur(8px)}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px}
.mut{color:var(--mut);font-size:13px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stat b{display:block;font-size:20px}
.btn{border:0;border-radius:14px;padding:11px 16px;color:#fff;font-weight:600;font-size:14px;
background:linear-gradient(135deg,var(--a1),var(--a2));cursor:pointer}
.btn:active{transform:scale(.97)}
.btn.ghost{background:var(--card);border:1px solid var(--line)}
h2{font-size:17px;margin:18px 2px 6px}
.lb{display:flex;justify-content:space-between;padding:5px 0;border-top:1px solid var(--line)}
nav{position:fixed;left:0;right:0;bottom:0;display:flex;justify-content:space-around;
padding:8px 8px calc(8px + env(safe-area-inset-bottom));background:rgba(11,11,20,.92);backdrop-filter:blur(14px);border-top:1px solid var(--line)}
nav button{background:none;border:0;color:var(--mut);font-size:11px;display:flex;flex-direction:column;align-items:center;gap:2px;padding:4px 10px;border-radius:12px}
nav button span{font-size:20px}
nav button.on{color:#fff;background:var(--card)}
.chips{display:flex;gap:8px;overflow-x:auto;padding:4px 0}
.chip{white-space:nowrap;padding:7px 14px;border-radius:99px;background:var(--card);border:1px solid var(--line);font-size:13px}
.chip.on{background:linear-gradient(135deg,var(--a1),var(--a2));border-color:transparent}
.empty{text-align:center;color:var(--mut);padding:30px 0}
</style>
</head>
<body>
<div id="app"><div class="empty">Yuklanmoqda…</div></div>
<nav id="nav"></nav>
<script>
const tg = Telegram.WebApp; tg.ready(); tg.expand();
try { tg.setHeaderColor('#0b0b14'); tg.setBackgroundColor('#0b0b14'); } catch (e) {}
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = (p, b) => fetch(p, {method: b ? 'POST' : 'GET',
  headers: {'X-Init-Data': tg.initData, 'Content-Type': 'application/json'},
  body: b ? JSON.stringify(b) : undefined}).then(r => r.json());
const TABS = [['home','🏠','Bosh'],['battles','⚔️','Batllar'],['shop','🛍','Do‘kon'],['wallet','👛','Hamyon']];
let S = null, kind = 'game';
let tab = new URLSearchParams(location.search).get('tab') || 'home';
let tonUI = null;

const uzs = n => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + " so'm";
function gamesHtml() {
  if (!S.games || !S.games.length) return '<div class="empty">Hozircha mavjud emas</div>';
  return S.games.map(g => `<div class="card"><b>${esc(g.emoji)} ${esc(g.name)}</b>
    <div class="chips" style="flex-wrap:wrap;margin-top:8px">${g.packs.map(p =>
      `<div class="chip" onclick="openBot('game_${p.id}')">${esc(p.title)} · ${uzs(p.uzs)}</div>`).join('')}</div></div>`).join('');
}
function hero(key, title) { return `<div class="hero" style="background-image:url(/img/${key})"><h1>${esc(title)}</h1></div>`; }
function nav() {
  document.getElementById('nav').innerHTML = TABS.map(([k, i, n]) =>
    `<button class="${tab === k ? 'on' : ''}" onclick="go('${k}')"><span>${i}</span>${n}</button>`).join('');
}
function go(t) { tab = t; tg.HapticFeedback?.selectionChanged(); render(); }

function render() {
  nav();
  const el = document.getElementById('app');
  if (!S) return;
  const u = S.user;
  if (tab === 'home') {
    el.innerHTML = hero('home', S.brand) + `
      <div class="card"><div class="row"><div><b>${esc(u.name)}</b><div class="mut">ID ${u.id}</div></div>
      ${u.vip ? '<span class="chip on">👑 VIP</span>' : ''}</div></div>
      <div class="stats">
        <div class="card stat"><span class="mut">Balans</span><b>${u.balance}⭐</b></div>
        <div class="card stat"><span class="mut">Batl kreditlari</span><b>${u.credits}</b></div>
        <div class="card stat"><span class="mut">Faol batllar</span><b>${S.battles.length}</b></div>
        <div class="card stat"><span class="mut">Hamyon</span><b>${u.wallet ? '✅ ulangan' : '—'}</b></div>
      </div>
      <h2>Tezkor amallar</h2>
      <div class="card row"><span>Balansni to'ldirish</span><button class="btn" onclick="go('wallet')">➕</button></div>
      <div class="card row"><span>Batl yaratish</span><button class="btn ghost" onclick="openBot('')">Botda</button></div>`;
  } else if (tab === 'battles') {
    el.innerHTML = hero('battle', 'Faol batllar') + (S.battles.length ? S.battles.map(b => `
      <div class="card"><div class="row"><b>${esc(b.title)}</b><span class="chip">👥 ${b.n}</span></div>
      <div class="mut">💎 ${b.points} ball${b.ends ? ' · ⏰ ' + esc(b.ends) : ''}</div>
      ${b.top.map((t, i) => `<div class="lb"><span>${['🥇','🥈','🥉','4.','5.'][i]} ${esc(t.nick)}</span><b>${t.points}</b></div>`).join('')}
      <div style="margin-top:10px"><button class="btn" onclick="openBot('join_${b.id}')">✅ Qatnashish</button></div></div>`).join('')
      : '<div class="empty">Hozircha faol batl yo‘q</div>');
  } else if (tab === 'shop') {
    const kinds = [['game','🎮 Donat'],['premium','💎 Premium'],['stars','⭐ Stars'],['gift','🎁 Giftlar'],['nft','🖼 NFT']];
    const items = S.items.filter(i => i.kind === kind);
    el.innerHTML = hero('shop', 'Do‘kon') + `<div class="chips">${kinds.map(([k, n]) =>
      `<div class="chip ${kind === k ? 'on' : ''}" onclick="kind='${k}';render()">${n}</div>`).join('')}</div>` +
      (kind === 'game' ? gamesHtml() : items.length ? items.map(i => `<div class="card"><b>${esc(i.title)}</b><div class="mut">${esc(i.descr)}</div>
      <div class="row" style="margin-top:10px"><button class="btn" onclick="openBot('buy_${i.id}')">${uzs(i.uzs)}</button>
      ${i.kind === 'stars' ? '' : `<button class="btn ghost" onclick="buy(${i.id})">${i.stars}⭐</button>`}</div></div>`).join('')
      : '<div class="empty">Bu bo‘limda mahsulot yo‘q</div>');
  } else if (tab === 'wallet') {
    el.innerHTML = hero('wallet', 'Hamyon') + `
      <div class="card"><span class="mut">Bot balansi</span><b style="font-size:26px;display:block">${u.balance}⭐</b></div>
      <h2>Balansni to'ldirish</h2>
      <div class="chips">${[50,100,250,500,1000].map(a => `<div class="chip" onclick="topup(${a})">${a}⭐</div>`).join('')}</div>
      <h2>💝 Donat</h2>
      <div class="chips">${[10,25,50,100,250].map(a => `<div class="chip" onclick="donate(${a})">${a}⭐</div>`).join('')}</div>
      <h2>🧾 Buyurtmalarim</h2>
      <div id="ords" class="card"><span class="mut">Yuklanmoqda…</span></div>
      <h2>TON hamyon</h2>
      <div class="card"><div class="mut" style="word-break:break-all;margin-bottom:10px">${u.wallet ? esc(u.wallet) : 'Hamyon ulanmagan'}</div>
      <div id="tonbtn"></div></div>`;
    initTon();
    loadOrders();
  }
}

function pay(link) {
  tg.openInvoice(link, st => {
    if (st === 'paid') { tg.HapticFeedback?.notificationOccurred('success'); setTimeout(load, 1500); }
  });
}
async function buy(id) {
  const r = await api('/api/buy', {item_id: id});
  if (r.link) pay(r.link); else if (r.bot) tg.openTelegramLink(r.bot); else tg.showAlert(r.error || 'Xatolik');
}
async function topup(a) {
  const r = await api('/api/topup', {stars: a});
  if (r.link) pay(r.link); else tg.showAlert(r.error || 'Xatolik');
}
async function donate(a) {
  const r = await api('/api/donate', {stars: a});
  if (r.link) pay(r.link); else tg.showAlert(r.error || 'Xatolik');
}
async function loadOrders() {
  const r = await api('/api/orders'); const el = document.getElementById('ords'); if (!el) return;
  el.innerHTML = (r.orders || []).length ? r.orders.map(o => `<div style="padding:6px 0;border-bottom:1px solid rgba(128,128,128,.2)">
    <b>${esc(o.code)}</b> · ${esc(o.title)}<br><span class="mut">${esc(o.state)} · ${esc(o.ts)}</span></div>`).join('') : '<span class="mut">Hozircha buyurtma yo‘q</span>';
}
function openBot(p) { tg.openTelegramLink(`https://t.me/${S.bot}${p ? '?start=' + p : ''}`); tg.close(); }

function initTon() {
  if (!window.TON_CONNECT_UI) { return; }
  if (!tonUI) {
    tonUI = new TON_CONNECT_UI.TonConnectUI({manifestUrl: location.origin + '/tonconnect-manifest.json'});
    tonUI.onStatusChange(async w => {
      let addr = w ? w.account.address : '';
      try { if (addr) addr = TON_CONNECT_UI.toUserFriendlyAddress(addr); } catch (e) {}
      if (addr !== (S.user.wallet || '')) { await api('/api/wallet', {address: addr}); S.user.wallet = addr; if (tab === 'wallet') render(); }
    });
  }
  tonUI.uiOptions = {buttonRootId: 'tonbtn'};
}

async function load() {
  S = await api('/api/state');
  if (S.error) { document.getElementById('app').innerHTML = `<div class="empty">${esc(S.error)}</div>`; return; }
  render();
}
load();
</script>
</body>
</html>
'''


##############################################################################
# app/config.py
##############################################################################
load_dotenv()

log = logging.getLogger("battlehub")

BOT_TOKEN = os.environ["BOT_TOKEN"]
_bid = BOT_TOKEN.split(":")[0]
BOT_ID = int(_bid) if _bid.isdigit() else 0          # multi-bot: har bir bot o'z bot_id si bilan ajratiladi
OWNER_ID = int(os.environ["OWNER_ID"])
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
TZ = int(os.getenv("TZ_OFFSET", "5"))
TONCENTER_KEY = os.getenv("TONCENTER_KEY", "")

# ---- moliyaviy tizim sozlamalari (env orqali o'zgartirish mumkin)
ORDER_TTL = int(os.getenv("ORDER_TTL_MIN", "60")) * 60          # kutilayotgan buyurtma muddati
MAX_DELIVERY_RETRIES = int(os.getenv("MAX_DELIVERY_RETRIES", "6"))
RETRY_BACKOFF = (60, 300, 900, 3600, 10800, 21600)              # eksponensial kutish (soniya)
FUNDS_RECHECK = 600                                             # Stars yetmasa har 10 daqiqada qayta tekshirish
FUNDS_WAIT_MAX = 72 * 3600                                      # 72 soatdan keyin «failed»
STALE_PROCESSING = 600                                          # «processing» da qotib qolgan yetkazish (soniya)
TON_ORDER_GRACE = 24 * 3600                                     # muddati o'tgan buyurtmaga kelgan TON ni qabul qilish oynasi
MIN_TOPUP, MAX_TOPUP = 10, 10000
MIN_DONATION, MAX_DONATION = 10, 10000


##############################################################################
# app/core/db.py
##############################################################################
def now() -> int:
    """Yagona vaqt manbai (testlarda almashtiriladi)."""
    return int(time.time())


class DBError(RuntimeError):
    pass


class Tx:
    """Bitta tranzaksiya ichidagi so'rovlar. Qulf tx() tomonidan butun tranzaksiya davomida ushlab turiladi."""

    def __init__(self, conn):
        self._c = conn

    async def query(self, sql, *a):
        return await asyncio.to_thread(lambda: self._c.execute(sql, a).fetchall())

    async def one(self, sql, *a):
        rows = await self.query(sql, *a)
        return rows[0] if rows else None

    async def execute(self, sql, *a):
        def _do():
            cur = self._c.execute(sql, a)
            return cur.lastrowid, cur.rowcount
        return await asyncio.to_thread(_do)


class Database:
    """SQLite (WAL) ustida asinxron qatlam. Barcha yozuvlar bitta qulf bilan ketma-ketlashtiriladi;
    pul bilan bog'liq amallar tx() ichida atomik bajariladi."""

    def __init__(self):
        self._c = None
        self._lock = asyncio.Lock()
        self._owner = None

    async def connect(self, path: str):
        def _open():
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            c = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=FULL")
            return c
        self._c = await asyncio.to_thread(_open)

    async def close(self):
        if self._c is not None:
            c, self._c = self._c, None
            await asyncio.to_thread(c.close)

    async def _acquire(self):
        if self._owner is not None and self._owner is asyncio.current_task():
            raise DBError("tx() ichida oddiy DB chaqiruvi: tx obyektidan foydalaning")
        await self._lock.acquire()

    async def query(self, sql, *a):
        await self._acquire()
        try:
            return await asyncio.to_thread(lambda: self._c.execute(sql, a).fetchall())
        finally:
            self._lock.release()

    async def execute(self, sql, *a):
        await self._acquire()
        try:
            def _do():
                cur = self._c.execute(sql, a)
                return cur.lastrowid, cur.rowcount
            return await asyncio.to_thread(_do)
        finally:
            self._lock.release()

    @asynccontextmanager
    async def tx(self):
        await self._acquire()
        self._owner = asyncio.current_task()
        try:
            await asyncio.to_thread(self._c.execute, "BEGIN IMMEDIATE")
            try:
                yield Tx(self._c)
            except BaseException:
                await asyncio.shield(asyncio.to_thread(self._rollback))
                raise
            try:
                await asyncio.to_thread(self._c.execute, "COMMIT")
            except BaseException:
                await asyncio.shield(asyncio.to_thread(self._rollback))
                raise
        finally:
            self._owner = None
            self._lock.release()

    def _rollback(self):
        try:
            self._c.execute("ROLLBACK")
        except sqlite3.Error:
            pass


DB = Database()


def tx():
    return DB.tx()


async def q(sql, *a):
    return await DB.query(sql, *a)


async def one(sql, *a):
    rows = await DB.query(sql, *a)
    return rows[0] if rows else None


async def run(sql, *a):
    return await DB.execute(sql, *a)


class _RO:
    """O'qish uchun Tx-ga o'xshash adapter (battle.views / engine o'qish funksiyalari uchun)."""
    async def query(self, sql, *a):
        return await DB.query(sql, *a)

    async def one(self, sql, *a):
        rows = await DB.query(sql, *a)
        return rows[0] if rows else None

    async def execute(self, sql, *a):
        return await DB.execute(sql, *a)


RO = _RO()


async def S(key, default=""):
    row = await one("SELECT value FROM settings WHERE key=?", key)
    return row["value"] if row and row["value"] is not None else default


async def S_t(t, key, default=""):
    """tx() ichida sozlamani o'qish."""
    row = await t.one("SELECT value FROM settings WHERE key=?", key)
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
    await run("INSERT OR IGNORE INTO users(id,name,username,joined) VALUES(?,?,?,?)",
              u.id, u.full_name, u.username, now())
    return True


DEFAULTS = {
    "brand": "Battle Hub", "battle_fee": 25, "vip_price": 500, "vip_days": 30,
    "ton_rate": 200, "markup": 15, "ref_per_credit": 3, "ton_address": "", "force_sub": "",
    "uzs_rate": 230, "card_number": "", "card_holder": "", "provider_token": "",
    "stars_warn": 1500, "stars_crit": 500,
}


async def seed_defaults():
    for k, v in DEFAULTS.items():
        await run("INSERT OR IGNORE INTO settings VALUES(?,?)", k, str(v))
    if not await one("SELECT 1 FROM games LIMIT 1"):
        # Paket nomlari tayyor, NARXLAR yo'q: owner botning ichidan narx qo'yguncha foydalanuvchiga ko'rinmaydi
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
                      f"⭐ {n} Stars", "To'lovdan so'ng admin akkauntingizga yuboradi", int(n * 1.3), str(n))


async def init_db(path: str | None = None):
    await DB.connect(path or DB_PATH)
    await migrate()
    await seed_defaults()
    log.info("DB tayyor: %s", path or DB_PATH)


async def close_db():
    await DB.close()


##############################################################################
# app/core/migrations.py
##############################################################################
# =====================================================================================
# Migratsiyalar. Har biri bir marta, bitta tranzaksiyada bajariladi (schema_migrations).
#   1 — baza (avvalgi versiyalardagi jadvallar; eski bazaga tegmaydi)
#   2 — moliyaviy yadro (orders ustunlari qayta nomlanadi, yangi jadvallar, ledger triggerlari)
#   3 — eski buyurtmalarni yangi tuzilmaga ko'chirish (ma'lumot yo'qolmaydi)
# =====================================================================================

M1 = [
    """CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, name TEXT, username TEXT, wallet TEXT DEFAULT '',
        balance INTEGER DEFAULT 0, credits INTEGER DEFAULT 0, vip_until INTEGER DEFAULT 0,
        ref_by INTEGER, ref_count INTEGER DEFAULT 0, joined INTEGER, banned INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS channels(
        id INTEGER PRIMARY KEY, title TEXT, username TEXT, group_id INTEGER, owner_id INTEGER)""",
    """CREATE TABLE IF NOT EXISTS battles(
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, channel_id INTEGER, title TEXT,
        rules TEXT, points INTEGER DEFAULT 1, end_ts INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
        post_id INTEGER, created INTEGER)""",
    """CREATE TABLE IF NOT EXISTS participants(
        battle_id INTEGER, user_id INTEGER, nick TEXT, points INTEGER DEFAULT 0,
        PRIMARY KEY(battle_id, user_id))""",
    """CREATE TABLE IF NOT EXISTS rounds(
        id INTEGER PRIMARY KEY AUTOINCREMENT, battle_id INTEGER, n INTEGER, points INTEGER,
        post_id INTEGER, group_id INTEGER, thread_id INTEGER, active INTEGER DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS scored(
        round_id INTEGER, user_id INTEGER, PRIMARY KEY(round_id, user_id))""",
    """CREATE TABLE IF NOT EXISTS plog(
        id INTEGER PRIMARY KEY AUTOINCREMENT, battle_id INTEGER, user_id INTEGER,
        delta INTEGER, reason TEXT, ts INTEGER)""",
    """CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT, descr TEXT DEFAULT '',
        price_stars INTEGER, payload TEXT DEFAULT '', active INTEGER DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, item_id INTEGER DEFAULT 0,
        stars INTEGER, ton REAL, method TEXT DEFAULT '', status TEXT DEFAULT 'pending',
        code TEXT, charge TEXT, ts INTEGER, uzs INTEGER DEFAULT 0, meta TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS games(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, emoji TEXT DEFAULT '🎮',
        server INTEGER DEFAULT 0, id_label TEXT DEFAULT 'Player ID', active INTEGER DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS packs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER, title TEXT,
        price_uzs INTEGER DEFAULT 0, active INTEGER DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS gids(
        user_id INTEGER, game_id INTEGER, pid TEXT, server TEXT DEFAULT '', PRIMARY KEY(user_id, game_id))""",
    "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_rounds_thread ON rounds(group_id, thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_rounds_post ON rounds(post_id)",
]

M2 = [
    # --- orders: ustunlarni professional nomlarga o'tkazamiz (ma'lumot saqlanadi)
    "ALTER TABLE orders RENAME COLUMN kind TO product_type",
    "ALTER TABLE orders RENAME COLUMN item_id TO product_id",
    "ALTER TABLE orders RENAME COLUMN method TO payment_method",
    "ALTER TABLE orders RENAME COLUMN code TO order_code",
    "ALTER TABLE orders RENAME COLUMN charge TO provider_payment_id",
    "ALTER TABLE orders RENAME COLUMN ts TO created_at",
    "ALTER TABLE orders ADD COLUMN bot_id INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN product_title TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE orders ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE orders ADD COLUMN amount INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN currency TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN payment_provider TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN paid_at INTEGER",
    "ALTER TABLE orders ADD COLUMN delivered_at INTEGER",
    "ALTER TABLE orders ADD COLUMN expires_at INTEGER",
    "ALTER TABLE orders ADD COLUMN last_error TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN ton_nano INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(bot_id, user_id, id)",
    "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(bot_id, payment_status, delivery_status)",
    # --- payments
    """CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL DEFAULT 0,
        order_id INTEGER NOT NULL REFERENCES orders(id), user_id INTEGER,
        method TEXT NOT NULL, provider TEXT NOT NULL, provider_payment_id TEXT,
        amount INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'created', created_at INTEGER NOT NULL,
        confirmed_at INTEGER, error TEXT NOT NULL DEFAULT '')""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_provider
        ON payments(bot_id, provider, provider_payment_id) WHERE provider_payment_id IS NOT NULL""",
    "CREATE INDEX IF NOT EXISTS ix_payments_order ON payments(order_id)",
    "CREATE INDEX IF NOT EXISTS ix_payments_status ON payments(bot_id, status, method)",
    """CREATE TABLE IF NOT EXISTS payment_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL DEFAULT 0,
        payment_id INTEGER, order_id INTEGER, event TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_pevents_order ON payment_events(order_id)",
    # --- deliveries
    """CREATE TABLE IF NOT EXISTS deliveries(
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL DEFAULT 0,
        order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id), mode TEXT NOT NULL DEFAULT 'auto',
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        funds_checks INTEGER NOT NULL DEFAULT 0, next_retry_at INTEGER, locked_at INTEGER,
        last_error TEXT NOT NULL DEFAULT '', external_ref TEXT NOT NULL DEFAULT '',
        user_notified INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL, delivered_at INTEGER)""",
    "CREATE INDEX IF NOT EXISTS ix_deliveries_due ON deliveries(bot_id, status, next_retry_at)",
    # --- ledger (o'zgartirib bo'lmaydi)
    """CREATE TABLE IF NOT EXISTS ledger_entries(
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL DEFAULT 0,
        order_id INTEGER, payment_id INTEGER, user_id INTEGER,
        type TEXT NOT NULL, account TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT '', provider_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'completed', idem_key TEXT NOT NULL,
        created_at INTEGER NOT NULL, metadata TEXT NOT NULL DEFAULT '')""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_idem ON ledger_entries(bot_id, idem_key)",
    "CREATE INDEX IF NOT EXISTS ix_ledger_order ON ledger_entries(order_id)",
    "CREATE INDEX IF NOT EXISTS ix_ledger_user ON ledger_entries(bot_id, user_id, account)",
    "CREATE INDEX IF NOT EXISTS ix_ledger_time ON ledger_entries(bot_id, type, created_at)",
    """CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger_entries
        BEGIN SELECT RAISE(ABORT, 'ledger_entries o''zgartirilmaydi'); END""",
    """CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger_entries
        BEGIN SELECT RAISE(ABORT, 'ledger_entries o''chirilmaydi'); END""",
    # --- donations / subscriptions / refunds / audit
    """CREATE TABLE IF NOT EXISTS donations(
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL DEFAULT 0,
        order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id), user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed',
        created_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_donations_user ON donations(bot_id, user_id)",
    """CREATE TABLE IF NOT EXISTS subscriptions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER NOT NULL,
        plan TEXT NOT NULL, order_id INTEGER UNIQUE REFERENCES orders(id), status TEXT NOT NULL DEFAULT 'active',
        starts_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, reminded INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_subs_user ON subscriptions(bot_id, user_id, status)",
    """CREATE TABLE IF NOT EXISTS refunds(
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL DEFAULT 0,
        order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id), payment_id INTEGER, user_id INTEGER,
        amount INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT '', method TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '', admin_id INTEGER,
        error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, completed_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS audit_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, bot_id INTEGER NOT NULL DEFAULT 0,
        user_id INTEGER, admin_id INTEGER, order_id INTEGER, action TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ok', error TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS ix_audit_order ON audit_logs(order_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_logs(bot_id, ts)",
]

_LEGACY_METHOD = {          # eski method -> (provider, currency)
    "stars": ("telegram_stars", "XTR"), "uzs": ("telegram_payments", "UZS"), "card": ("manual_card", "UZS"),
    "ton": ("ton", "TON"), "balance": ("wallet", "XTR"), "manual": ("manual", "UZS"),
}
_SALE_TYPE = {"premium": "PREMIUM_PURCHASE", "gift": "GIFT_PURCHASE", "nft": "NFT_PURCHASE"}
_AUTO_TYPES = {"premium", "gift"}
_INTERNAL_TYPES = {"topup", "battle_credit", "vip", "bot_fund", "donation"}


async def _ledger_raw(t, *, order_id, user_id, type_, account, amount, currency, provider, provider_id, idem, ts, meta):
    await t.execute(
        "INSERT OR IGNORE INTO ledger_entries(bot_id,order_id,user_id,type,account,amount,currency,provider,provider_id,"
        "status,idem_key,created_at,metadata) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?,?)",
        BOT_ID, order_id, user_id, type_, account, amount, currency, provider, provider_id, idem, ts, meta)


async def _m3_backfill(t):
    """Eski buyurtmalarni yangi tuzilmaga ko'chiradi. Hech narsa o'chirilmaydi, hech kimga qayta yuborilmaydi."""
    ts_now = now()
    for r in await t.query("SELECT * FROM orders WHERE bot_id=0 ORDER BY id"):
        oid, uid = r["id"], r["user_id"]
        ptype, pid = r["product_type"], r["product_id"] or 0
        title = ""
        if ptype == "item":
            it = await t.one("SELECT kind,title FROM items WHERE id=?", pid)
            ptype, title = (it["kind"], it["title"]) if it else ("item", "Mahsulot")
        elif ptype == "fee":
            ptype, title = "battle_credit", "⚔️ Batl yaratish krediti"
        elif ptype == "game":
            g = await t.one("SELECT p.title pt, g.name gn, g.emoji ge FROM packs p JOIN games g ON g.id=p.game_id WHERE p.id=?", pid)
            title = f"{g['ge']} {g['gn']} — {g['pt']}" if g else "🎮 O'yin donati"
        elif ptype == "topup":
            title = f"💰 Balansni to'ldirish — {r['stars']}⭐"
        elif ptype == "vip":
            title = "👑 VIP obuna"
        method = r["payment_method"] or ""
        provider, cur = _LEGACY_METHOD.get(method, ("", ""))
        amount = {"XTR": r["stars"] or 0, "UZS": r["uzs"] or 0, "TON": int(round((r["ton"] or 0) * 1e9))}.get(cur, 0)
        st = r["status"] or "pending"
        created = r["created_at"] or ts_now
        if st == "pending":
            fresh = created > ts_now - ORDER_TTL
            ps, ds = ("pending" if fresh else "expired"), "none"
        elif st == "rejected":
            ps, ds = "failed", "none"
        elif st == "delivered" or ptype in _INTERNAL_TYPES:
            ps, ds = "paid", "delivered"
        else:                                  # paid / sending: yetkazilishi noaniq -> ko'rib chiqish kerak
            ps, ds = "paid", "failed"
        code = f"BH-L{oid}"
        ton_nano = int(round((r["ton"] or 0) * 1e9))
        await t.execute(
            "UPDATE orders SET bot_id=?, product_type=?, product_title=?, payment_status=?, delivery_status=?, amount=?, "
            "currency=?, payment_provider=?, paid_at=?, delivered_at=?, expires_at=?, ton_nano=?, order_code=? WHERE id=?",
            BOT_ID, ptype, title, ps, ds, amount if ps == "paid" else 0, cur if ps == "paid" else "",
            provider if ps == "paid" else "", created if ps == "paid" else None,
            created if ds == "delivered" else None, (created + ORDER_TTL) if ps == "pending" else None,
            ton_nano, code, oid)
        if ps != "paid":
            continue
        ppid = r["provider_payment_id"] or f"legacy:{oid}"
        pay_id, _ = await t.execute(
            "INSERT OR IGNORE INTO payments(bot_id,order_id,user_id,method,provider,provider_payment_id,amount,currency,"
            "status,created_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?,'confirmed',?,?)",
            BOT_ID, oid, uid, method or "legacy", provider or "legacy", ppid, amount, cur or "XTR", created, created)
        meta = "legacy=1"
        if method != "balance" and cur:
            await _ledger_raw(t, order_id=oid, user_id=uid, type_="PAYMENT", account="cash", amount=amount, currency=cur,
                              provider=provider, provider_id=ppid, idem=f"legacy:{oid}:pay", ts=created, meta=meta)
        if ptype == "topup":
            await _ledger_raw(t, order_id=oid, user_id=uid, type_="WALLET_TOPUP", account="wallet", amount=r["stars"] or 0,
                              currency="XTR", provider="wallet", provider_id=ppid, idem=f"legacy:{oid}:wallet", ts=created, meta=meta)
        else:
            if method == "balance":
                await _ledger_raw(t, order_id=oid, user_id=uid, type_="WALLET_SPEND", account="wallet", amount=-(r["stars"] or 0),
                                  currency="XTR", provider="wallet", provider_id=ppid, idem=f"legacy:{oid}:wallet", ts=created, meta=meta)
            await _ledger_raw(t, order_id=oid, user_id=uid, type_=_SALE_TYPE.get(ptype, "PRODUCT_PURCHASE"), account="revenue",
                              amount=amount, currency=cur or "XTR", provider=provider, provider_id=ppid,
                              idem=f"legacy:{oid}:sale", ts=created, meta=meta)
        mode = "auto" if ptype in _AUTO_TYPES else ("internal" if ptype in _INTERNAL_TYPES else "manual")
        if ptype != "donation":
            await t.execute(
                "INSERT OR IGNORE INTO deliveries(bot_id,order_id,mode,status,last_error,created_at,updated_at,delivered_at) "
                "VALUES(?,?,?,?,?,?,?,?)", BOT_ID, oid, mode, ds,
                "" if ds == "delivered" else "LEGACY: yetkazilganligi noaniq — ko'rib chiqing", created, ts_now,
                created if ds == "delivered" else None)
    # --- balans tarixi: hozirgi balansga mos «ochilish qoldig'i»
    for u in await t.query("SELECT id, balance FROM users"):
        s = await t.one("SELECT COALESCE(SUM(amount),0) s FROM ledger_entries WHERE bot_id=? AND user_id=? AND account='wallet'",
                        BOT_ID, u["id"])
        diff = (u["balance"] or 0) - s["s"]
        if diff:
            await _ledger_raw(t, order_id=None, user_id=u["id"], type_="ADJUSTMENT", account="wallet", amount=diff, currency="XTR",
                              provider="migration", provider_id="", idem=f"opening:{u['id']}", ts=ts_now,
                              meta="opening balance (migration)")
    # --- eski VIP -> subscriptions
    for u in await t.query("SELECT id, vip_until FROM users WHERE vip_until > ?", ts_now):
        await t.execute("INSERT INTO subscriptions(bot_id,user_id,plan,order_id,status,starts_at,expires_at,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)", BOT_ID, u["id"], "vip", None, "active", ts_now, u["vip_until"], ts_now)
    await t.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_code ON orders(bot_id, order_code)")
    await t.execute("INSERT INTO audit_logs(ts,bot_id,action,status,error,ref) VALUES(?,?,?,?,?,?)",
                    ts_now, BOT_ID, "migration_backfill", "ok", "", "m3")


async def _m1(t):
    for s in M1:
        await t.execute(s)
    cols = {r["name"] for r in await t.query("PRAGMA table_info(orders)")}
    if "kind" in cols:                         # eski bazalar uchun yetishmayotgan ustunlar
        if "uzs" not in cols:
            await t.execute("ALTER TABLE orders ADD COLUMN uzs INTEGER DEFAULT 0")
        if "meta" not in cols:
            await t.execute("ALTER TABLE orders ADD COLUMN meta TEXT DEFAULT ''")


async def _m2(t):
    for s in M2:
        await t.execute(s)


async def _m3(t):
    await _m3_backfill(t)
    try:                                       # eski aralash «status» ustuni endi kerak emas (SQLite >= 3.35)
        await t.execute("ALTER TABLE orders DROP COLUMN status")
    except sqlite3.OperationalError as e:
        log.info("orders.status ustuni qoldirildi: %s", e)


async def _m4(t):
    """Bildirishnomalar: bit1 = «to'landi» xabari yuborilgan, bit2 = «yetkazildi» xabari yuborilgan.
    Eski buyurtmalar uchun qayta xabar yuborilmaydi."""
    await t.execute("ALTER TABLE orders ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")
    await t.execute("UPDATE orders SET notified=3")
    await t.execute("CREATE INDEX IF NOT EXISTS ix_orders_notify ON orders(bot_id, payment_status, notified)")
    await t.execute("CREATE INDEX IF NOT EXISTS ix_orders_ton ON orders(bot_id, payment_method, payment_status)")


async def _m5(t):
    """Battle Engine: holat mashinasi, ball manbalari (score events), settings, winners, contest audit (battle/schema.py)."""
    for sql in BATTLE_M5:
        await t.execute(sql)


async def _m6(t):
    """Battle rollari (battle_admins): manager / moderator."""
    for sql in BATTLE_M6:
        await t.execute(sql)


MIGRATIONS = [(1, "baseline", _m1), (2, "financial_core", _m2), (3, "legacy_backfill", _m3), (4, "notifications", _m4),
              (5, "battle_engine", _m5), (6, "battle_roles", _m6)]


async def migrate():
    await DB.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at INTEGER)")
    done = {r["version"] for r in await DB.query("SELECT version FROM schema_migrations")}
    for ver, name, fn in MIGRATIONS:
        if ver in done:
            continue
        async with tx() as t:
            await fn(t)
            await t.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)", ver, name, now())
        log.info("migratsiya %s (%s) bajarildi", ver, name)


##############################################################################
# app/core/money.py
##############################################################################
# ---- Telegram Premium: Bot API faqat 3/6/12 oylik sovg'a (Stars narxi Telegram tomonidan belgilangan)
PREMIUM_STARS = {3: 1000, 6: 1500, 12: 2500}

# ---- mahsulot turlari
SALE_TYPES = ("premium", "gift", "nft", "stars", "game")          # sotuv, yetkazish kerak
AUTO_TYPES = ("premium", "gift")                                   # Telegram API orqali avtomatik
INTERNAL_TYPES = ("topup", "battle_credit", "vip", "battle_star")                 # to'lov bilan bir tranzaksiyada bajariladi
ALL_TYPES = SALE_TYPES + INTERNAL_TYPES + ("donation",)
REVENUE_TYPES = SALE_TYPES + ("vip", "battle_credit", "battle_star")              # daromad hisoblanadigan turlar

TYPE_LABEL = {"premium": "💎 Premium", "gift": "🎁 Gift", "nft": "🖼 NFT", "stars": "⭐ Stars", "game": "🎮 O'yin donati",
              "topup": "💰 Balans to'ldirish", "vip": "👑 VIP", "battle_credit": "⚔️ Batl krediti", "donation": "💝 Donat",
              "battle_star": "⭐ Battle ovozi"}

# usul -> (provider, valyuta, nomi)
METHODS = {
    "stars": ("telegram_stars", "XTR", "⭐ Telegram Stars"),
    "uzs": ("telegram_payments", "UZS", "💳 Payme/Click/karta"),
    "card": ("manual_card", "UZS", "🏦 Karta o'tkazmasi"),
    "ton": ("ton", "TON", "💠 TON"),
    "wallet": ("wallet", "XTR", "💰 Bot balansi"),
}

# ledger turlari (sotuv)
SALE_LEDGER = {"premium": "PREMIUM_PURCHASE", "gift": "GIFT_PURCHASE", "nft": "NFT_PURCHASE", "stars": "STARS_PURCHASE",
               "game": "GAME_PURCHASE", "vip": "SUBSCRIPTION", "battle_credit": "BATTLE_FEE", "battle_star": "BATTLE_STAR"}

MIN_TON_RATIO = 0.98            # TON komissiya/yaxlitlash uchun ruxsat etilgan eng kam ulush


def calc_uzs(stars: int, rate: int) -> int:
    """⭐ narxini so'mga aylantiradi (1000 so'mgacha yuqoriga yaxlitlanadi)."""
    return int(math.ceil(stars * max(1, rate) / 1000.0) * 1000)


def calc_ton_nano(stars: int, ton_rate: int) -> int:
    return max(10_000_000, int(round(stars / max(1, ton_rate) * 1e9)))


def fmt_uzs(n: int) -> str:
    return f"{int(n):,}".replace(",", " ") + " so'm"


def fmt_ton(nano: int) -> str:
    return f"{nano / 1e9:.3f}".rstrip("0").rstrip(".") + " TON"


def fmt_amount(amount: int, currency: str) -> str:
    if currency == "XTR":
        return f"{amount}⭐"
    if currency == "UZS":
        return fmt_uzs(amount)
    if currency == "TON":
        return fmt_ton(amount)
    return f"{amount} {currency}"


def expected_amount(o, method: str) -> int:
    """To'lov summasi SERVERDA buyurtma yaratilganda muzlatilgan narxdan olinadi (mijoz yuborgan summaga ishonilmaydi)."""
    if method in ("stars", "wallet"):
        return int(o["stars"])
    if method in ("uzs", "card"):
        return int(o["uzs"])
    if method == "ton":
        return int(o["ton_nano"])
    return -1


def method_of(provider: str, currency: str) -> str:
    for k, (p, c, _) in METHODS.items():
        if p == provider and c == currency:
            return k
    return ""


def order_state(o) -> str:
    """Foydalanuvchiga ko'rinadigan buyurtma holati (to'lov va yetkazish ajratilgan)."""
    ps, ds, pt = o["payment_status"], o["delivery_status"], o["product_type"]
    if ps == "pending":
        return "⏳ To'lov kutilmoqda"
    if ps == "review":
        return "🔎 To'lov tekshirilmoqda"
    if ps == "expired":
        return "⌛ Muddati tugagan"
    if ps == "cancelled":
        return "✖️ Bekor qilingan"
    if ps == "failed":
        return "❌ To'lov tasdiqlanmadi"
    if ps == "refunded":
        return "↩️ Pul qaytarilgan"
    if ps == "paid":
        if pt == "donation":
            return "💝 Donat qabul qilindi"
        if ds == "delivered":
            return "✅ Bajarildi"
        if ds in ("pending", "processing", "retry"):
            return "💳 To'landi · 📦 yetkazilmoqda"
        if ds == "waiting_funds":
            return "💳 To'landi · ⏳ yetkazish navbatda"
        if ds == "manual_pending":
            return "💳 To'landi · 👨‍💼 admin yetkazmoqda"
        if ds == "uncertain":
            return "💳 To'landi · 🔎 yetkazish tekshirilmoqda"
        if ds == "failed":
            return "💳 To'landi · ⚠️ yetkazishda muammo (admin hal qiladi)"
        return "💳 To'landi"
    return ps


##############################################################################
# app/core/audit.py
##############################################################################
def error_ref() -> str:
    """Foydalanuvchiga ko'rsatiladigan xato raqami: log va audit bilan bog'lanadi."""
    return "E-" + secrets.token_hex(3).upper()


async def audit(action: str, *, user_id=None, admin_id=None, order_id=None, status="ok", error="", ref="", t=None):
    log.info("audit action=%s user=%s admin=%s order=%s status=%s ref=%s err=%s",
             action, user_id, admin_id, order_id, status, ref, (error or "")[:200])
    sql = ("INSERT INTO audit_logs(ts,bot_id,user_id,admin_id,order_id,action,status,error,ref) VALUES(?,?,?,?,?,?,?,?,?)")
    args = (now(), BOT_ID, user_id, admin_id, order_id, action, status, (error or "")[:500], ref)
    if t is not None:
        await t.execute(sql, *args)
    else:
        await run(sql, *args)


async def pay_event(event: str, *, order_id=None, payment_id=None, detail="", t=None):
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    sql = "INSERT INTO payment_events(bot_id,payment_id,order_id,event,detail,created_at) VALUES(?,?,?,?,?,?)"
    args = (BOT_ID, payment_id, order_id, event, detail[:2000], now())
    if t is not None:
        await t.execute(sql, *args)
    else:
        await run(sql, *args)


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
        [B("🎮 O'yin donati (PUBG · Free Fire · MLBB)", "g:list")],
        [B("🔥 Faol batllar", "m:battles"), B("🛍 Do'kon", "m:shop")],
        [B("⚔️ Batl yaratish", "m:new"), B("📂 Batllarim", "m:my")],
        [B("👛 Hamyon", "m:wallet"), B("👤 Profil", "m:profile")],
        [B("👑 VIP", "m:vip"), B("🎁 Do'st taklif qilish", "m:ref")],
        [B("💝 Donat", "m:donate"), B("🧾 Buyurtmalarim", "m:orders")],
        [B("🏆 Battle markazi", "bx:home")],
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


class Rcpt(StatesGroup):
    photo = State()


class GameBuy(StatesGroup):
    pid = State()
    srv = State()


class OGame(StatesGroup):
    name = State()
    server = State()


class OPack(StatesGroup):
    bulk = State()
    price = State()


class Don(StatesGroup):
    amount = State()


class OPay(StatesGroup):
    cost = State()


class BSet(StatesGroup):
    val = State()


class CNew(StatesGroup):
    ch = State()
    title = State()
    desc = State()
    dur = State()
    limit = State()
    reward = State()


class NftE(StatesGroup):
    photo = State()


class FB(StatesGroup):
    text = State()


class RoleAdd(StatesGroup):
    who = State()


##############################################################################
# app/battle_service.py
##############################################################################
_uname = None
MEDALS = ["🥇", "🥈", "🥉"]


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
    """Professional Battle Card (turi va holatiga qarab)."""
    return await BV.post_card(RO, b, fmt_ts)


async def join_markup(bot, bid) -> InlineKeyboardMarkup | None:
    """Dinamik tugmalar: faqat holatga mos keladiganlari chiqadi."""
    b = await one("SELECT b.state, b.btype, c.username FROM battles b LEFT JOIN channels c ON c.id=b.channel_id WHERE b.id=?", bid)
    if not b:
        return None
    u = await bot_username(bot)
    rows = []
    if b["state"] in ("REGISTRATION", "ACTIVE"):
        rows.append([B("🔥 QATNASHISH", url=f"https://t.me/{u}?start=join_{bid}")])
    if b["btype"] != "contest":
        rows.append([B("🏆 NATIJALAR", url=f"https://t.me/{u}?start=res_{bid}"), B("📊 TOP 10", f"bx:t:{bid}")])
        if b["state"] == "ACTIVE":
            rows.append([B("💬 FIKR BILDIRISH", url=f"https://t.me/{u}?start=fb_{bid}")])
    if b["username"]:
        rows.append([B("📢 KANAL", url=f"https://t.me/{b['username']}")])
    return K(*rows) if rows else None


async def update_post(bot: Bot, bid: int):
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not b or not b["post_id"]:
        return
    try:
        await bot.edit_message_text(await post_text(b), chat_id=b["channel_id"], message_id=b["post_id"],
                                    reply_markup=await join_markup(bot, bid))
    except Exception as e:
        log.debug("post yangilanmadi: %s", e)


_dirty: dict = {}


async def schedule_post_update(bot: Bot, bid: int, delay: float = 5.0):
    """Debounce: bir battle uchun `delay` soniya ichidagi ko'p o'zgarish — bitta edit (Telegram limitlarini asraydi)."""
    if bid in _dirty:
        return
    _dirty[bid] = True

    async def _go():
        try:
            await asyncio.sleep(delay)
        finally:
            _dirty.pop(bid, None)
        await update_post(bot, bid)
    t = asyncio.create_task(_go())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def finish_battle(bot: Bot, bid: int) -> bool:
    """Battle'ni yakunlaydi (idempotent). Vote/NFT: g'oliblar yoziladi (SETTLED). Contest: g'olib tekshiriladigan seed bilan tanlanadi.
    Ishtirokchisiz Contest bekor qilinadi (ticker abadiy qayta urinmaydi)."""
    draw, empty = None, False
    try:
        async with tx() as t:
            b0 = await t.one("SELECT btype FROM battles WHERE id=?", bid)
            if not b0:
                return False
            if b0["btype"] == "contest" and not await t.one("SELECT 1 FROM participants WHERE battle_id=? LIMIT 1", bid):
                if not await BE.transition(t, bid, "CANCELLED"):
                    return False
                empty = True
            else:
                if not await BE.finish(t, bid):
                    return False
                if b0["btype"] == "contest":
                    draw = await BE.draw_winner(t, bid)
                    await BE.transition(t, bid, "SETTLED")
                else:
                    await BE.settle(t, bid)          # g'oliblar bir marta yoziladi (SETTLED)
    except BE.BattleError:
        return False
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if empty:
        text = f"❌ <b>{esc(b['title'])}</b> — ishtirokchi bo'lmagani uchun bekor qilindi."
    elif draw:
        w = await one("SELECT nick FROM participants WHERE battle_id=? AND user_id=?", bid, draw["winner_id"])
        n = (await one("SELECT COUNT(*) c FROM participants WHERE battle_id=?", bid))["c"]
        text = BV.contest_result(b, draw, w["nick"] if w else str(draw["winner_id"]), n)
    else:
        rows = await leaderboard(bid)
        text = (f"🏁 <b>{esc(b['title'])}</b> — batl yakunlandi!\n\n" + await BV.results_text(RO, b, None, fmt_ts))
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
        await bot.send_message(b["owner_id"], f"✅ «{esc(b['title'])}» yakunlandi, natijalar kanalga yuborildi.")
    except Exception:
        pass
    return True


async def ticker(bot: Bot):
    while True:
        await asyncio.sleep(20)
        try:
            rows = await q("SELECT id FROM battles WHERE state='ACTIVE' AND end_ts>0 AND end_ts<=?", int(time.time()))
            for r in rows:
                await finish_battle(bot, r["id"])
        except Exception:
            log.exception("ticker xatosi")


def fmt_meta(meta: str) -> str:
    """Buyurtma ma'lumoti: 'ID|server' -> chiroyli ko'rinish."""
    if not meta:
        return ""
    pid, _, srv = meta.partition("|")
    return f"ID: <code>{esc(pid)}</code>" + (f" · Server: <code>{esc(srv)}</code>" if srv else "")


##############################################################################
# app/products/catalog.py
##############################################################################
@dataclass
class Product:
    ptype: str
    pid: int
    title: str
    stars: int
    uzs: int
    ton_nano: int
    meta: str = ""
    cost: int | None = None      # botga necha Stars tushishi (aniq bo'lsa), aks holda None

    @property
    def mode(self) -> str:
        if self.ptype in AUTO_TYPES:
            return "auto"
        if self.ptype in ("topup", "vip", "battle_credit", "donation"):
            return "internal"
        return "manual"


def item_cost(kind: str, payload: str, title: str = ""):
    """Gift/Premium botga necha Stars (tannarx) bo'lishini qaytaradi; aniq bilib bo'lmasa None."""
    try:
        if kind == "premium":
            return PREMIUM_STARS.get(int(payload))
        if kind == "gift":
            if "|" in payload:                    # buyurtma snapshoti: «gift_id|cost»
                return int(payload.split("|", 1)[1])
            m = re.search(r"\((\d+)⭐\)", title)
            return int(m.group(1)) if m else None
    except (ValueError, TypeError):
        pass
    return None


async def resolve(ptype: str, pid: int = 0, *, stars: int | None = None, meta: str = "") -> Product | None:
    """Mahsulot va NARX faqat serverdagi ma'lumotlardan hisoblanadi. Mijozdan faqat mahsulot identifikatori qabul qilinadi."""
    rate_uzs, rate_ton = max(1, await SI("uzs_rate", 230)), max(1, await SI("ton_rate", 200))

    def mk(title, st, uzs=None, m="", cost=None, ptype_=ptype, pid_=pid):
        return Product(ptype_, pid_, title, st, uzs if uzs is not None else calc_uzs(st, rate_uzs),
                       calc_ton_nano(st, rate_ton), m, cost)

    if ptype in ("premium", "gift", "nft", "stars"):
        it = await one("SELECT * FROM items WHERE id=? AND active=1 AND kind=?", pid, ptype)
        if not it or it["price_stars"] <= 0:
            return None
        if ptype == "premium":
            try:
                if int(it["payload"]) not in PREMIUM_STARS:
                    return None
            except ValueError:
                return None
        cost = item_cost(ptype, it["payload"], it["title"])
        snap = it["payload"]
        if ptype == "gift":
            if not snap:
                return None
            snap = f"{it['payload']}|{cost if cost is not None else 0}"
        return mk(it["title"], it["price_stars"], m=snap, cost=cost)
    if ptype == "game":
        p = await one("SELECT p.*, g.name gname, g.emoji, g.server FROM packs p JOIN games g ON g.id=p.game_id "
                      "WHERE p.id=? AND p.active=1 AND p.price_uzs>0 AND g.active=1", pid)
        if not p:
            return None
        st = max(1, math.ceil(p["price_uzs"] / rate_uzs))
        return mk(f"{p['emoji']} {p['gname']} — {p['title']}", st, uzs=p["price_uzs"], m=meta)
    if ptype in ("topup", "donation"):
        lo, hi = (MIN_TOPUP, MAX_TOPUP) if ptype == "topup" else (MIN_DONATION, MAX_DONATION)
        if stars is None or not lo <= int(stars) <= hi:
            return None
        title = f"💰 Balansni to'ldirish — {int(stars)}⭐" if ptype == "topup" else f"💝 Donat — {int(stars)}⭐"
        return mk(title, int(stars))
    if ptype == "battle_star":
        if stars is None or not MIN_TOPUP <= int(stars) <= MAX_TOPUP:
            return None
        try:
            bid_s, uid_s = meta.split("|")
            bid, tuid = int(bid_s), int(uid_s)
        except ValueError:
            return None
        row = await one("SELECT b.state, b.btype, COALESCE(s.star_score,1) sc, p.nick FROM battles b "
                        "JOIN participants p ON p.battle_id=b.id AND p.user_id=? LEFT JOIN battle_settings s ON s.battle_id=b.id "
                        "WHERE b.id=?", tuid, bid)
        if not row or row["state"] != "ACTIVE" or row["btype"] == "contest" or row["sc"] <= 0:
            return None
        return mk(f"⭐ Battle ovozi — {row['nick']}", int(stars), m=meta)
    if ptype == "vip":
        price = await SI("vip_price", 500)
        return mk("👑 VIP obuna", price) if price > 0 else None
    if ptype == "battle_credit":
        fee = await SI("battle_fee", 25)
        return mk("⚔️ Batl yaratish krediti", fee) if fee > 0 else None
    return None


async def product_available(o) -> bool:
    """To'lovdan oldin mahsulot hali sotuvdami (o'chirilmagan/yashirilmagan)."""
    t, pid = o["product_type"], o["product_id"] or 0
    if t in ("premium", "gift", "nft", "stars"):
        return bool(await one("SELECT 1 FROM items WHERE id=? AND active=1", pid))
    if t == "game":
        return bool(await one("SELECT 1 FROM packs p JOIN games g ON g.id=p.game_id WHERE p.id=? AND p.active=1 AND g.active=1", pid))
    return True


async def allowed_methods(ptype: str) -> list[str]:
    """Mahsulot turi va sozlamalarga qarab ruxsat etilgan to'lov usullari (tartib bilan)."""
    out = []
    if await S("card_number"):
        out.append("card")
    if await S("provider_token"):
        out.append("uzs")
    if ptype != "topup":
        out.append("wallet")
    if ptype != "stars":
        out.append("stars")
    if await S("ton_address"):
        out.append("ton")
    return out


##############################################################################
# app/economy/ledger.py
##############################################################################
async def post(t, *, order_id=None, payment_id=None, user_id=None, type_, account, amount, currency,
               provider="", provider_id="", idem, meta="") -> bool:
    """Ledger'ga o'zgarmas yozuv qo'shadi. idem_key takrorlansa hech narsa qilmaydi (False).
    «wallet» hisobi bo'yicha users.balance — ledger'dan hosil bo'ladigan kesh, shu tranzaksiyada yangilanadi."""
    _, rc = await t.execute(
        "INSERT OR IGNORE INTO ledger_entries(bot_id,order_id,payment_id,user_id,type,account,amount,currency,provider,provider_id,"
        "status,idem_key,created_at,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,'completed',?,?,?)",
        BOT_ID, order_id, payment_id, user_id, type_, account, amount, currency, provider, provider_id, idem, now(), meta)
    if rc and account == "wallet" and user_id is not None:
        await t.execute("UPDATE users SET balance=balance+? WHERE id=?", amount, user_id)
    return bool(rc)


async def wallet_balance(t, uid: int) -> int:
    r = await t.one("SELECT COALESCE(SUM(amount),0) s FROM ledger_entries WHERE bot_id=? AND user_id=? AND account='wallet'",
                    BOT_ID, uid)
    return int(r["s"])


async def reconcile(fix: bool = False):
    """users.balance (kesh) va ledger yig'indisini solishtiradi. Nomuvofiqliklar ro'yxatini qaytaradi."""
    bad = []
    async with tx() as t:
        for u in await t.query("SELECT id, balance FROM users"):
            s = await wallet_balance(t, u["id"])
            if s != (u["balance"] or 0):
                bad.append((u["id"], s, u["balance"] or 0))
                if fix:
                    await t.execute("UPDATE users SET balance=? WHERE id=?", s, u["id"])
    return bad


async def order_entries(oid: int):
    return await q("SELECT * FROM ledger_entries WHERE bot_id=? AND order_id=? ORDER BY id", BOT_ID, oid)


# ------------------------------------------------------------------ hisobotlar

async def finance_report(since: int) -> dict:
    """Tushum / tannarx / foyda. Faqat ledger va haqiqiy buyurtmalardan; tannarx noma'lum bo'lsa uydirilmaydi."""
    b = BOT_ID
    rev = {r["currency"]: r["s"] for r in await q(
        "SELECT currency, COALESCE(SUM(amount),0) s FROM ledger_entries WHERE bot_id=? AND account='revenue' AND created_at>=? GROUP BY currency", b, since)}
    don = {r["currency"]: r["s"] for r in await q(
        "SELECT currency, COALESCE(SUM(amount),0) s FROM ledger_entries WHERE bot_id=? AND account='donation' AND created_at>=? GROUP BY currency", b, since)}
    cash = {r["currency"]: r["s"] for r in await q(
        "SELECT currency, COALESCE(SUM(amount),0) s FROM ledger_entries WHERE bot_id=? AND account='cash' AND created_at>=? GROUP BY currency", b, since)}
    ph = ",".join("?" * len(REVENUE_TYPES))
    eq = await one(f"SELECT COUNT(*) n, COALESCE(SUM(stars),0) s FROM orders WHERE bot_id=? AND payment_status='paid' "
                   f"AND product_type IN ({ph}) AND paid_at>=?", b, *REVENUE_TYPES, since)
    known = await one("SELECT COUNT(*) n, COALESCE(SUM(o.stars),0) rev, COALESCE(SUM(c.amount),0) cost FROM ledger_entries c "
                      "JOIN orders o ON o.id=c.order_id WHERE c.bot_id=? AND c.type='COST' AND c.created_at>=? "
                      "AND o.payment_status='paid'", b, since)
    ph2 = ",".join("?" * len(SALE_TYPES))
    unk = await one(f"SELECT COUNT(*) n, COALESCE(SUM(stars),0) s FROM orders o WHERE bot_id=? AND payment_status='paid' "
                    f"AND product_type IN ({ph2}) AND paid_at>=? "
                    "AND NOT EXISTS (SELECT 1 FROM ledger_entries c WHERE c.order_id=o.id AND c.type='COST')", b, *SALE_TYPES, since)
    refunds = await one("SELECT COUNT(*) n FROM refunds WHERE bot_id=? AND status='completed' AND completed_at>=?", b, since)
    dcount = await one("SELECT COUNT(*) n FROM donations WHERE bot_id=? AND status='confirmed' AND created_at>=?", b, since)
    return {"revenue": rev, "donations": don, "cash": cash, "orders_n": eq["n"], "orders_stars": eq["s"],
            "known_n": known["n"], "known_rev": known["rev"], "known_cost": known["cost"],
            "unknown_n": unk["n"], "unknown_stars": unk["s"], "refunds": refunds["n"], "donation_n": dcount["n"]}


##############################################################################
# app/orders/service.py
##############################################################################
MAX_PENDING_PER_USER = 30


async def create_order(uid: int, ptype: str, pid: int = 0, *, stars: int | None = None, meta: str = ""):
    """Yangi buyurtma. Narx serverda hisoblanadi; bir xil kutilayotgan buyurtma qayta ishlatiladi (ikki marta bosish).
    Qaytaradi: buyurtma qatori yoki None (mahsulot yo'q / limit)."""
    p = await resolve(ptype, pid, stars=stars, meta=meta)
    if not p:
        return None
    ts = now()
    async with tx() as t:
        meta_final = meta if ptype == "game" else p.meta
        dup = await t.one(
            "SELECT * FROM orders WHERE bot_id=? AND user_id=? AND product_type=? AND product_id=? AND meta=? AND stars=? "
            "AND uzs=? AND payment_status='pending' AND expires_at>? ORDER BY id DESC LIMIT 1",
            BOT_ID, uid, ptype, pid, meta_final, p.stars, p.uzs, ts)
        if dup:
            return dup
        n = await t.one("SELECT COUNT(*) c FROM orders WHERE bot_id=? AND user_id=? AND payment_status='pending' AND expires_at>?",
                        BOT_ID, uid, ts)
        if n["c"] >= MAX_PENDING_PER_USER:
            return None
        for _ in range(8):
            code = "BH-" + secrets.token_hex(3).upper()
            try:
                oid, _ = await t.execute(
                    "INSERT INTO orders(bot_id,user_id,product_type,product_id,product_title,stars,ton,ton_nano,uzs,meta,order_code,"
                    "created_at,expires_at,payment_status,delivery_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending','none')",
                    BOT_ID, uid, ptype, pid, p.title, p.stars, p.ton_nano / 1e9, p.ton_nano, p.uzs, meta_final, code,
                    ts, ts + ORDER_TTL)
                break
            except sqlite3.IntegrityError:
                continue
        else:
            raise RuntimeError("order_code yaratib bo'lmadi")
        await audit("order_created", user_id=uid, order_id=oid, t=t)
        return await t.one("SELECT * FROM orders WHERE id=?", oid)


async def get_order(oid: int, uid: int | None = None):
    o = await one("SELECT * FROM orders WHERE id=? AND bot_id=?", oid, BOT_ID)
    if o and uid is not None and o["user_id"] != uid:
        return None
    return o


async def get_by_code(code: str):
    return await one("SELECT * FROM orders WHERE bot_id=? AND order_code=?", BOT_ID, code)


async def user_orders(uid: int, limit: int = 10, offset: int = 0):
    return await q("SELECT * FROM orders WHERE bot_id=? AND user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                   BOT_ID, uid, limit, offset)


async def cancel_order(oid: int, uid: int) -> bool:
    async with tx() as t:
        _, rc = await t.execute("UPDATE orders SET payment_status='cancelled' WHERE id=? AND user_id=? AND bot_id=? "
                                "AND payment_status='pending'", oid, uid, BOT_ID)
        if rc:
            await t.execute("UPDATE payments SET status='cancelled' WHERE order_id=? AND status IN ('created','receipt_sent')", oid)
            await audit("order_cancelled", user_id=uid, order_id=oid, t=t)
        return bool(rc)


async def expire_due() -> int:
    """Muddati o'tgan kutilayotgan buyurtmalarni «expired» qiladi (kech kelgan to'lov baribir qabul qilinadi)."""
    async with tx() as t:
        rows = await t.query("SELECT id FROM orders WHERE bot_id=? AND payment_status='pending' AND expires_at<=?", BOT_ID, now())
        for r in rows:
            await t.execute("UPDATE orders SET payment_status='expired' WHERE id=? AND payment_status='pending'", r["id"])
            await t.execute("UPDATE payments SET status='expired' WHERE order_id=? AND status IN ('created','receipt_sent')", r["id"])
        return len(rows)


##############################################################################
# app/subscriptions/service.py
##############################################################################
DAY = 86400


async def activate_vip(t, uid: int, oid: int, days: int) -> int:
    """VIP'ni to'lov tranzaksiyasi ichida faollashtiradi/uzaytiradi. Qaytaradi: yangi tugash vaqti."""
    ts = now()
    u = await t.one("SELECT vip_until FROM users WHERE id=?", uid)
    start = max(ts, (u["vip_until"] or 0) if u else 0)
    until = start + days * DAY
    await t.execute("INSERT OR IGNORE INTO subscriptions(bot_id,user_id,plan,order_id,status,starts_at,expires_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", BOT_ID, uid, "vip", oid, "active", start, until, ts)
    await t.execute("UPDATE users SET vip_until=? WHERE id=?", until, uid)
    return until


async def subs_maintenance(bot) -> dict:
    """Muddati tugayotgan obunalarga eslatma (3 kun oldin, bir marta) va tugaganlarini «expired» qilish.
    Telegram'da rasmiy avtomatik yechish yo'q, shuning uchun uzaytirish — foydalanuvchi bosadigan yangi buyurtma."""
    ts = now()
    reminded = expired = 0
    for s in await q("SELECT * FROM subscriptions WHERE bot_id=? AND status='active' AND reminded=0 AND expires_at<=? AND expires_at>?",
                     BOT_ID, ts + 3 * DAY, ts):
        async with tx() as t:
            _, rc = await t.execute("UPDATE subscriptions SET reminded=1 WHERE id=? AND reminded=0", s["id"])
        if rc:
            await send(bot, s["user_id"], "👑 <b>VIP obunangiz tez orada tugaydi.</b>\nUzluksiz foydalanish uchun uzaytiring 👇",
                       K([B("👑 VIP ni uzaytirish", "vip:buy")]))
            reminded += 1
    async with tx() as t:
        _, expired = await t.execute("UPDATE subscriptions SET status='expired' WHERE bot_id=? AND status='active' AND expires_at<=?",
                                     BOT_ID, ts)
    if expired:
        log.info("tugagan obunalar: %s", expired)
    return {"reminded": reminded, "expired": expired}


##############################################################################
# app/core/notify.py
##############################################################################
async def send(bot, uid: int, text: str, kb=None) -> bool:
    """Xabar yuboradi; muvaffaqiyatsiz bo'lsa False (asosiy jarayonni to'xtatmaydi)."""
    try:
        await bot.send_message(uid, text, reply_markup=kb)
        return True
    except Exception as e:
        log.warning("xabar yuborilmadi uid=%s: %s", uid, e)
        return False


async def owner_alert(bot, text: str, kb=None, order_id: int | None = None):
    if order_id and kb is None:
        kb = K([B("🧾 Buyurtma", f"o:od:{order_id}")])
    return await send(bot, OWNER_ID, text, kb)


async def who(uid: int) -> str:
    u = await one("SELECT name, username FROM users WHERE id=?", uid)
    s = f'<a href="tg://user?id={uid}">{esc((u["name"] if u else None) or str(uid))}</a>'
    if u and u["username"]:
        s += f" (@{u['username']})"
    return s


def receipt_text(o) -> str:
    """Foydalanuvchi uchun chek / buyurtma ma'lumoti."""
    lines = [f"🧾 <b>Buyurtma {o['order_code']}</b>", esc(o["product_title"] or o["product_type"])]
    if o["product_type"] == "game" and o["meta"]:
        lines.append(fmt_meta(o["meta"]))
    if o["payment_status"] == "paid" and o["currency"]:
        lines.append(f"💳 To'langan: <b>{fmt_amount(o['amount'], o['currency'])}</b>")
        lines.append(f"Usul: {METHODS.get(o['payment_method'], ('', '', o['payment_method'] or '-'))[2]}")
        lines.append(f"Sana: {fmt_ts(o['paid_at'])}")
    else:
        lines.append(f"💳 Narx: <b>{fmt_uzs(o['uzs'])}</b> / {o['stars']}⭐")
        lines.append(f"Sana: {fmt_ts(o['created_at'])}")
    lines.append(f"Holat: {order_state(o)}")
    if o["delivered_at"]:
        lines.append(f"Yetkazilgan: {fmt_ts(o['delivered_at'])}")
    return "\n".join(lines)


def order_kb(o):
    return K([B("🧾 Buyurtmam", f"ord:{o['id']}")], [HOME])


async def _claim(oid: int, bits: int, guard: int) -> bool:
    """notified bitlarini bir martalik «band qilish»: faqat guard biti hali yo'q bo'lsa muvaffaqiyatli."""
    _, rc = await run("UPDATE orders SET notified=notified|? WHERE id=? AND (notified & ?)=0", bits, oid, guard)
    return bool(rc)


async def _unclaim(oid: int, bits: int):
    await run("UPDATE orders SET notified=notified & ~? WHERE id=?", bits, oid)


async def _delivered_text(o) -> str:
    t, head = o["product_type"], "✅ "
    u = await one("SELECT balance, vip_until FROM users WHERE id=?", o["user_id"])
    if t == "topup":
        body = f"Balans <b>{o['stars']}⭐</b> ga to'ldirildi. Joriy balans: <b>{u['balance']}⭐</b>"
    elif t == "vip":
        body = f"👑 VIP faollashtirildi! Amal qilish muddati: <b>{fmt_ts(u['vip_until'])}</b>"
    elif t == "battle_credit":
        body = "Batl yaratish krediti qo'shildi."
    elif t == "donation":
        head, body = "💝 ", "Donatingiz uchun katta rahmat! Qo'llab-quvvatlashingiz biz uchun muhim."
    elif t == "battle_star":
        head, body = "⭐ ", "Ovozingiz hisobga olindi — ball qo'shildi!"
    elif t in ("premium", "gift"):
        head, body = "🎉 ", f"<b>{esc(o['product_title'])}</b> yetkazib berildi! Telegram'ni tekshiring."
    else:
        head, body = "🎉 ", f"<b>{esc(o['product_title'])}</b> — buyurtmangiz bajarildi."
    return f"{head}{body}\n\n{receipt_text(o)}"


async def notify_order(bot, oid: int):
    """Foydalanuvchiga holat xabarini bir martadan yuboradi (notified bitlari bilan). Xato bo'lsa keyin qayta uriniladi."""
    o = await one("SELECT * FROM orders WHERE id=? AND bot_id=?", oid, BOT_ID)
    if not o or o["payment_status"] != "paid":
        return
    done = o["delivery_status"] == "delivered" or o["product_type"] == "donation"
    if done:
        if o["notified"] & 2 == 0 and await _claim(oid, 3, 2):
            if not await send(bot, o["user_id"], await _delivered_text(o), order_kb(o)):
                await _unclaim(oid, 3 & ~o["notified"])
        return
    if o["notified"] & 1 == 0 and o["delivery_status"] in ("pending", "processing", "retry", "waiting_funds", "manual_pending",
                                                          "uncertain", "failed"):
        if await _claim(oid, 1, 1):
            how = "Hozir yetkazilmoqda, tayyor bo'lganda xabar beramiz." if o["delivery_status"] in ("pending", "processing") \
                else "Admin tez orada yetkazadi, tayyor bo'lganda xabar beramiz."
            if not await send(bot, o["user_id"], f"✅ <b>To'lov qabul qilindi!</b>\n\n{receipt_text(o)}\n\n{how}", order_kb(o)):
                await _unclaim(oid, 1)


async def owner_manual_notice(bot, oid: int):
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    if not o:
        return
    t = o["product_type"]
    how, rows = "", []
    if t == "game":
        how = f"\n{fmt_meta(o['meta'])}\nO'yinga donatni qo'lda kiriting, so'ng «Yetkazildi» ni bosing."
    elif t == "stars":
        how = (f"\n\n⭐ <b>{esc(o['meta'])} Stars</b> ni foydalanuvchiga yuboring. Bot API orqali Stars yuborib bo'lmaydi — "
               "Fragment orqali qo'lda yuboring.")
        rows.append([B("🌐 Fragment: Stars", url="https://fragment.com/stars/buy")])
    elif t == "nft":
        how = ("\n\n🖼 NFT ni qo'lda o'tkazing. Telegram NFT'ni bot orqali yuborish uchun rasmiy API yo'q, "
               "shuning uchun bu buyurtma avtomatik bajarilmaydi.")
    rows.append([B("✅ Yetkazildi", f"o:md:{oid}"), B("↩️ Qaytarish", f"o:rf:{oid}")])
    rows.append([B("🧾 Buyurtma", f"o:od:{oid}")])
    await send(bot, OWNER_ID, f"🧾 <b>Qo'lda yetkazish kerak</b> — {o['order_code']}\n{esc(o['product_title'])}\n"
                              f"Xaridor: {await who(o['user_id'])}\nTo'langan: {fmt_amount(o['amount'], o['currency'])}{how}", K(*rows))


##############################################################################
# app/deliveries/stars.py
##############################################################################
async def bot_stars(bot):
    """Botning Stars balansi (aniqlab bo'lmasa None)."""
    try:
        return (await bot.get_my_star_balance()).amount
    except Exception:
        return None


def order_cost(o):
    """Buyurtma botga necha Stars turishi (faqat aniq bo'lsa)."""
    try:
        if o["product_type"] == "premium":
            return PREMIUM_STARS.get(int(o["meta"]))
        if o["product_type"] == "gift" and "|" in (o["meta"] or ""):
            c = int(o["meta"].split("|", 1)[1])
            return c or None
    except (ValueError, TypeError):
        pass
    return None


async def pending_need() -> int:
    rows = await q("SELECT o.* FROM deliveries d JOIN orders o ON o.id=d.order_id WHERE d.bot_id=? AND d.mode='auto' "
                   "AND d.status IN ('pending','retry','waiting_funds','processing') AND o.payment_status='paid'", BOT_ID)
    return sum(order_cost(o) or 0 for o in rows)


def stars_level(bal, need, warn, crit) -> str:
    if bal is None:
        return "⚪"
    if bal < crit or bal < need:
        return "🔴"
    if bal < warn:
        return "🟡"
    return "🟢"


async def stars_status(bot) -> dict:
    bal, need = await bot_stars(bot), await pending_need()
    warn, crit = await SI("stars_warn", 1500), await SI("stars_crit", 500)
    return {"balance": bal, "need": need, "warn": warn, "crit": crit, "level": stars_level(bal, need, warn, crit)}


async def stars_alert(bot, force: bool = False):
    """Holat yomonlashganda (yoki 🔴 6 soatdan ortiq davom etsa) owner'ga bir marta xabar yuboradi."""
    st = await stars_status(bot)
    prev, last = await S("stars_state", "🟢"), await SI("stars_alert_ts", 0)
    cur = st["level"]
    if cur == "⚪":
        return st
    worse = {"🟢": 0, "🟡": 1, "🔴": 2}
    send_it = force or worse[cur] > worse.get(prev, 0) or (cur == "🔴" and now() - last > 6 * 3600)
    if cur == "🟢" and prev != "🟢":
        await owner_alert(bot, f"🟢 Botning Stars balansi tiklandi: <b>{st['balance']}⭐</b>")
    elif send_it and cur != "🟢":
        await owner_alert(bot, f"{cur} <b>Botning Stars balansi: {st['balance']}⭐</b>\nKutilayotgan yetkazishlar uchun kerak: "
                               f"<b>{st['need']}⭐</b>\nBalans yetmasa Premium/giftlar navbatda kutadi.",
                          K([B("🌟 Bot Stars", "o:bs")]))
        await set_s("stars_alert_ts", now())
    await set_s("stars_state", cur)
    return st


##############################################################################
# app/deliveries/service.py
##############################################################################
_FUNDS_RE = re.compile(r"not enough|balance_too_low|insufficient|no enough|stars? balance", re.I)
_tasks: set = set()


def classify(e: Exception):
    """Telegram xatosini turkumlaydi:
       retry     — so'rov bajarilmagani aniq (flood, 5xx): backoff bilan qayta uriniladi
       funds     — botda Stars yetmaydi: navbatda kutadi
       definite  — qayta urinish foydasiz (foydalanuvchi cheklagan, Premium bor va h.k.): owner hal qiladi
       uncertain — yuborilgan-yuborilmagani NOMA'LUM (timeout/tarmoq): ko'r-ko'rona qayta YUBORILMAYDI"""
    msg = str(e)[:250]
    if isinstance(e, TelegramRetryAfter):
        return "retry", msg, int(getattr(e, "retry_after", 0) or 0)
    if isinstance(e, (TelegramBadRequest, TelegramForbiddenError)):
        return ("funds" if _FUNDS_RE.search(msg) else "definite"), msg, 0
    if isinstance(e, TelegramServerError):
        return "retry", msg, 0
    return "uncertain", msg or type(e).__name__, 0


async def spec(o):
    """(tur, payload, tannarx) — avval buyurtma snapshoti, bo'lmasa (eski buyurtmalar) mahsulot jadvali."""
    kind, meta = o["product_type"], o["meta"] or ""
    if kind == "premium" and meta.isdigit():
        return kind, meta, PREMIUM_STARS.get(int(meta))
    if kind == "gift" and meta:
        return kind, meta.split("|")[0], order_cost(o)
    it = await one("SELECT * FROM items WHERE id=?", o["product_id"]) if o["product_id"] else None
    if it and it["kind"] == kind:
        return kind, it["payload"], item_cost(kind, it["payload"], it["title"])
    return kind, "", None


async def _api_send(bot, o, kind, payload):
    if kind == "premium":
        m = int(payload)
        await bot.gift_premium_subscription(user_id=o["user_id"], month_count=m, star_count=PREMIUM_STARS[m])
    elif kind == "gift":
        await bot.send_gift(gift_id=payload, user_id=o["user_id"], text=f"🎁 {await S('brand', '')}"[:120])
    else:
        raise ValueError("avtomatik yetkazilmaydigan tur")


def _kb(oid, uncertain=False):
    rows = [[B("✅ Yetkazildi deb belgilash", f"o:md:{oid}")]]
    rows.append([B("🔁 Qayta urinish" + (" (dublikat xavfi!)" if uncertain else ""), f"o:rt:{oid}")])
    rows.append([B("↩️ Pulni qaytarish", f"o:rf:{oid}"), B("🧾 Buyurtma", f"o:od:{oid}")])
    return K(*rows)


async def _set(oid, status, *, error="", next_retry=None, inc_funds=False, dec_attempt=False):
    """processing -> yangi holat (faqat «processing» bo'lsa)."""
    ts = now()
    async with tx() as t:
        _, rc = await t.execute(
            "UPDATE deliveries SET status=?, last_error=?, next_retry_at=?, locked_at=NULL, updated_at=?, "
            "funds_checks=funds_checks+?, attempts=attempts-? WHERE order_id=? AND status='processing'",
            status, error[:300], next_retry, ts, 1 if inc_funds else 0, 1 if dec_attempt else 0, oid)
        if rc:
            await t.execute("UPDATE orders SET delivery_status=?, last_error=? WHERE id=?", status, error[:300], oid)
        return bool(rc)


async def _user_once(bot, oid, text):
    """Foydalanuvchiga muammo haqida bir marta xabar (user_notified bilan)."""
    async with tx() as t:
        _, rc = await t.execute("UPDATE deliveries SET user_notified=1 WHERE order_id=? AND user_notified=0", oid)
    if rc:
        o = await one("SELECT user_id FROM orders WHERE id=?", oid)
        if o:
            await send(bot, o["user_id"], text, K([B("🧾 Buyurtmam", f"ord:{oid}")]))


async def _alert(bot, oid, head, uncertain=False):
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    d = await one("SELECT * FROM deliveries WHERE order_id=?", oid)
    await owner_alert(bot, f"{head}\nBuyurtma <b>{o['order_code']}</b> — {o['product_title']}\nXaridor: {await who(o['user_id'])}\n"
                           f"Urinishlar: {d['attempts']} · Xato: <code>{(d['last_error'] or '-')[:200]}</code>", _kb(oid, uncertain))


def kick(bot, oid: int):
    """To'lovdan so'ng yetkazishni darhol fon vazifasida boshlaydi (worker ham baribir ushlaydi)."""
    t = asyncio.create_task(_safe_process(bot, oid))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _safe_process(bot, oid):
    try:
        await process(bot, oid)
    except Exception:
        log.exception("yetkazish xatosi order=%s", oid)


async def process(bot, oid: int, force: bool = False) -> str:
    """Bitta avtomatik yetkazishni bajaradi. Qulf: deliveries.status='processing' (faqat bitta jarayon o'ta oladi)."""
    ts = now()
    async with tx() as t:
        o = await t.one("SELECT * FROM orders WHERE id=? AND bot_id=?", oid, BOT_ID)
        d = await t.one("SELECT * FROM deliveries WHERE order_id=?", oid)
        if not o or not d or o["payment_status"] != "paid" or d["mode"] != "auto":
            return "skip"
        if d["status"] not in ("pending", "retry", "waiting_funds"):
            return "skip"
        if not force and d["next_retry_at"] and d["next_retry_at"] > ts:
            return "skip"
        _, rc = await t.execute("UPDATE deliveries SET status='processing', locked_at=?, attempts=attempts+1, updated_at=?, "
                                "next_retry_at=NULL WHERE order_id=? AND status IN ('pending','retry','waiting_funds')", ts, ts, oid)
        if not rc:
            return "skip"
        await t.execute("UPDATE orders SET delivery_status='processing' WHERE id=?", oid)
        attempts = d["attempts"] + 1
    kind, payload, cost = await spec(o)
    if kind not in AUTO_TYPES or not payload:
        await _set(oid, "failed", error="mahsulot ma'lumoti topilmadi (o'chirilgan?)")
        await _alert(bot, oid, "🚨 <b>Yetkazib bo'lmadi</b>")
        return "failed"
    bal = await bot_stars(bot)
    if cost and bal is not None and bal < cost:
        return await _funds_wait(bot, oid, d, f"botda {bal}⭐, kerak {cost}⭐")
    try:
        await _api_send(bot, o, kind, payload)
    except Exception as e:
        k, msg, wait = classify(e)
        log.warning("yetkazish xatosi order=%s tur=%s: %s", oid, k, msg)
        await audit("delivery_error", user_id=o["user_id"], order_id=oid, status=k, error=msg)
        if k == "funds":
            return await _funds_wait(bot, oid, d, msg)
        if k == "retry":
            if attempts >= MAX_DELIVERY_RETRIES:
                await _set(oid, "failed", error=f"maksimal urinish tugadi: {msg}")
                await _alert(bot, oid, "🚨 <b>Yetkazib bo'lmadi</b> (urinishlar tugadi)")
                await _user_once(bot, oid, "⚠️ Buyurtmangizni yetkazishda muammo chiqdi. Admin hal qiladi, pulingiz xavfsiz.")
                return "failed"
            back = RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)]
            await _set(oid, "retry", error=msg, next_retry=now() + max(back, wait))
            return "retry"
        if k == "definite":
            await _set(oid, "failed", error=msg)
            await _alert(bot, oid, "🚨 <b>Yetkazib bo'lmadi</b> (Telegram rad etdi)")
            await _user_once(bot, oid, "⚠️ Buyurtmangizni yetkazishda muammo chiqdi. Admin hal qiladi, pulingiz xavfsiz.")
            return "failed"
        await _set(oid, "uncertain", error=msg)
        await _alert(bot, oid, "❓ <b>Natija noaniq</b> — yuborilgan bo'lishi mumkin. Avval foydalanuvchida borligini tekshiring!", True)
        await _user_once(bot, oid, "🔎 Buyurtmangiz yetkazilishi tekshirilmoqda. Tez orada xabar beramiz.")
        return "uncertain"
    return await _success(bot, o, cost)


async def _funds_wait(bot, oid, d, msg):
    ts = now()
    if ts - d["created_at"] > FUNDS_WAIT_MAX:
        await _set(oid, "failed", error=f"Stars yetmadi (72 soat): {msg}", dec_attempt=True)
        await _alert(bot, oid, "🚨 <b>Yetkazib bo'lmadi</b> — botda Stars yetishmadi")
        await _user_once(bot, oid, "⚠️ Buyurtmangiz kechikmoqda. Admin hal qiladi, pulingiz xavfsiz.")
        return "failed"
    await _set(oid, "waiting_funds", error=msg, next_retry=ts + FUNDS_RECHECK, inc_funds=True, dec_attempt=True)
    if d["funds_checks"] == 0:
        await _user_once(bot, oid, "⏳ Buyurtmangiz navbatga qo'yildi va tez orada yetkaziladi.")
        await stars_alert(bot, force=True)
    return "waiting_funds"


async def _success(bot, o, cost):
    oid, ts = o["id"], now()
    for attempt in (1, 2):
        try:
            async with tx() as t:
                _, rc = await t.execute("UPDATE deliveries SET status='delivered', delivered_at=?, updated_at=?, locked_at=NULL, "
                                        "last_error='' WHERE order_id=? AND status='processing'", ts, ts, oid)
                if not rc:
                    return "skip"
                await t.execute("UPDATE orders SET delivery_status='delivered', delivered_at=?, last_error='' WHERE id=?", ts, oid)
                if cost:
                    await post(t, order_id=oid, user_id=o["user_id"], type_="COST", account="cost", amount=cost, currency="XTR",
                               provider="telegram", provider_id=str(oid), idem=f"cost:{oid}")
                await audit("delivered", user_id=o["user_id"], order_id=oid, t=t)
            break
        except Exception as e:
            ref = error_ref()
            log.error("YUBORILDI, lekin DB yozilmadi ref=%s order=%s: %s", ref, oid, e)
            if attempt == 2:
                await owner_alert(bot, f"🚨 Buyurtma {o['order_code']} Telegram orqali yuborildi, lekin bazaga yozilmadi (ref <code>{ref}</code>). "
                                       "Holat «noaniq» bo'ladi — qayta yubormang, «Yetkazildi» deb belgilang.", _kb(oid, True))
                return "uncertain"
            await asyncio.sleep(1)
    await notify_order(bot, oid)
    await stars_alert(bot)
    return "delivered"


# ------------------------------------------------------------------ owner amallari

async def owner_requeue(bot, oid: int, admin_id: int, confirm_uncertain: bool = False):
    async with tx() as t:
        o = await t.one("SELECT * FROM orders WHERE id=? AND bot_id=?", oid, BOT_ID)
        d = await t.one("SELECT * FROM deliveries WHERE order_id=?", oid)
        if not o or not d or o["payment_status"] != "paid" or d["mode"] != "auto":
            return False, "Buyurtma avtomatik yetkazishga mos emas"
        if d["status"] == "uncertain" and not confirm_uncertain:
            return False, "needs_confirm"
        if d["status"] not in ("failed", "waiting_funds", "retry", "uncertain", "pending"):
            return False, f"Holat: {d['status']} — qayta urinib bo'lmaydi"
        await t.execute("UPDATE deliveries SET status='pending', attempts=0, next_retry_at=NULL, locked_at=NULL, updated_at=? "
                        "WHERE order_id=?", now(), oid)
        await t.execute("UPDATE orders SET delivery_status='pending' WHERE id=?", oid)
        await audit("delivery_requeued", admin_id=admin_id, order_id=oid, status=d["status"], t=t)
    res = await process(bot, oid, force=True)
    return True, res


async def mark_delivered(bot, oid: int, admin_id: int):
    ts = now()
    async with tx() as t:
        o = await t.one("SELECT * FROM orders WHERE id=? AND bot_id=?", oid, BOT_ID)
        d = await t.one("SELECT * FROM deliveries WHERE order_id=?", oid)
        if not o or not d or o["payment_status"] != "paid":
            return False
        _, rc = await t.execute("UPDATE deliveries SET status='delivered', delivered_at=?, updated_at=?, locked_at=NULL "
                                "WHERE order_id=? AND status NOT IN ('delivered','cancelled')", ts, ts, oid)
        if not rc:
            return False
        await t.execute("UPDATE orders SET delivery_status='delivered', delivered_at=? WHERE id=?", ts, oid)
        await audit("delivered_manually", user_id=o["user_id"], admin_id=admin_id, order_id=oid, t=t)
    await notify_order(bot, oid)
    return True


# ------------------------------------------------------------------ worker va tiklash

async def sweep_stale(bot, startup: bool = False) -> int:
    """«processing»da qotib qolgan yetkazishlar: yuborilgan-yuborilmagani noma'lum -> «uncertain» (qayta yuborilmaydi)."""
    cutoff = now() + 1 if startup else now() - STALE_PROCESSING
    rows = await q("SELECT order_id FROM deliveries WHERE bot_id=? AND status='processing' AND locked_at<?", BOT_ID, cutoff)
    n = 0
    for r in rows:
        async with tx() as t:
            _, rc = await t.execute("UPDATE deliveries SET status='uncertain', last_error='qayta ishga tushish/uzilish: natija noaniq', "
                                    "locked_at=NULL, updated_at=? WHERE order_id=? AND status='processing'", now(), r["order_id"])
            if rc:
                await t.execute("UPDATE orders SET delivery_status='uncertain' WHERE id=?", r["order_id"])
        if rc:
            n += 1
            await _alert(bot, r["order_id"], "❓ <b>Yetkazish uzilib qoldi</b> — natija noaniq. Avval foydalanuvchida borligini tekshiring!", True)
    return n


async def run_due(bot) -> int:
    rows = await q("SELECT order_id FROM deliveries WHERE bot_id=? AND mode='auto' AND status IN ('pending','retry','waiting_funds') "
                   "AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY id LIMIT 20", BOT_ID, now())
    for r in rows:
        await _safe_process(bot, r["order_id"])
    return len(rows)


async def delivery_loop(bot):
    while True:
        try:
            await run_due(bot)
        except Exception:
            log.exception("delivery_loop xatosi")
        await asyncio.sleep(15)


##############################################################################
# app/payments/service.py
##############################################################################
ACCEPT = ("pending", "expired", "review")


@dataclass
class Result:
    status: str                  # confirmed | duplicate | excess | review | rejected
    order: object = None
    payment_id: int | None = None
    reason: str = ""


# ------------------------------------------------------------------ to'lovni boshlash

async def start_payment(o, method: str):
    """To'lov urinishini yozib qo'yadi (status=created). Qaytaradi: (payment_row, xato_matni)."""
    if method not in METHODS:
        return None, "Noma'lum to'lov usuli"
    if method not in await allowed_methods(o["product_type"]):
        return None, "Bu to'lov usuli hozir mavjud emas"
    provider, currency, _ = METHODS[method]
    if not await product_available(o):
        return None, "Mahsulot hozircha sotuvda yo'q"
    async with tx() as t:
        cur = await t.one("SELECT * FROM orders WHERE id=? AND bot_id=?", o["id"], BOT_ID)
        if not cur or cur["payment_status"] != "pending" or cur["expires_at"] <= now():
            return None, "Buyurtma muddati tugagan yoki allaqachon to'langan"
        amount = expected_amount(cur, method)
        p = await t.one("SELECT * FROM payments WHERE order_id=? AND method=? AND status IN ('created','receipt_sent') "
                        "ORDER BY id DESC LIMIT 1", o["id"], method)
        if p:
            pid = p["id"]
        else:
            pid, _ = await t.execute(
                "INSERT INTO payments(bot_id,order_id,user_id,method,provider,amount,currency,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,'created',?)", BOT_ID, o["id"], cur["user_id"], method, provider, amount, currency, now())
            await pay_event("payment_started", order_id=o["id"], payment_id=pid, detail=method, t=t)
        await t.execute("UPDATE orders SET payment_method=?, payment_provider=? WHERE id=? AND payment_status='pending'",
                        method, provider, o["id"])
        return await t.one("SELECT * FROM payments WHERE id=?", pid), ""


# ------------------------------------------------------------------ pre_checkout

async def validate_precheckout(user_id: int, payload: str, currency: str, total: int):
    """Telegram pre_checkout_query uchun qat'iy tekshiruv. Qaytaradi: (ok, foydalanuvchiga xato matni)."""
    if not payload.startswith("o:") or not payload[2:].isdigit():
        return False, "Noma'lum to'lov"
    o = await get_order(int(payload[2:]))
    if not o or o["user_id"] != user_id:
        return False, "Buyurtma topilmadi"
    if o["payment_status"] != "pending":
        return False, "Buyurtma allaqachon to'langan yoki bekor qilingan"
    if o["expires_at"] and o["expires_at"] <= now():
        return False, "Buyurtma muddati tugagan. Iltimos, yangisini yarating"
    if currency == "XTR":
        method, exp = "stars", int(o["stars"])
    elif currency == "UZS":
        method, exp = "uzs", int(o["uzs"]) * 100
    else:
        return False, "Valyuta qo'llab-quvvatlanmaydi"
    if method not in await allowed_methods(o["product_type"]):
        return False, "Bu to'lov usuli mavjud emas"
    if total != exp:
        await audit("precheckout_amount_mismatch", user_id=user_id, order_id=o["id"], status="rejected",
                    error=f"{currency} got={total} exp={exp}")
        return False, "To'lov summasi buyurtmaga mos kelmadi"
    if not await product_available(o):
        return False, "Mahsulot hozircha sotuvda yo'q"
    if await one("SELECT 1 FROM payments WHERE order_id=? AND status='confirmed'", o["id"]):
        return False, "Buyurtma allaqachon to'langan"
    return True, ""


# ------------------------------------------------------------------ to'lovni tasdiqlash (yagona nuqta)

async def _insert_payment(t, o, method, provider, ppid, amount, currency, status, ts, error=""):
    row = await t.one("SELECT id FROM payments WHERE order_id=? AND method=? AND status IN ('created','receipt_sent') "
                      "AND provider_payment_id IS NULL ORDER BY id DESC LIMIT 1", o["id"], method)
    if row:
        await t.execute("UPDATE payments SET provider_payment_id=?, amount=?, currency=?, status=?, confirmed_at=?, error=? WHERE id=?",
                        ppid, amount, currency, status, ts, error, row["id"])
        return row["id"]
    pid, _ = await t.execute(
        "INSERT INTO payments(bot_id,order_id,user_id,method,provider,provider_payment_id,amount,currency,status,created_at,"
        "confirmed_at,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        BOT_ID, o["id"], o["user_id"], method, provider, ppid, amount, currency, status, ts, ts, error)
    return pid


async def _apply_star(t, o, charge: str, ts: int) -> str:
    try:
        bid, tuid = (int(x) for x in o["meta"].split("|"))
        sc = (await BE.get_settings(t, bid))["star_score"]
        await BE.add_score(t, bid, tuid, "star", o["stars"] * sc, f"star:{charge}", actor=o["user_id"],
                           reason=f"order {o['order_code']}", now=ts)
        return "delivered"
    except (BE.BattleError, ValueError):
        return "manual_pending"


async def confirm_payment(*, order_id: int, provider: str, provider_payment_id: str, method: str, amount: int,
                          currency: str, user_id: int | None = None, admin_id: int | None = None) -> Result:
    """Pul haqiqatan tushganini YAGONA tranzaksiyada qayd etadi: payment + order + ledger + (donation/subscription/delivery).
    Takroriy chaqiruv (bir xil provider_payment_id) hech narsani o'zgartirmaydi."""
    vip_days = await SI("vip_days", 30)
    ts = now()
    async with tx() as t:
        o = await t.one("SELECT * FROM orders WHERE id=? AND bot_id=?", order_id, BOT_ID)
        if not o:
            await audit("payment_order_not_found", order_id=order_id, status="rejected", error=provider_payment_id, t=t)
            return Result("rejected", None, None, "order_not_found")
        if user_id is not None and o["user_id"] != user_id:
            await audit("payment_user_mismatch", user_id=user_id, order_id=order_id, status="rejected", error=provider_payment_id, t=t)
            return Result("rejected", o, None, "user_mismatch")
        ex = await t.one("SELECT * FROM payments WHERE bot_id=? AND provider=? AND provider_payment_id=?",
                         BOT_ID, provider, provider_payment_id)
        if ex:
            return Result("duplicate", o, ex["id"])
        exp = expected_amount(o, method)
        ptype, status = o["product_type"], o["payment_status"]

        if method == "wallet":
            if status != "pending" or (o["expires_at"] or 0) <= ts or ptype == "topup":
                return Result("rejected", o, None, "order_not_payable")
            if await wallet_balance(t, o["user_id"]) < exp:
                return Result("rejected", o, None, "insufficient_balance")

        if status not in ACCEPT:
            # pul kelgan, lekin buyurtma to'lanishi mumkin emas (allaqachon to'langan/bekor/qaytarilgan) -> ortiqcha to'lov
            pid = await _insert_payment(t, o, method, provider, provider_payment_id, amount, currency, "excess", ts,
                                        f"order status={status}")
            await post(t, order_id=o["id"], payment_id=pid, user_id=o["user_id"], type_="PAYMENT", account="cash",
                       amount=amount, currency=currency, provider=provider, provider_id=provider_payment_id,
                       idem=f"pay:{provider}:{provider_payment_id}", meta="excess")
            await pay_event("payment_excess", order_id=o["id"], payment_id=pid, detail=provider_payment_id, t=t)
            await audit("payment_excess", user_id=o["user_id"], order_id=o["id"], status="excess", error=provider_payment_id, t=t)
            return Result("excess", o, pid, status)

        ok = (amount >= int(exp * MIN_TON_RATIO)) if currency == "TON" else (amount == exp)
        if not ok:
            pid = await _insert_payment(t, o, method, provider, provider_payment_id, amount, currency, "review", ts,
                                        f"expected={exp} got={amount}")
            await post(t, order_id=o["id"], payment_id=pid, user_id=o["user_id"], type_="PAYMENT", account="cash",
                       amount=amount, currency=currency, provider=provider, provider_id=provider_payment_id,
                       idem=f"pay:{provider}:{provider_payment_id}", meta="amount_mismatch")
            await t.execute("UPDATE orders SET payment_status='review', last_error=? WHERE id=? AND payment_status IN ('pending','expired')",
                            f"summa mos emas: kutilgan {exp}, kelgan {amount} {currency}", o["id"])
            await pay_event("payment_amount_mismatch", order_id=o["id"], payment_id=pid, detail=f"exp={exp} got={amount}", t=t)
            await audit("payment_amount_mismatch", user_id=o["user_id"], order_id=o["id"], status="review",
                        error=f"exp={exp} got={amount} {currency}", t=t)
            return Result("review", await t.one("SELECT * FROM orders WHERE id=?", o["id"]), pid, "amount_mismatch")

        # ---- muvaffaqiyatli to'lov
        pid = await _insert_payment(t, o, method, provider, provider_payment_id, amount, currency, "confirmed", ts)
        await t.execute("UPDATE payments SET status='superseded' WHERE order_id=? AND id<>? AND status IN ('created','receipt_sent')",
                        o["id"], pid)
        _, rc = await t.execute(
            "UPDATE orders SET payment_status='paid', paid_at=?, payment_method=?, payment_provider=?, provider_payment_id=?, "
            "amount=?, currency=?, last_error='' WHERE id=? AND payment_status IN ('pending','expired','review')",
            ts, method, provider, provider_payment_id, amount, currency, o["id"])
        if not rc:
            raise RuntimeError("order holati kutilmaganda o'zgardi")
        uid, oid = o["user_id"], o["id"]
        if method == "wallet":
            await post(t, order_id=oid, payment_id=pid, user_id=uid, type_="WALLET_SPEND", account="wallet", amount=-exp,
                       currency="XTR", provider="wallet", provider_id=provider_payment_id, idem=f"wallet_spend:{oid}")
        else:
            await post(t, order_id=oid, payment_id=pid, user_id=uid, type_="PAYMENT", account="cash", amount=amount,
                       currency=currency, provider=provider, provider_id=provider_payment_id,
                       idem=f"pay:{provider}:{provider_payment_id}", meta=f"admin={admin_id}" if admin_id else "")
        sale = dict(order_id=oid, payment_id=pid, user_id=uid, amount=amount, currency=currency, provider=provider,
                    provider_id=provider_payment_id, idem=f"sale:{oid}")
        dstatus = "none"
        if ptype == "topup":
            await post(t, order_id=oid, payment_id=pid, user_id=uid, type_="WALLET_TOPUP", account="wallet", amount=o["stars"],
                       currency="XTR", provider="wallet", provider_id=provider_payment_id, idem=f"topup:{oid}")
            dstatus = "delivered"
        elif ptype == "donation":
            await t.execute("INSERT OR IGNORE INTO donations(bot_id,order_id,user_id,amount,currency,status,created_at) "
                            "VALUES(?,?,?,?,?,'confirmed',?)", BOT_ID, oid, uid, amount, currency, ts)
            await post(t, type_="DONATION", account="donation", **sale)
        else:
            await post(t, type_=SALE_LEDGER[ptype], account="revenue", **sale)
            if ptype == "vip":
                await activate_vip(t, uid, oid, vip_days)
                dstatus = "delivered"
            elif ptype == "battle_credit":
                await t.execute("UPDATE users SET credits=credits+1 WHERE id=?", uid)
                dstatus = "delivered"
            elif ptype == "battle_star":
                dstatus = await _apply_star(t, o, provider_payment_id, ts)       # to'lov + ball BIR tranzaksiyada
                if dstatus == "manual_pending":                                   # Battle yopilgan bo'lsa: pul yo'qolmaydi, owner hal qiladi
                    await t.execute("INSERT OR IGNORE INTO deliveries(bot_id,order_id,mode,status,last_error,created_at,updated_at) "
                                    "VALUES(?,?,'manual','manual_pending','Battle faol emas: ball berilmadi',?,?)", BOT_ID, oid, ts, ts)
            else:
                mode = "auto" if ptype in AUTO_TYPES else "manual"
                dstatus = "pending" if mode == "auto" else "manual_pending"
                await t.execute("INSERT OR IGNORE INTO deliveries(bot_id,order_id,mode,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                BOT_ID, oid, mode, dstatus, ts, ts)
        await t.execute("UPDATE orders SET delivery_status=?, delivered_at=? WHERE id=?",
                        dstatus, ts if dstatus == "delivered" else None, oid)
        await pay_event("payment_confirmed", order_id=oid, payment_id=pid, detail=f"{provider}:{provider_payment_id}", t=t)
        await audit("payment_confirmed", user_id=uid, admin_id=admin_id, order_id=oid, t=t)
        return Result("confirmed", await t.one("SELECT * FROM orders WHERE id=?", oid), pid)


# ------------------------------------------------------------------ to'lovdan keyingi amallar (xabarlar, yetkazish)

async def after_confirm(bot, res: Result):
    """Tranzaksiya commit bo'lgandan KEYIN: xabarlar va yetkazishni ishga tushirish."""
    o = res.order
    try:
        if res.status == "confirmed":
            await notify_order(bot, o["id"])
            if o["product_type"] == "battle_star" and o["delivery_status"] == "delivered":
                await schedule_post_update(bot, int(o["meta"].split("|")[0]))
            if o["product_type"] == "donation":
                await owner_alert(bot, f"💝 <b>Yangi donat</b>\nBuyurtma {o['order_code']} · {fmt_amount(o['amount'], o['currency'])}\n"
                                       f"Foydalanuvchi: <a href=\"tg://user?id={o['user_id']}\">{o['user_id']}</a>")
            elif o["delivery_status"] == "manual_pending":
                await owner_manual_notice(bot, o["id"])
            elif o["delivery_status"] == "pending":
                kick(bot, o["id"])
        elif res.status == "review":
            await owner_alert(bot, f"🔎 <b>To'lov summasi mos kelmadi</b>\nBuyurtma {o['order_code']}: {o['last_error']}\n"
                                   "Tekshirib qaror qiling.", order_id=o["id"])
        elif res.status == "excess":
            await handle_excess(bot, res)
    except Exception:
        log.exception("after_confirm xatosi (order=%s)", o["id"] if o else None)


async def pay_with_wallet(bot, oid: int, uid: int) -> Result:
    o = await get_order(oid, uid)
    if not o:
        return Result("rejected", None, None, "order_not_found")
    res = await confirm_payment(order_id=oid, provider="wallet", provider_payment_id=f"wallet:{oid}", method="wallet",
                                amount=int(o["stars"]), currency="XTR", user_id=uid)
    await after_confirm(bot, res)
    return res


async def admin_confirm(bot, oid: int, admin_id: int) -> Result:
    """Karta/qo'lda to'lovni owner tasdiqlaydi (kim tasdiqlagani audit va ledger metadata'da qoladi)."""
    o = await get_order(oid)
    if not o:
        return Result("rejected", None, None, "order_not_found")
    res = await confirm_payment(order_id=oid, provider="manual_card", provider_payment_id=f"card:{oid}", method="card",
                                amount=int(o["uzs"]), currency="UZS", admin_id=admin_id)
    await after_confirm(bot, res)
    return res


async def reject_payment(bot, oid: int, admin_id: int) -> bool:
    async with tx() as t:
        o = await t.one("SELECT * FROM orders WHERE id=? AND bot_id=?", oid, BOT_ID)
        if not o:
            return False
        _, rc = await t.execute("UPDATE orders SET payment_status='failed', last_error='rad etildi' WHERE id=? "
                                "AND payment_status IN ('pending','expired','review')", oid)
        if not rc:
            return False
        await t.execute("UPDATE payments SET status='rejected' WHERE order_id=? AND status IN ('created','receipt_sent','review')", oid)
        await audit("payment_rejected", user_id=o["user_id"], admin_id=admin_id, order_id=oid, t=t)
    await send(bot, o["user_id"], f"❌ Buyurtma <b>{o['order_code']}</b> bo'yicha to'lov tasdiqlanmadi. "
                                  "Agar xato bo'lsa, admin bilan bog'laning.")
    return True


async def mark_receipt(oid: int):
    async with tx() as t:
        await t.execute("UPDATE payments SET status='receipt_sent' WHERE order_id=? AND method='card' AND status='created'", oid)
        await pay_event("receipt_sent", order_id=oid, t=t)


# ------------------------------------------------------------------ Telegram successful_payment

async def _retry(fn, tries=3):
    last = None
    for i in range(tries):
        try:
            return await fn()
        except Exception as e:
            last = e
            log.exception("urinish %s/%s muvaffaqiyatsiz", i + 1, tries)
            await asyncio.sleep(0.5 * (3 ** i))
    raise last


async def record_fund(uid: int, charge: str, amount: int) -> bool:
    async with tx() as t:
        return await post(t, user_id=uid, type_="BOT_FUND", account="bot_fund", amount=amount, currency="XTR",
                          provider="telegram_stars", provider_id=charge, idem=f"fund:{charge}")


async def handle_successful_payment(bot, m):
    sp = m.successful_payment
    payload, charge, uid = sp.invoice_payload, sp.telegram_payment_charge_id, m.from_user.id
    if payload.startswith("fund:"):
        if uid != OWNER_ID:
            return await audit("fund_by_non_owner", user_id=uid, status="rejected", error=charge)
        if await record_fund(uid, charge, sp.total_amount):
            await send(bot, uid, f"✅ Bot balansiga <b>{sp.total_amount}⭐</b> qo'shildi. Endi Premium va giftlar avtomatik yetkaziladi.")
        return
    if not payload.startswith("o:") or not payload[2:].isdigit():
        return await audit("successful_payment_unknown_payload", user_id=uid, status="error", error=f"{payload} {charge}")
    oid = int(payload[2:])
    method = "stars" if sp.currency == "XTR" else "uzs"
    amount = sp.total_amount if sp.currency == "XTR" else sp.total_amount // 100
    provider = METHODS[method][0]
    # xom hodisani darhol yozib qo'yamiz: keyingi bosqich yiqilsa ham to'lov yo'qolmaydi (replay_received qayta bajaradi)
    try:
        await pay_event("sp_received", order_id=oid, detail={"oid": oid, "charge": charge, "amount": amount,
                                                              "currency": sp.currency, "uid": uid, "method": method})
    except Exception:
        log.exception("sp_received yozilmadi")
    try:
        res = await _retry(lambda: confirm_payment(order_id=oid, provider=provider, provider_payment_id=charge, method=method,
                                                   amount=amount, currency=sp.currency, user_id=uid))
    except Exception as e:
        ref = error_ref()
        log.error("TO'LOV QAYD ETILMADI ref=%s order=%s charge=%s: %s", ref, oid, charge, e)
        await owner_alert(bot, f"🚨 <b>To'lov qabul qilindi, lekin qayd etilmadi!</b>\nRef: <code>{ref}</code>\nBuyurtma: {oid}\n"
                               f"Foydalanuvchi: {uid}\nCharge: <code>{charge}</code>\nSumma: {amount} {sp.currency}\n"
                               "Bot qayta ishga tushganda avtomatik qayta uriniladi.")
        await send(bot, uid, f"⏳ To'lovingiz qabul qilindi, buyurtma qayta ishlanmoqda. Ref: <code>{ref}</code>")
        return
    await after_confirm(bot, res)


async def replay_received(bot) -> int:
    """sp_received hodisasi bor-u payments yozuvi yo'q to'lovlarni qayta tasdiqlaydi (crash recovery)."""
    n = 0
    for ev in await q("SELECT * FROM payment_events WHERE bot_id=? AND event='sp_received' AND created_at<? "
                      "ORDER BY id DESC LIMIT 200", BOT_ID, now() - 20):
        try:
            d = json.loads(ev["detail"])
        except ValueError:
            continue
        provider = METHODS[d["method"]][0]
        if await one("SELECT 1 FROM payments WHERE bot_id=? AND provider=? AND provider_payment_id=?", BOT_ID, provider, d["charge"]):
            continue
        if await one("SELECT 1 FROM payment_events WHERE bot_id=? AND event='sp_replayed' AND detail=?", BOT_ID, d["charge"]):
            continue
        res = await confirm_payment(order_id=d["oid"], provider=provider, provider_payment_id=d["charge"], method=d["method"],
                                    amount=d["amount"], currency=d["currency"], user_id=d["uid"])
        if res.status == "rejected":
            await pay_event("sp_replayed", order_id=d["oid"], detail=d["charge"])     # qayta urinmaslik uchun
            await audit("payment_replay_rejected", user_id=d["uid"], order_id=d["oid"], status="rejected", error=d["charge"])
        await after_confirm(bot, res)
        n += 1
    return n


##############################################################################
# app/payments/refunds.py
##############################################################################
REFUNDABLE = SALE_TYPES + ("donation", "topup")
BAD_DELIVERY = ("processing", "uncertain")


async def refund_order(bot, oid: int, admin_id: int, mode: str = "original", reason: str = ""):
    """Buyurtmani qaytaradi. mode: 'original' (asl usulga) yoki 'wallet' (bot balansiga).
    Qaytarish bir buyurtmaga faqat BIR marta (refunds.order_id UNIQUE + holat tekshiruvi). Qaytadi: (ok, matn)."""
    async with tx() as t:
        o = await t.one("SELECT * FROM orders WHERE id=? AND bot_id=?", oid, BOT_ID)
        if not o:
            return False, "Buyurtma topilmadi"
        if o["payment_status"] != "paid":
            return False, "Buyurtma to'langan holatda emas (allaqachon qaytarilgan bo'lishi mumkin)"
        if o["product_type"] not in REFUNDABLE:
            return False, "Bu turdagi buyurtmani bot orqali qaytarib bo'lmaydi (VIP/kredit) — /give bilan tuzating"
        d = await t.one("SELECT * FROM deliveries WHERE order_id=?", oid)
        if d and (d["status"] in BAD_DELIVERY or d["status"] == "delivered"):
            return False, f"Yetkazish holati «{d['status']}» — qaytarib bo'lmaydi (avval holatni aniqlang)"
        p = await t.one("SELECT * FROM payments WHERE order_id=? AND status='confirmed' ORDER BY id LIMIT 1", oid)
        if not p:
            return False, "Tasdiqlangan to'lov topilmadi"
        method = p["method"]
        if method == "wallet":
            mode = "wallet"
        if mode == "wallet" and o["product_type"] == "topup":
            return False, "Balans to'ldirishni balansga qaytarib bo'lmaydi"
        if o["product_type"] == "topup" and await wallet_balance(t, o["user_id"]) < o["stars"]:
            return False, "Foydalanuvchi balansi yetarli emas (pul allaqachon ishlatilgan)"
        r = await t.one("SELECT * FROM refunds WHERE order_id=?", oid)
        if r and r["status"] != "failed":
            return False, f"Qaytarish allaqachon mavjud (holat: {r['status']})"
        if r:
            await t.execute("UPDATE refunds SET status='processing', error='', admin_id=?, reason=?, method=? WHERE order_id=?",
                            admin_id, reason, mode if mode == "wallet" else method, oid)
        else:
            await t.execute("INSERT INTO refunds(bot_id,order_id,payment_id,user_id,amount,currency,method,status,reason,admin_id,created_at) "
                            "VALUES(?,?,?,?,?,?,?,'processing',?,?,?)", BOT_ID, oid, p["id"], o["user_id"], p["amount"],
                            p["currency"], mode if mode == "wallet" else method, reason, admin_id, now())
        prior = d["status"] if d else ""
        if d:
            await t.execute("UPDATE deliveries SET status='cancelled', updated_at=? WHERE order_id=?", now(), oid)
        await t.execute("UPDATE orders SET delivery_status=? WHERE id=?", "cancelled" if d else o["delivery_status"], oid)
        await audit("refund_started", user_id=o["user_id"], admin_id=admin_id, order_id=oid, status=mode, t=t)
    return await _execute(bot, oid, admin_id, mode, method, p, prior)


async def _restore(oid, prior, err):
    async with tx() as t:
        await t.execute("UPDATE refunds SET status='failed', error=? WHERE order_id=? AND status='processing'", err[:300], oid)
        if prior:
            await t.execute("UPDATE deliveries SET status=? WHERE order_id=? AND status='cancelled'", prior, oid)
            await t.execute("UPDATE orders SET delivery_status=? WHERE id=?", prior, oid)


async def _execute(bot, oid, admin_id, mode, method, p, prior):
    if mode == "original" and method == "stars":
        try:
            await bot.refund_star_payment(user_id=p["user_id"], telegram_payment_charge_id=p["provider_payment_id"])
        except Exception as e:
            msg = str(e)
            if "ALREADY_REFUNDED" not in msg.upper():
                await _restore(oid, prior, msg)
                await audit("refund_failed", admin_id=admin_id, order_id=oid, status="error", error=msg)
                return False, f"Telegram qaytarmadi: {msg[:150]}"
    elif mode == "original" and method != "wallet":
        async with tx() as t:
            await t.execute("UPDATE refunds SET status='manual_required' WHERE order_id=? AND status='processing'", oid)
        o = await one("SELECT * FROM orders WHERE id=?", oid)
        await owner_alert(bot, f"💸 <b>Qo'lda qaytarish kerak</b> — {o['order_code']}\nSumma: <b>{fmt_amount(p['amount'], p['currency'])}</b> "
                               f"({method})\nXaridor: {await who(o['user_id'])}\nPulni o'zingiz qaytaring, so'ng tasdiqlang.",
                          K([B("✅ Pul qaytarildi", f"o:rfc:{oid}")], [B("🧾 Buyurtma", f"o:od:{oid}")]))
        return True, "Qo'lda qaytarish kutilmoqda: pulni o'tkazib, tasdiqlang"
    ok, msg = await finalize(bot, oid, admin_id, mode)
    return ok, msg


async def complete_manual_refund(bot, oid: int, admin_id: int):
    r = await one("SELECT * FROM refunds WHERE order_id=?", oid)
    if not r or r["status"] != "manual_required":
        return False, "Qo'lda qaytarish kutilmayapti"
    return await finalize(bot, oid, admin_id, "original")


async def finalize(bot, oid: int, admin_id: int, mode: str):
    """Qaytarishni ledger'ga yozadi va buyurtmani yopadi. Idempotent."""
    try:
        async with tx() as t:
            r = await t.one("SELECT * FROM refunds WHERE order_id=?", oid)
            if not r or r["status"] not in ("processing", "manual_required"):
                return False, "Qaytarish allaqachon bajarilgan yoki mavjud emas"
            o = await t.one("SELECT * FROM orders WHERE id=?", oid)
            p = await t.one("SELECT * FROM payments WHERE id=?", r["payment_id"])
            uid, amt, cur, method, ts = o["user_id"], p["amount"], p["currency"], p["method"], now()
            common = dict(order_id=oid, payment_id=p["id"], user_id=uid, provider=p["provider"], provider_id=p["provider_payment_id"] or "")
            if o["product_type"] == "topup":
                if await wallet_balance(t, uid) < o["stars"]:
                    raise RuntimeError("balans yetarli emas")
                await post(t, type_="REFUND", account="wallet", amount=-o["stars"], currency="XTR", idem=f"refund:{oid}:wallet", **common)
            else:
                acct = "donation" if o["product_type"] == "donation" else "revenue"
                await post(t, type_="REFUND", account=acct, amount=-amt, currency=cur, idem=f"refund:{oid}:rev", **common)
                if method == "wallet" or mode == "wallet":
                    await post(t, type_="WALLET_REFUND", account="wallet", amount=o["stars"] if method == "wallet" else o["stars"],
                               currency="XTR", idem=f"refund:{oid}:wallet", **common)
            if mode == "original" and method != "wallet":
                await post(t, type_="REFUND", account="cash", amount=-amt, currency=cur, idem=f"refund:{oid}:cash", **common)
            await t.execute("UPDATE orders SET payment_status='refunded', delivery_status='cancelled' WHERE id=?", oid)
            await t.execute("UPDATE payments SET status='refunded' WHERE id=?", p["id"])
            await t.execute("UPDATE donations SET status='refunded' WHERE order_id=?", oid)
            await t.execute("UPDATE deliveries SET status='cancelled' WHERE order_id=? AND status<>'delivered'", oid)
            await t.execute("UPDATE refunds SET status='completed', completed_at=?, admin_id=? WHERE order_id=?", ts, admin_id, oid)
            await audit("refund_completed", user_id=uid, admin_id=admin_id, order_id=oid, status=mode, t=t)
            await pay_event("refund_completed", order_id=oid, payment_id=p["id"], detail=mode, t=t)
    except Exception as e:
        log.exception("refund finalize xatosi order=%s", oid)
        await _restore(oid, "", str(e))
        return False, f"Xato: {e}"
    where = "bot balansingizga" if mode == "wallet" else "to'lov usulingizga"
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    await send(bot, o["user_id"], f"↩️ Buyurtma <b>{o['order_code']}</b> bo'yicha to'lov {where} qaytarildi: "
                                  f"<b>{fmt_amount(o['stars'], 'XTR') if mode == 'wallet' else fmt_amount(o['amount'], o['currency'])}</b>.")
    return True, "Qaytarildi"


async def recover_refunds(bot) -> int:
    """Crash'dan qolgan «processing» qaytarishlarni davom ettiradi (Telegram refund idempotent)."""
    n = 0
    for r in await q("SELECT * FROM refunds WHERE bot_id=? AND status='processing' AND created_at<?", BOT_ID, now() - 120):
        p = await one("SELECT * FROM payments WHERE id=?", r["payment_id"])
        mode = "wallet" if r["method"] == "wallet" else "original"
        method = p["method"] if p else ""
        ok, _ = await _execute(bot, r["order_id"], r["admin_id"] or 0, mode, method, p, "")
        n += 1 if ok else 0
    return n


# ------------------------------------------------------------------ ortiqcha (excess) to'lovlar

async def handle_excess(bot, res):
    """Buyurtma allaqachon to'langan/bekor bo'lgan, ammo yana pul kelgan. Stars bo'lsa avtomatik qaytariladi."""
    o, pid = res.order, res.payment_id
    p = await one("SELECT * FROM payments WHERE id=?", pid)
    if p["method"] == "stars":
        try:
            await bot.refund_star_payment(user_id=p["user_id"], telegram_payment_charge_id=p["provider_payment_id"])
        except Exception as e:
            if "ALREADY_REFUNDED" not in str(e).upper():
                return await owner_alert(bot, f"⚠️ Ortiqcha Stars to'lovi ({o['order_code']}) avtomatik qaytarilmadi: {str(e)[:150]}",
                                         K([B("✅ Qo'lda qaytarildi", f"o:xr:{pid}")]), o["id"])
        await mark_excess_refunded(pid, 0)
        return await send(bot, p["user_id"], f"↩️ Buyurtma {o['order_code']} ikki marta to'langani uchun ortiqcha to'lov qaytarildi.")
    await owner_alert(bot, f"⚠️ <b>Ortiqcha to'lov</b> — {o['order_code']}\n{fmt_amount(p['amount'], p['currency'])} ({p['method']}) "
                           f"{o['payment_status']} buyurtmaga kelgan. Pulni qaytaring va tasdiqlang.",
                      K([B("✅ Qo'lda qaytarildi", f"o:xr:{pid}")]), o["id"])


async def mark_excess_refunded(pid: int, admin_id: int) -> bool:
    async with tx() as t:
        p = await t.one("SELECT * FROM payments WHERE id=?", pid)
        if not p or p["status"] != "excess":
            return False
        await t.execute("UPDATE payments SET status='refunded' WHERE id=?", pid)
        await post(t, order_id=p["order_id"], payment_id=pid, user_id=p["user_id"], type_="REFUND", account="cash",
                   amount=-p["amount"], currency=p["currency"], provider=p["provider"], provider_id=p["provider_payment_id"] or "",
                   idem=f"refund:excess:{pid}", meta="excess")
        await audit("excess_refunded", admin_id=admin_id, order_id=p["order_id"], t=t)
    return True


##############################################################################
# app/payments/ton.py
##############################################################################
TON_FETCHER = None      # testlarda almashtiriladi: async def(addr) -> list[dict] | None


async def fetch_transactions(addr: str):
    if TON_FETCHER is not None:
        return await TON_FETCHER(addr)
    headers = {"X-API-Key": TONCENTER_KEY} if TONCENTER_KEY else {}
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get("https://toncenter.com/api/v2/getTransactions", params={"address": addr, "limit": 50}) as r:
                d = await r.json()
        return d.get("result", []) if d.get("ok", True) else None
    except Exception as e:
        log.warning("TON tekshiruvi xatosi: %s", e)
        return None


def incoming(txs):
    """Kiruvchi o'tkazmalar: hash, izoh (order kodi), nanoTON, vaqt."""
    out = []
    for t in txs or []:
        m = t.get("in_msg") or {}
        try:
            val = int(m.get("value") or 0)
        except (TypeError, ValueError):
            continue
        h = (t.get("transaction_id") or {}).get("hash") or ""
        if val > 0 and h:
            out.append({"hash": h, "comment": (m.get("message") or "").strip(), "nano": val, "utime": int(t.get("utime") or 0),
                        "src": m.get("source") or ""})
    return out


async def ton_scan(bot, only_order: int | None = None):
    """TON qabul manzilidagi kiruvchi o'tkazmalarni buyurtma kodiga (izoh) va summaga solishtiradi.
    Qaytaradi: tasdiqlangan to'lovlar soni yoki None (tarmoq xatosi)."""
    addr = await S("ton_address")
    if not addr:
        return 0
    txs = await fetch_transactions(addr)
    if txs is None:
        return None
    sql = ("SELECT * FROM orders WHERE bot_id=? AND payment_method='ton' AND payment_status IN ('pending','expired','review') "
           "AND created_at>=?")
    args = [BOT_ID, now() - TON_ORDER_GRACE - 7 * 86400]
    if only_order:
        sql += " AND id=?"
        args.append(only_order)
    pend = {o["order_code"]: o for o in await q(sql, *args)}
    done = 0
    for x in incoming(txs):
        o = pend.get(x["comment"])
        if o is None:
            if not only_order and x["comment"] and not await one(
                    "SELECT 1 FROM payment_events WHERE bot_id=? AND event='ton_unmatched' AND detail LIKE ?", BOT_ID, f"%{x['hash']}%"):
                paid = await one("SELECT 1 FROM payments WHERE bot_id=? AND provider='ton' AND provider_payment_id=?", BOT_ID, x["hash"])
                if not paid:
                    await pay_event("ton_unmatched", detail=f"{x['hash']} {fmt_ton(x['nano'])} comment={x['comment'][:40]}")
            continue
        if x["utime"] and x["utime"] < o["created_at"] - 120:
            continue
        if o["expires_at"] and now() > o["expires_at"] + TON_ORDER_GRACE:
            continue
        res = await confirm_payment(order_id=o["id"], provider="ton", provider_payment_id=x["hash"], method="ton",
                                    amount=x["nano"], currency="TON", user_id=o["user_id"])
        if res.status != "duplicate":
            await after_confirm(bot, res)
        if res.status == "confirmed":
            done += 1
    return done


async def ton_check_order(bot, oid: int):
    """Foydalanuvchi «Tekshirish» bosganda. Qaytadi: 'paid' | 'pending' | 'error'."""
    r = await ton_scan(bot, only_order=oid)
    if r is None:
        return "error"
    o = await one("SELECT payment_status FROM orders WHERE id=?", oid)
    return "paid" if o and o["payment_status"] == "paid" else "pending"


##############################################################################
# app/workers/loops.py
##############################################################################
async def notify_sweep(bot) -> int:
    """Xabari yetib bormagan to'langan buyurtmalar (crash/tarmoq xatosi) uchun qayta urinish."""
    rows = await q("SELECT id FROM orders WHERE bot_id=? AND payment_status='paid' AND ((notified & 1)=0 OR (delivery_status='delivered' AND (notified & 2)=0)) AND paid_at<? "
                   "AND delivery_status NOT IN ('uncertain','failed','cancelled') ORDER BY id LIMIT 50", BOT_ID, now() - 90)
    for r in rows:
        await notify_order(bot, r["id"])
    return len(rows)


async def recover(bot):
    """Bot qayta ishga tushganda bir marta: to'xtab qolgan jarayonlarni xavfsiz holatga keltirish."""
    steps = (("stale_deliveries", lambda: sweep_stale(bot, startup=True)), ("replay_payments", lambda: replay_received(bot)),
             ("refunds", lambda: recover_refunds(bot)), ("expire", expire_due), ("ton", lambda: ton_scan(bot)),
             ("notify", lambda: notify_sweep(bot)))
    for name, fn in steps:
        try:
            r = await fn()
            log.info("recovery %s: %s", name, r)
        except Exception:
            log.exception("recovery %s xatosi", name)


async def maintenance_loop(bot):
    tick = 0
    while True:
        await asyncio.sleep(30)
        tick += 1
        jobs = [("expire", expire_due), ("stale", lambda: sweep_stale(bot)), ("replay", lambda: replay_received(bot)),
                ("notify", lambda: notify_sweep(bot)), ("ton", lambda: ton_scan(bot)), ("refunds", lambda: recover_refunds(bot))]
        if tick % 20 == 0:
            jobs += [("subs", lambda: subs_maintenance(bot)), ("stars", lambda: stars_alert(bot))]
        for name, fn in jobs:
            try:
                await fn()
            except Exception:
                log.exception("maintenance %s xatosi", name)


def start_workers(bot):
    return [asyncio.create_task(delivery_loop(bot)), asyncio.create_task(maintenance_loop(bot))]


##############################################################################
# app/web.py
##############################################################################
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
    rate = await SI("uzs_rate", 230)
    items = [dict(id=i["id"], kind=i["kind"], title=i["title"], descr=i["descr"], stars=i["price_stars"],
                  uzs=calc_uzs(i["price_stars"], rate))
             for i in await q("SELECT * FROM items WHERE active=1 ORDER BY kind, price_stars")]
    games = []
    for g in await q("SELECT * FROM games WHERE active=1 ORDER BY id"):
        packs = await q("SELECT id,title,price_uzs FROM packs WHERE game_id=? AND active=1 AND price_uzs>0 ORDER BY price_uzs", g["id"])
        if packs:
            games.append({"id": g["id"], "name": g["name"], "emoji": g["emoji"],
                          "packs": [{"id": p["id"], "title": p["title"], "uzs": p["price_uzs"]} for p in packs]})
    return web.json_response({
        "games": games, "brand": await S("brand"), "bot": await bot_username(req.app["bot"]), "ton_rate": await SI("ton_rate", 200),
        "user": {"id": uid, "name": u["name"], "balance": u["balance"], "credits": u["credits"],
                 "vip": fmt_ts(u["vip_until"]) if is_vip(u) else "", "wallet": u["wallet"]},
        "battles": battles, "items": items})


async def _web_invoice(req, o):
    """Mini App: Stars hisob-fakturasi havolasi. Narx serverdagi buyurtmadan olinadi."""
    if not o:
        return web.json_response({"error": "Buyurtma yaratib bo'lmadi"})
    bot: Bot = req.app["bot"]
    p, err = await start_payment(o, "stars")
    if not p:
        return web.json_response({"error": err})
    title = (o["product_title"] or "Buyurtma")[:32]
    link = await bot.create_invoice_link(title=title, description=f"Buyurtma {o['order_code']}", payload=f"o:{o['id']}",
                                         currency="XTR", prices=[LabeledPrice(label=title, amount=o["stars"])])
    return web.json_response({"link": link, "order": o["order_code"]})


@auth
async def api_buy(req: web.Request):
    body = await req.json()
    it = await one("SELECT * FROM items WHERE id=? AND active=1", int(body.get("item_id", 0)))
    if not it:
        return web.json_response({"error": "Mahsulot topilmadi"})
    if it["kind"] == "stars":
        u = await bot_username(req.app["bot"])
        return web.json_response({"bot": f"https://t.me/{u}?start=buy_{it['id']}"})
    return await _web_invoice(req, await create_order(req["uid"], it["kind"], it["id"]))


@auth
async def api_topup(req: web.Request):
    body = await req.json()
    try:
        amount = int(body.get("stars", 0))
    except (TypeError, ValueError):
        amount = 0
    if not MIN_TOPUP <= amount <= MAX_TOPUP:
        return web.json_response({"error": f"Miqdor {MIN_TOPUP} dan {MAX_TOPUP} gacha bo'lsin"})
    return await _web_invoice(req, await create_order(req["uid"], "topup", stars=amount))


@auth
async def api_donate(req: web.Request):
    body = await req.json()
    try:
        amount = int(body.get("stars", 0))
    except (TypeError, ValueError):
        amount = 0
    if not MIN_DONATION <= amount <= MAX_DONATION:
        return web.json_response({"error": f"Miqdor {MIN_DONATION} dan {MAX_DONATION} gacha bo'lsin"})
    return await _web_invoice(req, await create_order(req["uid"], "donation", stars=amount))


@auth
async def api_orders(req: web.Request):
    rows = await user_orders(req["uid"], 15)
    return web.json_response({"orders": [{"code": o["order_code"], "title": o["product_title"], "state": order_state(o),
                                          "stars": o["stars"], "uzs": o["uzs"], "ts": fmt_ts(o["created_at"])} for o in rows]})


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
    return web.Response(text=INDEX_HTML, content_type="text/html", headers={"Cache-Control": "no-cache"})


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
        web.post("/api/donate", api_donate), web.get("/api/orders", api_orders),
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
STATUS_ICON = {"paid": "✅", "pending": "⏳", "review": "🔎", "failed": "❌", "expired": "⌛", "cancelled": "✖️", "refunded": "↩️"}


async def checkout(ev, o):
    """To'lov usulini tanlash ekrani. `o` — orders qatori."""
    if o is None:
        return await screen(ev, "shop", "Mahsulot hozircha mavjud emas.", K([HOME]))
    u = await one("SELECT balance FROM users WHERE id=?", o["user_id"])
    oid = o["id"]
    rows = []
    for m in await allowed_methods(o["product_type"]):
        if m == "card":
            rows.append([B(f"🏦 Karta orqali — {fmt_uzs(o['uzs'])}", f"pay:card:{oid}")])
        elif m == "uzs":
            rows.append([B(f"💳 Payme/Click/karta — {fmt_uzs(o['uzs'])}", f"pay:uzs:{oid}")])
        elif m == "wallet":
            rows.append([B(f"💰 Balansdan to'lash ({u['balance']}⭐)", f"pay:wallet:{oid}")])
        elif m == "stars":
            rows.append([B(f"⭐ Telegram Stars bilan ({o['stars']}⭐)", f"pay:stars:{oid}")])
        elif m == "ton":
            rows.append([B(f"💠 TON bilan ({fmt_ton(o['ton_nano'])})", f"pay:ton:{oid}")])
    rows.append([B("✖️ Bekor qilish", f"ordx:{oid}")])
    text = (f"🧾 <b>Buyurtma {o['order_code']}</b>\n\n{esc(o['product_title'])}\n"
            + (f"{fmt_meta(o['meta'])}\n" if o["product_type"] == "game" and o["meta"] else "")
            + f"💳 Narx: <b>{fmt_uzs(o['uzs'])}</b>\n\nTo'lov usulini tanlang 👇")
    if len(rows) == 1:
        text += "\n\n⚠️ Hozircha to'lov usullari sozlanmagan. Iltimos, admin bilan bog'laning."
    await screen(ev, "shop", text, K(*rows))


async def _mine(c: CallbackQuery, oid: int):
    o = await get_order(oid, c.from_user.id)
    if not o or o["payment_status"] != "pending":
        await c.answer("Buyurtma topilmadi yoki allaqachon to'langan", show_alert=True)
        return None
    return o


async def _begin(c: CallbackQuery, method: str):
    o = await _mine(c, int(c.data.split(":")[2]))
    if not o:
        return None, None
    p, err = await start_payment(o, method)
    if not p:
        await c.answer(err, show_alert=True)
        return None, None
    return o, p


# ------------------------------------------------------------------ karta + chek

@r_pay.callback_query(F.data.startswith("pay:card:"))
async def pay_card(c: CallbackQuery, state: FSMContext):
    o, p = await _begin(c, "card")
    if not o:
        return
    card, holder = await S("card_number"), await S("card_holder")
    await state.set_state(Rcpt.photo)
    await state.update_data(oid=o["id"])
    text = (f"🏦 <b>Kartaga o'tkazma</b> — {o['order_code']}\n\n1️⃣ Karta raqami:\n<code>{esc(card)}</code>\n"
            + (f"👤 Karta egasi: <b>{esc(holder)}</b>\n" if holder else "")
            + f"2️⃣ Aniq summa: <b>{fmt_uzs(o['uzs'])}</b>\n\n"
            "3️⃣ O'tkazgach <b>chek (skrinshot)</b> rasmini shu yerga yuboring 📎\n"
            "Admin tekshirib tasdiqlaydi, so'ng buyurtma bajariladi.\n"
            "ℹ️ Bank kartasi o'tkazmasi avtomatik tekshirilmaydi — tasdiqlashni admin qiladi.")
    await screen(c, "wallet", text, K([B("✖️ Bekor qilish", f"ordx:{o['id']}")]))


@r_pay.message(Rcpt.photo, F.photo | F.document)
async def receipt(m: Message, state: FSMContext):
    d = await state.get_data()
    o = await get_order(d.get("oid") or 0, m.from_user.id)
    await state.clear()
    if not o or o["payment_status"] not in ("pending", "expired"):
        return await m.answer("Buyurtma topilmadi yoki allaqachon to'langan.", reply_markup=K([HOME]))
    caption = (f"🧾 <b>Yangi to'lov cheki</b>\nBuyurtma {o['order_code']} — {esc(o['product_title'])}\n"
               + (f"{fmt_meta(o['meta'])}\n" if o["product_type"] == "game" and o["meta"] else "")
               + f"Summa: <b>{fmt_uzs(o['uzs'])}</b>\nXaridor: {await who(m.from_user.id)}")
    try:
        await m.bot.copy_message(OWNER_ID, m.chat.id, m.message_id, caption=caption,
                                 reply_markup=K([B("✅ Tasdiqlash", f"o:rok:{o['id']}"), B("❌ Rad etish", f"o:rno:{o['id']}")]))
    except Exception:
        return await m.answer("❌ Chekni adminga yuborib bo'lmadi. Keyinroq qayta urinib ko'ring.", reply_markup=K([HOME]))
    await mark_receipt(o["id"])
    await m.answer(f"✅ Chek qabul qilindi. Buyurtma <b>{o['order_code']}</b> admin tomonidan tekshirilmoqda — odatda bir necha daqiqa.",
                   reply_markup=K([B("🧾 Buyurtmam", f"ord:{o['id']}")], [HOME]))


@r_pay.message(Rcpt.photo)
async def receipt_wrong(m: Message):
    await m.answer("📎 Iltimos, to'lov chekini <b>rasm</b> ko'rinishida yuboring.", reply_markup=K([B("✖️ Bekor qilish", "x:cancel")]))


# ------------------------------------------------------------------ Telegram Payments / Stars

async def _invoice(c: CallbackQuery, o, currency: str, amount: int, token: str | None):
    title = o["product_title"][:32] or "Buyurtma"
    kw = dict(chat_id=c.from_user.id, title=title, description=f"Buyurtma {o['order_code']}", payload=f"o:{o['id']}",
              currency=currency, prices=[LabeledPrice(label=title, amount=amount)])
    if token:
        kw["provider_token"] = token
    try:
        await c.bot.send_invoice(**kw)
        await c.answer()
    except Exception as e:
        log.warning("invoice yuborilmadi: %s", e)
        await c.answer(f"To'lov hisobini ochib bo'lmadi: {e}"[:190], show_alert=True)


@r_pay.callback_query(F.data.startswith("pay:uzs:"))
async def pay_uzs(c: CallbackQuery):
    o, p = await _begin(c, "uzs")
    if o:
        await _invoice(c, o, "UZS", o["uzs"] * 100, await S("provider_token"))


@r_pay.callback_query(F.data.startswith("pay:stars:"))
async def pay_stars(c: CallbackQuery):
    o, p = await _begin(c, "stars")
    if o:
        await _invoice(c, o, "XTR", o["stars"], None)


@r_pay.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    if q.invoice_payload.startswith("fund:"):
        ok, err = q.from_user.id == OWNER_ID, "Faqat owner uchun"
    else:
        try:
            ok, err = await validate_precheckout(q.from_user.id, q.invoice_payload, q.currency, q.total_amount)
        except Exception:
            ref = error_ref()
            log.exception("pre_checkout xatosi ref=%s", ref)
            ok, err = False, f"Vaqtincha xatolik. Ref: {ref}"
    await q.answer(ok=ok, error_message=None if ok else err)


@r_pay.message(F.successful_payment)
async def paid(m: Message):
    await handle_successful_payment(m.bot, m)


# ------------------------------------------------------------------ balans va TON

@r_pay.callback_query(F.data.startswith("pay:wallet:"))
async def pay_wallet(c: CallbackQuery):
    o = await _mine(c, int(c.data.split(":")[2]))
    if not o:
        return
    res = await pay_with_wallet(c.bot, o["id"], c.from_user.id)
    if res.status in ("confirmed", "duplicate"):
        await c.answer("✅ To'landi")
        try:
            await c.message.delete()
        except Exception:
            pass
    elif res.reason == "insufficient_balance":
        await c.answer("Balans yetarli emas. Avval hamyonni to'ldiring.", show_alert=True)
    else:
        await c.answer("Buyurtmani to'lab bo'lmadi", show_alert=True)


@r_pay.callback_query(F.data.startswith("pay:ton:"))
async def pay_ton(c: CallbackQuery):
    o, p = await _begin(c, "ton")
    if not o:
        return
    text = (f"💠 <b>TON orqali to'lov</b> — {o['order_code']}\n\n1️⃣ Manzil:\n<code>{esc(await S('ton_address'))}</code>\n"
            f"2️⃣ Miqdor: <code>{o['ton_nano'] / 1e9:.3f}</code> TON\n"
            f"3️⃣ Izoh (comment) <b>albatta</b> yozing:\n<code>{o['order_code']}</code>\n\n"
            "To'lagach «Tekshirish» tugmasini bosing (yoki kuting — bot o'zi aniqlaydi). Izohsiz to'lov avtomatik topilmaydi.")
    await screen(c, "wallet", text, K([B("✅ To'lovni tekshirish", f"pay:chk:{o['id']}")], [B("✖️ Bekor", f"ordx:{o['id']}")]))


@r_pay.callback_query(F.data.startswith("pay:chk:"))
async def pay_check(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    o = await get_order(oid, c.from_user.id)
    if not o:
        return await c.answer("Topilmadi", show_alert=True)
    if o["payment_status"] == "paid":
        return await c.answer("✅ To'lov allaqachon qabul qilingan", show_alert=True)
    r = await ton_check_order(c.bot, oid)
    if r == "paid":
        await c.answer("✅ To'lov topildi")
        try:
            await c.message.delete()
        except Exception:
            pass
    elif r == "error":
        await c.answer("Tekshirib bo'lmadi, birozdan so'ng qayta urinib ko'ring.", show_alert=True)
    else:
        await c.answer("Hali to'lov topilmadi. 1-2 daqiqadan so'ng qayta tekshiring.", show_alert=True)


# ------------------------------------------------------------------ buyurtmalar tarixi / chek

@r_pay.callback_query(F.data == "m:orders")
async def orders_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await user_orders(c.from_user.id, 10)
    if not rows:
        return await screen(c, "wallet", "🧾 <b>Buyurtmalarim</b>\n\nHozircha buyurtma yo'q.", K([HOME]))
    kb = [[B(f"{STATUS_ICON.get(o['payment_status'], '•')} {o['order_code']} · {o['product_title'][:24]}", f"ord:{o['id']}")] for o in rows]
    kb.append([HOME])
    await screen(c, "wallet", "🧾 <b>Buyurtmalarim</b> (oxirgi 10 ta)", K(*kb))


@r_pay.callback_query(F.data.startswith("ord:"))
async def order_detail(c: CallbackQuery):
    o = await get_order(int(c.data[4:]), c.from_user.id)
    if not o:
        return await c.answer("Topilmadi", show_alert=True)
    rows = []
    if o["payment_status"] == "pending":
        rows.append([B("💳 To'lashni davom ettirish", f"ordp:{o['id']}"), B("✖️ Bekor qilish", f"ordx:{o['id']}")])
    rows.append([B("⬅️ Buyurtmalarim", "m:orders"), HOME])
    await screen(c, "wallet", receipt_text(o), K(*rows))


@r_pay.callback_query(F.data.startswith("ordp:"))
async def order_pay_again(c: CallbackQuery):
    o = await _mine(c, int(c.data[5:]))
    if o:
        await checkout(c, o)


@r_pay.callback_query(F.data.startswith("ordx:"))
async def order_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    ok = await cancel_order(int(c.data[5:]), c.from_user.id)
    await c.answer("Buyurtma bekor qilindi" if ok else "Buyurtmani bekor qilib bo'lmaydi")
    await orders_list(c, state)


##############################################################################
# app/handlers/games.py
##############################################################################
r_games = Router()
r_games.message.filter(F.chat.type == "private")


async def _pack(pack_id):
    return await one(
        "SELECT p.*, g.name gname, g.emoji, g.server, g.id_label FROM packs p JOIN games g ON g.id=p.game_id "
        "WHERE p.id=? AND p.active=1 AND p.price_uzs>0 AND g.active=1", pack_id)


@r_games.message(Command("donat"))
@r_games.callback_query(F.data == "g:list")
async def games_menu(ev, state: FSMContext):
    await state.clear()
    games = await q("SELECT * FROM games g WHERE g.active=1 AND EXISTS "
                    "(SELECT 1 FROM packs p WHERE p.game_id=g.id AND p.active=1 AND p.price_uzs>0) ORDER BY g.id")
    if not games:
        return await screen(ev, "shop", "🎮 <b>O'yin donati</b>\n\nHozircha mavjud emas.", K([HOME]))
    rows = [[B(f"{g['emoji']} {g['name']}", f"g:g:{g['id']}")] for g in games] + [[HOME]]
    await screen(ev, "shop", "🎮 <b>O'yin donati</b>\n\nO'yinni tanlang 👇", K(*rows))


@r_games.callback_query(F.data.startswith("g:g:"))
async def game_packs(c: CallbackQuery):
    gid = int(c.data.split(":")[2])
    g = await one("SELECT * FROM games WHERE id=? AND active=1", gid)
    packs = await q("SELECT * FROM packs WHERE game_id=? AND active=1 AND price_uzs>0 ORDER BY price_uzs", gid)
    if not g or not packs:
        return await c.answer("Hozircha mavjud emas", show_alert=True)
    rows = [[B(f"{p['title']} — {fmt_uzs(p['price_uzs'])}", f"g:p:{p['id']}")] for p in packs]
    rows.append([B("⬅️ Orqaga", "g:list"), HOME])
    await screen(c, "shop", f"{g['emoji']} <b>{esc(g['name'])}</b>\n\nPaketni tanlang 👇", K(*rows))


async def begin_game(ev, state: FSMContext, pack_id: int):
    p = await _pack(pack_id)
    if not p:
        return await screen(ev, "shop", "Bu paket hozircha mavjud emas.", K([B("🎮 O'yinlar", "g:list")]))
    await state.clear()
    await state.set_state(GameBuy.pid)
    await state.update_data(pack=pack_id)
    saved = await one("SELECT * FROM gids WHERE user_id=? AND game_id=?", ev.from_user.id, p["game_id"])
    rows = []
    if saved and (not p["server"] or saved["server"]):
        last = saved["pid"] + (f" ({saved['server']})" if p["server"] else "")
        rows.append([B(f"♻️ Oxirgi ID: {last}", "g:last")])
    rows.append([B("✖️ Bekor qilish", "x:cancel")])
    await screen(ev, "shop",
                 f"{p['emoji']} <b>{esc(p['gname'])}</b> — {esc(p['title'])}\n💳 {fmt_uzs(p['price_uzs'])}\n\n"
                 f"🆔 <b>{esc(p['id_label'])}</b> ni yuboring (faqat raqam):", K(*rows))


@r_games.callback_query(F.data.startswith("g:p:"))
async def pick_pack(c: CallbackQuery, state: FSMContext):
    await begin_game(c, state, int(c.data.split(":")[2]))


@r_games.message(GameBuy.pid, F.text)
async def got_pid(m: Message, state: FSMContext):
    pid = m.text.strip().replace(" ", "")
    if not pid.isdigit() or not 5 <= len(pid) <= 16:
        return await m.answer("⚠️ ID faqat raqamlardan iborat bo'lishi kerak (5–16 ta). Qayta yuboring.", reply_markup=cancel_kb())
    d = await state.get_data()
    p = await _pack(d.get("pack"))
    if not p:
        await state.clear()
        return await m.answer("Bu paket hozircha mavjud emas.", reply_markup=K([B("🎮 O'yinlar", "g:list")]))
    await state.update_data(pid=pid, srv="")
    if p["server"]:
        await state.set_state(GameBuy.srv)
        return await m.answer("🌐 <b>Server ID</b> ni yuboring (ID yonidagi qavs ichidagi raqam, masalan <code>2001</code>):",
                              reply_markup=cancel_kb())
    await confirm(m, state)


@r_games.message(GameBuy.srv, F.text)
async def got_srv(m: Message, state: FSMContext):
    srv = m.text.strip().replace(" ", "")
    if not srv.isdigit() or not 1 <= len(srv) <= 6:
        return await m.answer("⚠️ Server ID faqat raqam (1–6 ta). Qayta yuboring.", reply_markup=cancel_kb())
    await state.update_data(srv=srv)
    await confirm(m, state)


@r_games.callback_query(GameBuy.pid, F.data == "g:last")
async def use_last(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    p = await _pack(d.get("pack"))
    saved = await one("SELECT * FROM gids WHERE user_id=? AND game_id=?", c.from_user.id, p["game_id"]) if p else None
    if not p or not saved:
        return await c.answer("Saqlangan ID topilmadi", show_alert=True)
    await state.update_data(pid=saved["pid"], srv=saved["server"] if p["server"] else "")
    await confirm(c, state)


async def confirm(ev, state: FSMContext):
    d = await state.get_data()
    p = await _pack(d.get("pack"))
    if not p or not d.get("pid"):
        await state.clear()
        return await screen(ev, "shop", "Qaytadan boshlang.", K([B("🎮 O'yinlar", "g:list")]))
    text = (f"✅ <b>Buyurtmani tekshiring</b>\n\n🎮 {esc(p['gname'])} — {esc(p['title'])}\n"
            f"🆔 {esc(p['id_label'])}: <code>{esc(d['pid'])}</code>\n"
            + (f"🌐 Server: <code>{esc(d['srv'])}</code>\n" if p["server"] else "")
            + f"💳 Narx: <b>{fmt_uzs(p['price_uzs'])}</b>\n\n"
            "⚠️ ID noto'g'ri bo'lsa, donat boshqa akkauntga tushib ketishi mumkin. Diqqat bilan tekshiring!")
    rows = [[B("✅ Tasdiqlash va to'lash", "g:ok")], [B("✏️ ID ni o'zgartirish", f"g:p:{p['id']}")],
            [B("✖️ Bekor qilish", "x:cancel")]]
    await screen(ev, "shop", text, K(*rows))


@r_games.callback_query(F.data == "g:ok")
async def confirm_ok(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    p = await _pack(d.get("pack")) if d.get("pack") else None
    if not p or not d.get("pid"):
        await state.clear()
        return await c.answer("Qaytadan boshlang", show_alert=True)
    srv = d.get("srv", "") if p["server"] else ""
    await run("INSERT INTO gids(user_id,game_id,pid,server) VALUES(?,?,?,?) "
              "ON CONFLICT(user_id,game_id) DO UPDATE SET pid=excluded.pid, server=excluded.server",
              c.from_user.id, p["game_id"], d["pid"], srv)
    o = await create_order(c.from_user.id, "game", p["id"], meta=f"{d['pid']}|{srv}")
    await state.clear()
    if not o:
        return await c.answer("Paket mavjud emas", show_alert=True)
    await checkout(c, o)


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
    if arg.startswith("res_") and arg[4:].isdigit():
        b = await one("SELECT * FROM battles WHERE id=?", int(arg[4:]))
        if b:
            return await screen(m, "top", await BV.results_text(RO, b, m.from_user.id, fmt_ts),
                                K([B("📊 TOP 10", f"bx:top:{b['id']}"), B("👤 Mening natijam", f"bx:me:{b['id']}")], [HOME]))
    if arg.startswith("fb_") and arg[3:].isdigit():
        return await g_fb_begin(m, state, int(arg[3:]))
    if arg.startswith("buy_") and arg[4:].isdigit():
        return await show_item(m, int(arg[4:]))
    if arg.startswith("game_") and arg[5:].isdigit():
        return await begin_game(m, state, int(arg[5:]))
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
    kb.append([B("🏆 Natijalar", f"bx:res:{bid}"), B("📊 TOP 10", f"bx:top:{bid}")])
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
    if b["btype"] == "contest":                      # Contest: nik kerak emas, ism bilan 1 bosishda qatnashadi
        return await _register(ev, state, ev.from_user.full_name if len(ev.from_user.full_name) >= 2 else f"user{ev.from_user.id}")
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
    try:
        async with tx() as t:
            res = await BE.join(t, bid, ev.from_user.id, nick)
            if res == "exists":                       # dublikat yaratilmaydi, faqat nik yangilanadi
                await t.execute("UPDATE participants SET nick=? WHERE battle_id=? AND user_id=?", nick, bid, ev.from_user.id)
    except BE.BattleError as e:
        return await screen(ev, "battle", str(e), K([B("🔥 Faol batllar", "m:battles")], [HOME]))
    if res == "exists":
        await screen(ev, "battle", f"⚠️ Siz allaqachon bu Battle'da qatnashgansiz.\nNik yangilandi: <b>{esc(nick)}</b>",
                     K([B("🏆 Reyting", f"v:{bid}")], [HOME]))
        return await schedule_post_update(ev.bot, bid)
    await schedule_post_update(ev.bot, bid)
    if b["btype"] == "nft":
        await state.set_state(NftE.photo)
        await state.update_data(bid=bid)
        return await screen(ev, "battle", f"✅ Ro'yxatdan o'tdingiz!\n\n🖼 Endi <b>NFT rasmini</b> yuboring.\nIzohda: <code>Nomi | Kolleksiya | Noyoblik</code>",
                            cancel_kb())
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
    await checkout(c, await create_order(c.from_user.id, "vip"))


# ------------------------------------------------------------------ hamyon

@r_user.message(Command("wallet"))
@r_user.callback_query(F.data == "m:wallet")
async def wallet(ev, state: FSMContext):
    await state.clear()
    u = await one("SELECT * FROM users WHERE id=?", ev.from_user.id)
    addr = f"<code>{esc(u['wallet'])}</code>" if u["wallet"] else "ulanmagan"
    text = (f"👛 <b>Hamyon</b>\n\n💰 Bot balansi: <b>{u['balance']}⭐</b>\n💠 TON hamyon: {addr}\n\n"
            "Balans bilan batl krediti, VIP va do'kon xaridlarini to'lash mumkin.")
    rows = [[B("➕ Balansni to'ldirish", "w:top"), B("🧾 Buyurtmalarim", "m:orders")]]
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
    rate = await SI("uzs_rate", 230)
    rows = [[B(f"{a}⭐ · {fmt_uzs(calc_uzs(a, rate))}", f"w:t:{a}")] for a in amounts]
    rows.append([B("⬅️ Orqaga", "m:wallet")])
    await screen(c, "wallet", "➕ <b>Balansni to'ldirish</b>\n\nMiqdorni tanlang (so'mda to'lash mumkin):", K(*rows))


@r_user.callback_query(F.data.startswith("w:t:"))
async def topup_go(c: CallbackQuery):
    await checkout(c, await create_order(c.from_user.id, "topup", stars=int(c.data.split(":")[2])))


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


def price_label(it, rate: int) -> str:
    return fmt_uzs(calc_uzs(it["price_stars"], rate))


@r_shop.message(Command("shop"))
@r_shop.callback_query(F.data == "m:shop")
async def shop(ev, state: FSMContext):
    await state.clear()
    rows = [[B("🎮 O'yin donati", "g:list")],
            [B(KINDS[0][1], "s:k:premium"), B(KINDS[1][1], "s:k:stars")],
            [B(KINDS[2][1], "s:k:gift"), B(KINDS[3][1], "s:k:nft")], [HOME]]
    await screen(ev, "shop", "🛍 <b>Do'kon</b>\n\nStars, Premium, Giftlar va NFT — bitta joyda.\nBo'limni tanlang 👇", K(*rows))


@r_shop.callback_query(F.data.startswith("s:k:"))
async def category(c: CallbackQuery):
    kind = c.data.split(":")[2]
    items = await q("SELECT * FROM items WHERE kind=? AND active=1 ORDER BY price_stars LIMIT 40", kind)
    title = dict(KINDS).get(kind, "Do'kon")
    if not items:
        return await screen(c, "shop", f"{title}\n\nHozircha mahsulot yo'q.", K([B("⬅️ Orqaga", "m:shop")]))
    rate = await SI("uzs_rate", 230)
    rows = [[B(f"{i['title']} — {price_label(i, rate)}", f"s:i:{i['id']}")] for i in items]
    rows.append([B("⬅️ Orqaga", "m:shop")])
    await screen(c, "shop", f"<b>{title}</b>\n\nMahsulotni tanlang 👇", K(*rows))


async def show_item(ev, iid: int):
    it = await one("SELECT * FROM items WHERE id=? AND active=1", iid)
    if not it:
        return await screen(ev, "shop", "Mahsulot topilmadi.", K([B("🛍 Do'kon", "m:shop")]))
    uzs = fmt_uzs(calc_uzs(it["price_stars"], await SI("uzs_rate", 230)))
    text = f"<b>{esc(it['title'])}</b>\n\n{esc(it['descr'])}\n\n💳 Narx: <b>{uzs}</b>"
    cost = item_cost(it["kind"], it["payload"], it["title"])
    if cost is not None:
        bal = await bot_stars(ev.bot)
        if bal is not None:
            text += ("\n\n⚡️ To'lovdan so'ng avtomatik yetkaziladi." if bal >= cost
                     else "\n\n⏳ Yetkazish qo'lda amalga oshiriladi (admin tasdiqlashi bilan).")
    elif it["kind"] == "stars":
        text += "\n\nℹ️ Stars to'lov tasdiqlangach admin tomonidan sizning akkauntingizga yuboriladi."
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
    await checkout(c, await create_order(c.from_user.id, it["kind"], iid))


##############################################################################
# app/handlers/battle.py
##############################################################################
r_battle = Router()
r_battle.message.filter(F.chat.type == "private")

USER_CMDS = [("start", "Boshlash"), ("donate", "Donat"), ("menu", "Asosiy menyu"), ("battles", "Faol batllar"),
             ("donat", "O'yin donati"), ("shop", "Do'kon"), ("wallet", "Hamyon"), ("profile", "Profil"), ("help", "Yordam")]
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


async def can(b, uid: int, perm: str) -> bool:
    """Rol asosidagi huquq tekshiruvi (egasi / admin / moderator). Har bir amalda serverda qayta chaqiriladi."""
    return await BR.can(RO, b, uid, perm, OWNER_ID)


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
        return await checkout(ev, await create_order(uid, "battle_credit"))
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
            return await checkout(c, await create_order(uid, "battle_credit"))
    end_ts = int(time.time()) + d["hours"] * 3600 if d["hours"] else 0
    bid, _ = await run("INSERT INTO battles(owner_id,channel_id,title,rules,points,end_ts,created,btype) VALUES(?,?,?,?,?,?,?,?)",
                       uid, ch["id"], d["title"], d["rules"], d["points"], end_ts, int(time.time()), d.get("btype", "vote"))
    await run("INSERT OR IGNORE INTO battle_settings(battle_id,comment_score) VALUES(?,?)", bid, d["points"])
    async with tx() as t:
        await BE.log_event(t, "battle.created", battle_id=bid, user_id=uid, meta=d.get("btype", "vote"))
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
    rows = await q("SELECT id,title,state FROM battles WHERE owner_id=? OR id IN (SELECT battle_id FROM battle_admins WHERE user_id=?) "
                   "ORDER BY CASE state WHEN 'ACTIVE' THEN 0 WHEN 'PAUSED' THEN 1 WHEN 'CANCELLED' THEN 3 ELSE 2 END, id DESC LIMIT 20",
                   ev.from_user.id, ev.from_user.id)
    if not rows:
        return await screen(ev, "battle", "📂 Sizda hali batl yo'q.", K([B("⚔️ Batl yaratish", "m:new")], [HOME]))
    icon = {"ACTIVE": "🟢", "PAUSED": "⏸", "CANCELLED": "❌"}
    kb = [[B(f"{icon.get(r['state'], '🏁')} {r['title'][:32]}", f"b:{r['id']}")] for r in rows]
    await screen(ev, "battle", "📂 <b>Mening batllarim</b>", K(*kb, [HOME]))


async def manage(ev, state, bid: int, prefix: str = ""):
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    uid = ev.from_user.id
    role = await BR.role_of(RO, b, uid, OWNER_ID)
    if not role:
        return await ev.answer("Ruxsat yo'q", show_alert=True) if isinstance(ev, CallbackQuery) else None
    perms = BR.PERMS[role]
    text = prefix + await BV.dash_text(RO, b, fmt_ts) + f"\n🔑 Sizning rolingiz: {BR.LABEL[role]}"
    rows = []
    live = b["state"] in ("ACTIVE", "PAUSED", "REGISTRATION")
    if live:
        if b["btype"] != "contest" and b["state"] == "ACTIVE":
            if "round" in perms:
                rows.append([B("➕ Yangi raund (post)", f"b:r:{bid}")])
            rows.append([B("🏆 Reyting", f"b:lb:{bid}")] + ([B("➕ Ball berish", f"b:p:{bid}")] if "score" in perms else []))
        rows.append([B("📊 Natijalar", f"bx:res:{bid}")] + ([B("⚙️ Sozlamalar", f"bx:set:{bid}")] if "settings" in perms else []))
        if "state" in perms:
            rows.append([B("⏸ Pauza" if b["state"] == "ACTIVE" else "▶️ Davom ettirish", f"bx:pz:{bid}"),
                         B("🎲 G'olibni tanlash" if b["btype"] == "contest" else "🏁 Yakunlash", f"b:f:{bid}")])
        rows.append([B("📜 Audit", f"bx:au:{bid}")] + ([B("❌ Bekor qilish", f"bx:cx:{bid}")] if "cancel" in perms else []))
        if "roles" in perms:
            rows.append([B("👥 Adminlar va rollar", f"bx:rl:{bid}")])
    else:
        rows.append([B("🏆 Natijalar", f"bx:res:{bid}"), B("📜 Audit", f"bx:au:{bid}")])
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
    if not await can(b, c.from_user.id, "view"):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    rows = await leaderboard(bid)
    await screen(c, "top", f"🏆 <b>{esc(b['title'])}</b>\n\n{lb_text(rows) or 'Hali ishtirokchi yo‘q'}",
                 K([B("⬅️ Orqaga", f"b:{bid}")]))


# ------------------------------------------------------------------ raund

@r_battle.callback_query(F.data.startswith("b:r:"))
async def rnd_start(c: CallbackQuery, state: FSMContext):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not await can(b, c.from_user.id, "round") or b["status"] != "active":
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
    if not await can(b, c.from_user.id, "round") or b["status"] != "active":
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
    if not await can(b, c.from_user.id, "score"):
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
    if not await can(b, c.from_user.id, "score"):
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
    if not await can(b, m.from_user.id, "score"):
        return await state.clear()
    delta = int(t)
    try:
        async with tx() as tt:
            s = await BE.get_settings(tt, d["bid"])
            if delta < 0 and not (await BR.role_of(RO, b, m.from_user.id, OWNER_ID) == "owner" or s["allow_negative"]):
                raise BE.BattleError("❌ Manfiy ball berish uchun ruxsat yo'q (Sozlamalarda yoqing).")
            rc = await BE.add_score(tt, d["bid"], d["uid"], "manual" if delta > 0 else "penalty", delta,
                                    f"manual:{m.chat.id}:{m.message_id}", actor=m.from_user.id, reason="manual")
            await tt.execute("INSERT INTO plog(battle_id,user_id,delta,reason,ts) VALUES(?,?,?,?,?)",
                             d["bid"], d["uid"], delta, "manual", int(time.time()))
    except BE.BattleError as e:
        await state.clear()
        return await m.answer(str(e), reply_markup=K([B("⬅️ Orqaga", f"b:{d['bid']}")]))
    if rc:
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
    if not await can(b, c.from_user.id, "state"):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await screen(c, "battle", f"🏁 <b>{esc(b['title'])}</b> batlini yakunlaymizmi?\nNatijalar kanalga e'lon qilinadi.",
                 K([B("✅ Ha, yakunlash", f"b:ff:{bid}")], [B("⬅️ Yo'q", f"b:{bid}")]))


@r_battle.callback_query(F.data.startswith("b:ff:"))
async def fin_go(c: CallbackQuery, state: FSMContext):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not await can(b, c.from_user.id, "state"):
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

    # 3) ball: engine — UNIQUE(battle_id, idem_key) + tranzaksiya; raund yopilgan/battle faol emas bo'lsa rad etiladi
    try:
        async with tx() as t:
            st = await BE.get_settings(t, r["battle_id"])
            n = (await t.one("SELECT COUNT(*) c FROM battle_score_events WHERE battle_id=? AND round_id=? AND user_id=? AND source='comment'",
                             r["battle_id"], r["id"], u.id))["c"]
            if n >= max(1, st["comments_per_round"]):
                return
            await BE.add_score(t, r["battle_id"], u.id, "comment", r["points"], f"c:{r['id']}:{m.message_id}", round_id=r["id"])
            await t.execute("INSERT OR IGNORE INTO scored(round_id,user_id) VALUES(?,?)", r["id"], u.id)
    except BE.BattleError:
        return
    await run("INSERT INTO plog(battle_id,user_id,delta,reason,ts) VALUES(?,?,?,?,?)",
              r["battle_id"], u.id, r["points"], f"round:{r['id']}", int(time.time()))
    await schedule_post_update(m.bot, r["battle_id"])


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
    "uzs_rate": "Kurs: 1⭐ = necha so'm (UZS narxlar shundan hisoblanadi)",
    "card_number": "Karta raqami (so'mda to'lov qabul qilish)",
    "card_holder": "Karta egasi (F.I.Sh)",
    "provider_token": "Payme/Click provayder tokeni (BotFather → Payments), ixtiyoriy",
    "stars_warn": "Bot Stars balansi 🟡 ogohlantirish chegarasi (⭐)",
    "stars_crit": "Bot Stars balansi 🔴 kritik chegarasi (⭐)",
}
NUMERIC = {"battle_fee", "vip_price", "vip_days", "ton_rate", "markup", "ref_per_credit", "uzs_rate", "stars_warn", "stars_crit"}
CLEARABLE = {"force_sub", "ton_address", "card_number", "card_holder", "provider_token"}
SECRET = {"provider_token"}
IMAGES = {"home": "Bosh menyu", "battle": "Batllar", "shop": "Do'kon", "wallet": "Hamyon",
          "profile": "Profil", "owner": "Owner panel", "vip": "VIP", "ref": "Taklif", "top": "Reyting"}
KIND_NAMES = {"premium": "💎 Premium", "stars": "⭐ Stars", "gift": "🎁 Gift", "nft": "🖼 NFT"}


@r_owner.message(Command("owner"))
@r_owner.callback_query(F.data == "o:home")
async def panel(ev, state: FSMContext):
    await state.clear()
    rows = [[B("💹 Moliya", "o:fin"), B("🧾 Buyurtmalar", "o:ord")],
            [B("🌟 Bot Stars", "o:bs"), B("⚠️ Xatolar", "o:err")],
            [B("⚙️ Sozlamalar / narxlar", "o:set"), B("🛍 Do'kon boshqaruvi", "o:shop")],
            [B("🎮 O'yinlar donati", "o:gm"), B("📊 Statistika", "o:stats")],
            [B("🖼 Rasmlar", "o:img"), B("📣 Xabar yuborish", "o:bc")],
            [B("🏠 Menyu", "m:home")]]
    await screen(ev, "owner", "🛠 <b>Owner panel</b>\n\nBarcha narxlar, rasmlar va do'kon shu yerdan boshqariladi.", K(*rows))


# ------------------------------------------------------------------ sozlamalar

@r_owner.callback_query(F.data == "o:set")
async def settings(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = []
    for k, label in SETTINGS.items():
        v = await S(k)
        shown = ("✅ o'rnatilgan" if v else "—") if k in SECRET else (v[:18] or "—")
        rows.append([B(f"{label.split(' (')[0]}: {shown}", f"o:s:{k}")])
    rows.append([B("⬅️ Orqaga", "o:home")])
    await screen(c, "owner", "⚙️ <b>Sozlamalar</b>\nO'zgartirish uchun bosing 👇", K(*rows))


@r_owner.callback_query(F.data.startswith("o:s:"))
async def setting_pick(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    if key not in SETTINGS:
        return await c.answer()
    await state.set_state(OSet.val)
    await state.update_data(key=key)
    cur = ("✅ o'rnatilgan" if await S(key) else "—") if key in SECRET else (esc(await S(key)) or "—")
    await screen(c, "owner", f"✏️ <b>{SETTINGS[key]}</b>\nHozirgi qiymat: <code>{cur}</code>\n\nYangi qiymatni yuboring"
                 + (" (o'chirish uchun <code>-</code>)" if key in CLEARABLE else "") + ":", cancel_kb())


@r_owner.message(OSet.val, F.text)
async def setting_val(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    v = m.text.strip()
    if v == "-" and key in CLEARABLE:
        v = ""
    if key in SECRET:  # token yozilgan xabarni chatdan o'chiramiz
        try:
            await m.delete()
        except Exception:
            pass
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
    cost = item_cost(i["kind"], i["payload"], i["title"])
    if cost is not None:
        profit = i["price_stars"] - cost
        text += (f"\n\n💵 Tannarx (botdan yechiladi): <b>{cost}⭐</b>\n"
                 + (f"📈 Foyda: <b>{profit}⭐</b> ({round(profit * 100 / cost)}%)" if profit >= 0
                    else f"🚨 <b>ZARAR: {profit}⭐</b> — narxni oshiring!"))
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


# ------------------------------------------------------------------ o'yinlar donati

def parse_packs(text: str):
    """Har qatorda: «60 UC | 12000» (ajratgich | yoki =). Xato bo'lsa None."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(.+?)\s*[|=;]\s*([\d\s.,]+)$", line)
        if not m:
            return None
        title, price = m.group(1).strip()[:40], int(re.sub(r"\D", "", m.group(2)) or 0)
        if not title or price <= 0:
            return None
        out.append((title, price))
    return out or None


@r_owner.callback_query(F.data == "o:gm")
async def games_admin(c: CallbackQuery, state: FSMContext):
    await state.clear()
    games = await q("SELECT * FROM games ORDER BY id")
    rows = [[B(f"{'✅' if g['active'] else '🚫'} {g['emoji']} {g['name']}", f"o:g:{g['id']}")] for g in games]
    rows += [[B("➕ O'yin qo'shish", "o:ga")], [B("⬅️ Orqaga", "o:home")]]
    await screen(c, "owner", "🎮 <b>O'yinlar donati</b>\n\nO'yin va paketlarni, ularning narxini (so'mda) shu yerdan belgilaysiz. "
                 "Narxi qo'yilmagan paket foydalanuvchiga ko'rinmaydi.", K(*rows))


@r_owner.callback_query(F.data.startswith("o:g:"))
async def game_view(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_game(c, int(c.data.split(":")[2]))


async def show_game(c: CallbackQuery, gid: int):
    g = await one("SELECT * FROM games WHERE id=?", gid)
    if not g:
        return await c.answer("Topilmadi")
    packs = await q("SELECT * FROM packs WHERE game_id=? ORDER BY price_uzs, id", gid)
    rows = []
    for p in packs:
        mark = "✅" if p["active"] and p["price_uzs"] else "🚫"
        price = fmt_uzs(p["price_uzs"]) if p["price_uzs"] else "⚠️ narx qo'yilmagan"
        rows.append([B(f"{mark} {p['title']} — {price}", f"o:pk:{p['id']}")])
    rows += [[B("➕ Paket(lar) qo'shish", f"o:pa:{gid}")],
             [B("🔁 Yoq/o'chir", f"o:gt:{gid}"), B("🗑 O'yinni o'chirish", f"o:gd:{gid}")],
             [B("⬅️ Orqaga", "o:gm")]]
    srv = "ha (Server ID so'raladi)" if g["server"] else "yo'q"
    await screen(c, "owner", f"{g['emoji']} <b>{esc(g['name'])}</b>\nServer ID: {srv}\n\nPaketni bosib narxini o'zgartiring 👇", K(*rows))


@r_owner.callback_query(F.data.startswith("o:gt:"))
async def game_toggle(c: CallbackQuery, state: FSMContext):
    await run("UPDATE games SET active=1-active WHERE id=?", int(c.data.split(":")[2]))
    await games_admin(c, state)


@r_owner.callback_query(F.data.startswith("o:gd:"))
async def game_delete(c: CallbackQuery, state: FSMContext):
    gid = int(c.data.split(":")[2])
    await run("DELETE FROM packs WHERE game_id=?", gid)
    await run("DELETE FROM games WHERE id=?", gid)
    await games_admin(c, state)


@r_owner.callback_query(F.data == "o:ga")
async def game_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(OGame.name)
    await screen(c, "owner", "🎮 O'yin <b>nomini</b> yuboring (masalan: <code>Standoff 2</code>, <code>Genshin Impact</code>):", cancel_kb())


@r_owner.message(OGame.name, F.text)
async def game_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:40])
    await state.set_state(OGame.server)
    await m.answer("Bu o'yinda donat uchun <b>Server ID</b> ham kerakmi? (Mobile Legends kabi)",
                   reply_markup=K([B("Ha, kerak", "o:gs:1"), B("Yo'q", "o:gs:0")], [B("✖️ Bekor qilish", "x:cancel")]))


@r_owner.callback_query(OGame.server, F.data.startswith("o:gs:"))
async def game_server(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    server = int(c.data.split(":")[2])
    gid, _ = await run("INSERT INTO games(name,server,id_label) VALUES(?,?,?)", d["name"], server,
                       "User ID" if server else "Player ID")
    await state.clear()
    await show_game(c, gid)


@r_owner.callback_query(F.data.startswith("o:pa:"))
async def packs_add(c: CallbackQuery, state: FSMContext):
    gid = int(c.data.split(":")[2])
    await state.set_state(OPack.bulk)
    await state.update_data(gid=gid)
    await screen(c, "owner",
                 "➕ <b>Paketlar</b>\n\nHar bir qatorda <code>nomi | narxi (so'm)</code> yozing. Bir nechta qator yuborsangiz hammasi qo'shiladi:\n\n"
                 "<code>60 UC | 12000\n325 UC | 62000\n660 UC | 120000</code>", cancel_kb())


@r_owner.message(OPack.bulk, F.text)
async def packs_bulk(m: Message, state: FSMContext):
    items = parse_packs(m.text)
    if not items:
        return await m.answer("⚠️ Format noto'g'ri. Har qatorda: <code>60 UC | 12000</code>", reply_markup=cancel_kb())
    gid = (await state.get_data())["gid"]
    for title, price in items:
        ex = await one("SELECT id FROM packs WHERE game_id=? AND title=?", gid, title)
        if ex:
            await run("UPDATE packs SET price_uzs=?, active=1 WHERE id=?", price, ex["id"])
        else:
            await run("INSERT INTO packs(game_id,title,price_uzs) VALUES(?,?,?)", gid, title, price)
    await state.clear()
    await m.answer(f"✅ {len(items)} ta paket saqlandi.", reply_markup=K([B("🎮 O'yinga qaytish", f"o:g:{gid}")]))


@r_owner.callback_query(F.data.startswith("o:pk:"))
async def pack_view(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_pack(c, int(c.data.split(":")[2]))


async def show_pack(c: CallbackQuery, pid: int):
    p = await one("SELECT p.*, g.name gname FROM packs p JOIN games g ON g.id=p.game_id WHERE p.id=?", pid)
    if not p:
        return await c.answer("Topilmadi")
    price = fmt_uzs(p["price_uzs"]) if p["price_uzs"] else "⚠️ qo'yilmagan"
    await screen(c, "owner", f"📦 <b>{esc(p['gname'])}</b> — {esc(p['title'])}\nNarx: <b>{price}</b>\nHolat: {'faol' if p['active'] else 'o‘chirilgan'}",
                 K([B("💰 Narxni o'zgartirish", f"o:pp:{pid}")], [B("🔁 Yoq/o'chir", f"o:ptg:{pid}"), B("🗑 O'chirish", f"o:pd:{pid}")],
                   [B("⬅️ Orqaga", f"o:g:{p['game_id']}")]))


@r_owner.callback_query(F.data.startswith("o:pp:"))
async def pack_price(c: CallbackQuery, state: FSMContext):
    await state.set_state(OPack.price)
    await state.update_data(pid=int(c.data.split(":")[2]))
    await screen(c, "owner", "💰 Yangi narxni so'mda yuboring (masalan: <code>12000</code>):", cancel_kb())


@r_owner.message(OPack.price, F.text)
async def pack_price_in(m: Message, state: FSMContext):
    digits = re.sub(r"\D", "", m.text)
    if not digits or int(digits) <= 0:
        return await m.answer("⚠️ Musbat son yuboring.", reply_markup=cancel_kb())
    pid = (await state.get_data())["pid"]
    await run("UPDATE packs SET price_uzs=?, active=1 WHERE id=?", int(digits), pid)
    await state.clear()
    await m.answer(f"✅ Narx: {fmt_uzs(int(digits))}", reply_markup=K([B("📦 Paketga qaytish", f"o:pk:{pid}")]))


@r_owner.callback_query(F.data.startswith("o:ptg:"))
async def pack_toggle(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[2])
    await run("UPDATE packs SET active=1-active WHERE id=?", pid)
    await show_pack(c, pid)


@r_owner.callback_query(F.data.startswith("o:pd:"))
async def pack_delete(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[2])
    p = await one("SELECT game_id FROM packs WHERE id=?", pid)
    await run("DELETE FROM packs WHERE id=?", pid)
    if p:
        await show_game(c, p["game_id"])
    else:
        await games_admin(c, state)


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
    uid, amt = int(a[0]), int(a[1])
    if not await one("SELECT 1 FROM users WHERE id=?", uid):
        return await m.answer("❌ Foydalanuvchi topilmadi")
    async with tx() as t:
        ins = await post(t, user_id=uid, type_="ADJUSTMENT", account="wallet", amount=amt, currency="XTR", provider="admin",
                         provider_id=str(m.from_user.id), idem=f"adj:{m.message_id}:{uid}:{int(time.time())}",
                         meta=f"admin={m.from_user.id}")
        await audit("balance_adjustment", user_id=uid, admin_id=m.from_user.id, error=f"{amt:+d}", t=t)
    await m.answer("✅ Bajarildi (ledger'ga yozildi)" if ins else "Allaqachon bajarilgan")


@r_owner.message(Command("ban", "unban"))
async def ban(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        return await m.answer(f"Foydalanish: /{command.command} <user_id>")
    _, rc = await run("UPDATE users SET banned=? WHERE id=?", 1 if command.command == "ban" else 0, int(arg))
    await m.answer("✅ Bajarildi" if rc else "❌ Foydalanuvchi topilmadi")


##############################################################################
# app/handlers/donate.py
##############################################################################
r_donate = Router()
r_donate.message.filter(F.chat.type == "private")
PRESETS = (10, 25, 50, 100, 250, 500)


@r_donate.message(Command("donate"))
@r_donate.callback_query(F.data == "m:donate")
async def donate_menu(ev, state: FSMContext):
    await state.clear()
    rate = await SI("uzs_rate", 230)
    rows = [[B(f"💝 {a}⭐ · {fmt_uzs(calc_uzs(a, rate))}", f"d:{a}") for a in PRESETS[i:i + 2]] for i in range(0, len(PRESETS), 2)]
    rows += [[B("✍️ Boshqa summa", "d:custom")], [HOME]]
    await screen(ev, "wallet", "💝 <b>Donat</b>\n\nLoyihani qo'llab-quvvatlash uchun ixtiyoriy summa yuboring. "
                 "Donat — alohida tranzaksiya: u buyurtma/do'kon bilan aralashmaydi, chek beriladi.", K(*rows))


@r_donate.callback_query(F.data.regexp(r"^d:\d+$"))
async def donate_pick(c: CallbackQuery):
    o = await create_order(c.from_user.id, "donation", stars=int(c.data[2:]))
    if not o:
        return await c.answer("Summa noto'g'ri yoki juda ko'p ochiq buyurtma bor", show_alert=True)
    await checkout(c, o)


@r_donate.callback_query(F.data == "d:custom")
async def donate_custom(c: CallbackQuery, state: FSMContext):
    await state.set_state(Don.amount)
    await screen(c, "wallet", f"✍️ Donat miqdorini ⭐ da yuboring ({MIN_DONATION}–{MAX_DONATION}):", cancel_kb())


@r_donate.message(Don.amount, F.text)
async def donate_amount(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit() or not MIN_DONATION <= int(t) <= MAX_DONATION:
        return await m.answer(f"⚠️ {MIN_DONATION} dan {MAX_DONATION} gacha butun son yuboring.", reply_markup=cancel_kb())
    await state.clear()
    o = await create_order(m.from_user.id, "donation", stars=int(t))
    if not o:
        return await m.answer("Buyurtma yaratib bo'lmadi, keyinroq urinib ko'ring.", reply_markup=K([HOME]))
    await checkout(m, o)


##############################################################################
# app/admin/finance.py
##############################################################################
r_fin = Router()
r_fin.callback_query.filter(F.from_user.id == OWNER_ID)

PERIODS = {"d": ("Bugun", 86400), "w": ("7 kun", 7 * 86400), "m": ("30 kun", 30 * 86400), "a": ("Hammasi", 0)}
ICON = {"paid": "✅", "pending": "⏳", "review": "🔎", "failed": "❌", "expired": "⌛", "cancelled": "✖️", "refunded": "↩️"}


def _cur(d: dict) -> str:
    parts = [fmt_amount(v, c) for c, v in sorted(d.items()) if v]
    return " + ".join(parts) or "0"


# ------------------------------------------------------------------ hisobot

@r_fin.callback_query(F.data.regexp(r"^o:fin(:[dwma])?$"))
async def finance(c: CallbackQuery):
    key = c.data.split(":")[2] if c.data.count(":") == 2 else "d"
    label, span = PERIODS[key]
    since = int(time.time()) - span if span else 0
    r = await finance_report(since)
    st = await stars_status(c.bot)
    bal = f"{st['balance']}⭐" if st["balance"] is not None else "noma'lum"
    text = (f"💹 <b>Moliya — {label}</b>\n\n"
            f"💰 Tushum (sotuvlar): <b>{_cur(r['revenue'])}</b>\n"
            f"💝 Donatlar: <b>{_cur(r['donations'])}</b> ({r['donation_n']} ta)\n"
            f"🏦 Kassaga kelgan pul (qaytarishlarsiz emas, sof): {_cur(r['cash'])}\n"
            f"📦 To'langan sotuvlar: <b>{r['orders_n']}</b> ta (≈ {r['orders_stars']}⭐ ekvivalent)\n\n"
            f"💵 <b>Tannarx va foyda</b> (faqat aniq tannarx)\n"
            f"Premium/Gift: tushum {r['known_rev']}⭐ − tannarx {r['known_cost']}⭐ = <b>{r['known_rev'] - r['known_cost']}⭐</b> "
            f"({r['known_n']} ta)\n"
            f"❓ Tannarxi noma'lum: {r['unknown_n']} ta (≈ {r['unknown_stars']}⭐) — Stars/NFT/O'yin qo'lda yetkaziladi, "
            f"tannarx hisoblanmaydi\n↩️ Qaytarishlar: {r['refunds']} ta\n\n"
            f"🌟 Bot Stars: {st['level']} <b>{bal}</b> (kutilayotgan yetkazish uchun kerak: {st['need']}⭐)")
    rows = [[B(("• " if k == key else "") + v[0], f"o:fin:{k}") for k, v in PERIODS.items()],
            [B("🌟 Bot Stars", "o:bs"), B("⚠️ Xatolar", "o:err")], [B("⬅️ Orqaga", "o:home")]]
    await screen(c, "owner", text, K(*rows))


@r_fin.callback_query(F.data == "o:stats")
async def stats(c: CallbackQuery):
    day = int(time.time()) - 86400
    users = (await one("SELECT COUNT(*) c FROM users"))["c"]
    new = (await one("SELECT COUNT(*) c FROM users WHERE joined>?", day))["c"]
    ba = (await one("SELECT COUNT(*) c FROM battles WHERE status='active'"))["c"]
    bt = (await one("SELECT COUNT(*) c FROM battles"))["c"]
    pt = (await one("SELECT COUNT(*) c FROM participants"))["c"]
    pend = (await one("SELECT COUNT(*) c FROM orders WHERE bot_id=? AND payment_status='paid' AND delivery_status NOT IN "
                      "('delivered','none','cancelled')", BOT_ID))["c"]
    bad = await reconcile()
    text = (f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: <b>{users}</b> (24 soatda +{new})\n"
            f"⚔️ Batllar: <b>{ba}</b> faol / {bt} jami\n🙋 Ishtirokchilar: <b>{pt}</b>\n"
            f"📦 Yetkazilmagan buyurtmalar: <b>{pend}</b>\n"
            f"🧮 Ledger va balanslar: {'✅ mos' if not bad else f'⚠️ {len(bad)} ta nomuvofiqlik'}\n\n"
            "ℹ️ Stars'ni yechish: Telegram → bot sozlamalari → Balance (Fragment orqali).")
    rows = [[B("💹 Moliya", "o:fin")]]
    if bad:
        rows.insert(0, [B("🛠 Balanslarni ledger bo'yicha tuzatish", "o:rec")])
    rows.append([B("⬅️ Orqaga", "o:home")])
    await screen(c, "owner", text, K(*rows))


@r_fin.callback_query(F.data == "o:rec")
async def reconcile_fix(c: CallbackQuery):
    bad = await reconcile(fix=True)
    await c.answer(f"Tuzatildi: {len(bad)} ta", show_alert=True)
    await stats(c)


# ------------------------------------------------------------------ bot Stars monitori

@r_fin.callback_query(F.data == "o:bs")
async def bot_balance(c: CallbackQuery, state: FSMContext):
    await state.clear()
    st = await stars_status(c.bot)
    shown = f"<b>{st['balance']}⭐</b>" if st["balance"] is not None else "aniqlab bo'lmadi"
    costs = " · ".join(f"{m} oy = {s}⭐" for m, s in PREMIUM_STARS.items())
    text = (f"🌟 <b>Botning Stars balansi:</b> {st['level']} {shown}\n"
            f"Kutilayotgan yetkazishlar uchun kerak: <b>{st['need']}⭐</b>\n"
            f"Chegaralar: 🟡 &lt; {st['warn']}⭐ · 🔴 &lt; {st['crit']}⭐ yoki kutilayotganlarga yetmasa\n\n"
            "Premium va giftlar shu balansdan avtomatik yuboriladi. Balans yetmasa buyurtma navbatda kutadi va "
            "balans to'lgach avtomatik yetkaziladi (72 soatgacha).\n\n"
            f"💵 Premium tannarxi: {costs}\n\nTezroq to'ldirish uchun o'z Stars'ingiz bilan to'lov qilishingiz mumkin 👇")
    rows = [[B(f"➕ {a}⭐", f"o:fd:{a}") for a in (250, 500)], [B(f"➕ {a}⭐", f"o:fd:{a}") for a in (1000, 2500)],
            [B("🔄 Yangilash", "o:bs"), B("⬅️ Orqaga", "o:home")]]
    await screen(c, "owner", text, K(*rows))


@r_fin.callback_query(F.data.startswith("o:fd:"))
async def bot_fund(c: CallbackQuery):
    amount = int(c.data.split(":")[2])
    await c.bot.send_invoice(chat_id=c.from_user.id, title="Bot balansini to'ldirish",
                             description="Premium va giftlarni avtomatik yetkazish uchun", payload=f"fund:{amount}",
                             currency="XTR", prices=[LabeledPrice(label="Stars", amount=amount)])
    await c.answer()


# ------------------------------------------------------------------ buyurtmalar

FILTERS = {
    "act": ("⚠️ E'tibor kerak", "(payment_status='review' OR (payment_status='paid' AND delivery_status IN "
                                "('failed','uncertain','manual_pending','waiting_funds','retry')) OR EXISTS "
                                "(SELECT 1 FROM refunds r WHERE r.order_id=orders.id AND r.status IN ('manual_required','failed')))"),
    "pen": ("⏳ Kutilayotgan to'lovlar", "payment_status='pending'"),
    "paid": ("✅ To'langan", "payment_status='paid'"),
    "all": ("📋 Hammasi", "1=1"),
}


@r_fin.callback_query(F.data == "o:ord")
async def orders_menu(c: CallbackQuery):
    rows = [[B(v[0], f"o:ol:{k}")] for k, v in FILTERS.items()] + [[B("⬅️ Orqaga", "o:home")]]
    await screen(c, "owner", "🧾 <b>Buyurtmalar</b>\nFiltrni tanlang 👇", K(*rows))


@r_fin.callback_query(F.data.startswith("o:ol:"))
async def fin_orders_list(c: CallbackQuery):
    key = c.data.split(":")[2]
    title, cond = FILTERS[key]
    rows = await q(f"SELECT * FROM orders WHERE bot_id=? AND {cond} ORDER BY id DESC LIMIT 20", BOT_ID)
    kb = [[B(f"{ICON.get(o['payment_status'], '•')} {o['order_code']} · {o['product_title'][:16]} · {o['delivery_status']}", f"o:od:{o['id']}")]
          for o in rows] + [[B("⬅️ Orqaga", "o:ord")]]
    await screen(c, "owner", f"🧾 <b>{title}</b>" + ("" if rows else "\n\nHech narsa yo'q ✅"), K(*kb))


async def order_text(o) -> str:
    oid = o["id"]
    lines = [f"🧾 <b>Buyurtma {o['order_code']}</b> (#{oid})", esc(o["product_title"])]
    if o["product_type"] == "game" and o["meta"]:
        lines.append(fmt_meta(o["meta"]))
    lines += [f"Xaridor: {await who(o['user_id'])}", f"Holat: {order_state(o)}",
              f"To'lov: <b>{o['payment_status']}</b> · Yetkazish: <b>{o['delivery_status']}</b>",
              f"Narx: {fmt_uzs(o['uzs'])} / {o['stars']}⭐ / {o['ton_nano'] / 1e9:.3f} TON", f"Yaratilgan: {fmt_ts(o['created_at'])}"]
    if o["paid_at"]:
        lines.append(f"To'langan: {fmt_ts(o['paid_at'])} · {fmt_amount(o['amount'], o['currency'])} · "
                     f"{METHODS.get(o['payment_method'], ('', '', o['payment_method']))[2]}")
    if o["last_error"]:
        lines.append(f"⚠️ Xato: <code>{esc(o['last_error'][:150])}</code>")
    for p in await q("SELECT * FROM payments WHERE order_id=? ORDER BY id", oid):
        lines.append(f"💳 #{p['id']} {p['method']} {p['status']} {fmt_amount(p['amount'], p['currency'])}"
                     + (f" <code>{esc((p['provider_payment_id'] or '')[:18])}</code>" if p["provider_payment_id"] else ""))
    d = await one("SELECT * FROM deliveries WHERE order_id=?", oid)
    if d:
        lines.append(f"📦 {d['mode']}/{d['status']} · urinish {d['attempts']}"
                     + (f" · keyingi {fmt_ts(d['next_retry_at'])}" if d["next_retry_at"] else ""))
    r = await one("SELECT * FROM refunds WHERE order_id=?", oid)
    if r:
        lines.append(f"↩️ Qaytarish: {r['status']} ({r['method']})")
    led = await order_entries(oid)
    if led:
        lines.append("📒 Ledger: " + "; ".join(f"{e['type']} {e['amount']:+d} {e['currency']}" for e in led[:8]))
    return "\n".join(lines)


def adm_order_kb(o, d):
    oid, ps, rows = o["id"], o["payment_status"], []
    if ps in ("pending", "expired", "review"):
        rows.append([B("✅ To'langan deb tasdiqlash", f"o:cf:{oid}"), B("❌ Rad etish", f"o:rno:{oid}")])
    if ps == "paid" and d:
        if d["mode"] == "auto" and d["status"] in ("failed", "waiting_funds", "retry", "uncertain", "pending"):
            rows.append([B("🔁 Qayta urinish", f"o:rt:{oid}")])
        if d["status"] not in ("delivered", "cancelled"):
            rows.append([B("✅ Yetkazildi deb belgilash", f"o:md:{oid}")])
    if ps == "paid" and (not d or d["status"] not in ("processing",)):
        rows.append([B("↩️ Pulni qaytarish", f"o:rf:{oid}")])
    rows.append([B("⬅️ Orqaga", "o:ord")])
    return K(*rows)


@r_fin.callback_query(F.data.startswith("o:od:"))
async def order_view(c: CallbackQuery):
    o = await get_order(int(c.data.split(":")[2]))
    if not o:
        return await c.answer("Topilmadi")
    d = await one("SELECT * FROM deliveries WHERE order_id=?", o["id"])
    kb = adm_order_kb(o, d)
    r = await one("SELECT status FROM refunds WHERE order_id=?", o["id"])
    if r and r["status"] == "manual_required":
        kb.inline_keyboard.insert(0, [B("✅ Pul qaytarildi (qo'lda)", f"o:rfc:{o['id']}")])
    await screen(c, "owner", await order_text(o), kb)


# ---- to'lovni tasdiqlash / rad etish (karta cheki)

def _mark(c: CallbackQuery, text):
    return (c.message.caption or c.message.text or "") + text


@r_fin.callback_query(F.data.startswith("o:rok:") | F.data.startswith("o:cf:"))
async def pay_ok(c: CallbackQuery):
    res = await admin_confirm(c.bot, int(c.data.split(":")[2]), c.from_user.id)
    msg = {"confirmed": "✅ Tasdiqlandi", "duplicate": "Allaqachon tasdiqlangan", "excess": "Buyurtma allaqachon yopilgan — ortiqcha to'lov sifatida qayd etildi"}.get(
        res.status, "Tasdiqlab bo'lmadi: " + (res.reason or res.status))
    await c.answer(msg, show_alert=True)
    try:
        if c.message.caption is not None:
            await c.message.edit_caption(caption=_mark(c, f"\n\n{msg}"))
        else:
            await c.message.delete()
    except Exception:
        pass


@r_fin.callback_query(F.data.startswith("o:rno:"))
async def pay_no(c: CallbackQuery):
    ok = await reject_payment(c.bot, int(c.data.split(":")[2]), c.from_user.id)
    await c.answer("Rad etildi" if ok else "Allaqachon ko'rib chiqilgan")
    try:
        if c.message.caption is not None:
            await c.message.edit_caption(caption=_mark(c, "\n\n❌ <b>Rad etildi</b>" if ok else "\n\nℹ️ Allaqachon ko'rib chiqilgan"))
    except Exception:
        pass


# ---- yetkazish amallari

@r_fin.callback_query(F.data.startswith("o:rt:") | F.data.startswith("o:rtc:"))
async def retry(c: CallbackQuery):
    confirm = c.data.startswith("o:rtc:")
    oid = int(c.data.split(":")[2])
    ok, info = await owner_requeue(c.bot, oid, c.from_user.id, confirm_uncertain=confirm)
    if not ok and info == "needs_confirm":
        return await screen(c, "owner", "❓ <b>Natija noaniq edi</b> — Telegram yuborgan bo'lishi mumkin.\nQayta yuborsangiz foydalanuvchi "
                            "<b>ikki marta</b> olishi mumkin. Avval foydalanuvchida borligini tekshiring.",
                            K([B("⚠️ Baribir qayta yuborish", f"o:rtc:{oid}")], [B("✅ Yetkazilgan, belgilash", f"o:md:{oid}")],
                              [B("⬅️ Orqaga", f"o:od:{oid}")]))
    await c.answer(("Natija: " + str(info)) if ok else ("❌ " + str(info)), show_alert=True)
    o = await get_order(oid)
    if o:
        await order_view(c)


@r_fin.callback_query(F.data.startswith("o:md:"))
async def mark_done(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    ok = await mark_delivered(c.bot, oid, c.from_user.id)
    await c.answer("✅ Belgilandi, foydalanuvchiga xabar yuborildi" if ok else "Belgilab bo'lmadi (allaqachon yetkazilgan?)", show_alert=True)
    try:
        await c.message.delete()
    except Exception:
        pass


# ---- qaytarish

@r_fin.callback_query(F.data.startswith("o:rf:"))
async def refund_menu(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    o = await get_order(oid)
    if not o:
        return await c.answer("Topilmadi")
    rows = [[B("↩️ Asl to'lov usuliga", f"o:rfy:{oid}:original")]]
    if o["product_type"] != "topup" and o["payment_method"] != "wallet":
        rows.append([B("💰 Bot balansiga (so'm/Stars emas, ⭐ da)", f"o:rfy:{oid}:wallet")])
    rows.append([B("⬅️ Bekor", f"o:od:{oid}")])
    await screen(c, "owner", f"↩️ <b>Qaytarish</b> — {o['order_code']}\n{esc(o['product_title'])}\n"
                 f"To'langan: {fmt_amount(o['amount'], o['currency'])}\n\nQayerga qaytaramiz? Stars to'lovi Telegram orqali avtomatik "
                 "qaytariladi; karta/UZS/TON bo'lsa pulni o'zingiz qaytarib, tasdiqlaysiz.", K(*rows))


@r_fin.callback_query(F.data.startswith("o:rfy:"))
async def refund_do(c: CallbackQuery):
    _, _, oid, mode = c.data.split(":")
    ok, msg = await refund_order(c.bot, int(oid), c.from_user.id, mode)
    await c.answer(("✅ " if ok else "❌ ") + msg, show_alert=True)
    o = await get_order(int(oid))
    if o:
        d = await one("SELECT * FROM deliveries WHERE order_id=?", o["id"])
        await screen(c, "owner", await order_text(o), adm_order_kb(o, d))


@r_fin.callback_query(F.data.startswith("o:rfc:"))
async def refund_manual_done(c: CallbackQuery):
    ok, msg = await complete_manual_refund(c.bot, int(c.data.split(":")[2]), c.from_user.id)
    await c.answer(("✅ " if ok else "❌ ") + msg, show_alert=True)


@r_fin.callback_query(F.data.startswith("o:xr:"))
async def excess_done(c: CallbackQuery):
    ok = await mark_excess_refunded(int(c.data.split(":")[2]), c.from_user.id)
    await c.answer("✅ Belgilandi" if ok else "Allaqachon belgilangan", show_alert=True)


# ---- muvaffaqiyatsiz operatsiyalar

@r_fin.callback_query(F.data == "o:err")
async def failed_ops(c: CallbackQuery):
    lines = ["⚠️ <b>Muvaffaqiyatsiz / e'tibor talab operatsiyalar</b>\n"]
    kb = []
    for d in await q("SELECT d.*, o.order_code FROM deliveries d JOIN orders o ON o.id=d.order_id WHERE d.bot_id=? AND d.status IN "
                     "('failed','uncertain','waiting_funds') ORDER BY d.id DESC LIMIT 10", BOT_ID):
        lines.append(f"📦 {d['order_code']} — {d['status']} · {esc((d['last_error'] or '')[:60])}")
        kb.append([B(f"📦 {d['order_code']} ({d['status']})", f"o:od:{d['order_id']}")])
    for r in await q("SELECT r.*, o.order_code FROM refunds r JOIN orders o ON o.id=r.order_id WHERE r.bot_id=? AND r.status IN "
                     "('failed','manual_required') ORDER BY r.id DESC LIMIT 10", BOT_ID):
        lines.append(f"↩️ {r['order_code']} — {r['status']} {esc((r['error'] or '')[:60])}")
        kb.append([B(f"↩️ {r['order_code']} ({r['status']})", f"o:od:{r['order_id']}")])
    for p in await q("SELECT p.*, o.order_code FROM payments p JOIN orders o ON o.id=p.order_id WHERE p.bot_id=? AND p.status IN "
                     "('review','excess') ORDER BY p.id DESC LIMIT 10", BOT_ID):
        lines.append(f"💳 {p['order_code']} — {p['status']} {esc((p['error'] or '')[:60])}")
        kb.append([B(f"💳 {p['order_code']} ({p['status']})", f"o:od:{p['order_id']}")])
    for a in await q("SELECT * FROM audit_logs WHERE bot_id=? AND status='error' ORDER BY id DESC LIMIT 5", BOT_ID):
        lines.append(f"🛑 {fmt_ts(a['ts'])} {a['action']} ref={a['ref']} {esc((a['error'] or '')[:60])}")
    if len(lines) == 1:
        lines.append("Hammasi joyida ✅")
    kb.append([B("⬅️ Orqaga", "o:home")])
    await screen(c, "owner", "\n".join(lines)[:3800], K(*kb))


##############################################################################
# app/main.py
##############################################################################
_last_owner_err = 0.0


async def g_fb_begin(ev, state, bid):
    return await globals()["fb_begin"](ev, state, bid)


class Gate(BaseMiddleware):
    """Foydalanuvchini saqlaydi, bloklanganlarni to'xtatadi, majburiy obunani tekshiradi
    va kutilmagan xatolarni ushlab, foydalanuvchiga xato raqami (ref) ko'rsatadi."""

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
                kb = K([B("📢 Kanalga obuna bo'lish", url=f"https://t.me/{fs.lstrip('@')}")], [B("✅ Tekshirish", "sub:check")])
                await screen(event, "home", "🔒 Botdan foydalanish uchun avval kanalimizga obuna bo'ling:", kb)
                return
        try:
            return await handler(event, data)
        except Exception as e:
            await report_error(event, u, e)


async def report_error(event, u, e):
    global _last_owner_err
    ref = error_ref()
    log.exception("handler xatosi ref=%s user=%s", ref, u.id)
    try:
        await audit("handler_error", user_id=u.id, status="error", error=f"{type(e).__name__}: {e}", ref=ref)
    except Exception:
        pass
    text = (f"⚠️ Xatolik yuz berdi. Ref: <code>{ref}</code>\nTo'lov qilgan bo'lsangiz xavotir olmang — to'lovlar xavfsiz qayd etiladi. "
            "Muammo davom etsa, shu raqamni adminga ayting.")
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(f"Xatolik. Ref: {ref}", show_alert=True)
        else:
            await event.answer(text)
    except Exception:
        pass
    if time.time() - _last_owner_err > 30:
        _last_owner_err = time.time()
        try:
            await event.bot.send_message(OWNER_ID, f"🛑 Xato ref <code>{ref}</code>: {type(e).__name__}: {str(e)[:150]}\nFoydalanuvchi: {u.id}")
        except Exception:
            pass


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_db()
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

    r_bx = build_battle_router(globals())
    dp.include_routers(r_comments, r_owner, r_fin, r_bx, r_user, r_games, r_donate, r_battle, r_shop, r_pay)

    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in USER_CMDS], scope=BotCommandScopeAllPrivateChats())
    await set_scope_commands(bot, OWNER_ID, OWNER_CMDS)
    for r in await q("SELECT DISTINCT owner_id FROM channels"):
        if r["owner_id"] != OWNER_ID:
            await set_scope_commands(bot, r["owner_id"], ORG_CMDS)

    runner = await start_web(bot)
    await recover(bot)                       # qayta ishga tushish: noaniq yetkazishlar, yo'qolgan to'lovlar, TON, xabarlar
    tasks = start_workers(bot) + [asyncio.create_task(ticker(bot))]
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in tasks:
            t.cancel()
        await runner.cleanup()
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
