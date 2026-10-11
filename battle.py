import time

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (Message, CallbackQuery, BotCommand, BotCommandScopeChat)

from ..config import OWNER_ID
from ..db import q, one, run, SI
from ..kb import K, B, HOME, cancel_kb
from ..services import (bot_username, fmt_ts, is_vip, leaderboard, lb_text, new_order, post_text,
                        join_markup, finish_battle, update_post)
from ..states import Link, NewB, Rnd, Pts
from ..ui import screen, esc
from .pay import checkout

router = Router()
router.message.filter(F.chat.type == "private")

USER_CMDS = [("start", "Boshlash"), ("menu", "Asosiy menyu"), ("battles", "Faol batllar"),
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


def own(b, uid) -> bool:
    return bool(b) and (b["owner_id"] == uid or uid == OWNER_ID)


# ------------------------------------------------------------------ kanal ulash

@router.message(Command("linkchannel"))
@router.callback_query(F.data == "ch:link")
async def link_start(ev, state: FSMContext):
    await state.set_state(Link.ch)
    me = await ev.bot.me()
    text = ("📢 <b>Kanal ulash</b>\n\nShartlar:\n"
            f"1️⃣ @{me.username} ni kanalga <b>admin</b> qiling (post yuborish va tahrirlash huquqi bilan)\n"
            "2️⃣ Kanalga <b>muhokama (izoh) guruhi</b> ulang\n"
            f"3️⃣ @{me.username} ni shu guruhga ham <b>admin</b> qiling — aks holda izohlarni ko'ra olmaydi\n\n"
            "Keyin kanal @username'ini yoki kanaldan biror postni forward qilib yuboring 👇")
    await screen(ev, "battle", text, cancel_kb())


@router.message(Link.ch)
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


@router.message(Command("newbattle"))
@router.callback_query(F.data == "m:new")
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


@router.callback_query(NewB.ch, F.data.startswith("nb:ch:"))
async def nb_channel(c: CallbackQuery, state: FSMContext):
    await state.update_data(ch=int(c.data.split(":")[2]))
    await state.set_state(NewB.title)
    await screen(c, "battle", "⚔️ <b>Yangi batl</b>\n\n2/5 — Batl <b>nomini</b> yuboring (masalan: «Eng zo'r she'r»).", cancel_kb())


@router.message(NewB.title, F.text)
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


@router.message(NewB.rules, F.text)
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


@router.callback_query(NewB.points, F.data.startswith("nb:pts:"))
async def nb_points_cb(c: CallbackQuery, state: FSMContext):
    await state.update_data(points=int(c.data.split(":")[2]))
    await _ask_end(c, state)


@router.message(NewB.points, F.text)
async def nb_points_msg(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not 1 <= int(m.text) <= 1000:
        return await m.answer("⚠️ 1 dan 1000 gacha son yuboring.", reply_markup=cancel_kb())
    await state.update_data(points=int(m.text))
    await _ask_end(m, state)


@router.callback_query(NewB.end, F.data.startswith("nb:end:"))
async def nb_end(c: CallbackQuery, state: FSMContext):
    hours = int(c.data.split(":")[2])
    await state.update_data(hours=hours)
    d = await state.get_data()
    ends = fmt_ts(int(time.time()) + hours * 3600) if hours else "yaratuvchi hal qiladi"
    text = (f"✅ <b>Tasdiqlang</b>\n\n⚔️ {esc(d['title'])}\n📜 {esc(d['rules'])}\n"
            f"💎 Raund uchun: {d['points']} ball\n⏰ Tugash: {ends}")
    await screen(c, "battle", text, K([B("🚀 E'lon qilish", "nb:go")], [B("✖️ Bekor qilish", "x:cancel")]))


@router.callback_query(NewB.end, F.data == "nb:go")
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

@router.message(Command("mybattles"))
@router.callback_query(F.data == "m:my")
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


@router.callback_query(F.data.regexp(r"^b:\d+$"))
async def manage_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await manage(c, state, int(c.data[2:]))


@router.callback_query(F.data.startswith("b:lb:"))
async def lb(c: CallbackQuery):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not own(b, c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    rows = await leaderboard(bid)
    await screen(c, "top", f"🏆 <b>{esc(b['title'])}</b>\n\n{lb_text(rows) or 'Hali ishtirokchi yo‘q'}",
                 K([B("⬅️ Orqaga", f"b:{bid}")]))


# ------------------------------------------------------------------ raund

@router.callback_query(F.data.startswith("b:r:"))
async def rnd_start(c: CallbackQuery, state: FSMContext):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not own(b, c.from_user.id) or b["status"] != "active":
        return await c.answer("Ruxsat yo'q yoki batl yakunlangan", show_alert=True)
    await state.set_state(Rnd.text)
    await state.update_data(bid=bid)
    await screen(c, "battle", "➕ <b>Yangi raund</b>\n\nKanalga chiqadigan postni yuboring: <b>matn</b> yoki <b>rasm + izoh</b> (izoh 1000 belgigacha).",
                 cancel_kb())


@router.message(Rnd.text, F.text | F.photo)
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


@router.callback_query(Rnd.points, F.data.startswith("rd:pts:"))
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

@router.callback_query(F.data.startswith("b:p:"))
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


@router.callback_query(F.data.startswith("b:pu:"))
async def pts_pick(c: CallbackQuery, state: FSMContext):
    _, _, bid, uid = c.data.split(":")
    b = await one("SELECT * FROM battles WHERE id=?", int(bid))
    if not own(b, c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await state.set_state(Pts.amount)
    await state.update_data(bid=int(bid), uid=int(uid))
    await screen(c, "battle", "Necha ball qo'shilsin? Son yuboring (ayirish uchun manfiy son, masalan <code>-3</code>).", cancel_kb())


@router.message(Pts.amount, F.text)
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

@router.callback_query(F.data.startswith("b:f:"))
async def fin_ask(c: CallbackQuery):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not own(b, c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await screen(c, "battle", f"🏁 <b>{esc(b['title'])}</b> batlini yakunlaymizmi?\nNatijalar kanalga e'lon qilinadi.",
                 K([B("✅ Ha, yakunlash", f"b:ff:{bid}")], [B("⬅️ Yo'q", f"b:{bid}")]))


@router.callback_query(F.data.startswith("b:ff:"))
async def fin_go(c: CallbackQuery, state: FSMContext):
    bid = int(c.data.split(":")[2])
    b = await one("SELECT * FROM battles WHERE id=?", bid)
    if not own(b, c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await finish_battle(c.bot, bid)
    await manage(c, state, bid, prefix="🏁 Batl yakunlandi!\n\n")
