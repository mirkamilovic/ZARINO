"""Ko'rinishlar (matn kartalari). Faqat o'qiydi; `t` — query/one interfeysli ixtiyoriy obyekt (Tx yoki RO)."""
import json
import time
from html import escape as esc

from . import engine as BE
from .texts import T

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
