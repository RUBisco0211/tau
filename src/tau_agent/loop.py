"""Pure Pi-compatible provider/tool agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

from tau_agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
)
from tau_agent.provider import CancellationToken, ModelProvider
from tau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
)
from tau_agent.tools import AgentTool, AgentToolResult

BeforeToolCall = Callable[[ToolCall], Awaitable[tuple[bool, str | None]]]
AfterToolCall = Callable[
    [ToolCall, AgentToolResult, bool],
    Awaitable[tuple[AgentToolResult, bool]],
]


# NOTE: 核心 agent loop，本身无状态，需要外部传入历史 messages
async def run_agent_loop(
    *,
    provider: ModelProvider,
    model: str,
    system: str,  # system prompt
    messages: list[AgentMessage],  # 已有的 transcript，即当前 session 已有的历史记录
    tools: list[AgentTool],
    prompts: Sequence[AgentMessage] = (),  # 新的 prompt
    max_turns: int | None = None,
    signal: CancellationToken | None = None,
    get_steering_messages: Callable[[], Sequence[AgentMessage]] | None = None,
    get_follow_up_messages: Callable[[], Sequence[AgentMessage]] | None = None,
    before_tool_call: BeforeToolCall | None = None,
    after_tool_call: AfterToolCall | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the provider/tool loop and emit Pi-compatible agent events."""
    new_messages = list(prompts)
    if prompts:
        messages.extend(prompts)

    yield AgentStartEvent()
    yield TurnStartEvent()
    for prompt in prompts:
        yield MessageStartEvent(message=prompt)
        yield MessageEndEvent(message=prompt)

    if max_turns is not None and max_turns < 1:
        error = _error_message(model, "max_turns must be at least 1")
        messages.append(error)
        new_messages.append(error)
        yield MessageStartEvent(message=error)
        yield MessageEndEvent(message=error)
        yield TurnEndEvent(message=error)
        yield AgentEndEvent(messages=new_messages)
        return

    tool_by_name = {tool.name: tool for tool in tools}
    turn = 1
    first_turn = True
    pending = tuple(get_steering_messages() if get_steering_messages else ())

    while True:
        has_more_tools = True
        # NOTE: 一次 agent 回复流程，反复调用 LLM 直到返回中不再包括 tool call 请求
        # 注意 pending 为 steering 或 followup messages
        while has_more_tools or pending:
            if not first_turn:
                yield TurnStartEvent()
            first_turn = False

            # NOTE: 此时 pending 的是 steering messages，即在将来最近的一个 turn 完成之后立即进行
            # NOTE: 此时 pending 的是 steering messages，即在将来最近的一个 turn 完成之后立即进行
            for message in pending:
                messages.append(message)
                new_messages.append(message)
                yield MessageStartEvent(message=message)
                yield MessageEndEvent(message=message)
            pending = ()

            # NOTE: 防止模型无限推理：当前 turn 数大于预设的最大 turn 数量时给出错误信息并提前结束当前回复过程
            # NOTE: 防止模型无限推理：当前 turn 数大于预设的最大 turn 数量时给出提示并提前结束当前回复过程
            if max_turns is not None and turn > max_turns:
                error = _error_message(model, f"Agent stopped after max_turns={max_turns}")
                messages.append(error)
                new_messages.append(error)
                yield MessageStartEvent(message=error)
                yield MessageEndEvent(message=error)
                yield TurnEndEvent(message=error)
                yield AgentEndEvent(messages=new_messages)
                return

            # NOTE: 开启一次 llm 调用
            # Python async generators cannot pass a yielding callback through a
            # normal await cleanly, so consume the assistant sub-generator and
            # retain its final message through the terminal event.
            assistant = None
            async for event in _assistant_events(
                provider=provider,
                model=model,
                system=system,
                messages=_provider_context(messages),
                tools=tools,
                signal=signal,
            ):
                yield event
                if isinstance(event, MessageEndEvent) and isinstance(
                    event.message, AssistantMessage
                ):
                    assistant = event.message

            if assistant is None:  # defensive: _assistant_events always terminates
                assistant = _error_message(model, "Provider produced no assistant message")
                yield MessageStartEvent(message=assistant)
                yield MessageEndEvent(message=assistant)

            messages.append(assistant)
            new_messages.append(assistant)
            if assistant.stop_reason in {"error", "aborted"}:
                yield TurnEndEvent(message=assistant)
                yield AgentEndEvent(messages=new_messages)
                return

            # NOTE: 获取一次 llm 调用结果后，获取其中 tool call 请求
            tool_results: list[ToolResultMessage] = []
            calls = list(assistant.tool_calls)
            has_more_tools = bool(calls)
            # NOTE: 执行 tool calls
            for call in calls:
                async for event in _execute_tool_call(
                    call,
                    tool_by_name,
                    signal,
                    before_tool_call,
                    after_tool_call,
                ):
                    yield event
                    # NOTE: tool call 结果也加入 message
                    if isinstance(event, MessageEndEvent) and isinstance(
                        event.message, ToolResultMessage
                    ):
                        tool_results.append(event.message)
                        messages.append(event.message)
                        new_messages.append(event.message)

            yield TurnEndEvent(message=assistant, tool_results=tool_results)
            turn += 1
            # NOTE: 每个 turn 结束后重新获取已有的 steering prompt（steering 的需要在未来最近一个 turn 结束后立即发送）
            pending = tuple(get_steering_messages() if get_steering_messages else ())

        # NOTE: followup 的 prompt 只会在 agent 一次回复完成之后再获取，开启下一个完整请求流程
        follow_ups = tuple(get_follow_up_messages() if get_follow_up_messages else ())
        if follow_ups:
            pending = follow_ups
            continue
        break

    yield AgentEndEvent(messages=new_messages)


