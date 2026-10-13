"""Battle Engine sxemasi (migratsiya 5). Mavjud jadvallar (battles, participants, rounds, scored, plog) saqlanadi,
faqat kengaytiriladi. Eski kod `battles.status` ('active'/'ended') ga tayanadi, shuning uchun u `state` bilan sinxron yuritiladi."""

SOURCES = ("reaction", "comment", "star", "manual", "bonus", "penalty", "legacy")

M5 = [
    "ALTER TABLE battles ADD COLUMN btype TEXT NOT NULL DEFAULT 'vote'",
    "ALTER TABLE battles ADD COLUMN state TEXT NOT NULL DEFAULT 'ACTIVE'",
    "ALTER TABLE battles ADD COLUMN finished_at INTEGER",
    "ALTER TABLE battles ADD COLUMN reward TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE participants ADD COLUMN joined_at INTEGER",
    "ALTER TABLE participants ADD COLUMN meta TEXT NOT NULL DEFAULT ''",   # NFT/avatar va h.k. (JSON)
    "ALTER TABLE rounds ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE rounds ADD COLUMN start_at INTEGER",
    "ALTER TABLE rounds ADD COLUMN end_at INTEGER",
    """CREATE TABLE IF NOT EXISTS battle_settings(
        battle_id INTEGER PRIMARY KEY REFERENCES battles(id),
        reaction_score INTEGER NOT NULL DEFAULT 1, comment_score INTEGER NOT NULL DEFAULT 3,
        star_score INTEGER NOT NULL DEFAULT 1, participant_limit INTEGER NOT NULL DEFAULT 0,
        comments_per_round INTEGER NOT NULL DEFAULT 1, allow_negative INTEGER NOT NULL DEFAULT 0,
        reaction_weights TEXT NOT NULL DEFAULT '{}', auto_finish INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS battle_score_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, battle_id INTEGER NOT NULL REFERENCES battles(id),
        round_id INTEGER, user_id INTEGER NOT NULL, source TEXT NOT NULL, amount INTEGER NOT NULL,
        idem_key TEXT NOT NULL, actor_id INTEGER, reason TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
        UNIQUE(battle_id, idem_key))""",
    "CREATE INDEX IF NOT EXISTS ix_bse_user ON battle_score_events(battle_id, user_id, source)",
    """CREATE TABLE IF NOT EXISTS battle_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, battle_id INTEGER, round_id INTEGER, user_id INTEGER,
        action TEXT NOT NULL, amount INTEGER, source TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_bev_battle ON battle_events(battle_id, id)",
    """CREATE TABLE IF NOT EXISTS battle_winners(
        battle_id INTEGER NOT NULL REFERENCES battles(id), rank INTEGER NOT NULL, user_id INTEGER NOT NULL,
        score INTEGER NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(battle_id, rank))""",
    """CREATE TABLE IF NOT EXISTS contest_draws(
        battle_id INTEGER PRIMARY KEY REFERENCES battles(id), seed TEXT NOT NULL, candidates_hash TEXT NOT NULL,
        candidate_count INTEGER NOT NULL, winner_id INTEGER NOT NULL, selected_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS battle_feedback(
        id INTEGER PRIMARY KEY AUTOINCREMENT, battle_id INTEGER NOT NULL REFERENCES battles(id), user_id INTEGER NOT NULL,
        text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'visible', created_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_bfb ON battle_feedback(battle_id, user_id, created_at)",
    """CREATE TABLE IF NOT EXISTS nft_entries(
        battle_id INTEGER NOT NULL REFERENCES battles(id), user_id INTEGER NOT NULL, name TEXT NOT NULL,
        collection TEXT NOT NULL DEFAULT '', rarity TEXT NOT NULL DEFAULT '', image_file_id TEXT NOT NULL DEFAULT '',
        meta TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL, PRIMARY KEY(battle_id, user_id))""",
    "CREATE INDEX IF NOT EXISTS ix_part_rank ON participants(battle_id, points DESC)",
    "UPDATE battles SET state = CASE status WHEN 'active' THEN 'ACTIVE' ELSE 'FINISHED' END",
    "UPDATE participants SET joined_at = COALESCE(joined_at, 0)",
    "INSERT OR IGNORE INTO battle_settings(battle_id, comment_score) SELECT id, points FROM battles",
    # eski ballar «legacy» manbasi bilan hisobga olinadi: breakdown va keshning yig'indisi mos bo'lsin
    """INSERT OR IGNORE INTO battle_score_events(battle_id,user_id,source,amount,idem_key,reason,created_at)
        SELECT battle_id,user_id,'legacy',points,'legacy:'||battle_id||':'||user_id,'migration',strftime('%s','now')
        FROM participants WHERE points<>0""",
]
