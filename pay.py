from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, LabeledPrice

from ..config import OWNER_ID
from ..db import one, run, S
from ..kb import K, B, HOME
from ..services import fulfill, order_title, ton_paid, fmt_uzs, fmt_meta
from ..states import Rcpt
from ..ui import screen, esc

router = Router()


async def checkout(ev, oid: int):
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    u = await one("SELECT balance FROM users WHERE id=?", o["user_id"])
    it = await one("SELECT kind FROM items WHERE id=?", o["item_id"]) if o["item_id"] else None
    rows = []
    if await S("card_number"):
        rows.append([B(f"🏦 Karta orqali to'lash — {fmt_uzs(o['uzs'])}", f"pay:card:{oid}")])
    if await S("provider_token"):
        rows.append([B(f"💳 Payme/Click/karta — {fmt_uzs(o['uzs'])}", f"pay:uzs:{oid}")])
    if o["kind"] != "topup":
        rows.append([B(f"💰 Balansdan to'lash ({u['balance']}⭐)", f"pay:bal:{oid}")])
    if not (it and it["kind"] == "stars"):
        rows.append([B(f"⭐ Telegram Stars bilan ({o['stars']}⭐)", f"pay:xtr:{oid}")])
    if await S("ton_address"):
        rows.append([B(f"💠 TON bilan ({o['ton']} TON)", f"pay:ton:{oid}")])
    rows.append([B("✖️ Bekor qilish", "x:cancel")])
    text = (f"🧾 <b>Buyurtma #{oid}</b>\n\n{esc(await order_title(o))}\n"
            + (f"{fmt_meta(o['meta'])}\n" if o["meta"] else "")
            + f"💳 Narx: <b>{fmt_uzs(o['uzs'])}</b>\n\nTo'lov usulini tanlang 👇")
    if len(rows) == 1:
        text += "\n\n⚠️ Hozircha to'lov usullari sozlanmagan. Iltimos, admin bilan bog'laning."
    await screen(ev, "shop", text, K(*rows))


async def _mine(c: CallbackQuery, oid: int):
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    if not o or o["user_id"] != c.from_user.id or o["status"] != "pending":
        await c.answer("Buyurtma topilmadi yoki allaqachon to'langan", show_alert=True)
        return None
    return o


# ------------------------------------------------------------------ so'mda: kartaga o'tkazma + chek

@router.callback_query(F.data.startswith("pay:card:"))
async def pay_card(c: CallbackQuery, state: FSMContext):
    o = await _mine(c, int(c.data.split(":")[2]))
    if not o:
        return
    card, holder = await S("card_number"), await S("card_holder")
    if not card:
        return await c.answer("Karta sozlanmagan", show_alert=True)
    await run("UPDATE orders SET method='card' WHERE id=?", o["id"])
    await state.set_state(Rcpt.photo)
    await state.update_data(oid=o["id"])
    text = (f"🏦 <b>Kartaga o'tkazma</b> — buyurtma #{o['id']}\n\n"
            f"1️⃣ Karta raqami:\n<code>{esc(card)}</code>\n"
            + (f"👤 Karta egasi: <b>{esc(holder)}</b>\n" if holder else "")
            + f"2️⃣ Aniq summa: <b>{fmt_uzs(o['uzs'])}</b>\n\n"
            "3️⃣ O'tkazgach <b>chek (skrinshot)</b> rasmini shu yerga yuboring 📎\n"
            "Admin tekshirib tasdiqlaydi, so'ng buyurtma yetkaziladi.")
    await screen(c, "wallet", text, K([B("✖️ Bekor qilish", "x:cancel")]))


