#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from rag_solver import RagExamSolver


def _now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _load_answer_items(answer_bank_path: Path, limit: int) -> list[dict[str, Any]]:
    raw = json.loads(answer_bank_path.read_text(encoding="utf-8"))
    items = raw.get("items")
    if not isinstance(items, dict):
        return []
    out: list[dict[str, Any]] = []
    for _, item in items.items():
        if not isinstance(item, dict):
            continue
        q = str(item.get("question", "")).strip()
        options = item.get("options", [])
        try:
            answer_index = int(item.get("answer_index", 0))
        except Exception:  # noqa: BLE001
            answer_index = 0
        if not q or not isinstance(options, list):
            continue
        opts = [str(x).strip() for x in options if str(x).strip()]
        if len(opts) < 2 or answer_index < 1 or answer_index > len(opts):
            continue
        out.append({"question": q, "options": opts, "answer_index": answer_index})
        if len(out) >= limit:
            break
    return out


def _eval_one_model(
    *,
    model: str,
    index_path: Path,
    embed_model: str,
    ollama_base_url: str,
    top_k: int,
    web_search_enabled: bool,
    web_top_n: int,
    web_timeout_sec: int,
    web_weight: float,
    dataset: list[dict[str, Any]],
) -> dict[str, Any]:
    solver = RagExamSolver(
        index_path=str(index_path),
        generate_model=model,
        generate_fallback_models=[],
        embed_model=embed_model,
        ollama_base_url=ollama_base_url,
        web_search_enabled=web_search_enabled,
        web_top_n=web_top_n,
        web_timeout_sec=web_timeout_sec,
        web_weight=web_weight,
    )

    total = len(dataset)
    attempted = 0
    correct = 0
    errors = 0
    confs: list[float] = []
    wrong_samples: list[dict[str, Any]] = []
    error_samples: list[dict[str, str]] = []
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    for idx, row in enumerate(dataset, start=1):
        q = row["question"]
        opts = row["options"]
        gt = int(row["answer_index"])
        try:
            decision = solver.solve(question=q, options=opts, top_k=top_k)
            choice = int(getattr(decision, "choice", 0))
            conf = float(getattr(decision, "confidence", 0.0))
            attempted += 1
            confs.append(conf)
            if choice == gt:
                correct += 1
            elif len(wrong_samples) < 5:
                wrong_samples.append(
                    {
                        "idx": idx,
                        "choice": choice,
                        "correct": gt,
                        "confidence": round(conf, 4),
                        "question": q[:220],
                    }
                )
            print(
                f"[{model}] {idx}/{total} choice={choice} gt={gt} "
                f"conf={conf:.3f} ok={choice == gt}"
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if len(error_samples) < 5:
                error_samples.append({"idx": str(idx), "error": str(exc)[:300]})
            print(f"[{model}] {idx}/{total} ERROR: {exc}")
    finished_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    accuracy = (correct / attempted) if attempted else 0.0
    return {
        "model": model,
        "started_at": started_at,
        "finished_at": finished_at,
        "dataset_size": total,
        "attempted": attempted,
        "correct": correct,
        "errors": errors,
        "accuracy": round(accuracy, 6),
        "avg_confidence": round(mean(confs), 6) if confs else 0.0,
        "wrong_samples": wrong_samples,
        "error_samples": error_samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B check for RAG generate models using answer bank questions")
    parser.add_argument("--index-path", default="rag/index.json")
    parser.add_argument("--answer-bank-path", default="rag/exam_answer_bank.json")
    parser.add_argument("--embed-model", default="nomic-embed-text")
    parser.add_argument("--ollama-base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--models", default="qwen2.5:7b,qwen2.5:3b")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--web-search-enabled", action="store_true", default=False)
    parser.add_argument("--web-top-n", type=int, default=4)
    parser.add_argument("--web-timeout-sec", type=int, default=8)
    parser.add_argument("--web-weight", type=float, default=0.35)
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    index_path = Path(args.index_path)
    answer_bank_path = Path(args.answer_bank_path)
    models = [m.strip() for m in str(args.models).split(",") if m.strip()]
    limit = max(1, int(args.limit))
    top_k = max(1, int(args.top_k))
    if not index_path.exists():
        raise FileNotFoundError(f"index not found: {index_path}")
    if not answer_bank_path.exists():
        raise FileNotFoundError(f"answer bank not found: {answer_bank_path}")
    dataset = _load_answer_items(answer_bank_path, limit=limit)
    if not dataset:
        raise RuntimeError("no valid dataset rows from answer bank")

    results: list[dict[str, Any]] = []
    for model in models:
        results.append(
            _eval_one_model(
                model=model,
                index_path=index_path,
                embed_model=args.embed_model,
                ollama_base_url=args.ollama_base_url,
                top_k=top_k,
                web_search_enabled=bool(args.web_search_enabled),
                web_top_n=max(1, int(args.web_top_n)),
                web_timeout_sec=max(3, int(args.web_timeout_sec)),
                web_weight=float(args.web_weight),
                dataset=dataset,
            )
        )

    payload = {
        "meta": {
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "index_path": str(index_path),
            "answer_bank_path": str(answer_bank_path),
            "limit": limit,
            "top_k": top_k,
            "web_search_enabled": bool(args.web_search_enabled),
            "web_top_n": int(args.web_top_n),
            "web_timeout_sec": int(args.web_timeout_sec),
            "web_weight": float(args.web_weight),
            "dataset_size": len(dataset),
        },
        "results": results,
    }

    if args.report_path:
        out_path = Path(args.report_path)
    else:
        out_path = Path("logs") / f"model_ab_report_{_now_stamp()}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report_path={out_path}")
    for r in results:
        print(
            f"model={r['model']} accuracy={r['accuracy']:.4f} attempted={r['attempted']} "
            f"errors={r['errors']} avg_conf={r['avg_confidence']:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
