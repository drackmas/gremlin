"""Planning store: persistence, dependencies, transitions, revision, checkpoints."""

from __future__ import annotations

import json
import subprocess

import pytest

from planning.store import (
    PLAN_ABANDONED,
    PLAN_ACTIVE,
    PLAN_COMPLETED,
    TASK_BLOCKED,
    TASK_DONE,
    TASK_IN_PROGRESS,
    TASK_PENDING,
    TASK_SKIPPED,
    Checkpoint,
    PlanError,
    PlanStore,
    phase_status,
    plan_progress,
    render_plan,
    task_by_id,
)

SAMPLE = [
    {"title": "Scaffold", "tasks": [
        {"title": "wire up module"},
        {"title": "write unit tests", "depends_on": ["t1"]},
    ]},
    {"title": "Polish", "tasks": [
        {"title": "docs", "depends_on": ["t2"]},
        {"title": "cleanup"},
        {"title": "integration tests", "depends_on": ["t3", "t4"]},
    ]},
]


def make_store(tmp_path):
    return PlanStore(tmp_path / "plan.json")


def create_plan(store):
    return store.create("Add a feature", SAMPLE)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_create_writes_valid_json_and_reloads(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    path = tmp_path / "plan.json"
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["goal"] == "Add a feature"
    assert raw["status"] == PLAN_ACTIVE
    assert raw["revision"] == 0
    assert raw["created"] and raw["updated"]

    reloaded = PlanStore(path).load()
    assert reloaded.goal == "Add a feature"
    assert [t.id for p in reloaded.phases for t in p.tasks] == ["t1", "t2", "t3", "t4", "t5"]
    assert reloaded.phases[0].tasks[1].depends_on == ["t1"]
    assert all(t.status == TASK_PENDING for p in reloaded.phases for t in p.tasks)


def test_load_missing_returns_none(tmp_path):
    store = make_store(tmp_path)
    assert store.exists() is False
    assert store.load() is None
    assert store.summarize() is None


def test_load_corrupt_raises_and_summarize_is_safe(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("not json", encoding="utf-8")
    store = PlanStore(path)
    with pytest.raises(PlanError):
        store.load()
    # The system-prompt path must never break a turn on a bad plan.json.
    assert store.summarize() is None


def test_load_invalid_status_raises(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"goal": "g", "status": "bogus"}), encoding="utf-8")
    with pytest.raises(PlanError):
        PlanStore(path).load()


