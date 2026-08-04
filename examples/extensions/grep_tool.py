"""Tau extension: `grep` tool (port of Pi's built-in grep).

Searches file contents for a pattern and returns matching lines with file
paths and line numbers. Uses ripgrep (rg) when available — which respects
.gitignore — with a pure-Python fallback for systems without rg.

Port of Pi's `packages/coding-agent/src/core/tools/grep.ts`: same parameters
(pattern, path, glob, ignoreCase, literal, context, limit) and the same
output semantics: `path:line: text` for matches, `path-line- text` for
context lines, relative paths for directory searches, long lines truncated
to 500 chars, output capped at 50KB, and match-limit notices.

Install by copying into `~/.tau/extensions/`, or run:

    tau -e examples/extensions/grep_tool.py
"""

import asyncio
import json
import re
import shutil
from fnmatch import fnmatch
from pathlib import Path
from typing import Generator

from tau_agent.messages import TextContent
from tau_agent.tools import AgentTool, AgentToolResult, ToolExecutor
from tau_coding.extensions import ExtensionAPI

DEFAULT_LIMIT = 100
MAX_LINE_CHARS = 500
MAX_OUTPUT_BYTES = 50 * 1024  # 50KB, mirrors Pi's DEFAULT_MAX_BYTES

# Fallback-only: rg --hidden includes dotfiles but still respects .gitignore,
# which normally excludes VCS/cache dirs. The pure-Python fallback cannot parse
# .gitignore, so it skips these common directories to stay fast and useful.
_SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "node_modules"}


def _resolve(raw: str | None, cwd: Path) -> Path:
    path = Path(raw).expanduser() if raw else cwd
    if not path.is_absolute():
        path = cwd / path
    return path


def _format_path(file_path: Path, search_path: Path, is_directory: bool) -> str:
    """Return a relative path for directory searches, basename for file searches."""
    if is_directory:
        try:
            relative = file_path.relative_to(search_path)
            return relative.as_posix()
        except ValueError:
            pass
    return file_path.name


def _truncate_line(text: str) -> tuple[str, bool]:
    collapsed = text.replace("\r", "")
    if len(collapsed) <= MAX_LINE_CHARS:
        return collapsed, False
    return collapsed[: MAX_LINE_CHARS - 3].rstrip() + "...", True


def _truncate_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    return data[:max_bytes].decode("utf-8", errors="ignore").rstrip() + "...", True


