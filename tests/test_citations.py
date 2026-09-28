"""인용 추출·검증 테스트. 법령은 실제 legalize-kr 판본 이력 발췌(tests/fixtures/legalize-kr-history)를 임시 git 저장소로
재생해 정밀 판정으로 검증하고, 이력 없는 미러(tests/fixtures/legalize-kr, 최신본만)는 fail-closed 여부를 따로 본다.
'#N' 은 2차 적대적 검토(docs/REVIEW-2026-09-28.md §F)의 결함 번호."""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from _util import FIX, has_git, history_repo

from citations import (MISMATCH, NOT_FOUND, UNRESOLVED, UNVERIFIABLE, VERIFIED, Verifier, extract, extract_all,
                       norm_court, render_markdown)
from law_api import ApiUnavailable
from legal_engine import StatuteMirror
from precedent_engine import PrecedentMirror

AS_OF = date(2026, 9, 28)
Q76_2 = ("사용자 또는 근로자는 직장에서의 지위 또는 관계 등의 우위를 이용하여 업무상 적정범위를 넘어 "
         "다른 근로자에게 신체적ㆍ정신적 고통을 주거나 근무환경을 악화시키는 행위")
Q76_2_FULL = Q76_2 + "(이하 \"직장 내 괴롭힘\"이라 한다)를 하여서는 아니 된다"
Q750 = "고의 또는 과실로 인한 위법행위로 타인에게 손해를 가한 자는 그 손해를 배상할 책임이 있다"
Q_ENBANC = "종전 판례가 제시한 고정성 개념은 통상임금의 개념적 징표에서 제외하는 것이 옳다"
Q_HARASS = "성희롱이 성립하기 위해서 행위자에게 반드시 성적 동기나 의도가 있어야 하는 것은 아니지만"
PREC = str(FIX / "precedent-kr")
_V = {}


def verifier(as_of=AS_OF, api=None, history=True) -> Verifier:
    """판본 이력 미러(정밀) 또는 최신본만 있는 미러(추정)."""
    key = (as_of, history)
    if api is None and key in _V:
        return _V[key]
    root = str(history_repo()) if history else str(FIX / "legalize-kr")
    v = Verifier(StatuteMirror(root), PrecedentMirror(PREC), api=api, as_of=as_of)
    if api is None:
        _V[key] = v
    return v


def offline_verifier(api=None):   # test_swarm·test_examples 호환
    return verifier(api=api)


def statuses(text, **kw):
    return [f["status"] for f in verifier(**kw).verify_text(text)["findings"]]


def one(text, **kw):
    rep = verifier(**kw).verify_text(text)
    assert rep["total"] == 1, rep["findings"]
    return rep["findings"][0]


