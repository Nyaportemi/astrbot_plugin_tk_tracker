import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


class RecordStorage:
    """SQLite 存储层：会话隔离、消息去重和常数时间排行榜统计。"""

    SCHEMA_VERSION = 2
    LEGACY_SCOPE = "__legacy_global__"
    MIGRATION_KEY = "legacy_json_migration_v1"

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=5,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._create_schema()

    def _configure(self) -> None:
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_id TEXT NOT NULL DEFAULT '{self.LEGACY_SCOPE}',
                    server_name TEXT NOT NULL DEFAULT '',
                    player_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    occurred_ts INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL,
                    source_bot_id TEXT NOT NULL DEFAULT '',
                    message_key TEXT
                );

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS player_stats (
                    scope_id TEXT NOT NULL,
                    server_name TEXT NOT NULL,
                    player_id TEXT NOT NULL,
                    kick_count INTEGER NOT NULL,
                    last_record_id INTEGER NOT NULL,
                    PRIMARY KEY(scope_id, server_name, player_id)
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_id TEXT NOT NULL,
                    operator_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )

            # 兼容 1.1.0 数据库：只增加字段，不复制整表。
            self._ensure_column(
                "records", "scope_id", f"TEXT NOT NULL DEFAULT '{self.LEGACY_SCOPE}'"
            )
            self._ensure_column("records", "server_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("records", "occurred_ts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("records", "source_bot_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("records", "message_key", "TEXT")

            self._conn.execute("DROP INDEX IF EXISTS idx_records_player_id_id")
            self._conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_records_scope_player
                ON records(scope_id, player_id, server_name, id DESC);

                CREATE INDEX IF NOT EXISTS idx_records_scope_time
                ON records(scope_id, occurred_ts DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_records_message_key
                ON records(scope_id, message_key)
                WHERE message_key IS NOT NULL AND message_key <> '';

                CREATE INDEX IF NOT EXISTS idx_audit_scope_time
                ON audit_log(scope_id, id DESC);
                """
            )

            version = self._get_metadata_locked("schema_version")
            if version != str(self.SCHEMA_VERSION):
                self._conn.execute(
                    """
                    UPDATE records SET server_name = '历史数据'
                    WHERE scope_id = ? AND server_name = ''
                    """,
                    (self.LEGACY_SCOPE,),
                )
                self._conn.execute(
                    """
                    UPDATE records
                    SET occurred_ts = COALESCE(
                        CAST(strftime('%s', occurred_at) AS INTEGER), 0
                    )
                    WHERE occurred_ts = 0
                    """
                )
                self._rebuild_stats_locked()
                self._set_metadata_locked("schema_version", str(self.SCHEMA_VERSION))

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _get_metadata_locked(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _set_metadata_locked(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            (key, value),
        )

    def _rebuild_stats_locked(self) -> None:
        self._conn.execute("DELETE FROM player_stats")
        self._conn.execute(
            """
            INSERT INTO player_stats(
                scope_id, server_name, player_id, kick_count, last_record_id
            )
            SELECT scope_id, server_name, player_id, COUNT(*), MAX(id)
            FROM records
            GROUP BY scope_id, server_name, player_id
            """
        )

    def bind_legacy_scope(self, scope_id: str) -> int:
        """把无法识别会话的旧数据一次性绑定到首次使用的会话。"""
        if not scope_id or scope_id == self.LEGACY_SCOPE:
            return 0
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM records WHERE scope_id = ?",
                (self.LEGACY_SCOPE,),
            ).fetchone()
            count = int(row["count"])
            if count:
                self._conn.execute(
                    "UPDATE records SET scope_id = ? WHERE scope_id = ?",
                    (scope_id, self.LEGACY_SCOPE),
                )
                self._rebuild_stats_locked()
                self._set_metadata_locked("legacy_bound_scope", scope_id)
            return count

    def add_record(
        self,
        scope_id: str,
        server_name: str,
        player_id: str,
        occurred_at: str,
        occurred_ts: int,
        reason: str,
        source_bot_id: str,
        message_key: Optional[str],
        duplicate_window_seconds: int,
    ) -> tuple[bool, int, int]:
        """返回 (是否新增, 记录编号, 玩家在当前服务器的累计次数)。"""
        with self._lock, self._conn:
            if message_key:
                duplicate = self._conn.execute(
                    """
                    SELECT id FROM records
                    WHERE scope_id = ? AND message_key = ?
                    """,
                    (scope_id, message_key),
                ).fetchone()
            elif duplicate_window_seconds > 0:
                duplicate = self._conn.execute(
                    """
                    SELECT id FROM records
                    WHERE scope_id = ? AND server_name = ? AND player_id = ?
                      AND reason = ? AND source_bot_id = ? AND occurred_ts >= ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        scope_id,
                        server_name,
                        player_id,
                        reason,
                        source_bot_id,
                        occurred_ts - max(duplicate_window_seconds, 0),
                    ),
                ).fetchone()
            else:
                duplicate = None

            if duplicate is not None:
                count = self._player_count_locked(scope_id, player_id, server_name)
                return False, int(duplicate["id"]), count

            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO records(
                        scope_id, server_name, player_id, occurred_at, occurred_ts,
                        reason, source_bot_id, message_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope_id,
                        server_name,
                        player_id,
                        occurred_at,
                        occurred_ts,
                        reason,
                        source_bot_id,
                        message_key,
                    ),
                )
            except sqlite3.IntegrityError:
                duplicate = self._conn.execute(
                    "SELECT id FROM records WHERE scope_id = ? AND message_key = ?",
                    (scope_id, message_key),
                ).fetchone()
                count = self._player_count_locked(scope_id, player_id, server_name)
                return False, int(duplicate["id"]), count

            record_id = int(cursor.lastrowid)
            self._conn.execute(
                """
                INSERT INTO player_stats(
                    scope_id, server_name, player_id, kick_count, last_record_id
                ) VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(scope_id, server_name, player_id) DO UPDATE SET
                    kick_count = kick_count + 1,
                    last_record_id = excluded.last_record_id
                """,
                (scope_id, server_name, player_id, record_id),
            )
            count = self._player_count_locked(scope_id, player_id, server_name)
            return True, record_id, count

    def _player_count_locked(
        self, scope_id: str, player_id: str, server_name: Optional[str] = None
    ) -> int:
        sql = """
            SELECT COALESCE(SUM(kick_count), 0) AS count
            FROM player_stats WHERE scope_id = ? AND player_id = ?
        """
        params: list[object] = [scope_id, player_id]
        if server_name is not None:
            sql += " AND server_name = ?"
            params.append(server_name)
        row = self._conn.execute(sql, params).fetchone()
        return int(row["count"])

    def get_player_records(
        self,
        scope_id: str,
        player_id: str,
        limit: int = 5,
        server_name: Optional[str] = None,
    ) -> tuple[int, list[dict]]:
        safe_limit = max(1, min(int(limit), 50))
        sql = """
            SELECT id, server_name, occurred_at, reason
            FROM records WHERE scope_id = ? AND player_id = ?
        """
        params: list[object] = [scope_id, player_id]
        if server_name is not None:
            sql += " AND server_name = ?"
            params.append(server_name)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(safe_limit)

        with self._lock:
            count = self._player_count_locked(scope_id, player_id, server_name)
            rows = self._conn.execute(sql, params).fetchall()
        recent = [
            {
                "id": int(row["id"]),
                "server": row["server_name"],
                "time": row["occurred_at"],
                "reason": row["reason"],
            }
            for row in reversed(rows)
        ]
        return count, recent

    def get_leaderboard(
        self,
        scope_id: str,
        limit: int = 10,
        server_name: Optional[str] = None,
    ) -> list[tuple[str, int]]:
        safe_limit = max(1, min(int(limit), 100))
        sql = """
            SELECT player_id, SUM(kick_count) AS kick_count
            FROM player_stats WHERE scope_id = ?
        """
        params: list[object] = [scope_id]
        if server_name is not None:
            sql += " AND server_name = ?"
            params.append(server_name)
        sql += """
            GROUP BY player_id
            ORDER BY kick_count DESC, player_id COLLATE NOCASE ASC
            LIMIT ?
        """
        params.append(safe_limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [(row["player_id"], int(row["kick_count"])) for row in rows]

    def get_stats(
        self,
        scope_id: str,
        days: int,
        now_ts: int,
        server_name: Optional[str] = None,
    ) -> dict:
        since_ts = now_ts - max(1, days) * 86400
        where = "scope_id = ? AND occurred_ts >= ?"
        params: list[object] = [scope_id, since_ts]
        if server_name is not None:
            where += " AND server_name = ?"
            params.append(server_name)
        with self._lock:
            totals = self._conn.execute(
                f"""
                SELECT COUNT(*) AS records, COUNT(DISTINCT player_id) AS players
                FROM records WHERE {where}
                """,
                params,
            ).fetchone()
            repeated = self._conn.execute(
                f"""
                SELECT COUNT(*) AS count FROM (
                    SELECT player_id FROM records WHERE {where}
                    GROUP BY player_id HAVING COUNT(*) > 1
                )
                """,
                params,
            ).fetchone()
            reasons = self._conn.execute(
                f"""
                SELECT reason, COUNT(*) AS count FROM records WHERE {where}
                GROUP BY reason ORDER BY count DESC, reason ASC LIMIT 5
                """,
                params,
            ).fetchall()
        return {
            "records": int(totals["records"]),
            "players": int(totals["players"]),
            "repeated_players": int(repeated["count"]),
            "reasons": [(row["reason"], int(row["count"])) for row in reasons],
        }

    def delete_record(
        self, scope_id: str, record_id: int, operator_id: str, occurred_at: str
    ) -> Optional[dict]:
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT id, server_name, player_id, occurred_at, reason
                FROM records WHERE scope_id = ? AND id = ?
                """,
                (scope_id, record_id),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            self._conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
            self._refresh_player_stat_locked(
                scope_id, row["server_name"], row["player_id"]
            )
            self._write_audit_locked(
                scope_id,
                operator_id,
                "delete_record",
                f"record_id={record_id};player_id={row['player_id']}",
                occurred_at,
            )
            return result

    def _refresh_player_stat_locked(
        self, scope_id: str, server_name: str, player_id: str
    ) -> None:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(MAX(id), 0) AS last_id
            FROM records
            WHERE scope_id = ? AND server_name = ? AND player_id = ?
            """,
            (scope_id, server_name, player_id),
        ).fetchone()
        if int(row["count"]) == 0:
            self._conn.execute(
                """
                DELETE FROM player_stats
                WHERE scope_id = ? AND server_name = ? AND player_id = ?
                """,
                (scope_id, server_name, player_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE player_stats SET kick_count = ?, last_record_id = ?
                WHERE scope_id = ? AND server_name = ? AND player_id = ?
                """,
                (
                    int(row["count"]),
                    int(row["last_id"]),
                    scope_id,
                    server_name,
                    player_id,
                ),
            )

    def delete_player(
        self,
        scope_id: str,
        player_id: str,
        operator_id: str,
        occurred_at: str,
        server_name: Optional[str] = None,
    ) -> int:
        where = "scope_id = ? AND player_id = ?"
        params: list[object] = [scope_id, player_id]
        if server_name is not None:
            where += " AND server_name = ?"
            params.append(server_name)
        with self._lock, self._conn:
            cursor = self._conn.execute(f"DELETE FROM records WHERE {where}", params)
            stats_where = where
            self._conn.execute(f"DELETE FROM player_stats WHERE {stats_where}", params)
            deleted = int(cursor.rowcount)
            if deleted:
                detail = f"player_id={player_id};count={deleted}"
                if server_name is not None:
                    detail += f";server={server_name}"
                self._write_audit_locked(
                    scope_id, operator_id, "delete_player", detail, occurred_at
                )
            return deleted

    def count_scope(self, scope_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM records WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
        return int(row["count"])

    def clear_scope(
        self, scope_id: str, operator_id: str, occurred_at: str
    ) -> int:
        with self._lock, self._conn:
            count = self.count_scope(scope_id)
            self._conn.execute("DELETE FROM records WHERE scope_id = ?", (scope_id,))
            self._conn.execute(
                "DELETE FROM player_stats WHERE scope_id = ?", (scope_id,)
            )
            self._write_audit_locked(
                scope_id,
                operator_id,
                "clear_scope",
                f"count={count}",
                occurred_at,
            )
            return count

    def _write_audit_locked(
        self,
        scope_id: str,
        operator_id: str,
        action: str,
        detail: str,
        occurred_at: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO audit_log(scope_id, operator_id, action, detail, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scope_id, operator_id, action, detail, occurred_at),
        )

    def migrate_legacy_json(self, candidates: Iterable[Path]) -> tuple[int, Optional[Path]]:
        with self._lock:
            if self._get_metadata_locked(self.MIGRATION_KEY) is not None:
                return 0, None

        unique_candidates: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            candidate = Path(candidate)
            try:
                key = str(candidate.resolve())
            except OSError:
                key = str(candidate)
            if key not in seen:
                seen.add(key)
                unique_candidates.append(candidate)

        source = next((path for path in unique_candidates if path.is_file()), None)
        if source is None:
            with self._lock, self._conn:
                self._set_metadata_locked(self.MIGRATION_KEY, "no_legacy_file")
            return 0, None

        with source.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("旧版 records.json 顶层结构必须是对象")

        imported = 0
        batch: list[tuple[str, str, int, str]] = []

        def flush() -> None:
            nonlocal batch
            if batch:
                self._conn.executemany(
                    """
                    INSERT INTO records(
                        scope_id, server_name, player_id, occurred_at,
                        occurred_ts, reason
                    ) VALUES (?, '历史数据', ?, ?, ?, ?)
                    """,
                    [
                        (self.LEGACY_SCOPE, player, time_text, time_ts, reason)
                        for player, time_text, time_ts, reason in batch
                    ],
                )
                batch = []

        with self._lock, self._conn:
            for raw_player_id, raw_records in data.items():
                player_id = str(raw_player_id).strip()
                if not player_id:
                    continue
                if isinstance(raw_records, int):
                    records = (
                        {"time": "未知时间", "reason": "未知原因"}
                        for _ in range(max(raw_records, 0))
                    )
                elif isinstance(raw_records, list):
                    records = raw_records
                else:
                    continue

                for record in records:
                    if not isinstance(record, dict):
                        continue
                    time_text = str(record.get("time", "未知时间")).strip() or "未知时间"
                    reason = str(record.get("reason", "未知原因")).strip() or "未知原因"
                    try:
                        time_ts = int(datetime.strptime(time_text, "%Y-%m-%d %H:%M:%S").timestamp())
                    except ValueError:
                        time_ts = 0
                    batch.append((player_id, time_text, time_ts, reason))
                    imported += 1
                    if len(batch) >= 500:
                        flush()
            flush()
            self._rebuild_stats_locked()
            self._set_metadata_locked(self.MIGRATION_KEY, str(source))
        return imported, source

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
