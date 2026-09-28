import unittest
from datetime import date
from pathlib import Path

from _util import FIX, has_git, history_repo

from kr_common import parse_frontmatter_text, read_frontmatter, split_frontmatter
from legal_engine import StatuteMirror, parse_article_no
from temporal import (after_period, block_span, coverage, is_deleted_unit, parse_buchik, parse_subject_ex, parse_unit_list,
                      parse_when, unit_effective)

AS_OF = date(2026, 9, 28)
HIST = FIX / "legalize-kr-history" / "versions"


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.m = StatuteMirror(str(FIX / "legalize-kr"))

    def test_picks_current_labor_act_not_1997_repeal(self):
        r = self.m.resolve("근로기준법")
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.doc.rel.endswith("법률(법률).md"), r.doc.rel)

    def test_choice_does_not_depend_on_directory_order(self):
        # v1: PyYAML date 비교 TypeError 를 삼켜 디렉터리 순서로 골랐다 → 역순이면 1997년 폐지 법률
        self.m.dirs["근로기준법"] = list(reversed(self.m.dirs["근로기준법"]))
        self.assertTrue(self.m.resolve("근로기준법").doc.rel.endswith("법률(법률).md"))

    def test_exact_match_only(self):
        for q in ("기준법", "보호법", "근로법"):
            self.assertEqual(self.m.resolve(q).status, "not_found", q)

    def test_spacing_and_subordinate(self):
        self.assertEqual(self.m.resolve("개인정보 보호법").doc.law_dir, "개인정보보호법")
        r = self.m.resolve("근로기준법 시행령")
        self.assertEqual(r.doc.kind, "시행령")
        self.assertEqual(r.doc.title, "근로기준법 시행령")

    def test_long_frontmatter_parsed_fully(self):
        # 첨부파일 목록 때문에 프론트매터가 2KB 를 넘는다(v1 은 2048자만 읽어 메타데이터를 잃었다)
        p = FIX / "legalize-kr/kr/근로기준법/시행령.md"
        meta = read_frontmatter(p)
        self.assertEqual(meta["공포일자"], "2025-04-08")
        self.assertEqual(meta["시행일자"], "2025-10-23")
        self.assertIsInstance(meta["첨부파일"], list)


class TestArticles(unittest.TestCase):
    def setUp(self):
        self.m = StatuteMirror(str(FIX / "legalize-kr"))
        self.pipa = self.m.resolve("개인정보 보호법").doc
        self.lsa = self.m.resolve("근로기준법").doc

    def test_deleted_article(self):
        a = self.m.find_article(self.pipa, 8)
        self.assertTrue(a.deleted)
        self.assertEqual(a.deleted_on, date(2020, 2, 4))

    def test_last_article_does_not_swallow_buchik(self):
        a = self.m.find_article(self.pipa, 76)
        self.assertNotIn("부칙", a.text)

    def test_article_excludes_next_chapter_heading(self):
        a = self.m.find_article(self.pipa, 2)
        self.assertNotIn("제2장", a.text)

    def test_paragraphs_items_and_deleted_units(self):
        a = self.m.find_article(self.pipa, 15)
        self.assertEqual(sorted(a.paragraphs), [1, 2, 3])
        self.assertEqual(sorted(a.paragraphs[1].items, key=int), [str(i) for i in range(1, 8)])
        self.assertIsNone(a.unit_text(1, "9"))
        d = self.m.find_article(self.pipa, 2)
        self.assertIn("1의2", d.paragraphs[1].items if d.paragraphs else d.items)
        self.assertIsNone(d.unit_text(None, None, "가"))          # 호 없는 목은 없다
        lsa116 = self.m.find_article(self.lsa, 116)
        self.assertTrue(is_deleted_unit(lsa116.unit_text(4)))      # '**④** 삭제 <2009.5.21>'
        self.assertFalse(is_deleted_unit(lsa116.unit_text(2)))

    def test_headings_carry_insertion_markers(self):
        a = self.m.find_article(self.lsa, 76, 2)
        self.assertTrue(any("신설 2019.1.15" in h for h in a.headings), a.headings)

    def test_branch_article(self):
        a = self.m.find_article(self.lsa, 76, 2)
        self.assertEqual(a.title, "직장 내 괴롭힘의 금지")
        self.assertIsNone(self.m.find_article(self.lsa, 76, 9))

    def test_parse_article_no(self):
        self.assertEqual(parse_article_no("76조의2"), (76, 2))
        self.assertEqual(parse_article_no("제76조의2"), (76, 2))
        self.assertEqual(parse_article_no("76의2"), (76, 2))
        self.assertEqual(parse_article_no("76-2"), (76, 2))   # v1 docstring 과 달리 (76, None) 이던 것
        self.assertEqual(parse_article_no("60"), (60, None))
        with self.assertRaises(ValueError):
            parse_article_no("육십")


