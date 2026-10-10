"""A team task list.

It gives the gateway a second useful server, so one prompt can span two of
them ("add a packing task for each of today's orders"). Tasks are kept in
memory: restarting the pod empties the list, which is fine for a demo and keeps
the server free of any storage.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field


class Task(BaseModel):
    id: int
    title: str
    notes: str
    done: bool
    created_at: str


def build() -> MCPServer:
    mcp = MCPServer("tasks", title="Team tasks", instructions="A shared to-do list for the team.")
    tasks: dict[int, Task] = {}
    ids = itertools.count(1)

    @mcp.tool(title="Add task", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    def add_task(
        title: Annotated[str, Field(min_length=1, max_length=200, description="What needs doing.")],
        notes: Annotated[str, Field(description="Any detail worth keeping with the task.")] = "",
    ) -> Task:
        """Add a task to the team's to-do list."""
        task = Task(id=next(ids), title=title, notes=notes, done=False, created_at=datetime.now(UTC).isoformat(timespec="seconds"))
        tasks[task.id] = task
        return task

    @mcp.tool(title="List tasks", annotations=ToolAnnotations(read_only_hint=True))
    def list_tasks(
        show: Annotated[Literal["open", "done", "all"], Field(description="Which tasks to list.")] = "open",
    ) -> list[Task]:
        """List the team's tasks."""
        return [task for task in tasks.values() if show == "all" or task.done == (show == "done")]

    @mcp.tool(title="Complete task", annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
    def complete_task(task_id: Annotated[int, Field(ge=1, description="The task's ID.")]) -> Task:
        """Mark a task as done."""
        if task_id not in tasks:
            raise ToolError(f"There is no task {task_id}.")
        tasks[task_id] = tasks[task_id].model_copy(update={"done": True})
        return tasks[task_id]

    @mcp.tool(title="Server info", annotations=ToolAnnotations(read_only_hint=True))
    def server_info() -> dict[str, str | int]:
        """Say which server this is and how many tasks it holds."""
        return {"server": "tasks", "tasks": len(tasks)}

    return mcp
