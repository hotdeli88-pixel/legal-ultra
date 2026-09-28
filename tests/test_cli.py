"""CLI·견고성 테스트. '#N' 은 2차 적대적 검토(docs/REVIEW-2026-09-28.md §F)의 결함 번호."""

import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from unittest import mock

from _util import FIX, SCRIPTS, has_git, history_repo

import legal
import legal_engine
import precedent_engine
from law_api import DrfClient


def run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
        rc = legal.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TestLegalCli(unittest.TestCase):
    def setUp(self):
        self.env = {"LEGALIZE_KR_PATH": str(FIX / "legalize-kr"), "PRECEDENT_KR_PATH": str(FIX / "precedent-kr")}

    def test_long_inline_text_is_text_not_path(self):   # #24 'File name too long'
        rc, out, _ = run(["--no-api", "verify", "가" * 400 + " 민법 제750조"], self.env)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS", out)

    def test_invalid_as_of_is_usage_error(self):   # #24
        rc, _, err = run(["--no-api", "verify", "민법 제750조", "--as-of", "2026-13-45"], self.env)
        self.assertEqual(rc, 3)
        self.assertIn("as-of", err)

    def test_verify_exit_codes(self):
        self.assertEqual(run(["--no-api", "verify", "민법 제750조"], self.env)[0], 0)
        self.assertEqual(run(["--no-api", "verify", "민법 제750조제2항"], self.env)[0], 1)
        self.assertEqual(run(["--no-api", "verify", "근로기준법 제76조의9"], self.env)[0], 2)   # 이력 없음 → INCOMPLETE(fail-closed)
        self.assertEqual(run(["--no-api", "verify", "인용 없는 문장"], self.env)[0], 2)

    def test_bom_file_input(self):   # #25
        p = FIX.parent / "_tmp_bom.md"
        p.write_text("민법 제750조", encoding="utf-8-sig")
        try:
            self.assertEqual(run(["--no-api", "verify", str(p)], self.env)[0], 0)
        finally:
            p.unlink()

    def test_bad_input_files_fail_closed(self):   # 3차 검토 #15: 파일 이름을 본문으로 검증해 '인용 없음'으로 끝내지 않는다
        rc, _, err = run(["--no-api", "verify", "draft_missing.md"], self.env)
        self.assertEqual(rc, 3)
        self.assertIn("파일이 없음", err)
        p = FIX.parent / "_tmp_cp949.md"
        p.write_bytes("민법 제750조제2항에 따르면".encode("cp949"))
        try:
            rc, _, err = run(["--no-api", "verify", str(p)], self.env)
            self.assertEqual(rc, 1)                                          # CP949 로 읽어 가짜 항을 잡는다
            self.assertIn("CP949", err)
            p.write_bytes(bytes([0xff, 0xfe, 0x00, 0xd8]) + "민법 제750조".encode("utf-16-le"))
            self.assertEqual(run(["--no-api", "verify", str(p)], self.env)[0], 3)   # 깨진 UTF-16
        finally:
            p.unlink()

    @unittest.skipUnless(has_git(), "git 필요")
    def test_article_shows_in_force_and_pending_text(self):
        env = dict(self.env, LEGALIZE_KR_PATH=str(history_repo()))
        rc, out, _ = run(["--no-api", "article", "근로기준법", "60", "--hang", "5", "--as-of", "2026-09-28"], env)
        self.assertEqual(rc, 0, out)
        self.assertIn("기준일 시행 중", out)
        self.assertIn("제1항부터 제4항까지의 규정에 따른 휴가", out)      # 기준일 문언
        self.assertIn("최신 공포본 문언", out)                             # 시행 전 새 문언도 함께
        rc, out, _ = run(["--no-api", "article", "근로기준법", "44의4", "--as-of", "2026-09-28"], env)
        self.assertEqual(rc, 1, out)
        rc, _, _ = run(["--no-api", "article", "근로기준법", "육십"], env)
        self.assertEqual(rc, 3)


class TestRobustness(unittest.TestCase):
    def test_git_missing_does_not_crash(self):   # #24
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            r = precedent_engine._git(FIX, "status")
            self.assertEqual(r.returncode, 127)
            r = legal_engine._git(FIX, "status")
            self.assertEqual(r.returncode, 127)
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 1)):
            self.assertEqual(precedent_engine._git(FIX, "show", "HEAD:x").returncode, 127)

    def test_legacy_engine_cli_bad_numbers(self):   # #24
        env = {"LEGALIZE_KR_PATH": str(FIX / "legalize-kr")}
        with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(legal_engine.cli(["get", "근로기준법", "abc"]), 2)
            self.assertEqual(legal_engine.cli(["search", "괴롭힘", "all", "abc"]), 2)
        self.assertIn("오류", out.getvalue())

    def test_cp949_console_does_not_crash(self):
        # 한국어 Windows 파이프(cp949)에서도 이모지가 든 보고서를 출력할 수 있어야 한다
        env = dict(os.environ, PYTHONIOENCODING="cp949", LEGALIZE_KR_PATH=str(FIX / "legalize-kr"),
                   PRECEDENT_KR_PATH=str(FIX / "precedent-kr"), LEGAL_ULTRA_OFFLINE="1")
        r = subprocess.run([sys.executable, str(SCRIPTS / "legal.py"), "--no-api", "verify", "민법 제750조"],
                           capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))

    def test_find_law_before_first_version_is_not_current(self):
        c = DrfClient(oc="t", offline=False, use_cache=False, retries=0)
        items = [{"법령명한글": "개인정보 보호법", "법령약칭명": "", "시행일자": "20110930", "현행연혁코드": "연혁", "법령일련번호": "1"},
                 {"법령명한글": "개인정보 보호법", "법령약칭명": "", "시행일자": "20260908", "현행연혁코드": "현행", "법령일련번호": "2"}]
        with mock.patch.object(DrfClient, "search", return_value={"items": items}):
            self.assertEqual(c.find_law("개인정보 보호법", date(2010, 1, 1))["status"], "not_in_force")
            fl = c.find_law("개인정보 보호법", date(2020, 1, 1))
            self.assertEqual((fl["status"], fl["mst"]), ("ok", "1"))


if __name__ == "__main__":
    unittest.main()