class TestBuchik(unittest.TestCase):
    """부칙 시행일 계산(민법 제157조·제160조) — 실제 판본의 프론트매터 시행일자와 대조해 확인한 값."""

    def test_periods(self):
        self.assertEqual(after_period(date(2026, 6, 9), 1, "년"), date(2027, 6, 10))
        self.assertEqual(after_period(date(2026, 4, 7), 6, "개월"), date(2026, 10, 8))
        self.assertEqual(after_period(date(2026, 4, 7), 8, "개월"), date(2026, 12, 8))
        self.assertEqual(after_period(date(2026, 1, 31), 1, "개월"), date(2026, 3, 1))   # 말일 보정 후 익일
        self.assertEqual(after_period(date(2026, 6, 2), 6, "개월"), date(2026, 12, 3))

    def test_when_expressions(self):
        p = date(2026, 4, 7)
        self.assertEqual(parse_when("2027년 1월 1일", p), date(2027, 1, 1))
        self.assertEqual(parse_when("공포한 날", p), p)
        self.assertEqual(parse_when("공포 후 6개월이 경과한 날", p), date(2026, 10, 8))
        self.assertIsNone(parse_when("공포 후 1년 이내의 범위에서 대통령령으로 정하는 날", p))

    def test_unit_lists(self):
        units = parse_unit_list("제13조, 제11장의 제목, 제101조, 제102조의2, 제103조부터 제105조까지, 제110조제1호, "
                                "제114조제1호 및 제116조제2항제1호ㆍ제4호")
        keys = [(u.jo, u.sub, u.hang, u.ho, u.jo_end) for u in units]
        self.assertIn((102, 2, None, None, None), keys)
        self.assertIn((103, None, None, None, (105, 0)), keys)
        self.assertIn((116, None, 2, "1", None), keys)
        self.assertIn((116, None, 2, "4", None), keys)
        self.assertNotIn(11, [u.jo for u in units])                  # '제11장의 제목'은 조항이 아니다('제목'≠목)
        self.assertEqual(coverage(units, 104, None, None, None), "full")
        self.assertEqual(coverage(units, 116, None, 2, None), "partial")
        self.assertEqual(coverage(units, 116, None, 2, "2"), "none")

    def test_real_buchik_2026_4_7(self):
        text = (HIST / "근로기준법/법률(법률)/20260407-15.md").read_text(encoding="utf-8")
        b = [x for x in parse_buchik(split_frontmatter(text)[1]) if x.promulgated == date(2026, 4, 7)][0]
        self.assertEqual(b.main_effective, date(2026, 10, 8))
        self.assertEqual(sorted(e.effective for e in b.exceptions), [date(2026, 12, 8), date(2027, 1, 1)])
        self.assertEqual(coverage(b.exceptions[1].units, 44, 4, None, None), "full")

    # ---- 3차 적대적 검토(§G): 부칙 시행일 조문 변형 — 전체 미러 93,749개 블록 실측으로 모은 문형 ----
    @staticmethod
    def buchik(*blocks, title="테스트법"):
        return parse_buchik("## 부칙\n\n" + "\n\n".join(blocks), title)

    def test_when_expressions_round3(self):
        self.assertEqual(parse_when("공포 후 1년 6개월이 경과한 날", date(2026, 1, 15)), date(2027, 7, 16))
        self.assertEqual(parse_when("공포일", date(2026, 1, 15)), date(2026, 1, 15))
        self.assertEqual(parse_when("공포일이 속하는 달의 다음 달 1일", date(2026, 12, 15)), date(2027, 1, 1))
        self.assertIsNone(parse_when("해당 호에서 정하는 날", date(2026, 1, 15)))

    def test_real_buchik_2024_10_22_per_item_wording(self):
        # '다만, 다음 각 호의 사항은 그 구분에 따른 날부터' — 종전 파서는 모든 호를 '시행일 미상'으로 읽어 제60조⑥이 판정 불가였다
        text = (HIST / "근로기준법/법률(법률)/20260609-16.md").read_text(encoding="utf-8")
        b = [x for x in parse_buchik(split_frontmatter(text)[1], "근로기준법") if x.promulgated == date(2024, 10, 22)][0]
        self.assertEqual(b.main_effective, date(2025, 10, 23))
        self.assertEqual(unit_effective(b, 60, None, 6, None)[:2], (date(2024, 10, 22), date(2024, 10, 22)))
        self.assertEqual(unit_effective(b, 74, None, 7, None)[:2], (date(2025, 2, 23), date(2025, 2, 23)))
        self.assertEqual(unit_effective(b, 23, None, None, None)[:2], (date(2025, 10, 23), date(2025, 10, 23)))

    def test_items_with_mok_and_omitted_items(self):
        b = self.buchik("부칙 <제100호,2026.1.15>\n\n제1조(시행일) 이 법은 공포한 날부터 시행한다. 다만, 다음 각 호의 개정규정은 "
                        "해당 호에서 정하는 날부터 시행한다.\n\n1. 다음 각 목의 개정규정은 2027년 1월 1일부터 시행한다.\n"
                        "  가. 제5조제2항\n  나. 제7조\n2. 제9조의 개정규정: 공포 후 6개월이 경과한 날\n\n"
                        "제2조(경과조치) 제3조의 개정규정은 2030년 1월 1일부터 시행한다.")[0]
        self.assertEqual(unit_effective(b, 5, None, 2, None)[:2], (date(2027, 1, 1), date(2027, 1, 1)))
        self.assertEqual(unit_effective(b, 7, None, None, None)[:2], (date(2027, 1, 1), date(2027, 1, 1)))
        self.assertEqual(unit_effective(b, 9, None, None, None)[:2], (date(2026, 7, 16), date(2026, 7, 16)))
        self.assertEqual(unit_effective(b, 3, None, None, None)[:2], (date(2026, 1, 15), date(2026, 1, 15)))   # 제2조는 시행일 조문이 아님
        b = self.buchik("부칙 <제101호,2026.1.15>\n\n제1조(시행일) 이 법은 공포한 날부터 시행한다. 다만, 다음 각 호의 개정규정은 "
                        "각 호의 구분에 따른 날부터 시행한다.\n\n1. 생략\n2. 제9조의 개정규정: 2027년 1월 1일")[0]
        self.assertIsNone(unit_effective(b, 3, None, None, None)[1])        # 제 법 부칙의 '생략' 호 — 어느 조항인지 모름

    def test_siheng_hadoe_and_chained_same_article(self):
        b = self.buchik("부칙 <제102호,2004.3.17>\n\n①(시행일) 이 영은 공포한 날부터 시행하되, 제12조의 개정규정은 2004년 1월 1일부터 "
                        "적용한다. 다만, 제42조의 개정규정은 2004년 4월 1일부터 시행한다.\n\n②(경과조치) 이 영 시행 당시 …")[0]
        self.assertEqual(b.main_effective, date(2004, 3, 17))
        self.assertEqual(unit_effective(b, 12, None, None, None)[:2], (date(2004, 3, 17), date(2004, 3, 17)))  # '적용'은 시행일이 아님
        self.assertEqual(unit_effective(b, 42, None, None, None)[:2], (date(2004, 4, 1), date(2004, 4, 1)))
        b = self.buchik("부칙 <제103호,2020.1.1>\n\n제1조(시행일) 이 법은 2020년 3월 1일부터 시행한다. 다만, 제5조제1항은 2020년 6월 1일부터, "
                        "같은 조 제2항은 2021년 1월 1일부터 각각 시행한다.")[0]
        self.assertEqual(unit_effective(b, 5, None, 2, None)[:2], (date(2021, 1, 1), date(2021, 1, 1)))
        self.assertEqual(unit_effective(b, 5, None, 1, None)[:2], (date(2020, 6, 1), date(2020, 6, 1)))

    def test_other_law_buchik(self):
        # 타법개정 부칙의 맨 조항 번호는 그 다른 법률의 것 — 이 법 조항에 걸지 않는다. 이 법을 고치는 부칙 조항을 가리킨 단서만 쓴다
        b = self.buchik("부칙(다른법) <제200호,2021.5.1>\n\n제1조(시행일) 이 법은 공포한 날부터 시행한다. 다만, ㆍㆍㆍ<생략>ㆍㆍㆍ 부칙 제4조는 "
                        "2022년 1월 1일부터 시행한다.\n\n제2조 및 제3조 생략\n\n제4조(다른 법률의 개정) ①부터 ③까지 생략\n"
                        "④ 테스트법 일부를 다음과 같이 개정한다.\n제3조 중 \"가\"를 \"나\"로 한다.")[0]
        self.assertEqual(b.amending, (4, 4))
        self.assertEqual(unit_effective(b, 3, None, None, None)[:2], (date(2022, 1, 1), date(2022, 1, 1)))
        b = self.buchik("부칙(다른법) <제300호,2021.5.1>\n\n제1조(시행일) 이 법은 공포 후 1년이 경과한 날부터 시행한다. 다만, 제3조의 "
                        "개정규정은 공포한 날부터 시행한다.\n\n제2조(다른 법률의 개정) 테스트법 일부를 다음과 같이 개정한다.")[0]
        self.assertEqual(unit_effective(b, 3, None, None, None)[:2], (date(2022, 5, 2), date(2022, 5, 2)))
        self.assertEqual(unit_effective(b, 3, None, None, None, date(2022, 6, 1))[:2], (date(2022, 5, 2), date(2022, 6, 1)))
        self.assertEqual(block_span(b), (date(2022, 5, 2), date(2022, 5, 2)))

    def test_subject_variants(self):
        self.assertEqual(parse_subject_ex("별표 2 중 제4호나목 및 다목의 규정"), ([], False, True, []))      # 별표만 — 조문과 무관
        units, unc, irr, bu = parse_subject_ex("제5조 및 부칙 제2조")
        self.assertEqual(([u.jo for u in units], [u.jo for u in bu]), ([5], [2]))
        units, unc, irr, bu = parse_subject_ex("법률 제8830호 국세기본법 일부개정법률 제3조의 개정규정")
        self.assertEqual((units, unc, irr), ([], True, False))                                         # 다른 개정법률의 조항
        self.assertEqual(parse_subject_ex("제3조의 개정규정", "테스트법", foreign=True)[0], [])


