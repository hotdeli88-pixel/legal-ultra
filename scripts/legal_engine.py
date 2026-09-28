"""legal_engine.py - 대한민국 법률 초고속 로컬 검색 & 조문 슬라이싱 엔진.

legalize-kr Git 저장소(kr/ 디렉토리 내 마크다운 파일)를 기반으로
법령 검색, 특정 조문 추출, 위임 조문 추적, 개정 이력 대조를 밀리초 단위로 수행합니다.
"""

import os
import re
import sys
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# 기본 경로 탐색
DEFAULT_PATHS = [
    os.environ.get("LEGALIZE_KR_PATH"),
    os.path.join(os.getcwd(), "legalize-kr", "kr"),
    os.path.join(os.getcwd(), "kr"),
    r"C:\Users\sdm24\OneDrive\바탕 화면\새 폴더 (6)\legalize-kr\kr",
]

def get_kr_root() -> Path:
    for p in DEFAULT_PATHS:
        if p and os.path.isdir(p):
            return Path(p).resolve()
    raise FileNotFoundError("legalize-kr/kr 디렉토리를 찾을 수 없습니다. LEGALIZE_KR_PATH 환경변수를 설정하세요.")

def normalize_name(name: str) -> str:
    """공백 및 기호 정규화 (법제처 및 legalize-kr 규칙)."""
    s = re.sub(r"\s+", "", name)
    s = s.replace("·", "ㆍ")
    return s

def parse_yaml_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """YAML 프론트매터와 본문 분리."""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            meta_text = parts[1]
            body = parts[2]
            try:
                import yaml
                meta = yaml.safe_load(meta_text)
                if isinstance(meta, dict):
                    return meta, body
            except Exception:
                pass
            meta = {}
            for line in meta_text.splitlines():
                if ":" in line and not line.startswith(" "):
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip("'\"")
            return meta, body
    return {}, content

