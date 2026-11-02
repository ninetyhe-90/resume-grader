"""Minimal client for TypeSafe AI's Jev (System One decision model).

Jev does not generate text. A request carries a `state` plus typed `questions`
(noul / choice / score); the response carries calibrated probabilities. This
module builds the resume-judging questions, calls the API and validates the
answers strictly: any missing or malformed answer raises JevError. There is no
silent default.

Secrets are read from the environment only (TYPESAFE_API_KEY).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"  # pinned: aliases such as jev-latest change over time
RETRY_STATUS = (429, 529)
MAX_RETRIES = 2
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

Transport = Callable[[str, dict, bytes, float], "tuple[int, dict, bytes]"]


class JevError(Exception):
    """Raised for any Jev configuration, transport or response problem."""


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

CAREER_NOTE = (
    "The resume may be written in Chinese or English; judge the content, not the language. "
)

IMPACT = [
    "No outcomes at all; only duties or responsibilities are listed.",
    "Few outcomes; mostly duties, results are vague or unverifiable.",
    "Some concrete outcomes, but fewer than half of the roles show measurable results.",
    "Most roles show concrete, measurable outcomes (numbers, scale, before/after change).",
    "Nearly every role shows specific, measurable outcomes with a clearly attributable personal contribution.",
]
DEPTH = [
    "Only basic tasks; no sign of ownership.",
    "Executes assigned tasks; limited ownership or decision making.",
    "Owns well-scoped modules or tasks end to end.",
    "Owns complex projects or systems and makes design or strategy decisions.",
    "Leads large-scale or cross-team initiatives, with clear progression in responsibility.",
]
CLARITY = [
    "Disorganized and hard to follow; structure is missing.",
    "Understandable but cluttered, inconsistent or padded.",
    "Readable with a standard structure; some vagueness or padding.",
    "Clear, well organized, concise and consistent.",
    "Exceptionally clear: focused, every line adds information, easy to scan.",
]
RELEVANCE = [
    "No relevant skills or experience for the target job.",
    "Weak or tangential relevance.",
    "Partial match: some required skills or experience.",
    "Most required skills and experience are present.",
    "Strong match to nearly all requirements, including seniority.",
]
VERDICT = {
    "excellent": "Strong, specific and credible; most reviewers would shortlist this resume.",
    "good": "Competent and credible but with notable gaps or weaknesses; shortlisting depends on competition.",
    "poor": "Weak, vague, inconsistent or incomplete; most reviewers would reject it.",
}


def build_questions(has_jd: bool) -> dict[str, dict[str, Any]]:
    job_note = (
        " The target job description is in state.target_job; judge against it."
        if has_jd
        else " No target job is given; judge on general professional quality."
    )
    q: dict[str, dict[str, Any]] = {
        "impact": {
            "type": "score",
            "instructions": CAREER_NOTE + "How strong is the evidence of concrete, measurable results in state.resume?",
            "criteria": IMPACT,
        },
        "depth": {
            "type": "score",
            "instructions": CAREER_NOTE
            + "How deep is the candidate's demonstrated ownership and responsibility in state.resume? "
            "Judge relative to the candidate's career stage (a strong fresh graduate can reach level 3).",
            "criteria": DEPTH,
        },
        "clarity": {
            "type": "score",
            "instructions": CAREER_NOTE + "How clear, concise and well organized is state.resume?",
            "criteria": CLARITY,
        },
        "verdict": {
            "type": "choice",
            "instructions": CAREER_NOTE + "Give an overall verdict on state.resume." + job_note,
            "criteria": VERDICT,
        },
        "vague_claims": {
            "type": "noul",
            "instructions": CAREER_NOTE
            + "Does state.resume rely heavily on unsupported self-praise or generic buzzwords "
            "(for example 'hard-working', 'quick learner', 'team player') instead of concrete evidence? "
            "Answer true only if unsupported self-praise outweighs concrete evidence.",
        },
        "inconsistency": {
            "type": "noul",
            "instructions": CAREER_NOTE
            + "Does state.resume contain contradictions, such as overlapping full-time jobs, impossible dates, "
            "or titles and scope that do not fit the timeline? "
            "Answer true only if a concrete contradiction exists.",
        },
        "unexplained_gap": {
            "type": "noul",
            "instructions": CAREER_NOTE
            + "Does state.resume show an unexplained employment or education gap longer than 6 months? "
            "Answer false if the gap is explained (study, travel, caregiving, freelance) or absent.",
        },
    }
    if has_jd:
        q["relevance"] = {
            "type": "score",
            "instructions": CAREER_NOTE
            + "How well does state.resume match the requirements in state.target_job?",
            "criteria": RELEVANCE,
        }
        q["must_have_met"] = {
            "type": "noul",
            "instructions": CAREER_NOTE
            + "Does state.resume show evidence for every must-have requirement listed in state.target_job? "
            "Answer false if at least one must-have requirement has no supporting evidence.",
        }
    return q


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def _default_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme validated)
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JevError(f"network error calling Jev: {exc}") from exc


def validate_endpoint(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and (
        parsed.hostname in LOCAL_HOSTS or os.environ.get("JEV_ALLOW_HTTP") == "1"
    ):
        return url
    raise JevError(
        "the Jev endpoint must be https; plain http is allowed only for localhost "
        "or when JEV_ALLOW_HTTP=1 is set explicitly (the API key travels unencrypted)"
    )


def resolve_endpoint(explicit: str | None = None) -> str:
    """Endpoint priority: argument, JEV_ENDPOINT (full URL), JEV_BASE_URL + /v1/systemone, default."""
    if explicit:
        return explicit
    full = os.environ.get("JEV_ENDPOINT")
    if full:
        return full
    base = os.environ.get("JEV_BASE_URL")
    if base:
        return base.rstrip("/") + "/v1/systemone"
    return DEFAULT_ENDPOINT


def call_jev(
    state: dict[str, Any],
    questions: dict[str, dict[str, Any]],
    *,
    api_key: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    timeout: float = 30.0,
    transport: Transport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    key = api_key or os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise JevError(
            "JEV_API_KEY (or TYPESAFE_API_KEY) is not set. Export it, or run with --no-jev "
            "for a rules-only pre-screen."
        )
    url = validate_endpoint(resolve_endpoint(endpoint))
    body = json.dumps(
        {"model": model or os.environ.get("JEV_MODEL") or DEFAULT_MODEL, "state": state, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    send = transport or _default_transport

    status, resp_headers, raw = 0, {}, b""
    for attempt in range(MAX_RETRIES + 1):
        status, resp_headers, raw = send(url, headers, body, timeout)
        if status not in RETRY_STATUS or attempt == MAX_RETRIES:
            break
        retry_after = {k.lower(): v for k, v in resp_headers.items()}.get("retry-after", "")
        wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else float(2**attempt)
        sleep(min(wait, 10.0))
    if status != 200:
        detail = raw.decode("utf-8", "replace")[:300]
        raise JevError(f"Jev API returned HTTP {status}: {detail}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JevError(f"Jev API returned non-JSON body: {exc}") from exc
    return payload


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _num(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevError(f"answer field '{name}' is not a number")
    return float(value)


def parse_answers(payload: dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise JevError("Jev response has no 'answers' object")
    parsed: dict[str, dict[str, Any]] = {}
    for qid, q in questions.items():
        ans = answers.get(qid)
        if not isinstance(ans, dict) or ans.get("type") != q["type"]:
            raise JevError(f"Jev answer for '{qid}' is missing or has the wrong type")
        kind = q["type"]
        if kind == "noul":
            p = _num(ans.get("noul"), f"{qid}.noul")
            if not 0.0 <= p <= 1.0:
                raise JevError(f"answer '{qid}.noul' is outside [0, 1]")
            parsed[qid] = {"type": "noul", "p": p}
        elif kind == "choice":
            options = list(q["criteria"].keys())
            choice = ans.get("choice")
            if choice not in options:
                raise JevError(f"answer '{qid}.choice' is not one of {options}")
            probs = ans.get("probabilities") or {}
            if not isinstance(probs, dict) or not set(probs) <= set(options):
                raise JevError(f"answer '{qid}.probabilities' has unknown options")
            probs = {o: _num(probs.get(o, 0.0), f"{qid}.probabilities.{o}") for o in options}
            if sum(probs.values()) <= 0:
                probs = {o: (1.0 if o == choice else 0.0) for o in options}
            conf = ans.get("confidence")
            parsed[qid] = {
                "type": "choice",
                "choice": choice,
                "probabilities": probs,
                "confidence": None if conf is None else _num(conf, f"{qid}.confidence"),
            }
        elif kind == "score":
            n = len(q["criteria"])
            level = _num(ans.get("score"), f"{qid}.score")
            if not 0.0 <= level <= n - 1:
                raise JevError(f"answer '{qid}.score' is outside the scale 0..{n - 1}")
            probs = ans.get("probabilities")
            expected = level
            if isinstance(probs, dict) and probs:
                try:
                    weights = {int(k): _num(v, f"{qid}.probabilities.{k}") for k, v in probs.items()}
                except ValueError as exc:
                    raise JevError(f"answer '{qid}.probabilities' has non-integer keys") from exc
                if any(not 0 <= k < n for k in weights):
                    raise JevError(f"answer '{qid}.probabilities' has levels outside 0..{n - 1}")
                total = sum(weights.values())
                if total > 0:
                    expected = sum(k * w for k, w in weights.items()) / total
            conf = ans.get("confidence")
            parsed[qid] = {
                "type": "score",
                "level": level,
                "expected": expected,
                "pct": round(expected / (n - 1) * 100, 1),
                "confidence": None if conf is None else _num(conf, f"{qid}.confidence"),
            }
    return parsed
