import unittest
from datetime import date
from pathlib import Path

from _util import FIX, copy_fixture, git, has_git

from kr_common import parse_frontmatter_text, read_frontmatter, split_frontmatter
from legal_engine import StatuteMirror, parse_article_no

AS_OF = date(2026, 9, 28)


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

    def test_paragraphs_items(self):
        a = self.m.find_article(self.pipa, 15)
        self.assertEqual(sorted(a.paragraphs), [1, 2, 3])
        self.assertEqual(sorted(a.paragraphs[1].items, key=int), [str(i) for i in range(1, 8)])
        self.assertIsNone(a.unit_text(1, "9"))
        d = self.m.find_article(self.pipa, 2)
        self.assertIn("1의2", d.paragraphs[1].items if d.paragraphs else d.items)

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


class TestTemporal(unittest.TestCase):
    def setUp(self):
        self.m = StatuteMirror(str(FIX / "legalize-kr"))
        self.doc = self.m.resolve("근로기준법").doc
        self.a60 = self.m.find_article(self.doc, 60)

    def check(self, hang, as_of):
        return self.m.temporal_check(self.doc, self.a60, self.a60.unit_text(hang), as_of, hang)

    def test_new_paragraph_not_in_force_yet(self):
        self.assertEqual(self.check(9, AS_OF)["state"], "pending_new")   # <신설 2026.6.9>, 시행 2027-06-10

    def test_in_force_after_effective_date(self):
        self.assertEqual(self.check(9, date(2027, 7, 1))["state"], "in_force")

    def test_untouched_paragraph_in_force(self):
        self.assertEqual(self.check(1, AS_OF)["state"], "in_force")

    def test_amended_paragraph_text_pending(self):
        self.assertEqual(self.check(6, AS_OF)["state"], "pending_text")  # <개정 2026.6.9>


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


@unittest.skipUnless(has_git(), "git 필요")
class TestGitMirror(unittest.TestCase):
    """전체 이력이 있는 git 미러: 시행 중 판본을 커밋에서 직접 찾아 정밀 판정한다."""

    @classmethod
    def setUpClass(cls):
        repo = copy_fixture("legalize-kr")
        cur = repo / "kr/근로기준법/법률(법률).md"
        new_text = cur.read_text(encoding="utf-8")
        old = new_text.replace("공포일자: 2026-06-09", "공포일자: 2026-02-19").replace("시행일자: 2027-06-10", "시행일자: 2026-08-20")
        old = "\n".join(l for l in old.split("\n") if not l.startswith("**⑨**"))
        git(repo, "init", "-q")
        cur.write_text(old, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "법률: 근로기준법 (이전 판본)", "--date", "2026-02-19T00:00:00")
        cur.write_text(new_text, encoding="utf-8")
        git(repo, "commit", "-q", "-am", "법률: 근로기준법 (일부개정)", "--date", "2026-06-09T00:00:00")
        cls.repo = repo

    def setUp(self):
        self.m = StatuteMirror(str(self.repo))
        self.doc = self.m.resolve("근로기준법").doc

    def test_history_available(self):
        st = self.m.status()
        self.assertTrue(st["history"])
        self.assertEqual(len(self.m.history("근로기준법")["commits"]), 2)

    def test_precise_temporal(self):
        a = self.m.find_article(self.doc, 60)
        t9 = self.m.temporal_check(self.doc, a, a.unit_text(9), AS_OF, 9)
        self.assertEqual((t9["state"], t9["precise"]), ("pending_new", True))
        t1 = self.m.temporal_check(self.doc, a, a.unit_text(1), AS_OF, 1)
        self.assertEqual((t1["state"], t1["precise"]), ("in_force", True))

    def test_git_grep_paths_are_readable(self):
        hits = self.m.search("직장 내 괴롭힘", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["law_dir"], "근로기준법")   # v1: '\\352\\267…' 8진 이스케이프


class TestFrontmatter(unittest.TestCase):
    def test_dates_stay_strings(self):
        fm, body = split_frontmatter("---\n제목: 민법\n공포일자: 2024-01-02\n소관부처:\n- 법무부\n---\n# 민법\n")
        meta = parse_frontmatter_text(fm)
        self.assertEqual(meta["공포일자"], "2024-01-02")
        self.assertEqual(meta["소관부처"], ["법무부"])
        self.assertTrue(body.startswith("# 민법"))


if __name__ == "__main__":
    unittest.main()
