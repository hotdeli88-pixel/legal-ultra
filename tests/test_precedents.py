import unittest
from datetime import date

from _util import FIX

from precedent_engine import PrecedentMirror, is_constitutional, normalize_case_no


class TestCaseNumbers(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_case_no("2021다219529"), "2021다219529")
        self.assertEqual(normalize_case_no("2021 다 219529"), "2021다219529")
        self.assertEqual(normalize_case_no("서울고등법원-2025-누-7507"), "2025누7507")  # DRF 국세 출처 표기
        self.assertEqual(normalize_case_no("서울고법2006누16559"), "2006누16559")
        self.assertEqual(normalize_case_no("4294민상1234"), "4294민상1234")

    def test_not_case_numbers(self):
        for s in ("2024년12월", "3조 원", "제2024호", "12조의2"):
            self.assertIsNone(normalize_case_no(s), s)

    def test_constitutional(self):
        self.assertTrue(is_constitutional("2013헌마576"))
        self.assertFalse(is_constitutional("2021다219529"))


class TestMirror(unittest.TestCase):
    def setUp(self):
        self.m = PrecedentMirror(str(FIX / "precedent-kr"))

    def test_lookup(self):
        e = self.m.lookup("2021다219529")
        self.assertEqual(len(e), 1)
        self.assertEqual((e[0].court, e[0].date), ("대법원", date(2021, 9, 16)))

    def test_merged_case_secondary_number(self):
        e = self.m.lookup("2017므11863")
        self.assertEqual(len(e), 1)
        self.assertFalse(e[0].primary)
        doc = self.m.read(e[0])
        self.assertIn("2017므11863", self.m.case_numbers_in_meta(doc))

    def test_same_number_multiple_courts(self):
        courts = sorted(x.court for x in self.m.lookup("2008브4"))
        self.assertEqual(courts, ["대구지방법원", "울산지방법원"])

    def test_read_sections(self):
        doc = self.m.read(self.m.lookup("2021다219529")[0])
        self.assertIn("직장 내 성희롱", doc["meta"]["사건명"])
        self.assertIn("판시사항", doc["sections"])

    def test_latest_decision(self):
        self.assertEqual(self.m.latest_decision(), date(2025, 8, 14))


if __name__ == "__main__":
    unittest.main()
