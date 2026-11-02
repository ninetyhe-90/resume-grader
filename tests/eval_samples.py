#!/usr/bin/env python3
"""Live evaluation against a real Jev endpoint (needs JEV_API_KEY, makes network calls).

Sample files are named <label>_<nn>_<tag>.md with label in {excellent, good, poor}.
For every sample the resume is scored --repeats times, then the script reports
the mean composite, the spread across repeats, the grade distribution and a
confusion matrix against the filename label.

  python3 -B tests/eval_samples.py [--repeats 3] [--samples DIR] [--json]

This does NOT prove accuracy: the labels come from whoever wrote the samples.
It checks ordering (excellent > good > poor), repeatability and gross errors.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import jev_client  # noqa: E402
import resume_features as rf  # noqa: E402
import score_resume as sr  # noqa: E402

LABELS = {"excellent": "优秀", "good": "良", "poor": "差"}
ORDER = ["优秀", "良", "差"]


def score_with_backoff(text: str, jd: str | None, pause: float, waits: tuple[float, ...] = (20.0, 45.0, 90.0)) -> dict:
    """Score once; on a rate limit that survived the client's own retries, wait out the window."""
    for wait in (*waits, None):
        try:
            result = sr.score(text, jd=jd)
            time.sleep(pause)
            return result
        except jev_client.JevError as exc:
            if "HTTP 429" not in str(exc) or wait is None:
                raise
            print(f"  rate limited, waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def run(samples_dir: Path, repeats: int, jd: str | None, pause: float) -> list[dict]:
    rows = []
    for path in sorted(samples_dir.glob("*.md")):
        label_key = path.name.split("_", 1)[0]
        if label_key not in LABELS:
            raise SystemExit(f"cannot infer label from file name: {path.name}")
        text = rf.load_resume_text(str(path))
        runs = []
        for _ in range(repeats):
            runs.append(score_with_backoff(text, jd, pause))
        print(f"done {path.name}", file=sys.stderr)
        comps = [r["composite"] for r in runs]
        grades = [r["grade"] for r in runs]
        rows.append(
            {
                "file": path.name,
                "expected": LABELS[label_key],
                "composites": comps,
                "mean": round(statistics.mean(comps), 1),
                "spread": round(max(comps) - min(comps), 1),
                "grades": grades,
                "modal_grade": max(set(grades), key=grades.count),
                "grade_stable": len(set(grades)) == 1,
                "review": any(r["needs_human_review"] for r in runs),
                "caps": sorted({c["reason"] for r in runs for c in r["caps_applied"]}),
                "flags": sorted({f for r in runs for f in r["flags"] if not f.startswith("boundary")}),
                "verdict": [r["jev"]["answers"]["verdict"]["choice"] for r in runs],
            }
        )
    return rows


def summarize(rows: list[dict]) -> dict:
    matrix = {e: {g: 0 for g in ORDER} for e in ORDER}
    for r in rows:
        matrix[r["expected"]][r["modal_grade"]] += 1
    by_label = {e: [r["mean"] for r in rows if r["expected"] == e] for e in ORDER}
    ranges = {e: (min(v), max(v)) for e, v in by_label.items() if v}
    ordered = (
        ranges["优秀"][0] > ranges["良"][1] > ranges["良"][0] > ranges["差"][1]
        if all(k in ranges for k in ORDER)
        else None
    )
    exact = sum(matrix[e][e] for e in ORDER)
    off_by_two = matrix["优秀"]["差"] + matrix["差"]["优秀"]
    return {
        "matrix": matrix,
        "ranges": ranges,
        "bands_do_not_overlap": ordered,
        "exact_matches": f"{exact}/{len(rows)}",
        "off_by_two_grades": off_by_two,
        "max_spread": max(r["spread"] for r in rows),
        "unstable_grade_files": [r["file"] for r in rows if not r["grade_stable"]],
    }


def render(rows: list[dict], summary: dict) -> str:
    out = ["| file | expected | mean | spread | grades | verdict | caps |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(
            f"| {r['file']} | {r['expected']} | {r['mean']} | {r['spread']} | {'/'.join(r['grades'])} "
            f"| {'/'.join(r['verdict'])} | {'; '.join(c.split(':')[0] for c in r['caps']) or '-'} |"
        )
    out += ["", "Confusion matrix (rows = expected, columns = predicted):", "", "| expected \\ predicted | 优秀 | 良 | 差 |", "|---|---|---|---|"]
    for e in ORDER:
        out.append(f"| {e} | " + " | ".join(str(summary["matrix"][e][g]) for g in ORDER) + " |")
    out += [
        "",
        f"- exact matches: {summary['exact_matches']}",
        f"- off by two grades (excellent <-> poor): {summary['off_by_two_grades']}",
        f"- composite range per label: " + ", ".join(f"{k}={v[0]}-{v[1]}" for k, v in summary["ranges"].items()),
        f"- label bands separated (min excellent > max good, min good > max poor): {summary['bands_do_not_overlap']}",
        f"- max spread across repeats: {summary['max_spread']} points",
        f"- files whose grade changed between repeats: {summary['unstable_grade_files'] or 'none'}",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--samples", default=str(ROOT / "tests" / "samples"))
    ap.add_argument("--jd", help="optional JD file applied to every sample")
    ap.add_argument("--pause", type=float, default=4.0, help="seconds between requests (gateway rate limit)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")
    jd = Path(args.jd).read_text(encoding="utf-8").strip() if args.jd else None
    try:
        rows = run(Path(args.samples), args.repeats, jd, args.pause)
    except (jev_client.JevError, sr.ScoreError, rf.ResumeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    summary = summarize(rows)
    print(json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False, indent=2) if args.json else render(rows, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
