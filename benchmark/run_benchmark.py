"""Extraction benchmark harness.

Compares every extractor's predictions in `extractions` against labels in
data/labeled_v1.jsonl. For each extractor:

  - per-field accuracy (fraction correct, treating missing as its own value)
  - macro_accuracy = mean of per-field accuracies
  - cost_per_1k_usd = mean cost per call * 1000
  - p50/p95 latency in ms

Each label carries a `label_source` tag:
  - "human"         — a person read the listing and typed the label
  - "silver-gemini" — the user accepted Gemini's guess as-is at label time
                      (fine for coverage; NOT a fair test for Gemini itself,
                       since Gemini would be graded against its own answers)

The harness prints TWO tables:
  1. HUMAN-ONLY   — the honest number for a resume/writeup
  2. FULL (all)   — includes silver labels; useful for coverage but self-
                    labels Gemini, so its score is inflated. Reported for
                    transparency.

Each table also writes one row per extractor to `benchmark_runs`, with
distinct `eval_set` values so a future dashboard can chart them separately.

Field notes:
  - tenant_pref, veg_only, is_owner, negotiable, lock_in_months: exact match.
    None-vs-value counts as wrong either direction.
  - amenities: treated as sets; correct iff set equality.

Usage:
    python -m benchmark.run_benchmark
    python -m benchmark.run_benchmark --labels data/labeled_v1.jsonl
    python -m benchmark.run_benchmark --only human      # skip the full table
    python -m benchmark.run_benchmark --only all        # skip the human table
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


def _score_extractors(
    conn: sqlite3.Connection,
    labels: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Score every extractor against the given label subset. Returns one
    summary dict per extractor with at least one prediction in the subset."""
    listing_ids = list(labels.keys())
    extractions = _fetch_extractions(conn, listing_ids)

    extractors: dict[str, str] = {}
    for (ex, _), row in extractions.items():
        extractors.setdefault(ex, row["extractor_version"])

    out: list[dict[str, Any]] = []
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
        out.append({
            "extractor": extractor,
            "extractor_version": version,
            "n_examples": n_examples,
            "field_accuracies": field_accs,
            "macro_accuracy": macro_acc,
            "cost_per_1k_usd": mean_cost * 1000.0,
            "p50_latency_ms": _percentile(latencies, 0.50),
            "p95_latency_ms": _percentile(latencies, 0.95),
        })
    return out


def _print_table(summary_rows: list[dict[str, Any]]) -> None:
    if not summary_rows:
        print("  (no extractors have predictions for this subset)")
        return
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


def _persist(conn: sqlite3.Connection, summary_rows: list[dict[str, Any]],
             eval_set: str, notes: Optional[str]) -> None:
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


def run(labels_path: Path, only: str, notes: Optional[str]) -> None:
    labels = _load_labels(labels_path)
    if not labels:
        raise SystemExit(f"No labels found in {labels_path}")

    human_labels = {lid: r for lid, r in labels.items()
                    if r.get("label_source") == "human"}
    silver_labels = {lid: r for lid, r in labels.items()
                     if r.get("label_source") == "silver-gemini"}

    from collections import Counter
    counts = Counter(r.get("label_source", "unspecified") for r in labels.values())
    print(f"Loaded {len(labels)} labels from {labels_path}: {dict(counts)}\n",
          flush=True)

    do_human = only in ("both", "human")
    do_full = only in ("both", "all")

    written = 0
    with get_conn() as conn:
        if do_human:
            print("=" * 70)
            print(f"HUMAN-ONLY BENCHMARK  (n={len(human_labels)} labels)")
            print("Honest number for external reporting.")
            print("=" * 70)
            if human_labels:
                rows_h = _score_extractors(conn, human_labels)
                _print_table(rows_h)
                _persist(conn, rows_h, "holdout-v1-human", notes)
                written += len(rows_h)
            else:
                print("  (no human-labeled rows in this file)")
            print()

        if do_full:
            print("=" * 70)
            print(f"FULL BENCHMARK  (n={len(labels)} labels: "
                  f"{len(human_labels)} human + {len(silver_labels)} silver-gemini)")
            print("Coverage view. Gemini's score is inflated because silver labels")
            print("were derived from Gemini's own predictions — treat with care.")
            print("=" * 70)
            rows_f = _score_extractors(conn, labels)
            _print_table(rows_f)
            _persist(conn, rows_f, "holdout-v1-full", notes)
            written += len(rows_f)

    print(f"\nWrote {written} rows to benchmark_runs.", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="data/labeled_v1.jsonl")
    ap.add_argument("--only", default="both",
                    choices=["both", "human", "all"],
                    help="Which table(s) to print/persist. Default: both.")
    ap.add_argument("--notes", default=None)
    args = ap.parse_args()
    run(Path(args.labels), args.only, args.notes)


if __name__ == "__main__":
    main()
