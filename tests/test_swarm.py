import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from _util import FIX

from citations import Verifier
from legal_engine import StatuteMirror
from legal_swarm import Board, GateError
from precedent_engine import PrecedentMirror

AS_OF = "2026-09-28"
Q76_2 = ("사용자 또는 근로자는 직장에서의 지위 또는 관계 등의 우위를 이용하여 업무상 적정범위를 넘어 "
         "다른 근로자에게 신체적ㆍ정신적 고통을 주거나 근무환경을 악화시키는 행위")
Q76_3 = "누구든지 직장 내 괴롭힘 발생 사실을 알게 된 경우 그 사실을 사용자에게 신고할 수 있다"
Q_PREC = "사업주, 상급자 또는 근로자는 직장 내 성희롱을 하여서는 아니 된다"

STATUTE_EV = {"case_id": "", "as_of": AS_OF, "provisions": [
    {"citation": "근로기준법 제76조의2", "quote": Q76_2, "relevance": "괴롭힘 금지의 근거"},
    {"citation": "근로기준법 제76조의3제1항", "quote": Q76_3, "relevance": "신고"},
    {"citation": "근로기준법 제116조제2항", "relevance": "조치의무 위반 과태료"}]}
PREC_EV = {"case_id": "", "precedents": [
    {"citation": "대법원 2021. 9. 16. 선고 2021다219529 판결", "holding_quote": Q_PREC,
     "relevance": "직장 내 성희롱 사용자책임(괴롭힘 사건 아님 — 유추 한계 명시)"}]}
RISK = {"case_id": "", "risks": [
    {"risk": "조사·조치 의무 위반 과태료", "severity": "medium", "basis": ["근로기준법 제116조제2항"],
     "counter_argument": "사용자는 사실 확인이 진행 중이었다고 주장할 수 있음", "mitigation": "조사 착수 기록 보존"}]}
DRAFT = f"""# 법률의견서: 직장 내 괴롭힘 신고 대응

## ⚡ 결론 및 즉시 행동
- 결론: 회사는 신고 즉시 조사 의무가 있다. 가능성 등급: 높음(근거: 「근로기준법」 제76조의3제1항).
- 지금 할 일: 신고 접수 기록을 보존한다.

## 1. 사실관계
- 직원 A가 상급자 B의 폭언을 신고하였다.

## 2. 쟁점
- 쟁점 1: B의 행위가 직장 내 괴롭힘인지

## 3. 관련 법령 및 판례
- 「근로기준법」 제76조의2는 “{Q76_2}”를 금지한다.
- 대법원 2021. 9. 16. 선고 2021다219529 판결은 “{Q_PREC}”라는 규정을 전제로 판단하였다(성희롱 사건).

## 4. 포섭
- 폭언은 업무상 적정범위를 넘는다고 볼 여지가 크다.

## 5. 대응 전략
- 미이행 시 「근로기준법」 제116조제2항의 과태료 위험.
"""


def verifier():
    return Verifier(StatuteMirror(str(FIX / "legalize-kr")), PrecedentMirror(str(FIX / "precedent-kr")),
                    api=None, as_of=date(2026, 9, 28))


class SwarmBase(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="lu-ws-")
        self.b = Board(self.ws)
        self.v = verifier()
        self.cid = self.b.init_case("괴롭힘 신고 대응", "A가 B의 폭언을 신고", ["괴롭힘 해당 여부"], "노동",
                                    AS_OF, case_id="CASE-T1")

    def write(self, packet, obj):
        p = Path(packet["outputs"][0])
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def submit(self, pk):
        return self.b.submit(self.cid, pk["task_id"], pk["token"], verifier=self.v)

    def run_to_audit(self, draft=DRAFT):
        for role, obj in (("statute_analyst", STATUTE_EV), ("precedent_analyst", PREC_EV), ("risk_advocate", RISK)):
            pk = self.b.claim(self.cid, role, role)
            self.write(pk, obj)
            r = self.submit(pk)
            self.assertTrue(r["accepted"], (role, r["problems"], r["report"]["findings"]))
        pk = self.b.claim(self.cid, "counsel_builder", "writer")
        self.write(pk, draft)
        r = self.submit(pk)
        self.assertTrue(r["accepted"], (r["problems"], [f for f in r["report"]["findings"] if f["status"] != "VERIFIED"]))
        return self.b.claim(self.cid, "legal_auditor", "auditor")


