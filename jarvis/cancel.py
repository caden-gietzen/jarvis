"""cancel_task: lets the model stop running work when the user asks (or it is clearly right).

Jarvis never cancels on its own when you say "hey Jarvis" mid-task; the model hears you
first and decides. Built like showcode.build_server: an in-process SDK MCP server.
"""

import asyncio

from claude_agent_sdk import create_sdk_mcp_server, tool

from .events import bus

SERVER_NAME = "jarvis_control"
TOOL_NAME = "cancel_task"
TOOL_ID = f"mcp__{SERVER_NAME}__{TOOL_NAME}"
LIST_TOOL_NAME = "list_tasks"
LIST_TOOL_ID = f"mcp__{SERVER_NAME}__{LIST_TOOL_NAME}"
CANCEL_TIMEOUT_S = 10.0  # the control request should answer at once; never hang the handler

_DESCRIPTION = (
    "Stop work that is running. With task_id (get ids from list_tasks), stops only that background "
    "task or subagent (the work keeps going otherwise). Without task_id, interrupts the whole current turn. "
    "Use only when the user asks to stop/cancel, or it is clearly the right call; stopped work "
    "can leave edits half-finished, so check the files afterwards."
)
_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string",
                    "description": "Optional. Id of one background task to stop. Omit to interrupt the current turn."},
    },
}
_LIST_DESCRIPTION = (
    "List the background tasks and subagents running right now: id, description, current "
    "activity and elapsed time. Read-only. Use it to find the task_id for cancel_task."
)
_WARNING = "Warning: stopped work may have left edits half-finished; check the affected files."


def _result(text: str, error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": text}]}
    if error:
        out["is_error"] = True
    return out


async def cancel_task_impl(args: dict, get_client, on_interrupt=None) -> dict:
    """get_client is called now, not at build time: recovery replaces the client.
    on_interrupt() is called after a whole-turn interrupt succeeded (the turn ends with no reply,
    so the caller arranges a spoken confirmation)."""
    task_id = str((args or {}).get("task_id") or "").strip()
    client = get_client()
    try:
        if task_id:
            await asyncio.wait_for(client.stop_task(task_id), CANCEL_TIMEOUT_S)
            return _result(f"Stopped background task {task_id}. Other work is still running. {_WARNING}")
        await asyncio.wait_for(client.interrupt(), CANCEL_TIMEOUT_S)
        if on_interrupt:
            on_interrupt()
        return _result(f"Interrupted the current turn and everything it was running. {_WARNING}")
    except Exception as e:  # includes timeouts; report, never raise into the SDK
        what = f"task {task_id}" if task_id else "the current turn"
        return _result(f"Error: could not stop {what} ({type(e).__name__}: {e}). It may still be running.", True)


async def list_tasks_impl(args=None, get_bus=lambda: bus) -> dict:
    tasks = get_bus().get_tasks(active_only=True)
    if not tasks:
        return _result("No background tasks or subagents are running.")
    lines = [f"{len(tasks)} running:"]
    for t in tasks:
        lines.append(f"- id={t['task_id']} | {t['kind']} | {t['description']} | "
                     f"activity: {t['activity'] or 'unknown'} | elapsed: {t['elapsed']:.0f}s")
    return _result(chr(10).join(lines))


def build_server(get_client, on_interrupt=None):
    @tool(TOOL_NAME, _DESCRIPTION, _SCHEMA)
    async def cancel_task(args):
        return await cancel_task_impl(args, get_client, on_interrupt)

    @tool(LIST_TOOL_NAME, _LIST_DESCRIPTION, {"type": "object", "properties": {}})
    async def list_tasks(args):
        return await list_tasks_impl(args)

    return create_sdk_mcp_server(SERVER_NAME, tools=[cancel_task, list_tasks])
