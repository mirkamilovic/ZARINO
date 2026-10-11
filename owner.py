import asyncio
import math
import re
import time

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from ..config import OWNER_ID
from ..db import q, one, run, S, SI, set_s
from ..kb import K, B, cancel_kb
from ..services import fmt_ts, fulfill, fmt_uzs, order_title, fmt_meta
from ..states import OSet, OImg, OItem, OBc, OGame, OPack
from ..ui import screen, esc

router = Router()
router.message.filter(F.chat.type == "private", F.from_user.id == OWNER_ID)
router.callback_query.filter(F.from_user.id == OWNER_ID)

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
}
NUMERIC = {"battle_fee", "vip_price", "vip_days", "ton_rate", "markup", "ref_per_credit", "uzs_rate"}
CLEARABLE = {"force_sub", "ton_address", "card_number", "card_holder", "provider_token"}
SECRET = {"provider_token"}
IMAGES = {"home": "Bosh menyu", "battle": "Batllar", "shop": "Do'kon", "wallet": "Hamyon",
          "profile": "Profil", "owner": "Owner panel", "vip": "VIP", "ref": "Taklif", "top": "Reyting"}
KIND_NAMES = {"premium": "💎 Premium", "stars": "⭐ Stars", "gift": "🎁 Gift", "nft": "🖼 NFT"}


@router.message(Command("owner"))
@router.callback_query(F.data == "o:home")
async def panel(ev, state: FSMContext):
    await state.clear()
    rows = [[B("📊 Statistika", "o:stats"), B("🧾 Buyurtmalar", "o:ord")],
            [B("⚙️ Sozlamalar / narxlar", "o:set"), B("🛍 Do'kon boshqaruvi", "o:shop")],
            [B("🎮 O'yinlar donati", "o:gm")],
            [B("🖼 Rasmlar", "o:img"), B("📣 Xabar yuborish", "o:bc")],
            [B("🏠 Menyu", "m:home")]]
    await screen(ev, "owner", "🛠 <b>Owner panel</b>\n\nBarcha narxlar, rasmlar va do'kon shu yerdan boshqariladi.", K(*rows))


@router.callback_query(F.data == "o:stats")
async def stats(c: CallbackQuery):
    day = int(time.time()) - 86400
    users = (await one("SELECT COUNT(*) c FROM users"))["c"]
    new = (await one("SELECT COUNT(*) c FROM users WHERE joined>?", day))["c"]
    ba = (await one("SELECT COUNT(*) c FROM battles WHERE status='active'"))["c"]
    bt = (await one("SELECT COUNT(*) c FROM battles"))["c"]
    pt = (await one("SELECT COUNT(*) c FROM participants"))["c"]
    stars = (await one("SELECT COALESCE(SUM(stars),0) s FROM orders WHERE method='stars' AND status IN ('paid','delivered')"))["s"]
    ton = (await one("SELECT COALESCE(SUM(ton),0) s FROM orders WHERE method='ton' AND status IN ('paid','delivered')"))["s"]
    uzs = (await one("SELECT COALESCE(SUM(uzs),0) s FROM orders WHERE method IN ('card','uzs') AND status IN ('paid','delivered')"))["s"]
    pend = (await one("SELECT COUNT(*) c FROM orders WHERE status='paid'"))["c"]
    bal = ""
    try:
        sb = await c.bot.get_my_star_balance()
        bal = f"\n🌟 Botning Stars balansi: <b>{sb.amount}</b>"
    except Exception:
        pass
    text = (f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: <b>{users}</b> (24 soatda +{new})\n"
            f"⚔️ Batllar: <b>{ba}</b> faol / {bt} jami\n🙋 Ishtirokchilar: <b>{pt}</b>\n\n"
            f"💰 Tushum: <b>{fmt_uzs(uzs)}</b> + <b>{stars}⭐</b> + <b>{round(ton, 3)} TON</b>\n"
            f"📦 Yetkazilmagan buyurtmalar: <b>{pend}</b>{bal}\n\n"
            "ℹ️ Stars'ni yechish: Telegram → bot sozlamalari → Balance (Fragment orqali).")
    await screen(c, "owner", text, K([B("⬅️ Orqaga", "o:home")]))


