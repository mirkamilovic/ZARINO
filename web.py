import hashlib
import hmac
import io
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot
from aiogram.types import LabeledPrice

from . import db
from .config import BOT_TOKEN, WEB_HOST, WEB_PORT, WEBAPP_URL
from .db import q, one, run, S, SI
from .services import bot_username, new_order, order_title, is_vip, fmt_ts, leaderboard, calc_uzs
from .assets import DIR

log = logging.getLogger(__name__)
ROOT = os.path.join(os.path.dirname(__file__), "..", "webapp")


def check_init(init_data: str):
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True))
        h = d.pop("hash")
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(d.get("auth_date", 0)) > 86400:
            return None
        return json.loads(d["user"])
    except Exception:
        return None


def auth(handler):
    async def wrapper(req: web.Request):
        u = check_init(req.headers.get("X-Init-Data", ""))
        if not u:
            return web.json_response({"error": "Avtorizatsiya xatosi"}, status=401)
        row = await one("SELECT banned FROM users WHERE id=?", u["id"])
        if not row:
            # foydalanuvchi botni hali ishga tushirmagan bo'lsa ham profil yaratamiz
            await run("INSERT OR IGNORE INTO users(id,name,username,joined) VALUES(?,?,?,?)",
                      u["id"], (u.get("first_name", "") + " " + u.get("last_name", "")).strip(), u.get("username"), int(time.time()))
        elif row["banned"]:
            return web.json_response({"error": "Bloklangansiz"}, status=403)
        req["uid"] = u["id"]
        return await handler(req)
    return wrapper


@auth
async def state(req: web.Request):
    uid = req["uid"]
    u = await one("SELECT * FROM users WHERE id=?", uid)
    battles = []
    for b in await q("SELECT b.id,b.title,b.points,b.end_ts,(SELECT COUNT(*) FROM participants WHERE battle_id=b.id) n "
                     "FROM battles b WHERE b.status='active' ORDER BY b.id DESC LIMIT 20"):
        top = [{"nick": r["nick"], "points": r["points"]} for r in (await leaderboard(b["id"]))[:5]]
        battles.append({"id": b["id"], "title": b["title"], "points": b["points"], "n": b["n"],
                        "ends": fmt_ts(b["end_ts"]) if b["end_ts"] else "", "top": top})
    rate = await SI("uzs_rate", 230)
    items = [dict(id=i["id"], kind=i["kind"], title=i["title"], descr=i["descr"], stars=i["price_stars"],
                  uzs=calc_uzs(i["price_stars"], rate))
             for i in await q("SELECT * FROM items WHERE active=1 ORDER BY kind, price_stars")]
    games = []
    for g in await q("SELECT * FROM games WHERE active=1 ORDER BY id"):
        packs = await q("SELECT id,title,price_uzs FROM packs WHERE game_id=? AND active=1 AND price_uzs>0 ORDER BY price_uzs", g["id"])
        if packs:
            games.append({"id": g["id"], "name": g["name"], "emoji": g["emoji"],
                          "packs": [{"id": p["id"], "title": p["title"], "uzs": p["price_uzs"]} for p in packs]})
    return web.json_response({
        "games": games, "brand": await S("brand"), "bot": await bot_username(req.app["bot"]), "ton_rate": await SI("ton_rate", 200),
        "user": {"id": uid, "name": u["name"], "balance": u["balance"], "credits": u["credits"],
                 "vip": fmt_ts(u["vip_until"]) if is_vip(u) else "", "wallet": u["wallet"]},
        "battles": battles, "items": items})


async def _invoice(req, order_id):
    bot: Bot = req.app["bot"]
    o = await one("SELECT * FROM orders WHERE id=?", order_id)
    title = (await order_title(o))[:32]
    link = await bot.create_invoice_link(title=title, description=f"Buyurtma #{order_id}", payload=f"o:{order_id}",
                                         currency="XTR", prices=[LabeledPrice(label=title, amount=o["stars"])])
    return web.json_response({"link": link})


@auth
async def buy(req: web.Request):
    body = await req.json()
    it = await one("SELECT * FROM items WHERE id=? AND active=1", int(body.get("item_id", 0)))
    if not it:
        return web.json_response({"error": "Mahsulot topilmadi"})
    if it["kind"] == "stars":
        u = await bot_username(req.app["bot"])
        return web.json_response({"bot": f"https://t.me/{u}?start=buy_{it['id']}"})
    return await _invoice(req, await new_order(req["uid"], "item", it["price_stars"], it["id"]))


@auth
async def topup(req: web.Request):
    body = await req.json()
    amount = int(body.get("stars", 0))
    if not 10 <= amount <= 10000:
        return web.json_response({"error": "Miqdor 10 dan 10000 gacha bo'lsin"})
    return await _invoice(req, await new_order(req["uid"], "topup", amount))


@auth
async def wallet(req: web.Request):
    body = await req.json()
    addr = str(body.get("address", "")).strip()[:80]
    await run("UPDATE users SET wallet=? WHERE id=?", addr, req["uid"])
    return web.json_response({"ok": True})


async def img(req: web.Request):
    key = req.match_info["key"]
    if not key.isalnum():
        raise web.HTTPNotFound()
    fid = await S(f"img_{key}")
    if fid:
        try:
            buf = io.BytesIO()
            await req.app["bot"].download(fid, destination=buf)
            return web.Response(body=buf.getvalue(), content_type="image/jpeg", headers={"Cache-Control": "max-age=60"})
        except Exception:
            pass
    path = os.path.join(DIR, f"{key}.jpg")
    if os.path.exists(path):
        return web.FileResponse(path)
    raise web.HTTPNotFound()


async def index(req):
    return web.FileResponse(os.path.join(ROOT, "index.html"), headers={"Cache-Control": "no-cache"})


async def manifest(req):
    base = WEBAPP_URL or f"{req.scheme}://{req.host}"
    return web.json_response({"url": base, "name": await S("brand"), "iconUrl": f"{base}/img/home"})


async def start(bot: Bot):
    app = web.Application()
    app["bot"] = bot
    app.add_routes([
        web.get("/app", index), web.get("/health", lambda r: web.Response(text="ok")),
        web.get("/tonconnect-manifest.json", manifest), web.get("/img/{key}", img),
        web.get("/api/state", state), web.post("/api/buy", buy),
        web.post("/api/topup", topup), web.post("/api/wallet", wallet),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    log.info("Mini App serveri: %s:%s", WEB_HOST, WEB_PORT)
    return runner
