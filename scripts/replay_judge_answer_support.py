"""Offline replay of the judge's ANSWER-SUPPORT head against captured transcripts.

Purpose (2026-07-25, pre-calibration): the live repeat-eval showed 0/6 tickers
stable, but live churn mixes TWO variance sources — the generator writes different
claims each run, and the judge grades the same claims differently. This harness
isolates the second: re-judge a FIXED captured answer+evidence N times and measure
pure judge churn. That number is the ceiling on what rubric calibration can fix.

It drives the REAL `Judge._check_answer_support` code path (same prompt, same
forced `submit_grounding` tool, same fail-closed parsing) — not a copy of the
prompt. The one seam: evidence items are only touched via `Judge._view`, so a
subclass overrides `_view` to identity and feeds the already-rendered dicts the
capture stored (`_evidence_view` in eval_capture_investigations.py is field-for-
field the judge's view, with `resolved_report_date` in place of `quarter`).

Deliberately EXCLUDED: the INTEGRITY head (deterministic re-fetch — no churn to
measure) and `_check_key_evidence` (string membership — deterministic). The
replayed gate is therefore answer-support-only; live `grounded` additionally
required those deterministic passes.

Run:  ANTHROPIC_API_KEY=... python3 scripts/replay_judge_answer_support.py \
          --in output/investigator_eval_outputs_adr18_r3.json --repeat 3 \
          --out output/judge_replay_baseline.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.loop import _serialize_findings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class ReplayJudge(Judge):
    """A Judge whose `_view` is identity: evidence arrives pre-rendered (the
    capture stored the view, not the envelopes). Everything else — prompt,
    tool schema, model call, fail-closed parsing — is the production path."""

    _view = staticmethod(lambda e: e)


def _judge_view(captured: list[dict]) -> list[dict]:
    """Captured `_evidence_view` dicts → the judge's `_view` rendering.
    Byte-equivalent for narrative items; metrics rename resolved_report_date →
    quarter (the only field the two serializers spell differently)."""
    out = []
    for e in captured:
        if e.get("kind") == "metric":
            out.append({
                "kind": "metric",
                "feature": e["feature"],
                "status": e["status"],
                "value": e["value"],
                "quarter": e["resolved_report_date"],
            })
        else:
            out.append(e)
    return out


def _answer_from(rec: dict) -> str:
    """Rebuild the judge-facing answer exactly as the loop serialized it."""
    return _serialize_findings({
        "verdict_sentence": rec.get("verdict_sentence") or "",
        "rationale": rec.get("rationale") or "",
        "key_evidence": rec.get("key_evidence") or [],
        "caveats": rec.get("caveats") or [],
    })


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp",
                    default=str(REPO / "output" / "investigator_eval_outputs_adr18_r3.json"))
    ap.add_argument("--out", default=str(REPO / "output" / "judge_replay_baseline.json"))
    ap.add_argument("--repeat", type=int, default=3,
                    help="re-judgings per transcript (default 3)")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    import anthropic
    judge = ReplayJudge(
        client=anthropic.Anthropic(timeout=120.0, max_retries=2),
        reverify={},  # INTEGRITY head unused in this replay
    )

    records = json.load(open(args.inp))
    replayable, skipped = [], []
    for rec in records:
        # A run that never submitted findings (capped mid-gather) has no answer
        # for the head to grade — nothing to replay.
        if (rec.get("verdict_sentence") or "").strip():
            replayable.append(rec)
        else:
            skipped.append(rec["trace_id"])
    print(f"{len(replayable)} transcripts replayable, {len(skipped)} skipped "
          f"(no submitted findings): {skipped}")

    jobs = [(rec, k) for rec in replayable for k in range(1, args.repeat + 1)]

    def _one(job):
        rec, k = job
        answer = _answer_from(rec)
        view = _judge_view(rec["evidence"])
        t0 = time.time()
        try:
            ok, violations, advisories = judge._check_answer_support(
                answer, view, context=rec.get("predicate") or "")
            return {
                "trace_id": rec["trace_id"], "replay_index": k,
                "ok": ok, "violations": violations, "advisories": advisories,
                "elapsed_seconds": round(time.time() - t0, 1),
            }
        except Exception as e:  # noqa: BLE001 — one failed replay must not kill the grid
            return {"trace_id": rec["trace_id"], "replay_index": k,
                    "error": f"{type(e).__name__}: {e}",
                    "elapsed_seconds": round(time.time() - t0, 1)}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(_one, jobs))

    # ---- summary: pure judge churn on fixed inputs --------------------------
    by_trace: dict[str, list[dict]] = {}
    for r in results:
        by_trace.setdefault(r["trace_id"], []).append(r)
    summary = []
    for rec in replayable:
        rs = sorted(by_trace[rec["trace_id"]], key=lambda x: x["replay_index"])
        oks = [r.get("ok") for r in rs]
        summary.append({
            "trace_id": rec["trace_id"],
            "live_terminal_state": rec["terminal_state"],
            "live_trusted": rec["trusted"],
            "replay_oks": oks,
            "judge_churns": len({str(o) for o in oks}) > 1,
            "n_violations": [len(r.get("violations") or []) for r in rs],
            "n_advisories": [len(r.get("advisories") or []) for r in rs],
        })
    churned = [s["trace_id"] for s in summary if s["judge_churns"]]
    print(f"\nPure judge churn (fixed transcript, {args.repeat} re-judgings):")
    for s in summary:
        flag = "CHURN " if s["judge_churns"] else "stable"
        print(f"  {flag} {s['trace_id']:26} oks={s['replay_oks']} "
              f"violations={s['n_violations']} advisories={s['n_advisories']} "
              f"(live: {s['live_terminal_state']}/{'T' if s['live_trusted'] else 'U'})")
    print(f"\n{len(churned)}/{len(summary)} transcripts churn on re-judging alone.")

    Path(args.out).write_text(json.dumps(
        {"summary": summary, "replays": results, "skipped": skipped}, indent=2))
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
