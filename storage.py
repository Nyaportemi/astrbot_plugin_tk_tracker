import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional


class RecordStorage:
    """使用 SQLite 增量保存违规记录，避免整库常驻内存和反复重写。"""

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
        # DELETE 模式不会长期保留 WAL 文件，更适合本插件低频、小批量写入的场景。
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    reason TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_records_player_id_id
                ON records(player_id, id DESC);

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) WITHOUT ROWID;
                """
            )

    def add_record(self, player_id: str, occurred_at: str, reason: str) -> int:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO records(player_id, occurred_at, reason) VALUES (?, ?, ?)",
                (player_id, occurred_at, reason),
            )
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM records WHERE player_id = ?",
                (player_id,),
            ).fetchone()
        return int(row["count"])

    def get_player_records(self, player_id: str, limit: int = 5) -> tuple[int, list[dict]]:
        safe_limit = max(1, min(int(limit), 50))
        with self._lock:
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM records WHERE player_id = ?",
                (player_id,),
            ).fetchone()
            rows = self._conn.execute(
                """
                SELECT occurred_at, reason
                FROM records
                WHERE player_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (player_id, safe_limit),
            ).fetchall()

        # 与旧版一致：最近几条记录按时间从早到晚展示。
        recent = [
            {"time": row["occurred_at"], "reason": row["reason"]}
            for row in reversed(rows)
        ]
        return int(count_row["count"]), recent

    def get_leaderboard(self, limit: int = 10) -> list[tuple[str, int]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT player_id, COUNT(*) AS kick_count
                FROM records
                GROUP BY player_id
                ORDER BY kick_count DESC, player_id COLLATE NOCASE ASC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [(row["player_id"], int(row["kick_count"])) for row in rows]

    def delete_player(self, player_id: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM records WHERE player_id = ?",
                (player_id,),
            )
        return int(cursor.rowcount)

    def clear_all(self) -> int:
        with self._lock, self._conn:
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM records"
            ).fetchone()
            self._conn.execute("DELETE FROM records")
        return int(count_row["count"])

    def migrate_legacy_json(self, candidates: Iterable[Path]) -> tuple[int, Optional[Path]]:
        """首次启动时导入一个旧版 JSON 文件；之后不会重复导入。"""
        with self._lock:
            migrated = self._conn.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (self.MIGRATION_KEY,),
            ).fetchone()
            if migrated is not None:
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
            self._mark_migration_done("no_legacy_file")
            return 0, None

        with source.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("旧版 records.json 顶层结构必须是对象")

        imported = 0
        batch: list[tuple[str, str, str]] = []

        def flush() -> None:
            nonlocal batch
            if not batch:
                return
            self._conn.executemany(
                "INSERT INTO records(player_id, occurred_at, reason) VALUES (?, ?, ?)",
                batch,
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
                    occurred_at = str(record.get("time", "未知时间")).strip() or "未知时间"
                    reason = str(record.get("reason", "未知原因")).strip() or "未知原因"
                    batch.append((player_id, occurred_at, reason))
                    imported += 1
                    if len(batch) >= 500:
                        flush()

            flush()
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                (self.MIGRATION_KEY, str(source)),
            )

        return imported, source

    def _mark_migration_done(self, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                (self.MIGRATION_KEY, value),
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