# NOTE: 过滤掉失败或主动取消的 assistant message
def _provider_context(messages: list[AgentMessage]) -> list[AgentMessage]:
    """Return replayable messages while retaining failures in durable history.

    Providers cannot consistently accept an assistant turn with no content. Tau
    persists terminal failures for diagnostics, but an empty failed or aborted
    turn is not model context and must not poison the next request.
    """
    return [
        message
        for message in messages
        if not (
            isinstance(message, AssistantMessage)
            and message.stop_reason in {"error", "aborted"}
            and not message.content
        )
    ]


async def _assistant_events(
    *,
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    signal: CancellationToken | None,
) -> AsyncIterator[AgentEvent]:
    # NOTE: 一次 llm 调用（一个 turn）
    source: AsyncIterator[AssistantMessageEvent] = provider.stream_response(
        model=model,
        system=system,
        messages=messages,
        tools=tools,
        signal=signal,
    )
    started = False
    async for event in source:
        if isinstance(event, AssistantStartEvent):
            started = True
            yield MessageStartEvent(message=event.partial)
        elif isinstance(event, AssistantDoneEvent):
            if not started:
                yield MessageStartEvent(message=event.message)
            yield MessageEndEvent(message=event.message)
        elif isinstance(event, AssistantErrorEvent):
            if not started:
                yield MessageStartEvent(message=event.error)
            yield MessageEndEvent(message=event.error)
        else:
            yield MessageUpdateEvent(
                message=event.partial,
                assistant_message_event=event,
            )


async def _execute_tool_call(
    call: ToolCall,
    tools: Mapping[str, AgentTool],
    signal: CancellationToken | None,
    before_tool_call: BeforeToolCall | None,
    after_tool_call: AfterToolCall | None,
) -> AsyncIterator[AgentEvent]:
    yield ToolExecutionStartEvent(
        tool_call_id=call.id,
        tool_name=call.name,
        args=call.arguments,
    )

    blocked = False
    block_reason: str | None = None
    # NOTE: tool call 执行前拦截器，可阻塞
    if before_tool_call is not None:
        blocked, block_reason = await before_tool_call(call)

    if blocked:
        result = _error_result(block_reason or "Tool execution was blocked")
        is_error = True
    elif signal is not None and signal.is_cancelled():
        result = _error_result("Operation aborted")
        is_error = True
    else:
        tool = tools.get(call.name)
        if tool is None:
            result = _error_result(f"Tool {call.name} not found")
            is_error = True
        else:
            result, is_error, updates = await _run_tool(tool, call, signal)
            # NOTE: updates 为 tool call 部分中间过程可触发的事件
            for update in updates:
                yield ToolExecutionUpdateEvent(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.arguments,
                    partial_result=update,
                )

    if after_tool_call is not None:
        result, is_error = await after_tool_call(call, result, is_error)

    yield ToolExecutionEndEvent(
        tool_call_id=call.id,
        tool_name=call.name,
        result=result,
        is_error=is_error,
    )
    message = ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=result.content,
        details=result.details,
        added_tool_names=result.added_tool_names,
        is_error=is_error,
    )
    yield MessageStartEvent(message=message)
    yield MessageEndEvent(message=message)


async def _run_tool(
    tool: AgentTool,
    call: ToolCall,
    signal: CancellationToken | None,
) -> tuple[AgentToolResult, bool, list[AgentToolResult]]:
    updates: list[AgentToolResult] = []
    accepting = True

    def on_update(partial: AgentToolResult) -> None:
        if accepting:
            updates.append(partial.model_copy(deep=True))

    try:
        result = await tool.execute(call.id, call.arguments, signal, on_update)
        return result, False, updates
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - tools are an isolation boundary
        return _error_result(str(exc)), True, updates
    finally:
        accepting = False


def _error_result(message: str) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=message)], details={})


def _error_message(model: str, message: str) -> AssistantMessage:
    return AssistantMessage(
        model=model,
        content=[],
        stop_reason="error",
        error_message=message,
    )