class LegalEngine:
    def __init__(self, kr_root: Optional[str] = None):
        self.kr_root = Path(kr_root).resolve() if kr_root else get_kr_root()
        self.repo_root = self.kr_root.parent
        self._law_dirs_cache = None

    @property
    def law_dirs(self) -> List[str]:
        if self._law_dirs_cache is None:
            self._law_dirs_cache = [d.name for d in self.kr_root.iterdir() if d.is_dir()]
        return self._law_dirs_cache

    def resolve_law_dir(self, query: str) -> Optional[Path]:
        norm = normalize_name(query)
        # 1. 완전 일치
        direct = self.kr_root / norm
        if direct.is_dir():
            return direct
        # 2. 대소문자/정규화 일치 검색
        for d in self.law_dirs:
            if normalize_name(d) == norm:
                return self.kr_root / d
        # 3. 접두사/부분 일치
        matches = [d for d in self.law_dirs if norm in normalize_name(d)]
        if matches:
            # 가장 길이가 짧은 것(더 대표적인 것) 우선
            matches.sort(key=len)
            return self.kr_root / matches[0]
        return None

    def get_law_file(self, law_dir: Path, doc_type: str = "법률") -> Optional[Path]:
        """법률, 시행령, 시행규칙 파일 중 최신본 반환."""
        candidates = []
        for f in law_dir.iterdir():
            if not f.name.endswith(".md"):
                continue
            name_lower = f.stem.lower()
            if doc_type in name_lower or (doc_type == "법률" and "법률" in name_lower):
                candidates.append(f)

        if not candidates:
            # fallback
            md_files = list(law_dir.glob("*.md"))
            if md_files:
                return md_files[0]
            return None

        if len(candidates) == 1:
            return candidates[0]

        # 여러 후보 중 공포일자/시행일자가 가장 최신인 파일 선택
        best_file = None
        best_date = ""
        for c in candidates:
            try:
                with open(c, "r", encoding="utf-8") as fp:
                    meta, _ = parse_yaml_frontmatter(fp.read(2048))
                date = meta.get("공포일자", "") or meta.get("시행일자", "")
                if date >= best_date:
                    best_date = date
                    best_file = c
            except Exception:
                pass
        return best_file or candidates[0]

    def parse_article_no(self, query: str) -> Tuple[int, Optional[int]]:
        """'제23조의2', '23-2', '23조', '23' 등을 (23, 2) 형태로 파싱."""
        q = query.strip()
        m = re.search(r"(?:제)?(\d+)(?:조)?(?:의(\d+))?", q)
        if m:
            main_no = int(m.group(1))
            sub_no = int(m.group(2)) if m.group(2) else None
            return main_no, sub_no
        raise ValueError(f"유효하지 않은 조문 형식입니다: {query}")

    def list_articles(self, law_name: str, doc_type: str = "법률") -> List[Dict[str, Any]]:
        """해당 법령의 전체 조문 목록(조문번호, 제목, 줄번호) 반환."""
        law_dir = self.resolve_law_dir(law_name)
        if not law_dir:
            return []
        fpath = self.get_law_file(law_dir, doc_type)
        if not fpath:
            return []

        with open(fpath, "r", encoding="utf-8") as fp:
            content = fp.read()

        meta, body = parse_yaml_frontmatter(content)
        pattern = re.compile(r"^#####\s*제(\d+)조(?:의(\d+))?(?:\s*\((.*?)\))?.*$", re.MULTILINE)
        articles = []
        for m in pattern.finditer(body):
            main_no = int(m.group(1))
            sub_no = int(m.group(2)) if m.group(2) else None
            title = m.group(3) or ""
            art_str = f"제{main_no}조" + (f"의{sub_no}" if sub_no else "")
            articles.append({
                "article_no": art_str,
                "main": main_no,
                "sub": sub_no,
                "title": title.strip(),
                "heading": m.group(0).strip(),
                "offset": m.start()
            })
        return articles

    def get_article(self, law_name: str, article_query: str, doc_type: str = "법률") -> Optional[Dict[str, Any]]:
        """특정 조문의 전문과 메타데이터 반환."""
        law_dir = self.resolve_law_dir(law_name)
        if not law_dir:
            return None
        fpath = self.get_law_file(law_dir, doc_type)
        if not fpath:
            return None

        with open(fpath, "r", encoding="utf-8") as fp:
            raw_content = fp.read()

        meta, body = parse_yaml_frontmatter(raw_content)
        target_main, target_sub = self.parse_article_no(article_query)

        pattern = re.compile(r"^#####\s*제(\d+)조(?:의(\d+))?(?:\s*\((.*?)\))?.*$", re.MULTILINE)
        matches = list(pattern.finditer(body))

        for i, m in enumerate(matches):
            main_no = int(m.group(1))
            sub_no = int(m.group(2)) if m.group(2) else None
            if main_no == target_main and sub_no == target_sub:
                start_pos = m.start()
                end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(body)
                article_text = body[start_pos:end_pos].strip()
                art_str = f"제{main_no}조" + (f"의{sub_no}" if sub_no else "")
                title = m.group(3) or ""
                return {
                    "law_name": meta.get("제목", law_dir.name),
                    "doc_type": doc_type,
                    "promulgation_date": meta.get("공포일자"),
                    "enforcement_date": meta.get("시행일자"),
                    "article_no": art_str,
                    "title": title.strip(),
                    "content": article_text,
                    "file_path": str(fpath),
                }
        return None

    def search_keyword(self, keyword: str, law_filter: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """초고속 git grep 기반 키워드 검색."""
        cmd = ["git", "-C", str(self.repo_root), "grep", "-n", "-i", "-m", str(limit * 2), "--", keyword]
        if law_filter:
            norm_filter = normalize_name(law_filter)
            cmd.extend([f"kr/{norm_filter}/*"])
        else:
            cmd.extend(["kr/*"])

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            lines = res.stdout.splitlines()
        except Exception as e:
            return [{"error": str(e)}]

        results = []
        for line in lines:
            if not line.strip():
                continue
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            rel_path, line_no, text = parts[0], parts[1], parts[2]
            # 법률명 추출
            path_parts = Path(rel_path).parts
            law_name = path_parts[1] if len(path_parts) > 1 else ""
            doc_type = Path(rel_path).stem

            results.append({
                "law_name": law_name,
                "doc_type": doc_type,
                "line": int(line_no),
                "text": text.strip(),
                "file_path": str(self.repo_root / rel_path)
            })
            if len(results) >= limit:
                break
        return results

    def get_delegated(self, law_name: str, article_query: str) -> List[Dict[str, Any]]:
        """조문 내 위임 문구(대통령령, 시행령, 고용노동부령 등)를 탐색하여 하위 규정 조문 연계."""
        art = self.get_article(law_name, article_query, doc_type="법률")
        if not art:
            return []

        content = art["content"]
        delegated_items = []

        # 위임 표현 검출
        pres_patterns = [r"대통령령으로 정하는", r"대통령령이 정하는", r"시행령으로 정하는"]
        ord_patterns = [r"([가-힣]+부령)으로 정하는", r"시행규칙으로 정하는"]

        has_pres = any(re.search(p, content) for p in pres_patterns)
        has_ord = any(re.search(p, content) for p in ord_patterns)

        law_dir = self.resolve_law_dir(law_name)
        if not law_dir:
            return []

        target_main = art["article_no"]

        if has_pres:
            pres_file = self.get_law_file(law_dir, "시행령")
            if pres_file and pres_file.is_file():
                # 시행령 내에서 모법 조문 언급 탐색
                with open(pres_file, "r", encoding="utf-8") as fp:
                    p_body = fp.read()
                matches = re.findall(rf"(#####\s*제\d+조(?:의\d+)?\s*\([^)]*\).*?(?:법\s*제{art['article_no'].replace('제','').replace('조','')}.*?))(?=#####|\Z)", p_body, re.DOTALL)
                for m in matches:
                    delegated_items.append({
                        "type": "시행령",
                        "content": m.strip()[:1000]
                    })

        if has_ord:
            ord_file = self.get_law_file(law_dir, "시행규칙")
            if ord_file and ord_file.is_file():
                with open(ord_file, "r", encoding="utf-8") as fp:
                    o_body = fp.read()
                matches = re.findall(rf"(#####\s*제\d+조(?:의\d+)?\s*\([^)]*\).*?(?:법\s*제{art['article_no'].replace('제','').replace('조','')}.*?))(?=#####|\Z)", o_body, re.DOTALL)
                for m in matches:
                    delegated_items.append({
                        "type": "시행규칙",
                        "content": m.strip()[:1000]
                    })

        return delegated_items

    def diff_history(self, law_name: str, count: int = 5) -> List[Dict[str, str]]:
        """Git 커밋 히스토리를 통해 해당 법률의 개정 이력 조회."""
        law_dir = self.resolve_law_dir(law_name)
        if not law_dir:
            return []
        rel_dir = law_dir.relative_to(self.repo_root)

        cmd = [
            "git", "-C", str(self.repo_root), "log", f"-n{count}",
            "--date=short", "--pretty=format:%h|%ad|%an|%s", "--", str(rel_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        history = []
        for line in res.stdout.splitlines():
            if "|" in line:
                h, d, a, s = line.split("|", 3)
                history.append({"hash": h, "date": d, "author": a, "subject": s})
        return history

def cli():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage:")
        print("  python legal_engine.py get <법률명> <조문번호> [법률|시행령|시행규칙]")
        print("  python legal_engine.py search <키워드> [법률명필터] [건수]")
        print("  python legal_engine.py list <법률명> [법률|시행령|시행규칙]")
        print("  python legal_engine.py history <법률명>")
        print("  python legal_engine.py delegated <법률명> <조문번호>")
        sys.exit(0)

    engine = LegalEngine()
    cmd = sys.argv[1]

    if cmd == "get":
        if len(sys.argv) < 4:
            print("인자 필요: <법률명> <조문번호> [구분]")
            sys.exit(1)
        law_name = sys.argv[2]
        art_no = sys.argv[3]
        doc_type = sys.argv[4] if len(sys.argv) > 4 else "법률"
        art = engine.get_article(law_name, art_no, doc_type)
        if art:
            print(f"[{art['law_name']} {art['article_no']} ({art['title']})]")
            print(f"공포: {art['promulgation_date']} | 시행: {art['enforcement_date']}")
            print("-" * 50)
            print(art["content"])
        else:
            print(f"해당 조문을 찾을 수 없습니다: {law_name} {art_no}")

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("인자 필요: <키워드> [법률명필터] [건수]")
            sys.exit(1)
        kw = sys.argv[2]
        lfilter = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "all" else None
        limit = int(sys.argv[4]) if len(sys.argv) > 4 else 10
        hits = engine.search_keyword(kw, lfilter, limit)
        print(f"검색 결과: '{kw}' (총 {len(hits)}건)")
        for h in hits:
            print(f"- [{h['law_name']}/{h['doc_type']} L{h['line']}]: {h['text']}")

    elif cmd == "list":
        if len(sys.argv) < 3:
            print("인자 필요: <법률명> [구분]")
            sys.exit(1)
        law_name = sys.argv[2]
        doc_type = sys.argv[3] if len(sys.argv) > 3 else "법률"
        arts = engine.list_articles(law_name, doc_type)
        print(f"[{law_name}] 전체 조문 수: {len(arts)}")
        for a in arts[:30]:
            print(f"  {a['article_no']} ({a['title']})")
        if len(arts) > 30:
            print(f"  ... 외 {len(arts)-30}개 조문")

    elif cmd == "history":
        if len(sys.argv) < 3:
            print("인자 필요: <법률명>")
            sys.exit(1)
        law_name = sys.argv[2]
        hist = engine.diff_history(law_name)
        print(f"[{law_name}] 최근 개정 이력:")
        for h in hist:
            print(f"  {h['hash']} | {h['date']} | {h['subject']}")

    elif cmd == "delegated":
        if len(sys.argv) < 4:
            print("인자 필요: <법률명> <조문번호>")
            sys.exit(1)
        law_name = sys.argv[2]
        art_no = sys.argv[3]
        dels = engine.get_delegated(law_name, art_no)
        print(f"[{law_name} {art_no}] 위임 규정 연계 (총 {len(dels)}건):")
        for d in dels:
            print(f"[{d['type']}]\n{d['content']}\n" + "-" * 40)

if __name__ == "__main__":
    cli()
