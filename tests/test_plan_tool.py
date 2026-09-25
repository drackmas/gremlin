"""Plan tool: registry actions and agent-loop integration.

Covers the tool surface (create/view/execute/revise/render/checkpoint) and
proves the plan persists across turns: a second manager.run() must see the
plan in its system prompt without it ever being in the model's context.
"""

from __future__ import annotations

import json
import subprocess

from chat.manager import ChatManager
from models.base import ModelBackend, ModelEvent
from planning.store import PlanStore
from sessions import SessionManager
from skills.loader import SkillLoader
from tools import build_registry

SETTINGS = {"show_thinking": False, "base_url": "http://fake", "model": "fake"}

SAMPLE_PHASES = [
    {"title": "Scaffold", "tasks": [
        {"title": "wire up module"},
        {"title": "write unit tests", "depends_on": ["t1"]},
    ]},
    {"title": "Polish", "tasks": [
        {"title": "docs", "depends_on": ["t2"]},
    ]},
]


class FakeBackend(ModelBackend):
    """Scripted backend: each call pops the next list of events."""

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = []

    def stream(self, messages, tools, model):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools, "model": model})
        events = self.scripts.pop(0)
        for ev in events:
            yield ev


def make_manager(cfg, backend):
    sessions = SessionManager(cfg)
    registry = build_registry(cfg)
    manager = ChatManager(cfg, sessions, registry, SkillLoader(cfg), backend)
    return sessions, manager


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _init_repo(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "gremlin@test")
    _git(root, "config", "user.name", "Gremlin")


# ---------------------------------------------------------------------------
# Tool actions
# ---------------------------------------------------------------------------


def test_plan_create_view_roundtrip(cfg):
    registry = build_registry(cfg)
    result, ok = registry.execute("plan", {"action": "create", "goal": "Ship the feature", "phases": SAMPLE_PHASES})
    assert ok and "plan created" in result
    assert (cfg.root / "plan.json").exists()
    plan = PlanStore(cfg.root / "plan.json").load()
    assert plan.goal == "Ship the feature"
    assert [t.id for p in plan.phases for t in p.tasks] == ["t1", "t2", "t3"]

    view, ok = registry.execute("plan", {"action": "view"})
    assert ok
    assert "plan: Ship the feature" in view
    assert "t1 wire up module [pending]" in view


def test_plan_tool_execution_cycle(cfg):
    registry = build_registry(cfg)
    registry.execute("plan", {"action": "create", "goal": "g", "phases": SAMPLE_PHASES})

    # dependency gating surfaces as a tool error, not an exception
    result, ok = registry.execute("plan", {"action": "start", "task_id": "t2"})
    assert not ok and "dependency t1" in result

    result, ok = registry.execute("plan", {"action": "start", "task_id": "t1"})
    assert ok and "t1 -> in_progress" in result
    result, ok = registry.execute("plan", {"action": "progress", "task_id": "t1", "pct": 60})
    assert ok and "60%" in result
    result, ok = registry.execute("plan", {"action": "test", "task_id": "t1", "command": "pytest -q", "ok": True, "summary": "2 passed"})
    assert ok and "ok" in result
    result, ok = registry.execute("plan", {"action": "done", "task_id": "t1"})
    assert ok and "(1/3, 33%)" in result

    plan = PlanStore(cfg.root / "plan.json").load()
    t1 = plan.phases[0].tasks[0]
    assert t1.status == "done" and t1.progress == 100
    assert t1.test is not None and t1.test.ok is True

    # block needs a note
    result, ok = registry.execute("plan", {"action": "block", "task_id": "t3", "note": ""})
    assert not ok and "note" in result
    result, ok = registry.execute("plan", {"action": "block", "task_id": "t3", "note": "waiting on design"})
    assert ok and "blocked" in result
    result, ok = registry.execute("plan", {"action": "reset", "task_id": "t3"})
    assert ok and "t3 -> pending" in result


def test_plan_tool_unknown_action_and_args(cfg):
    registry = build_registry(cfg)
    result, ok = registry.execute("plan", {"action": "explode"})
    assert not ok and "unknown action" in result
    result, ok = registry.execute("plan", {"action": "done", "task_id": "nope"})  # no plan yet
    assert not ok and "no plan" in result
    registry.execute("plan", {"action": "create", "goal": "g", "phases": SAMPLE_PHASES})
    result, ok = registry.execute("plan", {"action": "done", "task_id": "nope"})
    assert not ok and "unknown task" in result
    result, ok = registry.execute("plan", {"action": "view"})
    assert ok and "plan: g" in result


