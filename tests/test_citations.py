import unittest
from datetime import date

from _util import FIX

from citations import (MISMATCH, NOT_FOUND, UNRESOLVED, UNVERIFIABLE, VERIFIED, Verifier, extract,
                       render_markdown)
from law_api import ApiUnavailable
from legal_engine import StatuteMirror
from precedent_engine import PrecedentMirror

AS_OF = date(2026, 9, 28)
Q76_2 = ("사용자 또는 근로자는 직장에서의 지위 또는 관계 등의 우위를 이용하여 업무상 적정범위를 넘어 "
         "다른 근로자에게 신체적ㆍ정신적 고통을 주거나 근무환경을 악화시키는 행위")
Q_ENBANC = "종전 판례가 제시한 고정성 개념은 통상임금의 개념적 징표에서 제외하는 것이 옳다"


def offline_verifier(api=None):
    return Verifier(StatuteMirror(str(FIX / "legalize-kr")), PrecedentMirror(str(FIX / "precedent-kr")),
                    api=api, as_of=AS_OF)


class TestExtract(unittest.TestCase):
    def labels(self, text):
        s, c, a = extract(text)
        return [(x.law, x.label) for x in s], [x.case_no for x in c], [x.agenda_no for x in a]

    def test_branch_article(self):
        s, _, _ = self.labels("근로기준법 제76조의9에 따라")
        self.assertEqual(s, [("근로기준법", "제76조의9")])   # v1: 제76조로 추출

    def test_lists_and_items(self):
        s, _, _ = self.labels("「개인정보 보호법」 제15조제1항제1호 및 제2호, 같은 법 제17조(개인정보의 제공)와 제18조")
        self.assertEqual(s, [("개인정보 보호법", "제15조제1항제1호"), ("개인정보 보호법", "제15조제1항제2호"),
                             ("개인정보 보호법", "제17조"), ("개인정보 보호법", "제18조")])

    def test_second_article_in_list_kept(self):
        s, _, _ = self.labels("근로기준법 제60조 및 제999조를 위반")
        self.assertEqual([x[1] for x in s], ["제60조", "제999조"])   # v1: 두 번째 누락

    def test_alias_and_anaphora(self):
        s, _, _ = self.labels("개인정보 보호법(이하 '법'이라 한다) 제15조 … 법 제17조, 근로기준법 시행령 제30조 및 같은 법 시행규칙 제9조")
        self.assertEqual(s, [("개인정보 보호법", "제15조"), ("개인정보 보호법", "제17조"),
                             ("근로기준법 시행령", "제30조"), ("근로기준법 시행규칙", "제9조")])

    def test_not_citations(self):
        s, c, _ = self.labels("계약은 2024년12월 체결되었고 매출은 3조 원, 750조 규모다.")
        self.assertEqual((s, c), ([], []))   # v1: '2024년12' 를 사건번호로 추출

    def test_cases(self):
        _, c, _ = self.labels("대법원 2024. 12. 19. 선고 2020다247190, 2023다302838 전원합의체 판결; 대판 2021다219529; 헌재 2013헌마576")
        self.assertEqual(c, ["2020다247190", "2023다302838", "2021다219529", "2013헌마576"])
        _, cs, _ = extract("대법원 2024. 12. 19. 선고 2020다247190, 2023다302838 전원합의체 판결")
        self.assertEqual(cs[1].decided, date(2024, 12, 19))   # 나열된 두 번째 사건도 선고일 승계

    def test_quote_attaches_to_nearest_citation_only(self):
        s, c, _ = extract("민법 제766조.\n대법원 2021. 9. 16. 선고 2021다219529 판결은 “직장 내 괴롭힘 판단 기준”을 제시하였다.")
        self.assertIsNone(s[0].quote)
        self.assertEqual(c[0].quote, "직장 내 괴롭힘 판단 기준")

    def test_law_name_does_not_cross_lines(self):
        s, _, _ = self.labels("CASE-1\n2026-09-28\n근로기준법 제60조\n행위자 개인의 불법행위 손해배상\n민법 제750조")
        self.assertEqual(s, [("근로기준법", "제60조"), ("민법", "제750조")])

    def test_authority(self):
        _, _, a = self.labels("법제처 22-0733 해석, 안건번호 20-0370")
        self.assertEqual(a, ["22-0733", "20-0370"])


