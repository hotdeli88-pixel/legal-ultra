"""temporal.py - 기준일(as-of)에 시행 중인 조문 판정 (legal-ultra v2).

legalize-kr 의 각 커밋은 '공포일 기준' 판본이고, 판본의 시행일자는 단조 증가하지 않는다(실측: 근로기준법
2026-02-19 판본 시행 2026-08-20, 03-17 판본 2027-01-01, 04-07 판본 2026-10-08, 06-09 판본 2027-06-10).
또 한 판본 안에서도 부칙 단서로 조항별 시행일이 다르다(예: 2026.4.7. 부칙 — 제44조의4 는 2027-01-01).
그래서 판정은 '조·항·호 단위'로 한다.

정밀 모드(전체 이력 git)
  - 인용 단위의 문언(과 조문 제목)이 바뀐 판본마다 그 개정의 부칙으로 이 단위의 시행일을 계산한다.
    부칙은 **최신 판본에 실린 문언**을 쓴다 — 부칙 시행일 조문 자체가 뒤에 개정되어 시행일이 미뤄지는 일이 있다
    (실측: 지방행정제재·부과금법 2020.3.24. 부칙 제1조는 세 번 개정되어 제7조의6 시행일이 2027-01-01).
  - 부칙 단서는 본칙 시행일 + '다만, …의 개정규정은 …부터', '다음 각 호의 구분에 따른 날'(번호 목록), 범위·같은 조·항·호 목록을
    해석한다. 단위의 일부에만 걸린 단서('… 중 … 부분', 단서·본문·전단·후단, '부분에 한정', 하위 항·호)는 그 단위의 문언이
    기준일에 개정 전·후로 섞여 있을 수 있으므로 'mixed'(후보 문언 두 개)로 돌려준다 — 인용문·제목은 두 후보 모두와 맞아야 확인.
  - 기준일 뒤에도 이 단위에 걸려 있는 부칙(시행 전·미확정)이 있으면 그 개정 판본보다 앞까지 거슬러 읽는다.
이력 없음(얕은 클론·zip)
  - 최신 공포본이 기준일에 이미 시행 중일 때만 판정한다: 인용 단위와 그 상위(항·호) 줄의 개정표시, 부칙 단서(전부·일부)가
    모두 기준일 전에 시행됐으면 최신 문언이 기준일 문언. legalize-kr 은 신설 조·호에 표시를 싣지 않는 일이 많아(실측)
    최신 공포본이 기준일 뒤에 시행되면 표시로는 판정할 수 없다 → '확인 불가'(fail-closed). 장·절 제목의 신설 표시가 기준일
    뒤인 조문만 '없음'으로 확정한다.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from typing import Callable, List, NamedTuple, Optional, Tuple

from kr_common import CIRCLED_CHARS, amendment_markers, iso, normalize_for_match, normalize_law_name, parse_date

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


_WHEN_PERIOD_RE = re.compile(
    r"(?P<base>공포|이\s*(?:법률|법|영|규칙|령)\s*(?:의\s*)?시행)\s*(?:일\s*|한\s*날\s*)?(?:후|로부터|부터)\s*"
    r"(?P<n>\d+)\s*(?P<unit>일|개월|월|년)(?:\s*(?P<n2>\d+)\s*(?P<unit2>개월|월))?\s*(?:이|가)?\s*(?:경과한|지난)\s*날")
_WHEN_DATE_RE = re.compile(r"(?<!\d)(\d{4}|\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_WHEN_DOT_RE = re.compile(r"(?<!\d)(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?")
_DELEGATED_RE = re.compile(r"대통령령|총리령|부령|(?:규칙|조례)으로\s*정하는|별도로\s*정하는|고시하는\s*날|정하여\s*고시")
_PROM_MONTH_RE = re.compile(r"공포\s*(?:한\s*날|일)\s*(?:이|의)?\s*속하는\s*달의\s*(?P<next>다음\s*달\s*)?(?:1\s*일|첫날|초일)")


def _year(y: str) -> int:
    v = int(y)
    if len(y) == 2:
        return 1900 + v if v >= 40 else 2000 + v
    return v


def _next_month_first(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def parse_when(expr: str, prom: date, main: Optional[date] = None) -> Optional[date]:
    """시행 시기 표현 → 날짜. 알 수 없으면(대통령령으로 정하는 날 등) None."""
    e = re.sub(r"\s+", " ", expr or "").strip()
    if _DELEGATED_RE.search(e):
        return None
    m = _WHEN_PERIOD_RE.search(e)
    if m:
        base = prom if m.group("base").startswith("공포") else main
        if base is None:
            return None
        unit = "개월" if m.group("unit") in ("개월", "월") else m.group("unit")
        if m.group("n2"):
            if unit != "년":
                return None
            d = add_months(base, 12 * int(m.group("n")) + int(m.group("n2"))) + timedelta(days=1)   # '1년 6개월'
        else:
            d = after_period(base, int(m.group("n")), unit)
        if re.search(r"속하는\s*달의\s*다음\s*달", e):
            return _next_month_first(d) if re.search(r"다음\s*달\s*(?:1\s*일|첫날|초일)", e) else None
        if re.search(r"속하는\s*달의\s*(?:1\s*일|첫날|초일)", e):
            return date(d.year, d.month, 1)
        if "속하는" in e:
            return None
        return d
    m = _PROM_MONTH_RE.search(e)
    if m:
        return _next_month_first(prom) if m.group("next") else None
    if "속하는" in e:
        return None
    m = _WHEN_DATE_RE.search(e)
    if m:
        try:
            return date(_year(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _WHEN_DOT_RE.search(e)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if re.search(r"공포\s*(?:한\s*날|일)", e):
        return prom
    if re.search(r"이\s*(?:법률|법|영|규칙|령)\s*(?:의\s*)?시행(?:일|과\s*동시|하는\s*날)|같은\s*날", e):
        return main
    return None


# ---------------------------------------------------------------------------
# 부칙 단서의 조항 목록
# ---------------------------------------------------------------------------


@dataclass
class USpec:
    """부칙 단서가 가리키는 조항(범위 가능). partial=단서·전단·'… 부분' 등 그 조항의 일부만."""
    jo: int
    sub: Optional[int] = None
    hang: Optional[int] = None
    ho: Optional[str] = None
    jo_end: Optional[Tuple[int, int]] = None
    hang_end: Optional[int] = None
    ho_end: Optional[str] = None
    partial: bool = False


_MOK_LETTERS = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허"
_REF_TOKEN_RE = re.compile(
    r"(?P<ext>「[^」]{1,80}」)"
    r"|(?P<buchik>부\s*칙)"
    r"|(?P<act>(?:법률|대통령령|총리령|[가-힣]{1,12}부령|[가-힣]{1,12}규칙)\s*제\s*\d+\s*호(?!\s*(?:서식|의\s*\d+\s*서식)))"
    r"|(?P<full>제\s*(?P<jo>\d+)\s*조(?:\s*의\s*(?P<sub>\d+))?(?:\s*제\s*(?P<hang>\d+)\s*항)?"
    r"(?:\s*제\s*(?P<ho>\d+)\s*호(?:\s*의\s*(?P<hosub>\d+))?)?(?:\s*(?P<mok>[" + _MOK_LETTERS + r"])\s*목(?!적))?)"
    r"|(?P<same>(?:같은\s*|동)조(?![가-힣])(?:\s*제\s*(?P<shang>\d+)\s*항)?(?:\s*제\s*(?P<sho>\d+)\s*호(?:\s*의\s*(?P<shosub>\d+))?)?)"
    r"|(?P<sameh>(?:같은\s*|동)항\s*제\s*(?P<shho>\d+)\s*호(?:\s*의\s*(?P<shhosub>\d+))?)"
    r"|(?P<bhang>제\s*(?P<bh>\d+)\s*항(?:\s*제\s*(?P<bhho>\d+)\s*호(?:\s*의\s*(?P<bhhosub>\d+))?)?)"
    r"|(?P<bho>제\s*(?P<bo>\d+)\s*호(?:\s*의\s*(?P<bosub>\d+))?)"
    r"|(?P<bmok>(?<![가-힣])[" + _MOK_LETTERS + r"]\s*목(?!적))"      # '제목'·'다목적'은 목이 아니다
    r"|(?P<from>부터|내지)"
    r"|(?P<to>까지)"
    r"|(?P<qual>단서|본문|전단|후단|외의\s*부분|부분한정)")
_FORM_RE = re.compile(r"(?:별표|부표|별지)[^,ㆍ및]*?(?=\s*(?:,|ㆍ|및|의\s*(?:개정|규정)|$))|(?:별표|부표)\s*\d*(?:\s*의\s*\d+)?"
                      r"|별지\s*(?:제\s*\d+\s*호(?:\s*의\s*\d+)?\s*)?서식|제\s*\d+\s*호(?:\s*의\s*\d+)?\s*서식")
_IRRELEVANT_RE = re.compile(r"부칙\s*제\s*\d+\s*조에\s*따라\s*개정되는|(?:법률|대통령령|총리령|부령|조례|규칙)\s*중\s.*?개정한\s*부분"
                            r"|다른\s*법(?:률|령)의\s*개정|시행일이\s*도래하지")
# '… 전에 공포되었으나 시행일이 도래하지 아니한 법률을 개정한 부분은 각각 해당 법률의 시행일부터' — 그 법률의 시행일을 따른다
_PENDING_LAW_RE = re.compile(r"시행일이\s*도래하지|시행되지\s*아니한\s*(?:법률|법령|대통령령)")


def _ho_key(h: str) -> Tuple[int, int]:
    a, _, b = str(h).partition("의")
    try:
        return int(a), int(b or 0)
    except ValueError:
        return (0, 0)


def parse_subject(s: str, own_title: Optional[str] = None) -> Tuple[List[USpec], bool, bool]:
    """부칙 단서의 주어('제19조제2항 및 제3항의 개정규정') → (조항 목록, 해석 불확실, 이 법 본문과 무관)."""
    return parse_subject_ex(s, own_title)[:3]


def parse_subject_ex(s: str, own_title: Optional[str] = None, foreign: bool = False,
                     ctx0: Optional[Tuple[int, Optional[int], Optional[int], Optional[str]]] = None
                     ) -> Tuple[List[USpec], bool, bool, List[USpec]]:
    """parse_subject + 부칙 조항 목록. foreign=True(타법개정 부칙): 맨 조항 번호는 그 다른 법률의 것이므로 건너뛰고,
    「이 법」 뒤의 조항만 이 법 조항으로 읽는다. '부칙 제N조…' 뒤의 조항은 부칙 조항(네 번째 값)."""
    t = re.sub(r"<[^<>]*>", " ", s or "")
    t = re.sub(r"\(([^()]*)\)", lambda m: " 부분한정 " if re.search(r"부분|한정|제외|경우", m.group(1)) else " ", t)
    had_form = bool(_FORM_RE.search(t))
    t = _FORM_RE.sub(" ", t)
    partial_all = False
    m = re.search(r"(?:규정|사항|조항)\s*중\s", t)
    if m:
        partial_all = True
        t = t[:m.start()]
    units: List[USpec] = []
    bu: List[USpec] = []
    ctx: Optional[Tuple[int, Optional[int], Optional[int], Optional[str]]] = None if foreign else ctx0
    mode = "foreign" if foreign else "own"
    skipped = 0
    in_range = False
    last: List[USpec] = []
    uncertain = False
    act_seen = False
    for m in _REF_TOKEN_RE.finditer(t):
        if m.group("ext"):
            same_law = own_title and normalize_law_name(m.group("ext")) == normalize_law_name(own_title)
            mode = "own" if same_law else "foreign"
            ctx, in_range = None, False
            continue
        if m.group("buchik"):
            mode, ctx, in_range = "buchik", None, False
            continue
        if m.group("act"):
            # '법률 제N호 ○○법 일부개정법률 제3조의 개정규정' — 다른 개정법률의 조항(이 법 조항인지 알 수 없음)
            mode, ctx, in_range = "foreign", None, False
            uncertain = act_seen = True
            continue
        if m.group("from"):
            in_range = True
            continue
        if m.group("to"):
            in_range = False
            continue
        if m.group("qual"):
            for u in last:
                u.partial = True
            continue
        if mode == "foreign":
            skipped += 1
            continue
        target = units if mode == "own" else bu
        ref = None
        if m.group("full"):
            ho = m.group("ho") + (f"의{m.group('hosub')}" if m.group("hosub") else "") if m.group("ho") else None
            ref = (int(m.group("jo")), int(m.group("sub")) if m.group("sub") else None,
                   int(m.group("hang")) if m.group("hang") else None, ho)
            mok_partial = bool(m.group("mok"))
        elif ctx is None:
            uncertain = True        # 앞 조항 없이 '같은 조'·'제3항' 등
            continue
        elif m.group("same"):
            ho = m.group("sho") + (f"의{m.group('shosub')}" if m.group("shosub") else "") if m.group("sho") else None
            ref = (ctx[0], ctx[1], int(m.group("shang")) if m.group("shang") else None, ho)
            mok_partial = False
        elif m.group("sameh"):
            ref = (ctx[0], ctx[1], ctx[2], m.group("shho") + (f"의{m.group('shhosub')}" if m.group("shhosub") else ""))
            mok_partial = False
        elif m.group("bhang"):
            ho = m.group("bhho") + (f"의{m.group('bhhosub')}" if m.group("bhhosub") else "") if m.group("bhho") else None
            ref = (ctx[0], ctx[1], int(m.group("bh")), ho)
            mok_partial = False
        elif m.group("bho"):
            ref = (ctx[0], ctx[1], ctx[2], m.group("bo") + (f"의{m.group('bosub')}" if m.group("bosub") else ""))
            mok_partial = False
        elif m.group("bmok"):
            if ctx[3] is None:
                uncertain = True
                continue
            ref = ctx
            mok_partial = True
        if ref is None:
            continue
        if in_range and target:
            start = target.pop()
            if start.ho is not None and ref[3] is not None and (start.jo, start.sub, start.hang) == ref[:3]:
                start.ho_end = ref[3]
            elif start.hang is not None and ref[2] is not None and start.ho is None and ref[3] is None and (start.jo, start.sub) == ref[:2]:
                start.hang_end = ref[2]
            elif start.hang is None and start.ho is None and ref[2] is None and ref[3] is None:
                start.jo_end = (ref[0], ref[1] or 0)
            else:
                uncertain = True
                target.append(start)
                start = USpec(*ref)
            target.append(start)
            last = [start]
            in_range = False
        else:
            u = USpec(ref[0], ref[1], ref[2], ref[3], partial=mok_partial)
            target.append(u)
            last = [u]
        ctx = ref
    for u in units + bu:
        u.partial = u.partial or partial_all
    irrelevant = not units and (bool(bu) or (not act_seen and (skipped > 0 or bool(_IRRELEVANT_RE.search(s or "")))))
    if not units and not bu and not skipped and had_form and not act_seen and not re.search(r"제\s*\d+\s*조|같은\s*조|동조", t):
        return units, False, True, bu          # 별표·서식만('별표 2 중 제4호나목 및 다목') — 조문 단위와 무관
    if not units and not bu and not irrelevant and re.search(r"규정|사항|조항|부분|생략|ㆍㆍㆍ", s or ""):
        uncertain = True
    return units, uncertain, irrelevant, bu


def parse_unit_list(s: str) -> List[USpec]:
    return parse_subject(s)[0]


def coverage(units: List[USpec], jo: int, sub: Optional[int], hang: Optional[int], ho: Optional[str]) -> str:
    """부칙 단서의 조항 목록이 인용 단위를 덮는 정도: full | partial | none."""
    best = "none"
    for u in units:
        if u.jo_end is not None:
            if (u.jo, u.sub or 0) <= (jo, sub or 0) <= u.jo_end:
                if u.partial:
                    best = "partial"
                    continue
                return "full"
            continue
        if (u.jo, u.sub) != (jo, sub):
            continue
        if u.hang is None and u.ho is None:
            if u.partial:
                best = "partial"
                continue
            return "full"
        if hang is None and (ho is None or u.hang is not None):
            best = "partial"                  # 조 전체 인용인데 단서는 그 일부 항·호에만
            continue
        if u.hang is not None:
            if hang is None or not (u.hang <= hang <= (u.hang_end or u.hang)):
                continue
            if u.ho is None:
                if u.partial:
                    best = "partial"
                    continue
                return "full"
            if ho is None:
                best = "partial"
                continue
        elif hang is not None:
            continue
        if ho is None:
            best = "partial"
            continue
        if _ho_key(u.ho) <= _ho_key(ho) <= _ho_key(u.ho_end or u.ho):
            if u.partial:
                best = "partial"
                continue
            return "full"
    return best


# ---------------------------------------------------------------------------
# 부칙 파싱
# ---------------------------------------------------------------------------


@dataclass
class Exception_:
    units: List[USpec]
    effective: Optional[date]
    raw: str
    uncertain: bool = False      # 대상 조항을 해석하지 못함 — 어느 조항에든 걸릴 수 있다
    applies: bool = False        # '…부터 적용한다'(시행은 본칙대로, 적용 시기만 다름)
    whole: str = ""              # 타법개정 부칙: 이 법을 고치는 부칙 조항에 걸린 단서 — full(이 법 개정분 전체)|partial(일부일 수 있음)


@dataclass
class Buchik:
    promulgated: date
    number: str
    other_law: Optional[str]          # 타법개정 부칙이면 그 법령명
    main_effective: Optional[date]
    main_raw: str
    exceptions: List[Exception_] = field(default_factory=list)
    proviso_omitted: bool = False     # '<단서 생략>'·'N. 생략' — 법제처가 이 법과 무관한 부분을 줄여 실은 것
    unparsed: bool = False            # 시행일 조문을 해석하지 못함
    amending: Optional[Tuple[int, Optional[int]]] = None   # 타법개정: 이 법을 고치는 부칙 조·항(예: 제7조 <399>)


_BUCHIK_HEAD_RE = re.compile(
    r"^부칙\s*(?:\((?P<other>[^)]*)\))?\s*<\s*(?:제\s*(?P<no>\d+)\s*호\s*,\s*)?(?P<date>\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2})\s*\.?\s*>",
    re.M)
_ACT = r"(?:이|본)\s*(?:법률|법|시행령|시행규칙|영|규칙|령)"
_DOTTED = r"\d{2,4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?"
_MAIN_RE = re.compile(_ACT + r"(?:\s*[과와]\s*부칙[^은는.]{0,30}?)?\s*[은는]\s*(?P<when>(?:" + _DOTTED + r"|[^.])*?)\s*(?:부터|로부터)?\s*시행(?:한다|하되|하고|하며|\s*,)")
# 번호 목록의 각 호가 제 시행일을 정하는 표현(실측 변형: '각 호의 구분에 따른 날', '해당 호에서 정하는 날', '그 구분에 따른 날',
# '각호의 구분에 의한 날', '해당 각 호에 규정된 날', '다음 각 호에서 정한 날' …)
_PER_ITEM = (r"(?:각각\s*)?(?:그\s*구분에\s*(?:따른|의한)|(?:해당\s*|다음\s*)?(?:각\s*)?호"
             r"(?:의\s*구분에\s*(?:따른|의한)|에서\s*정(?:하는|한)|에\s*규정된|에\s*따른))\s*날")
_GAKHO_MAIN_RE = re.compile(_ACT + r"\s*[은는]\s*다음\s*각\s*호(?:의\s*구분에\s*(?:따른|의한)|에서\s*정(?:하는|한)|에\s*규정된)\s*날\s*"
                            r"(?:부터|로부터)\s*(?:각각\s*)?시행")
_WHEN_CHUNK = r"(?:" + _DOTTED + r"|속하는|정하는|고시하는|지정하는|도래하는|되는|[^은는.])"
_SEG_RE = re.compile(r"(?:은|는)\s*(?:각각\s*)?(?P<when>" + _PER_ITEM + r"|" + _WHEN_CHUNK + r"{1,120}?)\s*(?:부터|로부터)\s*"
                     r"(?:(?P<comma>,)(?!\s*(?:각각\s*)?(?:시행|적용))|,?\s*(?:각각\s*)?(?P<verb>시행|적용)(?:하고|하며|하되|한다|하여야|하도록|하여|할)?)")
_ITEM_RE = re.compile(r"^\s*(?P<no>\d{1,2})\s*\\?\.(?!\s*\d{1,2}\s*\.)\s*(?P<text>.*?)\s*$")   # '2020. 1. 1.부터'는 호가 아니다
_MOK_LINE_RE = re.compile(r"^\s*(?P<no>[" + _MOK_LETTERS + r"])\s*\\?\.\s*(?P<text>.*?)\s*$")
_OMITTED_ITEM_RE = re.compile(r"^[\d\s\\.,ㆍ및부터내지까지]*(?:생략|<\s*생략\s*>|ㆍㆍㆍ.*)$")
_NEXT_ARTICLE = r"^\s*(?:#+\s*)?제\s*(?:[2-9]|[1-9]\d+)\s*조(?!\s*의\s*\d)"
_OMIT_MARK_RE = re.compile(r"(?:ㆍ{2,}|…+|\.{3,})\s*<?\s*생략\s*>?\s*(?:ㆍ{2,}|…+|\.{3,})|<\s*(?:단서\s*|후문\s*)?생략\s*>|ㆍ{3,}|…+")
_PARA_MARK_RE = re.compile(r"(?:\*\*)?([①-⑳])(?:\*\*)?|<\s*(\d{1,3})\s*>")


class _Item(NamedTuple):
    text: str
    moks: List[str]


def _segments(exc: str) -> List[Tuple[str, str, str]]:
    """'A는 D1부터 시행하고, B는 D2부터 적용한다' → [(A, D1, 시행), (B, D2, 적용)]. 'A는 D1부터, B는 D2부터 각각 시행' 도 읽는다."""
    out: List[Tuple[str, str, str]] = []
    pending: List[Tuple[str, str]] = []
    last = 0
    for m in _SEG_RE.finditer(exc):
        subj = exc[last:m.start()]
        subj = re.sub(r"^[\s,.]*(?:[②-⑳]\s*)?(?:다만\s*,?\s*)?(?:그리고|또한|및|이\s*경우)?\s*", "", subj)
        subj = re.sub(r"^(?:제\s*\d+\s*항|본문|같은\s*항)\s*에도\s*불구하고\s*,?\s*", "", subj).strip(" ,")
        when = m.group("when").strip()
        if m.group("comma"):
            pending.append((subj, when))
        else:
            verb = m.group("verb")
            out += [(s, w, verb) for s, w in pending] + [(subj, when, verb)]
            pending = []
        last = m.end()
    out += [(s, w, "시행") for s, w in pending]
    return out


def _first_article(block: str) -> str:
    """부칙 블록의 첫 조(시행일 조문)만 — 다른 조(경과조치·다른 법률의 개정)의 '…부터 시행' 문장을 섞지 않는다."""
    s = block.lstrip()
    if re.match(r"(?:#+\s*)?제\s*1\s*조(?!\s*의\s*\d)", s):
        cut = re.search("(?m)" + _NEXT_ARTICLE, block)
    else:
        cut = re.search(r"(?m)^\s*(?:\*\*)?[②-⑳]|" + _NEXT_ARTICLE, block)
    return block[:cut.start()] if cut else block


def _split_items(text: str) -> Tuple[str, List[_Item]]:
    head: List[str] = []
    items: List[_Item] = []
    for ln in text.split("\n"):
        m = _ITEM_RE.match(ln)
        if m:
            items.append(_Item(m.group("text"), []))
            continue
        mm = _MOK_LINE_RE.match(ln)
        if mm and items:
            items[-1].moks.append(mm.group("text"))
            continue
        if items:
            if ln.strip():
                items[-1] = _Item(items[-1].text + " " + ln.strip(), items[-1].moks)
        else:
            head.append(ln)
    return "\n".join(head), items


def _item_parts(text: str) -> Tuple[str, Optional[str]]:
    m = re.match(r"(?P<subj>[^:：]+?)\s*[:：]\s*(?P<when>.+)$", text)
    return (m.group("subj"), m.group("when")) if m else (text, None)


_AMEND_PHRASE_RE = re.compile(r"(?:일부|전부)\s*를\s*다음과\s*같이\s*개정한다|중\s*다음과\s*같이\s*개정한다")


def _locate_amending(block: str, pos: int) -> Optional[Tuple[int, Optional[int]]]:
    """block[pos] 가 들어 있는 부칙 조·항(항 번호: ①…⑳ 또는 <21> 꼴)."""
    before = block[:pos]
    arts = list(re.finditer(r"(?m)^\s*(?:#+\s*)?제\s*(\d+)\s*조(?:\s*의\s*\d+)?\s*\(", before))
    if not arts:
        return None
    paras = list(_PARA_MARK_RE.finditer(before[arts[-1].end():]))
    hang = None
    if paras:
        p = paras[-1]
        hang = (ord(p.group(1)) - ord("①") + 1) if p.group(1) else int(p.group(2))
    return int(arts[-1].group(1)), hang


def _amending_unit(block: str, own_title: str) -> Optional[Tuple[int, Optional[int]]]:
    """타법개정 부칙에서 이 법을 고치는 조·항: '제7조(다른 법률의 개정) ①부터 <398>까지 생략 <399> 고용정책 기본법 일부를 …'.
    제명이 그 뒤 바뀐 법률(옛 제명으로 실림)은, 실린 개정 조·항이 하나뿐이면 그것으로 본다(법제처는 이 법과 무관한 항을 '생략')."""
    key = re.sub(r"[\sㆍ·・]", "", own_title or "")
    if key:
        pat = (r"(?<![가-힣])" + r"[\sㆍ·・]*".join(map(re.escape, key)) +
               r"(?:\s*(?:일부|전부)개정(?:법률|령|규칙))?\s*(?:일부를|전부를|중\s*다음과|[을를]\s*다음과)")
        tm = re.search(pat, block)
        if tm is not None:
            loc = _locate_amending(block, tm.start())
            if loc is not None:
                return loc
    locs = {loc for loc in (_locate_amending(block, m.start()) for m in _AMEND_PHRASE_RE.finditer(block)) if loc is not None}
    return locs.pop() if len(locs) == 1 else None


def parse_buchik(body: str, own_title: Optional[str] = None) -> List[Buchik]:
    """본문의 '## 부칙' 절에서 부칙 블록들의 시행일 조문을 읽는다."""
    return list(_parse_buchik_cached(body or "", own_title or ""))


@lru_cache(maxsize=512)
def _parse_buchik_cached(body: str, own_title: str) -> Tuple[Buchik, ...]:
    i = body.find("\n## 부칙")
    if i < 0:
        i = body.find("## 부칙")
        if i < 0:
            return tuple()
    sec = body[i:]
    heads = list(_BUCHIK_HEAD_RE.finditer(sec))
    out: List[Buchik] = []
    for k, h in enumerate(heads):
        prom = parse_date(h.group("date"))
        if not prom:
            continue
        block = sec[h.end(): heads[k + 1].start() if k + 1 < len(heads) else len(sec)]
        b = Buchik(promulgated=prom, number=h.group("no") or "", other_law=h.group("other"),
                   main_effective=None, main_raw="")
        if b.other_law:
            b.amending = _amending_unit(block, own_title)
        _parse_first_article(b, _first_article(block), own_title)
        out.append(b)
    return tuple(out)


def _parse_first_article(b: Buchik, text: str, own_title: str) -> None:
    prom = b.promulgated
    head, items = _split_items(re.sub(r"<(?:개정|신설|전문개정)[^<>]*>", " ", text))
    flat = re.sub(r"\s+", " ", head)
    b.proviso_omitted = bool(re.search(r"단서\s*생략|후문\s*생략|<\s*생략\s*>|ㆍㆍㆍ|…", flat))
    # 법제처가 이 법과 무관한 부분을 줄인 표시('ㆍㆍㆍ<생략>ㆍㆍㆍ')는 지우고 읽는다 — 남은 문장만 이 법에 걸린다
    flat = re.sub(r"\s+", " ", _OMIT_MARK_RE.sub(" ", flat))
    gm = _GAKHO_MAIN_RE.search(flat)
    m = _MAIN_RE.search(flat)
    if gm and (m is None or gm.start() <= m.start()):
        # '이 법은 다음 각 호의 구분에 따른 날부터 시행한다.' + '1. …: 날짜' — 본칙 날짜는 '제N호 외의 …' 호
        rest: List[_Item] = []
        for it in items:
            subj, when = _item_parts(it.text)
            if when is not None and re.search(r"(?:제\s*\d+\s*호|각\s*호)\s*(?:외|밖)의|그\s*밖의|나머지", subj):
                b.main_effective = parse_when(when, prom)
                b.main_raw = f"{subj}: {when}"
            else:
                rest.append(it)
        if b.main_effective is None:
            b.unparsed = True
        for it in rest:
            _item_exceptions(b, it, None, "시행", own_title)
        return
    if m is None:
        b.unparsed = True
        return
    b.main_raw = m.group(0)
    b.main_effective = parse_when(m.group("when"), prom)
    tail = flat[m.end():]
    segs = _segments(tail)
    used_items = False
    ctx = None                                   # '같은 조 제2항은 …' 이 앞 단서의 조를 가리킨다
    for subj, when, verb in segs:
        per_item = bool(re.search(_PER_ITEM, when))
        if re.match(r"다음\s*각\s*호", subj) and items:
            used_items = True
            header = None if per_item else when
            for it in items:
                _item_exceptions(b, it, header, verb, own_title)
            continue
        if per_item and items:
            # 'A의 개정규정은 다음 각 호의 구분에 따른 날부터' — 각 호는 A 의 일부(대상·지역 등)별 시행일
            used_items = True
            whens = [_item_parts(it.text)[1] for it in items if not _OMITTED_ITEM_RE.match(it.text.strip())]
            for w in (whens or [None]):
                ctx = _add_exception(b, subj, w or "", verb, own_title, partial=True, ctx0=ctx)
            continue
        ctx = _add_exception(b, subj, when, verb, own_title, ctx0=ctx)
    if not segs and re.search(r"다만", tail) and re.search(r"제\s*\d+\s*조|규정|사항|각\s*호", tail):
        _add_uncertain(b, tail)
    if items and not used_items and not all(_OMITTED_ITEM_RE.match(it.text) for it in items):
        _add_uncertain(b, " / ".join(it.text for it in items))


def _item_exceptions(b: Buchik, it: _Item, header: Optional[str], verb: str, own_title: str) -> None:
    """번호 목록 한 호: '제60조제6항의 개정규정: 공포한 날' · '다음 각 목의 개정규정은 D부터 시행한다'(+ 가·나목) ·
    'A는 D부터 시행한다' · '생략'. header = 목록 전체에 공통인 시행 시기('다음 각 호의 개정규정은 D부터')."""
    t = it.text.strip()
    if not t:
        return
    if _OMITTED_ITEM_RE.match(t):
        b.proviso_omitted = True
        if not b.other_law:
            _add_uncertain(b, t)      # 제 법의 부칙이 줄여 실렸다 — 어느 조항인지 모른다
        return
    subj, when = _item_parts(t)
    if when is not None:
        for s in (it.moks if it.moks and re.search(r"다음\s*각\s*목", subj) else [subj]):
            _add_exception(b, s, when, verb, own_title)
        return
    if header is not None:
        for s in (it.moks if it.moks and re.search(r"다음\s*각\s*목", t) else [t]):
            _add_exception(b, s, header, verb, own_title)
        return
    segs = _segments(t)
    if not segs:
        _add_uncertain(b, t)
        return
    ctx = None
    for s, w, v in segs:
        if it.moks and re.match(r"다음\s*각\s*목", s):
            for mk in it.moks:
                _add_exception(b, mk, w, v, own_title)
        else:
            ctx = _add_exception(b, s, w, v, own_title, ctx0=ctx)


def _add_uncertain(b: Buchik, raw: str) -> None:
    raw = raw.strip()[:300]
    if b.other_law:
        b.exceptions.append(Exception_([], None, raw, whole="partial"))
    else:
        b.exceptions.append(Exception_([], None, raw, uncertain=True))


def _dates_in(when: str, prom: date, main: Optional[date]) -> List[Optional[date]]:
    """시행 시기 표현의 날짜. 한 표현에 날짜가 둘 이상이면('각각 2020년 1월 1일 및 2021년 1월 1일') 모두."""
    ds = set()
    for m in _WHEN_DATE_RE.finditer(when or ""):
        try:
            ds.add(date(_year(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    if len(ds) > 1 and not _WHEN_PERIOD_RE.search(when):
        return sorted(ds)
    return [parse_when(when, prom, main)]


def _add_exception(b: Buchik, subj: str, when: str, verb: str, own_title: str, partial: bool = False,
                   ctx0: Optional[Tuple[int, Optional[int], Optional[int], Optional[str]]] = None
                   ) -> Optional[Tuple[int, Optional[int], Optional[int], Optional[str]]]:
    """단서 하나를 b.exceptions 에 더하고, 다음 단서의 '같은 조'가 가리킬 조항을 돌려준다."""
    applies = verb == "적용"
    subj = (subj or "").strip()
    raw = f"{subj} … {when}부터 {verb}".strip()[:300]
    if _PENDING_LAW_RE.search(subj):
        return ctx0                   # 아직 시행 전인 다른 법률의 개정분 — 그 법률의 시행일을 따른다
    dates = _dates_in(when, b.promulgated, b.main_effective)
    units, uncertain, irrelevant, bu = parse_subject_ex(subj, own_title or None, foreign=bool(b.other_law), ctx0=ctx0)
    ctx = (units[-1].jo, units[-1].sub, units[-1].hang, units[-1].ho) if units else ctx0
    if b.other_law:
        # 타법개정 부칙의 맨 조항 번호는 그 다른 법률의 것 — 이 법에 걸리는 것은 이 법을 고치는 부칙 조항을 가리킨 단서뿐
        if units:
            whole = "partial"                 # 「이 법」 조항을 직접 가리킴(드묾) — 이 개정분 전체의 시행일 후보로 둔다
        elif bu:
            whole = "partial" if b.amending is None else coverage(bu, b.amending[0], None, b.amending[1], None)
            if whole == "none":
                return
        elif uncertain and "생략" not in subj:
            whole = "partial"
        else:
            return ctx0
        for d in dates:
            b.exceptions.append(Exception_([], d, raw, applies=applies, whole=whole if len(dates) == 1 else "partial"))
        return ctx0
    if irrelevant and not units:
        return ctx0
    if len(dates) > 1 or partial:
        for u in units:
            u.partial = True
    for d in dates:
        b.exceptions.append(Exception_(units, d, raw, uncertain=uncertain and not units, applies=applies))
        if uncertain and units:
            # 일부만 해석됨 — 해석한 조항 외에도 걸릴 수 있다
            b.exceptions.append(Exception_([], d, raw, uncertain=True, applies=applies))
    return ctx


def _foreign_effective(b: Buchik, fm: Optional[date]) -> Tuple[Optional[date], Optional[date], List[str]]:
    """타법개정 부칙이 이 법 개정분에 갖는 시행일 구간. 판본 시행일자(fm)가 있으면 후보에 함께 넣는다(둘이 다르면 구간)."""
    notes: List[str] = []
    active = [ex for ex in b.exceptions if ex.whole and not ex.applies]
    full = [ex.effective for ex in active if ex.whole == "full"]
    part = [ex.effective for ex in active if ex.whole == "partial"]
    if full:
        base: List[Optional[date]] = full
        notes.append(f"타법개정 부칙 단서로 이 법 개정분의 시행일이 따로 정해짐({', '.join(iso(d) or '미정' for d in full)})")
    elif b.main_effective is not None:
        base = [b.main_effective]
    else:
        base = [] if fm else [None]
    if part:
        notes.append(f"타법개정 부칙 단서가 이 법 개정분의 일부에 걸릴 수 있음({', '.join(iso(d) or '미정' for d in part)})")
    dates = base + part + ([fm] if fm else [])
    if fm and any(d != fm for d in base + part):
        notes.append(f"판본 시행일자({iso(fm)})와 타법개정 부칙의 시행일이 다름 — 둘 다 후보로 봄")
    known = [d for d in dates if d is not None]
    if not known:
        return None, None, notes + ["타법개정 부칙의 시행일을 확정할 수 없음"]
    if len(known) != len(dates):
        return min(known), None, notes
    return min(dates), max(dates), notes


def unit_effective(b: Buchik, jo: int, sub: Optional[int], hang: Optional[int], ho: Optional[str],
                   frontmatter_eff: Optional[date] = None,
                   article_only: bool = False) -> Tuple[Optional[date], Optional[date], List[str]]:
    """이 부칙(개정)이 인용 단위에 대해 갖는 시행일 구간 (가장 이른 날, 가장 늦은 날, 주의사항).
    같으면 그날 전부 시행. 다르면 그 사이에는 단위의 일부만 시행(또는 어느 날인지 불확실). None = 확정 불가.
    frontmatter_eff = 그 개정 판본의 시행일자(정밀 모드): 타법개정은 후보로 합치고, 제 법 부칙은 본칙을 해석하지 못했을 때만 쓴다."""
    if b.other_law:
        return _foreign_effective(b, frontmatter_eff)
    notes: List[str] = []
    main = b.main_effective
    if main is None and frontmatter_eff is not None and not b.exceptions and not _DELEGATED_RE.search(b.main_raw or ""):
        main = frontmatter_eff
        notes.append(f"부칙 시행일 조문을 해석하지 못해 판본 시행일자({iso(main)})로 판정")
    if b.unparsed and main is None:
        return None, None, ["부칙 시행일 조문을 해석하지 못함"]
    if main is None:
        return None, None, ["본칙 시행일 미확정(대통령령 위임 등)"]
    full_dates: List[Optional[date]] = []
    part_dates: List[Optional[date]] = []
    for ex in b.exceptions:
        if ex.applies:
            if ex.uncertain or coverage(ex.units, jo, sub, hang, ho) != "none":
                notes.append(f"부칙에 적용 시기를 따로 정한 규정이 있음({iso(ex.effective) or '미정'}) — 시행은 본칙대로")
            continue
        if ex.uncertain:
            part_dates.append(ex.effective)
            notes.append(f"부칙 단서의 대상 조항을 해석하지 못함({ex.raw[:60]}) — 이 조항에 걸리는지 확정 불가")
            continue
        units = [u for u in ex.units if u.hang is None and u.ho is None] if article_only else ex.units
        cov = coverage(units, jo, sub, hang, ho)
        if cov == "full":
            full_dates.append(ex.effective)
        elif cov == "partial":
            part_dates.append(ex.effective)
    if full_dates:
        notes.append(f"부칙 단서로 시행일이 따로 정해진 조항({', '.join(iso(d) or '미정' for d in full_dates)})")
        base = full_dates
    else:
        base = [main]
    if part_dates:
        notes.append(f"같은 조·항의 일부는 부칙 단서로 시행일이 다름({', '.join(iso(d) or '미정' for d in part_dates)})")
    dates = base + part_dates
    known = [d for d in dates if d is not None]
    if not known:
        return None, None, notes + ["시행일 미확정(대통령령 위임 등)"]
    if len(known) != len(dates):
        return min(known), None, notes
    return min(dates), max(dates), notes


# 시행일을 알 수 없는 개정(대통령령 위임·해석 실패)은 공포 후 이 기간 안이면 기준일에 시행 전일 수 있다고 본다
_UNKNOWN_HORIZON_DAYS = 5 * 365


def block_span(b: Buchik) -> Tuple[Optional[date], Optional[date]]:
    """이 개정이 조항을 따로 가리키지 않은 부분에 갖는 시행일 구간(제 법: 본칙, 타법개정: 이 법 개정분 전체)."""
    if b.other_law:
        lo, hi, _ = _foreign_effective(b, None)
        return lo, hi
    return b.main_effective, b.main_effective


def explicit_coverage(b: Buchik, jo: int, sub: Optional[int], hang: Optional[int], ho: Optional[str]) -> bool:
    if b.other_law:
        return False                  # 타법개정 단서는 조항을 가리키지 않는다(이 법 개정분 전체 — 개정표시·판본으로 판정)
    return any(ex.uncertain or coverage(ex.units, jo, sub, hang, ho) != "none" for ex in b.exceptions if not ex.applies)


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
    return normalize_for_match(re.sub(r"<[^>]*(?:개정|신설|삭제)[^>]*>|\[[^\]]*(?:신설|개정|이동)[^\]]*\]", "", text))


@dataclass
class UnitStatus:
    state: str                      # in_force | mixed | absent | deleted | unknown
    text: Optional[str] = None      # 기준일 시행 문언(하나로 확정된 경우)
    title: Optional[str] = None     # 기준일 조문 제목(하나로 확정된 경우)
    precise: bool = False
    version: str = ""               # 근거 판본(커밋 또는 HEAD)
    notes: List[str] = field(default_factory=list)
    head_exists: bool = False       # HEAD(최신 공포본)에 이 단위가 있는가
    pending_change: bool = False    # 최신 공포본의 이 단위 문언이 아직 시행 전인가
    exists: Optional[bool] = None   # (unknown 일 때) 기준일에 이 단위가 있었던 것은 확실한가 — 문언만 모름
    texts: List[str] = field(default_factory=list)            # 기준일 문언 후보(mixed: 개정 전·후)
    titles: List[Optional[str]] = field(default_factory=list)  # 기준일 제목 후보(비면 미확정)


# ---------------------------------------------------------------------------
# 정밀 판정
# ---------------------------------------------------------------------------


class _Row(NamedTuple):
    commit: str
    prom: date
    eff: date
    number: str
    text: Optional[str]
    title: Optional[str]
    body: str
    art: object = None


class _Res(NamedTuple):
    kind: str                         # value | mixed | unknown | before_history
    values: List[Optional[str]]
    pending: bool
    notes: List[str]
    idx: Optional[int]
    exists: Optional[bool] = None


def _resolve(rows: List[_Row], values: List[Optional[str]], sig: Callable[[Optional[str]], Optional[str]],
             eff_fn: Callable[[int], Tuple[Optional[date], Optional[date], List[str]]], as_of: date,
             exhausted: bool, what: str) -> Optional[_Res]:
    sigs = [sig(v) for v in values]
    changes = [i for i in range(len(rows) - 1) if sigs[i] != sigs[i + 1]]
    effs = {i: eff_fn(i) for i in changes}
    notes: List[str] = []

    def older_in_force(pos: int) -> bool:
        return all(effs[j][1] is not None and effs[j][1] <= as_of for j in changes[pos + 1:])

    def exists_certain() -> Optional[bool]:
        """기준일에 이 단위가 (문언과 별개로) 있었던 것이 확실한가: 단위를 만든 개정이 시행됐어야 한다."""
        creations = [j for j in changes if values[j] is not None and values[j + 1] is None]
        removals = [j for j in changes if values[j] is None and values[j + 1] is not None]
        if removals:
            return None
        if creations:
            hi = effs[creations[0]][1]
            return True if hi is not None and hi <= as_of else None
        return True if values[-1] is not None and rows[-1].eff <= as_of else None

    # 단위를 새로 만든 개정이 기준일에 아직 전혀 시행 전이면, 뒤의 개정이 그 문언을 고쳤더라도 기준일에는 없다
    creations = [j for j in changes if values[j] is not None and values[j + 1] is None]
    if creations and not [j for j in changes if values[j] is None and values[j + 1] is not None]:
        j = creations[-1]
        lo, hi, n = effs[j]
        if lo is not None and lo > as_of and all(values[k] is None for k in range(j + 1, len(rows))):
            return _Res("before_history", [None], True, notes + n + [
                f"이 {what}은(는) {iso(rows[j].prom)} 공포 개정으로 생겼고 {iso(lo)} 시행 — 기준일에는 없었음"], j)

    for pos, i in enumerate(changes):
        lo, hi, n = effs[i]
        if hi is not None and hi <= as_of:
            if not older_in_force(pos):
                return _Res("unknown", [], True, notes + n + [f"시행일이 늦은 이전 개정분이 이 {what}에 섞여 있어 기준일 {what}을 분리할 수 없음"],
                            i, exists=exists_certain())
            return _Res("value", [values[i]], pos > 0, notes + n, i)
        if lo is not None and lo <= as_of:
            if not older_in_force(pos):
                return _Res("unknown", [], True, notes + n + [f"시행일이 다른 개정분이 섞여 있어 기준일 {what}을 분리할 수 없음"],
                            i, exists=exists_certain())
            return _Res("mixed", [values[i + 1], values[i]], True,
                        notes + n + [f"{iso(rows[i].prom)} 공포 개정이 이 {what}에 일부만 시행 중(나머지 {iso(hi) or '미정'} 시행) — "
                                     f"개정 전·후 {what} 모두와 맞는 부분만 확인 가능"], i)
        if lo is None:
            return _Res("unknown", [], True, notes + n + [f"{iso(rows[i].prom)} 공포 개정분의 이 {what} 시행일을 알 수 없음"], i,
                        exists=exists_certain() if (values[i] is not None and values[i + 1] is not None) else None)
        notes += [f"{iso(rows[i].prom)} 공포 개정(이 {what} 시행 {iso(lo)}{'' if hi == lo else '~' + (iso(hi) or '미정')})은 기준일에 아직 시행 전"] + n
    base_i = (changes[-1] + 1) if changes else 0
    in_force_k = next((k for k in range(base_i, len(rows)) if rows[k].eff <= as_of), None)
    if in_force_k is None:
        if exhausted:
            return _Res("before_history", [None], bool(changes), notes + [
                f"이 법령의 가장 오래된 판본도 {iso(rows[-1].eff)} 시행 — 기준일({iso(as_of)})에는 시행 전"], None)
        return None
    return _Res("value", [values[base_i]], bool(changes), notes, in_force_k)


def _block_for(row: _Row, head_blocks: List[Buchik], own_title: str) -> Optional[Buchik]:
    """그 판본을 만든 개정의 부칙 — 최신 판본에 실린 문언(뒤에 개정된 시행일 반영)을 우선, 없으면 그 판본의 것."""
    def pick(blocks):
        same = [b for b in blocks if b.promulgated == row.prom]
        exact = [b for b in same if row.number and b.number == row.number]
        return (exact or same or [None])[0]
    return pick(head_blocks) or pick(parse_buchik(row.body, own_title))


def unit_status_precise(commits: List[str], load, as_of: date, jo: int, sub: Optional[int],
                        hang: Optional[int], ho: Optional[str], mok: Optional[str], max_versions: int = 150,
                        own_title: str = "") -> Optional[UnitStatus]:
    """commits: 이 파일을 바꾼 커밋(최신순). load(commit) → (articles, meta, body) 또는 None(내려받기 실패).
    공포일·시행일은 각 판본 프론트매터에서 읽는다(얕은 클론 경계 커밋의 작성일은 다른 법령 것일 수 있다).
    판정 불가(이력 부족·로드 실패)면 None — 호출자가 이력 없음 모드로 내려간다."""
    if not commits:
        return None
    rows: List[_Row] = []
    head_blocks: List[Buchik] = []
    must_reach: Optional[date] = None
    stop_at = None
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
        rows.append(_Row(commit, prom, eff, str(meta.get("공포번호") or "").lstrip("0"), text,
                         art.title if art is not None else None, body, art))
        if idx == 0:
            head_blocks = parse_buchik(body, own_title)
            # 기준일 뒤에도 시행 전·미확정인 개정(이 단위를 가리킨 부칙 단서, 또는 본칙·타법개정분 자체)
            # → 그 개정 판본보다 앞까지 읽어야 그 개정이 이 단위를 바꿨는지 안다(시행일은 판본 순서와 무관)
            for b in head_blocks:
                if b.promulgated > as_of:
                    continue
                pend = False
                if explicit_coverage(b, jo, sub, hang, ho) or explicit_coverage(b, jo, sub, None, None):
                    lo, hi, _ = unit_effective(b, jo, sub, hang, ho)
                    pend = hi is None or hi > as_of
                lo, hi = block_span(b)
                pend = pend or (hi is not None and hi > as_of) or (hi is None and (as_of - b.promulgated).days <= _UNKNOWN_HORIZON_DAYS)
                if pend:
                    must_reach = b.promulgated if must_reach is None else min(must_reach, b.promulgated)
        if stop_at is not None:
            break                                # 멈춤 판본보다 한 판 더 오래된 것까지 읽어 멈춤 판본의 변경 여부를 안다
        if idx >= 1 and eff <= as_of and prom <= as_of - timedelta(days=400) and (must_reach is None or prom < must_reach):
            stop_at = idx
    if len(rows) < 2:
        return None
    exhausted = stop_at is None and len(rows) == len(commits)
    if stop_at is None and not exhausted:
        return None                              # 최대 판본 수를 넘겨도 판정 근거에 닿지 못함

    blocks = [_block_for(r, head_blocks, own_title) for r in rows]

    def eff_for(level_hang, level_ho, article_only=False):
        def f(i: int):
            b = blocks[i]
            if b is None:
                return rows[i].eff, rows[i].eff, [f"{iso(rows[i].prom)} 개정의 부칙을 찾지 못해 판본 시행일자로 판정"]
            return unit_effective(b, jo, sub, level_hang, level_ho, rows[i].eff, article_only=article_only)
        return f

    st = UnitStatus(state="unknown", precise=True, head_exists=rows[0].text is not None and not is_deleted_unit(rows[0].text))
    tr = _resolve(rows, [r.text for r in rows], unit_signature, eff_for(hang, ho), as_of, exhausted, "문언")
    if tr is None:
        return None
    st.notes += tr.notes
    st.pending_change = tr.pending
    if tr.idx is not None:
        st.version = rows[tr.idx].commit[:10]
    if tr.kind == "unknown":
        st.exists = tr.exists
        return st
    if tr.kind == "before_history":
        st.state = "absent"
        return st
    # 조문 제목은 따로 추적한다(제목만 바뀌는 개정 — 실측: 민법 제801조 '약혼연령' → '약혼 나이' 2023-06-28 시행)
    ttr = _resolve(rows, [r.title for r in rows], lambda t: normalize_for_match(t) if t is not None else None,
                   eff_for(None, None, article_only=True), as_of, exhausted, "제목")
    titles: List[Optional[str]] = []
    if ttr is not None and ttr.kind in ("value", "mixed"):
        titles = list(ttr.values)
        if ttr.kind == "mixed" or ttr.pending:
            st.notes += [n for n in ttr.notes if n not in st.notes]
    elif ttr is not None:
        st.notes += [n for n in ttr.notes if n not in st.notes] + ["기준일 조문 제목을 확정하지 못함"]
    st.titles = [t for t in titles if t is not None] if titles and all(t is not None for t in titles) else []
    st.title = st.titles[0] if len(set(st.titles)) == 1 else None
    if tr.kind == "mixed":
        merged = _merge_subunits(rows, tr.idx, blocks[tr.idx], jo, sub, hang, ho, mok, as_of)
        if merged is not None:
            st.notes.append("부칙 단서로 하위 항·호만 시행일이 달라, 기준일에 시행된 쪽 문언으로 조립함")
            return _finish(st, merged)
        old, new = tr.values
        if old is None or new is None or is_deleted_unit(old) or is_deleted_unit(new):
            st.notes.append("개정 전·후로 이 단위의 존재·삭제 여부가 갈려 기준일 상태를 확정할 수 없음")
            st.exists = None
            return st
        st.state = "mixed"
        st.texts = [old, new]
        st.exists = True
        return st
    return _finish(st, tr.values[0])


def _merge_subunits(rows: List[_Row], i: int, block: Optional[Buchik], jo: int, sub: Optional[int], hang: Optional[int],
                    ho: Optional[str], mok: Optional[str], as_of: date) -> Optional[str]:
    """부칙 단서가 인용 단위의 **하위 항·호 전체**만 가리키면(부분 수식어 없음) 기준일 문언을 정확히 조립할 수 있다:
    본칙이 시행됐으면 개정 후 문언에서 아직 시행 전인 하위 단위만 개정 전 문언으로 되돌린다(반대도 같음)."""
    if block is None or mok is not None or i is None:
        return None
    new_art, old_art = rows[i].art, rows[i + 1].art
    if new_art is None or old_art is None:
        return None
    main = rows[i].eff if block.other_law else (block.main_effective or rows[i].eff)
    subs: List[Tuple[Optional[int], Optional[str], date]] = []
    for ex in block.exceptions:
        if ex.applies:
            continue
        if ex.uncertain:
            return None
        cov = coverage(ex.units, jo, sub, hang, ho)
        if cov == "none":
            continue
        if cov == "full" or ex.effective is None:
            return None
        for u in ex.units:
            if coverage([u], jo, sub, hang, ho) == "none":
                continue
            if (u.jo, u.sub) != (jo, sub) or u.partial or u.jo_end or u.hang_end or u.ho_end:
                return None
            if hang is None and ho is None and (u.hang is not None or u.ho is not None):
                subs.append((u.hang, u.ho, ex.effective))
            elif hang is not None and ho is None and u.hang == hang and u.ho is not None:
                subs.append((hang, u.ho, ex.effective))
            else:
                return None
    if main is None or not subs:
        return None
    base_new = main <= as_of
    text = (new_art if base_new else old_art).unit_text(hang, ho, None)
    if text is None:
        return None
    for h, o, e in subs:
        if (e <= as_of) == base_new:
            continue
        src_art, dst_art = (new_art, old_art) if e <= as_of else (old_art, new_art)
        cur, put = dst_art.unit_text(h, o), src_art.unit_text(h, o)
        if cur is None or cur not in text:
            return None
        text = text.replace(cur, put if put is not None else "", 1)
    return text


def _finish(st: UnitStatus, text: Optional[str]) -> UnitStatus:
    if text is None:
        st.state = "absent"
    elif is_deleted_unit(text):
        st.state = "deleted"
        st.text = text
        st.texts = [text]
    else:
        st.state = "in_force"
        st.text = text
        st.texts = [text]
    return st


# ---------------------------------------------------------------------------
# 이력 없음(최신 공포본만)
# ---------------------------------------------------------------------------


def _line_markers(text: Optional[str]) -> List[Tuple[str, date]]:
    return amendment_markers((text or "").split("\n")[0])


def unit_status_heuristic(art, all_buchik: List[Buchik], head_prom: Optional[date], head_eff: Optional[date],
                          as_of: date, hang: Optional[int], ho: Optional[str], mok: Optional[str]) -> UnitStatus:
    """이력 없음: 최신 공포본이 기준일에 시행 중일 때만 판정한다. 확실하지 않으면 unknown(fail-closed)."""
    text = art.unit_text(hang, ho, mok) if art is not None else None
    st = UnitStatus(state="unknown", precise=False, version="HEAD",
                    head_exists=text is not None and not is_deleted_unit(text))
    by_prom = {}
    for b in all_buchik:
        by_prom.setdefault(b.promulgated, []).append(b)
    if art is not None and all_buchik:
        base = min(all_buchik, key=lambda b: b.promulgated)
        base_eff = base.main_effective or base.promulgated
        if as_of < base_eff:
            st.notes.append(f"기준일이 현행 법문의 출발점(부칙 {iso(base.promulgated)}, 시행 {iso(base_eff)})보다 앞섬 — "
                            "전부개정 전 판본은 이력(setup 전체 이력) 또는 DRF 로만 확인 가능")
            return st
    # 장·절 제목의 신설 표시가 기준일 뒤면 그 조문은 기준일에 없었다(조 번호는 밀리지 않는다)
    if art is not None:
        for h in getattr(art, "headings", []):
            for k, d in amendment_markers(h):
                if k != "신설":
                    continue
                effs = [unit_effective(b, art.jo, art.sub, None, None)[1] for b in by_prom.get(d, [])]
                if d > as_of or any(e is not None and e > as_of for e in effs):
                    st.state = "absent"
                    st.notes.append(f"이 조문이 속한 장·절이 {iso(d)} 신설 — 기준일에는 없었음")
                    return st
    if head_eff is None:
        st.notes.append("최신 공포본의 시행일을 알 수 없음")
        return st

    def marker_in_force(d: date) -> bool:
        effs = [unit_effective(b, art.jo, art.sub, None, None)[1] for b in by_prom.get(d, [])]
        return (bool(effs) and all(e is not None and e <= as_of for e in effs)) or (not effs and (as_of - d).days > 3 * 365)

    # '④ 삭제 <2009.5.21>' — 삭제가 기준일 전에 시행됐으면 최신 공포본이 시행 전이어도 기준일에 삭제 상태다
    # (삭제된 항·호는 번호를 옮기지 않고, 그 뒤 개정이 있었다면 표시가 새로 붙는다)
    if text is not None and is_deleted_unit(text):
        marks = _line_markers(text)
        if marks and all(marker_in_force(d) for _, d in marks):
            st.state, st.text, st.texts = "deleted", text, [text]
            st.notes.append(f"{iso(max(d for _, d in marks))} 삭제가 기준일 전에 시행됨")
            return st

    def evidence_prior(lines: List[str]) -> bool:
        for ln in lines:
            if is_deleted_unit(ln):
                continue                  # 삭제 표시는 존재의 근거가 아니다
            for k, d in _line_markers(ln):
                effs = [unit_effective(b, art.jo, art.sub, None, None)[1] for b in by_prom.get(d, [])]
                if (effs and all(e is not None and e <= as_of for e in effs)) or (not effs and (as_of - d).days > 3 * 365):
                    return True
        return False

    def exists_at_as_of() -> Optional[bool]:
        """기준일에 이 번호의 단위가 있었던 것이 확실한가(문언과 별개). 시행 전 개정이 이 단위(항·호)를 건드렸으면
        번호 이동일 수 있어 판단하지 않는다(실측: 근로기준법 제60조 ⑧ <개정 2020.3.31, 2026.6.9> 는 기준일에 ⑦)."""
        if art is None or text is None:
            return None
        if hang is None and ho is None:
            moved = [d for m in re.finditer(r"\[[^\]]*이동[^\]]*\]", art.text)
                     for d in (parse_date(x.group(0)) for x in _WHEN_DOT_RE.finditer(m.group(0))) if d]
            if any(not marker_in_force(d) for d in moved):
                return None
            return True if evidence_prior(art.text.split("\n")) else None
        if any(not marker_in_force(d) for _, d in _line_markers(text)):
            return None
        return True if evidence_prior([text]) else None

    if head_eff > as_of:
        # 최신 공포본이 기준일 뒤에 시행 — 신설·번호 이동이 표시 없이 들어올 수 있어 표시로는 판정 불가
        st.exists = exists_at_as_of()
        st.notes.append(f"최신 공포본이 {iso(head_eff)} 시행이라 기준일({iso(as_of)}) 문언을 이력 없이 확인할 수 없음 — "
                        "전체 이력 미러(`legal.py setup`) 또는 DRF 가 필요")
        return st
    # 최신 공포본이 시행 중이어도 그보다 먼저 공포된 개정이 기준일에 아직 시행 전일 수 있다(시행일은 공포 순서와 무관).
    # 그 개정이 바꾼 조항은 개정표시가 없을 수 있어(신설 호 등) 가려낼 수 없다 → 그런 개정이 있으면 판정하지 않는다.
    for b in sorted(all_buchik, key=lambda x: x.promulgated, reverse=True):
        lo, hi = block_span(b)
        if (hi is not None and hi > as_of) or (hi is None and (as_of - b.promulgated).days <= _UNKNOWN_HORIZON_DAYS):
            st.notes.append(f"{iso(b.promulgated)} 공포 개정{'(' + b.other_law + ')' if b.other_law else ''}의 시행일이 "
                            f"{'기준일 뒤(' + iso(hi) + ')' if hi else '미확정'}이라, 최신 공포본 문언 중 기준일에 시행 중인 부분을 "
                            "이력 없이 가려낼 수 없음 — 전체 이력 미러(`legal.py setup`) 또는 DRF 가 필요")
            st.exists = exists_at_as_of()
            return st
    if art is None or text is None:
        st.state = "absent"          # 최신 공포본이 이미 시행 중인데 없다
        return st
    # 최신 공포본은 기준일에 시행 중 — 늦게 시행되는 부칙 단서(전부·일부)와 시행 전 개정표시만 확인하면 된다
    ancestors = []
    if hang is not None and art.paragraphs.get(hang) is not None:
        ancestors.append(art.paragraphs[hang].text)
    if ho is not None:
        items = art.paragraphs[hang].items if (hang is not None and art.paragraphs.get(hang)) else art.items
        if ho in items and mok is not None:
            ancestors.append(items[ho].text)
    events = amendment_markers(text) + [m for a in ancestors for m in _line_markers(a)]
    if hang is None and ho is None:
        events += amendment_markers(art.text)
    pending: List[Tuple[str, date, Optional[date]]] = []
    for k, d in sorted(set(events), key=lambda x: x[1], reverse=True):
        bs = by_prom.get(d, [])
        if not bs:
            if (as_of - d).days <= 3 * 365:
                st.notes.append(f"{iso(d)} 개정의 부칙을 찾지 못해 시행일 확인 불가")
                return st
            continue
        for b in bs:
            lo, hi, notes = unit_effective(b, art.jo, art.sub, hang, ho)
            if hi is None or hi > as_of:
                pending.append((k, d, hi))
                st.notes += notes
    for b in all_buchik:
        if explicit_coverage(b, art.jo, art.sub, hang, ho):
            lo, hi, notes = unit_effective(b, art.jo, art.sub, hang, ho)
            if hi is None or hi > as_of:
                pending.append(("부칙", b.promulgated, hi))
                st.notes += notes
    if pending:
        k, d, eff = pending[0]
        st.pending_change = True
        st.notes.append(f"{iso(d)} 개정({k})이 이 조항에 {iso(eff) or '미정'}부터 시행 — 기준일 문언은 이력 없이 확인 불가")
        st.exists = exists_at_as_of()
        return st
    st.title = art.title
    st.titles = [art.title] if art.title is not None else []
    return _finish(st, text)
