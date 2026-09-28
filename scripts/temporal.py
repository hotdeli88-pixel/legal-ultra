"""temporal.py - 기준일(as-of)에 시행 중인 조문 판정 (legal-ultra v2).

legalize-kr 의 각 커밋은 '공포일 기준' 판본이고, 판본의 시행일자는 단조 증가하지 않는다(실측: 근로기준법
2026-02-19 판본 시행 2026-08-20, 03-17 판본 2027-01-01, 04-07 판본 2026-10-08, 06-09 판본 2027-06-10).
또 한 판본 안에서도 부칙 단서로 조항별 시행일이 다르다(예: 2026.4.7. 부칙 — 제44조의4 는 2027-01-01).
그래서 판정은 '조·항·호 단위'로 한다:

  정밀 모드(전체 이력 git): 인용 단위의 텍스트가 바뀐 판본들을 찾고, 각 변경의 (조항별) 시행일이 기준일 이전인
    가장 최근 변경의 텍스트를 시행 중 텍스트로 본다. 변경 시행일은 판본 프론트매터 시행일자 + 부칙 단서 예외.
  이력 없음(얕은 클론·zip): 인용 단위에 걸린 개정표시(<개정>·<신설>·삭제·[본조신설], 장·절 제목의 신설 표시)와
    부칙 단서가 모두 기준일 이전 시행으로 확인될 때만 '시행 중'(최신 공포본 문언 = 기준일 문언). 법제처 원문은 항 번호가
    밀린 항에도 <개정 …>을 붙인다(실측: 근로기준법 2026.6.9. 개정의 제60조⑥⑦⑧). 시행 전 개정표시가 하나라도 있으면
    '확인 불가'(fail-closed). 조(條) 단위 신설이 기준일 이후인 것이 확실하면 '없음'.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from kr_common import CIRCLED_CHARS, amendment_markers, iso, normalize_for_match, parse_date

# ---------------------------------------------------------------------------
# 기간 계산 (민법 제157조 초일불산입, 제160조 월·연 계산)
# ---------------------------------------------------------------------------


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def after_period(prom: date, n: int, unit: str) -> date:
    """'공포 후 n{일|개월|년}이 경과한 날'. 실측 검증: 2026-06-09 + 1년 → 2027-06-10, 2026-04-07 + 6개월 → 2026-10-08."""
    if unit == "일":
        return prom + timedelta(days=n + 1)
    if unit == "개월":
        return add_months(prom, n) + timedelta(days=1)
    return add_months(prom, 12 * n) + timedelta(days=1)


_WHEN_PERIOD_RE = re.compile(r"공포\s*후\s*(\d+)\s*(일|개월|년)\s*이\s*경과한\s*날")
_WHEN_DATE_RE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def parse_when(expr: str, prom: date, main: Optional[date] = None) -> Optional[date]:
    """시행 시기 표현 → 날짜. 알 수 없으면(대통령령으로 정하는 날 등) None."""
    e = expr.strip()
    if "대통령령" in e or "총리령" in e or "부령" in e or "규칙으로 정하는" in e:
        return None
    m = _WHEN_DATE_RE.search(e)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _WHEN_PERIOD_RE.search(e)
    if m and "속하는" not in e:
        return after_period(prom, int(m.group(1)), m.group(2))
    if re.search(r"공포한\s*날", e):
        return prom
    if re.search(r"(?:이\s*(?:법|영|규칙|령)\s*시행일|같은\s*날)", e):
        return main
    return None


# ---------------------------------------------------------------------------
# 부칙 파싱
# ---------------------------------------------------------------------------

UnitRef = Tuple[int, Optional[int], Optional[int], Optional[str]]   # (조, 조의, 항, 호)


@dataclass
class Exception_:
    units: List[Tuple[UnitRef, Optional[int]]]   # (시작 단위, 범위 끝 조 번호) — 범위가 아니면 None
    effective: Optional[date]
    raw: str


@dataclass
class Buchik:
    promulgated: date
    number: str
    other_law: Optional[str]          # 타법개정 부칙이면 그 법령명
    main_effective: Optional[date]
    main_raw: str
    exceptions: List[Exception_] = field(default_factory=list)
    proviso_omitted: bool = False     # '<단서 생략>' — 조항별 예외를 알 수 없음


_BUCHIK_HEAD_RE = re.compile(
    r"^부칙\s*(?:\((?P<other>[^)]*)\))?\s*<\s*(?:제\s*(?P<no>\d+)\s*호\s*,\s*)?(?P<date>\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2})\s*\.?\s*>",
    re.M)
_MAIN_RE = re.compile(r"이\s*(?:법|영|규칙|령)\s*은\s*(?P<when>[^.]*?)부터\s*시행한다")
_UNIT_TOKEN_RE = re.compile(
    r"제\s*(?P<jo>\d+)\s*조(?:\s*의\s*(?P<sub>\d+))?(?:\s*제\s*(?P<hang>\d+)\s*항)?(?:\s*제\s*(?P<ho>\d+)\s*호(?:\s*의\s*(?P<hosub>\d+))?)?"
    r"|(?P<bare>[ㆍ·,]\s*(?:및\s*)?제\s*(?P<bn>\d+)\s*(?P<bk>항|호)(?:\s*의\s*(?P<bsub>\d+))?)")
_EXC_CLAUSE_RE = re.compile(
    r"(?P<units>.*?)(?:의|에\s*관한)\s*(?:개정\s*)?규정(?:\s*중[^은는]{0,60})?\s*(?:및\s*부칙[^은는]{0,40})?(?:은|는)\s*"
    r"(?P<when>.*?)부터(?:\s*각각)?\s*(?:시행하고|시행하며|시행한다|,)", re.S)


def parse_unit_list(s: str) -> List[Tuple[UnitRef, Optional[int]]]:
    """'제13조, 제102조의2, 제103조부터 제105조까지, 제116조제2항제1호ㆍ제4호' → 단위 목록(범위 포함). 별표·장 제목은 제외."""
    s = re.sub(r"\([^()]*\)", " ", s)
    s = re.sub(r"\[?별표\s*\d*(?:의\d+)?\]?[^,ㆍ및]*", " ", s)
    out: List[Tuple[UnitRef, Optional[int]]] = []
    last: Optional[UnitRef] = None
    prev_end = 0
    for m in _UNIT_TOKEN_RE.finditer(s):
        if m.group("bare"):
            if last is None:
                continue
            n = int(m.group("bn"))
            if m.group("bk") == "항":
                ref = (last[0], last[1], n, None)
            else:
                ho = str(n) + (f"의{m.group('bsub')}" if m.group("bsub") else "")
                ref = (last[0], last[1], last[2], ho)
            out.append((ref, None))
            last = ref
            prev_end = m.end()
            continue
        ho = None
        if m.group("ho"):
            ho = m.group("ho") + (f"의{m.group('hosub')}" if m.group("hosub") else "")
        ref = (int(m.group("jo")), int(m.group("sub")) if m.group("sub") else None,
               int(m.group("hang")) if m.group("hang") else None, ho)
        between = s[prev_end:m.start()]
        if out and re.search(r"부터|내지", between) and out[-1][1] is None and out[-1][0][2] is None and ref[2] is None:
            start = out.pop()[0]
            out.append((start, ref[0]))       # 조 범위(가지조문 포함)
        else:
            out.append((ref, None))
        last = ref
        prev_end = m.end()
    return out


def parse_buchik(body: str) -> List[Buchik]:
    """본문의 '## 부칙' 절에서 부칙 블록들을 읽는다."""
    i = body.find("\n## 부칙")
    if i < 0:
        i = body.find("## 부칙")
        if i < 0:
            return []
    sec = body[i:]
    heads = list(_BUCHIK_HEAD_RE.finditer(sec))
    out: List[Buchik] = []
    for k, h in enumerate(heads):
        prom = parse_date(h.group("date"))
        if not prom:
            continue
        block = sec[h.end(): heads[k + 1].start() if k + 1 < len(heads) else len(sec)]
        flat = re.sub(r"\s+", " ", block)
        b = Buchik(promulgated=prom, number=h.group("no") or "", other_law=h.group("other"),
                   main_effective=None, main_raw="", proviso_omitted="단서 생략" in flat)
        m = _MAIN_RE.search(flat)
        if m:
            b.main_raw = m.group(0)
            b.main_effective = parse_when(m.group("when"), prom)
            tail = flat[m.end(): m.end() + 1500]
            dm = re.match(r"\s*\.?\s*다만\s*,?\s*(?P<exc>.*?시행한다)", tail)
            if dm:
                for cm in _EXC_CLAUSE_RE.finditer(dm.group("exc")):
                    units = parse_unit_list(cm.group("units"))
                    if units:
                        b.exceptions.append(Exception_(units, parse_when(cm.group("when"), prom, b.main_effective),
                                                       cm.group(0).strip()[:300]))
        out.append(b)
    return out


def coverage(units: List[Tuple[UnitRef, Optional[int]]], jo: int, sub: Optional[int], hang: Optional[int],
             ho: Optional[str]) -> str:
    """부칙 단서의 단위 목록이 인용 단위를 덮는 정도: full | partial | none."""
    best = "none"
    for (ujo, usub, uhang, uho), rng_end in units:
        if rng_end is not None:
            if ujo <= jo <= rng_end:
                return "full"
            continue
        if ujo != jo or usub != sub:
            continue
        if uhang is None and uho is None:
            return "full"
        if uhang is not None and hang is None:
            best = "partial"
            continue
        if uhang is not None and uhang != hang:
            continue
        if uho is None:
            return "full"                    # 항 단위 예외가 인용 항(또는 그 호)을 덮음
        if ho is None:
            best = "partial"
            continue
        if uho == ho:
            return "full"
    return best


def unit_effective(b: Buchik, jo: int, sub: Optional[int], hang: Optional[int], ho: Optional[str],
                   frontmatter_eff: Optional[date] = None) -> Tuple[Optional[date], List[str]]:
    """이 부칙(개정)이 인용 단위에 대해 갖는 시행일과 주의사항."""
    notes: List[str] = []
    main = frontmatter_eff or b.main_effective
    for ex in b.exceptions:
        cov = coverage(ex.units, jo, sub, hang, ho)
        if cov == "full":
            return ex.effective, ([f"부칙 단서로 시행일이 따로 정해진 조항({iso(ex.effective) or '시행일 미정'})"])
        if cov == "partial":
            notes.append(f"같은 조의 일부 항·호는 부칙 단서로 시행일이 다름({iso(ex.effective) or '미정'})")
    if b.proviso_omitted:
        notes.append(f"{'타법개정 ' if b.other_law else ''}부칙 단서가 생략되어 있어 조항별 시행일은 확인하지 못함")
    return main, notes


# ---------------------------------------------------------------------------
# 단위 텍스트
# ---------------------------------------------------------------------------

_UNIT_PREFIX_RE = re.compile(r"^\s*(?:\*\*[" + CIRCLED_CHARS + r"]\*\*|[" + CIRCLED_CHARS + r"]|\d+(?:의\d+)?\\?\.|[가-힣]\\?\.)\s*")
_UNIT_DELETED_RE = re.compile(r"^삭제\s*(?:<[^>]*>)?\s*$")


def is_deleted_unit(text: Optional[str]) -> bool:
    if not text:
        return False
    first = text.strip().split("\n")[0]
    return bool(_UNIT_DELETED_RE.match(_UNIT_PREFIX_RE.sub("", first, count=1).strip()))


def unit_signature(text: Optional[str]) -> Optional[str]:
    """판본 간 비교용(개정표시 제거·공백 무시)."""
    if text is None:
        return None
    return normalize_for_match(re.sub(r"<[^>]*(?:개정|신설|삭제)[^>]*>|\[[^\]]*(?:신설|개정)[^\]]*\]", "", text))


@dataclass
class UnitStatus:
    state: str                      # in_force | absent | deleted | unknown
    text: Optional[str] = None      # 기준일 시행 텍스트(확인된 경우)
    title: Optional[str] = None
    precise: bool = False
    version: str = ""               # 근거 판본(커밋 또는 HEAD)
    notes: List[str] = field(default_factory=list)
    head_exists: bool = False       # HEAD(최신 공포본)에 이 단위가 있는가
    pending_change: bool = False    # 최신 공포본의 이 단위 문언이 아직 시행 전인가
    exists: Optional[bool] = None   # (unknown 일 때) 기준일에 이 단위가 있었던 것은 확실한가 — 문언만 모름


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------


def unit_status_precise(commits: List[str], load, as_of: date, jo: int, sub: Optional[int],
                        hang: Optional[int], ho: Optional[str], mok: Optional[str], max_versions: int = 60) -> Optional[UnitStatus]:
    """commits: 이 파일을 바꾼 커밋(최신순). load(commit) → (articles, meta, body) 또는 None(내려받기 실패).
    공포일·시행일은 각 판본 프론트매터에서 읽는다(얕은 클론 경계 커밋의 작성일은 다른 법령 것일 수 있다).
    판정 불가(이력 부족·로드 실패)면 None — 호출자가 이력 없음 모드로 내려간다."""
    rows = []   # (commit, prom, eff, text, title, own_buchik)
    extra = None
    for idx, commit in enumerate(commits[:max_versions]):
        got = load(commit)
        if got is None:
            return None
        arts, meta, body = got
        prom = parse_date(meta.get("공포일자"))
        eff = parse_date(meta.get("시행일자")) or prom
        if prom is None or eff is None:
            return None
        art = next((a for a in arts if a.jo == jo and a.sub == sub), None)
        text = art.unit_text(hang, ho, mok) if art is not None else None
        own = [b for b in parse_buchik(body) if b.promulgated == prom]
        rows.append((commit, prom, eff, text, art.title if art is not None else None, own))
        if extra is not None:
            break                     # 멈춤 판본보다 한 판 더 오래된 것까지 읽어 멈춤 판본의 변경 여부를 안다
        # 충분히 과거까지 왔으면 멈춘다: 기준일에 시행 중이고 기준일보다 400일 이상 전에 공포된 판본
        if eff <= as_of and prom <= as_of - timedelta(days=400) and idx >= 1:
            extra = idx
    if len(rows) < 2:
        return None
    exhausted = extra is None and len(rows) == len(commits[:max_versions]) and len(commits) <= max_versions
    head = rows[0]
    st = UnitStatus(state="unknown", precise=True, head_exists=head[3] is not None and not is_deleted_unit(head[3]))
    changes = []   # (i, 이 단위에 대한 시행일, 주의사항) — i 판본이 이 단위를 바꿨다
    for i in range(len(rows) - 1):
        if unit_signature(rows[i][3]) != unit_signature(rows[i + 1][3]):
            eff_u, notes = rows[i][2], []
            if rows[i][5]:
                cands = [unit_effective(b, jo, sub, hang, ho, rows[i][2]) for b in rows[i][5]]
                eff_u, notes = next((c for c in cands if c[1] and "따로 정해진" in c[1][0]), cands[0])
            changes.append((i, eff_u, notes))
    for pos, (i, eff_u, notes) in enumerate(changes):
        if eff_u is None:
            st.notes += notes + [f"{iso(rows[i][1])} 개정분의 이 조항 시행일을 알 수 없음(대통령령 위임 등)"]
            return st
        if eff_u <= as_of:
            older_pending = [c for c in changes[pos + 1:] if c[1] is None or c[1] > as_of]
            st.version = rows[i][0][:10]
            st.notes += notes
            if older_pending:
                st.notes.append("시행일이 늦은 이전 개정분이 이 조항에 섞여 있어 기준일 문언을 분리할 수 없음")
                st.exists = rows[i][3] is not None and not is_deleted_unit(rows[i][3])
                return st
            st.pending_change = pos > 0
            return _finish(st, rows[i][3], rows[i][4], rows[i][1], as_of)
        st.pending_change = True
        st.notes += [f"{iso(rows[i][1])} 공포 개정(이 조항 시행 {iso(eff_u)})은 기준일에 아직 시행 전"] + notes
    # 읽은 범위의 변경이 모두 시행 전(또는 변경 없음) → 가장 오래된 변경 직전 판본부터는 이 단위의 문언이 같다.
    # 그 구간 안에 기준일에 시행 중인 판본이 있어야 그 문언이 기준일 문언이라고 말할 수 있다.
    base_i = (changes[-1][0] + 1) if changes else 0
    in_force_k = next((k for k in range(base_i, len(rows)) if rows[k][2] <= as_of), None)
    if in_force_k is None:
        if exhausted:
            st.state = "absent"
            st.notes.append(f"이 법령의 가장 오래된 판본도 {iso(rows[-1][2])} 시행 — 기준일({iso(as_of)})에는 시행 전")
            return st
        return None
    base = rows[base_i]
    st.version = rows[in_force_k][0][:10]
    return _finish(st, base[3], base[4], base[1], as_of)


def _finish(st: UnitStatus, text: Optional[str], title: Optional[str], prom: date, as_of: date) -> UnitStatus:
    if text is None:
        st.state = "absent"
    elif is_deleted_unit(text):
        st.state = "deleted"
        st.text = text
    else:
        st.state = "in_force"
        st.text = text
        st.title = title
    return st


def unit_status_heuristic(art, all_buchik: List[Buchik], head_prom: Optional[date], head_eff: Optional[date],
                          as_of: date, hang: Optional[int], ho: Optional[str], mok: Optional[str]) -> UnitStatus:
    """이력 없음: HEAD 텍스트와 개정표시·부칙만으로 판정. 확실하지 않으면 unknown(fail-closed)."""
    text = art.unit_text(hang, ho, mok) if art is not None else None
    st = UnitStatus(state="unknown", precise=False, version="HEAD",
                    head_exists=text is not None and not is_deleted_unit(text))
    if art is None or text is None:
        if head_eff is not None and head_eff <= as_of:
            st.state = "absent"          # HEAD 가 시행 중인데 없다 → 기준일에도 없다(삭제 흔적도 없음)
            return st
        st.notes.append("최신 공포본이 기준일에 시행 전이라, 기준일 판본에 이 조항이 있었는지 확인 불가(전체 이력 필요)")
        return st
    # 최신 공포본의 부칙은 마지막 전부개정(또는 제정)부터만 실린다(실측: 근로기준법은 2007.4.11. 전부개정부터).
    # 기준일이 그보다 앞이면 조문 번호 체계부터 다를 수 있어 개정표시로는 판정할 수 없다.
    if all_buchik:
        base = min(all_buchik, key=lambda b: b.promulgated)
        base_eff = base.main_effective or base.promulgated
        if as_of < base_eff:
            st.notes.append(f"기준일이 현행 법문의 출발점(부칙 {iso(base.promulgated)}, 시행 {iso(base_eff)})보다 앞섬 — "
                            "전부개정 전 판본은 이력(setup 전체 이력) 또는 DRF 로만 확인 가능")
            return st
    # 조(條) 단위 신설 표시(장·절 제목 또는 [본조신설])가 기준일 이후면 '없음'이 확실(조 번호는 밀리지 않는다)
    art_level = []
    for h in getattr(art, "headings", []):
        art_level += [(k, d) for k, d in amendment_markers(h) if k in ("신설",)]
    art_level += [(k, d) for k, d in amendment_markers(art.text) if k == "본조신설"]
    by_prom = {b.promulgated: b for b in all_buchik}
    for k, d in art_level:
        b = by_prom.get(d)
        eff = unit_effective(b, art.jo, art.sub, None, None)[0] if b else None
        if d > as_of or (eff is not None and eff > as_of):
            st.state = "absent"
            st.notes.append(f"이 조문은 {iso(d)} 신설(시행 {iso(eff) or '미확인'}) — 기준일에는 없었음")
            return st
    if head_eff is None:
        st.notes.append("최신 공포본의 시행일을 알 수 없음")
        return st

    def eff_of(d: date) -> Tuple[bool, Optional[date]]:
        """(부칙 찾음, 이 조문에 대한 그 개정의 시행일)"""
        b = by_prom.get(d)
        if b is None:
            return False, None
        return True, unit_effective(b, art.jo, art.sub, None, None)[0]

    # 조문이 기준일에 이미 있었다는 적극적 증거: 기준일 전에 시행된 개정표시가 조문 어딘가에 있음.
    # legalize-kr 은 [본조신설] 표시를 거의 싣지 않아(근로기준법 전체 1건) 표시 없는 조문은 '나중에 신설된 조문'일 수 있다.
    prior = False
    for k, d in amendment_markers(art.text) + [m for h in getattr(art, "headings", []) for m in amendment_markers(h)]:
        found, eff = eff_of(d)
        if (found and eff is not None and eff <= as_of) or (not found and (as_of - d).days > 3 * 365):
            prior = True
            break
    if head_eff > as_of and not prior:
        st.notes.append(f"최신 공포본이 기준일 이후({iso(head_eff)}) 시행이고 이 조문에는 기준일 전 개정표시가 없어 "
                        "기준일에 있었는지 확인 불가(표시 없이 신설된 조문일 수 있음 — 전체 이력 필요)")
        return st
    # 이 단위에 걸린 개정표시·부칙 단서가 모두 기준일 전에 시행됐는지 확인한다(HEAD 가 시행 전이어도 같은 방법:
    # 시행 전 개정이 이 단위를 건드렸다면 그 개정일의 표시가 붙어 있다 — 항 신설·번호 이동에도 표시가 붙는 것을 실측)
    events = [(k, d) for k, d in amendment_markers(text)]
    if hang is None and ho is None:
        events += [(k, d) for k, d in amendment_markers(art.text)]
    for h in getattr(art, "headings", []):
        events += [(k, d) for k, d in amendment_markers(h) if k == "신설"]
    pending = []
    for k, d in sorted(set(events), key=lambda x: x[1], reverse=True):
        b = by_prom.get(d)
        if b is None:
            if (as_of - d).days <= 3 * 365:
                st.notes.append(f"{iso(d)} 개정의 부칙을 찾지 못해 시행일 확인 불가")
                return st
            continue
        eff, notes = unit_effective(b, art.jo, art.sub, hang, ho)
        if eff is None:
            st.notes += notes + [f"{iso(d)} 개정분의 시행일이 대통령령 등에 위임되어 확인 불가"]
            return st
        if eff > as_of:
            pending.append((k, d, eff))
    # 개정표시 없이 부칙 단서에만 나오는 조항(표시 없는 신설 등)도 확인
    for b in all_buchik:
        eff, _ = unit_effective(b, art.jo, art.sub, hang, ho)
        if eff is not None and eff > as_of and any(coverage(ex.units, art.jo, art.sub, hang, ho) == "full" for ex in b.exceptions):
            pending.append(("부칙", b.promulgated, eff))
    if pending:
        k, d, eff = pending[0]
        st.pending_change = True
        st.notes.append(f"{iso(d)} 개정({k})이 이 조항에 {iso(eff)}부터 시행 — 기준일 문언은 이력 없이 확인 불가")
        # 조 전체 인용이고 기준일 전 개정표시가 있으면 조문 자체는 기준일에도 있었다 — 문언만 모른다
        if hang is None and ho is None and mok is None and prior:
            st.exists = True
        return st
    if head_eff > as_of:
        st.notes.append(f"최신 공포본은 {iso(head_eff)} 시행이지만 이 조항에는 시행 전 개정표시가 없어 기준일 문언과 같다고 판단(개정표시 기준 추정)")
    return _finish(st, text, art.title, head_prom or as_of, as_of)
