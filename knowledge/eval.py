"""Eval runner: recall@k over the question set.

Questions live in data/knowledge/eval/questions.json:

    {"questions": [
        {"id": "plan-embed-model",
         "query": "which embedding model does the plan specify?",
         "expect": ["db-plan-final"]},
        ...
    ]}

``expect`` holds substrings of the source_path (or citation) of the
document that must appear in the top-k hits. A question passes if ANY
expected document is in the top-k. Results are written to
data/knowledge/eval/results-<timestamp>.json and a summary is printed.

Usage: python -m knowledge.eval [k]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .config import KnowledgeConfig
from .embed import Embedder
from .search import search
from .store import Index

DEFAULT_K = 5


def _load_questions(eval_dir: Path) -> list[dict]:
    path = eval_dir / "questions.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("questions", [])


def _hit_matches(hit, expect: list[str], paths: dict[str, str]) -> bool:
    source_path = paths.get(hit.doc_id, "")
    for e in expect:
        e = e.lower()
        if e in source_path.lower() or e in hit.citation.lower():
            return True
    return False


def run_eval(kcfg: KnowledgeConfig, k: int | None = None) -> dict:
    eval_dir = kcfg.eval_dir
    questions = _load_questions(eval_dir)
    if not questions:
        return {"error": f"no questions at {eval_dir / 'questions.json'}"}

    k = k or DEFAULT_K
    index = Index(kcfg)
    paths = {r["doc_id"]: r["source_path"] for r in index.documents()}
    results: list[dict] = []
    try:
        for q in questions:
            hits = search(kcfg, index, q["query"], k=k, embedder=Embedder(kcfg))
            top = [paths.get(h.doc_id, h.doc_id) for h in hits]
            passed = any(_hit_matches(h, q["expect"], paths) for h in hits)
            results.append(
                {
                    "id": q["id"],
                    "query": q["query"],
                    "expect": q["expect"],
                    "topic": q.get("topic", "general"),
                    "top_k": top,
                    "pass": passed,
                }
            )
    finally:
        index.close()

    n = len(results)
    passed_n = sum(1 for r in results if r["pass"])
    by_topic: dict[str, list[bool]] = {}
    for r in results:
        by_topic.setdefault(r["topic"], []).append(r["pass"])

    out = {
        "k": k,
        "recall_at_k": round(passed_n / n, 3) if n else 0.0,
        "passed": passed_n,
        "total": n,
        "by_topic": {
            t: round(sum(v) / len(v), 3) for t, v in sorted(by_topic.items())
        },
        "results": results,
    }
    if n:
        eval_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = eval_dir / f"results-{stamp}.json"
        path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        out["saved_to"] = str(path)
    return out


def main(k: int | None = None) -> int:
    import logging

    logging.basicConfig(level=logging.WARNING)
    from config import AppConfig, ensure_dirs

    cfg = AppConfig()
    ensure_dirs(cfg)
    kcfg = KnowledgeConfig(
        root=cfg.knowledge_dir,
        source_dir=cfg.knowledge_source_dir,
        models_dir=cfg.models_dir,
    )
    out = run_eval(kcfg, k=k)
    if "error" in out:
        print(out["error"])
        return 1
    print(f"recall@{out['k']} = {out['recall_at_k']:.0%} "
          f"({out['passed']}/{out['total']})")
    for t, v in out["by_topic"].items():
        print(f"  {t}: {v:.0%}")
    for r in out["results"]:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"  [{mark}] {r['id']}: {r['query']}")
        if not r["pass"]:
            for p in r["top_k"][:3]:
                print(f"         got: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else None))