@unittest.skipUnless(has_git(), "git 필요")
class TestUnitStatusPrecise(unittest.TestCase):
    """실제 판본 발췌를 공포일 순서로 커밋한 git 저장소에서 조·항·호 단위 기준일 판정."""

    @classmethod
    def setUpClass(cls):
        cls.m = StatuteMirror(str(history_repo()))
        cls.lsa = cls.m.resolve("근로기준법").doc

    def st(self, jo, sub=None, hang=None, ho=None, as_of=AS_OF, law=None):
        doc = self.lsa if law is None else self.m.resolve(law).doc
        return self.m.unit_status(doc, jo, sub, hang, ho, None, as_of)

    def test_history_available(self):
        st = self.m.status()
        self.assertEqual(st["history_mode"], "full")
        self.assertGreaterEqual(len(self.m.history("근로기준법", 30)["commits"]), 10)

    def test_new_paragraph_not_in_force_yet(self):
        s = self.st(60, hang=9)                           # <신설 2026.6.9>, 시행 2027-06-10
        self.assertEqual((s.state, s.head_exists, s.precise), ("absent", True, True))
        self.assertEqual(self.st(60, hang=9, as_of=date(2027, 7, 1)).state, "in_force")

    def test_renumbered_paragraph_keeps_in_force_text(self):
        s = self.st(60, hang=6)                           # 최신 ⑥ = 옛 ⑤ 개정본 — 기준일의 ⑥은 옛 문언
        self.assertEqual(s.state, "in_force")
        self.assertIn("출근한 것으로 본다", s.text)
        self.assertTrue(s.pending_change)
        self.assertEqual(self.st(60, hang=8).state, "absent")

    def test_buchik_exception_article(self):
        s = self.st(44, 4)                                # 2026.4.7. 부칙 단서: 2027-01-01 시행
        self.assertEqual(s.state, "absent")
        self.assertTrue(any("2027-01-01" in n for n in s.notes), s.notes)
        self.assertEqual(self.st(44, 4, as_of=date(2027, 1, 1)).state, "in_force")

    def test_delayed_deletion_still_in_force(self):
        self.assertEqual(self.st(102, 2).state, "in_force")              # 삭제는 2026-12-08 시행
        self.assertEqual(self.st(102, 2, as_of=date(2026, 12, 8)).state, "deleted")

    def test_deleted_units(self):
        self.assertEqual(self.st(116, hang=4).state, "deleted")
        self.assertEqual(self.st(1112, ho="4", law="민법").state, "deleted")
        self.assertEqual(self.st(78, ho="1", law="형법").state, "deleted")

    def test_past_dates(self):
        self.assertEqual(self.st(76, 2, as_of=date(2018, 1, 1)).state, "absent")
        self.assertEqual(self.st(76, 2, as_of=date(2019, 7, 16)).state, "in_force")   # 2019.1.15. 공포 + 6개월
        self.assertEqual(self.st(28, 2, law="개인정보 보호법", as_of=date(2019, 6, 1)).state, "absent")

    def test_git_grep_paths_are_readable(self):
        hits = self.m.search("직장 내 괴롭힘", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["law_dir"], "근로기준법")   # v1: '\\352\\267…' 8진 이스케이프


