"""Battle platformasi handlerlari. bot.py'dagi umumiy yordamchilar (screen, K, B, DB funksiyalari) `g` (bot.py globals) orqali olinadi —
shu bilan bot.py ↔ battle paketi o'rtasida aylanma import bo'lmaydi. Biznes-logika engine.py'da; bu yerda faqat Telegram qatlami."""
import json
import re
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InputMediaPhoto, Message, MessageReactionUpdated

from . import engine as BE
from . import roles as BR
from . import views as V
from .texts import T

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
