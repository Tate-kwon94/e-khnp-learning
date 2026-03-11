#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from rag_solver import RagExamSolver


def _load_dataset(answer_bank_path: Path, limit: int) -> list[dict[str, Any]]:
    raw = json.loads(answer_bank_path.read_text(encoding="utf-8"))
    items = raw.get("items")
    if not isinstance(items, dict):
        return []
    rows: list[dict[str, Any]] = []
    for item in items.values():
        if not isinstance(item, dict):
            continue
        q = str(item.get("question", "")).strip()
        opts = [str(x).strip() for x in item.get("options", []) if str(x).strip()]
        try:
            ans = int(item.get("answer_index", 0))
        except Exception:  # noqa: BLE001
            ans = 0
        if not q or len(opts) < 2 or ans < 1 or ans > len(opts):
            continue
        rows.append({"question": q, "options": opts, "answer_index": ans})
        if len(rows) >= limit:
            break
    return rows


def _to_float_list(raw: str) -> list[float]:
    out: list[float] = []
    for p in str(raw).split(","):
        p = p.strip()
        if not p:
            continue
        out.append(float(p))
    return out


def _simulate_chain(
    per_model: dict[str, dict[str, Any]],
    model_chain: list[str],
    threshold: float,
    margin: float,
) -> tuple[int, float, bool, str]:
    primary = model_chain[0]
    picked = per_model.get(primary, {})
    choice = int(picked.get("choice", 0) or 0)
    conf = float(picked.get("confidence", 0.0) or 0.0)
    switched = False
    picked_model = primary

    if conf < threshold:
        for alt in model_chain[1:]:
            cand = per_model.get(alt, {})
            alt_choice = int(cand.get("choice", 0) or 0)
            alt_conf = float(cand.get("confidence", 0.0) or 0.0)
            if alt_choice <= 0:
                continue
            improved = alt_conf >= (conf + margin)
            if alt_conf >= threshold or improved:
                choice = alt_choice
                conf = alt_conf
                switched = True
                picked_model = alt
                if alt_conf >= threshold:
                    break
    return choice, conf, switched, picked_model


def main() -> int:
    parser = argparse.ArgumentParser(description="Threshold/margin sweep for model switching chain")
    parser.add_argument("--index-path", default="rag/index.json")
    parser.add_argument("--answer-bank-path", default="rag/exam_answer_bank.json")
    parser.add_argument("--embed-model", default="nomic-embed-text")
    parser.add_argument("--ollama-base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--models", default="qwen2.5:3b,qwen2.5:7b,anpigon/eeve-korean-10.8b")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--thresholds", default="0.58,0.60,0.62,0.65")
    parser.add_argument("--margins", default="0.05,0.08,0.10")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    model_chain = [m.strip() for m in str(args.models).split(",") if m.strip()]
    if len(model_chain) < 2:
        raise RuntimeError("Need at least 2 models in chain")

    index_path = Path(args.index_path)
    answer_bank_path = Path(args.answer_bank_path)
    if not index_path.exists():
        raise FileNotFoundError(f"index not found: {index_path}")
    if not answer_bank_path.exists():
        raise FileNotFoundError(f"answer bank not found: {answer_bank_path}")

    dataset = _load_dataset(answer_bank_path, limit=max(1, int(args.limit)))
    if not dataset:
        raise RuntimeError("No dataset rows")

    solver = RagExamSolver(
        index_path=str(index_path),
        generate_model=model_chain[0],
        generate_fallback_models=model_chain[1:],
        embed_model=args.embed_model,
        ollama_base_url=args.ollama_base_url,
        web_search_enabled=True,
        web_top_n=4,
        web_timeout_sec=8,
        web_weight=0.35,
    )

    per_question: list[dict[str, Any]] = []
    for idx, row in enumerate(dataset, start=1):
        q = row["question"]
        opts = row["options"]
        ans = int(row["answer_index"])
        by_model: dict[str, dict[str, Any]] = {}
        for model in model_chain:
            try:
                dec = solver.solve(question=q, options=opts, top_k=max(1, int(args.top_k)), preferred_models=[model])
                by_model[model] = {
                    "choice": int(getattr(dec, "choice", 0)),
                    "confidence": float(getattr(dec, "confidence", 0.0)),
                }
            except Exception as exc:  # noqa: BLE001
                by_model[model] = {"choice": 0, "confidence": 0.0, "error": str(exc)[:220]}
        per_question.append(
            {
                "idx": idx,
                "answer_index": ans,
                "per_model": by_model,
            }
        )
        print(f"solved {idx}/{len(dataset)}")

    thresholds = _to_float_list(args.thresholds)
    margins = _to_float_list(args.margins)
    sweep_rows: list[dict[str, Any]] = []
    for th in thresholds:
        for mg in margins:
            attempted = 0
            accepted = 0
            correct_all = 0
            correct_accepted = 0
            switched_count = 0
            pick_count_by_model: dict[str, int] = {m: 0 for m in model_chain}
            for row in per_question:
                gt = int(row["answer_index"])
                choice, conf, switched, picked_model = _simulate_chain(
                    per_model=row["per_model"],
                    model_chain=model_chain,
                    threshold=float(th),
                    margin=float(mg),
                )
                attempted += 1
                pick_count_by_model[picked_model] = int(pick_count_by_model.get(picked_model, 0)) + 1
                if switched:
                    switched_count += 1
                if choice == gt:
                    correct_all += 1
                if conf >= th:
                    accepted += 1
                    if choice == gt:
                        correct_accepted += 1
            sweep_rows.append(
                {
                    "threshold": round(float(th), 4),
                    "margin": round(float(mg), 4),
                    "attempted": attempted,
                    "accepted": accepted,
                    "rejected": attempted - accepted,
                    "accept_rate": round((accepted / attempted) if attempted else 0.0, 6),
                    "accuracy_all": round((correct_all / attempted) if attempted else 0.0, 6),
                    "accuracy_accepted": round((correct_accepted / accepted) if accepted else 0.0, 6),
                    "switched_count": switched_count,
                    "picked_models": pick_count_by_model,
                }
            )

    payload = {
        "meta": {
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "index_path": str(index_path),
            "answer_bank_path": str(answer_bank_path),
            "dataset_size": len(dataset),
            "models": model_chain,
            "top_k": int(args.top_k),
            "thresholds": thresholds,
            "margins": margins,
        },
        "sweep": sweep_rows,
    }
    if args.report_path:
        out = Path(args.report_path)
    else:
        out = Path("logs") / f"conf_sweep_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report_path={out}")
    best = sorted(sweep_rows, key=lambda x: (x["accuracy_accepted"], x["accept_rate"]), reverse=True)[0]
    print(
        "best:",
        f"threshold={best['threshold']}",
        f"margin={best['margin']}",
        f"acc_accept={best['accuracy_accepted']}",
        f"accept_rate={best['accept_rate']}",
        f"switched={best['switched_count']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
