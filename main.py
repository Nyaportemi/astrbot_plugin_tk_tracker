import asyncio
import hashlib
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .storage import RecordStorage

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except ImportError:  # 兼容旧版 AstrBot
    get_astrbot_plugin_data_path = None


PLUGIN_NAME = "astrbot_plugin_tk_tracker"
PLUGIN_VERSION = "1.3.0"
KICK_PATTERN = re.compile(
    r"踢出玩家\s+(?P<player_id>\S+)\s+成功.*?原因[:：]\s*(?P<reason>.+)",
    re.DOTALL,
)
MAX_PLAYER_ID_LENGTH = 64
MAX_REASON_LENGTH = 500
CLEAR_CONFIRM_SECONDS = 30
MAINTENANCE_INTERVAL_SECONDS = 6 * 60 * 60


def _clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


class TKTrackerPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.plugin_config = config or {}
        self.super_admins = {
            str(admin_id) for admin_id in self.plugin_config.get("super_admins", [])
        }
        self.allowed_bot_ids = {
            str(bot_id) for bot_id in self.plugin_config.get("allowed_bot_ids", [])
        }
        self.auto_reply = bool(self.plugin_config.get("auto_reply", True))
        self.query_limit = _clamp_int(
            self.plugin_config.get("query_limit", 5), 5, 1, 20
        )
        self.leaderboard_limit = _clamp_int(
            self.plugin_config.get("leaderboard_limit", 10), 10, 1, 30
        )
        self.default_stats_days = _clamp_int(
            self.plugin_config.get("default_stats_days", 7), 7, 1, 365
        )
        self.duplicate_window_seconds = _clamp_int(
            self.plugin_config.get("duplicate_window_seconds", 30), 30, 0, 600
        )
        self.command_cooldown_seconds = _clamp_int(
            self.plugin_config.get("command_cooldown_seconds", 2), 2, 0, 60
        )
        self.risk_alert_enabled = bool(
            self.plugin_config.get("risk_alert_enabled", False)
        )
        self.risk_threshold = _clamp_int(
            self.plugin_config.get("risk_threshold", 3), 3, 2, 100
        )
        self.risk_window_days = _clamp_int(
            self.plugin_config.get("risk_window_days", 7), 7, 1, 365
        )
        self.risk_alert_cooldown_seconds = _clamp_int(
            self.plugin_config.get("risk_alert_cooldown_seconds", 3600),
            3600,
            0,
            86400,
        )
        self.retention_days = _clamp_int(
            self.plugin_config.get("retention_days", 0), 0, 0, 3650
        )
        self.audit_retention_days = _clamp_int(
            self.plugin_config.get("audit_retention_days", 180), 180, 0, 3650
        )
        self.export_limit = _clamp_int(
            self.plugin_config.get("export_limit", 5000), 5000, 100, 50000
        )
        self.backup_keep_count = _clamp_int(
            self.plugin_config.get("backup_keep_count", 10), 10, 1, 100
        )
        self.export_keep_count = _clamp_int(
            self.plugin_config.get("export_keep_count", 20), 20, 1, 200
        )
        self.backup_before_clear = bool(
            self.plugin_config.get("backup_before_clear", True)
        )
        self.bot_server_names = self._parse_server_map(
            self.plugin_config.get("bot_server_map", [])
        )
        self.pending_clears: dict[tuple[str, str], float] = {}
        self.bound_scopes: set[str] = set()
        self.command_last_used: dict[tuple[str, str, str], float] = {}
        self.risk_last_alerted: dict[tuple[str, str, str], float] = {}
        self._pending_storage_jobs = 0
        self._pending_storage_lock = threading.Lock()
        self._last_maintenance_monotonic = 0.0
        self._terminated = False

        self.plugin_dir = Path(__file__).resolve().parent
        if get_astrbot_plugin_data_path is not None:
            self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        else:
            self.data_dir = self.plugin_dir / "data" / "tk_tracker"

        self.storage = RecordStorage(self.data_dir / "records.db")
        self._storage_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tk-tracker-db",
        )
        legacy_candidates = (
            self.plugin_dir / "data" / "tk_tracker" / "records.json",
            Path.cwd() / "data" / "tk_tracker" / "records.json",
            self.data_dir / "records.json",
        )
        try:
            imported, source = self.storage.migrate_legacy_json(legacy_candidates)
            if source is not None:
                logger.info(f"已从旧版数据文件 {source} 迁移 {imported} 条记录。")
        except Exception as exc:
            logger.error(f"迁移旧版违规记录失败: {exc}", exc_info=True)

    @staticmethod
    def _parse_server_map(items) -> dict[str, str]:
        result: dict[str, str] = {}
        if not isinstance(items, list):
            return result
        for item in items:
            text = str(item).strip()
            if "=" not in text:
                continue
            bot_id, server_name = text.split("=", 1)
            bot_id = bot_id.strip()
            server_name = server_name.strip()
            if bot_id and server_name:
                result[bot_id] = server_name[:64]
        return result

    async def _storage_call(self, function, *args, **kwargs):
        if self._terminated:
            raise RuntimeError("插件存储已关闭")
        with self._pending_storage_lock:
            self._pending_storage_jobs += 1
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._storage_executor,
                partial(function, *args, **kwargs),
            )
        finally:
            with self._pending_storage_lock:
                self._pending_storage_jobs -= 1

    def _raw_scope_id(self, event: AstrMessageEvent) -> str:
        scope_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not scope_id:
            try:
                platform = str(event.get_platform_name())
            except (AttributeError, TypeError):
                platform = "unknown"
            try:
                session = event.get_group_id() or event.get_sender_id()
            except (AttributeError, TypeError):
                session = "unknown"
            scope_id = f"{platform}:session:{session}"
        return scope_id

    async def _scope_id(self, event: AstrMessageEvent) -> str:
        scope_id = self._raw_scope_id(event)

        try:
            is_group_scope = bool(event.get_group_id())
        except (AttributeError, TypeError):
            is_group_scope = ":groupmessage:" in scope_id.lower()

        if is_group_scope and scope_id not in self.bound_scopes:
            moved = await self._storage_call(self.storage.bind_legacy_scope, scope_id)
            if moved:
                logger.info(f"已将 {moved} 条旧版记录绑定到会话 {scope_id}。")
            self.bound_scopes.add(scope_id)
        return scope_id

    def _cooldown_remaining(
        self,
        event: AstrMessageEvent,
        command_name: str,
    ) -> int:
        if self.command_cooldown_seconds <= 0 or self.check_admin(event):
            return 0
        key = (
            self._raw_scope_id(event),
            str(event.get_sender_id()),
            command_name,
        )
        now = time.monotonic()
        previous = self.command_last_used.get(key, 0.0)
        remaining = self.command_cooldown_seconds - (now - previous)
        if remaining > 0:
            return max(1, math.ceil(remaining))
        self.command_last_used[key] = now
        if len(self.command_last_used) > 2000:
            cutoff = now - max(self.command_cooldown_seconds * 2, 60)
            self.command_last_used = {
                item_key: used_at
                for item_key, used_at in self.command_last_used.items()
                if used_at >= cutoff
            }
            while len(self.command_last_used) > 2000:
                self.command_last_used.pop(next(iter(self.command_last_used)))
        return 0

    async def _maybe_run_maintenance(self, now_ts: int) -> None:
        now = time.monotonic()
        if (
            self._last_maintenance_monotonic
            and now - self._last_maintenance_monotonic
            < MAINTENANCE_INTERVAL_SECONDS
        ):
            return
        self._last_maintenance_monotonic = now
        try:
            deleted = await self._storage_call(
                self.storage.cleanup_old_records,
                self.retention_days,
                self.audit_retention_days,
                now_ts,
            )
            if deleted["records"] or deleted["audits"]:
                logger.info(
                    "TK Tracker 定期清理完成："
                    f"记录 {deleted['records']} 条，审计 {deleted['audits']} 条。"
                )
        except Exception as exc:
            logger.error(f"TK Tracker 定期清理失败: {exc}", exc_info=True)

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        size = float(max(0, size_bytes))
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{int(size_bytes)} B"

    def _server_name(self, bot_id: str) -> str:
        return self.bot_server_names.get(bot_id, bot_id)

    @staticmethod
    def _server_filter(server_name: str):
        value = str(server_name or "").strip()
        return value or None

    @staticmethod
    def _now() -> tuple[str, int]:
        now = datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S"), int(now.timestamp())

    def check_admin(self, event: AstrMessageEvent) -> bool:
        sender_id = str(event.get_sender_id())
        if sender_id in self.super_admins:
            return True
        try:
            if event.is_admin():
                return True
        except (AttributeError, TypeError):
            pass
        try:
            return event.message_obj.sender.role in ("admin", "owner")
        except AttributeError:
            return False

    @filter.regex(r"踢出玩家\s+\S+\s+成功")
    async def on_kick_success(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id())
        if sender_id not in self.allowed_bot_ids:
            return

        match = KICK_PATTERN.search(event.message_str)
        if not match:
            return
        player_id = match.group("player_id").strip()
        reason = match.group("reason").strip()
        if not player_id or len(player_id) > MAX_PLAYER_ID_LENGTH:
            logger.warning("忽略格式异常的玩家 ID。")
            return
        if len(reason) > MAX_REASON_LENGTH:
            reason = reason[:MAX_REASON_LENGTH].rstrip() + "…"

        scope_id = await self._scope_id(event)
        server_name = self._server_name(sender_id)
        now_text, now_ts = self._now()
        await self._maybe_run_maintenance(now_ts)
        message_id = str(getattr(event.message_obj, "message_id", "") or "").strip()
        message_key = None
        if message_id:
            raw_key = f"{scope_id}\0{sender_id}\0{message_id}".encode("utf-8")
            message_key = hashlib.blake2b(raw_key, digest_size=16).hexdigest()

        try:
            added, record_id, current_kicks = await self._storage_call(
                self.storage.add_record,
                scope_id=scope_id,
                server_name=server_name,
                player_id=player_id,
                occurred_at=now_text,
                occurred_ts=now_ts,
                reason=reason,
                source_bot_id=sender_id,
                message_key=message_key,
                duplicate_window_seconds=self.duplicate_window_seconds,
            )
        except Exception as exc:
            logger.error(f"保存违规记录失败: {exc}", exc_info=True)
            yield event.plain_result("违规记录保存失败，请管理员检查日志。")
            return

        if not added:
            logger.info(f"忽略重复播报，已有记录 #{record_id}。")
            return
        logger.info(
            f"记录 #{record_id}: {player_id} 因 '{reason}' 被踢出，"
            f"服务器 {server_name}，累计 {current_kicks} 次。"
        )
        if not self.auto_reply:
            return

        reply_message = (
            f"违规处理记录 #{record_id}\n"
            f"玩家：{player_id}\n"
            f"服务器：{server_name}\n"
            f"时间：{now_text}\n"
            f"原因：{reason}\n"
            f"该服务器累计被踢出：{current_kicks} 次"
        )
        if self.risk_alert_enabled:
            try:
                risk = await self._storage_call(
                    self.storage.get_player_risk,
                    scope_id,
                    player_id,
                    self.risk_window_days,
                    now_ts,
                    server_name,
                )
                alert_key = (scope_id, server_name, player_id.casefold())
                last_alerted = self.risk_last_alerted.get(alert_key, 0.0)
                alert_ready = (
                    time.monotonic() - last_alerted
                    >= self.risk_alert_cooldown_seconds
                )
                if risk["records"] >= self.risk_threshold and alert_ready:
                    if self.risk_alert_cooldown_seconds > 0:
                        self.risk_last_alerted[alert_key] = time.monotonic()
                        while len(self.risk_last_alerted) > 5000:
                            self.risk_last_alerted.pop(
                                next(iter(self.risk_last_alerted))
                            )
                    reply_message += (
                        "\n\n风险提醒\n"
                        f"该玩家最近 {self.risk_window_days} 天已有 "
                        f"{risk['records']} 条记录，已达到提醒阈值 "
                        f"{self.risk_threshold} 条。"
                    )
            except Exception as exc:
                logger.error(f"生成风险提醒失败: {exc}", exc_info=True)
        yield event.plain_result(reply_message)

    @filter.command("tk帮助")
    async def tk_help(self, event: AstrMessageEvent):
        """查看 TK Tracker 使用说明"""
        remaining = self._cooldown_remaining(event, "tk帮助")
        if remaining:
            yield event.plain_result(f"操作过于频繁，请 {remaining} 秒后再试。")
            return
        help_text = (
            f"TK Tracker v{PLUGIN_VERSION} 帮助\n\n"
            "查询\n"
            "/tk查 <玩家ID> [服务器]  查看最近记录\n"
            "/tk排行 [服务器]         查看违规排行\n"
            "/tk统计 [天数] [服务器]  查看阶段统计\n"
            "/tk风险 <玩家ID> [天数] [服务器]  查看风险概况\n\n"
            "管理\n"
            "/tk删除 <记录编号>       删除单条记录\n"
            "/tk清空 <玩家ID> [服务器] 删除玩家记录\n"
            "/tk清空全部              申请清空当前会话\n"
            "/tk确认清空              30 秒内确认清空\n"
            "/tk审计 [条数]           查看管理操作\n"
            "/tk状态                  查看存储运行状态\n"
            "/tk备份                  立即备份数据库\n"
            "/tk导出 [天数] [服务器]  导出当前会话记录\n\n"
            "服务器参数可省略；记录只在当前群聊或会话中查询。"
        )
        yield event.plain_result(help_text)

    @filter.command("tk排行")
    async def tk_leaderboard(self, event: AstrMessageEvent, server_name: str = ""):
        """查看当前会话违规排行"""
        remaining = self._cooldown_remaining(event, "tk排行")
        if remaining:
            yield event.plain_result(f"操作过于频繁，请 {remaining} 秒后再试。")
            return
        scope_id = await self._scope_id(event)
        server_filter = self._server_filter(server_name)
        rows = await self._storage_call(
            self.storage.get_leaderboard,
            scope_id,
            self.leaderboard_limit,
            server_filter,
        )
        if not rows:
            yield event.plain_result("当前范围没有违规记录。")
            return
        title = "TK 违规排行"
        if server_filter:
            title += f" - {server_filter}"
        lines = [title]
        lines.extend(
            f"{index}. {player_id}：{count} 次"
            for index, (player_id, count) in enumerate(rows, 1)
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("tk查")
    async def query_tk(
        self, event: AstrMessageEvent, player_id: str = "", server_name: str = ""
    ):
        """查询玩家累计次数和最近记录"""
        player_id = player_id.strip()
        if not player_id:
            yield event.plain_result("缺少玩家 ID。用法：/tk查 <玩家ID> [服务器]")
            return
        remaining = self._cooldown_remaining(event, "tk查")
        if remaining:
            yield event.plain_result(f"操作过于频繁，请 {remaining} 秒后再试。")
            return
        scope_id = await self._scope_id(event)
        server_filter = self._server_filter(server_name)
        kicks, recent_records = await self._storage_call(
            self.storage.get_player_records,
            scope_id,
            player_id,
            self.query_limit,
            server_filter,
        )
        if kicks == 0:
            yield event.plain_result(f"玩家 {player_id} 在当前范围没有违规记录。")
            return

        lines = [f"玩家 {player_id} 累计被踢出 {kicks} 次。", "", "最近记录："]
        for record in recent_records:
            server = (
                f" [{record['server']}]"
                if not server_filter and record["server"]
                else ""
            )
            lines.append(
                f"#{record['id']}{server} [{record['time']}] {record['reason']}"
            )
        if kicks > self.query_limit:
            lines.append(f"另有更早的 {kicks - self.query_limit} 条记录未显示。")
        yield event.plain_result("\n".join(lines))

    @filter.command("tk统计")
    async def tk_stats(
        self, event: AstrMessageEvent, days: int = 0, server_name: str = ""
    ):
        """查看当前会话指定天数的违规统计"""
        safe_days = _clamp_int(days or self.default_stats_days, self.default_stats_days, 1, 365)
        remaining = self._cooldown_remaining(event, "tk统计")
        if remaining:
            yield event.plain_result(f"操作过于频繁，请 {remaining} 秒后再试。")
            return
        scope_id = await self._scope_id(event)
        server_filter = self._server_filter(server_name)
        stats = await self._storage_call(
            self.storage.get_stats,
            scope_id,
            safe_days,
            int(time.time()),
            server_filter,
        )
        lines = [f"最近 {safe_days} 天统计"]
        if server_filter:
            lines.append(f"服务器：{server_filter}")
        lines.extend(
            [
                "",
                f"违规记录：{stats['records']} 条",
                f"涉及玩家：{stats['players']} 人",
                f"重复违规玩家：{stats['repeated_players']} 人",
            ]
        )
        if stats["reasons"]:
            lines.append("")
            lines.append("常见原因：")
            lines.extend(
                f"{index}. {reason}：{count} 次"
                for index, (reason, count) in enumerate(stats["reasons"], 1)
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("tk风险")
    async def tk_risk(
        self,
        event: AstrMessageEvent,
        player_id: str = "",
        days: int = 0,
        server_name: str = "",
    ):
        """查看玩家近期违规风险概况"""
        player_id = player_id.strip()
        if not player_id:
            yield event.plain_result(
                "缺少玩家 ID。用法：/tk风险 <玩家ID> [天数] [服务器]"
            )
            return
        remaining = self._cooldown_remaining(event, "tk风险")
        if remaining:
            yield event.plain_result(f"操作过于频繁，请 {remaining} 秒后再试。")
            return
        safe_days = _clamp_int(
            days or self.risk_window_days,
            self.risk_window_days,
            1,
            365,
        )
        scope_id = await self._scope_id(event)
        server_filter = self._server_filter(server_name)
        risk = await self._storage_call(
            self.storage.get_player_risk,
            scope_id,
            player_id,
            safe_days,
            int(time.time()),
            server_filter,
        )
        if risk["records"] == 0:
            yield event.plain_result(
                f"玩家 {player_id} 最近 {safe_days} 天没有违规记录。"
            )
            return
        if risk["records"] >= self.risk_threshold:
            level = "较高"
        elif risk["records"] >= max(2, self.risk_threshold // 2):
            level = "一般"
        else:
            level = "较低"
        lines = [
            f"玩家 {player_id} 风险概况",
            f"统计范围：最近 {safe_days} 天",
            f"风险等级：{level}",
            f"违规记录：{risk['records']} 条",
            f"涉及服务器：{risk['servers']} 个",
            f"首次记录：{risk['first_at']}",
            f"最近记录：{risk['last_at']}",
        ]
        if risk["reasons"]:
            lines.append("常见原因：")
            lines.extend(
                f"{index}. {reason}：{count} 次"
                for index, (reason, count) in enumerate(risk["reasons"], 1)
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("tk删除")
    async def delete_record(self, event: AstrMessageEvent, record_id: int = 0):
        """管理员删除当前会话中的单条记录"""
        if record_id <= 0:
            yield event.plain_result("缺少记录编号。用法：/tk删除 <记录编号>")
            return
        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以删除记录。")
            return
        scope_id = await self._scope_id(event)
        now_text, _ = self._now()
        deleted = await self._storage_call(
            self.storage.delete_record,
            scope_id,
            record_id,
            str(event.get_sender_id()),
            now_text,
        )
        if deleted is None:
            yield event.plain_result(f"当前会话中没有记录 #{record_id}。")
            return
        yield event.plain_result(
            f"已删除记录 #{record_id}：{deleted['player_id']}，{deleted['reason']}。"
        )

    @filter.command("tk清空")
    async def clear_tk(
        self, event: AstrMessageEvent, player_id: str = "", server_name: str = ""
    ):
        """管理员清空指定玩家记录"""
        player_id = player_id.strip()
        if not player_id:
            yield event.plain_result("缺少玩家 ID。用法：/tk清空 <玩家ID> [服务器]")
            return
        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以清空记录。")
            return
        scope_id = await self._scope_id(event)
        server_filter = self._server_filter(server_name)
        now_text, _ = self._now()
        deleted = await self._storage_call(
            self.storage.delete_player,
            scope_id,
            player_id,
            str(event.get_sender_id()),
            now_text,
            server_filter,
        )
        if deleted:
            yield event.plain_result(f"已清空玩家 {player_id} 的 {deleted} 条记录。")
        else:
            yield event.plain_result(f"玩家 {player_id} 没有可清空的记录。")

    @filter.command("tk清空全部")
    async def clear_all_tk(self, event: AstrMessageEvent):
        """申请清空当前会话的全部记录"""
        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以清空全部记录。")
            return
        scope_id = await self._scope_id(event)
        count = await self._storage_call(self.storage.count_scope, scope_id)
        if count == 0:
            yield event.plain_result("当前会话没有可清空的记录。")
            return
        key = (scope_id, str(event.get_sender_id()))
        now = time.monotonic()
        self.pending_clears = {
            pending_key: expires_at
            for pending_key, expires_at in self.pending_clears.items()
            if expires_at >= now
        }
        self.pending_clears[key] = now + CLEAR_CONFIRM_SECONDS
        yield event.plain_result(
            f"当前会话共有 {count} 条记录。请在 {CLEAR_CONFIRM_SECONDS} 秒内发送 "
            "/tk确认清空；超时自动取消。"
        )

    @filter.command("tk确认清空")
    async def confirm_clear_all(self, event: AstrMessageEvent):
        """确认清空当前会话的全部记录"""
        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以确认清空。")
            return
        scope_id = await self._scope_id(event)
        key = (scope_id, str(event.get_sender_id()))
        expires_at = self.pending_clears.pop(key, None)
        if expires_at is None or time.monotonic() > expires_at:
            yield event.plain_result("没有待确认的清空操作，或确认已超时。")
            return
        backup_path = None
        if self.backup_before_clear:
            try:
                backup_path = await self._storage_call(
                    self.storage.create_backup,
                    self.data_dir / "backups",
                    self.backup_keep_count,
                )
            except Exception as exc:
                logger.error(f"清空前备份失败: {exc}", exc_info=True)
                yield event.plain_result("清空已取消：数据库备份失败，请检查日志。")
                return
        now_text, _ = self._now()
        deleted = await self._storage_call(
            self.storage.clear_scope,
            scope_id,
            str(event.get_sender_id()),
            now_text,
        )
        message = f"已清空当前会话的全部记录，共删除 {deleted} 条。"
        if backup_path is not None:
            message += f"\n清空前备份：{backup_path.name}"
        yield event.plain_result(message)

    @filter.command("tk审计")
    async def tk_audit(self, event: AstrMessageEvent, limit: int = 10):
        """管理员查看当前会话的管理操作"""
        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以查看审计记录。")
            return
        safe_limit = _clamp_int(limit, 10, 1, 50)
        scope_id = await self._scope_id(event)
        rows = await self._storage_call(
            self.storage.get_audit_log,
            scope_id,
            safe_limit,
        )
        if not rows:
            yield event.plain_result("当前会话没有管理操作记录。")
            return
        action_names = {
            "delete_record": "删除记录",
            "delete_player": "清空玩家",
            "clear_scope": "清空会话",
            "backup": "备份数据库",
            "export": "导出记录",
        }
        lines = [f"最近 {len(rows)} 条管理操作"]
        for row in rows:
            action = action_names.get(row["action"], row["action"])
            lines.append(
                f"#{row['id']} [{row['occurred_at']}] "
                f"{row['operator_id']} {action}（{row['detail']}）"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("tk状态")
    async def tk_status(self, event: AstrMessageEvent):
        """管理员查看数据库和后台任务状态"""
        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以查看运行状态。")
            return
        status = await self._storage_call(self.storage.get_status)
        with self._pending_storage_lock:
            pending_jobs = self._pending_storage_jobs
        retention = (
            f"{self.retention_days} 天"
            if self.retention_days > 0
            else "永久保留"
        )
        lines = [
            f"TK Tracker v{PLUGIN_VERSION} 状态",
            f"数据库结构：v{status['schema_version']}",
            f"完整性检查：{status['quick_check']}",
            f"记录数：{status['records']}",
            f"玩家数：{status['players']}",
            f"会话数：{status['scopes']}",
            f"审计记录：{status['audits']}",
            f"数据库大小：{self._format_size(status['size_bytes'])}",
            f"等待中的存储任务：{max(0, pending_jobs - 1)}",
            f"违规记录保留：{retention}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("tk备份")
    async def tk_backup(self, event: AstrMessageEvent):
        """管理员立即创建数据库备份"""
        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以备份数据库。")
            return
        scope_id = await self._scope_id(event)
        now_text, _ = self._now()
        try:
            backup_path = await self._storage_call(
                self.storage.create_backup,
                self.data_dir / "backups",
                self.backup_keep_count,
            )
            await self._storage_call(
                self.storage.write_audit,
                scope_id,
                str(event.get_sender_id()),
                "backup",
                f"file={backup_path.name}",
                now_text,
            )
        except Exception as exc:
            logger.error(f"数据库备份失败: {exc}", exc_info=True)
            yield event.plain_result("数据库备份失败，请检查日志。")
            return
        yield event.plain_result(
            f"数据库备份完成。\n文件：{backup_path.name}\n"
            f"目录：{backup_path.parent}"
        )

    @filter.command("tk导出")
    async def tk_export(
        self,
        event: AstrMessageEvent,
        days: int = 0,
        server_name: str = "",
    ):
        """管理员将当前会话记录导出为 CSV"""
        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以导出记录。")
            return
        safe_days = _clamp_int(days, 0, 0, 3650)
        scope_id = await self._scope_id(event)
        server_filter = self._server_filter(server_name)
        now_text, now_ts = self._now()
        scope_hash = hashlib.blake2b(
            scope_id.encode("utf-8"),
            digest_size=5,
        ).hexdigest()
        destination = (
            self.data_dir
            / "exports"
            / f"tk-records-{datetime.now():%Y%m%d-%H%M%S}-{scope_hash}.csv"
        )
        since_ts = now_ts - safe_days * 86400 if safe_days > 0 else 0
        try:
            count, truncated = await self._storage_call(
                self.storage.export_csv,
                scope_id,
                destination,
                self.export_limit,
                since_ts,
                server_filter,
                self.export_keep_count,
            )
            await self._storage_call(
                self.storage.write_audit,
                scope_id,
                str(event.get_sender_id()),
                "export",
                f"count={count};days={safe_days};file={destination.name}",
                now_text,
            )
        except Exception as exc:
            logger.error(f"记录导出失败: {exc}", exc_info=True)
            yield event.plain_result("记录导出失败，请检查日志。")
            return
        range_text = f"最近 {safe_days} 天" if safe_days else "全部时间"
        message = (
            f"记录导出完成，共 {count} 条。\n"
            f"范围：{range_text}\n文件：{destination.name}\n"
            f"目录：{destination.parent}"
        )
        if truncated:
            message += f"\n数据较多，仅导出最新 {self.export_limit} 条。"
        yield event.plain_result(message)

    async def terminate(self):
        """插件停用或卸载时释放数据库连接。"""
        if self._terminated:
            return
        self.pending_clears.clear()
        self.command_last_used.clear()
        self.risk_last_alerted.clear()
        try:
            await self._storage_call(self.storage.close)
        finally:
            self._terminated = True
            await asyncio.to_thread(self._storage_executor.shutdown, True)