@router.message(Rcpt.photo, F.photo | F.document)
async def receipt(m: Message, state: FSMContext):
    d = await state.get_data()
    o = await one("SELECT * FROM orders WHERE id=?", d.get("oid"))
    await state.clear()
    if not o or o["user_id"] != m.from_user.id or o["status"] != "pending":
        return await m.answer("Buyurtma topilmadi yoki allaqachon to'langan.", reply_markup=K([HOME]))
    who = f'<a href="tg://user?id={m.from_user.id}">{esc(m.from_user.full_name)}</a>'
    if m.from_user.username:
        who += f" (@{m.from_user.username})"
    caption = (f"🧾 <b>Yangi to'lov cheki</b>\nBuyurtma #{o['id']} — {esc(await order_title(o))}\n"
               + (f"{fmt_meta(o['meta'])}\n" if o["meta"] else "")
               + f"Summa: <b>{fmt_uzs(o['uzs'])}</b>\nXaridor: {who}")
    try:
        await m.bot.copy_message(OWNER_ID, m.chat.id, m.message_id, caption=caption,
                                 reply_markup=K([B("✅ Tasdiqlash", f"o:rok:{o['id']}"),
                                                 B("❌ Rad etish", f"o:rno:{o['id']}")]))
    except Exception:
        return await m.answer("❌ Chekni adminga yuborib bo'lmadi. Keyinroq qayta urinib ko'ring.", reply_markup=K([HOME]))
    await m.answer(f"✅ Chek qabul qilindi. Buyurtma #{o['id']} admin tomonidan tekshirilmoqda — odatda bir necha daqiqa.",
                   reply_markup=K([HOME]))


@router.message(Rcpt.photo)
async def receipt_wrong(m: Message):
    await m.answer("📎 Iltimos, to'lov chekini <b>rasm</b> ko'rinishida yuboring.",
                   reply_markup=K([B("✖️ Bekor qilish", "x:cancel")]))


# ------------------------------------------------------------------ so'mda: provayder (Payme/Click va h.k.)

@router.callback_query(F.data.startswith("pay:uzs:"))
async def pay_uzs(c: CallbackQuery):
    o = await _mine(c, int(c.data.split(":")[2]))
    token = await S("provider_token")
    if not o or not token:
        return
    title = (await order_title(o))[:32]
    try:
        await c.bot.send_invoice(
            chat_id=c.from_user.id, title=title, description=f"Buyurtma #{o['id']}", payload=f"o:{o['id']}",
            provider_token=token, currency="UZS", prices=[LabeledPrice(label=title, amount=o["uzs"] * 100)])
        await c.answer()
    except Exception as e:
        await c.answer(f"To'lov hisobini ochib bo'lmadi: {e}"[:190], show_alert=True)


# ------------------------------------------------------------------ boshqa usullar

@router.callback_query(F.data.startswith("pay:bal:"))
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


@router.callback_query(F.data.startswith("pay:xtr:"))
async def pay_stars(c: CallbackQuery):
    o = await _mine(c, int(c.data.split(":")[2]))
    if not o:
        return
    title = (await order_title(o))[:32]
    await c.bot.send_invoice(
        chat_id=c.from_user.id, title=title, description=f"Buyurtma #{o['id']}",
        payload=f"o:{o['id']}", currency="XTR", prices=[LabeledPrice(label=title, amount=o["stars"])])
    await c.answer()


@router.callback_query(F.data.startswith("pay:ton:"))
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


@router.callback_query(F.data.startswith("pay:chk:"))
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


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = False
    if q.invoice_payload.startswith("o:"):
        o = await one("SELECT status,user_id FROM orders WHERE id=?", int(q.invoice_payload[2:]))
        ok = bool(o and o["status"] == "pending" and o["user_id"] == q.from_user.id)
    await q.answer(ok=ok, error_message=None if ok else "Buyurtma topilmadi yoki allaqachon to'langan")


@router.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    if sp.invoice_payload.startswith("o:"):
        method = "stars" if sp.currency == "XTR" else "uzs"
        await fulfill(m.bot, int(sp.invoice_payload[2:]), method, sp.telegram_payment_charge_id)
