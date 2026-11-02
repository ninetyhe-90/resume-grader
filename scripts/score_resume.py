#!/usr/bin/env python3
"""Grade a resume as Excellent / Good / Poor.

Pipeline:
  1. resume_features: deterministic checks (contact, sections, dates, bullets, keywords)
  2. jev_client: Jev judges the semantic dimensions (impact, depth, clarity, relevance,
     risk signals, overall verdict) and returns calibrated probabilities
  3. this file: application-owned scoring, gates, thresholds and the final grade

Usage:
  python3 scripts/score_resume.py resume.pdf [--jd jd.txt] [--keywords "Python,Kafka"] [--json]
  python3 scripts/score_resume.py resume.md --no-jev      # rules-only pre-screen
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_client  # noqa: E402
import resume_features as rf  # noqa: E402

GRADES = ["差", "良", "优秀"]  # rank 0, 1, 2
DEFAULT_EXCELLENT = 80.0
DEFAULT_GOOD = 60.0
BOUNDARY_MARGIN = 3.0
JEV_SHARE = 0.65  # composite = JEV_SHARE * jev_quality + (1 - JEV_SHARE) * rules_score
MAX_STATE_CHARS = 15000
MIN_CHARS_FOR_CONTENT = 300

JEV_WEIGHTS = {"impact": 0.25, "depth": 0.20, "clarity": 0.10, "relevance": 0.25, "verdict": 0.20}
VERDICT_VALUE = {"excellent": 92.0, "good": 70.0, "poor": 30.0}
RISK_THRESHOLD = 0.7
VERDICT_CONFIDENCE_MIN = 0.6


class ScoreError(Exception):
    pass


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------

def redact(text: str) -> str:
    text = rf.EMAIL_RE.sub("[EMAIL]", text)
    return rf.PHONE_RE.sub("[PHONE]", text)


def build_state(text: str, jd: str | None, do_redact: bool) -> dict[str, Any]:
    body = redact(text) if do_redact else text
    if len(body) > MAX_STATE_CHARS:
        raise ScoreError(
            f"resume text is {len(body)} chars, above the {MAX_STATE_CHARS} limit for one Jev request; "
            "condense it or score the sections separately"
        )
    state: dict[str, Any] = {"resume": body}
    if jd:
        if len(jd) > MAX_STATE_CHARS:
            raise ScoreError("job description is too long for one Jev request")
        state["target_job"] = jd
    return state


# --------------------------------------------------------------------------
# Jev quality
# --------------------------------------------------------------------------

def jev_quality(parsed: dict[str, dict[str, Any]]) -> tuple[float, dict[str, float]]:
    parts: dict[str, float] = {}
    for key in ("impact", "depth", "clarity", "relevance"):
        if key in parsed:
            parts[key] = parsed[key]["pct"]
    verdict = parsed["verdict"]
    parts["verdict"] = round(sum(VERDICT_VALUE[o] * p for o, p in verdict["probabilities"].items())
                             / sum(verdict["probabilities"].values()), 1)
    total_w = sum(JEV_WEIGHTS[k] for k in parts)
    quality = sum(parts[k] * JEV_WEIGHTS[k] for k in parts) / total_w
    return round(quality, 1), parts


def risk_adjustments(parsed: dict[str, dict[str, Any]]) -> tuple[float, list[dict[str, Any]], dict[str, int], list[str]]:
    """Return (penalty, applied, caps, flags). caps maps reason -> max grade rank."""
    penalty = 0.0
    applied: list[dict[str, Any]] = []
    caps: dict[str, int] = {}
    flags: list[str] = []
    rules = [("vague_claims", 8.0, None, "unsupported self-praise"),
             ("unexplained_gap", 5.0, None, "unexplained gap over 6 months"),
             ("inconsistency", 10.0, 1, "timeline or fact inconsistency")]
    for qid, pts, cap, label in rules:
        p = parsed[qid]["p"]
        if p >= RISK_THRESHOLD:
            penalty += pts
            applied.append({"signal": qid, "p": round(p, 2), "penalty": pts, "label": label})
            if cap is not None:
                caps[f"{qid} p={p:.2f}: {label}"] = cap
    if "must_have_met" in parsed and parsed["must_have_met"]["p"] < 1 - RISK_THRESHOLD:
        p = parsed["must_have_met"]["p"]
        caps[f"must_have_met p={p:.2f}: a must-have requirement is not evidenced"] = 1
    conf = parsed["verdict"].get("confidence")
    if conf is not None and conf < VERDICT_CONFIDENCE_MIN:
        flags.append(f"jev_uncertain: verdict confidence {conf:.2f} < {VERDICT_CONFIDENCE_MIN}")
    return penalty, applied, caps, flags


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------

def grade_for(score: float, excellent: float, good: float) -> int:
    if score >= excellent:
        return 2
    if score >= good:
        return 1
    return 0


def rule_caps(features: dict[str, Any]) -> dict[str, int]:
    caps: dict[str, int] = {}
    if not features["has_contact"]:
        caps["no email or phone found"] = 1
    if features["char_count"] < MIN_CHARS_FOR_CONTENT:
        caps[f"only {features['char_count']} characters of content"] = 0
    if not features["sections_found"].get("experience"):
        caps["no experience or project section found"] = 1
    return caps


def final_grade(composite: float, caps: dict[str, int], excellent: float, good: float) -> tuple[int, int]:
    raw = grade_for(composite, excellent, good)
    capped = min([raw, *caps.values()]) if caps else raw
    return raw, capped


def is_boundary(composite: float, excellent: float, good: float) -> bool:
    return any(abs(composite - t) < BOUNDARY_MARGIN for t in (excellent, good))


def score(
    text: str,
    *,
    jd: str | None = None,
    keywords: list[str] | None = None,
    use_jev: bool = True,
    do_redact: bool = True,
    excellent: float = DEFAULT_EXCELLENT,
    good: float = DEFAULT_GOOD,
    model: str | None = None,
    transport: Any = None,
) -> dict[str, Any]:
    if not good < excellent:
        raise ScoreError("thresholds must satisfy good < excellent")
    features = rf.analyze(text, keywords)
    caps = rule_caps(features)
    flags: list[str] = []
    result: dict[str, Any] = {"thresholds": {"excellent": excellent, "good": good}}

    if use_jev:
        questions = jev_client.build_questions(bool(jd))
        state = build_state(text, jd, do_redact)
        payload = jev_client.call_jev(state, questions, model=model, transport=transport)
        parsed = jev_client.parse_answers(payload, questions)
        quality, parts = jev_quality(parsed)
        penalty, applied, jev_caps, jev_flags = risk_adjustments(parsed)
        caps.update(jev_caps)
        flags.extend(jev_flags)
        composite = JEV_SHARE * quality + (1 - JEV_SHARE) * features["rules_score"] - penalty
        composite = round(max(0.0, min(100.0, composite)), 1)
        result["jev"] = {
            "model": payload.get("model"),
            "quality": quality,
            "parts": parts,
            "risk_penalties": applied,
            "answers": parsed,
            "redacted": do_redact,
        }
        verdict_choice = parsed["verdict"]["choice"]
        raw_rank = grade_for(composite, excellent, good)
        if {"excellent": 2, "good": 1, "poor": 0}[verdict_choice] != raw_rank:
            flags.append(f"disagreement: Jev verdict is '{verdict_choice}' but composite grade is {GRADES[raw_rank]}")
    else:
        composite = features["rules_score"]
        caps["rules-only: without a semantic judge the resume cannot be rated excellent"] = 1
        flags.append("rules_only: Jev was not used; the grade reflects format and writing hygiene only")
        result["jev"] = None

    raw, final = final_grade(composite, caps, excellent, good)
    boundary = is_boundary(composite, excellent, good)
    if boundary:
        flags.append("boundary: composite is within %.0f points of a threshold; review manually" % BOUNDARY_MARGIN)
    result.update(
        {
            "grade": GRADES[final],
            "grade_before_caps": GRADES[raw],
            "composite": composite,
            "caps_applied": [{"reason": r, "max_grade": GRADES[c]} for r, c in caps.items() if c < raw],
            "flags": flags,
            "needs_human_review": boundary or any(f.startswith(("jev_uncertain", "disagreement")) for f in flags),
            "rules": {k: features[k] for k in (
                "rules_score", "sub_scores", "char_count", "has_contact", "sections_found",
                "missing_required_sections", "bullet_count", "metric_rate", "filler_hits",
                "date_styles", "missing_keywords")},
            "suggestions": suggestions(features, result.get("jev")),
        }
    )
    return result


# --------------------------------------------------------------------------
# Suggestions and report
# --------------------------------------------------------------------------

RULE_TEXT = {
    "weak_opener": "bullet starts with a duty phrase (负责/参与/responsible for); lead with the action and result",
    "first_person": "avoid first person",
    "long_bullet": "bullet is too long; split it",
    "no_metric": "no number in this bullet; add scale, percentage or before/after",
    "repeated_opener": "same opening word repeated 3+ times in a role",
}


def suggestions(features: dict[str, Any], jev: dict[str, Any] | None) -> list[str]:
    tips: list[str] = []
    if not features["has_contact"]:
        tips.append("Add an email or phone number; without contact details the resume cannot be used.")
    if features["missing_required_sections"]:
        tips.append("Add clear section headings for: " + ", ".join(features["missing_required_sections"]) + ".")
    rate = features["metric_rate"]
    if rate is not None and rate < 0.5:
        tips.append(f"Only {rate:.0%} of bullets contain a number. Aim for at least half: scale, percentage, time saved, revenue.")
    counts: dict[str, int] = {}
    for f in features["findings"]:
        counts[f["rule"]] = counts.get(f["rule"], 0) + 1
    for rule in ("weak_opener", "repeated_opener", "long_bullet", "first_person"):
        if counts.get(rule):
            tips.append(f"{counts[rule]} bullet(s): {RULE_TEXT[rule]}.")
    ds = features["date_styles"]
    if len(ds) > 1:
        tips.append("Use one date format everywhere (for example 2021.03 - 2023.06).")
    if features["filler_hits"] >= 2:
        tips.append("Replace generic self-praise (责任心强/team player) with concrete evidence.")
    if features["missing_keywords"]:
        tips.append("Keywords not found: " + ", ".join(features["missing_keywords"][:10]) + ". Add them only where truthful.")
    if jev:
        parts = jev["parts"]
        for key, text in (
            ("impact", "Show measurable outcomes and your personal contribution in each role."),
            ("depth", "Make ownership explicit: what you designed, decided or led."),
            ("clarity", "Tighten structure: reverse-chronological, one idea per bullet, no padding."),
            ("relevance", "Reorder or reword experience to address the target job's core requirements."),
        ):
            if key in parts and parts[key] < 50:
                tips.append(text)
        for item in jev["risk_penalties"]:
            tips.append(f"Jev flagged: {item['label']} (p={item['p']}). Fix or explain it.")
    worst = sorted({f["index"] for f in features["findings"] if f["rule"] == "weak_opener"})[:3]
    for i in worst:
        tips.append(f"Rewrite example: \"{features['bullets'][i][:60]}\"")
    return tips


def render_markdown(r: dict[str, Any]) -> str:
    th = r["thresholds"]
    out = [
        f"# Resume grade: {r['grade']}",
        "",
        f"- Composite score: {r['composite']} (excellent >= {th['excellent']:g}, good >= {th['good']:g})",
        f"- Grade before caps: {r['grade_before_caps']}",
        f"- Needs human review: {'yes' if r['needs_human_review'] else 'no'}",
    ]
    if r["jev"] is None:
        out.append("- Judge: NONE (rules-only pre-screen, not a full evaluation)")
    else:
        out.append(f"- Judge: Jev {r['jev']['model']} (quality {r['jev']['quality']}, redacted={r['jev']['redacted']})")
    if r["caps_applied"]:
        out += ["", "## Caps applied"] + [f"- [{c['max_grade']} max] {c['reason']}" for c in r["caps_applied"]]
    if r["flags"]:
        out += ["", "## Flags"] + [f"- [!] {f}" for f in r["flags"]]
    out += ["", "## Rule checks", "", "| item | score |", "|---|---|"]
    for k, v in r["rules"]["sub_scores"].items():
        out.append(f"| {k} | {'n/a' if v is None else v} |")
    out.append(f"| rules_score | {r['rules']['rules_score']} |")
    if r["jev"]:
        out += ["", "## Jev judgement", "", "| dimension | score (0-100) |", "|---|---|"]
        for k, v in r["jev"]["parts"].items():
            out.append(f"| {k} | {v} |")
        out += ["", "| risk signal | probability |", "|---|---|"]
        for k in ("vague_claims", "inconsistency", "unexplained_gap", "must_have_met"):
            a = r["jev"]["answers"].get(k)
            if a:
                out.append(f"| {k} | {a['p']:.2f} |")
    if r["suggestions"]:
        out += ["", "## Suggestions"] + [f"- {s}" for s in r["suggestions"]]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Grade a resume: excellent / good / poor")
    ap.add_argument("resume", help="path to .pdf .docx .txt .md .json")
    ap.add_argument("--jd", help="path to a job description text file (enables relevance scoring)")
    ap.add_argument("--keywords", default="", help="comma separated must-have keywords extracted from the JD")
    ap.add_argument("--no-jev", action="store_true", help="rules-only pre-screen, Jev is not called")
    ap.add_argument("--no-redact", action="store_true", help="send email/phone to Jev unmasked")
    ap.add_argument("--excellent", type=float, default=DEFAULT_EXCELLENT)
    ap.add_argument("--good", type=float, default=DEFAULT_GOOD)
    ap.add_argument("--model", help="Jev model (default jev-1.13.0, env JEV_MODEL)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    args = ap.parse_args(argv)

    try:
        text = rf.load_resume_text(args.resume)
        jd = None
        if args.jd:
            jd_path = Path(args.jd).expanduser()
            if not jd_path.is_file():
                raise ScoreError(f"job description file not found: {jd_path}")
            jd = jd_path.read_text(encoding="utf-8", errors="replace").strip() or None
        keywords = [k for k in re.split(r"[,，;；\n]", args.keywords) if k.strip()]
        result = score(
            text, jd=jd, keywords=keywords, use_jev=not args.no_jev, do_redact=not args.no_redact,
            excellent=args.excellent, good=args.good, model=args.model,
        )
    except (rf.ResumeError, jev_client.JevError, ScoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