def test_roundtrip_preserves_all_fields(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    store.start("t1", "started")
    store.set_progress("t1", 40)
    store.record_test("t1", "pytest -q", True, "3 passed")
    store.block("t3", "waiting on design")
    plan = store.require()
    plan.checkpoints.append(Checkpoint("abc12345678901", "manual note"))

    reloaded = PlanStore(tmp_path / "plan.json").load()
    t1 = task_by_id(reloaded, "t1")
    assert t1.status == TASK_IN_PROGRESS
    assert t1.progress == 40
    assert t1.notes == ["started"]
    assert t1.test is not None and t1.test.command == "pytest -q" and t1.test.ok and t1.test.summary == "3 passed"
    t3 = task_by_id(reloaded, "t3")
    assert t3.status == TASK_BLOCKED and t3.notes == ["waiting on design"]


# ---------------------------------------------------------------------------
# Create validation
# ---------------------------------------------------------------------------


def test_create_requires_goal_and_phases(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(PlanError):
        store.create("", SAMPLE)
    with pytest.raises(PlanError):
        store.create("g", [])
    with pytest.raises(PlanError):
        store.create("g", None)


def test_create_validates_phase_structure(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(PlanError):
        store.create("g", [{"title": "", "tasks": [{"title": "x"}]}])
    with pytest.raises(PlanError):
        store.create("g", [{"title": "A", "tasks": []}])
    with pytest.raises(PlanError):
        store.create("g", [{"title": "A", "tasks": [{"title": ""}]}])
    with pytest.raises(PlanError):
        store.create("g", [{"title": "A", "tasks": [{"title": "x", "id": "t9"}, {"title": "y", "id": "t9"}]}])


def test_create_validates_dependencies(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(PlanError, match="unknown task"):
        store.create("g", [{"title": "A", "tasks": [{"title": "x", "depends_on": ["nope"]}]}])
    with pytest.raises(PlanError, match="itself"):
        store.create("g", [{"title": "A", "tasks": [{"title": "x", "id": "a", "depends_on": ["a"]}]}])
    with pytest.raises(PlanError, match="cycle"):
        store.create(
            "g",
            [{"title": "A", "tasks": [
                {"title": "x", "id": "a", "depends_on": ["b"]},
                {"title": "y", "id": "b", "depends_on": ["a"]},
            ]}],
        )


def test_create_rejected_while_active_allowed_after_abandon(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    with pytest.raises(PlanError, match="active plan"):
        create_plan(store)
    store.abandon("done in another way")
    p2 = create_plan(store)
    assert p2.status == PLAN_ACTIVE
    assert p2.revision == 0


# ---------------------------------------------------------------------------
# Dependencies & transitions
# ---------------------------------------------------------------------------


def test_dependency_gating_on_start(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    with pytest.raises(PlanError, match="dependency t1"):
        store.start("t2")
    store.start("t1")
    store.complete("t1")
    store.start("t2")  # dep satisfied
    plan = store.require()
    assert task_by_id(plan, "t2").status == TASK_IN_PROGRESS


def test_dependency_satisfied_by_skip(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    store.skip("t1")
    store.start("t2")  # skipped dep counts as satisfied
    assert task_by_id(store.require(), "t2").status == TASK_IN_PROGRESS


def test_mandatory_dependency_chain(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    # t5 needs t3 and t4; t3 needs t2; t2 needs t1.
    with pytest.raises(PlanError):
        store.start("t5")
    for tid in ("t1", "t4"):
        store.start(tid)
        store.complete(tid)
    store.start("t2")
    store.complete("t2")
    store.start("t3")
    store.complete("t3")
    store.start("t5")
    assert task_by_id(store.require(), "t5").status == TASK_IN_PROGRESS


def test_transition_rules(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    # direct complete from pending is allowed
    store.complete("t4")
    plan = store.require()
    t4 = task_by_id(plan, "t4")
    assert t4.status == TASK_DONE and t4.progress == 100
    with pytest.raises(PlanError, match="from done"):
        store.start("t4")
    with pytest.raises(PlanError, match="from done"):
        store.complete("t4")
    # block requires a note
    with pytest.raises(PlanError, match="note"):
        store.block("t1", "")
    store.block("t1", "waiting on API")
    assert task_by_id(store.require(), "t1").status == TASK_BLOCKED
    with pytest.raises(PlanError, match="from blocked"):
        store.complete("t1")
    store.skip("t1")
    store.reset("t1")
    t1 = task_by_id(store.require(), "t1")
    assert t1.status == TASK_PENDING and t1.progress == 0
    with pytest.raises(PlanError, match="already pending"):
        store.reset("t1")
    store.start("t1")  # works again after reset


def test_unknown_task_id(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    with pytest.raises(PlanError, match="unknown task id"):
        store.start("t99")


def test_mutations_blocked_when_plan_not_active(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    store.abandon("nope")
    with pytest.raises(PlanError, match="abandoned"):
        store.start("t1")
    with pytest.raises(PlanError, match="abandoned"):
        store.set_progress("t1", 10)
    with pytest.raises(PlanError, match="abandoned"):
        store.record_test("t1", "pytest", True)


def test_set_progress_clamps_and_guards(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    store.set_progress("t1", 150)
    assert task_by_id(store.require(), "t1").progress == 100
    store.set_progress("t1", -5)
    assert task_by_id(store.require(), "t1").progress == 0
    store.set_progress("t1", 55)
    assert task_by_id(store.require(), "t1").progress == 55
    store.complete("t1")
    with pytest.raises(PlanError):
        store.set_progress("t1", 10)


def test_record_test_stores_result(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    store.record_test("t2", "pytest -q tests/test_x.py", False, "1 failed")
    t2 = task_by_id(store.require(), "t2")
    assert t2.test is not None
    assert t2.test.ok is False and t2.test.summary == "1 failed"
    # a failed test does not change task status
    assert t2.status == TASK_PENDING
    store.record_test("t2", "pytest -q tests/test_x.py", True, "4 passed")
    t2 = task_by_id(store.require(), "t2")
    assert t2.test.ok is True
    # recording results does not change task status
    store.record_test("t1", "pytest", True)
    assert task_by_id(store.require(), "t1").status == TASK_PENDING
    store.skip("t1")
    with pytest.raises(PlanError, match="skipped"):
        store.record_test("t1", "pytest", True)


def test_progress_stats_and_phase_status(tmp_path):
    store = make_store(tmp_path)
    plan = create_plan(store)
    done, total, pct = plan_progress(plan)
    assert (done, total, pct) == (0, 5, 0)
    assert phase_status(plan.phases[0]) == TASK_PENDING

    store.start("t1")
    assert phase_status(store.require().phases[0]) == TASK_IN_PROGRESS
    store.complete("t1")
    done, total, pct = plan_progress(store.require())
    assert (done, total, pct) == (1, 5, 20)

    store.block("t2", "x")
    assert phase_status(store.require().phases[0]) == TASK_BLOCKED

    # finish the rest: phase 1 done, phase 2 all skipped
    store.skip("t2")
    for tid in ("t3", "t4", "t5"):
        store.skip(tid)
    plan = store.require()
    assert phase_status(plan.phases[0]) == TASK_DONE
    assert phase_status(plan.phases[1]) == TASK_SKIPPED
    done, total, pct = plan_progress(plan)
    assert (done, total, pct) == (5, 5, 100)


# ---------------------------------------------------------------------------
# Revision & lifecycle
# ---------------------------------------------------------------------------


def test_revise_bumps_revision_and_records_reason(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    store.checkpoint = None  # ensure no checkpoints yet
    plan = store.revise("tests revealed the scaffold is wrong")
    assert plan.revision == 1
    assert plan.status == PLAN_ACTIVE
    assert plan.notes[-1] == "revision 1: tests revealed the scaffold is wrong"
    # structure untouched when not provided
    assert [t.id for p in plan.phases for t in p.tasks] == ["t1", "t2", "t3", "t4", "t5"]


def test_revise_replaces_structure_and_preserves_checkpoints(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    _init_repo(tmp_path)
    _, commit = store.checkpoint("before rework")
    plan = store.revise(
        "scoping was wrong",
        goal="Add a smaller feature",
        phases_in=[{"title": "Only", "tasks": [{"title": "one task"}]}],
    )
    assert plan.revision == 1
    assert plan.goal == "Add a smaller feature"
    assert [t.id for p in plan.phases for t in p.tasks] == ["t1"]
    assert [c.commit for c in plan.checkpoints] == [commit]
    with pytest.raises(PlanError, match="unknown task"):
        task_by_id(plan, "t5")
    reloaded = PlanStore(tmp_path / "plan.json").load()
    assert reloaded.revision == 1 and reloaded.checkpoints[0].commit == commit


def test_revise_requires_reason_and_revives_finished_plans(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    with pytest.raises(PlanError, match="reason"):
        store.revise("")
    for tid in ("t1", "t2", "t3", "t4", "t5"):
        store.skip(tid)
    store.finish()
    assert store.require().status == PLAN_COMPLETED
    plan = store.revise("found a bug in the shipped feature")
    assert plan.status == PLAN_ACTIVE and plan.revision == 1


def test_finish_requires_all_tasks_complete(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    with pytest.raises(PlanError, match="cannot finish"):
        store.finish()
    for tid in ("t1", "t4"):
        store.complete(tid)
    with pytest.raises(PlanError, match="cannot finish"):
        store.finish()
    for tid in ("t2", "t3", "t5"):
        store.skip(tid)
    plan = store.finish()
    assert plan.status == PLAN_COMPLETED
    with pytest.raises(PlanError, match="already completed"):
        store.finish()
    # a finished plan can be replaced by a fresh create
    p2 = store.create("Next thing", SAMPLE)
    assert p2.status == PLAN_ACTIVE and p2.revision == 0


def test_abandon_requires_reason(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    with pytest.raises(PlanError, match="reason"):
        store.abandon("")
    plan = store.abandon("user changed direction")
    assert plan.status == PLAN_ABANDONED
    assert "abandoned: user changed direction" in plan.notes
    with pytest.raises(PlanError, match="already abandoned"):
        store.abandon("again")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_summarize_contains_goal_and_tasks(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    text = store.summarize()
    assert "goal: Add a feature" in text
    assert "t1 wire up module [pending]" in text
    assert "progress: 0/5 (0%)" in text


def test_view_shows_full_state(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    store.start("t1")
    store.record_test("t1", "pytest -q", False, "2 failed")
    text = render_plan(store.require())
    assert "plan: Add a feature" in text
    assert "t1 wire up module [in_progress]" in text
    assert "FAILED `pytest -q`" in text
    assert "deps: t1" in text


def test_render_md_writes_file(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    store.complete("t4")
    store.block("t1", "waiting")
    md_path = store.render()
    text = (tmp_path / "plan.md").read_text(encoding="utf-8")
    assert md_path == str(tmp_path / "plan.md")
    assert "# Plan: Add a feature" in text
    assert "## Scaffold (p1) — blocked" in text
    assert "- [ ] **t2** write unit tests (deps: t1)" in text
    assert "- [x] **t4** cleanup" in text
    assert "- [b] **t1** wire up module" in text
    assert "  - waiting" in text


# ---------------------------------------------------------------------------
# Git checkpoints
# ---------------------------------------------------------------------------


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _init_repo(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "gremlin@test")
    _git(root, "config", "user.name", "Gremlin")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")


def test_checkpoint_commits_dirty_tree(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    _init_repo(tmp_path)
    (tmp_path / "change.txt").write_text("new work\n", encoding="utf-8")
    plan, commit = store.checkpoint("before self-modification")
    assert len(plan.checkpoints) == 1
    assert plan.checkpoints[0].message == "before self-modification"
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    assert commit == head
    # the working tree change was committed into the checkpoint
    tracked = _git(tmp_path, "show", "--name-only", "--format=", head).stdout.split()
    assert "change.txt" in tracked
    # only the plan.json checkpoint record dirties the tree again
    assert _git(tmp_path, "status", "--porcelain").stdout.strip() == "M plan.json"
    # persisted in plan.json
    reloaded = PlanStore(tmp_path / "plan.json").load()
    assert reloaded.checkpoints[0].commit == commit


def test_checkpoint_on_clean_tree_records_head_without_new_commit(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    _init_repo(tmp_path)
    before = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _, commit = store.checkpoint("no changes yet")
    assert commit == before
    count = _git(tmp_path, "rev-list", "--count", "HEAD").stdout.strip()
    assert count == "1"  # only the baseline commit
    assert len(store.require().checkpoints) == 1


def test_checkpoint_requires_git_repo(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    with pytest.raises(PlanError, match="git repository"):
        store.checkpoint("no repo here")


def test_checkpoint_requires_message(tmp_path):
    store = make_store(tmp_path)
    create_plan(store)
    _init_repo(tmp_path)
    with pytest.raises(PlanError, match="message"):
        store.checkpoint("   ")
def test_at_most_one_in_progress(tmp_path):
    """Starting a second task while one is in_progress raises PlanError."""
    store = make_store(tmp_path)
    create_plan(store)
    _init_repo(tmp_path)

    store.start("t1")
    assert store.require().phases[0].tasks[0].status == TASK_IN_PROGRESS

    with pytest.raises(PlanError, match="already in_progress"):
        store.start("t2")


def test_reset_in_progress_allows_starting_another(tmp_path):
    """Resetting the in_progress task allows starting a different one."""
    store = make_store(tmp_path)
    create_plan(store)
    _init_repo(tmp_path)

    store.start("t1")
    store.reset("t1")
    assert store.require().phases[0].tasks[0].status == TASK_PENDING

    store.start("t4")
    assert store.require().phases[1].tasks[1].status == TASK_IN_PROGRESS


def test_summarize_shows_current_and_next_eligible(tmp_path):
    """summarize() includes CURRENT and NEXT ELIGIBLE lines."""
    store = make_store(tmp_path)
    create_plan(store)
    _init_repo(tmp_path)

    # All pending: t1 and t4 (no deps) are eligible.
    text = store.summarize()
    assert "t1 wire up module" in text
    assert "t4 cleanup" in text

    # Start t1: it becomes CURRENT, and t4 is still the only eligible.
    store.start("t1")
    text = store.summarize()
    assert "CURRENT: t1 wire up module [in_progress]" in text
    assert "t4 cleanup" in text

    # Complete t1: t2 (depends on t1) becomes eligible alongside t4.
    store.complete("t1")
    text = store.summarize()
    assert "CURRENT:" not in text  # nothing in_progress
    assert "t2 write unit tests" in text
    assert "t4 cleanup" in text