async def _rg_search(
    rg: str,
    search_path: Path,
    *,
    pattern: str,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context: int,
    limit: int,
    signal,
) -> AgentToolResult:
    """Search via ripgrep --json, mirroring Pi's streaming implementation."""
    args = ["--json", "--line-number", "--color=never", "--hidden"]
    if ignore_case:
        args.append("--ignore-case")
    if literal:
        args.append("--fixed-strings")
    if glob:
        args += ["--glob", glob]
    args += ["--", pattern, str(search_path)]

    process = await asyncio.create_subprocess_exec(
        rg,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    matches: list[dict[str, object]] = []
    match_limit_reached = False
    killed = False

    async def read_stream() -> None:
        nonlocal match_limit_reached, killed
        async for raw in process.stdout:  # type: ignore[union-attr]
            if signal is not None and signal.is_cancelled():
                process.kill()
                killed = True
                break
            for line in raw.decode("utf-8", errors="replace").splitlines():
                if not line.strip() or len(matches) >= limit:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "match":
                    continue
                data = event.get("data") or {}
                file_path = (data.get("path") or {}).get("text")
                line_number = data.get("line_number")
                line_text = (data.get("lines") or {}).get("text")
                if file_path and isinstance(line_number, int):
                    matches.append(
                        {
                            "file_path": file_path,
                            "line_number": line_number,
                            "line_text": line_text,
                        }
                    )
                    if len(matches) >= limit:
                        match_limit_reached = True
                        process.kill()
                        killed = True
                        break

    stderr_task = asyncio.create_task(process.stderr.read())
    await asyncio.gather(read_stream(), stderr_task)
    stderr = stderr_task.result().decode("utf-8", errors="replace")
    await process.wait()

    if signal is not None and signal.is_cancelled():
        raise RuntimeError("Operation aborted")
    if not killed and process.returncode not in (0, 1):
        raise RuntimeError(stderr.strip() or f"ripgrep exited with code {process.returncode}")

    return _format_matches(
        matches,
        search_path=search_path,
        is_directory=search_path.is_dir(),
        context=context,
        match_limit_reached=match_limit_reached,
    )


async def _python_search(
    search_path: Path,
    *,
    pattern: str,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context: int,
    limit: int,
    signal,
) -> AgentToolResult:
    """Best-effort fallback when ripgrep is not installed."""
    try:
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(re.escape(pattern) if literal else pattern, flags)
    except re.error as exc:
        raise RuntimeError(f"Invalid regex pattern: {exc}") from exc

    files = [search_path] if search_path.is_file() else _walk_files(search_path)
    matches: list[dict[str, object]] = []

    for file_path in files:
        if len(matches) >= limit:
            break
        if signal is not None and signal.is_cancelled():
            raise RuntimeError("Operation aborted")
        rel = _format_path(file_path, search_path, search_path.is_dir())
        if glob and not fnmatch(rel, glob):
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # skip unreadable/binary files
        if "\x00" in content:
            continue  # binary
        lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for index, line in enumerate(lines):
            if compiled.search(line):
                matches.append(
                    {
                        "file_path": str(file_path),
                        "line_number": index + 1,
                        "line_text": line,
                    }
                )
                if len(matches) >= limit:
                    break

    return _format_matches(
        matches,
        search_path=search_path,
        is_directory=search_path.is_dir(),
        context=context,
        match_limit_reached=len(matches) >= limit,
    )


def _walk_files(search_path: Path) -> Generator[Path]:
    for root, dirs, files in search_path.walk():
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            yield root / name


def _format_matches(
    matches: list[dict[str, object]],
    *,
    search_path: Path,
    is_directory: bool,
    context: int,
    match_limit_reached: bool,
) -> AgentToolResult:
    """Format matches like Pi: `path:line: text`, context as `path-line- text`."""
    if not matches:
        return AgentToolResult(content=[TextContent(text="No matches found")])

    output_lines: list[str] = []
    lines_truncated = False
    for match in matches:
        file_path = Path(str(match["file_path"]))
        line_number = int(match["line_number"])
        rel = _format_path(file_path, search_path, is_directory)
        if context > 0:
            try:
                file_lines = file_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                file_lines = []
            start = max(0, line_number - 1 - context)
            end = min(len(file_lines), line_number + context)
            for current in range(start, end):
                text, was_truncated = _truncate_line(file_lines[current])
                lines_truncated = lines_truncated or was_truncated
                if current == line_number - 1:
                    output_lines.append(f"{rel}:{current + 1}: {text}")
                else:
                    output_lines.append(f"{rel}-{current + 1}- {text}")
        else:
            text, was_truncated = _truncate_line(str(match.get("line_text") or "").rstrip("\n"))
            lines_truncated = lines_truncated or was_truncated
            output_lines.append(f"{rel}:{line_number}: {text}")

    raw_output = "\n".join(output_lines)
    output, truncated = _truncate_bytes(raw_output, MAX_OUTPUT_BYTES)

    notices: list[str] = []
    if match_limit_reached:
        notices.append(
            f"{len(matches)} matches limit reached. Use limit={len(matches) * 2} for more, "
            "or refine pattern"
        )
    if truncated:
        notices.append(f"{MAX_OUTPUT_BYTES // 1024}KB limit reached")
    if lines_truncated:
        notices.append(
            f"Some lines truncated to {MAX_LINE_CHARS} chars. Use read tool to see full lines"
        )
    if notices:
        output += f"\n\n[{' '.join(notices)}]"

    return AgentToolResult(content=[TextContent(text=output)])


def _grep_executor(tau: ExtensionAPI) -> ToolExecutor:
    """Return the async executor for the grep tool, resolving paths against the session cwd.

    The cwd is read lazily on first execution: ``setup()`` runs before the extension
    runtime is bound to a session, while tool executions always happen on a bound
    session.
    """

    async def execute(tool_call_id, arguments, signal=None, on_update=None):  # noqa: ANN001, ANN202
        del tool_call_id, on_update
        if signal is not None and signal.is_cancelled():
            raise RuntimeError("Operation aborted")

        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise RuntimeError("pattern is required and must be a non-empty string")

        search_path = _resolve(arguments.get("path"), Path(tau.context.cwd))
        if not search_path.exists():
            raise RuntimeError(f"Path not found: {search_path}")

        limit = arguments.get("limit", DEFAULT_LIMIT)
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        context = arguments.get("context", 0)
        try:
            context = max(0, int(context))
        except (TypeError, ValueError):
            context = 0

        kwargs = {
            "pattern": pattern,
            "glob": arguments.get("glob"),
            "ignore_case": bool(arguments.get("ignoreCase", False)),
            "literal": bool(arguments.get("literal", False)),
            "context": context,
            "limit": limit,
        }

        rg = shutil.which("rg")
        if rg is not None:
            return await _rg_search(rg, search_path, signal=signal, **kwargs)
        return await _python_search(search_path, signal=signal, **kwargs)

    return execute


def setup(tau: ExtensionAPI) -> None:
    """Register the grep tool."""
    tau.register_tool(
        AgentTool(
            name="grep",
            label="grep",
            description=(
                "Search file contents for a pattern. Returns matching lines with file "
                "paths and line numbers. Uses ripgrep when available (respects "
                f".gitignore). Output is truncated to {DEFAULT_LIMIT} matches or "
                f"{MAX_OUTPUT_BYTES // 1024}KB (whichever is hit first). Long lines are "
                f"truncated to {MAX_LINE_CHARS} chars."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (regex or literal string)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search (default: current directory)",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Filter files by glob, e.g. '*.py' or '**/*.spec.py'",
                    },
                    "ignoreCase": {
                        "type": "boolean",
                        "description": "Case-insensitive search (default: false)",
                    },
                    "literal": {
                        "type": "boolean",
                        "description": "Treat pattern as literal string (default: false)",
                    },
                    "context": {
                        "type": "integer",
                        "description": "Lines before and after each match (default: 0)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches to return (default: 100)",
                    },
                },
                "required": ["pattern"],
            },
            execute_fn=_grep_executor(tau),
            prompt_snippet="Search file contents for patterns (respects .gitignore)",
        )
    )
