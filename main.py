import os
import json
from datetime import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

class TKTrackerPlugin(Star):
    # 核心变动：这里增加了 config: dict = None，这是 AstrBot 官方注入动态配置的标准做法
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        
        self.data_dir = os.path.join(os.getcwd(), "data", "tk_tracker")
        self.data_file = os.path.join(self.data_dir, "records.json")
        self.records = self.load_data()
        
        # 接收 WebUI 传过来的配置，如果没有则使用空字典兜底
        self.plugin_config = config or {}

    def load_data(self) -> dict:
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for player_id, records in data.items():
                        if isinstance(records, int):
                            data[player_id] = [{"time": "未知时间", "reason": "未知原因"} for _ in range(records)]
                    return data
            except Exception as e:
                logger.error(f"读取违规记录失败: {e}")
                return {}
        return {}

    def save_data(self):
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir, exist_ok=True)
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.records, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存违规记录失败: {e}")

    # ===== 权限检查辅助函数 =====
    def check_admin(self, event: AstrMessageEvent) -> bool:
        # 🚀 从官方配置面板中实时读取管理员名单
        admins = self.plugin_config.get("super_admins", [])
        if event.get_sender_id() in admins:
            return True
        try:
            role = event.message_obj.sender.role
            if role in ['admin', 'owner']:
                return True
        except Exception:
            pass
        return False

    # ===== 核心监听功能 =====
    @filter.regex(r"[\s\S]*踢出玩家[\s\S]*")
    async def on_kick_success(self, event: AstrMessageEvent):
        # 🚀 从官方配置面板中实时读取管服机器人名单
        allowed_bots = self.plugin_config.get("allowed_bot_ids", [])
        sender_id = event.get_sender_id()
        
        if sender_id not in allowed_bots:
            return

        text = event.message_str
        
        if "踢出玩家" in text and "成功" in text and "原因" in text:
            try:
                part1 = text.split("踢出玩家")[1]
                player_id = part1.split("成功")[0].strip() 
                
                after_reason = text.split("原因")[1]
                reason = after_reason.strip("：: \n\r\t")
                
                if not player_id:
                    return

                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                if player_id not in self.records:
                    self.records[player_id] = []
                
                self.records[player_id].append({
                    "time": current_time,
                    "reason": reason
                })
                
                current_kicks = len(self.records[player_id])
                self.save_data()
                
                logger.info(f"✅ 成功记录违规: 玩家 {player_id} 因 '{reason}' 被踢出。累计: {current_kicks}")
                
                reply_message = (
                    f"⚠️ 【违规处理记录】\n"
                    f"👤 玩家：{player_id}\n"
                    f"🕒 时间：{current_time}\n"
                    f"📝 原因：{reason}\n"
                    f"📊 该玩家累计被踢出次数：{current_kicks} 次"
                )
                
                yield event.plain_result(reply_message)
                
            except Exception as e:
                logger.error(f"❌ 提取违规信息时出错: {e}")
        
    @filter.command("tk帮助")
    async def tk_help(self, event: AstrMessageEvent):
        '''查看玩家违规记录插件的说明手册'''
        help_text = (
            "🛠️ 【TK Tracker 违规记录插件】\n"
            "✨ 本插件会自动监听管服机器人的播报，无需手动记录。\n"
            "------------------------\n"
            "📚 【查询指令】 (所有人可用)\n"
            "🔹 /tk查 [玩家ID]\n"
            "  └ 查询某个玩家的被踢次数和近期违规原因。\n\n"
            "👑 【管理指令】 (仅管理员可用)\n"
            "🔸 /tk清空 [玩家ID]\n"
            "  └ 清除某个特定玩家的违规记录。\n"
            "🔸 /tk清空全部\n"
            "  └ 危险操作！清空数据库内所有人的违规记录。"
        )
        yield event.plain_result(help_text)

    @filter.command("tk查")
    async def query_tk(self, event: AstrMessageEvent, player_id: str):
        '''查询玩家被踢出的次数及历史记录 (用法: /tk查 [玩家id])'''
        player_records = self.records.get(player_id, [])
        kicks = len(player_records)
        
        if kicks == 0:
            yield event.plain_result(f"✅ 玩家 {player_id} 目前没有违规被踢出的记录。")
        else:
            reply = f"📊 玩家 {player_id} 累计被踢出过 {kicks} 次。\n\n📜 违规历史："
            recent_records = player_records[-5:] 
            for i, record in enumerate(recent_records, 1):
                reply += f"\n{i}. [{record['time']}] {record['reason']}"
            if kicks > 5:
                reply += f"\n... (省略更早的 {kicks - 5} 条记录)"
            yield event.plain_result(reply)

    @filter.command("tk清空")
    async def clear_tk(self, event: AstrMessageEvent, player_id: str):
        '''清空指定玩家的踢出记录 (用法: /tk清空 [玩家id])'''
        if not self.check_admin(event):
            yield event.plain_result("❌ 权限不足：只有管理员才能执行清空操作！")
            return
            
        if player_id in self.records:
            del self.records[player_id]
            self.save_data()
            logger.info(f"清空记录: 管理员清空了玩家 {player_id} 的违规记录。")
            yield event.plain_result(f"✅ 管理员操作成功：已清空玩家 {player_id} 的违规记录！")
        else:
            yield event.plain_result(f"⚠️ 玩家 {player_id} 目前没有违规记录，无需清空。")

    @filter.command("tk清空全部")
    async def clear_all_tk(self, event: AstrMessageEvent):
        '''清空所有玩家的踢出记录 (用法: /tk清空全部)'''
        if not self.check_admin(event):
            yield event.plain_result("❌ 权限不足：只有管理员才能执行清空所有记录的操作！")
            return
            
        self.records = {}
        self.save_data()
        logger.info("清空记录: 管理员清空了所有玩家的违规记录。")
        yield event.plain_result("🚨 数据库已重置：管理员成功清空了所有玩家的违规记录！")