import re
import time

from aiogram import Router, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from ..config import OWNER_ID, WEBAPP_URL
from ..db import q, one, run, S, SI
from ..kb import K, B, HOME, home_kb, back, cancel_kb
from ..services import (bot_username, fmt_ts, is_vip, leaderboard, lb_text, new_order,
                        update_post, post_text, calc_uzs, fmt_uzs)
from ..states import Join, Wal
from ..ui import screen, esc
from .pay import checkout

router = Router()
router.message.filter(F.chat.type == "private")

ADDR_RE = re.compile(r"^(?:[EUk0]Q[A-Za-z0-9_-]{46}|-?\d:[0-9a-fA-F]{64})$")


# ------------------------------------------------------------------ bosh menyu

@router.message(Command("menu"))
@router.callback_query(F.data == "m:home")
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


@router.callback_query(F.data == "x:cancel")
async def cancel(c: CallbackQuery, state: FSMContext):
    await home(c, state)


@router.message(Command("help"))
async def help_cmd(m: Message):
    text = ("<b>Yordam</b>\n\n"
            "<b>Foydalanuvchi:</b> /menu /battles /shop /wallet /profile\n"
            "<b>Batl yaratuvchi:</b> /newbattle /mybattles /linkchannel\n\n"
            "1️⃣ Batl postidagi «Qatnashish» tugmasini bosing\n"
            "2️⃣ Nik nomingizni kiriting\n"
            "3️⃣ Kanaldagi raund postlariga izoh yozing — har bir raundda 1 ta izoh = ball.")
    await screen(m, "home", text, back())


@router.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext, is_new: bool = False):
    await state.clear()
    arg = command.args or ""
    if arg.startswith("ref_") and is_new:
        await _ref(m, arg[4:])
    if arg.startswith("join_") and arg[5:].isdigit():
        return await start_join(m, state, int(arg[5:]))
    if arg.startswith("buy_") and arg[4:].isdigit():
        from .shop import show_item
        return await show_item(m, int(arg[4:]))
    if arg.startswith("game_") and arg[5:].isdigit():
        from .games import begin_game
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

@router.message(Command("battles"))
@router.callback_query(F.data == "m:battles")
async def battles(ev, state: FSMContext):
    await state.clear()
    rows = await q("SELECT b.id,b.title,(SELECT COUNT(*) FROM participants WHERE battle_id=b.id) n "
                   "FROM battles b WHERE b.status='active' ORDER BY b.id DESC LIMIT 15")
    if not rows:
        return await screen(ev, "battle", "🔥 <b>Faol batllar</b>\n\nHozircha faol batl yo'q.\nO'zingiz yarating — «⚔️ Batl yaratish»!",
                            K([B("⚔️ Batl yaratish", "m:new")], [HOME]))
    kb = [[B(f"⚔️ {r['title'][:30]} · 👥 {r['n']}", f"v:{r['id']}")] for r in rows] + [[HOME]]
    await screen(ev, "battle", "🔥 <b>Faol batllar</b>\n\nBirini tanlang 👇", K(*kb))


@router.callback_query(F.data.startswith("v:"))
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

@router.callback_query(F.data.startswith("j:"))
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


@router.callback_query(Join.nick, F.data == "jn:use")
async def join_use(c: CallbackQuery, state: FSMContext):
    await _register(c, state, c.from_user.full_name if len(c.from_user.full_name) >= 2 else f"user{c.from_user.id}")


@router.message(Join.nick, F.text)
async def join_nick(m: Message, state: FSMContext):
    if not 2 <= len(m.text.strip()) <= 24:
        return await m.answer("⚠️ Nik 2 dan 24 belgigacha bo'lishi kerak. Qayta yuboring.", reply_markup=cancel_kb())
    await _register(m, state, m.text)


# ------------------------------------------------------------------ profil / referal / VIP

@router.message(Command("profile"))
@router.callback_query(F.data == "m:profile")
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


@router.callback_query(F.data == "m:ref")
async def ref(c: CallbackQuery):
    uname = await bot_username(c.bot)
    link = f"https://t.me/{uname}?start=ref_{c.from_user.id}"
    per = await SI("ref_per_credit", 3)
    u = await one("SELECT ref_count FROM users WHERE id=?", c.from_user.id)
    text = (f"🎁 <b>Do'st taklif qiling</b>\n\nHar <b>{per}</b> ta do'stingiz uchun <b>1 ta bepul batl krediti</b> olasiz.\n\n"
            f"👥 Taklif qilganlar: <b>{u['ref_count']}</b>\n🔗 Havolangiz:\n<code>{link}</code>")
    await screen(c, "ref", text, K([B("📤 Ulashish", url=f"https://t.me/share/url?url={link}&text=Batllarda%20qatnash!")], [HOME]))


@router.callback_query(F.data == "m:vip")
async def vip(c: CallbackQuery):
    price, days = await SI("vip_price", 500), await SI("vip_days", 30)
    u = await one("SELECT * FROM users WHERE id=?", c.from_user.id)
    now = f"\n\n✅ Faol: {fmt_ts(u['vip_until'])} gacha" if is_vip(u) else ""
    text = (f"👑 <b>VIP obuna</b>\n\n• Cheksiz batl yaratish (kredit kerak emas)\n• {days} kun amal qiladi\n"
            f"• Narxi: <b>{price}⭐</b>{now}")
    await screen(c, "vip", text, K([B(f"👑 VIP olish — {price}⭐", "vip:buy")], [HOME]))


@router.callback_query(F.data == "vip:buy")
async def vip_buy(c: CallbackQuery):
    await checkout(c, await new_order(c.from_user.id, "vip", await SI("vip_price", 500)))


# ------------------------------------------------------------------ hamyon

@router.message(Command("wallet"))
@router.callback_query(F.data == "m:wallet")
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


@router.callback_query(F.data == "w:top")
async def topup(c: CallbackQuery):
    amounts = (50, 100, 250, 500, 1000, 2500)
    rate = await SI("uzs_rate", 230)
    rows = [[B(f"{a}⭐ · {fmt_uzs(calc_uzs(a, rate))}", f"w:t:{a}")] for a in amounts]
    rows.append([B("⬅️ Orqaga", "m:wallet")])
    await screen(c, "wallet", "➕ <b>Balansni to'ldirish</b>\n\nMiqdorni tanlang (so'mda to'lash mumkin):", K(*rows))


@router.callback_query(F.data.startswith("w:t:"))
async def topup_go(c: CallbackQuery):
    await checkout(c, await new_order(c.from_user.id, "topup", int(c.data.split(":")[2])))


@router.callback_query(F.data == "w:set")
async def wallet_set(c: CallbackQuery, state: FSMContext):
    await state.set_state(Wal.addr)
    await screen(c, "wallet", "✍️ TON hamyon manzilingizni yuboring (UQ… yoki EQ… bilan boshlanadi).", cancel_kb())


@router.message(Wal.addr, F.text)
async def wallet_addr(m: Message, state: FSMContext):
    a = m.text.strip()
    if not ADDR_RE.match(a):
        return await m.answer("⚠️ Manzil noto'g'ri ko'rinadi. Qayta yuboring.", reply_markup=cancel_kb())
    await run("UPDATE users SET wallet=? WHERE id=?", a, m.from_user.id)
    await wallet(m, state)


@router.callback_query(F.data == "w:del")
async def wallet_del(c: CallbackQuery, state: FSMContext):
    await run("UPDATE users SET wallet='' WHERE id=?", c.from_user.id)
    await wallet(c, state)
