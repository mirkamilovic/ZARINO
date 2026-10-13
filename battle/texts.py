"""Matnlar (lokalizatsiya). Hozir UZ; RU/EN qo'shish uchun TEXTS ga yangi kalit-til qo'shing, T(lang=...) ishlatiladi."""
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
