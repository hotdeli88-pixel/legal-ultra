"""templates/examples/ 의 예시 산출물이 실제로 게이트를 통과하는지(=문서 예시에 환각이 없는지) 보장한다."""

import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from _util import FIX, ROOT, history_repo

from citations import Verifier
from legal_engine import StatuteMirror
from legal_swarm import Board, run_gate
from precedent_engine import PrecedentMirror

EX = ROOT / "templates" / "examples"


def verifier():
    # 실제 판본 이력 발췌(정밀 판정) — 예시 산출물의 인용이 기준일 문언과 맞는지까지 확인한다
    return Verifier(StatuteMirror(str(history_repo())), PrecedentMirror(str(FIX / "precedent-kr")),
                    api=None, as_of=date(2026, 9, 28))


class TestExamples(unittest.TestCase):
    def test_each_example_passes_its_gate(self):
        v = verifier()
        for name, gate in (("evidence_statute.json", "statute_evidence"), ("evidence_precedent.json", "precedent_evidence"),
                           ("risk_memo.json", "risk_memo"), ("draft_opinion.md", "draft")):
            r = run_gate(gate, EX / name, EX, "2026-09-28", v)
            bad = [f for f in r["report"]["findings"] if f["status"] != "VERIFIED"]
            self.assertEqual((r["verdict"], r["problems"], bad), ("PASS", [], []), name)

    def test_draft_cites_only_evidence(self):
        r = run_gate("draft", EX / "draft_opinion.md", EX, "2026-09-28", verifier())
        self.assertEqual(r["report"]["outside_evidence"], [])

    def test_examples_run_through_board(self):
        ws = tempfile.mkdtemp(prefix="lu-ex-")
        b = Board(ws)
        cid = b.init_case("예시", "A가 B의 폭언을 신고", ["괴롭힘 여부", "조사 의무"], "노동", "2026-09-28", case_id="CASE-EXAMPLE")
        v = verifier()
        for role, name in (("statute_analyst", "evidence_statute.json"), ("precedent_analyst", "evidence_precedent.json"),
                           ("risk_advocate", "risk_memo.json"), ("counsel_builder", "draft_opinion.md")):
            pk = b.claim(cid, role, role)
            shutil.copy(EX / name, pk["outputs"][0])
            self.assertTrue(b.submit(cid, pk["task_id"], pk["token"], verifier=v)["accepted"], name)
        apk = b.claim(cid, "legal_auditor", "auditor")
        b.audit(cid, apk["token"], "approve", ["반대논리 반영 확인"], verifier=v)
        fin = b.finalize(cid, b.claim(cid, "lead_counsel", "lead")["token"])
        text = Path(fin["final"]).read_text(encoding="utf-8")
        self.assertIn("✅ PASS", text)
        self.assertTrue(json.loads((Path(fin["final"]).parent / "audit_verdict.json").read_text(encoding="utf-8"))["verdict"] == "approve")


if __name__ == "__main__":
    unittest.main()
