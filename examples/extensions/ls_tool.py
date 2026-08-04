"""Tau extension: `ls` tool (port of Pi's built-in ls).

Lists directory contents: entries sorted alphabetically (case-insensitive),
'/' suffix for directories, includes dotfiles, optional entry limit and
50KB output cap.

Port of Pi's `packages/coding-agent/src/core/tools/ls.ts`: same parameters
(path, limit) and same output semantics (sorted, '/' suffix, dotfiles,
truncation notices). Pure standard-library implementation.

Install by copying into `~/.tau/extensions/`, or run:

    tau -e examples/extensions/ls_tool.py
"""

import os
from pathlib import Path

from tau_agent.messages import TextContent
from tau_agent.tools import AgentTool, AgentToolResult, ToolExecutor
from tau_coding.extensions import ExtensionAPI

DEFAULT_LIMIT = 500
MAX_OUTPUT_BYTES = 50 * 1024  # 50KB, mirrors Pi's DEFAULT_MAX_BYTES


def _truncate_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """Return (text truncated to max_bytes, whether truncation happened)."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    return data[:max_bytes].decode("utf-8", errors="ignore").rstrip() + "...", True


def _ls_executor(tau: ExtensionAPI) -> ToolExecutor:
    """Return the async executor for the ls tool, resolving paths against the session cwd.

    The cwd is read lazily on first execution: ``setup()`` runs before the extension
    runtime is bound to a session, while tool executions always happen on a bound
    session.
    """

    async def execute(tool_call_id, arguments, signal=None, on_update=None):  # noqa: ANN001, ANN202
        del tool_call_id, on_update
        if signal is not None and signal.is_cancelled():
            raise RuntimeError("Operation aborted")

        cwd = Path(tau.context.cwd)
        raw_path = arguments.get("path") or "."
        limit = arguments.get("limit", DEFAULT_LIMIT)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        if limit <= 0:
            limit = DEFAULT_LIMIT

        dir_path = Path(raw_path).expanduser()
        if not dir_path.is_absolute():
            dir_path = cwd / dir_path

        if not dir_path.exists():
            raise RuntimeError(f"Path not found: {dir_path}")
        if not dir_path.is_dir():
            raise RuntimeError(f"Not a directory: {dir_path}")

        try:
            entries = sorted(os.scandir(dir_path), key=lambda entry: entry.name.lower())
        except OSError as exc:
            raise RuntimeError(f"Cannot read directory: {exc}") from exc

        results: list[str] = []
        entry_limit_reached = False
        for entry in entries:
            if len(results) >= limit:
                entry_limit_reached = True
                break
            try:
                suffix = "/" if entry.is_dir() else ""
            except OSError:
                continue  # skip entries we cannot stat (Pi does the same)
            results.append(entry.name + suffix)

        if not results:
            return AgentToolResult(content=[TextContent(text="(empty directory)")])

        raw_output = "\n".join(results)
        output, truncated = _truncate_bytes(raw_output, MAX_OUTPUT_BYTES)

        notices: list[str] = []
        if entry_limit_reached:
            notices.append(f"{limit} entries limit reached. Use limit={limit * 2} for more")
        if truncated:
            notices.append(f"{MAX_OUTPUT_BYTES // 1024}KB limit reached")
        if notices:
            output += f"\n\n[{'. '.join(notices)}]"

        return AgentToolResult(content=[TextContent(text=output)])

    return execute


def setup(tau: ExtensionAPI) -> None:
    """Register the ls tool."""
    tau.register_tool(
        AgentTool(
            name="ls",
            label="ls",
            description=(
                "List directory contents. Returns entries sorted alphabetically, with "
                "'/' suffix for directories. Includes dotfiles. Output is truncated to "
                f"{DEFAULT_LIMIT} entries or {MAX_OUTPUT_BYTES // 1024}KB "
                "(whichever is hit first)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory to list (default: current directory)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of entries to return (default: 500)",
                    },
                },
            },
            execute_fn=_ls_executor(tau),
            prompt_snippet="List directory contents",
        )
    )