class TestExtract(unittest.TestCase):
    def labels(self, text):
        s, c, a = extract(text)
        return [(x.law, x.label) for x in s], [x.case_no for x in c], [x.agenda_no for x in a]

    def test_branch_article(self):
        s, _, _ = self.labels("근로기준법 제76조의9에 따라")
        self.assertEqual(s, [("근로기준법", "제76조의9")])   # v1: 제76조로 추출

    def test_branch_item_standard_and_legacy_forms(self):   # #4
        s, _, _ = self.labels("개인정보 보호법 제2조제1호의9, 같은 법 제2조제1의2호")
        self.assertEqual([x[1] for x in s], ["제2조제1호의9", "제2조제1호의2"])

    def test_circled_paragraph_and_invisible_chars(self):   # #10
        s, _, _ = self.labels("민법 제750조②에 따라, 민법 제750조 제②항, **근로기준법** 제76조의​9, 근로기준법 제７６조")
        self.assertEqual(s, [("민법", "제750조제2항"), ("민법", "제750조제2항"), ("근로기준법", "제76조의9"), ("근로기준법", "제76조")])

    def test_lists_ranges_and_same_units(self):   # #10
        s, _, _ = self.labels("「개인정보 보호법」 제15조제1항제1호 및 제2호, 같은 항 제9호, 같은 법 제17조(개인정보의 제공)와 제18조, "
                              "형법 제250조제1항 내지 제9항, 개인정보 보호법 제2조제1호부터 제3호까지")
        self.assertEqual(s, [("개인정보 보호법", "제15조제1항제1호"), ("개인정보 보호법", "제15조제1항제2호"),
                             ("개인정보 보호법", "제15조제1항제9호"), ("개인정보 보호법", "제17조"), ("개인정보 보호법", "제18조"),
                             ("형법", "제250조제1항"), ("형법", "제250조제9항"),
                             ("개인정보 보호법", "제2조제1호"), ("개인정보 보호법", "제2조제3호")])

    def test_mok_letters(self):   # #10, #20
        s, _, _ = extract("개인정보 보호법 제2조제1호 각 목의 정보, 같은 법 제2조제1호가목ㆍ나목")
        self.assertEqual([(x.label, x.problem) for x in s], [("제2조제1호", None), ("제2조제1호가목", None), ("제2조제1호나목", None)])
        s, _, _ = extract("민법 제750조가목")
        self.assertIsNotNone(s[0].problem)                    # 호 없는 목 → 특정 불가
        s, _, _ = extract("민법 제750조가 목적으로 하는 바")
        self.assertIsNone(s[0].mok)

    def test_second_article_in_list_kept(self):
        s, _, _ = self.labels("근로기준법 제60조 및 제999조를 위반")
        self.assertEqual([x[1] for x in s], ["제60조", "제999조"])   # v1: 두 번째 누락

    def test_alias_positional(self):   # #12, #20
        s, _, _ = self.labels("「개인정보 보호법」(이하 \"법\"이라 한다) 제15조. 또한 법 제17조.\n\n"
                              "「근로기준법」(이하 \"법\"이라 한다) 제23조. 법 제24조.")
        self.assertEqual(s, [("개인정보 보호법", "제15조"), ("개인정보 보호법", "제17조"), ("근로기준법", "제23조"), ("근로기준법", "제24조")])
        s, _, _ = self.labels("「중대재해 처벌 등에 관한 법률」(이하 '중대재해처벌법'이라 한다) 제6조. 중대재해처벌법 제4조")
        self.assertEqual(s, [("중대재해 처벌 등에 관한 법률", "제6조"), ("중대재해 처벌 등에 관한 법률", "제4조")])
        s, _, _ = extract("법 제107조에 따라 처벌된다. 「개인정보 보호법」(이하 \"법\"이라 한다)")
        self.assertIsNone(s[0].law)                          # 정의보다 앞의 약칭은 특정 불가

    def test_anaphora_to_last_mention(self):   # #20
        s, _, _ = self.labels("근로기준법 및 같은 법 시행령 제30조, 근로기준법 시행령 제30조 및 같은 법 시행규칙 제9조")
        self.assertEqual(s, [("근로기준법 시행령", "제30조"), ("근로기준법 시행령", "제30조"), ("근로기준법 시행규칙", "제9조")])

    def test_no_implicit_law(self):   # #12
        s, _, _ = extract("민법 제750조와 달리 상법에서는 제24조가 명의대여자의 책임을 정한다.")
        self.assertEqual([(x.law, x.label) for x in s], [("민법", "제750조"), ("상법", "제24조")])
        s, _, _ = extract("근로기준법 제23조를 본다. 또한 제999조가 있다.")
        self.assertEqual(s[1].law, None)
        self.assertIsNotNone(s[1].problem)                   # 앞 문장의 법령으로 추정하지 않는다

    def test_contract_clauses_are_not_statutes(self):   # #12, #20
        s, _, _ = self.labels("취업규칙 제5조와 근로계약서 제12조, 정관 제20조, 이용약관 제7조, 회사 인사규정 제3조를 검토했다. 근로기준법 제23조")
        self.assertEqual(s, [("근로기준법", "제23조")])

    def test_prose_before_law_name(self):   # #20
        s, _, _ = extract("회사는 취업규칙 개정 후 근로기준법 제23조를 위반하였다.")
        self.assertIn("근로기준법", s[0].law_candidates)

    def test_not_citations(self):
        s, c, _ = self.labels("계약은 2024년12월 체결되었고 매출은 3조 원, 750조 규모다.")
        self.assertEqual((s, c), ([], []))   # v1: '2024년12' 를 사건번호로 추출

    def test_cases_and_date_formats(self):   # #9
        _, c, _ = self.labels("대법원 2024. 12. 19. 선고 2020다247190, 2023다302838 전원합의체 판결; 대판 2021다219529; 헌재 2013헌마576")
        self.assertEqual(c, ["2020다247190", "2023다302838", "2021다219529", "2013헌마576"])
        _, cs, _ = extract("대법원 2024. 12. 19. 선고 2020다247190, 2023다302838 전원합의체 판결")
        self.assertEqual(cs[1].decided, date(2024, 12, 19))   # 나열된 두 번째 사건도 선고일 승계
        for t in ("서울고등법원 2021년 9월 17일 선고 2021다219529 판결", "서울고등법원 2021-09-17 선고 2021다219529 판결",
                  "서울고등법원 2021. 9. 17.자 2021다219529 결정"):
            _, cs, _ = extract(t)
            self.assertEqual((cs[0].court, cs[0].decided), ("서울고등법원", date(2021, 9, 17)), t)

    def test_court_normalization(self):   # #19
        self.assertEqual(norm_court("서울고법"), norm_court("서울고등법원"))
        self.assertEqual(norm_court("서울가법"), "서울가정법원")
        self.assertEqual(norm_court("부산고등법원(창원)"), norm_court("부산고등법원 창원재판부"))

    def test_authority_patterns(self):   # #10
        _, _, a = self.labels("법제처 22-0733 해석, 안건번호 20-0370, 법령해석례 21-0101")
        self.assertEqual(a, ["22-0733", "20-0370", "21-0101"])
        _, _, a = extract("고용노동부 행정해석(근로기준정책과-9999)")
        self.assertTrue(a and a[0].ministry)


