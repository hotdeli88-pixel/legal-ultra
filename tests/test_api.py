import io
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from _util import FIX

import law_api
from law_api import ApiAuthError, ApiError, ApiUnavailable, DrfClient


def fx(name: str) -> bytes:
    return (FIX / "drf" / name).read_bytes()


class FakeResp(io.BytesIO):
    def __init__(self, data: bytes, ctype: str = "application/xml;charset=UTF-8"):
        super().__init__(data)
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def router(routes):
    calls = []

    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        ep = "search" if "lawSearch" in url else "service"
        calls.append((ep, q))
        data = routes.get((ep, q.get("target")))
        if isinstance(data, Exception):
            raise data
        if data is None:
            return FakeResp(b"", "")
        return FakeResp(data)
    return urlopen, calls


class TestDrf(unittest.TestCase):
    def client(self):
        return DrfClient(oc="tester", offline=False, use_cache=False, retries=0)

    def test_record_tags_per_target(self):
        urlopen, _ = router({("search", "prec"): fx("list_prec.xml"), ("search", "detc"): fx("list_detc.xml"),
                             ("search", "ordin"): fx("list_ordin.xml"), ("search", "expc"): fx("list_expc.xml"),
                             ("search", "law"): fx("list_law.xml")})
        with mock.patch("urllib.request.urlopen", urlopen):
            c = self.client()
            prec = c.search("prec", "교육")
            self.assertTrue(prec["items"])
            self.assertTrue(prec["items"][0]["_id"])
            self.assertTrue(c.search("detc", "교육")["items"])        # 레코드 태그 'Detc'(v1: 'detc' → 항상 0건)
            ordin = c.search("ordin", "교육")["items"]                 # 레코드 태그 'law'(v1: 'ordin' → 항상 0건)
            self.assertTrue(ordin and ordin[0].get("자치법규명"))
            self.assertTrue(c.search("expc", "교육")["items"])        # 루트 'Expc'
            self.assertTrue(c.search("law", "교육")["items"])

    def test_law_body_filters_chapter_headings(self):
        urlopen, _ = router({("service", "law"): fx("body_law.xml")})
        with mock.patch("urllib.request.urlopen", urlopen):
            body = self.client().law_articles("209959")
        arts = body["articles"]
        self.assertEqual(len(arts), 29)                  # 조문단위 39 중 '전문'(장·절 표제) 10개 제외
        first = [a for a in arts if a["jo"] == 1][0]
        self.assertEqual(first["title"], "목적")           # v1: 같은 번호의 '제1장 총칙' 을 조문으로 반환
        self.assertTrue(body["info"].get("법령명_한글"))

    def test_not_found_envelope(self):
        urlopen, _ = router({("service", "prec"): fx("body_prec.xml")})
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertIsNone(self.client().precedent_body("1"))

    def test_precedent_body_ok(self):
        urlopen, _ = router({("service", "prec"): fx("body_prec_ok.xml")})
        with mock.patch("urllib.request.urlopen", urlopen):
            b = self.client().precedent_body("617149")
        self.assertEqual(b["사건번호"], "2025두35074")
        self.assertFalse(b["판시사항"].startswith("<br/>"))

    def test_auth_failure(self):
        urlopen, _ = router({("search", "law"): fx("auth_fail_synthetic.xml")})
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(ApiAuthError):
                self.client().search("law", "민법")

    def test_empty_body_is_error_not_zero_results(self):
        urlopen, _ = router({})
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(ApiError):
                self.client().search("prec", "교육")

    def test_unknown_target_blocked_before_network(self):
        urlopen, calls = router({})
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(ApiError):
                self.client().search("cgmExpcMoel", "근로")   # v1 문서의 뒤집힌 코드
        self.assertEqual(calls, [])

    def test_network_down_fast_fail(self):
        urlopen, calls = router({("search", "law"): urllib.error.URLError("Tunnel connection failed: 403 Forbidden")})
        with mock.patch("urllib.request.urlopen", urlopen):
            c = self.client()
            with self.assertRaises(ApiUnavailable):
                c.search("law", "민법")
            with self.assertRaises(ApiUnavailable):
                c.search("law", "형법")
        self.assertEqual(len(calls), 1)

    def test_case_number_uses_nb_and_exact_match(self):
        urlopen, calls = router({("search", "prec"): fx("list_prec.xml")})
        with mock.patch("urllib.request.urlopen", urlopen):
            hits = self.client().precedent_by_number("2025누7507")
        self.assertEqual(calls[0][1].get("nb"), "2025누7507")
        self.assertTrue(all("7507" in h["사건번호"] for h in hits))

    def test_registry_codes(self):
        self.assertIn("moelCgmExpc", law_api.TARGETS)
        self.assertIn("ttSpecialDecc", law_api.TARGETS)
        self.assertNotIn("cgmExpcMoel", law_api.TARGETS)
        self.assertEqual(len(law_api.COMMITTEES), 12)
        self.assertEqual(sum(1 for t in law_api.TARGETS if t.endswith("CgmExpc")), 39)

    def test_cache_key_excludes_oc(self):
        a = DrfClient(oc="alice", use_cache=True)
        b = DrfClient(oc="bob", use_cache=True)
        url = law_api.SEARCH_URL + "?query=x&target=law&type=XML"
        self.assertEqual(a._cache_path(url), b._cache_path(url))


if __name__ == "__main__":
    unittest.main()
