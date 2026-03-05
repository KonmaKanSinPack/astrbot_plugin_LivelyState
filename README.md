# astrbot_plugin_LivelyState

`LivelyState` 是一个 AstrBot 状态机插件，用于给角色增加“持续状态记忆”（如情绪、体力、口渴、当前行为），并在每次 LLM 请求时把状态注入系统提示词，约束角色回复更连贯、更符合“正在做什么”。
* 她不再每句“重开存档”，而是带着上一刻的心情继续和你说话。
* 有情绪起伏、有体力变化、有当下状态——这次，她真的“活”起来了。
* 不是台词机，而是会累、会渴、会心动的同伴，陪你把故事聊下去。

## 功能特性

- 持久化全局状态（默认保存为 `global_state.json`）
- 在 LLM 请求阶段自动追加状态约束提示词
- 提供 `change_current_state` 工具供模型按需更新状态
- 提供命令 `state_check` 查看当前状态
- 内置字段校验（必填字段、数值范围 0-100）

## 当前状态字段

- `LastUpdateTime`：最近更新时间戳
- `Emotion`：情绪
- `Energy`：体力（0-100）
- `Thirst`：口渴度（0-100）
- `State`：当前行为状态
- `update_reason`：状态更新原因
- `target_id`：状态关联目标（`none` 表示全局）

默认初始值：

```json
{
	"LastUpdateTime": 0,
	"Emotion": "Normal",
	"Energy": 100,
	"Thirst": 0,
	"State": "Idle",
	"update_reason": "Initial state",
	"target_id": "none"
}
```

> 实际运行时 `LastUpdateTime` 会写入当前时间戳。

## 安装

1. 将插件目录放入 AstrBot 插件目录。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 在 AstrBot 中启用插件。

## 使用方法

### 1) 查看状态

发送命令：

```text
state_check
```

插件会返回当前完整状态。

### 2) 状态更新（由 LLM 工具调用）

插件注册了 LLM 工具：

- `change_current_state`

该工具需要以下字段：

- 文本字段：`Emotion`、`State`、`update_reason`、`target_id`
- 数值字段：`Energy`、`Thirst`（必须是 0-100 的整数）

当字段缺失或不合法时会返回 `Update Failed` 信息。

## 元信息

- 名称：`LivelyState`
- 版本：`v0.0.1`
- 作者：`兔子`
- 仓库：<https://github.com/KonmaKanSinPack/astrbot_plugin_LivelyState>

## 许可证

本项目使用 AGPL-3.0（详见 `LICENSE`）。

## 参考

- AstrBot 文档：<https://astrbot.app>
