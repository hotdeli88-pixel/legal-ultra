"""테스트 공용 도우미."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SCRIPTS))

# 테스트는 네트워크·사용자 캐시에 의존하지 않는다
os.environ["LEGAL_ULTRA_OFFLINE"] = "1"
os.environ["LEGAL_ULTRA_CACHE"] = tempfile.mkdtemp(prefix="lu-cache-")
for k in ("LEGALIZE_KR_PATH", "PRECEDENT_KR_PATH", "LAW_OPENAPI_OC"):
    os.environ.pop(k, None)


def has_git() -> bool:
    return shutil.which("git") is not None


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", env=env).stdout


def copy_fixture(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="lu-fix-")) / name
    shutil.copytree(FIX / name, d)
    return d
