# Tau Agent Core Workflow

这份笔记说明 Tau 这个 agent 项目的核心工作流定义在哪里，以及一次用户输入如何从 CLI/TUI 入口流到模型、工具、事件和持久化会话。

## 代码定位

核心工作流分三层：

1. `src/tau_agent/loop.py`
   - 定义最底层的纯 agent loop：`run_agent_loop(...)`。
   - 只认识 provider、model、system prompt、messages、tools 和事件。
   - 不依赖 CLI、Textual、Rich、项目路径、session 文件。

2. `src/tau_agent/harness.py`
   - 定义状态化封装：`AgentHarness`。
   - 保存内存 transcript，管理取消、监听器、运行中排队消息。
   - 对外提供 `prompt(...)`、`prompt_message(...)`、`continue_(...)`。

3. `src/tau_coding/session.py`
   - 定义编码代理环境：`CodingSession`。
   - 装配系统提示词、项目上下文、skills、extensions、coding tools、provider、session storage。
   - 调用 `AgentHarness`，并把完成的消息持久化为 JSONL session tree。

入口层在：

- `src/tau_coding/cli.py`
  - print mode 通过 `run_print_mode(...)` 创建 `CodingSession`，然后 `async for event in session.prompt(prompt)` 渲染事件。
- `src/tau_coding/tui/app.py`
  - TUI 提交输入后进入 `_submit_prompt(...)` / `_run_prompt(...)`，同样消费 `session.prompt(...)` 的事件流。

## 一次请求的完整流程

```text
用户输入
  |
  v
CLI run_print_mode(...) 或 TUI _submit_prompt(...)
  |
  v
CodingSession.prompt(...)
  |
  |-- extension input hooks
  |-- prompt template / skill command expansion
  |-- auto compaction check
  |
  v
AgentHarness.prompt_message(...)
  |
  v
run_agent_loop(...)
  |
  |-- provider.stream_response(...)
  |-- MessageStart/Update/End events
  |-- execute tool calls
  |-- append ToolResultMessage
  |-- repeat while assistant keeps requesting tools
  |
  v
AgentEndEvent
  |
  v
CodingSession persists completed messages
  |
  v
CLI renderer / TUI adapter renders CodingSessionEvent stream
```

## 最底层：`run_agent_loop`

`run_agent_loop(...)` 是 Tau 的 agent 心脏。它接收：

- `provider`: 模型提供方，必须实现 `stream_response(...)`。
- `model`: 当前模型名。
- `system`: 系统提示词。
- `messages`: 可变 transcript 列表，loop 会把新消息追加进去。
- `tools`: 可用工具列表。
- `prompts`: 本轮新增的用户消息。
- `max_turns`: 可选最大 turn 数。
- `get_steering_messages` / `get_follow_up_messages`: 运行中追加输入的队列钩子。
- `before_tool_call` / `after_tool_call`: 工具执行前后的拦截钩子。

它的主要循环是：

1. 发出 `AgentStartEvent` 和 `TurnStartEvent`。
2. 把本轮 prompt 追加到 transcript，并发出用户消息的 start/end 事件。
3. 调用 `provider.stream_response(...)`，把 provider 的增量事件转换成 Tau 自己的 agent events。
4. 收到最终 `AssistantMessage` 后，把它追加进 transcript。
5. 如果 assistant 结束原因是 `error` 或 `aborted`，结束整个 run。
6. 如果 assistant 带有 tool calls，逐个执行工具。
7. 每个工具会产生 tool execution events，并最终生成一个 `ToolResultMessage` 追加到 transcript。
8. 结束当前 turn。
9. 如果刚才有工具结果，就用更新后的 transcript 再问一次模型。
10. 如果没有工具结果，但有 follow-up 队列，就开始下一轮；否则发出 `AgentEndEvent`。

这就是“模型请求工具 -> Tau 执行工具 -> 工具结果回灌给模型 -> 模型继续推理”的核心 agent 工作流。

## Provider 流如何变成 Tau 事件

`run_agent_loop(...)` 内部用 `_assistant_events(...)` 包装 provider 流。

provider 层发出的事件来自 `tau_agent.provider_events`，例如：

- assistant start
- text delta / thinking delta / tool-call delta
- assistant done
- assistant error

loop 会把这些转换成 `tau_agent.events` 里的统一事件：

- `MessageStartEvent`
- `MessageUpdateEvent`
- `MessageEndEvent`

前端只需要消费 Tau 统一事件，不需要知道 OpenAI、Anthropic、Google 或其他 provider 的原始流格式。

## 工具执行流程

工具执行在 `src/tau_agent/loop.py` 的 `_execute_tool_call(...)`。

流程是：

