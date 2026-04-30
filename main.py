from collections import deque
import json
import time
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from typing import Any, Dict, List, Optional, Tuple
from astrbot.api.event import MessageChain
from astrbot.api.star import StarTools
from astrbot.api import AstrBotConfig
import json_repair

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

class CharacterState:
    # 这组状态是当前插件认可的“唯一全局身体状态集合”。
    # 目的不是把角色写死，而是把 physical_state 从自由文本收敛为有限状态，
    # 让不同会话看到的是同一套身体事实，而不是每轮 prompt 各写各的。
    STATE_MACHINE: Dict[str, Dict[str, Any]] = {
        "Idle": {
            "label": "空闲",
            "description": "当前没有持续中的主活动，可以自然接入对话。",
            "energy_delta": 1,
            "thirst_delta": 1,
            "auto_fallback": None,
            "allowed_next_states": ["Idle", "Resting", "Sleeping", "Working", "Exercising", "Traveling", "Socializing"],
        },
        "Resting": {
            "label": "休息中",
            "description": "正在恢复体力，但仍保持清醒，可继续对话。",
            "energy_delta": 3,
            "thirst_delta": 1,
            "auto_fallback": "Idle",
            "allowed_next_states": ["Idle", "Resting", "Sleeping", "Socializing"],
        },
        "Sleeping": {
            "label": "睡眠中",
            "description": "处于睡眠或半睡眠状态，回复应明显带有迟缓感。",
            "energy_delta": 5,
            "thirst_delta": 1,
            "auto_fallback": "Idle",
            "allowed_next_states": ["Idle", "Resting", "Sleeping"],
        },
        "Working": {
            "label": "忙碌中",
            "description": "正在处理工作或持续任务，回复应体现分心或忙碌感。",
            "energy_delta": -2,
            "thirst_delta": 2,
            "auto_fallback": "Idle",
            "allowed_next_states": ["Idle", "Resting", "Working", "Traveling", "Socializing"],
        },
        "Exercising": {
            "label": "运动中",
            "description": "正在进行较高体力消耗活动，回复应体现喘息、动作延续或疲劳累积。",
            "energy_delta": -4,
            "thirst_delta": 3,
            "auto_fallback": "Resting",
            "allowed_next_states": ["Idle", "Resting", "Exercising", "Traveling"],
        },
        "Traveling": {
            "label": "移动中",
            "description": "正在路上、出门或转场途中，回复应体现不完全稳定的环境。",
            "energy_delta": -2,
            "thirst_delta": 2,
            "auto_fallback": "Idle",
            "allowed_next_states": ["Idle", "Resting", "Working", "Exercising", "Traveling", "Socializing"],
        },
        "Socializing": {
            "label": "互动中",
            "description": "正在持续与人互动或参与社交场景，但不代表身体状态被重新定义。",
            "energy_delta": -1,
            "thirst_delta": 1,
            "auto_fallback": "Idle",
            "allowed_next_states": ["Idle", "Resting", "Working", "Traveling", "Socializing"],
        },
    }

    STATE_ALIASES: Dict[str, str] = {
        "idle": "Idle",
        "rest": "Resting",
        "resting": "Resting",
        "sleep": "Sleeping",
        "sleeping": "Sleeping",
        "nap": "Sleeping",
        "work": "Working",
        "working": "Working",
        "busy": "Working",
        "exercise": "Exercising",
        "exercising": "Exercising",
        "run": "Exercising",
        "running": "Exercising",
        "train": "Exercising",
        "travel": "Traveling",
        "traveling": "Traveling",
        "outside": "Traveling",
        "move": "Traveling",
        "moving": "Traveling",
        "social": "Socializing",
        "socializing": "Socializing",
        "chat": "Socializing",
        "interacting": "Socializing",
        "互动": "Socializing",
        "聊天": "Socializing",
        "社交": "Socializing",
        "空闲": "Idle",
        "待机": "Idle",
        "休息": "Resting",
        "躺": "Resting",
        "睡": "Sleeping",
        "睡觉": "Sleeping",
        "工作": "Working",
        "忙": "Working",
        "运动": "Exercising",
        "跑": "Exercising",
        "出门": "Traveling",
        "路上": "Traveling",
    }

    def __init__(self, update_interval_sec: int = 300, active_state_timeout_sec: int = 1800):
        self.path = StarTools.get_data_dir() / f"global_state.json"
        # 这是给用户手写 Body_Sheet / History 初始字段的固定模板文件。
        # 把模板从代码里拆出来后，后续扩字段不需要再改 main.py。
        self.template_path = Path(__file__).with_name("state_profile_template.json")
        # 这里限制一个最小步长，是为了避免状态每次读取都发生细碎抖动，
        # 否则全局状态会因为请求过于频繁而显得不稳定。
        self.update_interval_sec = max(60, int(update_interval_sec))
        # 持续中的动作不能无限挂着；超过这个时间后自动回落到 Idle，
        # 用确定性规则兜底，避免模型忘记更新状态时出现“永远在跑步”。
        self.active_state_timeout_sec = max(self.update_interval_sec, int(active_state_timeout_sec))

    def load_profile_template(self) -> Dict[str, Any]:
        """读取可编辑的长期事实模板文件。

        这个模板只负责定义初始字段和默认骨架，不负责覆盖运行中的真实状态。
        这样用户可以自由扩字段，同时又不会把已经累积的状态记录洗掉。
        """
        fallback_template = {
            "Body_Sheet": {},
            "History": {},
        }

        if not self.template_path.exists():
            return fallback_template

        try:
            template_data = json_repair.loads(self.template_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to parse profile template, using fallback template. Error: {e}")
            return fallback_template

        if not isinstance(template_data, dict):
            return fallback_template

        return {
            "Body_Sheet": self.normalize_body_sheet(template_data.get("Body_Sheet", {})),
            "History": self.normalize_history(template_data.get("History", {})),
        }

    
    def default_state(self) -> Dict[str, Any]:
        profile_template = self.load_profile_template()
        return {
            "LastUpdateTime": time.time(),
            "emotion": "Normal",
            "energy_level": 100,
            "thirst": 0,
            "physical_state": "Idle",
            "update_reason": "Initial state",
            "target_id": "none",
            # Body_Sheet / History 的初始骨架来自可编辑模板文件，
            # 这样你可以直接在模板里定义应该有哪些默认字段。
            "Body_Sheet": profile_template["Body_Sheet"],
            "History": profile_template["History"],
            # 这两个字段只用于硬冷却判断，不参与角色设定本身。
            # 单独记录是为了避免自然状态推进刷新 LastUpdateTime 后，误伤真正的工具冷却逻辑。
            "_last_fast_state_update_time": 0.0,
            "_last_body_sheet_update_time": 0.0,
        }

    def normalize_body_sheet(self, body_sheet: Any) -> Dict[str, Dict[str, str]]:
        """把 Body_Sheet 规范化成 {部位: {属性: 描述}}。

        这样写的原因：身体档案是长期事实，必须尽量稳定。
        这里做一次结构清洗，能避免 LLM 或人工改出半残的嵌套结构。
        """
        if not isinstance(body_sheet, dict):
            return {}

        normalized: Dict[str, Dict[str, str]] = {}
        for part_name, attributes in body_sheet.items():
            safe_part_name = str(part_name).strip()
            if not safe_part_name or not isinstance(attributes, dict):
                continue

            normalized_attributes: Dict[str, str] = {}
            for attribute_name, attribute_value in attributes.items():
                safe_attribute_name = str(attribute_name).strip()
                safe_attribute_value = str(attribute_value).strip() if attribute_value is not None else ""
                if safe_attribute_name and safe_attribute_value:
                    normalized_attributes[safe_attribute_name] = safe_attribute_value

            if normalized_attributes:
                normalized[safe_part_name] = normalized_attributes

        return normalized

    def normalize_history(self, history: Any) -> Dict[str, int]:
        """把 History 规范化成 {计数名: 非负整数}。

        History 的定位是累计量，不是自由文本日志；因此这里强制收敛成整数计数。
        """
        if not isinstance(history, dict):
            return {}

        normalized: Dict[str, int] = {}
        for counter_name, counter_value in history.items():
            safe_counter_name = str(counter_name).strip()
            if not safe_counter_name:
                continue

            try:
                normalized[safe_counter_name] = max(0, int(counter_value))
            except (TypeError, ValueError):
                continue

        return normalized

    def merge_body_sheet(self, current_body_sheet: Any, body_sheet_updates: Any) -> Dict[str, Dict[str, str]]:
        """对长期身体档案做局部合并。

        这里不用整块覆盖，是为了支持“只更新一个部位的一项属性”，
        避免模型补一项描述时把其它已记录身体信息抹掉。
        """
        merged_body_sheet = {
            part_name: dict(attributes)
            for part_name, attributes in self.normalize_body_sheet(current_body_sheet).items()
        }

        for part_name, attributes in self.normalize_body_sheet(body_sheet_updates).items():
            merged_body_sheet.setdefault(part_name, {}).update(attributes)

        return merged_body_sheet

    def apply_history_delta(self, current_history: Any, history_delta: Any) -> Dict[str, int]:
        """把增量计数累加到 History 上。

        为什么用 delta：History 本质是累计统计，使用增量比“整份覆盖”更不容易被模型误改。
        """
        merged_history = dict(self.normalize_history(current_history))

        for counter_name, increment in self.normalize_history(history_delta).items():
            merged_history[counter_name] = merged_history.get(counter_name, 0) + increment

        return merged_history

    def list_available_states(self) -> List[str]:
        return list(self.STATE_MACHINE.keys())

    def resolve_physical_state(self, value: Any, fallback: Optional[str] = None) -> Tuple[str, bool]:
        """把自由输入映射成规范状态值。

        返回 `(state_name, is_recognized)`，这样调用方可以区分：
        是确实识别到了合法状态，还是仅仅退回到了 fallback。
        """
        normalized_fallback = fallback or "Idle"
        if value is None:
            return normalized_fallback, False

        raw_text = str(value).strip()
        if not raw_text:
            return normalized_fallback, False

        if raw_text in self.STATE_MACHINE:
            return raw_text, True

        lowered = raw_text.lower()
        if lowered in self.STATE_ALIASES:
            return self.STATE_ALIASES[lowered], True

        for alias, state_name in self.STATE_ALIASES.items():
            if alias and alias in lowered:
                return state_name, True

        return normalized_fallback, False

    def normalize_physical_state(self, value: Any, fallback: str = "Idle") -> str:
        state_name, _ = self.resolve_physical_state(value, fallback=fallback)
        return state_name

    def get_state_meta(self, physical_state: str) -> Dict[str, Any]:
        canonical_state = self.normalize_physical_state(physical_state)
        return self.STATE_MACHINE.get(canonical_state, self.STATE_MACHINE["Idle"])

    def get_allowed_transitions(self, physical_state: str) -> List[str]:
        canonical_state = self.normalize_physical_state(physical_state)
        return list(self.STATE_MACHINE.get(canonical_state, self.STATE_MACHINE["Idle"])["allowed_next_states"])

    def is_transition_allowed(self, current_state: str, next_state: str) -> bool:
        current_canonical = self.normalize_physical_state(current_state)
        next_canonical = self.normalize_physical_state(next_state, fallback=current_canonical)
        return next_canonical == current_canonical or next_canonical in self.get_allowed_transitions(current_canonical)

    def _normalize_state(self, state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """把持久化状态收敛回一份可用的标准结构。

        这样写的原因：全局状态文件可能来自旧版本、人工修改或 LLM 工具调用，
        在每次读取时先做归一化，比在各个调用点分别补漏洞更稳。
        """
        default_state = self.default_state()
        profile_template = self.load_profile_template()
        if not isinstance(state, dict):
            return default_state

        def _safe_int(value: Any, fallback: int) -> int:
            try:
                return max(0, min(100, int(value)))
            except (TypeError, ValueError):
                return fallback

        def _safe_text(value: Any, fallback: str) -> str:
            text = str(value).strip() if value is not None else ""
            return text or fallback

        def _safe_float(value: Any, fallback: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return fallback

        normalized = {
            "LastUpdateTime": default_state["LastUpdateTime"],
            "emotion": _safe_text(state.get("emotion"), default_state["emotion"]),
            "energy_level": _safe_int(state.get("energy_level"), default_state["energy_level"]),
            "thirst": _safe_int(state.get("thirst"), default_state["thirst"]),
            # 这里统一把旧状态、自然语言状态和工具输入状态压成固定枚举值，
            # 是实现“全局只有一套身体状态”的关键。
            "physical_state": self.normalize_physical_state(
                _safe_text(state.get("physical_state"), default_state["physical_state"]),
                fallback=default_state["physical_state"],
            ),
            "update_reason": _safe_text(state.get("update_reason"), default_state["update_reason"]),
            "target_id": _safe_text(state.get("target_id"), default_state["target_id"]),
            # 这里会把模板里新增但当前 state 里还没有的字段自动补进来；
            # 已有值则保留，避免用户修改模板后把运行中的记录整体覆盖掉。
            "Body_Sheet": self.merge_body_sheet(
                profile_template.get("Body_Sheet", default_state["Body_Sheet"]),
                state.get("Body_Sheet", default_state["Body_Sheet"]),
            ),
            "History": {
                **profile_template.get("History", default_state["History"]),
                **self.normalize_history(state.get("History", default_state["History"])),
            },
            "_last_fast_state_update_time": _safe_float(state.get("_last_fast_state_update_time"), 0.0),
            "_last_body_sheet_update_time": _safe_float(state.get("_last_body_sheet_update_time"), 0.0),
        }

        try:
            normalized["LastUpdateTime"] = float(state.get("LastUpdateTime", default_state["LastUpdateTime"]))
        except (TypeError, ValueError):
            normalized["LastUpdateTime"] = default_state["LastUpdateTime"]

        return normalized

    def _derive_emotion(self, state: Dict[str, Any]) -> str:
        """根据体力和欲望做保守的情绪修正。

        目的不是替代角色人格，而是给全局状态一个最低限度的一致反馈，
        让不同上下文下的情绪不会完全漂移。
        """
        energy_level = state["energy_level"]
        thirst = state["thirst"]
        current_emotion = state["emotion"]

        if energy_level <= 20:
            return "Tired"
        if thirst >= 85:
            return "Restless"
        if current_emotion in {"Tired", "Restless"} and energy_level >= 70 and thirst <= 40:
            return "Calm"
        return current_emotion

    def get_whole_state(self, enable_update: bool = True):
        if not self.path.exists():
            # File doesn't exist, create with default state
            state = self.default_state()
            self.save(state)
            return state
        else:
            # File exists, read it
            try:
                state = json_repair.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"Failed to parse state file, using default state. Error: {e}")
                state = self.default_state()
                self.save(state)
                return state

        normalized_state = self._normalize_state(state)
        if normalized_state != state:
            self.save(normalized_state)

        if enable_update:
            return self.update(time.time(), state=normalized_state)

        return normalized_state

    def save(self,state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def delete(self):
        if self.path.exists():
            self.path.unlink()

    def update(self, current_time: Optional[float] = None, enable_update: bool = True, state: Optional[Dict[str, Any]] = None):
        """按时间推进状态。

        为什么这么写：全局状态如果完全依赖 LLM 主动调用工具，就会频繁出现
        “状态冻结”。这里用确定性推进做兜底，让体力、欲望和持续动作随着时间
        自然变化，从而在不同会话上下文里仍能共享同一份连续状态。
        """
        if not enable_update:
            return state if state is not None else self.get_whole_state(enable_update=False)

        current_state = self._normalize_state(state if state is not None else self.get_whole_state(enable_update=False))
        now = current_time or time.time()
        elapsed = max(0, now - current_state["LastUpdateTime"])
        steps = int(elapsed // self.update_interval_sec)

        if steps <= 0:
            return current_state

        next_state = dict(current_state)
        state_meta = self.get_state_meta(next_state["physical_state"])
        # 显式状态机让每个状态的数值演化规则可控，
        # 避免“Running”“Workout”“在路上”这种自由文本在不同会话里各算各的。
        thirst_delta = steps * int(state_meta.get("thirst_delta", 1))
        energy_delta = steps * int(state_meta.get("energy_delta", 0))

        next_state["energy_level"] = max(0, min(100, next_state["energy_level"] + energy_delta))
        next_state["thirst"] = max(0, min(100, next_state["thirst"] + thirst_delta))

        fallback_state = state_meta.get("auto_fallback")
        # 对持续状态设置自动回退，是因为真正的目标不是“记住一个词”，
        # 而是维护一条连续、可信的身体轨迹。
        if fallback_state and elapsed >= self.active_state_timeout_sec:
            next_state["physical_state"] = fallback_state

        next_state["emotion"] = self._derive_emotion(next_state)
        next_state["LastUpdateTime"] = min(now, current_state["LastUpdateTime"] + steps * self.update_interval_sec)
        next_state["update_reason"] = "Natural time-based progression"

        if next_state != current_state:
            self.save(next_state)

        return next_state

# class StateManager: 本来想写不同ID独立状态的，想了想还是算了
#     def __init__(self):
#         self.character_states =    


class GlobalObserver:
    def __init__(self, max_size=50, trigger_threshold = 20):
        self.recent_messages = deque(maxlen=max_size)
        
        # 我们可以顺便设一个触发阈值，比如每攒够 20 条新消息就触发一次总结
        self.trigger_threshold = trigger_threshold
        self.new_message_count = 0  
        
        # 用来存储大模型总结出来的“当前状态”
        self.current_state = ""

    async def add_message(self, message_text, event, context):
        """积累最近对话，供全局观察者做短期环境总结。

        这个观察者只负责“最近聊了什么、氛围如何”，不负责定义物理状态真相；
        真正的全局身体/情绪状态仍以 CharacterState 为准。
        """
        if not str(message_text).strip():
            return

        self.recent_messages.append(message_text)
        self.new_message_count += 1
        
        # 检查是否达到了触发总结的条件
        if self.new_message_count >= self.trigger_threshold:
            if await self._trigger_summarization(event, context):
                self.new_message_count = 0  # 重置计数器
    
    async def _trigger_summarization(self,event, context):
        """触发后台总结逻辑。"""
        
        # 1. 把 deque 里的消息拿出来拼成一段文本
        # 注意：这里我们只要文本，不带 user_id，彻底隔绝隐私
        chat_history_text = "\n".join(self.recent_messages)
        
        # 2. 这里将来会调用大模型API，传入 chat_history_text
        # task_prompt = (f"你是一个系统后台的“认知状态提取器。\n"
        #                 f"你的任务是阅读系统刚刚发生的 N 条多用户对话记录，提炼出 AI 助手此刻的“心境、刚刚聊了什么或整体氛围”，并浓缩成一，两句话。\n"
        #                 f"【绝对规则】（违反将导致系统崩溃）：\n"
        #                 f"1. 视角限制：只能描述“系统/AI助手”刚刚经历了什么，不要复述用户说了什么。\n"
        #                 f"2. 格式要求：输出必须是一句简短的、以第一人称或客观状态描述的中文句子，最多不超过 50 个字。严禁输出“摘要如下”、“我现在的状态是”等任何废话。\n"
        #                 f"以下是你最近与多位用户的聊天记录。assistant表示AI助手的回复，user表示用户的提问：\n"
        #                 f"{chat_history_text}\n"
        #                 )
        try:
            summary = await self.send_prompt(event, context, extra_prompt=chat_history_text)
        except Exception as e:
            logger.warning(f"Global observer summarization failed: {e}")
            return False

        if not str(summary).strip():
            return False

        self.current_state = f"<recent_chat_summary>{summary.strip()}</recent_chat_summary>"
        return True

    async def send_prompt(self, event, context, extra_prompt=""):
        # provider_id = await self.context.get_current_chat_provider_id(uid)
        # logger.info(f"uid:{uid}")

        #获取人格
        # system_prompt = await self.get_persona_system_prompt(uid)
        # person_prompt = await self.context.persona_manager.get_default_persona_v3(uid)
        # if not person_prompt:
        #     person_prompt = self.context.provider_manager.selected_default_persona["prompt"]

        #发送信息到llm
        sys_msg = (f"你是一个系统后台的“认知状态提取器。\n"
                        f"你的任务是阅读系统刚刚发生的 N 条多用户对话记录，提炼出 AI 助手此刻的“心境、刚刚聊了什么或整体氛围”，并浓缩成一，两句话。\n"
                        f"【绝对规则】（违反将导致系统崩溃）：\n"
                        f"1. 视角限制：只能描述“系统/AI助手”刚刚经历了什么，不要复述用户说了什么。\n"
                        f"2. 格式要求：输出必须是一句简短的、以第一人称或客观状态描述的中文句子，最多不超过 50 个字。严禁输出“摘要如下”、“我现在的状态是”等任何废话。\n"
                        f"3. 再不泄露用户敏感信息的情况下，你需要尽可能提炼出自己与具体的用户做了什么，让你能知道自己刚刚与哪些用户聊了什么，比如xxx在和我聊天气情况。\n"
                        f"以下是你最近与多位用户的聊天记录。assistant表示AI助手的回复，user表示用户的提问：\n"
                    )
        provider = context.get_using_provider()
        llm_resp = await provider.text_chat(
                prompt=extra_prompt,
                session_id=None,
                contexts="",
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
        logger.info(f"Recent messages: {list(self.recent_messages)}")

@register("LivelyState", "兔子", "这是一个维护全局身体状态、情绪惯性与长期身体档案的状态记忆插件，让角色在不同对话上下文中依然保持统一反应与连续状态。", "v1.2.0")
class LivelyState(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.fast_state_cooldown_sec = max(0, int(config.get("fast_state_cooldown_sec", 300)))
        self.body_sheet_cooldown_sec = max(0, int(config.get("body_sheet_cooldown_sec", 1800)))
        self.global_state = CharacterState(
            update_interval_sec=config.get("auto_update_interval_sec", 300),
            active_state_timeout_sec=config.get("active_state_timeout_sec", 1800),
        )
        self.global_observer = GlobalObserver(max_size=config.get("queue_max_size", 50), trigger_threshold=config.get("trigger_threshold", 20))

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    def _format_structured_state_block(self, data: Dict[str, Any], compact: bool = False) -> str:
        """把结构化状态转成 JSON 文本。

        `compact=True` 用于 prompt 注入，尽量缩短 token；
        默认格式仍保留给 `/state_check` 做可读展示。
        """
        if compact:
            return json.dumps(data or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return json.dumps(data or {}, ensure_ascii=False, indent=2, sort_keys=True)

    def _to_public_state(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """过滤内部冷却元数据，避免直接暴露给用户。"""
        return {
            key: value
            for key, value in (state or {}).items()
            if not str(key).startswith("_")
        }

    def _build_state_style_rules(self, state_info: Dict[str, Any]) -> str:
        """把状态压成少量高收益的回复约束。"""
        energy_level = state_info.get("energy_level", 100)
        thirst = state_info.get("thirst", 0)
        physical_state = self.global_state.normalize_physical_state(state_info.get("physical_state", "Idle"))
        emotion = state_info.get("emotion", "Normal")
        body_sheet = self.global_state.normalize_body_sheet(state_info.get("Body_Sheet", {}))
        history = self.global_state.normalize_history(state_info.get("History", {}))
        state_meta = self.global_state.get_state_meta(physical_state)

        style_rules = [
            f"- 场景、动作、地点必须符合 {physical_state}（{state_meta['label']}）；若下一句会冲突，先调 apply_state_transition。",
            f"- 情绪保持 {emotion} 的惯性，不要突然反向跳变。",
        ]

        if energy_level < 30:
            style_rules.append("- 体力低：语气和动作都应明显疲惫。")
        elif energy_level < 60:
            style_rules.append("- 体力中低：可带轻微疲劳感。")

        if thirst > 70:
            style_rules.append("- thirst 高：可轻微流露较强渴求，但不要盖过主场景。")
        elif thirst > 40:
            style_rules.append("- thirst 中等：偶尔流露即可。")

        if body_sheet:
            style_rules.append("- 外观和部位描写必须符合 Body_Sheet。")

        if history:
            style_rules.append("- 提及累计经历时必须符合 History。")

        return "\n".join(style_rules)

    def _build_persistent_profile_prompt(self, state_info: Dict[str, Any]) -> str:
        """构建精简的长期事实区块。"""
        body_sheet = self.global_state.normalize_body_sheet(state_info.get("Body_Sheet", {}))
        history = self.global_state.normalize_history(state_info.get("History", {}))

        profile_lines: List[str] = []
        if body_sheet:
            profile_lines.append(f"body_sheet={self._format_structured_state_block(body_sheet, compact=True)}")
        if history:
            profile_lines.append(f"history={self._format_structured_state_block(history, compact=True)}")

        if not profile_lines:
            return ""

        return (
            "<persistent_facts>\n"
            + "\n".join(profile_lines)
            + "\n</persistent_facts>\n"
        )

    def _build_global_state_system_prompt(self, uid: str, state_info: Dict[str, Any]) -> str:
        """构建更短的高优先级全局状态约束。"""
        target_id = state_info.get("target_id", "none")
        time_elapsed = max(0.0, time.time() - state_info.get("LastUpdateTime", time.time()))
        physical_state = self.global_state.normalize_physical_state(state_info.get("physical_state", "Idle"))
        state_meta = self.global_state.get_state_meta(physical_state)
        state_snapshot = {
            "physical_state": physical_state,
            "state_label": state_meta["label"],
            "emotion": state_info.get("emotion", "Normal"),
            "energy": state_info.get("energy_level", 100),
            "thirst": state_info.get("thirst", 0),
            "elapsed_sec": round(time_elapsed, 1),
        }
        if target_id != "none":
            state_snapshot["target_id"] = target_id

        return (
            "[GLOBAL_STATE MUST OBEY]\n"
            "- 以下状态是跨会话唯一事实；recent_global_context 仅供参考。\n"
            "- 若你的下一句会与状态冲突，先调用 apply_state_transition。\n"
            f"state={self._format_structured_state_block(state_snapshot, compact=True)}\n"
            f"{self._build_persistent_profile_prompt(state_info)}"
            "rules:\n"
            f"{self._build_state_style_rules(state_info)}\n"
        )

    def _build_recent_context_prompt(self) -> str:
        """构建低优先级的最近对话观察摘要。

        这里刻意只把它作为 reference block，避免“刚刚聊了什么”反过来改写身体状态，
        否则跨会话时容易出现气氛一致但人物状态漂移的问题。
        """
        if not self.global_observer.current_state:
            return ""

        return (
            "<recent_global_context>\n"
            "This block is reference-only. If it conflicts with GLOBAL STATE AUTHORITY, obey GLOBAL STATE AUTHORITY.\n"
            f"{self.global_observer.current_state}\n"
            "</recent_global_context>"
        )

    @filter.llm_tool(name="apply_state_transition") 
    async def apply_state_transition(self, event: AstrMessageEvent, 
                                emotion: Optional[str] = None,
                                energy_level: Optional[int] = None,
                                thirst: Optional[int] = None,
                                physical_state: Optional[str] = None,
                                update_reason: Optional[str] = None,
                                target_id: Optional[str] = None,
                                body_sheet_updates: Optional[str] = None,
                                history_delta: Optional[str] = None,
                                ) -> MessageEventResult:

        '''更新当前角色状态（持久化）。

        使用建议（给 LLM 的决策规则）：
        - 【核心原则：抓大放小】只有当发生重大场景转移（如出门/回家）、大段活动切换（如从工作切到睡觉、从日常聊天切到出门运动）或剧烈情绪/能量变化时，才调用 apply_state_transition。
        - 【调用冷却】快状态更新默认存在 300 秒硬冷却；Body_Sheet 默认存在 1800 秒硬冷却
        - 【禁止频繁调用】同一场景内的连续微小动作（如倒水、换个姿势躺着、脱衣服洗澡、关灯等），禁止调用工具；直接在文本回复的动作描写中体现即可。
        - 【判定冲突标准】只有当你准备回复的内容与当前状态存在根本性矛盾（例如当前状态是“在外面跑步”，但你要回复“在床上睡觉”）时，才必须先调用工具。细微姿势或交互改变不算冲突。
        - 距离上次更新已过较长时间，且当前活动按常理应自然结束或转场时调用。
        - `emotion / energy_level / thirst / physical_state / target_id` 属于快状态，用来更新当前情绪、体力、欲望和身体状态。
        - `body_sheet_updates` 属于长期身体档案的局部更新，只在明确设定、持久性身体变化或需要补录长期事实时使用。
        - `history_delta` 属于历史计数的增量更新，只在某个事件已经真实发生后再增加对应计数。
        - 普通动作、临时姿势、瞬时感受不要写进 `body_sheet_updates`；普通口头描述、想象、铺垫也不要改 `history_delta`。
        - `history_delta` 只能更新当前已注册的 History 键名；
        - 如果只需要补录 Body_Sheet 或 History，而不需要切换当前 physical_state，也可以单独调用本工具，并填写 `update_reason` 说明原因。
        - 由于工具参数类型限制，`body_sheet_updates` 和 `history_delta` 必须传 JSON 字符串，而不是嵌套对象。

        支持部分更新：只传入发生变化的字段即可，未传入字段会沿用旧值。

        推荐格式示例：
        - 更新快状态：`{"physical_state": "Resting", "energy_level": 42, "update_reason": "运动结束后开始休息"}`
        - 更新身体档案：`{"body_sheet_updates": "{\"Breasts\": {\"Status\": \"轻微发胀\"}}", "update_reason": "补录持续性身体变化"}`
        - 更新历史计数：`{"history_delta": "{\"Orgasm_Count\": 1}", "update_reason": "该事件刚刚实际发生"}`

        Args:
            emotion (str, optional): 情绪状态。
            energy_level (int, optional): 体力值，范围 0-100。
            thirst (int, optional): 欲望值，范围 0-100。值越高表示角色当前欲望越强烈。
            physical_state (str, optional): 物理/行为状态。推荐只使用以下规范值：
                Idle, Resting, Sleeping, Working, Exercising, Traveling, Socializing。
                也支持自然语言输入，但插件会先归一化为规范状态再保存。
            update_reason (str, optional): 状态更新原因。
            target_id (str, optional): 当前关注对象 ID。它只表示“此刻主要在和谁互动”，
                不代表存在另一套独立状态；身体状态和情绪状态始终是全局唯一的。
            body_sheet_updates (str, optional): 长期身体档案的局部更新，需传 JSON 字符串对象。
                只在明确设定、持久性变化或需要补录身体事实时使用；普通动作描写不要写进这里。
            history_delta (str, optional): 历史计数的增量更新，需传 JSON 字符串对象。
                这里传入的是“本次增加多少”，不是最新总数，例如 `{"1_Count": 1}` 表示该计数加 1。
        '''
        

        cur_state = {
            "LastUpdateTime": time.time(),
            "emotion": emotion,
            "energy_level": energy_level,
            "thirst": thirst,
            "physical_state": physical_state,
            "update_reason": update_reason,
            "target_id": target_id,
            "body_sheet_updates": body_sheet_updates,
            "history_delta": history_delta,
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
        pretty_state = self._format_structured_state_block(self._to_public_state(self.global_state.get_whole_state()))
        await self.context.send_message(event.unified_msg_origin, MessageChain().message(f"当前状态信息：\n{pretty_state}"))
        event.stop_event()

    @filter.command("state_del")
    async def state_del(self, event: AstrMessageEvent) -> MessageEventResult:
        self.global_state.delete()
        await self.context.send_message(event.unified_msg_origin, MessageChain().message("状态已重置。"))
        event.stop_event()

    @filter.on_llm_request()
    async def add_state(self, event: AstrMessageEvent, req: ProviderRequest) -> MessageEventResult:
        uid = event.unified_msg_origin
        user_name = event.get_sender_name()
        # 保存原始提示，后面会把“全局状态事实”叠加到 system prompt，
        # 把“最近聊了什么”的观察摘要放回普通 prompt，明确主次关系。
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
        if len(history) >=2:
            logger.info(f"上一个会话历史: {(history[-1])}")
            if history[-1]["role"] == "assistant":
                last_reply = [msg.get("text", "")
                            for msg in history[-1].get("content", [])
                            if isinstance(msg, Dict) and msg.get("type") == "text"]
                last_reply_text = f"[role:assistant,reply to user:name:{user_name}]: " + "\n".join(last_reply)
                await self.global_observer.add_message(last_reply_text, event, self.context)
            elif history[-2]["role"] == "assistant":
                last_reply = [msg.get("text", "")
                            for msg in history[-2].get("content", [])
                            if isinstance(msg, Dict) and msg.get("type") == "text"]
                last_reply_text = f"[role:assistant,reply to user:name:{user_name}]: " + "\n".join(last_reply)
                await self.global_observer.add_message(last_reply_text, event, self.context)
            else:
                logger.warning("无法找到上一条助手回复，不更新状态观察器。")

        message_str = event.message_str
        await self.global_observer.add_message(f"[role:user,name:{user_name}]: {message_str}", event, self.context)
        # logger.info(f"Added message to observer: [role:user,uid:{uid}]: {message_str}")
        self.global_observer.view_recent_messages()
        state_info = self.global_state.get_whole_state()
        global_state_prompt = self._build_global_state_system_prompt(uid, state_info)
        req.system_prompt = "\n\n".join(
            part for part in [(req.system_prompt or "").strip(), global_state_prompt.strip()] if part
        )

        prompt_sections = []
        recent_context_prompt = self._build_recent_context_prompt()
        if recent_context_prompt:
            prompt_sections.append(recent_context_prompt)
        prompt_sections.append(ori_prompt)
        req.prompt = "\n".join(prompt_sections)
        # logger.info(f"当前系统提示词——LivelyState: {req.system_prompt}")

    def _parse_tool_json_object_arg(self, raw_value: Any, field_name: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """把 llm_tool 里的 JSON 字符串参数解析成对象。

        AstrBot 的工具注册不接受这两个字段的嵌套 Dict 类型，
        所以这里改为 string 入参，再在函数内部做一次宽松解析。
        """
        if raw_value is None:
            return None, None

        if isinstance(raw_value, dict):
            return raw_value, None

        if not isinstance(raw_value, str):
            return None, f"{field_name} 必须是 JSON 字符串对象"

        stripped = raw_value.strip()
        if not stripped:
            return None, None

        json_text = self._extract_json_block(stripped) or stripped

        try:
            parsed_value = json_repair.loads(json_text)
        except Exception as e:
            return None, f"{field_name} 不是合法的 JSON 对象字符串: {e}"

        if not isinstance(parsed_value, dict):
            return None, f"{field_name} 必须解析为对象"

        return parsed_value, None

    def _handle_apply(self, event, payload: dict) -> str:
        if not payload:
            return "请提供大模型返回的 Dict 内容。"

        if not isinstance(payload, dict):
            return "状态更新数据必须是对象"

        uid = event.unified_msg_origin
        current_state = self.global_state.get_whole_state()

        updatable_fields = [
            "emotion",
            "energy_level",
            "thirst",
            "physical_state",
            "update_reason",
            "target_id",
            "body_sheet_updates",
            "history_delta",
        ]
        required_numeric_fields = ["energy_level", "thirst"]

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

        raw_body_sheet_updates = payload.get("body_sheet_updates")
        body_sheet_updates, body_sheet_parse_error = self._parse_tool_json_object_arg(
            raw_body_sheet_updates,
            "body_sheet_updates",
        )
        if body_sheet_parse_error:
            return f"Update Failed：{body_sheet_parse_error}"

        normalized_body_sheet_updates = self.global_state.normalize_body_sheet(body_sheet_updates)
        if isinstance(body_sheet_updates, dict) and body_sheet_updates and not normalized_body_sheet_updates:
            return "Update Failed：body_sheet_updates 至少需要包含一个合法的部位与属性描述"

        raw_history_delta = payload.get("history_delta")
        history_delta, history_delta_parse_error = self._parse_tool_json_object_arg(
            raw_history_delta,
            "history_delta",
        )
        if history_delta_parse_error:
            return f"Update Failed：{history_delta_parse_error}"

        normalized_history_delta = self.global_state.normalize_history(history_delta)
        if isinstance(history_delta, dict) and history_delta and not normalized_history_delta:
            return "Update Failed：history_delta 至少需要包含一个合法的非负整数增量"

        current_history = self.global_state.normalize_history(current_state.get("History", {}))
        unknown_history_keys = sorted(set(normalized_history_delta.keys()) - set(current_history.keys()))
        if unknown_history_keys:
            return (
                "Update Failed：history_delta 包含未注册的计数字段 "
                f"{', '.join(unknown_history_keys)}；请先在 state_profile_template.json 中定义它们"
            )

        has_scalar_update = any(payload.get(field_name) is not None for field_name in [
            "emotion", "energy_level", "thirst", "physical_state", "update_reason", "target_id"
        ])
        if not has_scalar_update and not normalized_body_sheet_updates and not normalized_history_delta:
            return "Update Failed：至少需要提供一个可更新字段"

        normalized_physical_state = current_state.get("physical_state", "Idle")
        requested_physical_state = payload.get("physical_state")
        if requested_physical_state is not None:
            normalized_physical_state, is_recognized = self.global_state.resolve_physical_state(
                requested_physical_state,
                fallback=current_state.get("physical_state", "Idle"),
            )
            if not is_recognized:
                available_states = ", ".join(self.global_state.list_available_states())
                return f"Update Failed：physical_state 非法，可用规范状态：{available_states}"

            current_physical_state = self.global_state.normalize_physical_state(current_state.get("physical_state", "Idle"))
            if not self.global_state.is_transition_allowed(current_physical_state, normalized_physical_state):
                allowed_states = ", ".join(self.global_state.get_allowed_transitions(current_physical_state))
                return (
                    f"Update Failed：不允许从 {current_physical_state} 直接切换到 {normalized_physical_state}，"
                    f"当前允许转移到：{allowed_states}"
                )

        reason = _safe_text(payload.get("update_reason"), current_state.get("update_reason", "无理由说明。"))
        target_id = _safe_text(payload.get("target_id"), current_state.get("target_id", "none"))
        current_energy_level = _clamp_int(current_state.get("energy_level"), 100)
        current_thirst = _clamp_int(current_state.get("thirst"), 0)
        next_emotion = _safe_text(payload.get("emotion"), current_state.get("emotion", "Normal"))
        next_energy_level = _clamp_int(payload.get("energy_level"), current_energy_level)
        next_thirst = _clamp_int(payload.get("thirst"), current_thirst)
        next_target_id = target_id
        current_body_sheet = self.global_state.normalize_body_sheet(current_state.get("Body_Sheet", {}))
        merged_body_sheet = self.global_state.merge_body_sheet(current_body_sheet, normalized_body_sheet_updates)
        merged_history = self.global_state.apply_history_delta(current_history, normalized_history_delta)

        has_effective_fast_state_change = any([
            next_emotion != current_state.get("emotion", "Normal"),
            next_energy_level != current_energy_level,
            next_thirst != current_thirst,
            normalized_physical_state != current_state.get("physical_state", "Idle"),
            next_target_id != current_state.get("target_id", "none"),
        ])
        has_effective_body_sheet_change = merged_body_sheet != current_body_sheet
        has_effective_history_change = merged_history != current_history

        if not has_effective_fast_state_change and not has_effective_body_sheet_change and not has_effective_history_change:
            return "Update Failed：未检测到实际状态变化"

        now = time.time()
        if has_effective_fast_state_change and self.fast_state_cooldown_sec > 0:
            elapsed_fast_update = max(0.0, now - float(current_state.get("_last_fast_state_update_time", 0.0)))
            if elapsed_fast_update < self.fast_state_cooldown_sec:
                return (
                    "Update Failed：快状态更新冷却中，"
                    f"还需等待 {self.fast_state_cooldown_sec - elapsed_fast_update:.1f} 秒"
                )

        if has_effective_body_sheet_change and self.body_sheet_cooldown_sec > 0:
            elapsed_body_sheet_update = max(0.0, now - float(current_state.get("_last_body_sheet_update_time", 0.0)))
            if elapsed_body_sheet_update < self.body_sheet_cooldown_sec:
                return (
                    "Update Failed：Body_Sheet 更新冷却中，"
                    f"还需等待 {self.body_sheet_cooldown_sec - elapsed_body_sheet_update:.1f} 秒"
                )
        
        new_state_data = {
            # 慢变化字段不应该顺手把身体状态的时间轴重置掉；
            # 只有快状态真的发生变化时才刷新 LastUpdateTime。
            "LastUpdateTime": now if has_effective_fast_state_change else current_state.get("LastUpdateTime", now),
            "emotion": next_emotion,
            "energy_level": next_energy_level,
            "thirst": next_thirst,
            "physical_state": normalized_physical_state,
            "update_reason": reason,
            "target_id": next_target_id,
            "Body_Sheet": merged_body_sheet,
            "History": merged_history,
            "_last_fast_state_update_time": now if has_effective_fast_state_change else float(current_state.get("_last_fast_state_update_time", 0.0)),
            "_last_body_sheet_update_time": now if has_effective_body_sheet_change else float(current_state.get("_last_body_sheet_update_time", 0.0)),
        }
            # Ensure required fields exist and are normalized
            
            # Persist state
        self.global_state.save(new_state_data)
        logger.info(f"查看新数据：{new_state_data}")
        report = f"状态已更新，原因：{reason}，状态：{self._to_public_state(self.global_state.get_whole_state(enable_update=False))}"
        
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