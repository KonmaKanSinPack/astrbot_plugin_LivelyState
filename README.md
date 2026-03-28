***

# astrbot_plugin_LivelyState

`LivelyState` 是一个 AstrBot 状态机与记忆插件，用于给角色增加“持续状态记忆”（如情绪、体力、当前行为）以及“全局短期记忆感知”。它会在每次 LLM 请求时将状态和记忆注入提示词，约束角色回复更连贯、更符合“当下情境”。

* 她不再每句“重开存档”，而是带着上一刻的心情继续和你说话。
* **[全新升级]** 她现在拥有了**“全局观察者”**能力！能自动感知群聊氛围，记住刚刚发生了什么，避免跨用户聊天的割裂感。
* 有情绪起伏、有体力变化、有隐秘的渴求、有当下的状态——这次，她真的“活”起来了。
* 不是台词机，而是会累、会心动、会感知环境的同伴，陪你把故事聊下去。

## ✨ 功能特性

-   **持久化全局状态**：角色状态自动保存至本地文件（默认保存为 `global_state.json`），重启不丢失。
-   **全局观察者（Global Observer）**：自动监听最近的对话记录（双端队列），每攒够一定条数自动调用大模型进行“状态总结”，生成角色的短期记忆与环境感知。
-   **无缝状态注入**：在 LLM 请求阶段自动将“环境总结摘要”与“当前物理/心理状态”追加为系统提示词与用户提示词。
-   **LLM 自主状态更新**：提供 `apply_state_transition` 工具供大模型在场景转换、体力消耗时自主决定更新状态（支持增量/部分更新）。
-   **强大的容错与校验**：内置严格的数据清洗机制，自动修复 LLM 返回的残缺 JSON，限制数值范围（0-100），防止系统崩溃。
-   **新增快捷命令**：提供 `state_check`（查看状态）与 `state_del`（一键重置状态）指令。

## 📊 当前状态字段

底层数据结构已全面升级，当前维护以下核心字段：

-   `LastUpdateTime`：最近更新时间戳（秒级）
-   `emotion`：情绪状态（如 Normal, Happy, Tired）
-   `energy_level`：体力值（0-100 整数，<30 时角色会表现出疲惫）
-   `thirst`：欲望/渴求值（0-100 整数，影响角色互动的迫切感）
-   `physical_state`：当前物理/行为状态（如 Idle, Sleeping, Running）
-   `update_reason`：状态更新原因（由 LLM 填写）
-   `target_id`：状态关联目标（`none` 表示全局状态，其他为特定用户 ID）

默认初始值示例：

```json
{
  "LastUpdateTime": 1711500000.0,
  "emotion": "Normal",
  "energy_level": 100,
  "thirst": 0,
  "physical_state": "Idle",
  "update_reason": "Initial state",
  "target_id": "none"
}
```

> *实际运行时 `LastUpdateTime` 会动态写入真实时间戳。*

## 📦 安装

1. 将插件目录放入 AstrBot 的 `data/plugins/` 目录中。
2. 安装必要的依赖（请确保 `requirements.txt` 中包含 `json_repair` 等所需库）：

```bash
pip install -r requirements.txt
# 如果没有 requirements.txt，请手动执行：pip install pydantic json_repair
```

3. 在 AstrBot 管理面板或配置文件中启用该插件，并重启框架。

## 🚀 使用方法

### 1) 查看当前状态

在聊天中向机器人发送命令：

```text
/state_check
```


### 2) 重置状态

如果角色状态卡死或你想让她“重新开始”，发送命令：

```text
/state_del
```

插件将删除本地状态文件并恢复至默认初始值。

### 3) 状态自动流转（由 LLM 驱动）

插件已向大模型注册了原生函数调用工具：**`apply_state_transition`**。

你无需手动调用。大模型已配置了严格的**【抓大放小】**决策规则，只有在发生“重大场景转移”、“大段活动切换”或“剧烈情绪/能量变化”时，LLM 才会自主调用该工具更新底层数据。普通的小动作会在文本中直接表现，不会频繁刷新状态。

## 🏷️ 元信息

-   **名称**：`LivelyState`
-   **版本**：`v1.1.0`
-   **作者**：`兔子`
-   **仓库**：[https://github.com/KonmaKanSinPack/astrbot_plugin_LivelyState](https://github.com/KonmaKanSinPack/astrbot_plugin_LivelyState)

## 📄 许可证

本项目使用 **AGPL-3.0** 开源许可证（详见 `LICENSE` 文件）。

## 🔗 参考与鸣谢

-   AstrBot 官方文档：[https://astrbot.app](https://astrbot.app)