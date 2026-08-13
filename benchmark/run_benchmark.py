"""Extraction benchmark harness.

Compares every extractor's predictions in `extractions` against the human
labels in data/labeled_v1.jsonl. For each extractor:

  - per-field accuracy (fraction correct, treating missing as its own value)
  - macro_accuracy = mean of per-field accuracies
  - cost_per_1k_usd = mean cost per call * 1000
  - p50/p95 latency in ms

One row per extractor written to `benchmark_runs`. Also prints a comparison
table so you can eye the winner immediately.

Field notes:
  - tenant_pref, veg_only, is_owner, negotiable, lock_in_months: exact match.
    None-vs-value counts as wrong either direction.
  - amenities: treated as sets; correct iff set equality. (Jaccard could be
    added later as a soft score; exact-set is what our JSON schema promises.)

Usage:
    python -m benchmark.run_benchmark
    python -m benchmark.run_benchmark --labels data/labeled_v1.jsonl --eval-set holdout-v1
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from pathlib import Path
from typing import Any, Optional

from scrapers.db_utils import get_conn, utcnow_iso

FIELDS = ["tenant_pref", "veg_only", "is_owner", "negotiable",
          "lock_in_months", "amenities"]


def _load_labels(path: Path) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        out[int(rec["listing_id"])] = rec
    return out


def _fetch_extractions(conn: sqlite3.Connection,
                       listing_ids: list[int]) -> dict[tuple[str, int], dict[str, Any]]:
    """Returns { (extractor, listing_id) -> row dict }."""
    if not listing_ids:
        return {}
    placeholders = ",".join("?" * len(listing_ids))
    rows = conn.execute(
        f"""SELECT extractor, extractor_version, listing_id,
                   tenant_pref, veg_only, is_owner, negotiable,
                   lock_in_months, amenities_json, latency_ms, cost_usd
            FROM extractions WHERE listing_id IN ({placeholders})""",
        listing_ids,
    ).fetchall()
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for r in rows:
        out[(r["extractor"], r["listing_id"])] = {
            "extractor_version": r["extractor_version"],
            "tenant_pref": r["tenant_pref"],
            "veg_only": r["veg_only"],
            "is_owner": r["is_owner"],
            "negotiable": r["negotiable"],
            "lock_in_months": r["lock_in_months"],
            "amenities": set(json.loads(r["amenities_json"] or "[]")),
            "latency_ms": r["latency_ms"] or 0.0,
            "cost_usd": r["cost_usd"] or 0.0,
        }
    return out


def _score(pred: Any, gold: Any, field: str) -> int:
    if field == "amenities":
        p = set(pred) if pred is not None else set()
        g = set(gold) if gold is not None else set()
        return int(p == g)
    return int(pred == gold)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    k = (len(sv) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sv) - 1)
    frac = k - lo
    return sv[lo] + (sv[hi] - sv[lo]) * frac


def run(labels_path: Path, eval_set: str, notes: Optional[str]) -> None:
    labels = _load_labels(labels_path)
    if not labels:
        raise SystemExit(f"No labels found in {labels_path}")

    print(f"Loaded {len(labels)} labels from {labels_path}\n", flush=True)

    with get_conn() as conn:
        listing_ids = list(labels.keys())
        extractions = _fetch_extractions(conn, listing_ids)

        # Which extractors have data for these listings?
        extractors: dict[str, str] = {}
        for (ex, _), row in extractions.items():
            extractors.setdefault(ex, row["extractor_version"])

        if not extractors:
            raise SystemExit("No extractions found for labeled listings.")

        summary_rows: list[dict[str, Any]] = []
        for extractor, version in extractors.items():
            field_scores: dict[str, list[int]] = {f: [] for f in FIELDS}
            latencies: list[float] = []
            costs: list[float] = []
            n_examples = 0

            for lid, label in labels.items():
                pred = extractions.get((extractor, lid))
                if pred is None:
                    continue
                n_examples += 1
                latencies.append(pred["latency_ms"])
                costs.append(pred["cost_usd"])
                for field in FIELDS:
                    gold = label.get(field)
                    if field == "amenities":
                        gold = gold or []
                    field_scores[field].append(_score(pred[field], gold, field))

            if n_examples == 0:
                continue

            field_accs = {
                f: (sum(v) / len(v)) if v else 0.0
                for f, v in field_scores.items()
            }
            macro_acc = statistics.mean(field_accs.values())
            mean_cost = statistics.mean(costs) if costs else 0.0
            summary_rows.append({
                "extractor": extractor,
                "extractor_version": version,
                "n_examples": n_examples,
                "field_accuracies": field_accs,
                "macro_accuracy": macro_acc,
                "cost_per_1k_usd": mean_cost * 1000.0,
                "p50_latency_ms": _percentile(latencies, 0.50),
                "p95_latency_ms": _percentile(latencies, 0.95),
            })

        # ---- print table ---------------------------------------------------
        print(f"{'field':<18}", *[f"{r['extractor']:>16}" for r in summary_rows])
        print("-" * (18 + 17 * len(summary_rows)))
        for field in FIELDS:
            row = [f"{field:<18}"]
            for r in summary_rows:
                row.append(f"{r['field_accuracies'][field]*100:>15.1f}%")
            print(*row)
        print("-" * (18 + 17 * len(summary_rows)))
        for label, key in [("macro accuracy", "macro_accuracy"),
                           ("cost / 1k USD", "cost_per_1k_usd"),
                           ("p50 latency ms", "p50_latency_ms"),
                           ("p95 latency ms", "p95_latency_ms")]:
            row = [f"{label:<18}"]
            for r in summary_rows:
                v = r[key]
                if "accuracy" in key:
                    row.append(f"{v*100:>15.1f}%")
                elif "cost" in key:
                    row.append(f"{'$'+format(v,'.4f'):>16}")
                else:
                    row.append(f"{v:>15.1f}")
            print(*row)

        # ---- write to benchmark_runs --------------------------------------
        for r in summary_rows:
            conn.execute(
                "INSERT INTO benchmark_runs "
                "(ran_at, extractor, extractor_version, eval_set, n_examples, "
                " field_accuracies_json, macro_accuracy, cost_per_1k_usd, "
                " p50_latency_ms, p95_latency_ms, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (utcnow_iso(), r["extractor"], r["extractor_version"], eval_set,
                 r["n_examples"], json.dumps(r["field_accuracies"]),
                 r["macro_accuracy"], r["cost_per_1k_usd"],
                 r["p50_latency_ms"], r["p95_latency_ms"], notes),
            )

    print(f"\nWrote {len(summary_rows)} rows to benchmark_runs (eval_set={eval_set!r})",
          flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="data/labeled_v1.jsonl")
    ap.add_argument("--eval-set", default="holdout-v1")
    ap.add_argument("--notes", default=None)
    args = ap.parse_args()
    run(Path(args.labels), args.eval_set, args.notes)


if __name__ == "__main__":
    main()
