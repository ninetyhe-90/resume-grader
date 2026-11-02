#!/usr/bin/env python3
"""Offline self-test: no network, no API key. Run: python3 -B scripts/selftest.py"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

# Isolate from the caller's environment: a real JEV_BASE_URL / key must never
# change test behaviour or make a test touch the network.
for _name in [n for n in os.environ if n.startswith(("JEV_", "TYPESAFE_"))]:
    del os.environ[_name]

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_client  # noqa: E402
import resume_features as rf  # noqa: E402
import score_resume as sr  # noqa: E402

STRONG = """张三
邮箱: zhangsan@example.com  手机: 138 0013 8000

个人简介
五年后端开发经验,专注高并发交易系统。

工作经历
某某科技 高级后端工程师  2021.03 - 至今
- 主导订单系统重构,P99 延迟从 800ms 降到 120ms,支撑日均 2000 万订单
- 设计分库分表方案,数据库成本下降 35%,迁移零停机
- 搭建灰度发布平台,发布事故率下降 60%,覆盖 40 个服务
某某网络 后端工程师  2019.07 - 2021.02
- 实现消息推送服务,日均触达 500 万用户,到达率 99.5%
- 优化慢查询 30 余处,接口平均耗时降低 45%

教育背景
某某大学 计算机科学 本科  2015.09 - 2019.06

专业技能
Java, Go, Kafka, MySQL, Redis, Kubernetes
"""

WEAK = """李四
工作经历
负责公司的日常工作
参与项目开发
协助同事完成任务
负责文档整理
我是一个责任心强、学习能力强的人
"""


def fake_transport(levels: dict[str, float] | None = None, noul: dict[str, float] | None = None,
                   verdict: str = "good", conf: float = 0.9, status: int = 200, calls: list | None = None):
    levels = levels or {}
    noul = noul or {}

    def transport(url, headers, body, timeout):
        if calls is not None:
            calls.append((url, headers, json.loads(body)))
        if status != 200:
            return status, {"retry-after": "0"}, b'{"detail":"boom"}'
        req = json.loads(body)
        answers = {}
        for qid, q in req["questions"].items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": noul.get(qid, 0.1)}
            elif q["type"] == "choice":
                probs = {o: (conf if o == verdict else (1 - conf) / 2) for o in q["criteria"]}
                answers[qid] = {"type": "choice", "choice": verdict, "confidence": conf, "probabilities": probs}
            else:
                n = len(q["criteria"])
                lvl = levels.get(qid, 2.0)
                lo, hi = int(lvl), min(n - 1, int(lvl) + 1)
                probs = {str(i): 0.0 for i in range(n)}
                probs[str(lo)] = 1.0 if lo == hi else hi - lvl
                if lo != hi:
                    probs[str(hi)] = lvl - lo
                answers[qid] = {"type": "score", "score": lvl, "confidence": 0.9, "probabilities": probs}
        return 200, {}, json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

    return transport


class FeatureTests(unittest.TestCase):
    def test_strong_resume_rules(self):
        f = rf.analyze(STRONG)
        self.assertTrue(f["has_contact"])
        self.assertEqual(f["missing_required_sections"], [])
        self.assertGreaterEqual(f["metric_rate"], 0.9)
        self.assertEqual(f["date_styles"], {"dot": 5})
        self.assertEqual(rf.date_consistency(STRONG), 100.0)
        self.assertGreater(f["rules_score"], 85)

    def test_weak_resume_rules(self):
        f = rf.analyze(WEAK)
        self.assertFalse(f["has_contact"])
        self.assertIn("education", f["missing_required_sections"])
        self.assertEqual(f["metric_rate"], 0.0)
        self.assertGreaterEqual(f["filler_hits"], 2)
        self.assertLess(f["rules_score"], 60)

    def test_mixed_dates_lower_consistency(self):
        text = "公司A 2020.03 - 2021.05\n公司B 2018年6月 - 2020年2月\n公司C Mar 2016 - Jun 2018"
        self.assertLess(rf.date_consistency(text), 100.0)

    def test_year_numbers_outside_ranges_ignored(self):
        self.assertIsNone(rf.date_consistency("served 2000 users in 2021"))

    def test_keyword_matching_boundaries(self):
        self.assertTrue(rf.keyword_in_text("Python", "熟悉Python开发"))
        self.assertFalse(rf.keyword_in_text("java", "javascript"))
        self.assertTrue(rf.keyword_in_text("分布式", "负责分布式系统"))
        cov, missing = rf.keyword_coverage("Python Kafka", ["python", "kafka", "go"])
        self.assertEqual(missing, ["go"])
        self.assertAlmostEqual(cov, 66.7, places=1)

    def test_garbled_text_rejected(self):
        p = Path("/tmp/_resume_garbled.txt")
        p.write_text("(cid:1)(cid:2)(cid:3)(cid:4)(cid:5) abc", encoding="utf-8")
        with self.assertRaises(rf.ResumeError):
            rf.load_resume_text(str(p))


def make_docx(path: Path, paragraphs: list[str]) -> None:
    import zipfile
    from xml.sax.saxutils import escape
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


def make_pdf(path: Path, lines: list[str]) -> None:
    """Minimal single-page text PDF (ASCII only, Helvetica)."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 11 Tf 40 800 Td 16 TL\n" + "\n".join(f"({esc(l)}) Tj T*" for l in lines) + "\nET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