class TestVerifyOffline(unittest.TestCase):
    def setUp(self):
        self.v = offline_verifier()

    def one(self, text):
        rep = self.v.verify_text(text)
        self.assertEqual(rep["total"], 1, rep["findings"])
        return rep["findings"][0]

    def test_verified_with_exact_quote(self):
        f = self.one(f"「근로기준법」 제76조의2(직장 내 괴롭힘의 금지)는 “{Q76_2}”를 금지한다.")
        self.assertEqual(f["status"], VERIFIED, f)

    def test_fabricated_branch_article(self):
        self.assertEqual(self.one("근로기준법 제76조의9")["status"], NOT_FOUND)

    def test_not_yet_in_force(self):
        self.assertEqual(self.one("근로기준법 제60조제9항은 불리한 처우를 금지한다.")["status"], MISMATCH)
        f = self.one("2027. 6. 10. 시행 예정인 개정 근로기준법 제60조제9항")
        self.assertEqual(f["status"], VERIFIED)
        self.assertTrue(any("시행예정" in w for w in f["warnings"]))

    def test_deleted_article(self):
        self.assertEqual(self.one("개인정보 보호법 제8조")["status"], MISMATCH)

    def test_missing_item(self):
        self.assertEqual(self.one("개인정보 보호법 제15조제1항제9호")["status"], NOT_FOUND)

    def test_title_mismatch(self):
        self.assertEqual(self.one("민법 제750조(불법행위의 책임)")["status"], MISMATCH)
        self.assertEqual(self.one("민법 제750조(불법행위의 내용)")["status"], VERIFIED)

    def test_paraphrased_statute_quote_rejected(self):
        f = self.one("민법 제750조는 “고의나 과실로 남에게 손해를 입힌 사람은 배상해야 한다”고 규정한다.")
        self.assertEqual(f["status"], MISMATCH)

    def test_fabricated_law_names(self):
        self.assertEqual(self.one("특별 근로기준법 제5조")["status"], UNRESOLVED)
        self.assertEqual(self.one("직장 내 괴롭힘 방지법 제3조")["status"], UNRESOLVED)

    def test_case_ok_and_date_mismatch(self):
        self.assertEqual(self.one("대법원 2025. 8. 14. 선고 2023다216777 판결")["status"], VERIFIED)
        self.assertEqual(self.one("대법원 2021. 9. 17. 선고 2021다219529 판결")["status"], MISMATCH)

    def test_distorted_holding(self):
        # legal-ultra v1 references/roles.md 예시: 실존 사건에 다른 판시를 붙인 왜곡
        f = self.one("대법원 2021다219529 판결은 “직장 내 괴롭힘 판단 시 업무상 적정범위를 넘었는지에 대한 객관적 판단 기준”을 제시하였다.")
        self.assertEqual(f["status"], MISMATCH)

    def test_enbanc_quote(self):
        self.assertEqual(self.one(f"대법원 2024. 12. 19. 선고 2020다247190 전원합의체 판결은 “{Q_ENBANC}”고 판시하였다.")["status"], VERIFIED)
        self.assertEqual(self.one("대법원 2024. 12. 19. 선고 2020다247190 판결은 “고정성은 통상임금의 개념적 징표가 아니다”고 하였다.")["status"], MISMATCH)

    def test_fabricated_case(self):
        self.assertEqual(self.one("대법원 1999다999999 판결")["status"], NOT_FOUND)
        self.assertEqual(self.one("대법원 2099. 1. 1. 선고 2099다1 판결")["status"], MISMATCH)

    def test_court_disambiguation(self):
        f = self.one("2008브4 결정")
        self.assertEqual(f["status"], VERIFIED)
        self.assertTrue(any("여러 법원" in w for w in f["warnings"]))
        self.assertEqual(self.one("서울고등법원 2008브4 결정")["status"], MISMATCH)

    def test_constitutional_needs_api(self):
        self.assertEqual(self.one("헌법재판소 2016. 10. 27. 선고 2013헌마576 결정")["status"], UNVERIFIABLE)

    def test_document_verdicts(self):
        self.assertEqual(self.v.verify_text("민법 제750조")["verdict"], "PASS")
        self.assertEqual(self.v.verify_text("사실관계만 있고 인용이 없는 문서")["verdict"], "NO_CITATIONS")  # v1: PASSED 100%
        self.assertEqual(self.v.verify_text("민법 제750조, 헌재 2013헌마576")["verdict"], "INCOMPLETE")
        self.assertEqual(self.v.verify_text("민법 제750조, 근로기준법 제76조의9")["verdict"], "FAIL")
        md = render_markdown(self.v.verify_text("민법 제750조, 근로기준법 제76조의9"))
        self.assertIn("FAIL", md)

    def test_evidence_closed_world_warning(self):
        rep = self.v.verify_text("민법 제750조 및 제766조", evidence_text="민법 제750조")
        self.assertEqual(rep["outside_evidence"], ["민법 제766조"])


