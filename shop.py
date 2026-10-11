from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from ..db import q, one, SI
from ..kb import K, B, HOME
from ..services import new_order, calc_uzs, fmt_uzs
from ..ui import screen, esc
from .pay import checkout

router = Router()
router.message.filter(F.chat.type == "private")

KINDS = [("premium", "💎 Premium"), ("stars", "⭐ Stars"), ("gift", "🎁 Giftlar"), ("nft", "🖼 NFT")]


def price_label(it, rate: int) -> str:
    return fmt_uzs(calc_uzs(it["price_stars"], rate))


@router.message(Command("shop"))
@router.callback_query(F.data == "m:shop")
async def shop(ev, state: FSMContext):
    await state.clear()
    rows = [[B("🎮 O'yin donati", "g:list")],
            [B(KINDS[0][1], "s:k:premium"), B(KINDS[1][1], "s:k:stars")],
            [B(KINDS[2][1], "s:k:gift"), B(KINDS[3][1], "s:k:nft")], [HOME]]
    await screen(ev, "shop", "🛍 <b>Do'kon</b>\n\nStars, Premium, Giftlar va NFT — bitta joyda.\nBo'limni tanlang 👇", K(*rows))


@router.callback_query(F.data.startswith("s:k:"))
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
    if it["kind"] == "nft" and it["payload"].startswith("http"):
        text += f"\n🔗 <a href=\"{esc(it['payload'])}\">NFT haqida</a>"
    await screen(ev, "shop", text, K([B("🛒 Sotib olish", f"s:b:{iid}")], [B("⬅️ Orqaga", f"s:k:{it['kind']}")]))


@router.callback_query(F.data.startswith("s:i:"))
async def item(c: CallbackQuery):
    await show_item(c, int(c.data.split(":")[2]))


@router.callback_query(F.data.startswith("s:b:"))
async def buy(c: CallbackQuery):
    iid = int(c.data.split(":")[2])
    it = await one("SELECT * FROM items WHERE id=? AND active=1", iid)
    if not it:
        return await c.answer("Mahsulot topilmadi", show_alert=True)
    await checkout(c, await new_order(c.from_user.id, "item", it["price_stars"], iid))
