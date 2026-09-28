"""legal_engine.py - legalize-kr 로컬 법령 미러 엔진 (legal-ultra v2).

legalize-kr(https://github.com/legalize-kr/legalize-kr) 저장소 구조
    kr/{법령명(공백 제거)}/{법률|시행령|시행규칙|대통령령|…}.md
    kr/{법령명}/{기본파일명}({법령구분}).md   ← 같은 경로를 다른 법령ID가 쓰는 경우
를 읽어 조문(조·항·호·목)을 구조화하고, 시행일 기준(as-of) 유효성을 판정한다.

v1 대비 수정된 결함(실데이터 재현 근거: docs/REVIEW-2026-09-28.md)
  - 같은 디렉터리의 여러 판본 중 파일 선택이 디렉터리 순서에 좌우되던 문제
    (PyYAML date 비교 TypeError 가 삼켜짐 → 1997년 폐지 법률을 현행 근로기준법으로 읽을 수 있었음)
  - 프론트매터 2KB 절단, 삭제 조문을 "존재"로 보고, 마지막 조문이 부칙 전체를 삼키던 문제
  - 위임 조문 탐색 정규식 오류(가지조문 0건, 엉뚱한 제1조 반환, "정한다" 형태 누락)
  - git grep 경로 8진 이스케이프, 부분 일치로 다른 법령에 매칭되던 문제
  - 시행예정 판본을 현행으로 제시하던 문제 → as-of 판정 추가
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_common import (  # noqa: E402
    CIRCLED, CIRCLED_CHARS, amendment_markers, cache_root, configure_stdout, env_path,
    iso, normalize_law_name, parse_date, parse_frontmatter_text, read_frontmatter,
    split_frontmatter, today,
)

# ---------------------------------------------------------------------------
# 자료 구조
# ---------------------------------------------------------------------------


@dataclass
class Item:
    """호(1., 10의2.)와 그 아래 목(가., 나.)."""
    no: str
    text: str
    subitems: Dict[str, str] = field(default_factory=dict)


@dataclass
class Paragraph:
    """항(①…). no=0 은 번호 없는 단일 본문."""
    no: int
    text: str
    items: Dict[str, Item] = field(default_factory=dict)


@dataclass
class Article:
    jo: int
    sub: Optional[int]
    title: str
    heading: str
    text: str
    line: int
    deleted: bool = False
    deleted_on: Optional[date] = None
    paragraphs: Dict[int, Paragraph] = field(default_factory=dict)
    items: Dict[str, Item] = field(default_factory=dict)  # 항 번호 없이 바로 달린 호

    @property
    def label(self) -> str:
        return f"제{self.jo}조" + (f"의{self.sub}" if self.sub else "")

    def unit_text(self, hang: Optional[int] = None, ho: Optional[str] = None,
                  mok: Optional[str] = None) -> Optional[str]:
        """인용 단위(조/항/호/목)의 본문. 없으면 None."""
        if hang is None and ho is None:
            return self.text
        items = self.items
        base_text = self.text
        if hang is not None:
            p = self.paragraphs.get(hang)
            if p is None:
                return None
            items, base_text = p.items, p.text
            if ho is None:
                return p.text + "".join("\n" + i.text for i in p.items.values())
        if ho is not None:
            it = items.get(ho)
            if it is None:
                return None
            if mok is None:
                return it.text + "".join("\n" + t for t in it.subitems.values())
            return it.subitems.get(mok)
        return base_text


@dataclass
class LawDoc:
    law_dir: str
    rel: str          # 저장소 기준 경로, 예: kr/근로기준법/법률(법률).md
    kind: str         # 파일 기본명: 법률, 시행령, 시행규칙, 대통령령 …
    meta: Dict[str, object]

    @property
    def title(self) -> str:
        return str(self.meta.get("제목") or self.law_dir)

    @property
    def promulgated(self) -> Optional[date]:
        return parse_date(self.meta.get("공포일자"))

    @property
    def effective(self) -> Optional[date]:
        return parse_date(self.meta.get("시행일자"))

    @property
    def status(self) -> str:
        return str(self.meta.get("상태") or "")

    def summary(self) -> Dict[str, object]:
        return {
            "title": self.title, "file": self.rel, "kind": self.kind,
            "법령ID": self.meta.get("법령ID"), "법령MST": self.meta.get("법령MST"),
            "공포일자": iso(self.promulgated), "시행일자": iso(self.effective),
            "상태": self.status, "출처": self.meta.get("출처"),
        }


@dataclass
class Resolution:
    status: str                    # ok | not_found | ambiguous
    query: str
    doc: Optional[LawDoc] = None
    candidates: List[str] = field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------------------
# 파서
# ---------------------------------------------------------------------------

_ART_HEAD_RE = re.compile(r"^#####\s*제(\d+)조(?:의(\d+))?\s*(.*)$")
_PARA_RE = re.compile(r"^\*\*([" + CIRCLED_CHARS + r"])\*\*\s?(.*)$")
_ITEM_RE = re.compile(r"^ {1,3}(\d+(?:의\d+)?)\\?\.\s?(.*)$")
_MOK_RE = re.compile(r"^ {3,}([가-힣])\\?\.\s?(.*)$")
_DELETED_RE = re.compile(r"^삭제\s*(?:<([^>]*)>)?\s*$")


def _split_title(rest: str) -> str:
    rest = rest.strip()
    if not rest.startswith("("):
        return ""
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return rest[1:i].strip()
    return rest[1:].strip()


def parse_articles(body: str, line_offset: int = 0) -> List[Article]:
    """본문을 조문 단위로 자른다. 조문은 다음 제목줄('#' 으로 시작: 다음 조·절·장·부칙)에서 끝난다.
    line_offset: 프론트매터 줄 수 — Article.line 을 파일 기준 줄번호(git grep 과 같은 기준)로 맞춘다."""
    lines = body.split("\n")
    arts: List[Article] = []
    cur: Optional[Article] = None
    buf: List[str] = []

    def close():
        nonlocal cur, buf
        if cur is None:
            return
        text = "\n".join(buf).strip("\n")
        cur.text = text
        _structure(cur, buf)
        m = _DELETED_RE.match(text.strip())
        if m and not cur.paragraphs and not cur.items:
            cur.deleted = True
            cur.deleted_on = parse_date(m.group(1)) if m.group(1) else None
        arts.append(cur)
        cur, buf = None, []

    for idx, line in enumerate(lines, 1):
        if line.startswith("#"):
            close()
            m = _ART_HEAD_RE.match(line)
            if m:
                cur = Article(
                    jo=int(m.group(1)), sub=int(m.group(2)) if m.group(2) else None,
                    title=_split_title(m.group(3)), heading=line.strip(), text="", line=idx + line_offset)
            continue
        if cur is not None:
            buf.append(line)
    close()
    return arts


def _structure(art: Article, lines: List[str]) -> None:
    para: Optional[Paragraph] = None
    item: Optional[Item] = None
    for line in lines:
        if not line.strip():
            continue
        m = _PARA_RE.match(line)
        if m:
            para = Paragraph(no=CIRCLED[m.group(1)], text=line.strip())
            art.paragraphs[para.no] = para
            item = None
            continue
        m = _ITEM_RE.match(line)
        if m:
            item = Item(no=m.group(1), text=line.strip())
            (para.items if para else art.items)[item.no] = item
            continue
        m = _MOK_RE.match(line)
        if m and item is not None:
            item.subitems[m.group(1)] = line.strip()
            continue
        # 이어지는 줄
        if item is not None:
            item.text += "\n" + line.strip()
        elif para is not None:
            para.text += "\n" + line.strip()


# ---------------------------------------------------------------------------
# git 도우미
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = False, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=check, timeout=timeout)


# ---------------------------------------------------------------------------
# 미러
# ---------------------------------------------------------------------------

_SUFFIXES = ("시행규칙", "시행령")


class StatuteMirror:
    """legalize-kr 로컬 미러. 경로 우선순위:
    인자 → $LEGALIZE_KR_PATH → ./legalize-kr → ~/legalize-kr → <skill>/data/legalize-kr
    (저장소 루트 또는 그 안의 kr/ 어느 쪽을 줘도 된다)"""

    def __init__(self, root: Optional[str] = None):
        self.repo_root: Optional[Path] = None
        self.kr_root: Optional[Path] = None
        cands: List[Path] = []
        if root:
            cands.append(Path(root).expanduser())
        ep = env_path("LEGALIZE_KR_PATH")
        if ep:
            cands.append(ep)
        cands += [Path.cwd() / "legalize-kr", Path.home() / "legalize-kr",
                  Path(__file__).resolve().parent.parent / "data" / "legalize-kr"]
        for c in cands:
            c = c.resolve()
            if (c / "kr").is_dir() or (c / ".git").exists() and self._git_has_kr(c):
                self.repo_root, self.kr_root = c, c / "kr"
                break
            if c.name == "kr" and c.is_dir():
                self.repo_root, self.kr_root = c.parent, c
                break
        self._dirs: Optional[Dict[str, List[str]]] = None   # dir -> [file names]
        self._title_index: Optional[Dict[str, List[str]]] = None
        self._meta_cache: Dict[str, Dict[str, object]] = {}
        self._git_info: Optional[Dict[str, object]] = None
        self._lock = threading.RLock()

    @staticmethod
    def _git_has_kr(c: Path) -> bool:
        try:
            r = _git(c, "ls-tree", "--name-only", "HEAD", "kr")
            return r.returncode == 0 and r.stdout.strip() == "kr"
        except (OSError, subprocess.SubprocessError):
            return False

    # ---- 상태 --------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.repo_root is not None

    def git_info(self) -> Dict[str, object]:
        if self._git_info is not None:
            return self._git_info
        info: Dict[str, object] = {"is_git": False}
        if self.repo_root and (self.repo_root / ".git").exists():
            try:
                r = _git(self.repo_root, "log", "-1", "--format=%H|%cd", "--date=short")
                if r.returncode == 0 and "|" in r.stdout:
                    h, d = r.stdout.strip().split("|", 1)
                    info.update(is_git=True, head=h, head_date=d)
                    s = _git(self.repo_root, "rev-parse", "--is-shallow-repository")
                    info["shallow"] = s.stdout.strip() == "true"
            except (OSError, subprocess.SubprocessError):
                pass
        self._git_info = info
        return info

    def status(self) -> Dict[str, object]:
        if not self.available:
            return {"available": False, "hint": "legalize-kr 미러 없음: `legal.py setup` 또는 LEGALIZE_KR_PATH 설정"}
        gi = self.git_info()
        return {"available": True, "root": str(self.repo_root), "law_dirs": len(self.dirs),
                "git": gi.get("is_git"), "head_date": gi.get("head_date"),
                "history": bool(gi.get("is_git")) and not gi.get("shallow", True)}

    # ---- 색인 --------------------------------------------------------------

    @property
    def dirs(self) -> Dict[str, List[str]]:
        # 검증기는 인용을 병렬(스레드)로 확인한다 → 색인은 잠금 아래 완성한 뒤에만 공개한다.
        # (빈 dict 를 먼저 공개하면 다른 스레드가 '법령 없음'으로 오판한다 — 테스트로 재현된 경쟁 조건)
        if self._dirs is None:
            with self._lock:
                if self._dirs is None:
                    self._dirs = self._build_dirs()
        return self._dirs

    def _build_dirs(self) -> Dict[str, List[str]]:
        dirs: Dict[str, List[str]] = {}
        if self.kr_root and self.kr_root.is_dir() and any(self.kr_root.iterdir()):
            with os.scandir(self.kr_root) as it:
                for e in it:
                    if e.is_dir():
                        dirs[e.name] = sorted(f.name for f in os.scandir(e.path) if f.name.endswith(".md"))
        elif self.repo_root and self.git_info().get("is_git"):
            # --no-checkout 부분 클론: 작업 트리 없이 트리 객체에서 목록을 읽는다
            r = _git(self.repo_root, "ls-tree", "-r", "--name-only", "HEAD", "kr")
            for p in r.stdout.splitlines():
                parts = p.split("/")
                if len(parts) == 3 and parts[2].endswith(".md"):
                    dirs.setdefault(parts[1], []).append(parts[2])
        return dirs

    def _read(self, rel: str) -> Optional[str]:
        p = self.repo_root / rel if self.repo_root else None
        if p and p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
        if self.repo_root and self.git_info().get("is_git"):
            r = _git(self.repo_root, "show", f"HEAD:{rel}")
            if r.returncode == 0:
                return r.stdout
        return None

    def _meta(self, rel: str) -> Dict[str, object]:
        if rel in self._meta_cache:
            return self._meta_cache[rel]
        p = self.repo_root / rel
        if p.is_file():
            meta = read_frontmatter(p)
        else:
            text = self._read(rel) or ""
            meta = parse_frontmatter_text(split_frontmatter(text)[0])
        self._meta_cache[rel] = meta
        return meta

    def _doc(self, d: str, fname: str) -> LawDoc:
        rel = f"kr/{d}/{fname}"
        kind = fname[:-3].split("(")[0]
        return LawDoc(law_dir=d, rel=rel, kind=kind, meta=self._meta(rel))

    def title_index(self) -> Dict[str, List[str]]:
        """정규화된 '제목' → [rel] (디렉터리명과 제목이 다른 문서 대비). 디스크 캐시."""
        if self._title_index is None:
            with self._lock:
                if self._title_index is None:
                    self._title_index = self._build_title_index()
        return self._title_index

    def _build_title_index(self) -> Dict[str, List[str]]:
        head = self.git_info().get("head")
        if head:
            key = str(head)[:12]
        else:  # git 이 아닌 미러: 파일 수·수정시각으로 캐시 무효화
            key = f"{sum(len(v) for v in self.dirs.values())}-{int(self.kr_root.stat().st_mtime)}"
        tag = hashlib.sha1(str(self.repo_root).encode("utf-8")).hexdigest()[:10]
        cache = cache_root() / f"statute-titles-{tag}-{key}.json"
        if cache.is_file():
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        idx: Dict[str, List[str]] = {}
        for d, files in self.dirs.items():
            for f in files:
                rel = f"kr/{d}/{f}"
                t = normalize_law_name(str(self._meta(rel).get("제목") or ""))
                if t:
                    idx.setdefault(t, []).append(rel)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return idx

    # ---- 법령 해석 ---------------------------------------------------------

    def resolve(self, name: str) -> Resolution:
        """법령명 → 문서. **정확 일치만** 허용(부분 일치로 다른 법령에 붙지 않는다)."""
        q = normalize_law_name(name)
        if not self.available:
            return Resolution("not_found", name, note="미러 없음")
        if not q:
            return Resolution("not_found", name, note="빈 법령명")
        wanted_kind = None
        base = q
        for suf in _SUFFIXES:
            if q.endswith(suf) and q[: -len(suf)] in self.dirs:
                base, wanted_kind = q[: -len(suf)], suf
                break
        rels: List[str] = []
        if base in self.dirs:
            files = self.dirs[base]
            if wanted_kind:
                rels = [f"kr/{base}/{f}" for f in files if f.startswith(wanted_kind)]
            else:
                rels = [f"kr/{base}/{f}" for f in files if not f.startswith(_SUFFIXES)]
            titled = [r for r in rels if normalize_law_name(str(self._meta(r).get("제목") or "")) == q]
            rels = titled or rels
        if not rels:
            rels = list(self.title_index().get(q, []))
        if not rels:
            pool = list(self.dirs.keys())
            sugg = difflib.get_close_matches(base, pool, n=5, cutoff=0.6)
            if not sugg:
                sugg = [d for d in pool if base in d][:5]
            return Resolution("not_found", name, candidates=sugg,
                              note="정확히 일치하는 법령명이 없음(약칭·오기일 수 있음 — 정식 법령명으로 인용)")
        docs = [self._doc(r.split("/")[1], r.split("/")[2]) for r in rels]
        # 같은 제목의 여러 판본(법령ID 충돌 해소 파일 포함) → 공포일자가 가장 늦은 것.
        docs.sort(key=lambda x: (iso(x.promulgated), x.status == "시행"), reverse=True)
        note = ""
        if len(docs) > 1:
            note = "동명 문서 %d건 중 공포일자 최신본 선택: %s" % (len(docs), ", ".join(d.rel for d in docs))
        return Resolution("ok", name, doc=docs[0], note=note)

    # ---- 조문 --------------------------------------------------------------

    @lru_cache(maxsize=64)
    def _articles_for(self, rel: str, rev: str = "HEAD") -> Tuple[Tuple[Article, ...], Dict[str, object]]:
        if rev == "HEAD":
            text = self._read(rel)
        else:
            r = _git(self.repo_root, "show", f"{rev}:{rel}")
            text = r.stdout if r.returncode == 0 else None
        if text is None:
            return tuple(), {}
        fm, body = split_frontmatter(text)
        offset = text.count("\n") - body.count("\n")
        return tuple(parse_articles(body, offset)), parse_frontmatter_text(fm)

    def articles(self, doc: LawDoc) -> List[Article]:
        return list(self._articles_for(doc.rel)[0])

    def find_article(self, doc: LawDoc, jo: int, sub: Optional[int] = None, rev: str = "HEAD") -> Optional[Article]:
        for a in self._articles_for(doc.rel, rev)[0]:
            if a.jo == jo and a.sub == sub:
                return a
        return None

    def versions(self, doc: LawDoc, limit: int = 400) -> List[Tuple[str, str]]:
        """(commit, 공포일자) 최신순. 전체 이력 클론에서만 의미가 있다."""
        gi = self.git_info()
        if not gi.get("is_git") or gi.get("shallow", True):
            return []
        r = _git(self.repo_root, "log", f"-n{limit}", "--format=%H|%ad", "--date=short", "--", doc.rel)
        return [tuple(l.split("|", 1)) for l in r.stdout.splitlines() if "|" in l]  # type: ignore

    def in_force_revision(self, doc: LawDoc, as_of: date) -> Optional[Tuple[str, Dict[str, object]]]:
        """as_of 에 시행 중인 판본(커밋, 메타). 이력이 없으면 None."""
        for commit, _ in self.versions(doc):
            meta = self._articles_for(doc.rel, commit)[1]
            eff = parse_date(meta.get("시행일자"))
            if eff and eff <= as_of:
                return commit, meta
        return None

    # ---- 시행일 판정 ---------------------------------------------------------

    def temporal_check(self, doc: LawDoc, art: Article, unit_text: str, as_of: date,
                       hang: Optional[int] = None, ho: Optional[str] = None) -> Dict[str, object]:
        """인용 단위가 as_of 에 시행 중인지 판정.

        반환: {"state": in_force|pending_new|pending_text|pending_delete|unknown, "note": str, "precise": bool}
        - HEAD 판본의 시행일자 ≤ as_of → in_force
        - HEAD 가 시행예정 판본이면: 전체 이력이 있으면 시행 중 판본에서 직접 확인(precise),
          없으면 개정표시(<신설 공포일>, <개정 공포일>, 삭제 <공포일>)로 판정
        """
        eff, prom = doc.effective, doc.promulgated
        recent = sorted({d for _, d in amendment_markers(unit_text or "")
                         if d <= as_of and (as_of - d).days <= 400 and d != prom})
        recent_note = ("최근 개정분(" + ", ".join(iso(d) for d in recent) + ") 포함 — 부칙상 조항별 시행일은 "
                       "전체 이력 미러 또는 DRF(조문시행일자)로 확인 권장") if recent else ""
        if eff is None or eff <= as_of:
            return {"state": "in_force", "note": recent_note, "precise": not recent}
        base_note = f"미러 판본은 {iso(prom)} 공포·{iso(eff)} 시행예정(as-of {iso(as_of)} 기준 미시행)"
        rev = self.in_force_revision(doc, as_of)
        if rev is not None:
            commit, meta = rev
            old = self.find_article(doc, art.jo, art.sub, rev=commit)
            label = f"시행 중 판본(공포 {iso(parse_date(meta.get('공포일자')))}, 시행 {iso(parse_date(meta.get('시행일자')))}, {commit[:10]})"
            if old is None or old.deleted:
                return {"state": "pending_new", "precise": True,
                        "note": f"{base_note}; {label}에는 이 조문이 없음"}
            if old.unit_text(hang, ho) is None:
                return {"state": "pending_new", "precise": True,
                        "note": f"{base_note}; {label}에는 이 항·호가 없음"}
            if (old.unit_text(hang, ho) or "").strip() != (unit_text or "").strip():
                return {"state": "pending_text", "precise": True,
                        "note": f"{base_note}; {label}과 문언이 다름 — 현행 문언으로 인용할 것"}
            return {"state": "in_force", "precise": True, "note": f"{base_note}; {label}과 동일"}
        # 이력 없음 → 개정표시 휴리스틱
        marks = amendment_markers(unit_text or "")
        art_marks = amendment_markers(art.text)
        on_prom = [k for k, d in marks if prom and d == prom]
        if art.deleted and art.deleted_on and prom and art.deleted_on == prom:
            return {"state": "pending_delete", "precise": False,
                    "note": f"{base_note}; 이 조문은 시행예정 개정으로 삭제될 예정(현재는 시행 중)"}
        if any(k in ("신설", "본조신설") for k in on_prom) or any(
                k == "본조신설" and prom and d == prom for k, d in art_marks):
            return {"state": "pending_new", "precise": False,
                    "note": f"{base_note}; 이 조항은 {iso(prom)} 신설분이라 아직 시행 전"}
        if on_prom:
            return {"state": "pending_text", "precise": False,
                    "note": f"{base_note}; 이 조항은 {iso(prom)} 개정분 — 현행 문언과 다를 수 있음"}
        return {"state": "in_force", "precise": False,
                "note": f"{base_note}; 이 조항에는 시행예정 개정 표시가 없음(개정 대상 아님으로 추정)"
                        + (f"; {recent_note}" if recent_note else "")}

    # ---- 검색 --------------------------------------------------------------

    def search(self, keyword: str, law: Optional[str] = None, limit: int = 20,
               kinds: Optional[List[str]] = None) -> List[Dict[str, object]]:
        """고정 문자열 검색. git 작업 트리면 git grep, 아니면 파이썬 스캔."""
        if not self.available or not keyword.strip():
            return []
        scope = "kr/"
        if law:
            res = self.resolve(law)
            if res.status == "ok" and res.doc:
                scope = f"kr/{res.doc.law_dir}/"
            else:
                return []
        hits: List[Dict[str, object]] = []
        has_tree = self.kr_root is not None and self.kr_root.is_dir() and any(self.kr_root.iterdir())
        if self.git_info().get("is_git") and has_tree:
            r = _git(self.repo_root, "grep", "-n", "-I", "-F", "--null", "--max-count", "5",
                     "-e", keyword, "--", scope)
            for line in r.stdout.split("\n"):
                parts = line.split("\0")
                if len(parts) < 3:
                    continue
                rel, lno, text = parts[0], parts[1], "\0".join(parts[2:])
                hits.append(self._hit(rel, int(lno), text))
                if len(hits) >= limit * 3:
                    break
        elif has_tree:
            base = self.repo_root / scope
            for dirpath, _, files in os.walk(base):
                for f in sorted(files):
                    if not f.endswith(".md"):
                        continue
                    p = Path(dirpath) / f
                    try:
                        for i, t in enumerate(p.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                            if keyword in t:
                                hits.append(self._hit(p.relative_to(self.repo_root).as_posix(), i, t))
                    except OSError:
                        continue
                if len(hits) >= limit * 3:
                    break
        if kinds:
            hits = [h for h in hits if h["kind"] in kinds]
        # 조문 본문 적중을 앞에, 같은 법령 중복을 줄인다
        hits.sort(key=lambda h: (h["article"] is None, h["law_dir"], h["line"]))
        return hits[:limit]

    def _hit(self, rel: str, lno: int, text: str) -> Dict[str, object]:
        parts = rel.split("/")
        d = parts[1] if len(parts) > 2 else ""
        fname = parts[-1]
        art = None
        doc = self._doc(d, fname) if d else None
        if doc:
            for a in self._articles_for(rel)[0]:
                if a.line <= lno:
                    art = a
                else:
                    break
            # 조문 범위를 벗어난 줄(부칙 등)
            if art is not None and lno > art.line + art.text.count("\n") + 2:
                art = None
        return {"law_dir": d, "title": doc.title if doc else d, "kind": fname[:-3], "file": rel,
                "line": lno, "article": art.label if art else None,
                "article_title": art.title if art else None, "text": text.strip()[:300]}

    # ---- 위임 조문 -----------------------------------------------------------

    _DELEG_RE = re.compile(
        r"(대통령령|총리령|[가-힣]{1,12}부령|[가-힣]{1,12}(?:위원회|원|처|청)규칙|시행령|시행규칙)\s*(?:으로|이|에서)\s*정(?:하는|한|한다|하며|하고|할|해야|하여야)")

    def delegated(self, law_name: str, jo: int, sub: Optional[int] = None) -> Dict[str, object]:
        """법률 조문의 위임 문구를 찾고, 시행령·시행규칙에서 '법 제N조(의M)'를 인용하는 조문을 돌려준다."""
        res = self.resolve(law_name)
        if res.status != "ok" or not res.doc:
            return {"error": f"법령을 찾을 수 없음: {law_name}", "candidates": res.candidates}
        doc = res.doc
        art = self.find_article(doc, jo, sub)
        if art is None:
            return {"error": f"{doc.title}에 제{jo}조" + (f"의{sub}" if sub else "") + " 없음"}
        phrases = sorted({m.group(1) for m in self._DELEG_RE.finditer(art.text)})
        ref = rf"법\s*제{jo}조" + (rf"의{sub}" if sub else r"(?!의\d)") + r"(?!\d)"
        ref_re = re.compile(ref)
        out = []
        for kind in _SUFFIXES[::-1]:  # 시행령, 시행규칙
            files = [f for f in self.dirs.get(doc.law_dir, []) if f.startswith(kind)]
            for f in files:
                sub_doc = self._doc(doc.law_dir, f)
                for a in self.articles(sub_doc):
                    if ref_re.search(a.text) or ref_re.search(a.heading):
                        out.append({"doc": sub_doc.title, "file": sub_doc.rel, "article": a.label,
                                    "title": a.title, "text": a.text[:1500]})
        return {"law": doc.title, "article": art.label, "title": art.title,
                "delegation_phrases": phrases, "linked": out,
                "note": "정규식 기반 연계(참고용). 정확한 위임관계는 DRF 3단비교(thdcmp)로 교차 확인"}

    # ---- 이력 --------------------------------------------------------------

    def history(self, law_name: str, count: int = 10) -> Dict[str, object]:
        res = self.resolve(law_name)
        if res.status != "ok" or not res.doc:
            return {"error": f"법령을 찾을 수 없음: {law_name}", "candidates": res.candidates}
        gi = self.git_info()
        if not gi.get("is_git"):
            return {"error": "git 저장소가 아니어서 이력을 볼 수 없음"}
        r = _git(self.repo_root, "log", f"-n{count}", "--date=short", "--format=%h|%ad|%s", "--", res.doc.rel)
        items = []
        for line in r.stdout.splitlines():
            if line.count("|") >= 2:
                h, d, s = line.split("|", 2)
                items.append({"commit": h, "date": d, "subject": s})
        note = "얕은 클론(--depth 1)이라 최신 커밋만 보임 — 전체 이력은 `legal.py setup --full-history`" if gi.get("shallow") else ""
        return {"law": res.doc.title, "file": res.doc.rel, "commits": items, "note": note}


# ---------------------------------------------------------------------------
# 조문 번호 파싱
# ---------------------------------------------------------------------------

_ARTNO_RE = re.compile(r"^\s*(?:제\s*)?(\d+)\s*(?:조)?\s*(?:(?:의|-)\s*(\d+))?\s*(?:조)?\s*$")


def parse_article_no(text: str) -> Tuple[int, Optional[int]]:
    """'제76조의2', '76조의2', '76의2', '76-2', '60' → (76, 2) / (60, None)."""
    m = _ARTNO_RE.match(str(text))
    if not m:
        raise ValueError(f"조문 번호 형식이 아님: {text!r} (예: 60, 76조의2, 제76조의2)")
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


# ---------------------------------------------------------------------------
# (하위호환) v1 CLI: python legal_engine.py get|search|list|history|delegated
# ---------------------------------------------------------------------------


def cli(argv: Optional[List[str]] = None) -> int:
    configure_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("사용법 (v1 호환, 새 통합 CLI는 scripts/legal.py):")
        print("  python legal_engine.py get <법령명> <조문번호> [법률|시행령|시행규칙]")
        print("  python legal_engine.py search <키워드> [법령명|all] [건수]")
        print("  python legal_engine.py list <법령명> [법률|시행령|시행규칙]")
        print("  python legal_engine.py history <법령명>")
        print("  python legal_engine.py delegated <법령명> <조문번호>")
        return 0
    m = StatuteMirror()
    if not m.available:
        print("legalize-kr 미러를 찾을 수 없습니다. LEGALIZE_KR_PATH 를 설정하거나 `python legal.py setup` 을 실행하세요.")
        return 2
    cmd = argv[0]

    def with_kind(name: str, kind: Optional[str]) -> str:
        return name if not kind or kind == "법률" else f"{name} {kind}"

    if cmd == "get" and len(argv) >= 3:
        res = m.resolve(with_kind(argv[1], argv[3] if len(argv) > 3 else None))
        if res.status != "ok":
            print(f"법령을 찾을 수 없습니다: {argv[1]} (후보: {', '.join(res.candidates) or '없음'})")
            return 1
        jo, sub = parse_article_no(argv[2])
        a = m.find_article(res.doc, jo, sub)
        if not a:
            print(f"해당 조문을 찾을 수 없습니다: {res.doc.title} {argv[2]}")
            return 1
        print(f"[{res.doc.title} {a.label} ({a.title})]  파일: {res.doc.rel}")
        print(f"공포: {iso(res.doc.promulgated)} | 시행: {iso(res.doc.effective)}"
              + (" | ⚠ 시행예정 판본" if res.doc.effective and res.doc.effective > today() else ""))
        if a.deleted:
            print("⚠ 삭제된 조문")
        print("-" * 50)
        print(a.heading)
        print(a.text)
        return 0
    if cmd == "search" and len(argv) >= 2:
        law = argv[2] if len(argv) > 2 and argv[2] != "all" else None
        limit = int(argv[3]) if len(argv) > 3 else 10
        hits = m.search(argv[1], law, limit)
        print(f"검색 결과: '{argv[1]}' (총 {len(hits)}건)")
        for h in hits:
            loc = f"{h['article']}" if h["article"] else f"L{h['line']}"
            print(f"- [{h['title']} / {h['kind']} {loc}] {h['text']}")
        return 0
    if cmd == "list" and len(argv) >= 2:
        res = m.resolve(with_kind(argv[1], argv[2] if len(argv) > 2 else None))
        if res.status != "ok":
            print(f"법령을 찾을 수 없습니다: {argv[1]}")
            return 1
        arts = m.articles(res.doc)
        print(f"[{res.doc.title}] 조문 수: {len(arts)} (삭제 {sum(a.deleted for a in arts)})")
        for a in arts[:60]:
            print(f"  {a.label} ({a.title})" + (" [삭제]" if a.deleted else ""))
        if len(arts) > 60:
            print(f"  ... 외 {len(arts) - 60}개")
        return 0
    if cmd == "history" and len(argv) >= 2:
        print(json.dumps(m.history(argv[1]), ensure_ascii=False, indent=2))
        return 0
    if cmd == "delegated" and len(argv) >= 3:
        jo, sub = parse_article_no(argv[2])
        print(json.dumps(m.delegated(argv[1], jo, sub), ensure_ascii=False, indent=2))
        return 0
    print("알 수 없는 명령 또는 인자 부족. --help 참조")
    return 2


if __name__ == "__main__":
    sys.exit(cli())
