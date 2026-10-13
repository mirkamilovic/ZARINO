"""Battle darajasidagi rollar. Egasi (battles.owner_id yoki global OWNER_ID) — barcha huquq.
manager — operatsion boshqaruv; moderator — ko'rish va fikrlarni moderatsiya. Rol har bir Battle uchun alohida.
Har bir handler `can()` bilan SERVERDA qayta tekshiradi (callback data'ga ishonilmaydi)."""
import time

from .engine import BattleError, log_event

ROLES = ("manager", "moderator")
MAX_ADMINS = 10
ALL_PERMS = {"view", "score", "round", "state", "settings", "feedback", "cancel", "roles"}
PERMS = {
    "owner": ALL_PERMS,
    "manager": {"view", "score", "round", "state", "settings", "feedback"},
    "moderator": {"view", "feedback"},
}
LABEL = {"owner": "👑 Egasi", "manager": "🛡 Admin", "moderator": "🧹 Moderator"}


async def role_of(t, b, uid: int, global_owner: int | None = None):
    """'owner' | 'manager' | 'moderator' | None."""
    if not b or uid is None:
        return None
    if b["owner_id"] == uid or (global_owner is not None and uid == global_owner):
        return "owner"
    r = await t.one("SELECT role FROM battle_admins WHERE battle_id=? AND user_id=?", b["id"], uid)
    return r["role"] if r else None


async def can(t, b, uid: int, perm: str, global_owner: int | None = None) -> bool:
    role = await role_of(t, b, uid, global_owner)
    return bool(role) and perm in PERMS.get(role, ())


async def admins(t, bid: int):
    return await t.query("SELECT a.user_id, a.role, u.name, u.username FROM battle_admins a LEFT JOIN users u ON u.id=a.user_id "
                         "WHERE a.battle_id=? ORDER BY a.role, a.created_at", bid)


async def admin_ids(t, bid: int) -> list[int]:
    return [r["user_id"] for r in await t.query("SELECT user_id FROM battle_admins WHERE battle_id=?", bid)]


async def grant(t, bid: int, uid: int, role: str, actor: int, now: int | None = None) -> str:
    """'added' | 'changed' | 'same'. Egaga rol berib bo'lmaydi; maksimum MAX_ADMINS."""
    if role not in ROLES:
        raise BattleError("❌ Noma'lum rol.")
    b = await t.one("SELECT owner_id FROM battles WHERE id=?", bid)
    if not b:
        raise BattleError("❌ Battle topilmadi.")
    if uid == b["owner_id"]:
        raise BattleError("❌ Battle egasiga rol berib bo'lmaydi.")
    ex = await t.one("SELECT role FROM battle_admins WHERE battle_id=? AND user_id=?", bid, uid)
    if ex and ex["role"] == role:
        return "same"
    if not ex and (await t.one("SELECT COUNT(*) c FROM battle_admins WHERE battle_id=?", bid))["c"] >= MAX_ADMINS:
        raise BattleError(f"❌ Adminlar soni {MAX_ADMINS} tadan oshmasligi kerak.")
    ts = now or int(time.time())
    await t.execute("INSERT INTO battle_admins(battle_id,user_id,role,added_by,created_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(battle_id,user_id) DO UPDATE SET role=excluded.role, added_by=excluded.added_by", bid, uid, role, actor, ts)
    await log_event(t, "battle.role_granted", battle_id=bid, user_id=uid, source=role, meta={"actor": actor}, ts=ts)
    return "changed" if ex else "added"


async def revoke(t, bid: int, uid: int, actor: int, now: int | None = None) -> bool:
    _, rc = await t.execute("DELETE FROM battle_admins WHERE battle_id=? AND user_id=?", bid, uid)
    if rc:
        await log_event(t, "battle.role_revoked", battle_id=bid, user_id=uid, meta={"actor": actor}, ts=now)
    return bool(rc)