class TestQuotes(unittest.TestCase):
    def q(self, text):
        ex = extract_all(text)
        return [(type(c).__name__[0], c.quotes) for c in ex.citations()], [o.text for o in ex.orphans]

    def test_positions(self):   # #6
        self.assertEqual(self.q("“" + Q750 + "”(민법 제750조)."), ([("S", [Q750])], []))
        self.assertEqual(self.q("민법 제750조에 관하여 다음과 같이 규정한다.\n“" + Q750 + ".”"), ([("S", [Q750 + "."])], []))
        self.assertEqual(self.q("민법 제750조는 ‘" + Q750 + "’고 규정한다."), ([("S", [Q750])], []))
        self.assertEqual(self.q("민법 제750조는 다음과 같이 규정한다:\n> " + Q750), ([("S", [Q750])], []))
        cites, _ = self.q("대법원 2021. 9. 16. 선고 2021다219529 판결 참조. 위 판결은 “" + Q_HARASS + "”라고 판시하였다.")
        self.assertEqual(cites, [("C", [Q_HARASS])])

    def test_nested_and_inner_citations(self):   # #7
        cites, _ = self.q("근로기준법 제76조의2는 “" + Q76_2_FULL + "”고 규정한다.")
        self.assertEqual(cites, [("S", [Q76_2_FULL])])           # 안쪽 "…"에서 잘리지 않는다
        cites, _ = self.q("근로기준법 제116조제1항은 “사용자가 제76조의2를 위반하여 직장 내 괴롭힘을 한 경우에는 과태료”라고 규정한다.")
        self.assertEqual(len(cites), 1)                           # 인용문 안의 '제76조의2'는 따로 뽑지 않는다
        self.assertTrue(cites[0][1][0].startswith("사용자가 제76조의2"))

    def test_party_statements_not_attached(self):   # #20
        cites, orphans = self.q("원고는 민법 제750조에 근거하여 “피고가 고의로 창고에 불을 질렀다”고 주장한다.")
        self.assertEqual((cites, orphans), ([("S", [])], []))
        cites, _ = self.q("민법 제750조는 불법행위 책임을 정한다. 한편 A는 “나는 그 자리에 없었다”고 진술하였다.")
        self.assertEqual(cites, [("S", [])])

    def test_disguised_holding_is_attached(self):
        cites, _ = self.q("대법원 2021. 9. 16. 선고 2021다219529 판결은 “사용자는 어떤 경우에도 책임이 없다”라고 진술하였다.")
        self.assertEqual(cites, [("C", ["사용자는 어떤 경우에도 책임이 없다"])])

    def test_orphan_legal_quote(self):
        _, orphans = self.q("“사용자는 해고 30일 전에 예고하여야 한다”라고 규정하고 있다.")
        self.assertEqual(orphans, ["사용자는 해고 30일 전에 예고하여야 한다"])

    def test_enumeration_quote_scope(self):
        ex = extract_all("민법 제750조 및 제751조는 “" + Q750 + "”고 규정한다.")
        self.assertEqual(ex.statutes[0].quote_units, [(750, None, None, None, None), (751, None, None, None, None)])

    def test_future_context_negation(self):   # #11
        s, _, _ = extract("시행 예정이 아닌 현행 근로기준법 제60조제9항은 불리한 처우를 금지한다.")
        self.assertFalse(s[0].future_context)
        s, _, _ = extract("2027. 6. 10. 시행 예정인 개정 근로기준법 제60조제9항")
        self.assertTrue(s[0].future_context)


