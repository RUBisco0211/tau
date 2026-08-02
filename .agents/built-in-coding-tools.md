# Tau Built-in Coding Tools

这份笔记说明 `src/tau_coding` 包里默认内置的 agent tools。实现集中在 `src/tau_coding/tools.py`，由 `create_coding_tools(...)` 组装。

## 默认工具集

`create_coding_tools(...)` 默认返回 4 个工具，顺序固定：

```python
[
    create_read_tool(...),
    create_write_tool(...),
    create_edit_tool(...),
    create_bash_tool(...),
]
```

也就是：

```text
read
write
edit
bash
```

这些工具属于 `tau_coding` 应用层，而不是 `tau_agent` 核心层。`tau_agent` 只认识通用的 `AgentTool` / `AgentToolResult` 协议。

## 装配位置

`CodingSession.load(...)` 会创建默认工具：

```text
src/tau_coding/session.py
  -> create_coding_tools(cwd=..., shell_command_prefix=..., image_support=...)
```

如果 `CodingSessionConfig.tools` 显式传入自定义工具，就不会使用默认工具集。

默认工具创建后还会经过 extension runtime 组合：

```text
base_tools = create_coding_tools(...)
tools = extension_runtime.compose_tools(base_tools)
```

因此实际暴露给模型的工具可能是：

```text
内置工具 + extension tools / extension wrappers
```

## 工具定义模型

`tau_coding.tools` 里先定义较丰富的 `ToolDefinition`：

```python
ToolDefinition(
    name=...,
    description=...,
    prompt_snippet=...,
    prompt_guidelines=...,
    input_schema=...,
    executor=...,
)
```

然后通过 `ToolDefinition.to_agent_tool()` 转成核心 loop 使用的 `AgentTool`。

`AgentTool` 来自 `src/tau_agent/tools.py`，包含：

- `name`
- `description`
- `parameters` / `input_schema`
- `execute_fn`
- prompt metadata
- optional renderer / argument preparer

目前内置工具的 `to_agent_tool()` 会忽略 `on_update`，因此默认 `read/write/edit/bash` 基本都是一次性返回最终结果，不主动产生 `ToolExecutionUpdateEvent`。

## `read`

用途：读取文件。

实现入口：

```text
create_read_tool_definition(...)
create_read_tool(...)
```

参数 schema：

```json
{
  "type": "object",
  "properties": {
    "path": { "type": "string" },
    "offset": { "type": "integer" },
    "limit": { "type": "integer" }
  },
  "required": ["path"]
}
```

行为：

- `path` 可以是绝对路径，也可以是相对 `cwd` 的路径。
- 不能读取不存在的路径。
- 不能把目录当文件读取。
- 文本文件按 UTF-8 解码，并把 CRLF/CR 规范化成 LF。
- `offset` 是 1-indexed 行号；`offset=0` 会当作从文件开头读取。
- `limit` 是最多读取的行数。
- 文本输出最多 `DEFAULT_MAX_OUTPUT_LINES = 2000` 行或 `DEFAULT_MAX_OUTPUT_BYTES = 50KB`。
- 如果输出被截断，会提示下一次继续读取应使用的 `offset`。

图片行为：

- 支持从文件内容识别图片，不只依赖扩展名。
- 支持 `jpg`、`png`、`gif`、`webp`、`bmp`。
- 支持图片会返回 `ImageContent`，供 vision-capable model 使用。
- `bmp` 等可能会被转换为 `png`。
- 超尺寸图片会被缩放或省略。
- 如果当前模型不支持图片输入，会返回明确的文本说明，提醒不要推断图片内容。

返回内容：

- 文本文件返回 `TextContent`。
- 图片文件可能返回 `TextContent + ImageContent`。
- `details` 包含 resolved path、truncation 信息或图片处理信息。

适用场景：

```text
查看代码、配置、文档、图片资源。
```

不适合：

```text
读取巨大完整文件；应使用 offset/limit 分段读取。
```

## `write`

用途：创建或完整覆盖文件。

实现入口：

```text
create_write_tool_definition(...)
create_write_tool(...)
```

参数 schema：

```json
{
  "type": "object",
  "properties": {
    "path": { "type": "string" },
    "content": { "type": "string" }
  },
  "required": ["path", "content"]
}
```

行为：

- `path` 可以是绝对路径，也可以是相对 `cwd` 的路径。
- 自动创建父目录。
- 如果目标文件已存在，会完整覆盖。
- 使用 UTF-8 写入。
- 对同一路径使用 async lock，避免同进程内并发 write/edit 交错。

返回内容：

- 文本提示写入成功。
- `details` 包含 resolved path 和写入字符数。

适用场景：

```text
创建新文件、生成完整文件、明确需要整文件重写。
```

不适合：

```text
小范围修改已有文件；这种情况优先使用 edit。
```

## `edit`