EN_RESUME = [
    "Jane Doe", "jane.doe@example.com  +1 415 555 0100",
    "Experience", "Acme Corp - Senior Backend Engineer   Mar 2021 - Present",
    "- Led the order system rewrite, cutting P99 latency from 800ms to 120ms",
    "- Designed sharding that reduced database cost by 35%",
    "- Built a canary platform covering 40 services",
    "Education", "State University - BSc Computer Science   2015 - 2019",
    "Skills", "Java, Go, Kafka, MySQL",
]


class FormatTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix="resume-grader-test-"))

    def test_docx_roundtrip_matches_plain_text(self):
        p = self.dir / "r.docx"
        make_docx(p, STRONG.split("\n"))
        text = rf.load_resume_text(str(p))
        a, b = rf.analyze(text), rf.analyze(STRONG)
        for k in ("rules_score", "has_contact", "missing_required_sections", "bullet_count", "metric_rate", "date_styles"):
            self.assertEqual(a[k], b[k], k)

    def test_docx_table_layout_dates_on_own_line(self):
        p = self.dir / "t.docx"
        make_docx(p, ["李四", "lisi@example.com", "工作经历", "某某公司 后端工程师", "2020.03 - 2023.06",
                      "主导订单系统重构,P99 延迟降低 60%", "教育背景", "某某大学 计算机", "2016.09 - 2020.06",
                      "专业技能", "Java, Go"])
        f = rf.analyze(rf.load_resume_text(str(p)))
        self.assertEqual(f["date_styles"], {"dot": 4})
        self.assertNotIn("03 - 2023.06", f["bullets"])
        self.assertTrue(all(not rf._is_date_only(b) for b in f["bullets"]))

    def test_year_prefix_is_not_a_list_number(self):
        self.assertIsNone(rf._BULLET_MARK.match("2020.03 - 2023.06"))
        self.assertIsNotNone(rf._BULLET_MARK.match("1. did a thing"))
        self.assertIsNotNone(rf._BULLET_MARK.match("12) did a thing"))

    def test_date_only_lines_are_not_bullets(self):
        for line in ("2020.03 - 2023.06", "Mar 2021 - Present", "2019年6月 至今"):
            self.assertTrue(rf._is_date_only(line), line)
        self.assertFalse(rf._is_date_only("Led a team of 5 from 2020 to 2022"))

    @unittest.skipUnless(__import__("shutil").which("pdftotext"), "pdftotext not installed")
    def test_pdf_english_resume(self):
        p = self.dir / "r.pdf"
        make_pdf(p, EN_RESUME)
        f = rf.analyze(rf.load_resume_text(str(p)))
        self.assertTrue(f["has_contact"])
        self.assertEqual(f["missing_required_sections"], [])
        self.assertEqual(f["bullet_count"], 3)
        self.assertEqual(f["metric_rate"], 1.0)

    @unittest.skipUnless(__import__("shutil").which("pdftotext"), "pdftotext not installed")
    def test_pdf_without_text_layer_is_an_error(self):
        p = self.dir / "scan.pdf"
        make_pdf(p, [""])
        with self.assertRaises(rf.ResumeError):
            rf.load_resume_text(str(p))

    def test_bad_inputs_raise_resume_error(self):
        bad_zip = self.dir / "bad.docx"; bad_zip.write_text("not a zip")
        old_doc = self.dir / "old.doc"; old_doc.write_text("x")
        empty = self.dir / "empty.txt"; empty.write_text("")
        for p in (bad_zip, old_doc, empty, self.dir / "missing.txt"):
            with self.assertRaises(rf.ResumeError, msg=p.name):
                rf.load_resume_text(str(p))

    def test_json_resume_is_flattened(self):
        p = self.dir / "r.json"
        p.write_text(json.dumps({"name": "A", "email": "a@b.com", "items": ["工作经历", "负责开发"]}), encoding="utf-8")
        self.assertIn("a@b.com", rf.load_resume_text(str(p)))
        bad = self.dir / "bad.json"; bad.write_text("{oops")
        with self.assertRaises(rf.ResumeError):
            rf.load_resume_text(str(bad))