@unittest.skipUnless(has_git(), "git 필요")
class TestStatutesPrecise(unittest.TestCase):
    """판본 이력 대조(정밀) — 실제 판본 발췌로 재생한 git 저장소."""

    def test_verified_with_exact_quote(self):
        f = one(f"「근로기준법」 제76조의2(직장 내 괴롭힘의 금지)는 “{Q76_2}”를 금지한다.")
        self.assertEqual(f["status"], VERIFIED, f)
        self.assertTrue(f["evidence"]["temporal"]["precise"])

    def test_fabricated_branch_article_and_item(self):   # #4
        self.assertEqual(one("근로기준법 제76조의9")["status"], NOT_FOUND)
        self.assertEqual(one("개인정보 보호법 제2조제1호의9")["status"], NOT_FOUND)
        self.assertEqual(one("민법 제750조②에 따라")["status"], NOT_FOUND)

    def test_deleted_units(self):   # #1
        for t in ("민법 제1112조제4호", "형법 제78조제1호", "근로기준법 제116조제4항", "개인정보 보호법 제8조"):
            f = one(t)
            self.assertEqual(f["status"], MISMATCH, (t, f["detail"]))
            self.assertIn("삭제", f["detail"])

    def test_not_yet_in_force(self):   # #2
        for t in ("「개인정보 보호법」 제28조의12제1항", "근로기준법 제44조의4", "도로교통법 제137조제5항제1호",
                  "근로기준법 제60조제9항은 불리한 처우를 금지한다.", "시행 예정이 아닌 현행 근로기준법 제60조제9항"):
            self.assertEqual(one(t)["status"], MISMATCH, t)
        f = one("2027. 6. 10. 시행 예정인 개정 근로기준법 제60조제9항")
        self.assertEqual(f["status"], VERIFIED)
        self.assertTrue(any("시행예정" in w for w in f["warnings"]))

    def test_past_as_of(self):   # #2
        self.assertEqual(one("근로기준법 제76조의2(직장 내 괴롭힘의 금지)", as_of=date(2018, 1, 1))["status"], MISMATCH)
        self.assertEqual(one("「개인정보 보호법」 제28조의2(가명정보의 처리 등)", as_of=date(2019, 6, 1))["status"], MISMATCH)
        self.assertEqual(one("「개인정보 보호법」 제15조", as_of=date(2019, 6, 1))["status"], VERIFIED)

    def test_renumbered_paragraphs(self):   # #3
        f = one("근로기준법 제60조제8항")                                      # 시행 중 판본에는 ①~⑦뿐(⑧은 시행 전 판본에만)
        self.assertEqual(f["status"], MISMATCH)
        self.assertIn("시행 전", f["detail"])
        self.assertEqual(one("근로기준법 제60조제10항")["status"], NOT_FOUND)    # 어느 판본에도 없음
        f = one("근로기준법 제60조제5항은 “사용자는 제1항부터 제4항까지의 규정에 따른 휴가를 근로자가 청구한 시기에 주어야 하고”라고 규정한다.")
        self.assertEqual(f["status"], VERIFIED, f["detail"])                    # 시행 중 ⑤의 문언
        self.assertTrue(any("시행 전 개정" in w for w in f["warnings"]))
        f = one("근로기준법 제60조제5항은 “사용자는 근로자가 제1항ㆍ제2항 및 제4항에 따른 유급휴가를 대통령령으로 정하는 시간단위 및 "
                "일수의 범위에서 분할하여 청구한 때에는 이를 부여하여야 한다”고 규정한다.")
        self.assertEqual(f["status"], MISMATCH)                                 # 아직 시행 전인 새 ⑤의 문언
        f = one("근로기준법 제60조제5항은 “사용자는 근로자가 제1항ㆍ제2항 및 제4항에 따른 유급휴가를 대통령령으로 정하는 시간단위 및 "
                "일수의 범위에서 분할하여 청구한 때에는 이를 부여하여야 한다”고 규정한다.", as_of=date(2027, 7, 1))
        self.assertEqual(f["status"], VERIFIED)

    def test_whole_articles_and_multi_date_markers(self):   # #18
        for t in ("근로기준법 제60조", "도로교통법 제148조의2", "근로기준법 제61조제2항", "근로기준법 제102조의2"):
            self.assertEqual(one(t)["status"], VERIFIED, t)

    def test_historical_version(self):   # #14
        t = "구 근로기준법(2019. 1. 15. 법률 제16270호로 개정되기 전의 것) 제76조의2"
        self.assertEqual(one(t)["status"], MISMATCH)
        self.assertEqual(one("구 근로기준법(2019. 1. 15. 법률 제16270호로 개정되기 전의 것) 제76조")["status"], VERIFIED)
        self.assertEqual(one("구 근로기준법 제60조")["status"], UNVERIFIABLE)   # 판본 미특정

    def test_titles_exact(self):   # #15
        self.assertEqual(one("민법 제750조(불법행위의 책임)")["status"], MISMATCH)
        self.assertEqual(one("민법 제750조(내용)")["status"], MISMATCH)
        self.assertEqual(one("근로기준법 제23조(해고 등의 제한 및 경영상 해고의 요건)")["status"], MISMATCH)
        self.assertEqual(one("민법 제750조(불법행위의 내용)")["status"], VERIFIED)

    def test_quotes(self):   # #6 #7 #8
        bad = ["“고의나 과실이 없어도 타인에게 손해를 가한 자는 배상책임을 진다”(민법 제750조).",
               "민법 제750조에 관하여 다음과 같이 규정한다.\n“고의나 과실이 없어도 타인에게 손해를 가한 자는 배상책임을 진다.”",
               "민법 제750조는 ‘고의나 과실이 없어도 … 배상책임을 진다’고 규정한다.",
               "민법 제750조 참조. 이 조항은 “고의나 과실이 없어도 손해를 배상하여야 한다”고 규정한다.",
               "근로기준법 제76조의2는 “" + Q76_2 + "(이하 \"직장 내 괴롭힘\"이라 한다)를 하여서는 아니 되며, 위반한 사용자는 3년 이하의 징역에 처한다”고 규정한다.",
               "근로기준법 제76조의3제1항은 “사용자는 제1항에 따른 신고를 접수하거나 … 조사를 실시하여야 한다”고 규정한다.",
               "민법 제1112조제1호는 “피상속인의 배우자는 그 법정상속분의 2분의 1”이라고 정한다.",
               "민법 제750조는 “고의나 과실로 남에게 손해를 입힌 사람은 배상해야 한다”고 규정한다."]
        for t in bad:
            self.assertEqual(statuses(t), [MISMATCH], t)
        good = ["“" + Q750 + "”(민법 제750조).", "민법 제750조는 다음과 같이 규정한다:\n> " + Q750,
                "근로기준법 제76조의2는 “" + Q76_2_FULL + "”고 규정한다.",
                "민법 제750조 및 제751조는 “" + Q750 + "”고 규정한다."]
        for t in good:
            self.assertEqual(set(statuses(t)), {VERIFIED}, t)

    def test_orphan_quote_fails(self):
        self.assertEqual(verifier().verify_text("“사용자는 해고 30일 전에 예고하여야 한다”라고 규정하고 있다.")["verdict"], "FAIL")

    def test_alias_and_implicit(self):   # #12 #20
        self.assertEqual(statuses("「개인정보 보호법」(이하 \"법\"이라 한다) 제15조. 또한 법 제107조에 따라 처벌된다.\n\n"
                                  "「근로기준법」(이하 \"법\"이라 한다) 제23조."), [VERIFIED, NOT_FOUND, VERIFIED])
        self.assertEqual(statuses("「중대재해 처벌 등에 관한 법률」(이하 '중대재해처벌법'이라 한다) 제6조. 또한 중대재해처벌법 제4조는 의무를 규정한다."),
                         [VERIFIED, VERIFIED])
        self.assertEqual(statuses("민법 제750조와 달리 상법에서는 제24조가 명의대여자의 책임을 정한다."), [VERIFIED, VERIFIED])
        self.assertEqual(statuses("근로기준법 제23조를 본다. 또한 제999조가 있다."), [VERIFIED, UNRESOLVED])
        self.assertEqual(statuses("근로기준법 및 같은 법 시행령 제30조"), [VERIFIED])
        self.assertEqual(statuses("**근로기준법** 제23조"), [VERIFIED])
        self.assertEqual(statuses("회사는 취업규칙 개정 후 근로기준법 제23조를 위반하였다."), [VERIFIED])
        self.assertEqual(verifier().verify_text("취업규칙 제5조")["verdict"], "NO_CITATIONS")

    def test_units(self):   # #10
        self.assertEqual(statuses("개인정보 보호법 제15조제1항제1호, 같은 항 제9호"), [VERIFIED, NOT_FOUND])
        self.assertEqual(statuses("형법 제250조제1항 내지 제9항"), [VERIFIED, NOT_FOUND])
        self.assertEqual(statuses("민법 제750조가목"), [UNRESOLVED])
        self.assertEqual(statuses("개인정보 보호법 제2조제1호 각 목의 정보"), [VERIFIED])
        self.assertEqual(statuses("근로기준법 제60조 제1항"), [VERIFIED])

    def test_fabricated_law_names(self):
        self.assertEqual(one("특별 근로기준법 제5조")["status"], UNRESOLVED)
        self.assertEqual(one("직장 내 괴롭힘 방지법 제3조")["status"], UNRESOLVED)
        self.assertEqual(one("중처법 제4조")["status"], UNRESOLVED)