用途：对单个文件做精确文本替换。

实现入口：

```text
create_edit_tool_definition(...)
create_edit_tool(...)
```

参数 schema：

```json
{
  "type": "object",
  "properties": {
    "path": { "type": "string" },
    "edits": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "oldText": { "type": "string" },
          "newText": { "type": "string" }
        },
        "required": ["oldText", "newText"],
        "additionalProperties": false
      }
    }
  },
  "required": ["path", "edits"],
  "additionalProperties": false
}
```

行为：

- 只编辑单个 UTF-8 文本文件。
- 文件必须存在，不能是目录。
- 每个 `oldText` 必须非空。
- 每个 `oldText` 必须在原文件中唯一匹配。
- 多个 edit 不能重叠。
- 多个 edit 都是基于原始文件匹配，不是基于前一个 edit 后的中间结果。
- 所有 edit 会先验证，任何一个失败都会保持文件不变。
- 匹配时会规范化为 LF。
- 写回时会恢复原文件主要换行风格。
- UTF-8 BOM 会被保留。
- 对同一路径使用 async lock。

返回内容：

- 文本提示替换成功。
- `details` 包含：
  - `path`
  - `edits`
  - `diff`
  - `patch`
  - `first_changed_line`

适用场景：

```text
对已有文件做小范围、可定位、可验证的修改。
```

使用建议：

- 修改多个分散位置时，优先在一次 `edit` 调用里放多个 edits。
- `oldText` 尽量短，但必须能唯一匹配。
- 如果两个修改靠得很近，合并成一个 edit。
- 不要为了连接远距离修改而包含大段不变内容。

## `bash`

用途：在 session 工作目录下执行 shell 命令。

实现入口：

```text
create_bash_tool_definition(...)
create_bash_tool(...)
```

参数 schema：

```json
{
  "type": "object",
  "properties": {
    "command": { "type": "string" },
    "timeout": { "type": "number" }
  },
  "required": ["command"]
}
```

行为：

- 使用 `asyncio.create_subprocess_shell(...)` 执行命令。
- 工作目录是 session `cwd`。
- stdout 和 stderr 合并返回。
- `timeout` 可选；如果提供，必须大于 0。
- POSIX 下会以新 session 启动进程，超时或取消时杀掉整个 process group。
- 非 POSIX 平台退化为杀掉直接子进程。
- 输出按尾部截断，最多 `DEFAULT_MAX_OUTPUT_LINES = 2000` 行或 `DEFAULT_MAX_OUTPUT_BYTES = 50KB`。
- 如果输出被截断，完整输出会写到临时文件，路径放在 `details.full_output_path`。
- 可配置 `shell_command_prefix`，用于给每条命令前置一段 shell setup。

返回内容：

- 命令输出文本；无输出时返回 `(no output)`。
- 如果非零退出、超时或取消，会在输出后追加状态说明。
- `details` 包含：
  - `command`
  - `exit_code`
  - `timed_out`
  - `cancelled`
  - `duration_seconds`
  - `truncation`
  - `full_output_path`
  - `shell_command_prefix_applied`

适用场景：

```text
运行测试、列目录、搜索、执行项目命令、查看 git 状态等。
```

注意：

- 当前内置 `bash` 不流式返回 stdout；它等待进程结束后一次性返回结果。
- 因此它通常只产生 `ToolExecutionStartEvent` 和 `ToolExecutionEndEvent`，不会产生实时 stdout 的 `ToolExecutionUpdateEvent`。

## 路径与输出限制

默认工具共享这些限制：

```text
DEFAULT_MAX_OUTPUT_BYTES = 50 * 1024
DEFAULT_MAX_OUTPUT_LINES = 2000
```

路径解析一般通过 helper 将相对路径解析到 session `cwd`。

`read` 使用头部截断，适合从文件开头阅读并通过 offset 继续。

`bash` 使用尾部截断，适合查看命令最后的报错、测试总结和日志尾部。

## 与 agent loop 的关系

模型生成 tool call 后，`run_agent_loop(...)` 会：

```text
ToolExecutionStartEvent
  -> AgentTool.execute(...)
ToolExecutionEndEvent
  -> ToolResultMessage
MessageStartEvent(toolResult)
MessageEndEvent(toolResult)
```

内置工具返回的 `AgentToolResult` 会被包装成 `ToolResultMessage` 追加到 transcript。下一次 turn 调用模型时，模型就能看到工具结果。

## 维护时快速定位

- 默认工具集合：`create_coding_tools(...)`
- 读取文件/图片：`create_read_tool_definition(...)`
- 创建或覆盖文件：`create_write_tool_definition(...)`
- 精确替换：`create_edit_tool_definition(...)`
- 执行 shell：`create_bash_tool_definition(...)`
- 通用 tool 协议：`src/tau_agent/tools.py`
- 行为测试：`tests/test_coding_tools.py`

