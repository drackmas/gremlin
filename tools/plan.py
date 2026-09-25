"""Plan tool: persistent planning and task management.

Backs Gremlin's autonomous coding loop: inspect the codebase, create a
structured plan (goal, phases, tasks, dependencies), execute it incrementally
while recording test results, and revise it when results invalidate it. State
lives in ``plan.json`` at the project root (authoritative, machine-readable);
``plan.md`` is an optional human-readable view. Git checkpoints are recorded
in the plan so self-modifying work can be reverted.
"""

from __future__ import annotations

from config import AppConfig
from planning.store import PlanError, PlanStore, plan_progress, render_plan

from .registry import Tool

ACTIONS = [
    "create",
    "view",
    "start",
    "done",
    "progress",
    "block",
    "skip",
    "reset",
    "test",
    "revise",
    "checkpoint",
    "render",
    "finish",
    "abandon",
]


def build_plan_tool(cfg: AppConfig) -> Tool:
    store = PlanStore(cfg.root / "plan.json")

    def plan(args: dict) -> str:
        action = args["action"]

        if action == "create":
            p = store.create(args.get("goal", ""), args.get("phases"), args.get("notes"))
            n_tasks = sum(len(ph.tasks) for ph in p.phases)
            return f"plan created: {p.goal} — {len(p.phases)} phase(s), {n_tasks} task(s); state persisted to plan.json"

        if action == "view":
            return render_plan(store.require())

        if action == "start":
            store.start(args["task_id"], args.get("note"))
            return f"{args['task_id']} -> in_progress"

        if action == "done":
            p = store.complete(args["task_id"], args.get("note"))
            done, total, pct = plan_progress(p)
            return f"{args['task_id']} -> done ({done}/{total}, {pct}%)"

        if action == "progress":
            store.set_progress(args["task_id"], int(args["pct"]), args.get("note"))
            return f"{args['task_id']} progress: {max(0, min(100, int(args['pct'])))}%"

        if action == "block":
            store.block(args["task_id"], args.get("note", ""))
            return f"{args['task_id']} -> blocked: {args.get('note', '').strip()}"

        if action == "skip":
            store.skip(args["task_id"], args.get("note"))
            return f"{args['task_id']} -> skipped"

        if action == "reset":
            store.reset(args["task_id"])
            return f"{args['task_id']} -> pending"

        if action == "test":
            store.record_test(args["task_id"], args.get("command", ""), bool(args.get("ok")), args.get("summary", ""))
            verdict = "ok" if args.get("ok") else "FAILED"
            return f"{args['task_id']} test recorded: `{args.get('command', '')}` {verdict}"

        if action == "revise":
            p = store.revise(args.get("reason", ""), args.get("goal"), args.get("phases"))
            return f"plan revised (revision {p.revision}): {args.get('reason', '').strip()}"

        if action == "checkpoint":
            _, commit = store.checkpoint(args.get("message", ""))
            return f"checkpoint {commit[:12]}: {args.get('message', '').strip()} (recorded in plan.json)"

        if action == "render":
            return f"plan.md written to {store.render()}"

        if action == "finish":
            p = store.finish()
            done, total, pct = plan_progress(p)
            return f"plan completed ({done}/{total}, {pct}%)"

        if action == "abandon":
            store.abandon(args.get("reason", ""))
            return f"plan abandoned: {args.get('reason', '').strip()}"

        raise PlanError(f"unknown action: {action}")

    return Tool(
        name="plan",
        description=(
            "Persistent planning and task management for substantial coding or refactoring work. "
            "State lives in plan.json at the project root and survives context compaction and restarts. "
            "Workflow: inspect the codebase, then create (goal + phases + tasks with depends_on); "
            "execute incrementally: start a task, do the work, run the relevant tests with run_command, "
            "record results with test, then mark it done. Before large self-modifying changes, create a "
            "git checkpoint. If results invalidate the plan, revise it instead of continuing blindly. "
            "Actions: create, view, start, done, progress, block, skip, reset, test, revise, checkpoint, "
            "render (write human-readable plan.md), finish, abandon."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ACTIONS,
                    "description": "plan action to perform",
                },
                "goal": {
                    "type": "string",
                    "description": "what the plan achieves (create, or revise when changing it)",
                },
                "phases": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "phase title"},
                            "tasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string", "description": "task title"},
                                        "depends_on": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "ids of tasks that must be done or skipped first",
                                        },
                                    },
                                    "required": ["title"],
                                },
                            },
                        },
                        "required": ["title", "tasks"],
                    },
                    "description": "phases with their tasks (create, or revise when replacing the structure)",
                },
                "task_id": {"type": "string", "description": "task id to act on (e.g. t1)"},
                "note": {"type": "string", "description": "optional note attached to the task action"},
                "pct": {"type": "integer", "description": "progress percentage 0-100 (progress action)"},
                "command": {"type": "string", "description": "test command that was run (test action)"},
                "ok": {"type": "boolean", "description": "whether the tests passed (test action)"},
                "summary": {"type": "string", "description": "short test result summary (test action)"},
                "reason": {
                    "type": "string",
                    "description": "why the plan is being revised or abandoned",
                },
                "message": {"type": "string", "description": "checkpoint label (checkpoint action)"},
                "notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "initial plan-level notes (create action)",
                },
            },
            "required": ["action"],
        },
        handler=plan,
    )
