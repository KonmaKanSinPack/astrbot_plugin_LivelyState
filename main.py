import json
import time
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from typing import Any, Dict, List, Optional, Tuple
class CharacterState:
    def __init__(self):
        self.LastUpdateTime = time.time()
        self.Emotion = "Normal"
        self.Energy = 100
        self.Thirst = 0
        self.State = "Idle"
    
    def get_whole_state(self):
        return {
            "LastUpdateTime": self.LastUpdateTime,
            "Emotion": self.Emotion,
            "Energy": self.Energy,
            "Thirst": self.Thirst,
            "State": self.State
        }

    def update(self, current_time, enable_update=True):
        # 更新角色状态的逻辑
        self.LastUpdateTime = current_time
        # 这里可以添加更多的状态更新逻辑
        if enable_update:
            pass

# class StateManager: 本来想写不同ID独立状态的，想了想还是算了
#     def __init__(self):
#         self.character_states =    

@register("LivelyState", "兔子", "状态机", "0.0.1")
class LivelyState(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self.global_state = CharacterState()


    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    @filter.command("state_check")
    async def state_check(self, event: AstrMessageEvent) -> MessageEventResult:
        await self.context.send_message(event.unified_msg_origin, f"当前状态信息：{self.global_state.get_whole_state()}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> MessageEventResult:
        
        state_prompt = self._handle_prompt(event)
        # logger.info("状态提示词创建完毕")
        llm_response = await self.send_prompt(event, state_prompt)
        # logger.info(f"状态提示词发送完毕，收到回复\n{llm_response}")
        report = self._handle_apply(event, llm_response)
        logger.info("State update report: %s", report)
        state_info = self.global_state.get_whole_state()
        state_prompt = (
            f"\n## Character State Constraints\n"
            f"**Current State**: {state_info['State']}\n"
            f"**Emotion**: {state_info['Emotion']} | **Energy**: {state_info['Energy']}/100 | **Desire**: {state_info['Thirst']}/100\n"
            f"Consider the above state factors when responding. Ensure your response is consistent with the character's current state."
        )
        req.system_prompt += state_prompt
        # logger.info(f"Current system prompt: {req.system_prompt}")

    def _handle_prompt(self, event: AstrMessageEvent) -> str:
        # if not conversation:
        #     return "Attach conversation text after the prompt sub-command, e.g., /memory prompt recent conversation."
        logger.info("Creating state prompt, operator: %s", event.get_sender_name())

        cur_msg = event.message_str
        time_elapsed = time.time() - self.global_state.LastUpdateTime
        current_time = time.time()
        
        template = (
            "## Character State Assessment Task\n\n"
            "### Current Context\n"
            f"- User Latest Message: {cur_msg}\n"
            f"- Previous State: {self.global_state.State}\n"
            f"- Time Since Last Update: {time_elapsed:.1f}s\n\n"
            "### Character Current State\n"
            f"{json.dumps(self.global_state.get_whole_state(), ensure_ascii=False, indent=2)}\n\n"
            "### Task\n"
            "Based on the user message and conversation history, evaluate whether the character's state needs to be updated. Analysis must follow logical consistency and physical/physiological rules.\n\n"
            "### Evaluation Criteria\n"
            "1. **Causality**: State changes must be reasonably explained and aligned with time progression and event relationships.\n"
            "2. **Physical Logic**: State transitions must follow real-world physical laws.\n"
            "3. **Physiological Logic**: Consider energy depletion, emotional fluctuations, and other physiological factors.\n"
            "4. **Consistency**: Ensure new state aligns with historical behavior.\n\n"
            "### Output Format (JSON)\n"
            "{\n"
            "  \"summary\": {\n"
            "    \"do_update\": true/false,\n"
            "    \"update_reason\": \"Brief explanation of whether state needs update and why\"\n"
            "  },\n"
            "  \"whole_state\": {\n"
            "    \"Emotion\": \"current mood\",\n"
            "    \"Energy\": <0-100>,\n"
            "    \"Thirst\": <0-100>,\n"
            "    \"State\": \"current behavioral state\"\n"
            "  }\n"
            "}\n\n"
            "**IMPORTANT**: The whole_state field is only required when do_update is true. Output must be valid JSON."
        )

        return template

    def _handle_apply(self, event, payload_text: str) -> str:
        payload_text = payload_text.strip()
        if not payload_text:
            return "请提供大模型返回的 JSON 内容。"

        json_text = self._extract_json_block(payload_text)
        if json_text is None:
            return "未能解析 JSON，请直接粘贴模型输出或 ```json ``` 代码块。"

        try:
            operations = json.loads(json_text)
        except json.JSONDecodeError as exc:
            return f"JSON 解析失败: {exc}"

        uid = event.unified_msg_origin

        do_update = operations.get("summary", {}).get("do_update", False)
        reason = operations.get("summary", {}).get("update_reason", "无理由说明。")
        
        if do_update:
            new_state_data = operations.get("whole_state", {})
            if new_state_data:
                self.global_state.Emotion = new_state_data.get("Emotion", self.global_state.Emotion)
                self.global_state.Energy = new_state_data.get("Energy", self.global_state.Energy)
                self.global_state.Thirst = new_state_data.get("Thirst", self.global_state.Thirst)
                self.global_state.State = new_state_data.get("State", self.global_state.State)
                self.global_state.LastUpdateTime = time.time()
                report = f"状态已更新，原因：{reason}，状态：{self.global_state.get_whole_state()}"
            else:
                report = f"未提供新的状态数据，状态未更新。原因：{reason}"
        else:
            report = f"无需更新状态。原因：{reason}"
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

    async def send_prompt(self,event: AstrMessageEvent,prompt: str) -> str:
        #获取UID
        uid = event.unified_msg_origin

        #获取人格
        person_prompt = await self.get_persona_system_prompt(uid)

        #获取历史记录
        conver_mgr = self.context.conversation_manager
        cur_cid = await conver_mgr.get_curr_conversation_id(uid)
        conversation = await conver_mgr.get_conversation(uid, cur_cid)
        history = json.loads(conversation.history) if conversation and conversation.history else []

        #发送信息到llm
        sys_msg = f"{person_prompt}"
        provider = self.context.get_using_provider()
        llm_resp = await provider.text_chat(
                prompt=prompt,
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


    async def get_persona_system_prompt(self, session: str) -> str:
        """获取人格系统提示词

        Args:
            session: 会话ID

        Returns:
            人格系统提示词
        """
        base_system_prompt = ""
        try:
            # 尝试获取当前会话的人格设置
            uid = session  # session 就是 unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
                uid
            )

            # 获取默认人格设置
            default_persona_obj = self.context.provider_manager.selected_default_persona

            if curr_cid:
                conversation = await self.context.conversation_manager.get_conversation(
                    uid, curr_cid
                )

                if (
                    conversation
                    and conversation.persona_id
                    and conversation.persona_id != "[%None]"
                ):
                    # 有指定人格，尝试获取人格的系统提示词
                    personas = self.context.provider_manager.personas
                    if personas:
                        for persona in personas:
                            if (
                                hasattr(persona, "name")
                                and persona.name == conversation.persona_id
                            ):
                                base_system_prompt = getattr(persona, "prompt", "")
                                
                                break

            # 如果没有获取到人格提示词，尝试使用默认人格
            if (
                not base_system_prompt
                and default_persona_obj
                and default_persona_obj.get("prompt")
            ):
                base_system_prompt = default_persona_obj["prompt"]
                

        except Exception as e:
            logger.warning(f"获取人格系统提示词失败: {e}")

        return base_system_prompt

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""