class FakeApi:
    """DRF 대역. mode='down' 이면 접속 불가, 'lying' 이면 무엇을 물어도 다른 사건을 돌려준다(v1 첫 결과 폴백 재현용)."""

    def __init__(self, mode="ok"):
        self.mode = mode

    def _guard(self):
        if self.mode == "down":
            raise ApiUnavailable("법제처 API 접속 불가: 테스트")

    def constitutional_by_number(self, no):
        self._guard()
        return [{"사건번호": "2013헌마576", "종국일자": "2016.10.27", "사건명": "2012년도 대학교육역량강화사업 기본계획 취소 등",
                 "헌재결정례일련번호": "52626"}] if no == "2013헌마576" else []

    def constitutional_body(self, i):
        return {"결정요지": "가. 2012년도와 2013년도 대학교육역량강화사업 기본계획은 대학교육역량강화 지원사업을 추진하기 위한 국가의 기본방침을 밝히는 것"}

    def precedent_by_number(self, no):
        self._guard()
        if self.mode == "lying":   # 호출자가 사건번호 일치를 확인하지 않으면 통과시켜 버리는 응답
            return []
        return []

    def interpretation_by_number(self, no, target="expc"):
        self._guard()
        return [{"안건번호": "22-0733", "안건명": "감사원 - 교육공무원임용령 …", "회신일자": "2022.12.30",
                 "법령해석례일련번호": "335283"}] if no == "22-0733" else []

    def interpretation_body(self, i, target="expc"):
        return {"회답": "「교육공무원법」 제12조제1항제2호에 따라 교사를 특별채용하기 위하여"}

    def find_law(self, name, as_of=None):
        self._guard()
        return {"status": "not_found", "candidates": []}


class TestVerifyWithApi(unittest.TestCase):
    def test_constitutional_via_api(self):
        v = offline_verifier(FakeApi())
        f = v.verify_text("헌법재판소 2016. 10. 27. 선고 2013헌마576 결정")["findings"][0]
        self.assertEqual(f["status"], VERIFIED)
        f = v.verify_text("헌법재판소 2016. 10. 28. 선고 2013헌마576 결정")["findings"][0]
        self.assertEqual(f["status"], MISMATCH)

    def test_authority_via_api(self):
        v = offline_verifier(FakeApi())
        rep = v.verify_text("법제처 22-0733 해석은 “「교육공무원법」 제12조제1항제2호에 따라 교사를 특별채용하기 위하여”라고 회신")
        st = {f["citation"]: f["status"] for f in rep["findings"]}
        self.assertEqual(st["법제처 해석 안건 22-0733"], VERIFIED)
        self.assertEqual(v.verify_text("법제처 99-9999 해석")["findings"][0]["status"], NOT_FOUND)

    def test_network_failure_is_not_hallucination(self):
        # v1: 접속 실패 시 실존 조문을 '❌ 불일치'로 표시
        v = Verifier(None, None, FakeApi("down"), as_of=AS_OF)
        rep = v.verify_text("근로기준법 제60조, 대법원 2021다219529, 헌재 2013헌마576")
        self.assertEqual({f["status"] for f in rep["findings"]}, {UNVERIFIABLE})
        self.assertEqual(rep["verdict"], "INCOMPLETE")

    def test_no_first_result_fallback(self):
        v = Verifier(None, None, FakeApi("lying"), as_of=AS_OF)
        self.assertNotEqual(v.verify_text("대법원 2099다1 판결")["findings"][0]["status"], VERIFIED)


if __name__ == "__main__":
    unittest.main()
