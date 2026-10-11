from aiogram.types import InlineKeyboardMarkup as M, InlineKeyboardButton as IB, WebAppInfo
from .config import OWNER_ID, WEBAPP_URL


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
