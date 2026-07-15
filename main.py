import re
from datetime import datetime
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

from .storage import RecordStorage

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except ImportError:  # 兼容尚未提供统一插件数据目录的旧版 AstrBot
    get_astrbot_plugin_data_path = None


PLUGIN_NAME = "astrbot_plugin_tk_tracker"
KICK_PATTERN = re.compile(
    r"踢出玩家\s+(?P<player_id>\S+)\s+成功.*?原因[:：]\s*(?P<reason>.+)",
    re.DOTALL,
)
MAX_PLAYER_ID_LENGTH = 64
MAX_REASON_LENGTH = 500

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

    def check_admin(self, event: AstrMessageEvent) -> bool:
        sender_id = str(event.get_sender_id())

        if sender_id in self.super_admins:
            return True
        try:
            role = event.message_obj.sender.role
            if role in ['admin', 'owner']:
                return True
        except AttributeError:
            pass
        return False

    @filter.regex(r"踢出玩家\s+\S+\s+成功")
    async def on_kick_success(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id())

        if sender_id not in self.allowed_bot_ids:
            return

        text = event.message_str
        match = KICK_PATTERN.search(text)
        if not match:
            return

        player_id = match.group("player_id").strip()
        reason = match.group("reason").strip()

        if not player_id or len(player_id) > MAX_PLAYER_ID_LENGTH:
            logger.warning("忽略格式异常的玩家 ID。")
            return

        if len(reason) > MAX_REASON_LENGTH:
            reason = reason[:MAX_REASON_LENGTH].rstrip() + "…"

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        current_kicks = self.storage.add_record(player_id, current_time, reason)
        logger.info(
            f"成功记录违规: 玩家 {player_id} 因 '{reason}' 被踢出，累计 {current_kicks} 次。"
        )

        reply_message = (
            "违规处理记录\n"
            f"玩家：{player_id}\n"
            f"时间：{current_time}\n"
            f"原因：{reason}\n"
            f"累计被踢出：{current_kicks} 次"
        )
        yield event.plain_result(reply_message)

    @filter.command("tk帮助")
    async def tk_help(self, event: AstrMessageEvent):
        """查看 TK Tracker 使用说明"""
        help_text = (
            "TK Tracker 帮助\n\n"
            "查询指令\n"
            "/tk查 <玩家ID>  查看累计次数和最近 5 条记录\n"
            "/tk排行         查看被踢次数前 10 名\n\n"
            "管理指令\n"
            "/tk清空 <玩家ID>  删除该玩家的全部记录\n"
            "/tk清空全部       删除全部记录\n\n"
            "说明：插件会自动记录白名单管服机器人发送的踢人播报。"
        )
        yield event.plain_result(help_text)

    @filter.command("tk排行")
    async def tk_leaderboard(self, event: AstrMessageEvent):
        """查看违规被踢次数排行榜（前十名）"""
        top_10 = self.storage.get_leaderboard(10)
        if not top_10:
            yield event.plain_result("目前没有违规记录。")
            return

        lines = ["TK 违规排行 Top 10"]
        lines.extend(
            f"{index}. {player_id}：{count} 次"
            for index, (player_id, count) in enumerate(top_10, 1)
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("tk查")
    async def query_tk(self, event: AstrMessageEvent, player_id: str = ""):
        """查询玩家被踢出的次数及最近记录"""
        player_id = player_id.strip()
        if not player_id:
            yield event.plain_result("缺少玩家 ID。用法：/tk查 <玩家ID>")
            return

        kicks, recent_records = self.storage.get_player_records(player_id, 5)
        if kicks == 0:
            yield event.plain_result(f"玩家 {player_id} 目前没有违规记录。")
        else:
            lines = [f"玩家 {player_id} 累计被踢出 {kicks} 次。", "", "最近记录："]
            for i, record in enumerate(recent_records, 1):
                lines.append(f"{i}. [{record['time']}] {record['reason']}")
            if kicks > 5:
                lines.append(f"另有更早的 {kicks - 5} 条记录未显示。")
            yield event.plain_result("\n".join(lines))

    @filter.command("tk清空")
    async def clear_tk(self, event: AstrMessageEvent, player_id: str = ""):
        """清空指定玩家的踢出记录"""
        player_id = player_id.strip()
        if not player_id:
            yield event.plain_result("缺少玩家 ID。用法：/tk清空 <玩家ID>")
            return

        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以清空记录。")
            return

        deleted = self.storage.delete_player(player_id)
        if deleted:
            logger.info(f"清空记录: 管理员清空了玩家 {player_id} 的违规记录。")
            yield event.plain_result(f"已清空玩家 {player_id} 的 {deleted} 条记录。")
        else:
            yield event.plain_result(f"玩家 {player_id} 没有可清空的记录。")

    @filter.command("tk清空全部")
    async def clear_all_tk(self, event: AstrMessageEvent):
        """清空所有玩家的踢出记录"""
        if not self.check_admin(event):
            yield event.plain_result("权限不足：只有管理员可以清空全部记录。")
            return

        deleted = self.storage.clear_all()
        logger.info("清空记录: 管理员清空了所有玩家的违规记录。")
        yield event.plain_result(f"已清空全部记录，共删除 {deleted} 条。")

    async def terminate(self):
        """插件停用或卸载时释放数据库连接。"""
        self.storage.close()