class TestUnitStatusNoHistory(unittest.TestCase):
    """이력 없는 미러: 개정표시·부칙 단서로 판정하되 확실하지 않으면 unknown."""

    @classmethod
    def setUpClass(cls):
        cls.m = StatuteMirror(str(FIX / "legalize-kr"))

    def st(self, law, jo, sub=None, hang=None, ho=None, as_of=AS_OF):
        return self.m.unit_status(self.m.resolve(law).doc, jo, sub, hang, ho, None, as_of)

    def test_pending_units_unknown(self):
        self.assertEqual(self.st("근로기준법", 60, hang=9).state, "unknown")
        self.assertEqual(self.st("근로기준법", 44, 4).state, "unknown")          # 표시 없는 신설(부칙 단서만)
        s = self.st("근로기준법", 60)
        self.assertEqual((s.state, s.exists), ("unknown", True))                 # 조 전체: 존재는 확실

    def test_certain_results(self):
        self.assertEqual(self.st("민법", 750).state, "in_force")
        self.assertEqual(self.st("민법", 1112, ho="4").state, "deleted")
        self.assertEqual(self.st("근로기준법", 76, 2, as_of=date(2018, 1, 1)).state, "absent")   # 장 제목 <신설 2019.1.15>
        self.assertEqual(self.st("근로기준법", 60, as_of=date(2005, 1, 1)).state, "unknown")      # 2007 전부개정 이전

    def test_deleted_and_renumbered_units_with_pending_head(self):   # 3차 검토
        self.assertEqual(self.st("근로기준법", 116, hang=4).state, "deleted")        # '④ 삭제 <2009.5.21>' — 최신본이 시행 전이어도 확정
        s = self.st("근로기준법", 60, hang=8)                                          # <개정 2020.3.31, 2026.6.9> — 기준일엔 ⑦
        self.assertEqual(s.state, "unknown")
        self.assertIsNone(s.exists)


