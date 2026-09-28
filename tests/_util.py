"""테스트 공용 도우미."""

import json
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


def git(repo: Path, *args: str, date: str = "") -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", env=env).stdout


def copy_fixture(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="lu-fix-")) / name
    shutil.copytree(FIX / name, d)
    return d


_HISTORY = None


def history_repo() -> Path:
    """tests/fixtures/legalize-kr-history/manifest.json 의 판본들을 공포일 순서대로 커밋한 git 저장소(세션당 1회 생성).
    실제 legalize-kr 과 같이 개정 1건 = 커밋 1개이고, 커밋 날짜는 공포일이다."""
    global _HISTORY
    if _HISTORY is not None:
        return _HISTORY
    src = FIX / "legalize-kr-history"
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    repo = Path(tempfile.mkdtemp(prefix="lu-hist-")) / "legalize-kr"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    for e in manifest["commits"]:
        dst = repo / e["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src / e["snapshot"], dst)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", e["subject"], date=f"{e['date']}T12:00:00+09:00")
    _HISTORY = repo
    return repo