# ------------------------------------------------------------------ sozlamalar

@router.callback_query(F.data == "o:set")
async def settings(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = []
    for k, label in SETTINGS.items():
        v = await S(k)
        shown = ("✅ o'rnatilgan" if v else "—") if k in SECRET else (v[:18] or "—")
        rows.append([B(f"{label.split(' (')[0]}: {shown}", f"o:s:{k}")])
    rows.append([B("⬅️ Orqaga", "o:home")])
    await screen(c, "owner", "⚙️ <b>Sozlamalar</b>\nO'zgartirish uchun bosing 👇", K(*rows))


@router.callback_query(F.data.startswith("o:s:"))
async def setting_pick(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    if key not in SETTINGS:
        return await c.answer()
    await state.set_state(OSet.val)
    await state.update_data(key=key)
    cur = ("✅ o'rnatilgan" if await S(key) else "—") if key in SECRET else (esc(await S(key)) or "—")
    await screen(c, "owner", f"✏️ <b>{SETTINGS[key]}</b>\nHozirgi qiymat: <code>{cur}</code>\n\nYangi qiymatni yuboring"
                 + (" (o'chirish uchun <code>-</code>)" if key in CLEARABLE else "") + ":", cancel_kb())


@router.message(OSet.val, F.text)
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

@router.callback_query(F.data == "o:img")
async def images(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[B(f"🖼 {name}", f"o:i:{k}")] for k, name in IMAGES.items()] + [[B("⬅️ Orqaga", "o:home")]]
    await screen(c, "owner", "🖼 <b>Rasmlar</b>\nQaysi bo'limning rasmini almashtiramiz?\n(Mini App ham shu rasmlardan foydalanadi)", K(*rows))


@router.callback_query(F.data.startswith("o:i:"))
async def image_pick(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(OImg.photo)
    await state.update_data(key=key)
    await screen(c, "owner", f"🖼 <b>{IMAGES.get(key, key)}</b>\n\nYangi rasmni yuboring (tavsiya: 1280×640, gorizontal).",
                 K([B("♻️ Standartga qaytarish", f"o:ir:{key}")], [B("✖️ Bekor qilish", "x:cancel")]))


@router.message(OImg.photo, F.photo)
async def image_set(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    await set_s(f"img_{key}", m.photo[-1].file_id)
    await state.clear()
    await m.answer("✅ Rasm almashtirildi", reply_markup=K([B("🖼 Rasmlar", "o:img")]))


@router.callback_query(F.data.startswith("o:ir:"))
async def image_reset(c: CallbackQuery, state: FSMContext):
    await set_s(f"img_{c.data.split(':')[2]}", "")
    await state.clear()
    await images(c, state)


# ------------------------------------------------------------------ do'kon

@router.callback_query(F.data == "o:shop")
async def shop_admin(c: CallbackQuery, state: FSMContext):
    await state.clear()
    items = await q("SELECT * FROM items ORDER BY kind, price_stars")
    rows = [[B(f"{'✅' if i['active'] else '🚫'} {i['title'][:28]} · {i['price_stars']}⭐", f"o:it:{i['id']}")] for i in items[:40]]
    rows += [[B("➕ Mahsulot qo'shish", "o:ia"), B("🎁 Giftlarni import", "o:gi")], [B("⬅️ Orqaga", "o:home")]]
    await screen(c, "owner", "🛍 <b>Do'kon boshqaruvi</b>", K(*rows))


@router.callback_query(F.data.startswith("o:it:"))
async def item_view(c: CallbackQuery):
    iid = int(c.data.split(":")[2])
    i = await one("SELECT * FROM items WHERE id=?", iid)
    if not i:
        return await c.answer("Topilmadi")
    text = (f"<b>{esc(i['title'])}</b>\nTuri: {KIND_NAMES.get(i['kind'], i['kind'])}\nNarx: {i['price_stars']}⭐\n"
            f"Payload: <code>{esc(i['payload'])}</code>\nHolat: {'faol' if i['active'] else 'o‘chirilgan'}")
    await screen(c, "owner", text, K([B("🔁 Yoq/o'chir", f"o:itt:{iid}"), B("🗑 O'chirish", f"o:itd:{iid}")],
                                      [B("✏️ Narxni o'zgartirish", f"o:itp:{iid}")], [B("⬅️ Orqaga", "o:shop")]))


@router.callback_query(F.data.startswith("o:itt:"))
async def item_toggle(c: CallbackQuery, state: FSMContext):
    await run("UPDATE items SET active=1-active WHERE id=?", int(c.data.split(":")[2]))
    await shop_admin(c, state)


@router.callback_query(F.data.startswith("o:itd:"))
async def item_delete(c: CallbackQuery, state: FSMContext):
    await run("DELETE FROM items WHERE id=?", int(c.data.split(":")[2]))
    await shop_admin(c, state)


@router.callback_query(F.data.startswith("o:itp:"))
async def item_price(c: CallbackQuery, state: FSMContext):
    await state.set_state(OItem.price)
    await state.update_data(edit=int(c.data.split(":")[2]))
    await screen(c, "owner", "Yangi narxni ⭐ da yuboring:", cancel_kb())


@router.callback_query(F.data == "o:ia")
async def item_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(OItem.kind)
    rows = [[B(n, f"o:ik:{k}")] for k, n in KIND_NAMES.items()] + [[B("✖️ Bekor qilish", "x:cancel")]]
    await screen(c, "owner", "Mahsulot turini tanlang:", K(*rows))


@router.callback_query(OItem.kind, F.data.startswith("o:ik:"))
async def item_kind(c: CallbackQuery, state: FSMContext):
    await state.update_data(kind=c.data.split(":")[2])
    await state.set_state(OItem.title)
    await screen(c, "owner", "Mahsulot <b>nomini</b> yuboring:", cancel_kb())


@router.message(OItem.title, F.text)
async def item_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:60])
    await state.set_state(OItem.descr)
    await m.answer("Qisqa <b>tavsif</b> yuboring (yoki <code>-</code>):", reply_markup=cancel_kb())


@router.message(OItem.descr, F.text)
async def item_descr(m: Message, state: FSMContext):
    await state.update_data(descr="" if m.text.strip() == "-" else m.text.strip()[:300])
    await state.set_state(OItem.price)
    await m.answer("Narxini ⭐ da yuboring (TON'ga kurs bo'yicha avto hisoblanadi):", reply_markup=cancel_kb())


@router.message(OItem.price, F.text)
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


@router.message(OItem.payload, F.text)
async def item_payload(m: Message, state: FSMContext):
    d = await state.get_data()
    p = m.text.strip()
    if d["kind"] == "premium" and p not in ("3", "6", "12"):
        return await m.answer("⚠️ Faqat 3, 6 yoki 12.", reply_markup=cancel_kb())
    await run("INSERT INTO items(kind,title,descr,price_stars,payload) VALUES(?,?,?,?,?)",
              d["kind"], d["title"], d["descr"], d["price"], p)
    await state.clear()
    await m.answer("✅ Mahsulot qo'shildi", reply_markup=K([B("🛍 Do'kon", "o:shop")]))


@router.callback_query(F.data == "o:gi")
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


@router.callback_query(F.data == "o:gm")
async def games_admin(c: CallbackQuery, state: FSMContext):
    await state.clear()
    games = await q("SELECT * FROM games ORDER BY id")
    rows = [[B(f"{'✅' if g['active'] else '🚫'} {g['emoji']} {g['name']}", f"o:g:{g['id']}")] for g in games]
    rows += [[B("➕ O'yin qo'shish", "o:ga")], [B("⬅️ Orqaga", "o:home")]]
    await screen(c, "owner", "🎮 <b>O'yinlar donati</b>\n\nO'yin va paketlarni, ularning narxini (so'mda) shu yerdan belgilaysiz. "
                 "Narxi qo'yilmagan paket foydalanuvchiga ko'rinmaydi.", K(*rows))


@router.callback_query(F.data.startswith("o:g:"))
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


@router.callback_query(F.data.startswith("o:gt:"))
async def game_toggle(c: CallbackQuery, state: FSMContext):
    await run("UPDATE games SET active=1-active WHERE id=?", int(c.data.split(":")[2]))
    await games_admin(c, state)


@router.callback_query(F.data.startswith("o:gd:"))
async def game_delete(c: CallbackQuery, state: FSMContext):
    gid = int(c.data.split(":")[2])
    await run("DELETE FROM packs WHERE game_id=?", gid)
    await run("DELETE FROM games WHERE id=?", gid)
    await games_admin(c, state)


@router.callback_query(F.data == "o:ga")
async def game_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(OGame.name)
    await screen(c, "owner", "🎮 O'yin <b>nomini</b> yuboring (masalan: <code>Standoff 2</code>, <code>Genshin Impact</code>):", cancel_kb())


@router.message(OGame.name, F.text)
async def game_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:40])
    await state.set_state(OGame.server)
    await m.answer("Bu o'yinda donat uchun <b>Server ID</b> ham kerakmi? (Mobile Legends kabi)",
                   reply_markup=K([B("Ha, kerak", "o:gs:1"), B("Yo'q", "o:gs:0")], [B("✖️ Bekor qilish", "x:cancel")]))