class JevClientTests(unittest.TestCase):
    def test_missing_key_raises(self):
        import os
        saved = {k: os.environ.pop(k, None) for k in ("JEV_API_KEY", "TYPESAFE_API_KEY")}
        try:
            with self.assertRaises(jev_client.JevError):
                jev_client.call_jev({"resume": "x"}, jev_client.build_questions(False))
        finally:
            for k, v in saved.items():
                if v:
                    os.environ[k] = v

    def test_endpoint_must_be_https(self):
        import os
        os.environ.pop("JEV_ALLOW_HTTP", None)
        with self.assertRaises(jev_client.JevError):
            jev_client.validate_endpoint("http://10.0.0.1/v1/systemone")
        with self.assertRaises(jev_client.JevError):
            jev_client.validate_endpoint("http://203.0.113.10/v1/systemone")
        jev_client.validate_endpoint("http://localhost:8000/v1/systemone")
        jev_client.validate_endpoint("https://api.typesafe.ai/v1/systemone")

    def test_plain_http_requires_explicit_opt_in(self):
        import os
        os.environ["JEV_ALLOW_HTTP"] = "1"
        try:
            jev_client.validate_endpoint("http://203.0.113.10/v1/systemone")
        finally:
            os.environ.pop("JEV_ALLOW_HTTP", None)

    def test_base_url_and_key_env_names(self):
        import os
        saved = {k: os.environ.get(k) for k in ("JEV_BASE_URL", "JEV_ENDPOINT", "JEV_API_KEY")}
        os.environ.pop("JEV_ENDPOINT", None)
        os.environ["JEV_BASE_URL"] = "https://gw.example.com/"
        os.environ["JEV_API_KEY"] = "k-from-env"
        try:
            calls: list = []
            jev_client.call_jev({"resume": "x"}, {}, transport=fake_transport(calls=calls))
            self.assertEqual(calls[0][0], "https://gw.example.com/v1/systemone")
            self.assertEqual(calls[0][1]["Authorization"], "Bearer k-from-env")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_retry_then_fail(self):
        calls: list = []
        with self.assertRaises(jev_client.JevError):
            jev_client.call_jev({"resume": "x"}, {}, api_key="k", transport=fake_transport(status=429, calls=calls),
                                sleep=lambda s: None)
        self.assertEqual(len(calls), jev_client.MAX_RETRIES + 1)

    def test_no_retry_on_auth_error(self):
        calls: list = []
        with self.assertRaises(jev_client.JevError):
            jev_client.call_jev({"resume": "x"}, {}, api_key="k", transport=fake_transport(status=401, calls=calls))
        self.assertEqual(len(calls), 1)

    def test_noul_questions_carry_no_criteria(self):
        # The gateway rejects any criteria on noul questions (HTTP 400), even null.
        for has_jd in (False, True):
            for qid, q in jev_client.build_questions(has_jd).items():
                if q["type"] == "noul":
                    self.assertNotIn("criteria", q, qid)
                else:
                    self.assertTrue(q["criteria"], qid)

    def test_malformed_answer_rejected(self):
        qs = jev_client.build_questions(False)
        with self.assertRaises(jev_client.JevError):
            jev_client.parse_answers({"answers": {}}, qs)
        bad = json.loads(fake_transport()("u", {}, json.dumps({"questions": qs}).encode(), 1)[2])
        bad["answers"]["vague_claims"]["noul"] = 1.5
        with self.assertRaises(jev_client.JevError):
            jev_client.parse_answers(bad, qs)

    def test_score_expected_value_uses_probabilities(self):
        qs = {"impact": jev_client.build_questions(False)["impact"]}
        payload = {"answers": {"impact": {"type": "score", "score": 3, "confidence": 0.5,
                                           "probabilities": {"2": 0.5, "3": 0.5}}}}
        self.assertEqual(jev_client.parse_answers(payload, qs)["impact"]["pct"], 62.5)


