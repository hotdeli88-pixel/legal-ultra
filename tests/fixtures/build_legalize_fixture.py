"""테스트 픽스처 생성기 — 실제 legalize-kr 전체 이력 클론에서 필요한 판본만 발췌한다.

    python3 tests/fixtures/build_legalize_fixture.py <legalize-kr 전체 이력 클론 경로>

만드는 것
  tests/fixtures/legalize-kr-history/manifest.json  판본 커밋 목록(공포일 순) — 테스트가 임시 git 저장소로 재생한다
  tests/fixtures/legalize-kr-history/versions/…     판본별 발췌본
  tests/fixtures/legalize-kr/kr/…                   각 파일의 최신 판본(이력 없는 미러 모드 테스트용, 위와 같은 발췌본)

발췌 규칙(검증 로직이 쓰는 정보는 그대로 둔다)
  - 프론트매터·제목·장/절/관 제목줄은 전부 유지(개정표시 <신설 …> 포함)
  - 조문은 KEEP 에 적은 것만 원문 그대로 유지
  - 부칙은 (1) 첫 블록(현행 법문의 출발점) (2) 그 판본 자신의 공포일 블록 (3) 남긴 조문·제목의 개정표시 날짜의 블록만 남기고,
    각 블록은 시행일 조문(제1조 또는 단일 조문)까지만 둔다 — 엔진이 읽는 것은 본칙 시행일과 '다만, …' 단서뿐이다
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "scripts"))
from kr_common import amendment_markers, parse_date, parse_frontmatter_text, split_frontmatter  # noqa: E402

# 파일 → (남길 조문 또는 None=통째로, 이 날짜보다 오래된 판본 하나까지 포함(None=최신본만), 최대 판본 수)
KEEP = {
    "kr/근로기준법/법률(법률).md": ({(1, None), (2, None), (23, None), (44, 4), (60, None), (61, None), (76, None), (76, 2),
                                  (76, 3), (102, 2), (116, None)}, "2017-11-01", 30),
    "kr/근로기준법/법률.md": (None, None, 1),                    # 1997년 폐지 법률(동명 문서) — 통째로
    "kr/근로기준법/시행령.md": ({(1, None), (7, 2), (30, None), (33, None)}, "2025-03-01", 2),
    "kr/민법/법률.md": ({(1, None), (2, None), (750, None), (751, None), (766, None), (1112, None)}, "2023-05-01", 10),
    "kr/형법/법률.md": ({(1, None), (78, None), (250, None)}, "2023-08-01", 10),
    "kr/개인정보보호법/법률.md": ({(1, None), (2, None), (8, None), (15, None), (17, None), (28, 2), (28, 12), (28, 13),
                                 (75, None), (76, None)}, "2017-04-01", 10),
    "kr/개인정보보호법/시행령.md": ({(1, None), (14, 2)}, None, 1),
    "kr/도로교통법/법률.md": ({(1, None), (137, None), (148, 2)}, "2025-01-15", 10),
    "kr/중대재해처벌등에관한법률/법률.md": ({(1, None), (2, None), (4, None), (6, None)}, None, 1),
    "kr/상법/법률.md": ({(1, None), (24, None)}, "2024-09-01", 10),
    "kr/주택임대차보호법/법률.md": ({(1, None), (6, 3)}, None, 1),
}
_ART = re.compile(r"^#####\s*제(\d+)조(?:의(\d+))?")
_BUCHIK = re.compile(r"^부칙\s*(?:\([^)]*\))?\s*<[^>]*?(\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2})\s*\.?\s*>")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def trim(text: str, keep) -> str:
    if keep is None:
        return text
    fm, body = split_frontmatter(text)
    prom = parse_date(parse_frontmatter_text(fm).get("공포일자"))
    out, cur_keep, in_buchik, seen_article = [], False, False, False
    buchik_blocks = []
    for line in body.split("\n"):
        if in_buchik:
            m = _BUCHIK.match(line)
            if m:
                buchik_blocks.append([parse_date(m.group(1)), [line]])
            elif buchik_blocks:
                buchik_blocks[-1][1].append(line)
            continue
        if line.startswith("## 부칙"):
            in_buchik = True
            out += ["", line, ""]
            continue
        m = _ART.match(line)
        if m:
            seen_article = True
            cur_keep = (int(m.group(1)), int(m.group(2)) if m.group(2) else None) in keep
            if cur_keep:
                out.append(line)
            continue
        if line.startswith("#"):          # 장·절·관 제목줄은 전부 유지(개정표시가 판정에 쓰인다)
            cur_keep = False
            out += [line, ""]
            continue
        if cur_keep or not seen_article:
            out.append(line)
    kept_text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip("\n")
    dates = {d for _, d in amendment_markers(kept_text)}
    keep_blocks = []
    for i, (d, lines) in enumerate(buchik_blocks):
        if i == 0 or d == prom or d in dates:
            cut = []
            for ln in lines:
                if cut and re.match(r"^\s*제\s*2\s*조", ln):
                    break                    # 시행일 조문(제1조)까지만
                cut.append(ln)
            keep_blocks.append("\n".join(cut).strip("\n"))
    return f"---\n{fm}\n---\n{kept_text}\n\n" + "\n\n".join(keep_blocks) + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1]).resolve()
    hist = HERE / "legalize-kr-history"
    head = HERE / "legalize-kr"
    for d in (hist, head):
        if d.exists():
            shutil.rmtree(d)
    entries = []
    for rel, (keep, since, max_n) in KEEP.items():
        log = git(src, "log", "--format=%H|%s", "--", rel).splitlines()
        rows = []
        for line in log:
            h, subj = line.split("|", 1)
            text = git(src, "show", f"{h}:{rel}")
            meta = parse_frontmatter_text(split_frontmatter(text)[0])
            prom = str(meta.get("공포일자"))
            rows.append((h, subj, prom, text))
            if since is None or prom < since or len(rows) >= max_n:
                break                         # since 보다 오래된 판본 하나까지(판정 멈춤 조건 충족용)
        for k, (h, subj, prom, text) in enumerate(reversed(rows)):
            snap = Path("versions") / rel.replace("kr/", "", 1).replace(".md", "") / f"{prom.replace('-', '')}-{k:02d}.md"
            (hist / snap).parent.mkdir(parents=True, exist_ok=True)
            (hist / snap).write_text(trim(text, keep), encoding="utf-8")
            entries.append({"path": rel, "snapshot": snap.as_posix(), "date": prom, "subject": subj, "source_commit": h[:12]})
        latest = hist / entries[-1]["snapshot"]
        (head / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(latest, head / rel)
    entries.sort(key=lambda e: (e["date"], e["path"], e["snapshot"]))
    src_head = git(src, "log", "-1", "--format=%H %cd", "--date=short").strip()
    (hist / "manifest.json").write_text(json.dumps({"source": "https://github.com/legalize-kr/legalize-kr", "source_head": src_head,
                                                    "commits": entries}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(entries)} 판본, 최신본 {len(KEEP)} 파일")
    return 0


if __name__ == "__main__":
    sys.exit(main())
