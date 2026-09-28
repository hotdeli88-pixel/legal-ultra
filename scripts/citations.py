"""citations.py - 법령·판례·해석례 인용 추출 및 다중 출처 검증 (legal-ultra v2 Zero-Hallucination Gate).

판정(fail-closed):
  VERIFIED     출처에서 확인되고 인용 속성(조·항·호·목, 제목, 선고일, 법원, 인용문, 시행 여부)이 일치
  MISMATCH     존재하지만 인용 속성이 원문과 모순 (제목·선고일·법원·인용문 불일치, 삭제·폐지·미시행)
  NOT_FOUND    사용 가능한 공식 출처 어디에서도 확인되지 않음 (환각 의심 — 원문 확보 전 인용 금지)
  UNRESOLVED   무엇을 가리키는지 특정 불가 (약칭·법령명 없는 조문·선행어 없는 '같은 법')
  UNVERIFIABLE 출처 접근 불가로 확인 자체를 못함 (네트워크 차단, 미러 없음, 헌재 결정인데 API 불가)

문서 판정: PASS(인용 ≥1, 전부 VERIFIED) · FAIL(MISMATCH/NOT_FOUND/UNRESOLVED 존재) ·
          INCOMPLETE(실패는 없으나 UNVERIFIABLE 존재) · NO_CITATIONS(인용 0건 — 통과 아님)

v1(law 스킬 law_verify.py) 대비: '제76조의9'를 '제76조'로 검증하던 문제, 사건번호가 달라도 첫 검색결과로
통과시키던 문제, '제15조 및 제17조'의 두 번째 인용 누락, '2024년12월'을 사건번호로 오인, 접속 실패를
'불일치'로 표시, 인용 0건을 PASS 로 판정하던 문제를 모두 제거했다.
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_common import iso, normalize_for_match, normalize_law_name, parse_date, quote_found, today  # noqa: E402
from precedent_engine import CASE_CODES, CONSTITUTIONAL_CODES, is_constitutional  # noqa: E402

VERIFIED, MISMATCH, NOT_FOUND, UNRESOLVED, UNVERIFIABLE = (
    "VERIFIED", "MISMATCH", "NOT_FOUND", "UNRESOLVED", "UNVERIFIABLE")
FAILING = {MISMATCH, NOT_FOUND, UNRESOLVED}

# ---------------------------------------------------------------------------
# 인용 자료형
# ---------------------------------------------------------------------------


@dataclass
class StatuteCite:
    raw: str
    start: int
    end: int
    law: Optional[str]
    jo: int
    sub: Optional[int] = None
    hang: Optional[int] = None
    ho: Optional[str] = None
    mok: Optional[str] = None
    claimed_title: Optional[str] = None
    quote: Optional[str] = None
    historical: bool = False
    future_context: bool = False
    law_candidates: List[str] = field(default_factory=list)
    law_origin: str = "explicit"   # explicit | continuation | anaphora | alias | implicit | none

    @property
    def label(self) -> str:
        s = f"제{self.jo}조" + (f"의{self.sub}" if self.sub else "")
        if self.hang:
            s += f"제{self.hang}항"
        if self.ho:
            s += f"제{self.ho}호"
        if self.mok:
            s += f"{self.mok}목"
        return s

    def key(self) -> Tuple:
        return ("S", normalize_law_name(self.law or ""), self.jo, self.sub, self.hang, self.ho, self.mok,
                self.claimed_title, self.quote, self.historical, self.future_context)


@dataclass
class CaseCite:
    raw: str
    start: int
    end: int
    case_no: str
    court: Optional[str] = None
    decided: Optional[date] = None
    en_banc: bool = False
    quote: Optional[str] = None

    def key(self) -> Tuple:
        return ("C", self.case_no, self.court, self.decided, self.en_banc, self.quote)


@dataclass
class AuthorityCite:
    raw: str
    start: int
    end: int
    agenda_no: str
    target: str = "expc"
    quote: Optional[str] = None

    def key(self) -> Tuple:
        return ("A", self.target, self.agenda_no, self.quote)


@dataclass
class Finding:
    kind: str                # statute | case | authority
    citation: str
    status: str
    detail: str
    source: str = ""
    warnings: List[str] = field(default_factory=list)
    evidence: Dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 추출
# ---------------------------------------------------------------------------

_LAW_SUFFIX = r"(?:법률|법|령|규칙|규정|조례|헌법|시행령|시행규칙)"
_ART_RE = re.compile(
    r"(?<![0-9])(?P<je>제\s*)?(?P<jo>\d{1,4})\s*조(?!원)"
    r"(?:\s*의\s*(?P<sub>\d{1,3}))?"
    r"(?:\s*(?:제\s*)?(?P<hang>\d{1,3})\s*항)?"
    r"(?:\s*(?:제\s*)?(?P<ho>\d{1,3}(?:\s*의\s*\d{1,3})?)\s*호)?"
    r"(?:\s*(?P<mok>[가-하])\s*목)?"
)
_SAME_ART_RE = re.compile(r"(?:같은\s*조|동조)\s*(?:제\s*)?(?P<hang>\d{1,3})\s*항(?:\s*(?:제\s*)?(?P<ho>\d{1,3}(?:의\d{1,3})?)\s*호)?")
_CONT_GAP_RE = re.compile(r"^\s*(?:\([^()]{1,40}\))?\s*(?:[,ㆍ·/]|및|또는|와|과|이나|내지|부터|~|∼|-|,\s*및|,\s*또는)?\s*$")
_ANAPHORA_RE = re.compile(r"(?:(?<![가-힣])(?:같은|동)\s*법률?|(?<![가-힣])동법|(?<![가-힣])이\s*법)(?:\s*(시행령|시행규칙))?$")
_ALIAS_DEF_RE = re.compile(
    r"(?:[「『](?P<b>[^」』]{2,60})[」』]|(?P<p>[가-힣A-Za-z0-9ㆍ·]+(?:\s+[가-힣A-Za-z0-9ㆍ·]+){0,8}?(?:법률|법|령|규칙|규정)))"
    r"\s*\(\s*이하\s*[‘'\"“]?(?P<alias>[가-힣A-Za-z ]{1,12}?)[’'\"”]?\s*(?:이?라|로)\s*(?:한다|함)\s*\)")
_TITLE_RE = re.compile(r"^\s*\(([^()]{1,40})\)")
_QUOTE_RE = re.compile(r"[“\"]([^”\"]{6,600})[”\"]")
_SENT_END_RE = re.compile(r"(?<!\d)[.!?。](?:\s|$)")   # '2026. 6. 9.' 같은 날짜의 마침표는 문장 끝이 아니다
_PARTICLE_TAIL_RE = re.compile(r"(?<=[법률령칙정례])(?:상|의|에서|에|은|는|이|가|과|와)$")
_FUTURE_CTX_RE = re.compile(r"(시행\s*예정|시행예정|개정\s*법률|개정법|개정\s*후|신설\s*예정|공포\s*후)")

_COURT_RE = (r"(?P<court>대법원|헌법재판소|대판|헌재|"
             r"[가-힣]{2,12}(?:고등법원|지방법원|가정법원|행정법원|회생법원|특허법원)(?:\s*[가-힣]{1,8}지원)?|"
             r"[가-힣]{1,6}(?:고법|지법|행법|가법))")
_CODE_ALT = "|".join(re.escape(c) for c in CASE_CODES)
_CASE_RE = re.compile(
    rf"(?:{_COURT_RE}\s*)?"
    r"(?:(?P<date>\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?)\s*(?:선고|자)?\s*)?"
    rf"(?<![0-9가-힣A-Za-z])(?P<no>(?:(?:19|20|42|43)\d{{2}}|\d{{2}})(?:{_CODE_ALT})\d{{1,7}})(?![0-9])"
    r"(?:\s*(?P<enbanc>전원합의체))?"
)
_AUTH_RE = re.compile(r"(?:법제처[^\n]{0,30}?|안건번호\s*)(?P<no>\d{2}-\d{3,4})(?!\d)")

_STOP_TOKENS = {"및", "또는", "와", "과", "또한", "그리고", "따라서", "이에", "본", "위", "해당", "관련", "현행", "개정", "신", "즉",
                "시행", "신설", "종전", "구법", "신법", "예정", "개정된", "현재",
                "특히", "한편", "아울러", "다만", "따라", "의하여", "의한", "따른", "위하여", "근거", "적용"}


_PROSE_END_RE = re.compile(r"(?:은|는|이|가|을|를|의|에|에서|로|으로|와|과|도|만|인|된|한|던|적|상|중|및|등|시|때|며|고|서|면|나|께|로서|로써|하에|따라|따른|관한|대한|위한|의한|규정한)$")


def _is_prose_token(t: str) -> bool:
    """법령명 앞에서 떼어내도 되는 서술 어절인가(조사·어미로 끝나거나 접속·지시어)."""
    t = t.strip("、,.;:()[]\"'“”‘’")
    return (not t) or t in _STOP_TOKENS or t == "구" or bool(_PROSE_END_RE.search(t)) or not re.search(r"[가-힣]", t)


def _lookback_law(text: str, start: int, floor: int = 0) -> Tuple[Optional[str], List[str], bool, str]:
    """조문 인용 직전 문맥에서 법령명 후보를 찾는다. (대표명, 후보들(긴 것부터), 구법여부, 종류)
    floor: 직전 인용의 끝 — 그 앞의 글자(이전 인용)는 법령명에 섞지 않는다."""
    lb = text[max(0, start - 80, floor):start]
    nl = lb.rfind("\n", 0, len(lb.rstrip(" \t")))
    if nl >= 0:   # 법령명은 줄을 넘지 않는다(앞 줄 단어가 법령명 후보에 섞이지 않게)
        lb = lb[nl + 1:]
    lb = lb.rstrip()
    # 법령명 뒤 괄호(약칭 정의 '(이하 "법"이라 한다)', 영문명 등)는 떼고 본다
    lb = re.sub(r"\s*\([^()]{0,60}\)\s*$", "", lb).rstrip()
    if not lb:
        return None, [], False, "none"
    # 「법령명」
    if lb.endswith(("」", "』")):
        open_idx = max(lb.rfind("「"), lb.rfind("『"))
        if open_idx >= 0:
            name = lb[open_idx + 1:-1].strip()
            hist = bool(re.search(r"(?:^|\s)구\s*$", lb[:open_idx]))
            return name, [name], hist, "explicit"
    m = _ANAPHORA_RE.search(lb)
    if m:
        return None, [m.group(1) or ""], False, "anaphora"
    lb2 = _PARTICLE_TAIL_RE.sub("", lb)
    m = re.search(r"([가-힣A-Za-z0-9ㆍ·\s]{1,70})$", lb2)
    if not m:
        return None, [], False, "none"
    tail = m.group(1)
    tokens = tail.split()
    if not tokens or not re.search(_LAW_SUFFIX + r"$", tokens[-1]):
        return None, [], False, "none"
    tokens = tokens[-7:]
    hist = False
    cands: List[str] = []
    for i in range(len(tokens)):
        t = tokens[i:]
        if t[0] == "구":
            hist = True
            continue
        if t[0] in _STOP_TOKENS or re.search(r"(?:은|는|을|를|와|과|에서|에게|으로|로써|하며|하고|하여|되어|된|한|인|적)$", t[0]) and len(t) > 1:
            continue
        cands.append(" ".join(t))
    if not cands:
        return None, [], hist, "none"
    if len(tokens) >= 2 and tokens[-2] == "구":
        hist = True
    # 접미사만 남은 후보('법', '시행령')는 단독 법령명이 아니다
    solid = [c for c in cands if not re.fullmatch(r"(?:법률|법|령|규칙|규정|시행령|시행규칙|영)", c)]
    if not solid:
        return None, [cands[-1]], hist, "alias"
    return solid[0], solid, hist, "explicit"


def extract(text: str) -> Tuple[List[StatuteCite], List[CaseCite], List[AuthorityCite]]:
    statutes: List[StatuteCite] = []
    cases: List[CaseCite] = []
    auths: List[AuthorityCite] = []
    text = text or ""
    aliases: Dict[str, List[str]] = {}
    for am in _ALIAS_DEF_RE.finditer(text):
        if am.group("b"):
            aliases[am.group("alias").strip()] = [am.group("b").strip()]
        else:
            toks = am.group("p").split()
            aliases[am.group("alias").strip()] = [" ".join(toks[i:]) for i in range(len(toks))
                                                  if toks[i] not in _STOP_TOKENS]

    # ---- 판례 (먼저 뽑아 조문 오인과 겹치지 않게) ----
    case_spans: List[Tuple[int, int]] = []
    prev_case: Optional[CaseCite] = None
    for m in _CASE_RE.finditer(text):
        no = m.group("no")
        court = m.group("court")
        if court in ("대판",):
            court = "대법원"
        if court in ("헌재",):
            court = "헌법재판소"
        decided = parse_date(m.group("date")) if m.group("date") else None
        if prev_case and not court and not decided and _CONT_GAP_RE.match(text[prev_case.end:m.start()]):
            court, decided = prev_case.court, prev_case.decided
        end = m.end()
        tail = text[end:end + 12]
        tm = re.match(r"\s*(판결|결정|판례)", tail)
        if tm:
            end += tm.end()
        c = CaseCite(raw=text[m.start():end].strip(), start=m.start(), end=end, case_no=no, court=court,
                     decided=decided, en_banc=bool(m.group("enbanc")))
        cases.append(c)
        case_spans.append((m.start(), end))
        prev_case = c

    # ---- 해석례 ----
    for m in _AUTH_RE.finditer(text):
        auths.append(AuthorityCite(raw=m.group(0).strip(), start=m.start(), end=m.end(), agenda_no=m.group("no")))

    def in_case(pos: int) -> bool:
        return any(s <= pos < e for s, e in case_spans)

    # ---- 조문 ----
    prev: Optional[StatuteCite] = None
    last_law: Optional[str] = None
    last_law_para = -1
    para_start = 0
    for m in _ART_RE.finditer(text):
        if in_case(m.start()):
            continue
        before_char = text[m.start() - 1] if m.start() > 0 else " "
        if re.match(r"[가-힣]", before_char):
            # '근로기준법제60조'는 허용, '3조원'·'매출3조' 같은 비인용은 제외
            if not (m.group("je") and re.search(_LAW_SUFFIX + r"$", text[max(0, m.start() - 6):m.start()])):
                continue
        if "\n\n" in text[para_start:m.start()]:
            para_start = text.rfind("\n\n", 0, m.start())
        jo = int(m.group("jo"))
        sub = int(m.group("sub")) if m.group("sub") else None
        hang = int(m.group("hang")) if m.group("hang") else None
        ho = re.sub(r"\s+", "", m.group("ho")) if m.group("ho") else None
        law, cands, hist, origin = None, [], False, "none"
        gap = text[prev.end:m.start()] if prev else None
        if prev is not None and gap is not None and _CONT_GAP_RE.match(gap):
            law, cands, hist, origin = prev.law, list(prev.law_candidates), prev.historical, "continuation"
        else:
            floor = max([prev.end if prev else 0] + [e for s_, e in case_spans if e <= m.start()])
            name, cands, hist, origin = _lookback_law(text, m.start(), floor)
            if origin == "explicit":
                law = name
            elif origin == "anaphora":
                suffix = cands[0] if cands else ""
                if last_law:
                    base = re.sub(r"\s*(?:시행령|시행규칙)$", "", last_law)
                    law = f"{base} {suffix}".strip()
                    cands = [law]
            elif origin == "alias":
                token = cands[0] if cands else ""
                if token in aliases and aliases[token]:
                    law, cands, origin = aliases[token][0], list(aliases[token]), "alias"
                elif last_law and token in ("시행령", "시행규칙"):
                    base = re.sub(r"\s*(?:시행령|시행규칙)$", "", last_law)
                    law, cands, origin = f"{base} {token}", [f"{base} {token}"], "implicit"
                elif last_law and token in ("법", "법률"):
                    law, cands, origin = last_law, [last_law], "implicit"
            elif not m.group("je"):
                continue  # '750조' 처럼 '제'도 법령명도 없으면 인용으로 보지 않는다
            elif last_law and last_law_para == para_start:
                law, cands, origin = last_law, [last_law], "implicit"
        end = m.end()
        after = text[end:end + 60]
        title = None
        tm = _TITLE_RE.match(after)
        if tm and not re.search(r"\d{4}|이하|개정|신설|시행|선고|판결|결정|참조|위반|삭제|단서|본문|전단|후단", tm.group(1)):
            title = tm.group(1).strip()
        c = StatuteCite(raw=text[m.start():end].strip(), start=m.start(), end=end, law=law, jo=jo, sub=sub,
                        hang=hang, ho=ho, mok=m.group("mok"), claimed_title=title, historical=hist,
                        law_candidates=cands, law_origin=origin)
        c.future_context = bool(_FUTURE_CTX_RE.search(text[max(0, m.start() - 40):m.start()]))
        statutes.append(c)
        if law:
            last_law = law
            last_law_para = para_start
        prev = c
        # '제15조제1항 및 제2항' → 같은 조의 항 나열
        pos = end
        while True:
            im = re.match(r"\s*(?:[,ㆍ·]|및|또는|와|과)\s*제\s*(\d{1,3}(?:의\d{1,3})?)\s*호(?!\s*의)", text[pos:])
            if im and ho:
                c2 = StatuteCite(raw=text[pos:pos + im.end()].strip(), start=pos, end=pos + im.end(), law=law, jo=jo,
                                 sub=sub, hang=hang, ho=im.group(1), historical=hist, law_candidates=cands,
                                 law_origin="continuation")
                statutes.append(c2)
                pos += im.end()
                prev = c2
                continue
            hm = re.match(r"\s*(?:[,ㆍ·]|및|또는|와|과)\s*(?:같은\s*조\s*)?제\s*(\d{1,3})\s*항(?:\s*(?:제\s*)?(\d{1,3}(?:의\d{1,3})?)\s*호)?", text[pos:])
            if not hm or not (hang or c.hang):
                break
            c2 = StatuteCite(raw=text[pos:pos + hm.end()].strip(), start=pos, end=pos + hm.end(), law=law, jo=jo, sub=sub,
                             hang=int(hm.group(1)), ho=hm.group(2), historical=hist, law_candidates=cands,
                             law_origin="continuation")
            statutes.append(c2)
            pos += hm.end()
            prev = c2
    # '같은 조 제2항'
    for m in _SAME_ART_RE.finditer(text):
        before = [s for s in statutes if s.end <= m.start() and s.law]
        if not before or any(s.start <= m.start() < s.end for s in statutes):
            continue
        b = before[-1]
        statutes.append(StatuteCite(raw=m.group(0), start=m.start(), end=m.end(), law=b.law, jo=b.jo, sub=b.sub,
                                    hang=int(m.group("hang")), ho=m.group("ho"), historical=b.historical,
                                    law_candidates=b.law_candidates, law_origin="anaphora"))
    statutes.sort(key=lambda s: s.start)
    _attach_quotes(text, statutes, cases, auths)
    return statutes, cases, auths


def _attach_quotes(text: str, statutes, cases, auths) -> None:
    """인용문(“…”)은 바로 앞 인용에만 붙인다: 다음 인용(종류 무관)·줄바꿈 전까지, 인용 끝에서 120자 이내 시작."""
    allc = sorted(list(statutes) + list(cases) + list(auths), key=lambda x: x.start)
    for i, c in enumerate(allc):
        nxt = allc[i + 1].start if i + 1 < len(allc) else len(text)
        nl = text.find("\n", c.end)
        stop = min(nxt, nl if nl >= 0 else len(text), c.end + 700)
        qm = _QUOTE_RE.search(text[c.end:stop])
        # 같은 문장 안의 따옴표만 연결한다(“…규정한다. 한편 A는 “…”라고 진술” 같은 다른 문장의 인용은 제외)
        if qm and qm.start() <= 120 and not _SENT_END_RE.search(text[c.end:c.end + qm.start()]):
            c.quote = qm.group(1).strip()


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------


class Verifier:
    def __init__(self, statutes=None, precedents=None, api=None, as_of: Optional[date] = None,
                 use_api: bool = True, cross_check: bool = False):
        self.statutes = statutes if (statutes is not None and statutes.available) else None
        self.precedents = precedents if (precedents is not None and precedents.available) else None
        self.api = api if use_api else None
        self.as_of = as_of or today()
        self.cross_check = cross_check
        self._api_state: Optional[str] = None

    # ---- 출처 상태 ----
    def sources(self) -> Dict[str, object]:
        s: Dict[str, object] = {"as_of": iso(self.as_of)}
        s["legalize-kr"] = self.statutes.status() if self.statutes else {"available": False}
        s["precedent-kr"] = self.precedents.status() if self.precedents else {"available": False}
        s["drf_api"] = {"enabled": self.api is not None, "state": self._api_state or "미사용"}
        return s

    def _api_call(self, fn, *a, **k):
        from law_api import ApiError, ApiUnavailable, ApiAuthError
        if self.api is None:
            raise ApiUnavailable("API 비활성")
        try:
            r = fn(*a, **k)
            self._api_state = "사용 가능"
            return r
        except ApiAuthError as e:
            self._api_state = f"인증 실패: {e}"
            raise ApiUnavailable(str(e))
        except ApiUnavailable as e:
            self._api_state = f"접속 불가: {e}"
            raise
        except ApiError as e:
            self._api_state = f"응답 오류: {e}"
            raise ApiUnavailable(str(e))

    # ---- 조문 ----
    def verify_statute(self, c: StatuteCite) -> Finding:
        from law_api import ApiUnavailable
        cit = f"{c.law or '(법령명 없음)'} {c.label}" + (f"({c.claimed_title})" if c.claimed_title else "")
        if not c.law:
            why = {"none": "법령명이 없는 조문 인용", "alias": f"'{(c.law_candidates or [''])[0]}'가 가리키는 법령을 특정할 수 없음",
                   "anaphora": "'같은 법/동법'의 선행 법령이 없음"}.get(c.law_origin, "법령명 특정 불가")
            return Finding("statute", cit, UNRESOLVED, why + " — 정식 법령명으로 인용하세요")
        warnings: List[str] = []
        if c.law_origin in ("implicit",):
            warnings.append(f"법령명 생략 인용을 '{c.law}'로 추정함 — 명시 권장")
        doc = None
        res_note = ""
        if self.statutes:
            names = [c.law] + [x for x in c.law_candidates if x != c.law]
            full_tokens = max(names, key=len).split()
            for nm in names:
                r = self.statutes.resolve(nm)
                if r.status == "ok":
                    n = len(nm.split())
                    dropped = full_tokens[:-n] if len(full_tokens) > n and " ".join(full_tokens[-n:]) == nm else []
                    if dropped and not _is_prose_token(dropped[-1]):
                        return Finding("statute", cit, UNRESOLVED,
                                       f"법령명 앞 단어 '{dropped[-1]}'이(가) 법령명의 일부인지 불명확('{' '.join(dropped[-1:] + [nm])}'라는 법령은 없음) "
                                       f"— 「{r.doc.title}」처럼 정식 법령명을 낫표로 표기", warnings=warnings)
                    doc, res_note = r.doc, r.note
                    break
            if doc is None and self.api is not None:
                try:
                    fl = self._api_call(self.api.find_law, c.law, self.as_of)
                    if fl.get("status") == "ok":
                        r = self.statutes.resolve(fl["name"])
                        if r.status == "ok":
                            doc = r.doc
                            warnings.append(f"약칭/이칭 '{c.law}' → 정식명 '{fl['name']}' (DRF 확인) — 정식명 인용 권장")
                except ApiUnavailable:
                    pass
        if doc is not None:
            f = self._verify_local(c, doc, cit, warnings)
            if res_note:
                f.evidence["resolution"] = res_note
            if self.cross_check and self.api is not None and f.status == VERIFIED:
                self._cross_check(c, f)
            return f
        # 로컬로 못 찾음 → API 단독
        if self.api is not None:
            try:
                return self._verify_api(c, cit, warnings)
            except ApiUnavailable as e:
                if self.statutes:
                    sugg = self.statutes.resolve(c.law).candidates
                    return Finding("statute", cit, UNRESOLVED,
                                   f"legalize-kr 에 정확히 일치하는 법령명 없음(후보: {', '.join(sugg) or '없음'}); DRF 확인 불가({e})",
                                   warnings=warnings)
                return Finding("statute", cit, UNVERIFIABLE, f"법령 출처 없음: 로컬 미러 없음 + {e}", warnings=warnings)
        if self.statutes:
            sugg = self.statutes.resolve(c.law).candidates
            return Finding("statute", cit, UNRESOLVED,
                           f"정확히 일치하는 법령명 없음(약칭·오기·가공 법령 가능; 후보: {', '.join(sugg) or '없음'})",
                           warnings=warnings)
        return Finding("statute", cit, UNVERIFIABLE, "법령 출처 없음(legalize-kr 미러·DRF API 모두 불가)", warnings=warnings)

    def _verify_local(self, c: StatuteCite, doc, cit: str, warnings: List[str]) -> Finding:
        m = self.statutes
        cit = f"{doc.title} {c.label}" + (f"({c.claimed_title})" if c.claimed_title else "")
        gi = m.git_info()
        src = f"legalize-kr {doc.rel} (공포 {iso(doc.promulgated)}, 시행 {iso(doc.effective)}; 미러 {gi.get('head_date', '?')})"
        ev = {"law": doc.title, "file": doc.rel, "공포일자": iso(doc.promulgated), "시행일자": iso(doc.effective)}
        if doc.status == "폐지" and not c.historical:
            return Finding("statute", cit, MISMATCH, f"「{doc.title}」은(는) 폐지된 법령 — 구법 인용이면 '구'를 명시", src, warnings, ev)
        art = m.find_article(doc, c.jo, c.sub)
        if art is None:
            arts = m.articles(doc)
            rng = f"제{arts[0].jo}조~제{arts[-1].jo}조" if arts else "조문 없음"
            return Finding("statute", cit, NOT_FOUND, f"「{doc.title}」에 {c.label.split('제')[0] or ''}제{c.jo}조"
                           + (f"의{c.sub}" if c.sub else "") + f" 없음 (조문 범위 {rng})", src, warnings, ev)
        ev.update(article=art.label, title=art.title)
        if art.deleted:
            if art.deleted_on and doc.promulgated and art.deleted_on == doc.promulgated and doc.effective and doc.effective > self.as_of:
                warnings.append(f"시행예정 개정({iso(doc.effective)})으로 삭제 예정인 조문 — 현재는 시행 중")
            elif not c.historical:
                return Finding("statute", cit, MISMATCH, f"삭제된 조문(삭제 {iso(art.deleted_on)}) — 현행 인용 불가", src, warnings, ev)
        hang, ho = c.hang, c.ho
        if hang == 1 and not art.paragraphs:
            warnings.append("항이 하나뿐인 조문에 '제1항' 표기")
            hang = None
        if ho and hang is None and art.paragraphs and ho not in art.items:
            owners = [p.no for p in art.paragraphs.values() if ho in p.items]
            if len(owners) == 1:
                warnings.append(f"'제{ho}호'는 제{owners[0]}항 소속 — '제{owners[0]}항제{ho}호'로 표기 권장")
                hang = owners[0]
        unit = art.unit_text(hang, ho, c.mok)
        if unit is None:
            what = (f"제{hang}항" if hang and hang not in art.paragraphs else "") or (f"제{ho}호" if ho else "") or (f"{c.mok}목" if c.mok else "")
            have = f"항 {sorted(art.paragraphs)}" if art.paragraphs else "번호 있는 항 없음"
            return Finding("statute", cit, NOT_FOUND, f"「{doc.title}」 {art.label}에 {what or '해당 단위'} 없음 ({have})", src, warnings, ev)
        if c.claimed_title and art.title:
            a, b = normalize_for_match(c.claimed_title), normalize_for_match(art.title)
            if not (a == b or a in b or b in a):
                return Finding("statute", cit, MISMATCH, f"조문 제목 불일치: 인용 '({c.claimed_title})' ↔ 원문 '({art.title})'", src, warnings, ev)
        t = m.temporal_check(doc, art, unit, self.as_of, hang, ho)
        ev["temporal"] = t
        if t["state"] == "pending_new":
            if c.future_context:
                warnings.append("시행예정 조항(문맥상 명시됨): " + t["note"])
            else:
                return Finding("statute", cit, MISMATCH, "as-of 기준 미시행 조항: " + t["note"]
                               + " — 현행 조문으로 교체하거나 '시행예정'임을 명시", src, warnings, ev)
        elif t["state"] in ("pending_text", "pending_delete"):
            warnings.append(t["note"])
        elif t.get("note"):
            warnings.append(t["note"])
        if c.quote:
            if not quote_found(c.quote, unit) and not quote_found(c.quote, art.text):
                return Finding("statute", cit, MISMATCH, f"인용 문구가 {art.label} 원문과 불일치: “{c.quote[:60]}…”", src, warnings, ev)
            if t["state"] == "pending_text":
                warnings.append("인용 문구는 시행예정 판본 기준으로 대조됨 — 현행 문언 재확인 필요")
        return Finding("statute", cit, VERIFIED, f"「{doc.title}」 {art.label}({art.title or '제목 없음'}) 확인", src, warnings, ev)

    def _verify_api(self, c: StatuteCite, cit: str, warnings: List[str]) -> Finding:
        fl = self._api_call(self.api.find_law, c.law, self.as_of)
        if fl.get("status") != "ok":
            return Finding("statute", cit, NOT_FOUND,
                           f"국가법령정보 목록에 정확히 일치하는 법령명 없음(후보: {', '.join(x for x in fl.get('candidates', []) if x) or '없음'})",
                           "DRF eflaw", warnings)
        cit = f"{fl['name']} {c.label}" + (f"({c.claimed_title})" if c.claimed_title else "")
        body = self._api_call(self.api.law_articles, fl["mst"], fl.get("efYd"))
        src = f"DRF eflaw MST={fl['mst']} 시행 {fl.get('efYd')} ({fl.get('state')})"
        arts = [a for a in body["articles"] if a["jo"] == c.jo and a["sub"] == c.sub]
        if not arts:
            return Finding("statute", cit, NOT_FOUND, f"「{fl['name']}」에 제{c.jo}조" + (f"의{c.sub}" if c.sub else "") + " 없음", src, warnings)
        a = arts[0]
        if a["deleted"] and not c.historical:
            return Finding("statute", cit, MISMATCH, "삭제된 조문", src, warnings)
        if c.hang and a["paragraphs"] and c.hang not in a["paragraphs"]:
            return Finding("statute", cit, NOT_FOUND, f"제{c.hang}항 없음 (항 {sorted(a['paragraphs'])})", src, warnings)
        if c.ho:
            keys = [k.split(":", 1)[1] for k in a["items"]]
            if c.ho not in keys:
                return Finding("statute", cit, NOT_FOUND, f"제{c.ho}호 없음", src, warnings)
        if c.claimed_title and a["title"]:
            x, y = normalize_for_match(c.claimed_title), normalize_for_match(a["title"])
            if not (x == y or x in y or y in x):
                return Finding("statute", cit, MISMATCH, f"조문 제목 불일치: '({c.claimed_title})' ↔ '({a['title']})'", src, warnings)
        eff = parse_date(a.get("시행일자"))
        if eff and eff > self.as_of and not c.future_context:
            return Finding("statute", cit, MISMATCH, f"조문 시행일 {iso(eff)} 미도래", src, warnings)
        if c.quote and not quote_found(c.quote, a["text"]):
            return Finding("statute", cit, MISMATCH, f"인용 문구 불일치: “{c.quote[:60]}…”", src, warnings)
        return Finding("statute", cit, VERIFIED, f"「{fl['name']}」 제{c.jo}조" + (f"의{c.sub}" if c.sub else "") + f"({a['title']}) 확인",
                       src, warnings, {"law": fl["name"], "title": a["title"], "조문시행일자": a.get("시행일자")})

    def _cross_check(self, c: StatuteCite, f: Finding) -> None:
        from law_api import ApiUnavailable
        try:
            g = self._verify_api(c, f.citation, [])
        except ApiUnavailable as e:
            f.warnings.append(f"DRF 교차확인 불가: {e}")
            return
        if g.status != VERIFIED:
            f.warnings.append(f"⚠ 출처 충돌: legalize-kr 는 확인, DRF 는 {g.status}({g.detail}) — 미러 최신성 확인 필요")
        else:
            f.source += " + DRF 교차확인"

    # ---- 판례 ----
    def verify_case(self, c: CaseCite) -> Finding:
        from law_api import ApiUnavailable
        cit = " ".join(x for x in [c.court or "", f"{c.decided.year}. {c.decided.month}. {c.decided.day}." if c.decided else "", c.case_no] if x)
        if is_constitutional(c.case_no):
            if c.court and c.court != "헌법재판소":
                return Finding("case", cit, MISMATCH, f"헌법재판소 사건번호인데 법원이 '{c.court}'로 표기됨")
            try:
                hits = self._api_call(self.api.constitutional_by_number, c.case_no) if self.api else None
            except ApiUnavailable as e:
                return Finding("case", cit, UNVERIFIABLE, f"헌재 결정은 precedent-kr 에 없어 DRF(detc) 필요 — {e}")
            if hits is None:
                return Finding("case", cit, UNVERIFIABLE, "헌재 결정 확인에는 DRF API 가 필요(비활성)")
            if not hits:
                return Finding("case", cit, NOT_FOUND, "국가법령정보 헌재결정례에서 사건번호 미확인", "DRF detc")
            h = hits[0]
            d = parse_date(h.get("종국일자"))
            if c.decided and d and c.decided != d:
                return Finding("case", cit, MISMATCH, f"선고일 불일치: 인용 {iso(c.decided)} ↔ 실제 {iso(d)}", "DRF detc")
            warnings: List[str] = []
            if c.quote:
                body = self._api_call(self.api.constitutional_body, h.get("헌재결정례일련번호", "")) or {}
                src_text = "\n".join(body.get(k, "") for k in ("판시사항", "결정요지", "전문"))
                if src_text and not quote_found(c.quote, src_text):
                    return Finding("case", cit, MISMATCH, f"인용 문구가 결정문과 불일치: “{c.quote[:60]}…”", "DRF detc")
            return Finding("case", cit, VERIFIED, f"헌법재판소 {iso(d)} {h.get('사건번호')} {h.get('사건명', '')}", "DRF detc",
                           warnings, {"사건명": h.get("사건명"), "종국일자": iso(d)})
        if c.decided and c.decided > self.as_of:
            return Finding("case", cit, MISMATCH, f"선고일({iso(c.decided)})이 기준일({iso(self.as_of)}) 이후 — 불가능한 날짜")
        # 법원 판례
        if self.precedents:
            f = self._verify_case_local(c, cit)
            if f is not None:
                return f
        if self.api is not None:
            try:
                return self._verify_case_api(c, cit)
            except ApiUnavailable as e:
                if self.precedents:
                    return self._case_missing_local(c, cit, f"DRF 확인 불가({e})")
                return Finding("case", cit, UNVERIFIABLE, f"판례 출처 없음: precedent-kr 미러 없음 + {e}")
        if self.precedents:
            return self._case_missing_local(c, cit, "")
        return Finding("case", cit, UNVERIFIABLE, "판례 출처 없음(precedent-kr 미러·DRF API 모두 불가)")

    def _case_missing_local(self, c: CaseCite, cit: str, extra: str) -> Finding:
        head = self.precedents.latest_decision()
        if c.decided and head and c.decided > head - timedelta(days=21):
            return Finding("case", cit, UNVERIFIABLE, f"precedent-kr(기준 {iso(head)})보다 최근 선고 — 미반영 가능 {extra}".strip())
        return Finding("case", cit, NOT_FOUND,
                       "precedent-kr(국가법령정보 공개판례 미러)에서 사건번호 미확인 — 가공·오기 의심, 미공개 판결이면 원문 확보 전 인용 금지 "
                       + extra, "precedent-kr")

    def _verify_case_local(self, c: CaseCite, cit: str) -> Optional[Finding]:
        entries = self.precedents.lookup(c.case_no)
        if not entries:
            return None
        src = "precedent-kr"
        cands = entries
        if c.court:
            want = c.court.replace(" ", "")
            cands = [e for e in entries if e.court.replace(" ", "") == want or (want == "대법원" and e.grade == "대법원")
                     or want in e.court.replace(" ", "") or e.court.replace(" ", "") in want]
            if not cands:
                return Finding("case", cit, MISMATCH, "법원 불일치: 인용 '%s' ↔ 실제 %s" % (
                    c.court, ", ".join(sorted({f'{e.court}({iso(e.date)})' for e in entries}))), src)
        if c.decided:
            dated = [e for e in cands if e.date == c.decided]
            if not dated:
                return Finding("case", cit, MISMATCH, "선고일 불일치: 인용 %s ↔ 실제 %s" % (
                    iso(c.decided), ", ".join(sorted({f'{e.court} {iso(e.date)}' for e in cands}))), src)
            cands = dated
        if len({(e.court, e.date) for e in cands}) > 1 and not c.court:
            warn_multi = "동일 사건번호가 여러 법원에 존재(하급심) — 법원·선고일을 함께 표기하세요: " + ", ".join(
                sorted({f"{e.court} {iso(e.date)}" for e in cands}))
        else:
            warn_multi = ""
        e = sorted(cands, key=lambda x: (not x.primary, x.path))[0]
        warnings = [warn_multi] if warn_multi else []
        doc = None
        if c.quote or not e.primary or c.en_banc:
            doc = self.precedents.read(e)
            if doc is None and self.api is not None:
                warnings.append("본문을 로컬에서 읽지 못함(부분 클론 오프라인) — DRF 본문으로 대조 시도")
        if not e.primary and doc is not None:
            if c.case_no not in self.precedents.case_numbers_in_meta(doc):
                return None
        title = str((doc or {}).get("meta", {}).get("사건명", "")) if doc else ""
        if c.quote:
            text = ""
            if doc:
                s = doc["sections"]
                text = "\n".join(s.get(k, "") for k in ("판시사항", "판결요지", "판례내용")) or doc["text"]
            if text:
                if not quote_found(c.quote, text):
                    return Finding("case", cit, MISMATCH, f"판시 인용문이 판결문과 불일치: “{c.quote[:60]}…” (사건명: {title})", src, warnings)
            else:
                warnings.append("인용문 대조 불가(본문 미확보)")
        if c.en_banc and doc and "전원합의체" not in doc["text"] and "전원합의체" not in title:
            warnings.append("'전원합의체' 표기가 판결문에서 확인되지 않음")
        return Finding("case", cit, VERIFIED, f"{e.court} {iso(e.date)} 선고 {c.case_no}" + (f" [{title}]" if title else ""),
                       f"{src} {e.path}", warnings, {"court": e.court, "date": iso(e.date), "path": e.path, "사건명": title})

    def _verify_case_api(self, c: CaseCite, cit: str) -> Finding:
        hits = self._api_call(self.api.precedent_by_number, c.case_no)
        if not hits:
            if self.precedents:
                return self._case_missing_local(c, cit, "(DRF 판례 목록에서도 미확인)")
            return Finding("case", cit, NOT_FOUND, "국가법령정보 판례 목록(nb 사건번호 검색)에서 미확인 — 미공개 판결이면 원문 확보 전 인용 금지", "DRF prec")
        if c.court:
            hh = [h for h in hits if c.court.replace(" ", "") in (h.get("법원명", "") + h.get("사건번호", "")).replace(" ", "")]
            if not hh:
                return Finding("case", cit, MISMATCH, f"법원 불일치: 인용 '{c.court}' ↔ 실제 {', '.join(h.get('법원명') or '?' for h in hits)}", "DRF prec")
            hits = hh
        if c.decided:
            hh = [h for h in hits if parse_date(h.get("선고일자")) == c.decided]
            if not hh:
                return Finding("case", cit, MISMATCH, f"선고일 불일치: 인용 {iso(c.decided)} ↔ 실제 {', '.join(iso(parse_date(h.get('선고일자'))) for h in hits)}", "DRF prec")
            hits = hh
        h = hits[0]
        warnings: List[str] = []
        if c.quote:
            body = self._api_call(self.api.precedent_body, h.get("판례일련번호", "")) or {}
            text = "\n".join(body.get(k, "") for k in ("판시사항", "판결요지", "판례내용"))
            if not text:
                warnings.append("판결 본문 미제공(데이터출처에 따라 본문 없음) — 인용문 대조 불가")
            elif not quote_found(c.quote, text):
                return Finding("case", cit, MISMATCH, f"판시 인용문이 판결문과 불일치: “{c.quote[:60]}…”", "DRF prec")
        return Finding("case", cit, VERIFIED, f"{h.get('법원명') or ''} {iso(parse_date(h.get('선고일자')))} {h.get('사건번호')} [{h.get('사건명', '')}]",
                       f"DRF prec ID={h.get('판례일련번호')}", warnings, {"사건명": h.get("사건명")})

    # ---- 해석례 ----
    def verify_authority(self, a: AuthorityCite) -> Finding:
        from law_api import ApiUnavailable
        cit = f"법제처 해석 안건 {a.agenda_no}"
        if self.api is None:
            return Finding("authority", cit, UNVERIFIABLE, "해석례 확인에는 DRF API 필요(비활성)")
        try:
            hits = self._api_call(self.api.interpretation_by_number, a.agenda_no, a.target)
        except ApiUnavailable as e:
            return Finding("authority", cit, UNVERIFIABLE, f"DRF 확인 불가: {e}")
        if not hits:
            return Finding("authority", cit, NOT_FOUND, "국가법령정보 법령해석례에서 안건번호 미확인", "DRF expc")
        h = hits[0]
        if a.quote:
            body = self._api_call(self.api.interpretation_body, h.get("법령해석례일련번호", ""), a.target) or {}
            text = "\n".join(body.get(k, "") for k in ("질의요지", "회답", "이유"))
            if text and not quote_found(a.quote, text):
                return Finding("authority", cit, MISMATCH, f"인용 문구가 해석례와 불일치: “{a.quote[:60]}…”", "DRF expc")
        return Finding("authority", cit, VERIFIED, f"{h.get('안건명', '')} (회신 {h.get('회신일자', '')})", "DRF expc")

    # ---- 문서 ----
    def verify_text(self, text: str, evidence_text: Optional[str] = None, workers: int = 6) -> Dict[str, object]:
        statutes, cases, auths = extract(text)
        jobs: List[Tuple[str, object]] = []
        seen: Dict[Tuple, int] = {}
        for c in statutes + cases + auths:
            k = c.key()
            if k in seen:
                continue
            seen[k] = len(jobs)
            jobs.append((type(c).__name__, c))

        def run(job):
            kind, c = job
            if kind == "StatuteCite":
                return self.verify_statute(c)
            if kind == "CaseCite":
                return self.verify_case(c)
            return self.verify_authority(c)

        if jobs:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as ex:
                findings = list(ex.map(run, jobs))
        else:
            findings = []
        closed = []
        if evidence_text is not None:
            ev_s, ev_c, _ = extract(evidence_text)

            def skeys(x: StatuteCite):
                return {(normalize_law_name(n), x.jo, x.sub) for n in ([x.law] if x.law else []) + list(x.law_candidates)}
            ev_keys = set().union(*[skeys(x) for x in ev_s]) if ev_s else set()
            ev_cases = {x.case_no for x in ev_c}
            for (kind, c), f in zip(jobs, findings):
                if kind == "CaseCite":
                    outside = c.case_no not in ev_cases
                elif kind == "StatuteCite":
                    outside = not (skeys(c) & ev_keys)
                else:
                    outside = False
                if outside:
                    f.warnings.append("증거 패킷(evidence)에 없는 인용 — 조사관 검증 범위 밖")
                    closed.append(f.citation)
        return self._summarize(findings, closed)

    def _summarize(self, findings: List[Finding], closed: List[str]) -> Dict[str, object]:
        counts = {s: sum(1 for f in findings if f.status == s) for s in (VERIFIED, MISMATCH, NOT_FOUND, UNRESOLVED, UNVERIFIABLE)}
        if not findings:
            verdict = "NO_CITATIONS"
        elif any(f.status in FAILING for f in findings):
            verdict = "FAIL"
        elif counts[UNVERIFIABLE]:
            verdict = "INCOMPLETE"
        else:
            verdict = "PASS"
        return {"verdict": verdict, "as_of": iso(self.as_of), "total": len(findings), "counts": counts,
                "outside_evidence": closed, "sources": self.sources(),
                "findings": [asdict(f) for f in findings]}


# ---------------------------------------------------------------------------
# 보고서
# ---------------------------------------------------------------------------

_BADGE = {"PASS": "✅ PASS — 모든 인용 확인", "FAIL": "❌ FAIL — 불일치·미확인 인용 존재",
          "INCOMPLETE": "⚠️ INCOMPLETE — 일부 인용을 출처 부재로 검증하지 못함",
          "NO_CITATIONS": "⚠️ NO_CITATIONS — 검증할 인용이 없음(통과 아님)"}
_ICON = {VERIFIED: "✅ 확인", MISMATCH: "❌ 불일치", NOT_FOUND: "❌ 미확인", UNRESOLVED: "❌ 특정불가", UNVERIFIABLE: "⚠️ 검증불가"}


def render_markdown(rep: Dict[str, object], title: str = "인용 검증 보고서") -> str:
    src = rep["sources"]
    lk, pk, api = src.get("legalize-kr", {}), src.get("precedent-kr", {}), src.get("drf_api", {})
    c = rep["counts"]
    lines = [f"## 🛡️ {title} (legal-ultra citation verifier)", "",
             f"- **판정**: {_BADGE[rep['verdict']]}",
             f"- **기준일(as-of)**: {rep['as_of']}",
             f"- **출처**: legalize-kr {'사용(미러 ' + str(lk.get('head_date')) + ', 이력 ' + ('있음' if lk.get('history') else '없음') + ')' if lk.get('available') else '없음'}"
             f" · precedent-kr {'사용(최신 선고 ' + str(pk.get('latest_decision')) + ')' if pk.get('available') else '없음'}"
             f" · DRF API {api.get('state') if api.get('enabled') else '비활성'}",
             f"- **인용 {rep['total']}건**: 확인 {c[VERIFIED]} · 불일치 {c[MISMATCH]} · 미확인 {c[NOT_FOUND]} · 특정불가 {c[UNRESOLVED]} · 검증불가 {c[UNVERIFIABLE]}",
             ""]
    if rep["findings"]:
        lines += ["| # | 인용 | 판정 | 근거·사유 | 출처 |", "|---:|---|:---:|---|---|"]
        for i, f in enumerate(rep["findings"], 1):
            detail = f["detail"].replace("|", "\\|")
            if f["warnings"]:
                detail += "<br>⚠ " + "<br>⚠ ".join(w.replace("|", "\\|") for w in f["warnings"] if w)
            lines.append(f"| {i} | {f['citation'].replace('|', '/')} | {_ICON[f['status']]} | {detail} | {f['source'].replace('|', '/')} |")
        lines.append("")
    if rep.get("outside_evidence"):
        lines.append(f"- 증거 패킷 밖 인용 {len(rep['outside_evidence'])}건: 감사관 확인 필요")
    lines.append("> 이 보고서는 스크립트가 출처(legalize-kr·precedent-kr·국가법령정보 DRF)와 대조해 자동 생성한 것이며, "
                 "법리 판단의 타당성까지 보증하지 않습니다.")
    return "\n".join(lines)


def dumps(rep: Dict[str, object]) -> str:
    return json.dumps(rep, ensure_ascii=False, indent=2, default=str)