class TestHappyPath(SwarmBase):
    def test_end_to_end(self):
        self.assertIsNone(self.b.claim(self.cid, "counsel_builder", "early"))   # 의존성 미충족
        apk = self.run_to_audit()
        out = self.b.audit(self.cid, apk["token"], "approve", ["반대논리 반영 확인"], verifier=self.v)
        self.assertEqual(out["verifier_verdict"], "PASS")
        audit_file = Path(self.b.case_dir(self.cid)) / "audit_verdict.json"
        self.assertEqual(json.loads(audit_file.read_text(encoding="utf-8"))["generated_by"], "legal_swarm.py audit")
        lpk = self.b.claim(self.cid, "lead_counsel", "lead")
        fin = self.b.finalize(self.cid, lpk["token"])
        text = Path(fin["final"]).read_text(encoding="utf-8")
        self.assertIn("부록 A. 인용 검증 보고서", text)
        self.assertIn("✅ PASS", text)
        self.assertIn(out["draft_sha256"], text)
        self.assertEqual(self.b.board(self.cid)["case"]["status"], "delivered")


class TestGates(SwarmBase):
    def test_statute_gate_rejects_fabricated_and_keeps_task_running(self):
        pk = self.b.claim(self.cid, "statute_analyst", "s")
        bad = {**STATUTE_EV, "provisions": STATUTE_EV["provisions"] + [{"citation": "근로기준법 제76조의9"}]}
        self.write(pk, bad)
        r = self.submit(pk)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["verdict"], "FAIL")
        self.assertEqual(self.b.task(self.cid, "T1_STATUTE")["status"], "running")
        self.write(pk, STATUTE_EV)
        self.assertTrue(self.submit(pk)["accepted"])

    def test_precedent_gate_requires_holding_quote_and_rejects_distortion(self):
        pk = self.b.claim(self.cid, "precedent_analyst", "p")
        self.write(pk, {"precedents": [{"citation": "대법원 2021. 9. 16. 선고 2021다219529 판결"}]})
        self.assertIn("holding_quote", " ".join(self.submit(pk)["problems"]))
        self.write(pk, {"precedents": [{"citation": "대법원 2021. 9. 16. 선고 2021다219529 판결",
                                        "holding_quote": "직장 내 괴롭힘 판단 시 업무상 적정범위를 넘었는지에 대한 객관적 판단 기준"}]})
        self.assertFalse(self.submit(pk)["accepted"])
        self.write(pk, {"precedents": [], "no_precedent_reason": "직접 선례 없음"})
        self.assertTrue(self.submit(pk)["accepted"])

    def test_draft_gate_rejects_self_verdict_and_win_percentage(self):
        pk = None
        for role, obj in (("statute_analyst", STATUTE_EV), ("precedent_analyst", PREC_EV), ("risk_advocate", RISK)):
            p = self.b.claim(self.cid, role, role)
            self.write(p, obj)
            self.submit(p)
        pk = self.b.claim(self.cid, "counsel_builder", "w")
        self.write(pk, DRAFT + "\n검증 결과: PASS (무결점 승인)\n")
        self.assertFalse(self.submit(pk)["accepted"])
        self.write(pk, DRAFT.replace("가능성 등급: 높음", "승소 가능성 85%"))
        r = self.submit(pk)
        self.assertFalse(r["accepted"])
        self.assertTrue(any("확률" in x for x in r["problems"]))

    def test_submit_requires_valid_token(self):
        pk = self.b.claim(self.cid, "statute_analyst", "s")
        with self.assertRaises(GateError):
            self.b.submit(self.cid, "T1_STATUTE", "wrong", verifier=self.v)
        with self.assertRaises(GateError):
            self.b.claim(self.cid, "nobody", "x")
        self.assertIsNotNone(pk)


