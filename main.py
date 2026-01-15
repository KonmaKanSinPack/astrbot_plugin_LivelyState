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
class CharacterState:
    def __init__(self):
        self.path = StarTools.get_data_dir() / f"global_state.json"

    
    def default_state(self) -> Dict[str, Any]:
        return {
            "LastUpdateTime": time.time(),
            "Emotion": "Normal",
            "Energy": 100,
            "Thirst": 0,
            "State": "Idle",
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

    def update(self, current_time, enable_update=True):
        # 更新角色状态的逻辑
        # self.LastUpdateTime = current_time
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
        await self.context.send_message(event.unified_msg_origin, MessageChain().message(f"当前状态信息：{self.global_state.get_whole_state()}"))
        event.stop_event()

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> MessageEventResult:
        uid = event.unified_msg_origin

        state_prompt = self._handle_prompt(event)
        # logger.info("状态提示词创建完毕")
        llm_response = await self.send_prompt(event, state_prompt)
        # logger.info(f"状态提示词发送完毕，收到回复\n{llm_response}")
        report = self._handle_apply(event, llm_response)
        logger.info("State update report: %s", report)
        state_info = self.global_state.get_whole_state()
        target_id = state_info.get("target_id", "none")
        if target_id == "none":
            target_note = "This state is global (not tied to any specific user)."
        elif target_id == uid:
            target_note = f"This state is tied to YOU (user {target_id})."
        else:
            target_note = f"This state is tied to ANOTHER user ({target_id}); maintain consistency with that context."
        state_prompt = (
            f"\n## Character State Constraints [MANDATORY]\n\n"
            f"**Current Physical State**: {state_info['State']}\n"
            f"**Emotional State**: {state_info['Emotion']}\n"
            f"**Energy Level**: {state_info['Energy']}/100 | **Desire Level**: {state_info['Thirst']}/100\n"
            f"**Current User ID**: {uid}\n"
            f"**Target ID**: {target_id} (who this state is associated with; 'none' means global)\n"
            f"**Last State Update Reason**: {state_info.get('update_reason', 'unspecified')}\n"
            f"**Association Note**: {target_note}\n\n"
            f"### Response Requirements:\n"
            f"1. **Your response MUST reflect the current state** ({state_info['State']})\n"
            f"2. If state is physical activity (Running, Bathing, Cooking, etc.):\n"
            f"   - Response should indicate you are CURRENTLY doing this activity\n"
            f"   - You can chat while doing it, but acknowledge the activity\n"
            f"   - Example: If Running → mention being out of breath, checking phone while running, etc.\n"
            f"3. If energy is low (<30), show fatigue in your response\n"
            f"4. Emotion ({state_info['Emotion']}) should be reflected in your tone\n"
            f"5. **NEVER pretend to be in a different state than specified above**\n"
            f"6. State origin stays consistent across users: if now Resting because just finished Running, say that to ANY user.\n\n"
            f"The state information is GROUND TRUTH - your response must align with it."
        )
        # logger.info(f"当前状态信息:{state_prompt}")
        req.system_prompt += state_prompt
        logger.info(f"Current system prompt: {req.system_prompt}")

    def _handle_prompt(self, event: AstrMessageEvent) -> str:
        # if not conversation:
        #     return "Attach conversation text after the prompt sub-command, e.g., /memory prompt recent conversation."
        logger.info("Creating state prompt, operator: %s", event.get_sender_name())

        cur_msg = event.message_str
        uid = event.unified_msg_origin
        state_info = self.global_state.get_whole_state()
        time_elapsed = time.time() - state_info["LastUpdateTime"]
        current_state = state_info["State"]
        target_id = state_info.get("target_id", "none")
        
        template = (
            "## Character State Assessment Task\n\n"
            "### Current Context\n"
            f"- User Latest Message: {cur_msg}\n"
            f"- Current User ID: {uid}\n"
            f"- Current State Target ID: {target_id} (who this state is associated with; 'none' means global)\n"
            f"- Time Since Last Update: {time_elapsed:.1f}s\n\n"
            "### Character Current State\n"
            f"{json.dumps(state_info, ensure_ascii=False, indent=2)}\n\n"
            "### Task\n"
            "Evaluate whether the character's state needs to be updated based on TIME PROGRESSION and OBJECTIVE REALITY, NOT just user messages.\n\n"
            "### CRITICAL State Transition Rules\n"
            "0. **STATE HAS ABSOLUTE PRIORITY**:\n"
            "   - The current State field is the GROUND TRUTH of what the character is doing\n"
            "   - If conversation history conflicts with current state (e.g., history shows chatting but state=Running), TRUST THE STATE\n"
            "   - Character can chat WHILE doing other activities (running, cooking, resting)\n"
            "   - Do NOT change state just to match conversation context\n"
            "   - Only update state based on TIME and PHYSICAL REALITY, not conversation inference\n\n"
            "1. **Physical Activities Cannot Be Interrupted Instantly**:\n"
            "   - Running, exercising, bathing, cooking, etc. require COMPLETION TIME\n"
            "   - Receiving a message does NOT instantly change physical state\n"
            "   - Example: If Running → can reply while running OR finish running first (based on time elapsed)\n"
            "   - Example: If Bathing (5 min ago) → still bathing, can't instantly switch to Chatting\n\n"
            "2. **Time-Based State Evolution**:\n"
            "   - Consider how much time has passed since last update\n"
            "   - Activities have natural durations: Sleeping (6-8h), Eating (15-30m), Exercise (30m-1h)\n"
            "   - State changes must align with realistic time requirements\n\n"
            "3. **Message Interaction Rules**:\n"
            "   - User messages do NOT force immediate state changes\n"
            "   - Character can respond WHILE maintaining current state\n"
            "   - Only update state if: (a) enough time passed for activity completion, OR (b) user message explicitly requests state change\n\n"
            "4. **Energy & Physiological Constraints**:\n"
            "   - Energy depletes during activities, recovers during rest/sleep\n"
            "   - Low energy (<20) limits physical activities\n"
            "   - State transitions must respect current energy levels\n\n"
            "5. **Logical Consistency**:\n"
            "   - Cannot teleport or skip intermediate states\n"
            "   - Example: Running → Idle/Resting (valid) | Running → Sleeping (invalid, must rest first)\n"
            "   - State changes need causal justification\n\n"
            "### Evaluation Criteria\n"
            "- **Priority 0**: Is current state the GROUND TRUTH? (YES - do not change based on conversation history)\n"
            "- **Priority 1**: Does enough TIME support this state change?\n"
            "- **Priority 2**: Is the transition PHYSICALLY/LOGICALLY possible?\n"
            "- **Priority 3**: Does user message REQUIRE state change, or can character respond in current state?\n\n"
            "### Output Format (JSON)\n"
            "{\n"
            "  \"summary\": {\n"
            "    \"do_update\": true/false,\n"
            f"    \"target_id\": \"If state relates to current user, use '{uid}'; use 'none' for global state; use other user_id if relates to another user\"\n"
            "  },\n"
            "  \"whole_state\": {\n"
            "    \"LastUpdateTime\": <current_timestamp>,\n"
            "    \"Emotion\": \"emotional state\",\n"
            "    \"Energy\": <0-100>,\n"
            "    \"Thirst\": <0-100>,\n"
            "    \"State\": \"new state (only if justified by time/logic)\",\n"
            "    \"update_reason\": \"Explain based on TIME ELAPSED and OBJECTIVE REALITY, consistent across all users\",\n"
            f"    \"target_id\": \"Same as summary.target_id - use '{uid}' for current user, 'none' for global, or specific user_id\"\n"
            "  }\n"
            "}\n\n"
            "**IMPORTANT**:\n"
            "- Current State is ALWAYS correct - conversation history may be misleading\n"
            "- Set do_update=false if insufficient time has passed for state completion\n"
            "- Do NOT infer state from conversation - use explicit State field only\n"
            "- whole_state required only when do_update=true\n"
            "- Output must be valid JSON\n"
            "- AVOID instant state switches just because user sent a message"
        )

        return template

    def _handle_apply(self, event, payload_text: str) -> str:
        if not payload_text:
            return "请提供大模型返回的 JSON 内容。"

        json_text = self._extract_json_block(payload_text)
        if json_text is None:
            return "未能解析 JSON，请直接粘贴模型输出或 ```json ``` 代码块。"

        try:
            # json_repair.loads() returns parsed object directly (dict/list)
            operations = json_repair.loads(json_text)
            logger.info("JSON parsed successfully: %s", operations)
        except Exception as e:
            logger.warning("JSON repair failed, fallback to standard parser: %s", e)
            try:
                operations = json.loads(json_text.strip())
            except json.JSONDecodeError as exc:
                return f"JSON parsing failed: {exc}"

        uid = event.unified_msg_origin

        do_update = operations.get("summary", {}).get("do_update", False)
        reason = operations.get("summary", {}).get("update_reason", "无理由说明。")
        target_id = operations.get("summary", {}).get("target_id") or "none"
        
        if do_update:
            new_state_data = operations.get("whole_state", {})
            if new_state_data:
                # Ensure required fields exist and are normalized
                new_state_data.setdefault("LastUpdateTime", time.time())
                new_state_data.setdefault("target_id", target_id)
                new_state_data.setdefault("update_reason", reason)
                # Persist state
                self.global_state.save(new_state_data)
                logger.info(f"查看新数据：{new_state_data}")
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
        logger.info(f"当前提供商：{provider}")
        logger.info(f"获取会话的配置文件{self.context.astrbot_config_mgr.get_conf(uid)}")
        # logger.info(f"获取提供商配置{self.context.astrbot_config_mgr.g(uid, "provider_settings")}")
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