@router.callback_query(OGame.server, F.data.startswith("o:gs:"))
async def game_server(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    server = int(c.data.split(":")[2])
    gid, _ = await run("INSERT INTO games(name,server,id_label) VALUES(?,?,?)", d["name"], server,
                       "User ID" if server else "Player ID")
    await state.clear()
    await show_game(c, gid)


@router.callback_query(F.data.startswith("o:pa:"))
async def packs_add(c: CallbackQuery, state: FSMContext):
    gid = int(c.data.split(":")[2])
    await state.set_state(OPack.bulk)
    await state.update_data(gid=gid)
    await screen(c, "owner",
                 "➕ <b>Paketlar</b>\n\nHar bir qatorda <code>nomi | narxi (so'm)</code> yozing. Bir nechta qator yuborsangiz hammasi qo'shiladi:\n\n"
                 "<code>60 UC | 12000\n325 UC | 62000\n660 UC | 120000</code>", cancel_kb())


@router.message(OPack.bulk, F.text)
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


@router.callback_query(F.data.startswith("o:pk:"))
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


@router.callback_query(F.data.startswith("o:pp:"))
async def pack_price(c: CallbackQuery, state: FSMContext):
    await state.set_state(OPack.price)
    await state.update_data(pid=int(c.data.split(":")[2]))
    await screen(c, "owner", "💰 Yangi narxni so'mda yuboring (masalan: <code>12000</code>):", cancel_kb())


@router.message(OPack.price, F.text)
async def pack_price_in(m: Message, state: FSMContext):
    digits = re.sub(r"\D", "", m.text)
    if not digits or int(digits) <= 0:
        return await m.answer("⚠️ Musbat son yuboring.", reply_markup=cancel_kb())
    pid = (await state.get_data())["pid"]
    await run("UPDATE packs SET price_uzs=?, active=1 WHERE id=?", int(digits), pid)
    await state.clear()
    await m.answer(f"✅ Narx: {fmt_uzs(int(digits))}", reply_markup=K([B("📦 Paketga qaytish", f"o:pk:{pid}")]))


@router.callback_query(F.data.startswith("o:ptg:"))
async def pack_toggle(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[2])
    await run("UPDATE packs SET active=1-active WHERE id=?", pid)
    await show_pack(c, pid)


@router.callback_query(F.data.startswith("o:pd:"))
async def pack_delete(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[2])
    p = await one("SELECT game_id FROM packs WHERE id=?", pid)
    await run("DELETE FROM packs WHERE id=?", pid)
    if p:
        await show_game(c, p["game_id"])
    else:
        await games_admin(c, state)


# ------------------------------------------------------------------ buyurtmalar

@router.callback_query(F.data == "o:ord")
async def orders(c: CallbackQuery):
    rows = await q("SELECT * FROM orders WHERE status IN ('pending','paid') AND kind IN ('item','game') ORDER BY id DESC LIMIT 15")
    kb = [[B(f"#{o['id']} · {'🎮' if o['kind'] == 'game' else '🛍'} {o['status']} · {fmt_uzs(o['uzs'])} · {o['method'] or '-'}",
             f"o:od:{o['id']}")] for o in rows]
    kb.append([B("⬅️ Orqaga", "o:home")])
    await screen(c, "owner", "🧾 <b>Buyurtmalar</b> (kutilayotgan / yetkazilmagan)" + ("" if rows else "\n\nHammasi joyida ✅"), K(*kb))


@router.callback_query(F.data.startswith("o:od:"))
async def order_view(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    if not o:
        return await c.answer("Topilmadi")
    text = (f"🧾 <b>Buyurtma #{oid}</b>\n{esc(await order_title(o))}\n"
            + (f"{fmt_meta(o['meta'])}\n" if o["meta"] else "")
            + f"Xaridor: <a href=\"tg://user?id={o['user_id']}\">{o['user_id']}</a>\n"
            f"Holat: {o['status']} · {o['method'] or '-'}\nNarx: {fmt_uzs(o['uzs'])} / {o['stars']}⭐ / {o['ton']} TON\n"
            f"Kod: <code>{o['code']}</code>\nVaqt: {fmt_ts(o['ts'])}")
    rows = []
    if o["status"] == "pending":
        rows.append([B("✅ To'langan deb tasdiqlash", f"o:cf:{oid}")])
    if o["status"] == "paid":
        rows.append([B("📦 Yetkazildi", f"o:dlv:{oid}")])
    rows.append([B("⬅️ Orqaga", "o:ord")])
    await screen(c, "owner", text, K(*rows))


@router.callback_query(F.data.startswith("o:rok:"))
async def receipt_ok(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    done = await fulfill(c.bot, oid, "card")
    await c.answer("✅ Tasdiqlandi" if done else "Buyurtma allaqachon ko'rib chiqilgan", show_alert=True)
    try:
        await c.message.edit_caption(caption=(c.message.caption or "") + ("\n\n✅ <b>Tasdiqlandi</b>" if done else "\n\nℹ️ Allaqachon ko'rib chiqilgan"))
    except Exception:
        pass


@router.callback_query(F.data.startswith("o:rno:"))
async def receipt_no(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    o = await one("SELECT * FROM orders WHERE id=?", oid)
    if o and o["status"] == "pending":
        await run("UPDATE orders SET status='rejected' WHERE id=?", oid)
        try:
            await c.bot.send_message(o["user_id"], f"❌ Buyurtma #{oid} bo'yicha to'lov tasdiqlanmadi. "
                                                   "Agar xato bo'lsa, admin bilan bog'laning.")
        except Exception:
            pass
    await c.answer("Rad etildi")
    try:
        await c.message.edit_caption(caption=(c.message.caption or "") + "\n\n❌ <b>Rad etildi</b>")
    except Exception:
        pass


@router.callback_query(F.data.startswith("o:cf:"))
async def order_confirm(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    await fulfill(c.bot, oid, "manual")
    await c.answer("Tasdiqlandi")
    await orders(c)


@router.callback_query(F.data.startswith("o:dlv:"))
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

@router.callback_query(F.data == "o:bc")
async def bc_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(OBc.msg)
    await screen(c, "owner", "📣 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring (matn, rasm, tugma — hammasi nusxalanadi):", cancel_kb())


@router.message(OBc.msg)
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

@router.message(Command("give"))
async def give(m: Message, command: CommandObject):
    a = (command.args or "").split()
    if len(a) != 2 or not a[0].isdigit() or not a[1].lstrip("-").isdigit():
        return await m.answer("Foydalanish: /give <user_id> <stars>")
    _, rc = await run("UPDATE users SET balance=balance+? WHERE id=?", int(a[1]), int(a[0]))
    await m.answer("✅ Bajarildi" if rc else "❌ Foydalanuvchi topilmadi")


@router.message(Command("ban", "unban"))
async def ban(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        return await m.answer(f"Foydalanish: /{command.command} <user_id>")
    _, rc = await run("UPDATE users SET banned=? WHERE id=?", 1 if command.command == "ban" else 0, int(arg))
    await m.answer("✅ Bajarildi" if rc else "❌ Foydalanuvchi topilmadi")