class ScoringTests(unittest.TestCase):
    def run_score(self, text, transport=None, **kw):
        import os
        os.environ["JEV_API_KEY"] = "test-key"
        return sr.score(text, transport=transport or fake_transport(), **kw)

    def test_excellent(self):
        t = fake_transport(levels={"impact": 4, "depth": 3.5, "clarity": 3.5}, verdict="excellent", conf=0.95)
        r = self.run_score(STRONG, t)
        self.assertEqual(r["grade"], "优秀")
        self.assertFalse(r["caps_applied"])

    def test_good(self):
        r = self.run_score(STRONG, fake_transport(levels={"impact": 2, "depth": 2, "clarity": 2}, verdict="good"))
        self.assertEqual(r["grade"], "良")

    def test_poor(self):
        t = fake_transport(levels={"impact": 0, "depth": 1, "clarity": 1}, verdict="poor",
                           noul={"vague_claims": 0.9})
        r = self.run_score(WEAK, t)
        self.assertEqual(r["grade"], "差")

    def test_inconsistency_caps_excellent_at_good(self):
        t = fake_transport(levels={"impact": 4, "depth": 4, "clarity": 4}, verdict="excellent", conf=0.99,
                           noul={"inconsistency": 0.95})
        r = self.run_score(STRONG, t)
        self.assertEqual(r["grade"], "良")
        self.assertEqual(r["grade_before_caps"], "优秀")
        self.assertTrue(r["caps_applied"])

    def test_missing_contact_caps_at_good(self):
        text = STRONG.replace("zhangsan@example.com", "").replace("138 0013 8000", "")
        t = fake_transport(levels={"impact": 4, "depth": 4, "clarity": 4}, verdict="excellent", conf=0.99)
        r = self.run_score(text, t)
        self.assertEqual(r["grade"], "良")

    def test_low_verdict_confidence_flags_review(self):
        r = self.run_score(STRONG, fake_transport(verdict="good", conf=0.4))
        self.assertTrue(r["needs_human_review"])

    def test_one_grade_disagreement_with_verdict_flags_review(self):
        # Jev says "good" with high confidence while the composite lands in excellent.
        t = fake_transport(levels={"impact": 4, "depth": 4, "clarity": 4}, verdict="good", conf=0.9)
        r = self.run_score(STRONG, t)
        self.assertEqual(r["grade"], "优秀")
        self.assertTrue(any(f.startswith("disagreement") for f in r["flags"]))
        self.assertTrue(r["needs_human_review"])

    def test_agreement_does_not_flag(self):
        t = fake_transport(levels={"impact": 4, "depth": 3.5, "clarity": 3.5}, verdict="excellent", conf=0.95)
        r = self.run_score(STRONG, t)
        self.assertFalse(any(f.startswith("disagreement") for f in r["flags"]))

    def test_jd_adds_relevance_questions(self):
        calls: list = []
        r = self.run_score(STRONG, fake_transport(calls=calls), jd="招聘高级后端工程师,要求精通 Kafka")
        sent = calls[0][2]
        self.assertIn("relevance", sent["questions"])
        self.assertIn("target_job", sent["state"])
        self.assertIn("relevance", r["jev"]["parts"])

    def test_redaction_before_sending(self):
        calls: list = []
        self.run_score(STRONG, fake_transport(calls=calls))
        body = json.dumps(calls[0][2], ensure_ascii=False)
        self.assertNotIn("zhangsan@example.com", body)
        self.assertNotIn("138 0013 8000", body)
        self.assertIn("[EMAIL]", body)
        self.assertNotIn("test-key", body)

    def test_no_redact_flag_sends_raw(self):
        calls: list = []
        self.run_score(STRONG, fake_transport(calls=calls), do_redact=False)
        self.assertIn("zhangsan@example.com", json.dumps(calls[0][2], ensure_ascii=False))

    def test_too_long_resume_is_an_error(self):
        with self.assertRaises(sr.ScoreError):
            self.run_score("x" * 20000, fake_transport())

    def test_rules_only_is_labelled(self):
        r = sr.score(STRONG, use_jev=False)
        self.assertIsNone(r["jev"])
        self.assertTrue(any(f.startswith("rules_only") for f in r["flags"]))
        self.assertIn("NONE", sr.render_markdown(r))

    def test_short_chinese_duty_bullets_are_checked(self):
        f = rf.analyze(WEAK)
        self.assertEqual(f["bullet_count"], 5)
        self.assertGreaterEqual(sum(1 for x in f["findings"] if x["rule"] == "weak_opener"), 4)

    def test_rules_only_never_excellent(self):
        r = sr.score(STRONG, use_jev=False)
        self.assertGreaterEqual(r["composite"], 80)
        self.assertEqual(r["grade_before_caps"], "优秀")
        self.assertEqual(r["grade"], "良")

    def test_tiny_resume_capped_at_poor(self):
        r = sr.score("张三 a@b.com\n工作经历\n- 做过开发 2020.01 - 2021.01", use_jev=False)
        self.assertEqual(r["grade"], "差")

    def test_boundary_detection(self):
        self.assertTrue(sr.is_boundary(61.5, 80, 60))
        self.assertFalse(sr.is_boundary(70, 80, 60))

    def test_threshold_validation(self):
        with self.assertRaises(sr.ScoreError):
            sr.score(STRONG, use_jev=False, excellent=50, good=60)

    def test_markdown_render_has_no_emoji(self):
        r = self.run_score(STRONG, fake_transport())
        md = sr.render_markdown(r)
        self.assertTrue(all(ord(c) < 0x2000 or 0x3000 <= ord(c) < 0xA000 or ord(c) in range(0xFF00, 0xFFF0)
                            for c in md))


if __name__ == "__main__":
    unittest.main(verbosity=2)