class TestStatutesNoHistory(unittest.TestCase):
    """이력 없는 미러(최신본만): 확실하지 않으면 UNVERIFIABLE — 가짜·삭제·미시행 인용을 통과시키지 않는다."""

    def st(self, t, **kw):
        return one(t, history=False, **kw)["status"]

    def test_never_passes_bad_citations(self):
        for t in ("근로기준법 제44조의4", "도로교통법 제137조제5항제1호", "근로기준법 제60조제9항", "근로기준법 제76조의9",
                  "민법 제1112조제4호", "형법 제78조제1호", "근로기준법 제116조제4항", "「개인정보 보호법」 제28조의12제1항",
                  "근로기준법 제60조제8항", "민법 제750조(내용)"):
            self.assertIn(self.st(t), (MISMATCH, NOT_FOUND, UNVERIFIABLE), t)

    def test_certain_cases_still_decided(self):
        self.assertEqual(self.st("민법 제750조(불법행위의 내용)"), VERIFIED)          # 최신본이 이미 시행 중
        self.assertEqual(self.st("민법 제1112조제4호"), MISMATCH)
        self.assertEqual(self.st("근로기준법 제60조"), VERIFIED)                      # 조 전체: 기준일 전 개정표시로 존재 확인
        self.assertEqual(self.st("근로기준법 제60조제9항"), UNVERIFIABLE)              # 시행 전 신설 표시 → 이력 없이 판정 불가
        self.assertEqual(self.st("근로기준법 제76조의2", as_of=date(2018, 1, 1)), MISMATCH)   # 장 제목의 <신설 2019.1.15>

    def test_status_reports_history_mode(self):
        st = StatuteMirror(str(FIX / "legalize-kr")).status()
        self.assertEqual(st["history_mode"], "none")
        self.assertIn("setup", st["history_note"])