class TestAuditLoop(SwarmBase):
    def test_revise_reopens_draft_then_escalates_after_budget(self):
        apk = self.run_to_audit()
        for i in (1, 2):
            out = self.b.audit(self.cid, apk["token"], "revise", [f"반대논리 보강 {i}"], verifier=self.v)
            self.assertEqual(self.b.task(self.cid, "T4_DRAFT")["status"], "needs_revision")
            self.assertEqual(self.b.task(self.cid, "T5_AUDIT")["status"], "pending")
            wpk = self.b.claim(self.cid, "counsel_builder", "writer")      # v1: needs_revision 은 claim 불가(교착)
            self.assertEqual(wpk["revision_feedback"]["issues"][0], f"반대논리 보강 {i}")
            self.write(wpk, DRAFT + f"\n<!-- rev {i} -->\n")
            self.assertTrue(self.submit(wpk)["accepted"])
            apk = self.b.claim(self.cid, "legal_auditor", "auditor")
        out = self.b.audit(self.cid, apk["token"], "revise", ["여전히 부족"], verifier=self.v)
        self.assertTrue(out.get("escalated"))
        self.assertEqual(self.b.board(self.cid)["case"]["status"], "escalated")
        with self.assertRaises(GateError):
            self.b.claim(self.cid, "counsel_builder", "writer")

    def test_cannot_approve_failing_draft(self):
        apk = self.run_to_audit()
        draft = Path(self.b.case_dir(self.cid)) / "draft_opinion.md"
        draft.write_text(DRAFT + "\n- 근로기준법 제76조의9도 적용된다.\n", encoding="utf-8")   # 제출 후 변조
        with self.assertRaises(GateError):
            self.b.audit(self.cid, apk["token"], "approve", [], verifier=self.v)
        self.assertNotEqual(self.b.task(self.cid, "T6_FINAL")["status"], "running")

    def test_revise_verdict_never_unlocks_final(self):
        apk = self.run_to_audit()
        self.b.audit(self.cid, apk["token"], "revise", ["x"], verifier=self.v)
        self.assertIsNone(self.b.claim(self.cid, "lead_counsel", "lead"))   # v1: revise 여도 T5 최종본이 열림

    def test_finalize_rejects_draft_changed_after_approval(self):
        apk = self.run_to_audit()
        self.b.audit(self.cid, apk["token"], "approve", [], verifier=self.v)
        lpk = self.b.claim(self.cid, "lead_counsel", "lead")
        (Path(self.b.case_dir(self.cid)) / "draft_opinion.md").write_text(DRAFT + "\n추가 문단\n", encoding="utf-8")
        with self.assertRaises(GateError):
            self.b.finalize(self.cid, lpk["token"])


class TestIsolation(unittest.TestCase):
    def test_two_cases_do_not_collide_and_cwd_independent(self):
        ws = tempfile.mkdtemp(prefix="lu-ws-")
        b = Board(ws)
        a = b.init_case("A", "f", ["i"], as_of=AS_OF, case_id="CASE-A")
        pk = b.claim(a, "statute_analyst", "s")
        b.init_case("B", "f", ["i"], as_of=AS_OF, case_id="CASE-B")               # v1: 두 번째 init 이 A 를 덮어씀
        self.assertEqual(b.task(a, "T1_STATUTE")["status"], "running")
        self.assertEqual(b.task("CASE-B", "T1_STATUTE")["status"], "pending")
        with self.assertRaises(GateError):
            b.init_case("A2", "f", ["i"], as_of=AS_OF, case_id="CASE-A")          # 같은 ID 덮어쓰기 금지
        Path(pk["outputs"][0]).write_text(json.dumps(STATUTE_EV, ensure_ascii=False), encoding="utf-8")
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.mkdtemp())                                           # v1: CWD 상대경로라 실패
            r = b.submit(a, "T1_STATUTE", pk["token"], verifier=verifier())
        finally:
            os.chdir(cwd)
        self.assertTrue(r["accepted"], r["problems"])

    def test_verify_mode(self):
        ws = tempfile.mkdtemp(prefix="lu-ws-")
        src = Path(ws) / "doc.md"
        src.write_text(DRAFT, encoding="utf-8")
        b = Board(ws)
        cid = b.init_case("외부 문서 검증", "제3자가 작성한 의견서", ["인용 검증"], as_of=AS_OF, mode="verify", draft=str(src))
        pk = b.claim(cid, "legal_auditor", "auditor")
        self.assertEqual(pk["task_id"], "T5_AUDIT")
        out = b.audit(cid, pk["token"], "revise", ["결론 근거 보강 필요"], verifier=verifier())
        self.assertTrue(out.get("escalated"))   # verify 모드에는 집필 태스크가 없으므로 사람에게 넘긴다


if __name__ == "__main__":
    unittest.main()