1. 发出 `ToolExecutionStartEvent`。
2. 如果配置了 `before_tool_call`，先询问是否阻止这个工具调用。
3. 检查取消信号。
4. 按工具名从 `tool_by_name` 查找 `AgentTool`。
5. 调用工具的 `execute(call.id, call.arguments, signal, on_update)`。
6. 工具可以通过 `on_update(...)` 发出中间结果，loop 转成 `ToolExecutionUpdateEvent`。
7. 工具成功或失败后发出 `ToolExecutionEndEvent`。
8. 把最终工具结果包装为 `ToolResultMessage`，再发出 message start/end 事件。

工具异常会被当作工具边界内的错误处理，转换成 error result，不会直接炸掉整个 agent loop。

## `AgentHarness` 做了什么

`AgentHarness` 是可复用的状态化 agent brain。它在 `run_agent_loop(...)` 外面加了几件事：

- 保存当前 transcript：`self._messages`。
- 确保同一个 harness 同时只能运行一个 agent loop。
- 支持 `cancel()`。
- 支持 event listeners：每个 loop event 都会通知订阅者。
- 支持运行中输入队列：
  - `steer(...)`: 在工具批次/turn 边界插入 steering message。
  - `follow_up(...)`: 当前 run 本来要结束时，接着跑一个后续用户消息。
- 支持 interrupted tool repair：如果 assistant 已经发出 tool call，但用户中断导致没有 tool result，harness 会补一个 `ToolResultMessage(content="Tool call interrupted by user")`，避免之后 provider 拒收 transcript。

所以可以把它理解成：

```text
run_agent_loop = 无状态算法
AgentHarness  = 带 transcript、队列、取消和监听器的 agent 实例
```

## `CodingSession` 做了什么

`CodingSession` 是“coding agent 环境”。它不重新实现 agent loop，而是负责把 Tau 的应用能力装配到 harness 周围：

- 加载 project context 文件，例如 `AGENTS.md`。
- 加载 skills 和 prompt templates。
- 创建默认 coding tools，例如 shell、读写文件、图片读取等。
- 合成 extension tools 和 extension prompt guidelines。
- 构建 system prompt。
- 创建 `AgentHarness`。
- 在 `prompt(...)` 中执行输入 hooks、展开 prompt、自动压缩上下文。
- 消费 harness 事件，并在 `MessageEndEvent` 时持久化完成消息。
- 将 `AgentEndEvent` 包装成 `SessionAgentEndEvent`。
- 在 run 结束后发出 `AgentSettledEvent`。
- 遇到上下文溢出时尝试 compaction，然后自动 retry。

持久化边界在 `CodingSession._persist_messages_since(...)`：

- 每个完成的 message 被写成 `MessageEntry`。
- 每次写入后更新 `LeafEntry`。
- session tree 因此能支持 resume、branch、tree navigation。

## CLI 和 TUI 如何接入

print mode：

```text
tau --print "..."
  -> cli.run_print_mode(...)
  -> CodingSession.load(...)
  -> session.prompt(...)
  -> renderer.render(event)
```

TUI：

```text
用户在输入框提交
  -> TauApp._submit_prompt(...)
  -> 启动 worker 执行 TauApp._run_prompt(...)
  -> async for event in self.session.prompt(...)
  -> adapter.apply(event)
  -> Textual 界面刷新
```

两者共享同一条 `CodingSession.prompt(...) -> AgentHarness -> run_agent_loop(...)` 主路径。差别只在事件最终如何渲染。

## 设计边界

项目的核心边界是：

```text
tau_ai
  provider/model streaming layer

tau_agent
  portable agent loop, messages, events, tools, harness, session primitives

tau_coding
  CLI/TUI coding-agent product layer: resources, commands, tools, sessions, rendering
```

重要原则：

- `tau_agent` 不应该依赖 Textual、Rich、CLI 参数、Tau 配置目录、项目资源加载。
- provider 的原始流应该在 provider layer / loop wrapper 中转换成统一 events。
- 前端只消费 events，不直接耦合 provider chunk。
- coding-specific 行为放在 `tau_coding.session.CodingSession`，不要塞进 `run_agent_loop(...)`。

## 维护时的快速定位

- 想改“模型什么时候继续、工具结果怎么回灌”：看 `src/tau_agent/loop.py`。
- 想改“运行中追加输入、取消、事件监听”：看 `src/tau_agent/harness.py`。
- 想改“项目上下文、skills、系统提示词、默认 coding tools、session 持久化”：看 `src/tau_coding/session.py`。
- 想改 print mode 渲染或启动参数：看 `src/tau_coding/cli.py`。
- 想改 TUI 提交流程或事件渲染：看 `src/tau_coding/tui/app.py` 和 `src/tau_coding/tui/adapter.py`。
- 想确认行为契约：看 `tests/test_agent_loop.py`、`tests/test_agent_harness.py`、`tests/test_coding_session.py`。