class TestCases(unittest.TestCase):
    def test_case_ok_and_mismatches(self):   # #9
        self.assertEqual(one("대법원 2025. 8. 14. 선고 2023다216777 판결")["status"], VERIFIED)
        for t in ("대법원 2021. 9. 17. 선고 2021다219529 판결", "대법원 2021년 9월 17일 선고 2021다219529 판결",
                  "대법원 2021-09-17 선고 2021다219529 판결", "서울고등법원 2021년 9월 16일 선고 2021다219529 판결"):
            self.assertEqual(one(t)["status"], MISMATCH, t)

    def test_holding_quotes(self):   # #8
        self.assertEqual(one(f"대법원 2021. 9. 16. 선고 2021다219529 판결은 “{Q_HARASS}”라고 판시하였다.")["status"], VERIFIED)
        f = one("대법원 2021. 9. 16. 선고 2021다219529 판결은 “성희롱이 성립하기 위해서 행위자에게 반드시 성적 동기나 의도가 있어야 하는 … 것이다”라고 판시하였다.")
        self.assertEqual(f["status"], MISMATCH)                                    # 생략으로 뜻을 뒤집음
        f = one("대법원 2021다219529 판결은 “직장 내 괴롭힘 판단 시 업무상 적정범위를 넘었는지에 대한 객관적 판단 기준”을 제시하였다.")
        self.assertEqual(f["status"], MISMATCH)                                    # v1 예시의 왜곡된 판시
        f = one("대법원 2021. 9. 16. 선고 2021다219529 판결은 “민법 제756조에 따른 사용자책임은 사용자가 무과실을 입증하면 언제나 면책된다”고 판시하였다.")
        self.assertEqual(f["status"], MISMATCH)                                    # 인용문 안 조문 표기에 끊기지 않음

    def test_body_only_text_needs_explicit_context(self):   # #8
        f = one("대법원 2021. 9. 16. 선고 2021다219529 판결은 “상고를 모두 기각한다”라고 판시하였다.")
        self.assertEqual(f["status"], MISMATCH)                                    # 판시사항·판결요지에 없음
        self.assertIn("판결 이유", f["detail"])
        f = one("대법원 2021. 9. 16. 선고 2021다219529 판결은 판결문에서 “상고를 모두 기각한다”라고 밝혔다.")
        self.assertEqual(f["status"], VERIFIED)

    def test_enbanc(self):
        self.assertEqual(one(f"대법원 2024. 12. 19. 선고 2020다247190 전원합의체 판결은 “{Q_ENBANC}”고 판시하였다.")["status"], VERIFIED)
        self.assertEqual(one("대법원 2021. 9. 16. 선고 2021다219529 전원합의체 판결")["status"], MISMATCH)

    def test_fabricated_and_recent(self):   # #17
        self.assertEqual(one("대법원 1999다999999 판결")["status"], NOT_FOUND)
        self.assertEqual(one("대법원 2099. 1. 1. 선고 2099다1 판결")["status"], MISMATCH)
        latest = PrecedentMirror(PREC).latest_decision()
        self.assertEqual(one(f"대법원 {latest.year}. {latest.month}. {latest.day}. 선고 {latest.year}다777777 판결")["status"], NOT_FOUND)
        after = latest.replace(day=min(latest.day + 1, 28))
        self.assertEqual(one(f"대법원 {after.year}. {after.month}. {after.day}. 선고 {after.year}다777777 판결")["status"], UNVERIFIABLE)

    def test_lower_courts(self):   # #19, citation_rules '법원명 필수'
        self.assertEqual(one("2008브4 결정")["status"], UNRESOLVED)
        self.assertEqual(one("대구지방법원 2008. 5. 19.자 2008브4 결정")["status"], VERIFIED)
        self.assertEqual(one("대구지법 2008. 5. 19.자 2008브4 결정")["status"], VERIFIED)
        self.assertEqual(one("서울고등법원 2008브4 결정")["status"], MISMATCH)

    def test_merged_case_number_confirmed_by_body(self):   # #16
        self.assertEqual(statuses("대법원 2023. 7. 13. 선고 2017므11856, 2017므11863 판결"), [VERIFIED, VERIFIED])

    def test_phantom_serial_suffix(self):   # #16
        root = Path(tempfile.mkdtemp()) / "precedent-kr"
        shutil.copytree(FIX / "precedent-kr", root)
        src = root / "가사/하급심/대구지방법원_2008-05-19_2008브4.md"
        src.rename(src.with_name("대구지방법원_2008-05-19_2008브4_623093.md"))
        v = Verifier(StatuteMirror(str(history_repo())), PrecedentMirror(str(root)), None, as_of=AS_OF)
        f = v.verify_text("대구지방법원 2008. 5. 19.자 2008브623093 결정")["findings"][0]
        self.assertEqual(f["status"], NOT_FOUND)                                   # 판례일련번호 접미는 사건번호가 아니다
        pm = PrecedentMirror(str(root))
        pm.read = lambda e: None                                                    # 오프라인: 본문을 못 읽음
        v2 = Verifier(StatuteMirror(str(history_repo())), pm, None, as_of=AS_OF)
        self.assertEqual(v2.verify_text("대구지방법원 2008. 5. 19.자 2008브623093 결정")["findings"][0]["status"], UNVERIFIABLE)

    def test_offline_body_unavailable_is_not_pass(self):   # #5
        pm = PrecedentMirror(PREC)
        pm.read = lambda e: None
        v = Verifier(StatuteMirror(str(history_repo())), pm, None, as_of=AS_OF)
        for t in ("대법원 2019. 1. 10. 선고 2021다219529 판결은 “근로자는 퇴직 후 10년 이내에 언제든지 해고의 무효를 주장할 수 있다”고 판시하였다.",
                  "대법원 2021. 9. 16. 선고 2021다219529 판결은 “근로자는 퇴직 후 10년 이내에 언제든지 해고의 무효를 주장할 수 있다”고 판시하였다.",
                  "대법원 2021. 9. 16. 선고 2021다219529 전원합의체 판결"):
            self.assertIn(v.verify_text(t)["verdict"], ("FAIL", "INCOMPLETE"), t)
        self.assertEqual(v.verify_text("대법원 2021. 9. 16. 선고 2021다219529 판결은 “근로자는 퇴직 후 10년 이내에 언제든지 해고의 무효를 "
                                       "주장할 수 있다”고 판시하였다.")["verdict"], "INCOMPLETE")

    def test_constitutional_needs_api(self):
        self.assertEqual(one("헌법재판소 2016. 10. 27. 선고 2013헌마576 결정")["status"], UNVERIFIABLE)

    def test_ministry_interpretation(self):
        f = one("고용노동부 행정해석(근로기준정책과-9999)은 “5인 미만 사업장에도 해고예고 규정이 적용된다”라고 회시하였다.")
        self.assertEqual(f["status"], UNVERIFIABLE)