class TestDelegatedAndSearch(unittest.TestCase):
    def setUp(self):
        self.m = StatuteMirror(str(FIX / "legalize-kr"))

    def test_delegated_links_right_article(self):
        d = self.m.delegated("개인정보 보호법", 15)
        self.assertIn("대통령령", d["delegation_phrases"])
        self.assertEqual([x["article"] for x in d["linked"]], ["제14조의2"])  # v1: 시행령 제1조(목적)를 반환

    def test_delegated_branch_article(self):
        d = self.m.delegated("근로기준법", 60)
        self.assertIn("제33조", [x["article"] for x in d["linked"]])

    def test_search_attributes_article(self):
        hits = self.m.search("직장 내 괴롭힘", limit=10)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["title"], "근로기준법")
        self.assertIn(hits[0]["article"], ("제76조의2", "제76조의3"))


class TestFrontmatter(unittest.TestCase):
    def test_dates_stay_strings(self):
        fm, body = split_frontmatter("---\n제목: 민법\n공포일자: 2024-01-02\n소관부처:\n- 법무부\n---\n# 민법\n")
        meta = parse_frontmatter_text(fm)
        self.assertEqual(meta["공포일자"], "2024-01-02")
        self.assertEqual(meta["소관부처"], ["법무부"])
        self.assertTrue(body.startswith("# 민법"))


if __name__ == "__main__":
    unittest.main()
