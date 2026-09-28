"""precedent_engine.py - precedent-kr 로컬 판례 미러 엔진 (legal-ultra v2).

precedent-kr(https://github.com/legalize-kr/precedent-kr) 구조:
    {사건종류}/{법원등급}/{법원명}_{선고일자}_{사건번호}.md
    (병합: 2017므11856_11863, 본소/반소: 2000므1257_본소_1264_반소, 충돌 시 _{판례일련번호} 접미)

파일명만으로 사건번호·법원·선고일을 색인하므로 `--filter=blob:none --no-checkout` 부분 클론으로도
사건번호 실재·선고일·법원 검증이 완전 오프라인으로 된다(본문은 필요할 때 `git show` 로 지연 로드).
헌법재판소 결정은 이 미러에 없다 → DRF API(detc)로 확인한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_common import cache_root, env_path, parse_date, parse_frontmatter_text, split_frontmatter  # noqa: E402

# precedent-kr 124,980건 파일명에서 추출한 사건부호(157종) + 헌재 부호 + 지급명령 '차'.
# 사건번호 추출 시 이 목록으로만 허용해 '2024년12월' 같은 날짜를 사건번호로 오인하지 않는다.
CASE_CODES = sorted(set("""
다 누 도 구단 두 나 구합 노 가합 후 다카 구 마 가단 고단 고합 민상 허 므 모 라 그 형상 고정 행상 스 추 르
카합 감도 드 민공 사 브 수 무 드단 재누 초 로 카기 아 카 재다 행 드합 재두 재나 오 초기 느단 민재항 부
재노 느합 가소 보 루 느 으 프 재고합 우 마카 즈기 파 주 코 카담 소 고 카단 재다카 타기 재구합 재도 재구단
형공 호파 비합 감노 민재 재마 카확 즈 카경 비단 과 형항 형비상 형재항 슈 어 인라 타채 재가합 타 회합 쿠
재후 트 인마 민항 특상 하합 하면 비상 재고단 토 즈합 거 카정 가 타경 하 행항 흐 형재 터 초재 서 재감노
재보군형공 즈단 호기 재르 재드단 민준재 정모 정스 이 하단 재자 재가단 나합 준재가단 개기 머 재무 특재 재수
수흐 형 민특상 휴 카허 형비 재감도 전도 노합 감고 인 영장 푸 노형공 정로 커
헌가 헌나 헌다 헌라 헌마 헌바 헌사 헌아 차 차전 느합 너
""".split()), key=len, reverse=True)

CONSTITUTIONAL_CODES = {"헌가", "헌나", "헌다", "헌라", "헌마", "헌바", "헌사", "헌아"}

_CODE_ALT = "|".join(re.escape(c) for c in CASE_CODES)
# 공백 없는 표준 표기만 인식한다(공백·하이픈 변형은 normalize_case_no 가 먼저 제거).
CASE_NO_RE = re.compile(rf"(?<![0-9가-힣])((?:19|20|42|43)\d{{2}}|\d{{2}})({_CODE_ALT})(\d{{1,7}})(?![0-9])")


def normalize_case_no(s: str) -> Optional[str]:
    """'2021다219529', '2021 다 219529', '서울고등법원-2025-누-7507' → '2021다219529' / '2025누7507'."""
    t = re.sub(r"[\s\-]", "", s or "")
    t = re.sub(r"^\D+", "", t)  # 앞에 붙은 법원명 제거: '서울고법2006누16559'
    m = CASE_NO_RE.search(t)
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}{m.group(3)}"


def is_constitutional(case_no: str) -> bool:
    m = CASE_NO_RE.search(case_no or "")
    return bool(m and m.group(2) in CONSTITUTIONAL_CODES)


@dataclass
class PrecEntry:
    path: str        # 저장소 기준 경로
    kind: str        # 민사/형사/…
    grade: str       # 대법원/하급심
    court: str
    date: Optional[date]
    case_slot: str   # 파일명의 사건번호 부분
    primary: bool    # 파일의 대표 사건번호인가(병합 부번호가 아닌가)

    def to_dict(self) -> Dict[str, object]:
        return {"path": self.path, "kind": self.kind, "grade": self.grade, "court": self.court,
                "date": self.date.isoformat() if self.date else None, "case_slot": self.case_slot}


def _git(repo: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """git 이 없거나(FileNotFoundError) 지연 내려받기가 멈추면(TimeoutExpired) 실패 결과를 돌려준다(크래시 금지)."""
    try:
        return subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args=["git", *args], returncode=127, stdout="", stderr=str(e))


class PrecedentMirror:
    """경로 우선순위: 인자 → $PRECEDENT_KR_PATH → ./precedent-kr → ~/precedent-kr → <skill>/data/precedent-kr"""

    def __init__(self, root: Optional[str] = None):
        self.root: Optional[Path] = None
        cands: List[Path] = []
        if root:
            cands.append(Path(root).expanduser())
        ep = env_path("PRECEDENT_KR_PATH")
        if ep:
            cands.append(ep)
        cands += [Path.cwd() / "precedent-kr", Path.home() / "precedent-kr",
                  Path(__file__).resolve().parent.parent / "data" / "precedent-kr"]
        for c in cands:
            c = c.resolve()
            if (c / ".git").exists() or (c / "민사").is_dir() or (c / "형사").is_dir():
                self.root = c
                break
        self._index: Optional[Dict[str, List[PrecEntry]]] = None
        self._tsv: Optional[Tuple[str, Dict[str, str]]] = None
        self._head: Optional[str] = None
        self._lock = threading.RLock()

    @property
    def available(self) -> bool:
        return self.root is not None

    def head(self) -> str:
        if self._head is None:
            self._head = ""
            if self.root and (self.root / ".git").exists():
                r = _git(self.root, "rev-parse", "HEAD")
                self._head = r.stdout.strip() if r.returncode == 0 else ""
        return self._head

    def latest_decision(self) -> Optional[date]:
        """미러의 최신성 = 색인된 가장 늦은 선고일(커밋 날짜는 선고일이라 최신성 지표가 아니다)."""
        return parse_date(self._table()[1].get("latest"))

    def status(self) -> Dict[str, object]:
        if not self.available:
            return {"available": False, "hint": "precedent-kr 미러 없음: `legal.py setup` 또는 PRECEDENT_KR_PATH 설정"}
        latest = self.latest_decision()
        return {"available": True, "root": str(self.root), "case_numbers_indexed": int(self._table()[1].get("count", 0)),
                "latest_decision": latest.isoformat() if latest else None}

    # ---- 색인 --------------------------------------------------------------
    # 사건번호 → 파일 색인을 정렬된 TSV 로 캐시한다. 단건 조회는 TSV 텍스트에서 바로 찾고(수 ms),
    # 전체 dict 는 필요할 때만 만든다(12.9만 건 JSON 을 매번 적재하던 방식 대비 CLI 단건 조회 약 10배 빠름).

    def _list_paths(self) -> List[str]:
        if self.head():
            r = _git(self.root, "ls-tree", "-r", "--name-only", "HEAD")
            if r.returncode == 0 and r.stdout:
                return r.stdout.splitlines()
        out = []
        for dirpath, _, files in os.walk(self.root):
            if ".git" in dirpath:
                continue
            for f in files:
                if f.endswith(".md"):
                    out.append(Path(dirpath, f).relative_to(self.root).as_posix())
        return out

    def _cache_file(self) -> Path:
        tag = hashlib.sha1(str(self.root).encode("utf-8")).hexdigest()[:10]
        if self.head():
            key = self.head()[:16]
        else:  # git 이 아닌 디렉터리: 최상위 수정시각으로 무효화
            key = str(int(max((p.stat().st_mtime for p in self.root.iterdir()), default=0)))
        return cache_root() / f"precedent-index-{tag}-{key}.tsv"

    def _table(self) -> Tuple[str, Dict[str, str]]:
        """(TSV 본문, 헤더 메타). 줄 형식: 사건번호\t경로\t종류\t등급\t법원\t선고일\t사건슬롯\tprimary"""
        if self._tsv is None:
            with self._lock:
                if self._tsv is None:
                    self._tsv = self._load_or_build_tsv()
        return self._tsv

    def _load_or_build_tsv(self) -> Tuple[str, Dict[str, str]]:
        cache = self._cache_file()
        if cache.is_file():
            try:
                text = cache.read_text(encoding="utf-8")
                header, _, body = text.partition("\n")
                meta = dict(kv.split("=", 1) for kv in header.lstrip("# ").split() if "=" in kv)
                return "\n" + body, meta
            except (OSError, ValueError):
                pass
        rows: List[str] = []
        latest = ""
        for p in self._list_paths():
            parts = p.split("/")
            if len(parts) != 3 or not parts[2].endswith(".md"):
                continue
            slots = parts[2][:-3].split("_", 2)
            if len(slots) < 3:
                continue
            court, d, case_slot = slots
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and latest < d < "2100":
                latest = d
            first, prefix = True, None
            for tok in case_slot.replace(" ", "").split("_"):
                m = CASE_NO_RE.search(re.sub(r"^\D+", "", tok))
                if m:
                    no = f"{m.group(1)}{m.group(2)}{m.group(3)}"
                    prefix = f"{m.group(1)}{m.group(2)}"
                elif prefix and tok.isdigit() and len(tok) <= 6:
                    # 병합 부번호(예: 2017므11856_11863) 또는 판례일련번호 접미 — 모호하므로 primary=0
                    no = f"{prefix}{tok}"
                else:
                    continue
                rows.append("\t".join([no, p, parts[0], parts[1], court, d, case_slot.replace("\t", " "), "1" if first else "0"]))
                first = False
        rows.sort()
        count = len({r.split("\t", 1)[0] for r in rows})
        meta = {"latest": latest, "count": str(count)}
        body = "\n".join(rows) + "\n"
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(f"# latest={latest} count={count}\n" + body, encoding="utf-8")
        except OSError:
            pass
        return "\n" + body, meta

    @staticmethod
    def _entry(row: str) -> PrecEntry:
        no, path, kind, grade, court, d, slot, prim = row.split("\t")
        return PrecEntry(path, kind, grade, court, parse_date(d), slot, prim == "1")

    def index(self) -> Dict[str, List[PrecEntry]]:
        """전체 색인(dict). 대량 처리용 — 단건은 lookup() 이 더 빠르다."""
        if self._index is None:
            with self._lock:
                if self._index is None:
                    idx: Dict[str, List[PrecEntry]] = {}
                    for row in self._table()[0].strip("\n").split("\n"):
                        if row:
                            idx.setdefault(row.split("\t", 1)[0], []).append(self._entry(row))
                    self._index = idx
        return self._index

    def lookup(self, case_no: str) -> List[PrecEntry]:
        n = normalize_case_no(case_no)
        if not n:
            return []
        if self._index is not None:
            return list(self._index.get(n, []))
        text = self._table()[0]
        key = "\n" + n + "\t"
        out: List[PrecEntry] = []
        i = text.find(key)
        while i >= 0:
            j = text.find("\n", i + 1)
            out.append(self._entry(text[i + 1:j if j >= 0 else len(text)]))
            i = text.find(key, i + 1)
        return out

    # ---- 본문 --------------------------------------------------------------

    def read(self, entry: PrecEntry) -> Optional[Dict[str, object]]:
        """프론트매터 + 섹션(판시사항·판결요지·참조조문·참조판례·판례내용)."""
        text = None
        p = self.root / entry.path
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
        elif self.head():
            r = _git(self.root, "show", f"HEAD:{entry.path}", timeout=120)
            if r.returncode == 0:
                text = r.stdout
        if text is None:
            return None
        fm, body = split_frontmatter(text)
        meta = parse_frontmatter_text(fm)
        sections: Dict[str, str] = {}
        cur = None
        buf: List[str] = []
        for line in body.split("\n"):
            m = re.match(r"^##\s+(.+?)\s*$", line)
            if m:
                if cur:
                    sections[cur] = "\n".join(buf).strip()
                cur, buf = m.group(1), []
            elif cur:
                buf.append(line)
        if cur:
            sections[cur] = "\n".join(buf).strip()
        return {"meta": meta, "sections": sections, "text": body}

    def case_numbers_in_meta(self, doc: Dict[str, object]) -> List[str]:
        raw = str(doc.get("meta", {}).get("사건번호", ""))
        out, prefix = [], None
        for tok in re.split(r"[,\s]+", raw):
            m = CASE_NO_RE.search(tok)
            if m:
                prefix = f"{m.group(1)}{m.group(2)}"
                out.append(f"{prefix}{m.group(3)}")
            elif prefix and tok.isdigit():
                out.append(f"{prefix}{tok}")
        return out

    def search_titles(self, keyword: str, limit: int = 20) -> List[Dict[str, object]]:
        """파일 경로(법원·일자·사건번호)만으로는 주제 검색이 안 된다 → 작업 트리가 있으면 git grep."""
        if not self.available or not keyword.strip():
            return []
        if not any(self.root.glob("*/*/*.md")):
            return []
        r = _git(self.root, "grep", "-l", "-I", "-F", "-e", keyword, "--", ".", timeout=300)
        out = []
        for p in r.stdout.splitlines()[:limit]:
            parts = p.split("/")
            if len(parts) == 3:
                court, d, slot = (parts[2][:-3].split("_", 2) + ["", "", ""])[:3]
                out.append({"path": p, "court": court, "date": d, "case": slot, "kind": parts[0]})
        return out
