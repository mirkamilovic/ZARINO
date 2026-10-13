"""Battle Engine: holat mashinasi, ball hisobi, qatnashish, yakunlash/settlement, Random Contest.
Faqat `t` (tranzaksiya obyekti: query/one/execute) bilan ishlaydi — Telegram'dan mustaqil, test qilinadi.
Barcha yozuvlar idempotent: UNIQUE kalit + rowcount tekshiruvi."""
import hashlib
import json
import secrets
import time

from .schema import SOURCES

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
