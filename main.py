import hashlib
import re
import time
from datetime import datetime
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
KICK_PATTERN = re.compile(
    r"踢出玩家\s+(?P<player_id>\S+)\s+成功.*?原因[:：]\s*(?P<reason>.+)",
    re.DOTALL,
)
MAX_PLAYER_ID_LENGTH = 64
MAX_REASON_LENGTH = 500
CLEAR_CONFIRM_SECONDS = 30


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
        self.bot_server_names = self._parse_server_map(
            self.plugin_config.get("bot_server_map", [])
        )
        self.pending_clears: dict[tuple[str, str], float] = {}
        self.bound_scopes: set[str] = set()

        self.plugin_dir = Path(__file__).resolve().parent
        if get_astrbot_plugin_data_path is not None:
            self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        else:
            self.data_dir = self.plugin_dir / "data" / "tk_tracker"

        self.storage = RecordStorage(self.data_dir / "records.db")
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

    def _scope_id(self, event: AstrMessageEvent) -> str:
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

        try:
            is_group_scope = bool(event.get_group_id())
        except (AttributeError, TypeError):
            is_group_scope = ":groupmessage:" in scope_id.lower()

        if is_group_scope and scope_id not in self.bound_scopes:
            moved = self.storage.bind_legacy_scope(scope_id)
            if moved:
                logger.info(f"已将 {moved} 条旧版记录绑定到会话 {scope_id}。")
            self.bound_scopes.add(scope_id)
        return scope_id

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

        scope_id = self._scope_id(event)
        server_name = self._server_name(sender_id)
        now_text, now_ts = self._now()
        message_id = str(getattr(event.message_obj, "message_id", "") or "").strip()
        message_key = None
        if message_id:
            raw_key = f"{scope_id}\0{sender_id}\0{message_id}".encode("utf-8")
            message_key = hashlib.blake2b(raw_key, digest_size=16).hexdigest()

        try:
            added, record_id, current_kicks = self.storage.add_record(
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
        yield event.plain_result(reply_message)

    @filter.command("tk帮助")
    async def tk_help(self, event: AstrMessageEvent):
        """查看 TK Tracker 使用说明"""
        help_text = (
            "TK Tracker 帮助\n\n"
            "查询\n"
            "/tk查 <玩家ID> [服务器]  查看最近记录\n"
            "/tk排行 [服务器]         查看违规排行\n"
            "/tk统计 [天数] [服务器]  查看阶段统计\n\n"
            "管理\n"
            "/tk删除 <记录编号>       删除单条记录\n"
            "/tk清空 <玩家ID> [服务器] 删除玩家记录\n"
            "/tk清空全部              申请清空当前会话\n"
            "/tk确认清空              30 秒内确认清空\n\n"
            "服务器参数可省略；记录只在当前群聊或会话中查询。"
        )
        yield event.plain_result(help_text)

    @filter.command("tk排行")
    async def tk_leaderboard(self, event: AstrMessageEvent, server_name: str = ""):
        """查看当前会话违规排行"""
        scope_id = self._scope_id(event)
        server_filter = self._server_filter(server_name)
        rows = self.storage.get_leaderboard(
            scope_id, self.leaderboard_limit, server_filter
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
        scope_id = self._scope_id(event)
        server_filter = self._server_filter(server_name)
        kicks, recent_records = self.storage.get_player_records(
            scope_id, player_id, self.query_limit, server_filter
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
        scope_id = self._scope_id(event)
        server_filter = self._server_filter(server_name)
        stats = self.storage.get_stats(
            scope_id, safe_days, int(time.time()), server_filter
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

    @filter.command("tk删除")
    async def delete_record(self, event: AstrMessageEvent, record_id: int = 0):
        """管理员删除当前会话中的单条记录"""
        if record_id <= 0:
            yield event.plain_result("缺少记录编号。用法：/tk删除 <记录编号>")
            return
        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以删除记录。")
            return
        scope_id = self._scope_id(event)
        now_text, _ = self._now()
        deleted = self.storage.delete_record(
            scope_id, record_id, str(event.get_sender_id()), now_text
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
        scope_id = self._scope_id(event)
        server_filter = self._server_filter(server_name)
        now_text, _ = self._now()
        deleted = self.storage.delete_player(
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
        scope_id = self._scope_id(event)
        count = self.storage.count_scope(scope_id)
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
        scope_id = self._scope_id(event)
        key = (scope_id, str(event.get_sender_id()))
        expires_at = self.pending_clears.pop(key, None)
        if expires_at is None or time.monotonic() > expires_at:
            yield event.plain_result("没有待确认的清空操作，或确认已超时。")
            return
        now_text, _ = self._now()
        deleted = self.storage.clear_scope(
            scope_id, str(event.get_sender_id()), now_text
        )
        yield event.plain_result(f"已清空当前会话的全部记录，共删除 {deleted} 条。")

    async def terminate(self):
        """插件停用或卸载时释放数据库连接。"""
        self.pending_clears.clear()
        self.storage.close()
