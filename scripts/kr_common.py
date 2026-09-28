"""kr_common.py - legal-ultra 공용 유틸리티 (표준 라이브러리만 사용).

- 법령명 정규화 (legalize-kr 디렉터리 규칙: 공백 제거, 가운뎃점 → ㆍ)
- YAML 프론트매터 최소 파서 (날짜를 문자열로 유지 — PyYAML 의 date 객체 변환 함정 회피)
- 날짜 파싱/정규화, 원문자 항 번호(①…㊿) 변환, 개정 표시(<개정 …>, [본조신설 …]) 날짜 추출
- CLI 출력 인코딩 보정 (한국어 Windows 파이프의 cp949 에서 UnicodeEncodeError 방지)
"""

from __future__ import annotations

import datetime as _dt
import html
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 법령명 정규화
# ---------------------------------------------------------------------------

# legalize-kr 는 제목의 U+00B7(·)을 U+318D(ㆍ)로 정규화한다(README "Unicode 정규화").
# 사람이 입력하는 가운뎃점 변형(‧ ・ • ･ ∙ ⋅)도 같은 값으로 모은다.
_INTERPUNCT = "·‧・•･∙⋅"
_BRACKETS = "「」『』《》〈〉\"'“”‘’"


def normalize_law_name(name: str) -> str:
    """공백·인용부호 제거 + 가운뎃점 통일. legalize-kr 디렉터리명과 같은 규칙."""
    s = (name or "").strip()
    for ch in _BRACKETS:
        s = s.replace(ch, "")
    for ch in _INTERPUNCT:
        s = s.replace(ch, "ㆍ")
    return re.sub(r"\s+", "", s)


