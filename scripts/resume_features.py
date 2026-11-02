"""Resume text loading and deterministic (no LLM) feature checks.

Ideas adapted from open-source projects (see references/sources.md):
  - Resume-Matcher: weighted ATS components, date-format consistency,
    section completeness, writing-quality rules, whole-term keyword matching,
    unscorable components reported as None and re-weighted.
  - OpenResume: text must be extractable and sectioned before anything else
    can be judged.

Only the standard library is used. Everything here is deterministic.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_DOCX_MEMBER_BYTES = 10 * 1024 * 1024


class ResumeError(Exception):
    """Raised when the resume cannot be loaded or is unusable."""


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _flatten_json(obj: Any, lines: list[str]) -> None:
    if isinstance(obj, str):
        if obj.strip():
            lines.append(obj.strip())
    elif isinstance(obj, list):
        for item in obj:
            _flatten_json(item, lines)
    elif isinstance(obj, dict):
        for value in obj.values():
            _flatten_json(value, lines)


def _read_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_MEMBER_BYTES:
                raise ResumeError("docx document.xml is too large")
            xml = zf.read(info).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ResumeError(f"not a valid .docx file: {exc}") from exc
    paragraphs = []
    for chunk in xml.split("</w:p>"):
        parts = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", chunk, flags=re.S)
        line = html.unescape("".join(parts)).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


def _read_pdf(path: Path) -> str:
    exe = shutil.which("pdftotext")
    if not exe:
        raise ResumeError(
            "pdftotext not found (install poppler, e.g. `brew install poppler`), "
            "or convert the resume to .txt/.md/.docx first"
        )
    proc = subprocess.run(
        [exe, str(path), "-"], capture_output=True, timeout=60, check=False
    )
    if proc.returncode != 0:
        raise ResumeError(
            "pdftotext failed: " + proc.stderr.decode("utf-8", "replace").strip()[:200]
        )
    return proc.stdout.decode("utf-8", errors="replace")


def load_resume_text(path_str: str) -> str:
    path = Path(path_str).expanduser().resolve()
    if not path.is_file():
        raise ResumeError(f"file not found: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ResumeError("file is larger than 20 MB")
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".markdown"):
        text = path.read_text(encoding="utf-8", errors="replace")
    elif suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ResumeError(f"invalid JSON: {exc}") from exc
        lines: list[str] = []
        _flatten_json(data, lines)
        text = "\n".join(lines)
    elif suffix == ".docx":
        text = _read_docx(path)
    elif suffix == ".pdf":
        text = _read_pdf(path)
    else:
        raise ResumeError(f"unsupported file type: {suffix or '(none)'}")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if _garbled_count(text) >= 5:
        raise ResumeError(
            "extracted text looks garbled (broken font mapping or scanned image); "
            "run OCR or export the resume as text first"
        )
    if len(text.strip()) == 0:
        raise ResumeError("no text could be extracted from the resume")
    return text


_GARBLED_RE = re.compile(r"\(cid:\d+\)|\ufffd|[\ue000-\uf8ff]")


def _garbled_count(text: str) -> int:
    return len(_GARBLED_RE.findall(text))


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

SECTION_SYNONYMS: dict[str, list[str]] = {
    "summary": ["个人简介", "自我评价", "个人总结", "个人优势", "简介", "summary", "objective", "profile", "about"],
    "experience": [
        "工作经历", "工作经验", "实习经历", "实习经验", "项目经历", "项目经验",
        "职业经历", "experience", "work history", "employment", "projects", "project",
    ],
    "education": ["教育背景", "教育经历", "学历", "education", "academic"],
    "skills": ["专业技能", "技能特长", "技术栈", "技能", "skills", "technologies", "competencies"],
    "other": [
        "获奖", "荣誉", "证书", "语言能力", "兴趣", "awards", "certifications",
        "honors", "languages", "interests", "publications", "references",
    ],
}
REQUIRED_SECTIONS = ("experience", "education", "skills")

_HEADING_STRIP = re.compile(r"^[\s#*>\-_=|\[\]【】()（）:：]+|[\s#*_=|\[\]【】()（）:：]+$")


def heading_of(line: str) -> str | None:
    raw = line.strip()
    if not raw or len(raw) > 24 or raw[-1] in "。.;；,，":
        return None
    norm = _HEADING_STRIP.sub("", raw).lower()
    if not norm:
        return None
    for name, syns in SECTION_SYNONYMS.items():
        for syn in syns:
            if syn in norm and len(norm) <= len(syn) + 8:
                return name
    return None


def split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in text.split("\n"):
        name = heading_of(line)
        if name:
            current = name
            sections.setdefault(current, [])
            continue
        if line.strip():
            sections.setdefault(current, []).append(line.strip())
    return sections


# --------------------------------------------------------------------------
# Contact, dates
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(
    r"(?<![\d.])(?:\+?86[\s-]?)?1[3-9]\d[\s-]?\d{4}[\s-]?\d{4}(?!\d)"  # CN mobile
    r"|(?<![\d.])0\d{2,3}-\d{7,8}(?!\d)"  # CN landline
    r"|(?<![\d.])\+\d{1,3}[\s-]?\d[\d\s-]{6,}\d(?!\d)"  # international with +
    r"|(?<![\d.])\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"  # US style
)

_Y = r"(?:19|20)\d{2}"
_MONTH = (
    r"(?i:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
DATE_RE = re.compile(
    rf"(?P<cn>{_Y}\s*年\s*\d{{1,2}}\s*月?)"
    rf"|(?P<iso>(?<!\d){_Y}-\d{{1,2}}(?!\d))"
    rf"|(?P<slash>(?<!\d){_Y}/\d{{1,2}}(?!\d))"
    rf"|(?P<dot>(?<!\d){_Y}\.\d{{1,2}}(?!\d))"
    rf"|(?P<name_year>\b{_MONTH}\.?,?\s+{_Y}\b)"
    rf"|(?P<year>(?<!\d){_Y}(?!\d))"
)
_PRESENT_RE = re.compile(r"至今|现在|目前|present|current|now", re.I)


_RANGE_GAP_RE = re.compile(r"^[\s\-\u2013\u2014~\uff5e\u81f3\u5230]*(?:to)?[\s\-\u2013\u2014~\uff5e]*$", re.I)
_RANGE_TAIL_RE = re.compile(r"^[\s\-\u2013\u2014~\uff5e\u81f3\u5230]*(?:\u81f3\u4eca|\u73b0\u5728|\u76ee\u524d|present|current|now)", re.I)


def date_styles(text: str) -> dict[str, int]:
    """Count date styles inside date ranges only (avoids '2000 users in 2021')."""
    counts: dict[str, int] = {}
    for line in text.split("\n"):
        matches = [m for m in DATE_RE.finditer(line) if m.lastgroup]
        used: set[int] = set()
        for i, m in enumerate(matches):
            nxt = matches[i + 1] if i + 1 < len(matches) else None
            if nxt is not None and _RANGE_GAP_RE.match(line[m.end():nxt.start()]) and nxt.start() > m.end():
                used.update((i, i + 1))
            elif _RANGE_TAIL_RE.match(line[m.end():]):
                used.add(i)
        for i in used:
            style = matches[i].lastgroup
            counts[style] = counts.get(style, 0) + 1  # type: ignore[index]
    return counts


def date_consistency(text: str) -> float | None:
    counts = date_styles(text)
    total = sum(counts.values())
    if total < 2:
        return None
    return round(max(counts.values()) / total * 100, 1)


def strip_dates(text: str) -> str:
    return DATE_RE.sub(" ", text)


# --------------------------------------------------------------------------
# Bullets and writing quality
# --------------------------------------------------------------------------

_BULLET_MARK = re.compile(r"^\s*(?:[-*•·▪◦●■]|\d{1,2}[.)、](?!\d)|[（(]\d{1,2}[)）])\s*")
WEAK_OPENERS_EN = (
    "responsible for", "helped with", "helped to", "helped", "assisted with",
    "assisted in", "assisted", "involved in", "participated in", "worked on",
    "tasked with", "duties included", "in charge of",
)
WEAK_OPENERS_CN = ("主要负责", "负责", "参与", "协助", "协同", "配合", "帮助", "辅助", "从事")
_WEAK_EN_RE = re.compile(r"^\W*(?:" + "|".join(re.escape(o) for o in WEAK_OPENERS_EN) + r")\b", re.I)
_FIRST_PERSON_RE = re.compile(r"\bI\b|\b(?i:me|my|mine|myself)\b")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_DIGIT_RE = re.compile(r"\d")
_FILLER_CN = ("吃苦耐劳", "责任心强", "学习能力强", "团队合作精神", "沟通能力强", "积极主动", "勤奋好学")
_FILLER_EN = ("hard-working", "hardworking", "quick learner", "team player", "self-motivated", "detail-oriented")

PENALTY = {"weak_opener": 20, "first_person": 10, "long_bullet": 10, "no_metric": 5, "repeated_opener": 10}
_REPEATED_LIMIT = 3


def extract_bullets(sections: dict[str, list[str]]) -> list[str]:
    lines = sections.get("experience", [])
    marked = [_BULLET_MARK.sub("", l).strip() for l in lines if _BULLET_MARK.match(l)]
    if len(marked) >= 3:
        return [b for b in marked if b]
    out = []
    for l in lines:
        s = _BULLET_MARK.sub("", l).strip()
        if _is_date_only(s):
            continue
        if len(s) >= (6 if _is_cjk_text(s) else 12):
            out.append(s)
    return out


def _is_date_only(line: str) -> bool:
    rest = DATE_RE.sub(" ", line)
    rest = _PRESENT_RE.sub(" ", rest)
    return not re.search(r"[\w\u3400-\u9fff]", rest)


def _is_cjk_text(text: str) -> bool:
    return len(_CJK_RE.findall(text)) > len(text) * 0.3


def _opener_token(text: str) -> str:
    if _is_cjk_text(text):
        return text[:2]
    m = re.match(r"\W*([\w+#./&-]+)", text)
    return m.group(1).lower() if m else ""


def check_bullets(bullets: list[str]) -> tuple[list[dict[str, Any]], int | None]:
    findings: list[dict[str, Any]] = []
    penalties = [0] * len(bullets)
    openers: dict[str, list[int]] = {}
    for i, text in enumerate(bullets):
        def add(rule: str) -> None:
            findings.append({"index": i, "rule": rule, "excerpt": text[:100]})
            penalties[i] += PENALTY[rule]

        cjk = _is_cjk_text(text)
        if _WEAK_EN_RE.search(text) or (cjk and text.startswith(WEAK_OPENERS_CN)):
            add("weak_opener")
        if not cjk and _FIRST_PERSON_RE.search(text):
            add("first_person")
        too_long = len(text) > 90 if cjk else len(text.split()) > 35
        if too_long:
            add("long_bullet")
        if not _DIGIT_RE.search(strip_dates(text)):
            add("no_metric")
        token = _opener_token(text)
        if token:
            openers.setdefault(token, []).append(i)
    for idxs in openers.values():
        if len(idxs) >= _REPEATED_LIMIT:
            for i in idxs[1:]:
                findings.append({"index": i, "rule": "repeated_opener", "excerpt": bullets[i][:100]})
                penalties[i] += PENALTY["repeated_opener"]
    if not bullets:
        return findings, None
    score = round(sum(max(0, 100 - p) for p in penalties) / len(bullets))
    return findings, score


def metric_rate(bullets: list[str]) -> float | None:
    if not bullets:
        return None
    hit = sum(1 for b in bullets if _DIGIT_RE.search(strip_dates(b)))
    return hit / len(bullets)


def filler_hits(text: str) -> int:
    low = text.lower()
    return sum(low.count(w) for w in _FILLER_CN + _FILLER_EN)


# --------------------------------------------------------------------------
# Keywords
# --------------------------------------------------------------------------

_CJK_CLASS = r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]"


def keyword_in_text(keyword: str, text: str) -> bool:
    """Whole-term match; CJK keywords match as substrings (from Resume-Matcher)."""
    kw = keyword.strip().lower()
    if not kw:
        return False
    low = text.lower()
    if _CJK_RE.search(kw):
        return kw in low
    pattern = rf"(?:(?<!\w)|(?<={_CJK_CLASS})){re.escape(kw)}(?:(?!\w)|(?={_CJK_CLASS}))"
    return re.search(pattern, low) is not None


def keyword_coverage(text: str, keywords: list[str]) -> tuple[float | None, list[str]]:
    kws = [k.strip() for k in keywords if k.strip()]
    if not kws:
        return None, []
    missing = [k for k in kws if not keyword_in_text(k, text)]
    return round((len(kws) - len(missing)) / len(kws) * 100, 1), missing


# --------------------------------------------------------------------------
# Aggregate
# --------------------------------------------------------------------------

RULE_WEIGHTS = {
    "completeness": 0.25,
    "writing": 0.30,
    "metrics": 0.20,
    "date_consistency": 0.10,
    "keywords": 0.15,
}


def analyze(text: str, keywords: list[str] | None = None) -> dict[str, Any]:
    sections = split_sections(text)
    has_email = bool(EMAIL_RE.search(text))
    has_phone = bool(PHONE_RE.search(text))
    has_contact = has_email or has_phone
    found = {name: bool(sections.get(name)) for name in SECTION_SYNONYMS if name != "other"}
    required_hit = sum(1 for s in REQUIRED_SECTIONS if found.get(s))
    completeness = round((required_hit + (1 if has_contact else 0)) / (len(REQUIRED_SECTIONS) + 1) * 100, 1)

    bullets = extract_bullets(sections)
    findings, writing = check_bullets(bullets)
    rate = metric_rate(bullets)
    metrics_score = None if rate is None else round(min(100.0, rate / 0.5 * 100), 1)
    dates = date_consistency(text)
    kw_score, kw_missing = keyword_coverage(text, keywords or [])

    sub = {
        "completeness": completeness,
        "writing": None if writing is None else float(writing),
        "metrics": metrics_score,
        "date_consistency": dates,
        "keywords": kw_score,
    }
    scored = {k: v for k, v in sub.items() if v is not None}
    total_w = sum(RULE_WEIGHTS[k] for k in scored)
    rules_score = round(sum(v * RULE_WEIGHTS[k] for k, v in scored.items()) / total_w, 1) if total_w else 0.0

    return {
        "rules_score": rules_score,
        "sub_scores": sub,
        "char_count": len(text),
        "has_email": has_email,
        "has_phone": has_phone,
        "has_contact": has_contact,
        "sections_found": found,
        "missing_required_sections": [s for s in REQUIRED_SECTIONS if not found.get(s)],
        "bullet_count": len(bullets),
        "metric_rate": None if rate is None else round(rate, 3),
        "filler_hits": filler_hits(text),
        "date_styles": date_styles(text),
        "missing_keywords": kw_missing,
        "findings": findings,
        "bullets": bullets,
    }