def test_plan_revise_and_render(cfg):
    registry = build_registry(cfg)
    registry.execute("plan", {"action": "create", "goal": "big scope", "phases": SAMPLE_PHASES})
    result, ok = registry.execute(
        "plan",
        {"action": "revise", "reason": "tests revealed the scaffold is wrong", "goal": "smaller scope",
         "phases": [{"title": "Only", "tasks": [{"title": "one task"}]}]},
    )
    assert ok and "revision 1" in result
    plan = PlanStore(cfg.root / "plan.json").load()
    assert plan.revision == 1 and plan.goal == "smaller scope"
    assert [t.id for p in plan.phases for t in p.tasks] == ["t1"]

    result, ok = registry.execute("plan", {"action": "render"})
    assert ok and "plan.md" in result
    md = (cfg.root / "plan.md").read_text(encoding="utf-8")
    assert "# Plan: smaller scope" in md
    assert "revision**: 1" in md


def test_plan_checkpoint_action(cfg):
    _init_repo(cfg.root)
    (cfg.root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(cfg.root, "add", "-A")
    _git(cfg.root, "commit", "-q", "-m", "baseline")

    registry = build_registry(cfg)
    registry.execute("plan", {"action": "create", "goal": "g", "phases": SAMPLE_PHASES})
    (cfg.root / "work.txt").write_text("new work\n", encoding="utf-8")
    result, ok = registry.execute("plan", {"action": "checkpoint", "message": "before self-modification"})
    assert ok and "checkpoint" in result

    plan = PlanStore(cfg.root / "plan.json").load()
    assert len(plan.checkpoints) == 1
    head = _git(cfg.root, "rev-parse", "HEAD").stdout.strip()
    assert plan.checkpoints[0].commit == head
    tracked = _git(cfg.root, "show", "--name-only", "--format=", head).stdout.split()
    assert "work.txt" in tracked


def test_plan_finish_requires_all_tasks_done(cfg):
    registry = build_registry(cfg)
    registry.execute("plan", {"action": "create", "goal": "g", "phases": SAMPLE_PHASES})
    result, ok = registry.execute("plan", {"action": "finish"})
    assert not ok and "cannot finish" in result
    for tid in ("t1", "t2", "t3"):
        registry.execute("plan", {"action": "skip", "task_id": tid})
    result, ok = registry.execute("plan", {"action": "finish"})
    assert ok and "plan completed (3/3, 100%)" in result
    assert PlanStore(cfg.root / "plan.json").load().status == "completed"


# ---------------------------------------------------------------------------
# Agent-loop integration
# ---------------------------------------------------------------------------


def test_agent_loop_executes_plan_end_to_end(cfg):
    backend = FakeBackend(
        [
            [
                ModelEvent("text", text="Planning first."),
                ModelEvent("tool_call", tool_call_id="c1", name="plan",
                           arguments={"action": "create", "goal": "Ship it", "phases": SAMPLE_PHASES}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [
                ModelEvent("tool_call", tool_call_id="c2", name="plan", arguments={"action": "start", "task_id": "t1"}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [
                ModelEvent("tool_call", tool_call_id="c3", name="plan",
                           arguments={"action": "test", "task_id": "t1", "command": "pytest -q", "ok": True}),
                ModelEvent("tool_call", tool_call_id="c4", name="plan", arguments={"action": "done", "task_id": "t1"}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [
                ModelEvent("tool_call", tool_call_id="c5", name="plan", arguments={"action": "skip", "task_id": "t2"}),
                ModelEvent("tool_call", tool_call_id="c6", name="plan", arguments={"action": "skip", "task_id": "t3"}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [
                ModelEvent("tool_call", tool_call_id="c7", name="plan", arguments={"action": "finish"}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [ModelEvent("text", text="All done."), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    events = list(manager.run(s["id"], "ship it", SETTINGS))

    tool_events = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_events) == 7
    assert all(e["status"] == "ok" for e in tool_events)
    assert events[-1]["type"] == "done" and events[-1]["stop_reason"] == "completed"

    # authoritative state on disk, outside the LLM context
    plan = PlanStore(cfg.root / "plan.json").load()
    assert plan.status == "completed"
    assert plan.phases[0].tasks[0].status == "done"
    assert plan.phases[0].tasks[0].test is not None and plan.phases[0].tasks[0].test.ok


def test_plan_visible_in_next_turn_system_prompt(cfg):
    # turn 1: the model creates a plan
    backend = FakeBackend(
        [
            [
                ModelEvent("tool_call", tool_call_id="c1", name="plan",
                           arguments={"action": "create", "goal": "Persistent goal text", "phases": SAMPLE_PHASES}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [ModelEvent("text", text="planned"), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    list(manager.run(s["id"], "plan the work", SETTINGS))
    # turn 1 had no plan yet: no plan section in its system prompt
    assert "Current plan" not in backend.calls[0]["messages"][0]["content"]

    # turn 2: same session, new API message list built from disk state
    backend2 = FakeBackend([[ModelEvent("text", text="continuing"), ModelEvent("done")]])
    manager2 = ChatManager(cfg, sessions, build_registry(cfg), SkillLoader(cfg), backend2)
    list(manager2.run(s["id"], "continue", SETTINGS))
    system_prompt = backend2.calls[0]["messages"][0]["content"]
    assert "Current plan" in system_prompt
    assert "goal: Persistent goal text" in system_prompt
    assert "t1 wire up module [pending]" in system_prompt


def test_planning_guidance_always_in_prompt(cfg):
    backend = FakeBackend([[ModelEvent("text", text="ok"), ModelEvent("done")]])
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    list(manager.run(s["id"], "hi", SETTINGS))
    system_prompt = backend.calls[0]["messages"][0]["content"]
    assert "plan.json" in system_prompt
    assert "checkpoint" in system_prompt.lower()

def test_compaction_preserves_in_progress_task(cfg):

    # A plan with an in_progress task must survive compaction: the plan
    # state lives in plan.json on disk, outside the LLM context. After
    # compaction the system prompt is rebuilt and must show the task
    # still in_progress so the model continues it.
    backend = FakeBackend(
        [
            [
                ModelEvent("tool_call", tool_call_id="c1", name="plan",
                           arguments={"action": "create", "goal": "Ship", "phases": SAMPLE_PHASES}),
                ModelEvent("tool_call", tool_call_id="c2", name="plan",
                           arguments={"action": "start", "task_id": "t1"}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [ModelEvent("text", text="started"), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    list(manager.run(s["id"], "plan and start", SETTINGS))

    # t1 is in_progress on disk
    plan = PlanStore(cfg.root / "plan.json").load()
    assert plan.phases[0].tasks[0].status == "in_progress"

    # Simulate compaction: the context is compacted, but plan.json is untouched.
    # A new manager (or the same one on the next turn) rebuilds the system
    # prompt from disk and must see t1 still in_progress.
    backend2 = FakeBackend([[ModelEvent("text", text="continuing"), ModelEvent("done")]])
    manager2 = ChatManager(cfg, sessions, build_registry(cfg), SkillLoader(cfg), backend2)
    list(manager2.run(s["id"], "continue", SETTINGS))
    system_prompt = backend2.calls[0]["messages"][0]["content"]
    assert "Current plan" in system_prompt
    assert "t1 wire up module [in_progress]" in system_prompt


def test_restart_restores_task_state(cfg):
    # A process restart is equivalent to a fresh PlanStore instance reading
    # from disk. The in_progress task must be restored.
    backend = FakeBackend(
        [
            [
                ModelEvent("tool_call", tool_call_id="c1", name="plan",
                           arguments={"action": "create", "goal": "Ship", "phases": SAMPLE_PHASES}),
                ModelEvent("tool_call", tool_call_id="c2", name="plan",
                           arguments={"action": "start", "task_id": "t1"}),
                ModelEvent("tool_call", tool_call_id="c3", name="plan",
                           arguments={"action": "progress", "task_id": "t1", "pct": 60}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [ModelEvent("text", text="started"), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    list(manager.run(s["id"], "plan and start", SETTINGS))

    # Simulate a restart: new PlanStore instance reads from disk
    store = PlanStore(cfg.root / "plan.json")
    plan = store.load()
    assert plan is not None
    assert plan.phases[0].tasks[0].status == "in_progress"
    assert plan.phases[0].tasks[0].progress == 60
    assert plan.status == "active"


def test_system_prompt_shows_in_progress_for_auto_continue(cfg):
    # After compaction or restart, the system prompt must include the
    # in_progress task so the model automatically continues it. This is
    # the "auto-continuation" behavior: the model sees the current plan
    # state and knows which task to work on next.
    backend = FakeBackend(
        [
            [
                ModelEvent("tool_call", tool_call_id="c1", name="plan",
                           arguments={"action": "create", "goal": "Ship", "phases": SAMPLE_PHASES}),
                ModelEvent("tool_call", tool_call_id="c2", name="plan",
                           arguments={"action": "start", "task_id": "t1"}),
                ModelEvent("done", finish_reason="tool_calls"),
            ],
            [ModelEvent("text", text="started"), ModelEvent("done")],
        ]
    )
    sessions, manager = make_manager(cfg, backend)
    s = sessions.create("t")
    list(manager.run(s["id"], "plan and start", SETTINGS))

    # On the next turn, the system prompt must show t1 as in_progress
    backend2 = FakeBackend([[ModelEvent("text", text="continuing"), ModelEvent("done")]])
    manager2 = ChatManager(cfg, sessions, build_registry(cfg), SkillLoader(cfg), backend2)
    list(manager2.run(s["id"], "continue", SETTINGS))
    system_prompt = backend2.calls[0]["messages"][0]["content"]
    assert "Current plan" in system_prompt
    assert "in_progress" in system_prompt
    assert "t1 wire up module [in_progress]" in system_prompt