# ---------------------------------------------------------------------------
# 날짜
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def parse_date(value) -> Optional[_dt.date]:
    """'2026-06-09', '20260609', '2026.06.09', '2026. 6. 9.', '2026년 6월 9일' → date."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    s = str(value).strip()
    if re.fullmatch(r"\d{8}", s):
        try:
            return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    m = _DATE_RE.search(s)
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def iso(d: Optional[_dt.date]) -> str:
    return d.isoformat() if d else ""


def today() -> _dt.date:
    return _dt.date.today()


# ---------------------------------------------------------------------------
# YAML 프론트매터 (최소 파서)
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> Tuple[str, str]:
    """(frontmatter_text, body). 프론트매터가 없으면 ('', text)."""
    if not text.startswith("---"):
        return "", text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return "", text


def _scalar(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
        v = v.replace("''", "'")
    return v


def parse_frontmatter_text(fm: str) -> Dict[str, object]:
    """최상위 스칼라와 스칼라 리스트만 읽는다(날짜는 문자열 그대로).

    legalize-kr/precedent-kr 프론트매터 형식:
        제목: 민법
        소관부처:
        - 법무부
        첨부파일:
        - 별표번호: '0000'      ← 딕셔너리 리스트는 개수만 센다
          제목: ...
    """
    meta: Dict[str, object] = {}
    current: Optional[str] = None
    for raw in fm.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith((" ", "-")) and ":" in raw:
            k, v = raw.split(":", 1)
            k = k.strip()
            v = v.strip()
            if v == "":
                meta[k] = []
                current = k
            elif v == "[]":
                meta[k] = []
                current = None
            else:
                meta[k] = _scalar(v)
                current = None
            continue
        if current is None:
            continue
        stripped = raw.strip()
        if stripped.startswith("- "):
            item = stripped[2:]
            lst = meta.setdefault(current, [])
            if not isinstance(lst, list):
                continue
            # "- key: value" 는 딕셔너리 항목의 시작 → 개수만 기록
            if re.match(r"^[^:'\"]+:\s", item + " ") and not item.startswith("http"):
                lst.append({"_dict": True})
            else:
                lst.append(_scalar(item))
    return meta


def read_frontmatter(path: Path, max_bytes: int = 2_000_000) -> Dict[str, object]:
    """파일 앞부분에서 프론트매터만 스트리밍으로 읽는다(고정 2KB 절단 없음)."""
    lines: List[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            first = fp.readline()
            if first.rstrip() != "---":
                return {}
            size = len(first)
            for line in fp:
                size += len(line)
                if line.rstrip() == "---":
                    break
                lines.append(line.rstrip("\n"))
                if size > max_bytes:
                    break
    except OSError:
        return {}
    return parse_frontmatter_text("\n".join(lines))


# ---------------------------------------------------------------------------
# 원문자 항 번호
# ---------------------------------------------------------------------------


def _build_circled() -> Dict[str, int]:
    m: Dict[str, int] = {}
    for i in range(20):  # ①(U+2460)…⑳(U+2473)
        m[chr(0x2460 + i)] = i + 1
    for i in range(15):  # ㉑(U+3251)…㉟(U+325F)
        m[chr(0x3251 + i)] = 21 + i
    for i in range(15):  # ㊱(U+32B1)…㊿(U+32BF)
        m[chr(0x32B1 + i)] = 36 + i
    return m


CIRCLED = _build_circled()
CIRCLED_CHARS = "".join(CIRCLED.keys())


# ---------------------------------------------------------------------------
# 개정 표시
# ---------------------------------------------------------------------------

# <개정 2012.2.1, 2026.6.9>, <신설 2026.6.9>, <전문개정 2009.5.21>, [본조신설 2020.2.4], 삭제 <2020.2.4>
_MARKER_RE = re.compile(
    r"[<\[]\s*(개정|신설|전문개정|제목개정|본조신설|본조개정|타법개정|종전[^>\]]*?)\s+([^>\]]+)[>\]]"
)
_DELETE_RE = re.compile(r"삭제\s*<([^>]+)>")
_MARKER_DATE_RE = re.compile(r"(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})")


def amendment_markers(text: str) -> List[Tuple[str, _dt.date]]:
    """텍스트 안의 개정 표시를 (종류, 날짜) 목록으로. 삭제 표시는 ('삭제', 날짜)."""
    out: List[Tuple[str, _dt.date]] = []
    for m in _MARKER_RE.finditer(text):
        kind = m.group(1)
        for i, d in enumerate(_MARKER_DATE_RE.finditer(m.group(2))):
            try:
                dt = _dt.date(int(d.group(1)), int(d.group(2)), int(d.group(3)))
            except ValueError:
                continue
            # '<신설 2020.3.31, 2026.6.9>' — 첫 날짜만 신설이고 뒤 날짜는 그 뒤의 개정이다
            out.append((kind if (i == 0 or kind not in ("신설", "본조신설")) else "개정", dt))
    for m in _DELETE_RE.finditer(text):
        for d in _MARKER_DATE_RE.finditer(m.group(1)):
            try:
                out.append(("삭제", _dt.date(int(d.group(1)), int(d.group(2)), int(d.group(3)))))
            except ValueError:
                pass
    return out


# ---------------------------------------------------------------------------
# 텍스트 정규화 (인용문 대조용)
# ---------------------------------------------------------------------------

_MD_NOISE_RE = re.compile(r"\*\*|\\(?=[.\-*_()\[\]])")
_MARKER_STRIP_RE = re.compile(r"[<\[](?:개정|신설|전문개정|제목개정|본조신설|본조개정|타법개정|종전)[^>\]]*[>\]]")
_HTML_TAG_RE = re.compile(r"</?(?:br|p|div|span|img|b|i|u|font|table|tr|td|th|tbody|thead)\b[^>]*>", re.I)


def normalize_for_match(text: str) -> str:
    """인용문 대조용 정규화: 마크다운/HTML 잡음·개정표시·공백·따옴표 차이를 제거."""
    s = text or ""
    s = _HTML_TAG_RE.sub(" ", s)
    s = _MD_NOISE_RE.sub("", s)
    s = _MARKER_STRIP_RE.sub("", s)
    for ch in _INTERPUNCT:
        s = s.replace(ch, "ㆍ")
    for a, b in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"), ("「", ""), ("」", ""),
                 ("『", ""), ("』", ""), ("〈", "<"), ("〉", ">")):
        s = s.replace(a, b)
    s = s.replace('"', "").replace("'", "")
    return re.sub(r"\s+", "", s)


# 생략(…)으로 건너뛴 구간에 이런 말이 있으면 뜻이 뒤집힐 수 있다('…있어야 하는 것은 아니지만 …' → '…있어야 하는 … 것이다')
_NEGATION_RE = re.compile(r"아니|않|못|없|제외|금지|불가")
_ELLIPSIS_RE = re.compile(r"\.{3,}|…|⋯|\(중략\)|\[중략\]|\(생략\)|\[생략\]")


def quote_found(quote: str, source_text: str, max_gap: int = 300) -> bool:
    """인용문이 원문에 있는지. 말줄임(…, ..., (중략))은 허용하되 남용을 막는다:
    - 조각마다 정규화 6자 이상(짧은 조각을 흩뿌려 아무 문장이나 맞추는 것 차단)
    - 조각 사이 생략 구간은 정규화 300자 이하
    - 생략 구간에 부정·제외어(아니·않·못·없·제외·금지·불가)가 있으면 불인정"""
    src = normalize_for_match(source_text)
    parts = [normalize_for_match(p) for p in _ELLIPSIS_RE.split(quote or "")]
    parts = [p for p in parts if p]
    if not parts or any(len(p) < 6 for p in parts):
        return False

    failed = set()      # (조각 번호, 시작 위치) — 이미 실패한 상태는 다시 풀지 않는다(반복 원문에서 지수 시간 방지)

    def match_from(k: int, prev_end: int) -> bool:
        if k == len(parts):
            return True
        if (k, prev_end) in failed:
            return False
        p = parts[k]
        idx = src.find(p, prev_end)
        while idx >= 0:
            if idx - prev_end > max_gap:
                break
            if not _NEGATION_RE.search(src, prev_end, idx) and match_from(k + 1, idx + len(p)):
                return True
            idx = src.find(p, idx + 1)
        failed.add((k, prev_end))
        return False

    idx = src.find(parts[0])
    while idx >= 0:          # 첫 조각이 여러 번 나오면 각 위치에서 시도한다
        if match_from(1, idx + len(parts[0])):
            return True
        idx = src.find(parts[0], idx + 1)
    return False


_ZW_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
_SPACES_RE = re.compile("[\u00a0\u2000-\u200a\u202f\u205f\u3000]")
_FW_DIGITS = {ord("０") + i: ord("0") + i for i in range(10)}


_HTML_COMMENT_RE = re.compile(r"<!--.*?(?:-->|$)", re.S)
_HTML_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>|<\s*/\s*(?:p|div|li|tr|h[1-6])\s*>", re.I)
_HTML_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]{0,300})?/?\s*>")
_MD_UNDERSCORE_RE = re.compile(r"(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])")


def prep_text(text: str) -> str:
    """인용 추출 전 정리 — 검증기가 읽는 글자와 독자가 보는 글자를 맞춘다:
    HTML 주석(보이지 않음) 삭제, HTML 태그 제거·엔티티 해독, 마크다운 강조(*, _, ~~, `) 제거, 폭 없는 문자 제거,
    특수 공백 → 공백, 전각 숫자 → 반각. (‘민법 제<!-- -->1200조’·‘민법 제*1200*조’가 화면에는 조문으로 보이는데 추출되지 않던 문제,
    주석 속 ‘(이하 "근로기준법"이라 한다)’ 정의가 적용되던 문제, ‘근로기준법 제76조의<ZWSP>9’가 제76조로 읽히던 문제)"""
    t = (text or "").replace("\r\n", "\n")
    t = _HTML_COMMENT_RE.sub("", t)
    t = _HTML_BREAK_RE.sub("\n", t)
    t = _HTML_TAG_RE.sub("", t)
    t = html.unescape(t)
    t = _ZW_RE.sub("", t)
    t = _SPACES_RE.sub(" ", t)
    t = t.translate(_FW_DIGITS)
    t = t.replace("**", "").replace("~~", "").replace("`", "").replace("*", "")
    t = _MD_UNDERSCORE_RE.sub("", t)
    return t


# ---------------------------------------------------------------------------
# 출력 인코딩
# ---------------------------------------------------------------------------


def configure_stdout() -> None:
    """한국어 Windows 에서 출력이 파이프로 잡히면 cp949 가 되어 이모지/일부 문자에서
    UnicodeEncodeError 가 난다(v1 law_cli.py 에서 재현). UTF-8 로 재설정한다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def env_path(*names: str) -> Optional[Path]:
    for n in names:
        v = os.environ.get(n)
        if v:
            return Path(v).expanduser()
    return None


def cache_root() -> Path:
    base = os.environ.get("LEGAL_ULTRA_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache"), "legal-ultra")
    return Path(base)


def chunks(seq: Iterable, n: int):
    buf = []
    for x in seq:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf
