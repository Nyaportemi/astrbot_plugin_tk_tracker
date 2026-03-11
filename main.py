import os
import json
import re
from datetime import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

class TKTrackerPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        
        self.plugin_dir = os.path.dirname(__file__)
        self.data_dir = os.path.join(self.plugin_dir, "data", "tk_tracker")
        self.data_file = os.path.join(self.data_dir, "records.json")
        self.records = self.load_data()
        
        self.plugin_config = config or {}

    def load_data(self) -> dict:
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    
                    # 修复6：加强数据结构校验，防止历史坏数据或被手动乱改导致 KeyError
                    validated_data = {}
                    for player_id, records in data.items():
                        if isinstance(records, int): # 兼容远古版本
                            validated_data[player_id] = [{"time": "未知时间", "reason": "未知原因"} for _ in range(records)]
                        elif isinstance(records, list): # 正常情况检查内部字段
                            valid_records = []
                            for r in records:
                                if isinstance(r, dict) and "time" in r and "reason" in r:
                                    valid_records.append(r)
                            validated_data[player_id] = valid_records
                    return validated_data
            except json.JSONDecodeError as e:
                logger.error(f"读取违规记录失败(JSON格式错误): {e}")
            except Exception as e:
                logger.error(f"读取违规记录失败(未知错误): {e}", exc_info=True)
        return {}

    def save_data(self):
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir, exist_ok=True)
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.records, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存违规记录失败: {e}", exc_info=True)

    def check_admin(self, event: AstrMessageEvent) -> bool:
        admins = self.plugin_config.get("super_admins", [])
        # 修复7：强制将双方 ID 转为字符串，解决潜在的类型不一致逻辑边界问题
        str_admins = [str(a) for a in admins]
        sender_id = str(event.get_sender_id())
        
        if sender_id in str_admins:
            return True
        try:
            role = event.message_obj.sender.role
            if role in ['admin', 'owner']:
                return True
        except AttributeError: # 修复5：收敛异常类型，不再使用宽泛的 Exception
            pass
        return False

    # 修复2：收窄正则监听范围，只有同时具备关键字才会进入处理，降低无意义开销
    @filter.regex(r"踢出玩家\s+\S+\s+成功")
    async def on_kick_success(self, event: AstrMessageEvent):
        allowed_bots = self.plugin_config.get("allowed_bot_ids", [])
        str_allowed_bots = [str(b) for b in allowed_bots]
        sender_id = str(event.get_sender_id())
        
        if sender_id not in str_allowed_bots:
            return

        text = event.message_str
        
        # 修复3：使用结构化具名正则匹配替代脆弱的 split 切割，保证数据提取准确性
        match = re.search(r"踢出玩家\s+(?P<player_id>\S+)\s+成功.*?原因[:：]\s*(?P<reason>.+)", text, re.DOTALL)
        
        if not match:
            return
            
        player_id = match.group("player_id").strip()
        reason = match.group("reason").strip()
        
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

    # 修复4：给 player_id 增加默认空字符串，并处理用户未传参的兜底场景
    @filter.command("tk查")
    async def query_tk(self, event: AstrMessageEvent, player_id: str = ""):
        '''查询玩家被踢出的次数及历史记录 (用法: /tk查 [玩家id])'''
        if not player_id:
            yield event.plain_result("⚠️ 缺少参数！正确用法: /tk查 [玩家ID]")
            return
            
        player_records = self.records.get(player_id, [])
        kicks = len(player_records)
        
        if kicks == 0:
            yield event.plain_result(f"✅ 玩家 {player_id} 目前没有违规被踢出的记录。")
        else:
            reply = f"📊 玩家 {player_id} 累计被踢出过 {kicks} 次。\n\n📜 违规历史："
            recent_records = player_records[-5:] 
            for i, record in enumerate(recent_records, 1):
                # 双重保险，防止旧数据结构损坏
                reply += f"\n{i}. [{record.get('time', '未知时间')}] {record.get('reason', '未知原因')}"
            if kicks > 5:
                reply += f"\n... (省略更早的 {kicks - 5} 条记录)"
            yield event.plain_result(reply)

    @filter.command("tk清空")
    async def clear_tk(self, event: AstrMessageEvent, player_id: str = ""):
        '''清空指定玩家的踢出记录 (用法: /tk清空 [玩家id])'''
        if not player_id:
            yield event.plain_result("⚠️ 缺少参数！正确用法: /tk清空 [玩家ID]")
            return
            
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