class TestDocument(unittest.TestCase):
    def test_document_verdicts(self):
        v = verifier()
        self.assertEqual(v.verify_text("민법 제750조")["verdict"], "PASS")
        self.assertEqual(v.verify_text("사실관계만 있고 인용이 없는 문서")["verdict"], "NO_CITATIONS")  # v1: PASSED 100%
        self.assertEqual(v.verify_text("민법 제750조, 헌재 2013헌마576")["verdict"], "INCOMPLETE")
        self.assertEqual(v.verify_text("민법 제750조, 근로기준법 제76조의9")["verdict"], "FAIL")
        md = render_markdown(v.verify_text("민법 제750조, 근로기준법 제76조의9"))
        self.assertIn("FAIL", md)
        self.assertIn("판본 이력 전체", md)

    def test_evidence_closed_world_warning(self):
        rep = verifier().verify_text("민법 제750조 및 제766조", evidence_text="민법 제750조")
        self.assertEqual(rep["outside_evidence"], ["민법 제766조"])

    def test_verify_entry_pairs_quote_explicitly(self):   # #6 #22
        v = verifier()
        f, probs = v.verify_entry("민법 제750조.", "고의나 과실이 없어도 타인에게 손해를 가한 자는 배상책임을 진다")
        self.assertEqual((f.status, probs), (MISMATCH, []))
        f, probs = v.verify_entry("대법원 2021. 9. 16. 선고 2021다219529 판결", "사용자는 직장 내 성희롱에 대하여\n어떠한 경우에도 책임을 지지 아니한다")
        self.assertEqual(f.status, MISMATCH)
        f, probs = v.verify_entry("민법 제750조 및 제751조", None)
        self.assertIsNone(f)
        self.assertTrue(probs)
        self.assertEqual(v.verify_entry(123)[1], ["citation 이 문자열이 아님/비어 있음"])


class FakeApi:
    """DRF 대역. mode='down' 이면 접속 불가, 'lying' 이면 무엇을 물어도 다른 사건을 돌려준다(v1 첫 결과 폴백 재현용),
    'bodyfail' 이면 목록은 되고 본문 호출만 실패, 'empty' 면 본문이 비어 있음."""

    def __init__(self, mode="ok"):
        self.mode = mode

    def _guard(self):
        if self.mode == "down":
            raise ApiUnavailable("법제처 API 접속 불가: 테스트")

    def _body(self, value):
        if self.mode == "bodyfail":
            raise ApiUnavailable("본문 타임아웃: 테스트")
        return {} if self.mode == "empty" else value

    def constitutional_by_number(self, no):
        self._guard()
        return [{"사건번호": "2013헌마576", "종국일자": "2016.10.27", "사건명": "2012년도 대학교육역량강화사업 기본계획 취소 등",
                 "헌재결정례일련번호": "52626"}] if no == "2013헌마576" else []

    def constitutional_body(self, i):
        return self._body({"결정요지": "가. 2012년도와 2013년도 대학교육역량강화사업 기본계획은 대학교육역량강화 지원사업을 추진하기 위한 국가의 기본방침을 밝히는 것"})

    def precedent_by_number(self, no):
        self._guard()
        if self.mode == "lying":   # 호출자가 사건번호 일치를 확인하지 않으면 통과시켜 버리는 응답
            return [{"사건번호": "2020다1", "법원명": "대법원", "선고일자": "2020.01.01", "판례일련번호": "1"}]
        return []

    def interpretation_by_number(self, no, target="expc"):
        self._guard()
        return [{"안건번호": "22-0733", "안건명": "감사원 - 교육공무원임용령 …", "회신일자": "2022.12.30",
                 "법령해석례일련번호": "335283"}] if no == "22-0733" else []

    def interpretation_body(self, i, target="expc"):
        return self._body({"회답": "「교육공무원법」 제12조제1항제2호에 따라 교사를 특별채용하기 위하여"})

    def find_law(self, name, as_of=None):
        self._guard()
        return {"status": "not_found", "candidates": []}


