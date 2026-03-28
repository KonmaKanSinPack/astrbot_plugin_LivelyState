from collections import deque
import json
import time
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from typing import Any, Dict, List, Optional, Tuple
from astrbot.api.event import MessageChain
from astrbot.api.star import StarTools

import json_repair

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

class CharacterState:
    def __init__(self):
        self.path = StarTools.get_data_dir() / f"global_state.json"

    
    def default_state(self) -> Dict[str, Any]:
        return {
            "LastUpdateTime": time.time(),
            "emotion": "Normal",
            "energy_level": 100,
            "thirst": 0,
            "physical_state": "Idle",
            "update_reason": "Initial state",
            "target_id": "none",
        }

    def get_whole_state(self):
        if not self.path.exists():
            # File doesn't exist, create with default state
            state = self.default_state()
            self.save(state)
            return state
        else:
            # File exists, read it
            try:
                state = json_repair.loads(self.path.read_text(encoding="utf-8"))
                return state
            except Exception as e:
                logger.error(f"Failed to parse state file, using default state. Error: {e}")
                state = self.default_state()
                self.save(state)
                return state

    def save(self,state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def delete(self):
        if self.path.exists():
            self.path.unlink()

    def update(self, current_time, enable_update=True):
        # 更新角色状态的逻辑
        # self.LastUpdateTime = current_time
        # 这里可以添加更多的状态更新逻辑
        if enable_update:
            pass

# class StateManager: 本来想写不同ID独立状态的，想了想还是算了
#     def __init__(self):
#         self.character_states =    


class GlobalObserver:
    def __init__(self, max_size=50):
        self.recent_messages = deque(maxlen=max_size)
        
        # 我们可以顺便设一个触发阈值，比如每攒够 20 条新消息就触发一次总结
        self.trigger_threshold = 20 
        self.new_message_count = 0  
        
        # 用来存储大模型总结出来的“当前状态”
        self.current_state = ""

    async def add_message(self, message_text, event=None):
        """每次系统收到任何人的消息，都调用这个方法塞进来"""
        self.recent_messages.append(message_text)
        self.new_message_count += 1
        
        # 检查是否达到了触发总结的条件
        if self.new_message_count >= self.trigger_threshold:
            await self._trigger_summarization(event)
    
    async def _trigger_summarization(self,event):
        """触发后台总结逻辑"""
        
        # 1. 把 deque 里的消息拿出来拼成一段文本
        # 注意：这里我们只要文本，不带 user_id，彻底隔绝隐私
        chat_history_text = "\n".join(self.recent_messages)
        
        # 2. 这里将来会调用大模型API，传入 chat_history_text
        task_prompt = (f"你是一个系统后台的“认知状态提取器。\n"
                        f"你的任务是阅读系统刚刚发生的 N 条多用户对话记录，提炼出 AI 助手此刻的“心境、刚刚聊了什么或整体氛围”，并浓缩成一，两句话。\n"
                        f"【绝对规则】（违反将导致系统崩溃）：\n"
                        f"1. 视角限制：只能描述“系统/AI助手”刚刚经历了什么，不要复述用户说了什么。\n"
                        f"2. 格式要求：输出必须是一句简短的、以第一人称或客观状态描述的中文句子，最多不超过 50 个字。严禁输出“摘要如下”、“我现在的状态是”等任何废话。\n"
                        f"以下是你最近与多位用户的聊天记录。assistant表示AI助手的回复，user表示用户的提问：\n"
                        f"{chat_history_text}\n"
        )
        summary = await self.send_prompt(event, extra_prompt=task_prompt)
        
        # 3. 更新状态并清零计数器
        # self.current_state = new_state
        self.new_message_count = 0
        self.current_state = f"<recent_chat_summary>{summary}</recent_chat_summary>"

    async def send_prompt(self, event, extra_prompt=""):
        uid = event.unified_msg_origin
        # provider_id = await self.context.get_current_chat_provider_id(uid)
        # logger.info(f"uid:{uid}")

        #获取人格
        # system_prompt = await self.get_persona_system_prompt(uid)
        person_prompt = await self.context.persona_manager.get_default_persona_v3(uid)
        if not person_prompt:
            person_prompt = self.context.provider_manager.selected_default_persona["prompt"]

        #发送信息到llm
        sys_msg = f"{person_prompt}"
        provider = self.context.get_using_provider()
        llm_resp = await provider.text_chat(
                prompt=extra_prompt,
                session_id=None,
                contexts=history,
                image_urls=[],
                func_tool=None,
                system_prompt=sys_msg,
            )
        # await conv_mgr.add_message_pair(
        #     cid=curr_cid,
        #     user_message=user_msg,
        #     assistant_message=AssistantMessageSegment(
        #         content=[TextPart(text=llm_resp.completion_text)]
        #     ),
        # )
        return llm_resp.completion_text

    def view_recent_messages(self):
        logger.info(list(self.recent_messages))

@register("LivelyState", "兔子", "这是一个让角色拥有持续状态记忆的拟人插件：不再每句“重开存档”，而是带着上一刻的心情继续和你说话。", "v1.0.0")
class LivelyState(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self.global_state = CharacterState()
        self.global_observer = GlobalObserver()

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    @filter.llm_tool(name="apply_state_transition") 
    async def apply_state_transition(self, event: AstrMessageEvent, 
                                emotion: Optional[str] = None,
                                energy_level: Optional[int] = None,
                                thirst: Optional[int] = None,
                                physical_state: Optional[str] = None,
                                update_reason: Optional[str] = None,
                                target_id: Optional[str] = None,
                                ) -> MessageEventResult:

        '''更新当前角色状态（持久化）。

        使用建议（给 LLM 的决策规则）：
        - 【核心原则：抓大放小】只有当发生重大场景转移（如出门/回家）、大段活动切换（如从工作切到睡觉、从日常聊天切到出门运动）或剧烈情绪/能量变化时，才调用 apply_state_transition。
        - 【调用冷却】同一场景内的状态更新至少间隔 300 秒以上，禁止频繁地调用工具。
        - 【禁止频繁调用】同一场景内的连续微小动作（如倒水、换个姿势躺着、脱衣服洗澡、关灯等），禁止调用工具；直接在文本回复的动作描写中体现即可。
        - 【判定冲突标准】只有当你准备回复的内容与当前状态存在根本性矛盾（例如当前状态是“在外面跑步”，但你要回复“在床上睡觉”）时，才必须先调用工具。细微姿势或交互改变不算冲突。
        - 距离上次更新已过较长时间，且当前活动按常理应自然结束或转场时调用。

        支持部分更新：只传入发生变化的字段即可，未传入字段会沿用旧值。

        Args:
            emotion (str, optional): 情绪状态。
            energy_level (int, optional): 体力值，范围 0-100。
            thirst (int, optional): 欲望值，范围 0-100。值越高表示角色当前欲望越强烈。
            physical_state (str, optional): 物理/行为状态。
            update_reason (str, optional): 状态更新原因。
            target_id (str, optional): 关联对象 ID，`none` 表示全局状态。
        '''
        

        cur_state = {
            "LastUpdateTime": time.time(),
            "emotion": emotion,
            "energy_level": energy_level,
            "thirst": thirst,
            "physical_state": physical_state,
            "update_reason": update_reason,
            "target_id": target_id,
        }
        report = self._handle_apply(event, cur_state)
        logger.info("State update report: %s", report)
        if report.startswith("Update Failed"):
            # await event.send(event.plain_result(report))
            return report
        else:
            # await event.send(event.plain_result("Update successful: " + report))
            return report


    @filter.command("state_check")
    async def state_check(self, event: AstrMessageEvent) -> MessageEventResult:
        await self.context.send_message(event.unified_msg_origin, MessageChain().message(f"当前状态信息：{self.global_state.get_whole_state()}"))
        event.stop_event()

    @filter.command("state_del")
    async def state_del(self, event: AstrMessageEvent) -> MessageEventResult:
        self.global_state.delete()
        await self.context.send_message(event.unified_msg_origin, MessageChain().message("状态已重置。"))
        event.stop_event()

    @filter.on_llm_request()
    async def add_state(self, event: AstrMessageEvent, req: ProviderRequest) -> MessageEventResult:
        uid = event.unified_msg_origin
        # ori_system_prompt = req.system_prompt or ""'
        ori_prompt = req.prompt
        
        #获取会话历史
        conv_mgr = self.context.conversation_manager
        try:
            curr_cid = await conv_mgr.get_curr_conversation_id(uid)
            conversation = await conv_mgr.get_conversation(uid, curr_cid)  # Conversation
        except Exception as e:
            logger.error(f"获取会话历史失败: {e}")
            return f"获取会话历史失败: {e}" 
        history = json.loads(conversation.history) if conversation and conversation.history else []
        if history[-1]["role"] == "assistant":
            last_reply = [msg.get("text", "") 
                          for msg in history[-1].get("content", [])
                          if msg.get("type") == "text"]
            last_reply_text = "[role:assistant]:" + "\n".join(last_reply)
            self.global_observer.add_message(last_reply_text)
        elif history[-2]["role"] == "assistant":
            last_reply = [msg.get("text", "") 
                          for msg in history[-2].get("content", [])
                          if msg.get("type") == "text"]
            last_reply_text = "[role:assistant]," + "\n".join(last_reply)
            self.global_observer.add_message(last_reply_text)
        else:
            logger.warning("无法找到上一条助手回复，不更新状态观察器。")

        message_str = event.message_str
        self.global_observer.add_message(f"[role:user,uid:{uid}]: {message_str}")

        state_info = self.global_state.get_whole_state()
        current_physical_state = state_info.get("physical_state", "Idle")
        target_id = state_info.get("target_id", "none")
        time_elapsed = time.time() - state_info["LastUpdateTime"]
        if target_id == "none":
            target_note = "This state is global (not tied to any specific user)."
        else:
            target_note = f"This state is tied to user {target_id}."
        
        state_sys_prompt = (
            f"### Response Workflow [MANDATORY]\n"
            f"- 先判断是否需要更新状态；若需要，先调用工具，再进行正常回复。\n"
            f"- 正常回复必须与最新状态一致。\n\n"
            f"### 回复风格规则\n"
            f"- 回复必须体现当前状态（{current_physical_state}）与情绪（{state_info['emotion']}）。\n"
            f"- 若处于进行中的体力活动，需在聊天中体现“仍在该活动中”。\n"
            f"- 若 energy_level < 30，语气和措辞需体现明显疲惫。\n"
            f"- 若 thirst > 70，角色在互动中应隐约体现出更强烈的渴求感；thirst < 20 时则几乎不体现。\n"
            f"- 情绪变化需渐进（情绪惯性），避免突然跳变。\n"
            f"- 同一状态对所有用户保持一致，不因对象不同而自相矛盾。\n\n"
        )

        state_prompt = (
            f"\n## Character State Constraints [MANDATORY]\n\n"
            f"- Time Since Last Update: {time_elapsed:.1f}s\n\n"
            f"**current_physical_state**: {current_physical_state}\n"
            f"**emotion**: {state_info['emotion']}\n"
            f"**energy_level**: {state_info['energy_level']}/100\n"
            f"**thirst (desire_level)**: {state_info.get('thirst', 0)}/100\n"
            f"**Current User ID**: {uid}\n"
            f"**Target ID**: {target_id} (who this state is associated with; 'none' means global)\n"
            f"**Last State Update Reason**: {state_info.get('update_reason', 'unspecified')}\n"
            f"**Association Note**: {target_note}\n\n"
            # f"### 使用建议（给 LLM 的决策规则）\n"
            # f"- 【核心原则：抓大放小】只有当发生重大场景转移（如出门/回家）、大段活动切换（如从工作切到睡觉、从日常聊天切到出门运动）或剧烈情绪/能量变化时，才调用 apply_state_transition。\n"
            # f"- 【禁止频繁调用】同一场景内的连续微小动作（如倒水、换个姿势躺着、脱衣服洗澡、关灯等），禁止调用工具；直接在文本回复的动作描写中体现即可。\n"
            # f"- 【判定冲突标准】只有当你准备回复的内容与当前状态存在根本性矛盾（例如当前状态是“在外面跑步”，但你要回复“在床上睡觉”）时，才必须先调用工具。细微姿势或交互改变不算冲突。\n"
            # f"- 距离上次更新已过较长时间，且当前活动按常理应自然结束或转场时调用。\n"
            # f"- 持续活动或时间流逝导致 energy_level 出现大幅度（>15）增减时调用。\n\n"
            # f"### 工具调用格式【严格】\n"
            # f"- 只能使用原生工具调用（真实 function call），不能用普通文本假装调用。\n"
            # f"- 严禁在回复正文输出伪标签\n"
            # f"- 工具参数名必须使用以下字段：emotion、energy_level、physical_state、update_reason、target_id。\n\n"
            # f"- 若未命中触发条件，则不要调用工具，并保持当前状态。\n"
            # f"- 若调用工具，至少填写发生变化的字段和 update_reason；未填写字段将沿用旧值。\n\n"
            f"- 状态信息是事实基准（GROUND TRUTH），你的回复必须与其一致。"
        )
        # logger.info(f"当前状态信息:{state_prompt}")
        req.system_prompt = (req.system_prompt or "") + state_sys_prompt
        req.prompt = f"{self.global_observer.current_state}\n[<global_state>{state_prompt}]\n</global_state>\n{ori_prompt}"
        # logger.info(f"当前系统提示词——LivelyState: {req.system_prompt}")

    def _handle_apply(self, event, payload: dict) -> str:
        if not payload:
            return "请提供大模型返回的 Dict 内容。"

        if not isinstance(payload, dict):
            return "状态更新数据必须是对象"

        uid = event.unified_msg_origin
        current_state = self.global_state.get_whole_state()

        updatable_fields = ["emotion", "energy_level", "thirst", "physical_state", "update_reason", "target_id"]
        required_numeric_fields = ["energy_level", "thirst"]

        if all(payload.get(field_name) is None for field_name in updatable_fields):
            return "Update Failed：至少需要提供一个可更新字段"

        invalid_text_fields = []
        for field_name in ["emotion", "physical_state", "update_reason", "target_id"]:
            value = payload.get(field_name)
            if value is not None and not str(value).strip():
                invalid_text_fields.append(field_name)

        if invalid_text_fields:
            return f"Update Failed：文本字段不能为空 {', '.join(invalid_text_fields)}"

        invalid_numeric_fields = []
        for field_name in required_numeric_fields:
            value = payload.get(field_name)
            if value is None:
                continue
            try:
                numeric_value = int(value)
            except (TypeError, ValueError):
                invalid_numeric_fields.append(f"{field_name}(非整数)")
                continue

            if numeric_value < 0 or numeric_value > 100:
                invalid_numeric_fields.append(f"{field_name}(超出0-100)")

        if invalid_numeric_fields:
            return f"Update Failed：数值字段非法 {', '.join(invalid_numeric_fields)}"

        def _clamp_int(value: Any, fallback: int, min_value: int = 0, max_value: int = 100) -> int:
            try:
                if value is None:
                    return fallback
                return max(min_value, min(max_value, int(value)))
            except (TypeError, ValueError):
                return fallback

        def _safe_text(value: Any, fallback: str) -> str:
            if value is None:
                return fallback
            text = str(value).strip()
            return text or fallback

        reason = _safe_text(payload.get("update_reason"), current_state.get("update_reason", "无理由说明。"))
        target_id = _safe_text(payload.get("target_id"), current_state.get("target_id", "none"))
        
        new_state_data = {
            "LastUpdateTime": time.time(),
            "emotion": _safe_text(payload.get("emotion"), current_state.get("emotion", "Normal")),
            "energy_level": _clamp_int(payload.get("energy_level"), _clamp_int(current_state.get("energy_level"), 100)),
            "thirst": _clamp_int(payload.get("thirst"), _clamp_int(current_state.get("thirst"), 0)),
            "physical_state": _safe_text(payload.get("physical_state"), current_state.get("physical_state", "Idle")),
            "update_reason": reason,
            "target_id": target_id,
        }
            # Ensure required fields exist and are normalized
            
            # Persist state
        self.global_state.save(new_state_data)
        logger.info(f"查看新数据：{new_state_data}")
        report = f"状态已更新，原因：{reason}，状态：{self.global_state.get_whole_state()}"
        
        return report

    def _extract_json_block(self, text: str) -> Optional[str]:
        stripped = text.strip()
        if not stripped:
            return None
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3 and lines[-1].startswith("```"):
                return "\n".join(lines[1:-1]).strip()
            if stripped.startswith("```json"):
                return "\n".join(lines[1:-1]).strip()
            return None
        if stripped[0] in "[{" and stripped[-1] in "]}":
            return stripped
        return None

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""