class FakeDrfLaw:
    """DRF 단독(로컬 미러 없음) 경로 대역: 조문 구조는 law_api.drf_article 이 만드는 객체와 같다."""

    def find_law(self, name, as_of=None):
        return {"status": "ok", "name": "민법", "mst": "1", "efYd": "20260317", "state": "현행"}

    def law_articles(self, mst, ef_yd=None):
        import xml.etree.ElementTree as ET
        from law_api import _flat, drf_article
        xml = ("<조문단위><조문번호>3</조문번호><조문여부>조문</조문여부><조문제목>가상</조문제목><조문내용>제3조(가상)</조문내용>"
               "<항><항번호>①</항번호><항내용>① 첫째 항</항내용><호><호번호>1.</호번호><호내용>1. 가</호내용></호></항>"
               "<항><항번호>②</항번호><항내용>② 둘째 항</항내용><호><호번호>1.</호번호><호내용>1. 하나</호내용></호>"
               "<호><호번호>2.</호번호><호내용>2. 둘</호내용><목><목번호>가.</목번호><목내용>가. 갑</목내용></목></호>"
               "<호><호번호>3.</호번호><호내용>3. 삭제 &lt;2024. 9. 20.&gt;</호내용></호></항></조문단위>")
        u = ET.fromstring(xml)
        return {"info": {}, "articles": [{"jo": 3, "sub": None, "title": "가상", "시행일자": "20260317", "deleted": False,
                                          "article": drf_article(u, _flat(u), 3, None)}]}


class TestVerifyWithApi(unittest.TestCase):
    def test_constitutional_via_api(self):
        v = offline_verifier(FakeApi())
        self.assertEqual(v.verify_text("헌법재판소 2016. 10. 27. 선고 2013헌마576 결정")["findings"][0]["status"], VERIFIED)
        self.assertEqual(v.verify_text("헌법재판소 2016. 10. 28. 선고 2013헌마576 결정")["findings"][0]["status"], MISMATCH)

    def test_authority_via_api(self):
        v = offline_verifier(FakeApi())
        rep = v.verify_text("법제처 22-0733 해석은 “「교육공무원법」 제12조제1항제2호에 따라 교사를 특별채용하기 위하여”라고 회신")
        self.assertEqual([(f["citation"], f["status"]) for f in rep["findings"]], [("법제처 해석 안건 22-0733", VERIFIED)])
        self.assertEqual(v.verify_text("법제처 99-9999 해석")["findings"][0]["status"], NOT_FOUND)

    def test_body_failures_are_unverifiable_not_crash(self):   # #24
        for mode in ("bodyfail", "empty"):
            v = Verifier(None, None, FakeApi(mode), as_of=AS_OF)
            for t in ("헌법재판소 2016. 10. 27. 선고 2013헌마576 결정은 “대학의 자율성은 헌법상 기본권이 아니다”라고 판시하였다.",
                      "법제처 22-0733 해석은 “교사는 언제든지 특별채용할 수 있다”라고 회신하였다."):
                self.assertEqual(v.verify_text(t)["verdict"], "INCOMPLETE", (mode, t))

    def test_network_failure_is_not_hallucination(self):
        # v1: 접속 실패 시 실존 조문을 '❌ 불일치'로 표시
        v = Verifier(None, None, FakeApi("down"), as_of=AS_OF)
        rep = v.verify_text("근로기준법 제60조, 대법원 2021다219529, 헌재 2013헌마576")
        self.assertEqual({f["status"] for f in rep["findings"]}, {UNVERIFIABLE})
        self.assertEqual(rep["verdict"], "INCOMPLETE")

    def test_no_first_result_fallback(self):
        v = Verifier(None, None, FakeApi("lying"), as_of=AS_OF)
        self.assertNotEqual(v.verify_text("대법원 2099다1 판결")["findings"][0]["status"], VERIFIED)
        self.assertNotEqual(v.verify_text("대법원 2021다219529 판결")["findings"][0]["status"], VERIFIED)

    def test_drf_only_statute_units(self):   # #13
        v = Verifier(None, None, FakeDrfLaw(), as_of=AS_OF)
        cases = {"민법 제3조": VERIFIED, "민법 제3조제5항": NOT_FOUND, "민법 제3조제2항제3호": MISMATCH,
                 "민법 제3조제1항제2호": NOT_FOUND, "민법 제3조제2항제2호가목": VERIFIED, "민법 제3조제2항제2호나목": NOT_FOUND,
                 "민법 제3조(다른 제목)": MISMATCH}
        for t, want in cases.items():
            self.assertEqual(v.verify_text(t)["findings"][0]["status"], want, t)


if __name__ == "__main__":
    unittest.main()
