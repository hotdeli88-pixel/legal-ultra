"""citations.py - 법령·판례·해석례 인용 추출 및 다중 출처 검증 (legal-ultra v2 Zero-Hallucination Gate).

판정(fail-closed):
  VERIFIED     출처에서 확인되고 인용 속성(조·항·호·목, 제목, 선고일, 법원, 인용문, 기준일 시행 여부)이 일치
  MISMATCH     존재하지만 인용 속성이 원문과 모순 (제목·선고일·법원·인용문 불일치, 삭제·폐지·기준일 미시행)
  NOT_FOUND    사용 가능한 공식 출처 어디에서도 확인되지 않음 (환각 의심 — 원문 확보 전 인용 금지)
  UNRESOLVED   무엇을 가리키는지 특정 불가 (정의 없는 약칭·법령명 없는 조문·선행어 없는 '같은 법'·출처 없는 원문 인용)
  UNVERIFIABLE 출처 접근 불가·이력 부족으로 확인 자체를 못함 (네트워크 차단, 미러 없음, 본문 미확보, 기준일 문언 미확정)

문서 판정: PASS(인용 ≥1, 전부 VERIFIED) · FAIL(MISMATCH/NOT_FOUND/UNRESOLVED 존재) ·
          INCOMPLETE(실패는 없으나 UNVERIFIABLE 존재) · NO_CITATIONS(인용 0건 — 통과 아님)

추출 원칙
  - 따옴표 구간을 먼저 찾는다(“…” 는 중첩까지 균형 있게, "…", 12자 이상의 ‘…’, 콜론 뒤 인용 블록 '>').
    인용문 안의 조문·판례 표기는 인용문의 일부로 보고 따로 검증하지 않는다(인용문 전체가 원문과 대조된다).
  - 인용문은 동사로 성격을 가른다: 규정·판시·판단·설시·해석·회신 → 원문 인용(반드시 출처에 붙여 대조),
    주장·진술·항변·발언 → 당사자 말(대조하지 않음). 원문 인용인데 출처를 특정 못 하면 UNRESOLVED.
  - 출처 연결: 같은 문장의 앞 인용 → “…”(출처) → 같은 문장의 뒤 인용 → '위 판결은/이 조항은' → 앞 줄이
    '다음과 같이 …:' 로 끝나는 경우의 다음 줄 인용.
  - 법령명 없는 '제N조'는 같은 문단의 앞 법령으로 추정하지 않는다(다른 법 조문을 엉뚱한 법으로 검증하던 문제).
    약칭은 문서 안에서 '(이하 "…"이라 한다)'로 정의된 경우에만, 정의 위치 이후에만 쓴다.
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_common import (  # noqa: E402
    CIRCLED, CIRCLED_CHARS, iso, normalize_for_match, normalize_law_name, parse_date, prep_text, quote_found, today,
)
from precedent_engine import CASE_CODES, is_constitutional  # noqa: E402

VERIFIED, MISMATCH, NOT_FOUND, UNRESOLVED, UNVERIFIABLE = (
    "VERIFIED", "MISMATCH", "NOT_FOUND", "UNRESOLVED", "UNVERIFIABLE")
FAILING = {MISMATCH, NOT_FOUND, UNRESOLVED}

Unit = Tuple[int, Optional[int], Optional[int], Optional[str], Optional[str]]   # 조, 조의, 항, 호, 목

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
    quotes: List[str] = field(default_factory=list)
    quote_units: List[Unit] = field(default_factory=list)   # 인용문을 대조할 단위(나열 인용이면 전부)
    historical: bool = False
    hist_date: Optional[date] = None      # '(YYYY. M. D. … 개정되기 전의 것)' — 그 전날 시행 판본으로 대조
    future_context: bool = False
    law_candidates: List[str] = field(default_factory=list)
    law_origin: str = "explicit"          # explicit | continuation | anaphora | alias | none
    problem: Optional[str] = None         # 추출 단계에서 이미 특정 불가로 판단한 사유
    group: int = -1
    in_quote: bool = False

    @property
    def quote(self) -> Optional[str]:
        return self.quotes[0] if self.quotes else None

    @property
    def unit(self) -> Unit:
        return (self.jo, self.sub, self.hang, self.ho, self.mok)

    @property
    def label(self) -> str:
        s = f"제{self.jo}조" + (f"의{self.sub}" if self.sub else "")
        if self.hang:
            s += f"제{self.hang}항"
        if self.ho:
            s += "제" + (self.ho.replace("의", "호의") if "의" in self.ho else f"{self.ho}호")
        if self.mok:
            s += f"{self.mok}목"
        return s

    def key(self) -> Tuple:
        return ("S", normalize_law_name(self.law or ""), self.jo, self.sub, self.hang, self.ho, self.mok,
                self.claimed_title, tuple(self.quotes), tuple(self.quote_units), self.historical, self.hist_date,
                self.future_context, self.problem, self.law_origin if not self.law else "")


@dataclass
class CaseCite:
    raw: str
    start: int
    end: int
    case_no: str
    court: Optional[str] = None
    decided: Optional[date] = None
    en_banc: bool = False
    quotes: List[str] = field(default_factory=list)
    body_ok: bool = False     # 인용 문맥이 '판결 이유/설시/판단 부분'을 밝혔는가(아니면 판시사항·판결요지와만 대조)
    in_quote: bool = False
    has_tail: bool = False    # 뒤에 '판결/결정'이 붙었는가(나열 인용의 선고일 상속 판단용)

    @property
    def quote(self) -> Optional[str]:
        return self.quotes[0] if self.quotes else None

    def key(self) -> Tuple:
        return ("C", self.case_no, self.court, self.decided, self.en_banc, tuple(self.quotes), self.body_ok)


@dataclass
class AuthorityCite:
    raw: str
    start: int
    end: int
    agenda_no: str
    target: str = "expc"
    quotes: List[str] = field(default_factory=list)
    ministry: bool = False    # 부처 행정해석 문서번호(근로기준정책과-1234 등)
    in_quote: bool = False

    @property
    def quote(self) -> Optional[str]:
        return self.quotes[0] if self.quotes else None

    def key(self) -> Tuple:
        return ("A", self.target, self.agenda_no, tuple(self.quotes), self.ministry)


@dataclass
class QuoteSpan:
    start: int                # 여는 따옴표 위치
    end: int                  # 닫는 따옴표 다음 위치
    text: str                 # 따옴표 안
    kind: str                 # curly | straight | single | block
    verb: Optional[str] = None      # legal | party | None
    body_ok: bool = False
    attached: bool = False


@dataclass
class Finding:
    kind: str                # statute | case | authority | quote
    citation: str
    status: str
    detail: str
    source: str = ""
    warnings: List[str] = field(default_factory=list)
    evidence: Dict[str, object] = field(default_factory=dict)


@dataclass
class Extraction:
    text: str
    statutes: List[StatuteCite]
    cases: List[CaseCite]
    auths: List[AuthorityCite]
    orphans: List[QuoteSpan]
    spans: List[QuoteSpan]

    def citations(self) -> List[object]:
        return sorted(list(self.statutes) + list(self.cases) + list(self.auths), key=lambda c: c.start)


# ---------------------------------------------------------------------------
# 정규식
# ---------------------------------------------------------------------------

_LAW_SUFFIX = r"(?:법률|법|령|규칙|규정|조례|헌법|시행령|시행규칙)"
_MOK_CHARS = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허"
_MOK_TAIL = r"(?![적표록차격소재숨욕장사요공동련마수축화걸구])"   # '목적·목표·목록…' 같은 일반어 제외
_CIRC = "[" + CIRCLED_CHARS + "]"
_HO = r"(?P<ho>\d{1,3})(?:\s*의\s*(?P<hoa>\d{1,3}))?\s*호(?:의(?P<hob>\d{1,3})(?!\d))?"

_ART_RE = re.compile(
    r"(?<![0-9])(?P<je>제\s*)?(?P<jo>\d{1,4})\s*조(?!\s*원(?![가-힣]))"      # '3조 원'(금액)은 제외, '제2조 원칙'은 인용
    r"(?:\s*의\s*(?P<sub>\d{1,3})(?!\d))?"
    r"(?:\s*(?:제\s*)?(?:(?P<hang>\d{1,3})\s*항|(?P<hangc>" + _CIRC + r")(?:\s*항)?))?"
    r"(?:\s*(?:제\s*)?" + _HO + r")?"
    r"(?:\s*(?P<mok>[" + _MOK_CHARS + r"])목" + _MOK_TAIL + r")?")
_ENUM_SEP = r"\s*(?:[,ㆍ·]|및|또는|와|과|이나|내지|부터|~|∼)\s*"
_ENUM_HO_RE = re.compile(_ENUM_SEP + r"(?:같은\s*항\s*)?제\s*" + _HO
                         + r"(?:\s*(?P<mok>[" + _MOK_CHARS + r"])목" + _MOK_TAIL + r")?")
_ENUM_HANG_RE = re.compile(
    _ENUM_SEP + r"(?:같은\s*조\s*)?(?:제\s*(?P<hang>\d{1,3})\s*항|(?P<hangc>" + _CIRC + r")(?:\s*항)?)"
    r"(?:\s*(?:제\s*)?" + _HO + r")?")
_ENUM_MOK_RE = re.compile(_ENUM_SEP + r"(?P<mok>[" + _MOK_CHARS + r"])목" + _MOK_TAIL)
_SAME_UNIT_RE = re.compile(
    r"(?<![가-힣])(?:같은\s*(?P<l1>조|항|호)|동(?P<l2>조|항|호))\s*"
    r"(?:(?:제\s*(?P<hang>\d{1,3})\s*항|(?P<hangc>" + _CIRC + r")(?:\s*항)?))?"
    r"(?:\s*(?:제\s*)?" + _HO + r")?"
    r"(?:\s*(?P<mok>[" + _MOK_CHARS + r"])목" + _MOK_TAIL + r")?")
_CONT_GAP_RE = re.compile(r"^\s*(?:\([^()]{1,40}\))?\s*(?:[,ㆍ·/]|및|또는|와|과|이나|내지|부터|~|∼|-|,\s*및|,\s*또는)?\s*$")
_ANAPHORA_RE = re.compile(
    r"(?:(?<![가-힣])(?:같은|동)\s*(?P<kind>법률|법|영|령|규칙)|(?<![가-힣])동법|(?<![가-힣])(?:이|본)\s*법)"
    r"(?:\s*(?P<suffix>시행령|시행규칙))?$")
_ALIAS_DEF_RE = re.compile(
    r"(?:[「『](?P<b>[^」』]{2,60})[」』]|(?P<p>[가-힣A-Za-z0-9ㆍ·]+(?:\s+[가-힣A-Za-z0-9ㆍ·]+){0,8}?(?:법률|법|령|규칙|규정)))"
    r"\s*\((?:[^()]{0,80}?[,，]\s*)?이하\s*[‘'\"“]?(?P<alias>[가-힣A-Za-z0-9ㆍ· ]{1,20}?)[’'\"”]?\s*(?:이?라|로)\s*(?:한다|함|칭한다)\s*\)")
_TITLE_RE = re.compile(r"^\s*\(([^()]{1,40})\)")
_HIST_PAREN_RE = re.compile(
    r"(?P<d>\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?)[^()]{0,50}?"
    r"(?P<kind>(?:전부\s*)?개정되기\s*전|폐지되기\s*전|(?:전부\s*)?개정된\s*것)")
_PARTICLE_TAIL_RE = re.compile(
    r"(?<=[법률령칙정례])(?:상의|상|의|에서는|에서도|에서의|에서|에는|에도|에|은|는|이|가|과|와|을|를|도|만|으로는|으로|로는|로)$")
_CONNECTOR_TAIL_RE = re.compile(
    r"(?<=[법률령칙정례])\s*(?:에|이|가)?\s*(?:따르면|의하면|따라|의하여|의한|따른|규정된|정한|정하는|규정한|규정하는)$")
_SENT_END_RE = re.compile(r"(?<!\d)[.!?。](?=\s|$)")
_FUTURE_CTX_RE = re.compile(r"시행\s*예정|신설\s*예정|개정\s*예정|미시행|시행될|시행되는\s*날|공포\s*후\s*\d+\s*(?:일|개월|년)")
_FUTURE_NEG_RE = re.compile(r"^\s*(?:이|인|이다|이라)?\s*(?:아닌|아니|아님|없는|없이)")

# 비(非)법령 문서: 계약서·취업규칙 등의 조항 번호는 법령 인용이 아니다
_NONLAW_ALWAYS_RE = re.compile(r"(?:취업규칙|정관|계약서?|약관|협약서?|사규|내규|규약|합의서|협정서|각서|처리방침|확약서|서약서)$")
_NONLAW_RULE_RE = re.compile(r"(?:사내|회사|내부|인사|징계|복무|보수|급여|퇴직금|취업|운영|관리|여비|복리후생|윤리|위임전결|안전보건관리)규정$|지침$|가이드라인$|매뉴얼$")
_ORG_WORDS = {"당사", "회사", "사내", "본사", "귀사", "우리", "소속", "기관", "법인", "재단", "조합", "본", "이", "위", "동", "해당",
              "갑", "을", "甲", "乙", "A", "B", "C"}

_STOP_TOKENS = {"및", "또는", "와", "과", "또한", "그리고", "따라서", "이에", "본", "위", "해당", "관련", "현행", "개정", "신", "즉",
                "시행", "신설", "종전", "구법", "신법", "예정", "개정된", "현재", "후", "전", "이후", "이전", "당시", "동안", "중",
                "특히", "한편", "아울러", "다만", "따라", "의하여", "의한", "따른", "위하여", "근거", "적용", "경우", "때", "각",
                "모든", "그", "이", "또", "곧", "결국", "나아가", "더욱이", "오히려", "다시", "반면", "요컨대", "이는", "이를",
                "관계", "관계법령", "개별", "일반", "소정", "위반", "준수", "관하여", "대하여", "비추어", "보면", "보아",
                "달리", "같이", "함께", "더불어", "별도로", "따로", "역시", "게다가", "더구나", "그러나", "하지만", "그런데",
                "그러므로", "그래서", "바로", "이미", "아직", "이제", "여전히", "비록", "만약", "만일", "설령", "가령", "예컨대",
                "이를테면", "이와", "그와", "우선", "먼저", "끝으로", "마지막으로", "다음으로", "또는", "혹은"}
_PROSE_END_RE = re.compile(
    r"(?:은|는|이|가|을|를|의|에|에서|로|으로|와|과|도|만|인|된|한|던|적|상|중|및|등|시|때|며|고|서|면|나|께|로서|로써|하에|"
    r"따라|따른|관한|대한|위한|의한|규정한|다|요|여|라|니|지|후|전)$")

_COURT_RE = (r"(?P<court>대법원|헌법재판소|대판|헌재|특허법원|"
             r"[가-힣]{2,12}(?:고등법원|지방법원|가정법원|행정법원|회생법원)(?:\s*\(?[가-힣]{1,6}(?:지원|재판부)\)?|\([가-힣]{1,4}\))?|"
             r"[가-힣]{1,6}(?:고법|지법|행법|가법)(?:\s*[가-힣]{1,8}지원)?)")
_DATE_PAT = r"\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?|\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일|\d{4}-\d{1,2}-\d{1,2}"
_CODE_ALT = "|".join(re.escape(c) for c in CASE_CODES)
_CASE_RE = re.compile(
    rf"(?:{_COURT_RE}\s*)?"
    rf"(?:(?P<date>{_DATE_PAT})\s*(?:선고|자)?\s*)?"
    r"(?:(?<=선고)|(?<=자)|(?<![0-9가-힣A-Za-z]))"
    rf"(?P<no>(?:(?:19|20|42|43)\d{{2}}|\d{{2}})(?:{_CODE_ALT})\d{{1,7}})(?![0-9])"
    r"(?:\s*(?P<enbanc>전원합의체))?")
_AUTH_RE = re.compile(r"(?:법제처|법령\s*해석례?|해석례|안건번호|안건)[^\n]{0,24}?(?<![\d-])(?P<no>\d{2}-\d{3,4})(?![\d-])")
_MINISTRY_RE = re.compile(r"(?<![가-힣])(?P<no>[가-힣]{2,15}(?:과|팀)\s*-\s*\d{2,6}|(?:근기|임금|근로기준|노사)\s*\d{5}\s*-\s*\d{2,6})(?!\d)")
_MINISTRY_CTX_RE = re.compile(r"행정해석|회시|질의회시|해석|고용노동부|노동부|지침|부처|유권해석")

# 인용문 성격
_LEGAL_VERB_RE = re.compile(r"규정|정하|정한|정의|판시|판단|설시|명시|선언|밝히|밝혔|밝힌|해석|회신|회답|판결하|결정하|보았|본다|보고\s*있|적시|설명하|결론|"
                            r"제시|표현하|언급하였|기술하")
_LEGAL_SOURCE_WORD_RE = re.compile(r"판결|결정|판례|법원|헌법재판소|해석례|회신|회답|조항|조문|법률|법령|시행령|시행규칙")
_PARTY_VERB_RE = re.compile(r"주장|진술|항변|증언|말하|말했|말한|발언|요구|통보|기재|호소|답변|항의|신고|제보|고소|진정|문자|메시지|메일|카카오톡|카톡|"
                            r"녹취|폭언|욕설|소리치|외치|외쳤|협박|질문|물었|대답|답했|적었|적혀|적힌|쓰여|쓰인|게시|공지")
_PARTY_SUBJ_RE = re.compile(r"(?:원고|피고|신청인|피신청인|청구인|피청구인|피해자|가해자|근로자|사용자|회사|직원|상대방|증인|피의자|피고인|고소인|"
                            r"진정인|의뢰인|당사자|대표|팀장|상사|동료|[A-Z]|[甲乙丙丁])\s*(?:측)?(?:은|는|이|가)(?![가-힣])")
_BODY_CTX_RE = re.compile(r"이유|설시|판단\s*부분|판결문|결정문|본문|원심|사실관계|인정\s*사실")
_ANAPH_SUBJ_RE = re.compile(
    r"(?:위|이|같은|동|해당|앞의|앞서\s*본|상기|그)\s*(?P<k>대법원\s*판결|전원합의체\s*판결|판결|결정|판례|해석례|해석|회신|회답|"
    r"법률\s*조항|조항|조문|규정|조|항)(?![가-힣]{2})")
_NEXT_LINE_LEAD_RE = re.compile(r"(?:다음과\s*같|아래와\s*같)|[:：]\s*$")
_CITE_LIKE_RE = re.compile(r"제\s*\d+\s*조|\d{2,4}[가-힣]{1,3}\d{1,7}")
# '같은 법'의 선행어가 될 수 있는 본문 속 법령명(낫표 없이 쓴 것). '방법·위법·불법' 같은 일반어는 제외
_PROSE_LAW_RE = re.compile(r"(?<![가-힣ㆍ])(?P<n>[가-힣ㆍ]{2,30}?(?:법률|법))(?=상|의|에|은|는|이|가|과|와|을|를|도|\s|$|[,.)」])")
_NOT_LAW_WORDS = re.compile(r"(?:방법|불법|위법|적법|편법|입법|사법|문법|해법|용법|기법|수법|탈법|합법|비법|화법|요법|공법|준법|범법|처벌법|"
                            r"실정법|특별법|일반법|개정법|신법|구법|관계법|해당법|법률)$")


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _ho_of(m) -> Optional[str]:
    if not m.group("ho"):
        return None
    sub = m.group("hoa") or m.group("hob")
    return m.group("ho") + (f"의{sub}" if sub else "")


def _hang_of(m) -> Optional[int]:
    if m.group("hang"):
        return int(m.group("hang"))
    if m.group("hangc"):
        return CIRCLED[m.group("hangc")]
    return None


def _is_prose_token(t: str) -> bool:
    """법령명 앞에서 떼어내도 되는 서술 어절인가(조사·어미로 끝나거나 접속·지시·시간어)."""
    t = t.strip("、,.;:()[]\"'“”‘’")
    return (not t) or t in _STOP_TOKENS or t == "구" or bool(_PROSE_END_RE.search(t)) or not re.search(r"[가-힣]", t)


def norm_court(s: Optional[str]) -> str:
    """법원명 정규화: 공백·괄호 제거, 약칭 확장(서울고법→서울고등법원, 서울가법→서울가정법원 …), 원외재판부 표기 통일."""
    t = re.sub(r"[\s()]", "", s or "")
    t = t.replace("대판", "대법원").replace("헌재", "헌법재판소")
    for a, b in (("고법", "고등법원"), ("지법", "지방법원"), ("가법", "가정법원"), ("행법", "행정법원")):
        t = t.replace(a, b)
    t = re.sub(r"(고등법원)([가-힣]{2})(?:재판부|부)$", r"\1\2", t)
    return t


def _sentence_bounds(masked: str, pos: int) -> Tuple[int, int]:
    """pos 가 속한 문장의 [시작, 끝). 문장 경계 = 마침표류 또는 줄바꿈(따옴표 안은 가려진 텍스트 기준)."""
    start = 0
    for m in _SENT_END_RE.finditer(masked, 0, pos):
        start = m.end()
    nl = masked.rfind("\n", 0, pos)
    start = max(start, nl + 1)
    m = _SENT_END_RE.search(masked, pos)
    end = m.end() if m else len(masked)
    nl2 = masked.find("\n", pos)
    if nl2 >= 0:
        end = min(end, nl2)
    return start, end


# ---------------------------------------------------------------------------
# 따옴표 구간
# ---------------------------------------------------------------------------


def _inside(pos: int, spans: Sequence[QuoteSpan]) -> Optional[QuoteSpan]:
    for s in spans:
        if s.start <= pos < s.end:
            return s
    return None


def find_quote_spans(text: str) -> List[QuoteSpan]:
    """인용문 후보 구간. “…”(균형·중첩), "…"(문단 안 짝), ‘…’/'…'(정규화 12자 이상). 블록 인용(>)은 연결 단계에서 판단."""
    spans: List[QuoteSpan] = []
    i = 0
    n = len(text)
    while True:
        j = text.find("“", i)
        if j < 0:
            break
        depth, k, end = 0, j, -1
        limit = min(n, j + 4000)
        while k < limit:
            ch = text[k]
            if ch == "“":
                depth += 1
            elif ch == "”":
                depth -= 1
                if depth == 0:
                    end = k
                    break
            elif ch == "\n" and text.startswith("\n\n", k):
                break                      # 닫히지 않은 따옴표는 문단을 넘지 않는다
            k += 1
        if end < 0:
            i = j + 1
            continue
        spans.append(QuoteSpan(j, end + 1, text[j + 1:end], "curly"))
        i = end + 1
    # "…" : 곧은 따옴표는 문단 안에서 순서대로 짝짓는다(“…” 안의 것은 그 인용문의 일부)
    para_start = 0
    for para in re.split(r"(\n\s*\n)", text):
        pos = [para_start + m.start() for m in re.finditer('"', para) if not _inside(para_start + m.start(), spans)]
        for a, b in zip(pos[0::2], pos[1::2]):
            spans.append(QuoteSpan(a, b + 1, text[a + 1:b], "straight"))
        para_start += len(para)
    for rx in (re.compile(r"‘([^’\n]{1,800})’"), re.compile(r"(?<![A-Za-z])'([^'\n]{1,800})'(?![A-Za-z])")):
        for m in rx.finditer(text):
            if _inside(m.start(), spans) or len(normalize_for_match(m.group(1))) < 12:
                continue
            spans.append(QuoteSpan(m.start(), m.end(), m.group(1), "single"))
    spans.sort(key=lambda s: s.start)
    return spans


def _block_quotes(text: str) -> List[QuoteSpan]:
    out = []
    for m in re.finditer(r"(?m)(?:^[ \t]*>[^\n]*(?:\n|$))+", text):
        inner = "\n".join(re.sub(r"^[ \t]*>\s?", "", l) for l in m.group(0).splitlines())
        if len(normalize_for_match(inner)) >= 6:
            out.append(QuoteSpan(m.start(), m.end(), inner.strip(), "block"))
    return out


def _lead_line(text: str, pos: int) -> Tuple[int, int]:
    """pos(줄 첫머리) 바로 앞의 비어 있지 않은 줄의 [시작, 끝)."""
    end = text.rfind("\n", 0, pos)
    while end > 0:
        start = text.rfind("\n", 0, end) + 1
        if text[start:end].strip():
            return start, end
        end = start - 1
    return 0, 0


def _block_lead(text: str, b: QuoteSpan) -> bool:
    s, e = _lead_line(text, b.start)
    line = text[s:e].strip()
    return bool(line) and bool(_NEXT_LINE_LEAD_RE.search(line)) and bool(_CITE_LIKE_RE.search(line))


def _mask(text: str, spans: Sequence[QuoteSpan]) -> str:
    """따옴표 안을 같은 길이의 자리표로 가린 텍스트(문장 경계·문맥 판정이 인용문 안 마침표에 흔들리지 않게)."""
    chars = list(text)
    for s in spans:
        for k in range(s.start + 1, max(s.start + 1, s.end - 1)):
            if chars[k] != "\n":
                chars[k] = "_"
    return "".join(chars)


# ---------------------------------------------------------------------------
# 법령명 찾기
# ---------------------------------------------------------------------------


@dataclass
class _LawRef:
    name: Optional[str] = None
    cands: List[str] = field(default_factory=list)
    hist: bool = False
    hist_date: Optional[date] = None
    hist_note: str = ""
    origin: str = "none"     # explicit | anaphora | alias | nonlaw | none
    token: str = ""
    anaphora_kind: str = ""
    anaphora_suffix: str = ""
    anaphora_prefix: str = ""


def _strip_tail(lb: str) -> str:
    for _ in range(3):
        new = _CONNECTOR_TAIL_RE.sub("", lb).rstrip()
        new = _PARTICLE_TAIL_RE.sub("", new).rstrip()
        if new == lb:
            break
        lb = new
    return lb


def _lookback_law(text: str, start: int, floor: int, alias_at: Callable[[str, int], Optional[List[str]]]) -> _LawRef:
    """조문 인용 직전 문맥(같은 줄)에서 법령명을 찾는다."""
    lb = text[max(0, start - 120, floor):start]
    nl = lb.rfind("\n", 0, len(lb.rstrip(" \t")))
    if nl >= 0:
        lb = lb[nl + 1:]
    lb = lb.rstrip()
    ref = _LawRef()
    # 법령명 뒤 괄호: 판본 표시 '(2019. 1. 15. 법률 제16270호로 개정되기 전의 것)' 또는 약칭 정의 등
    pm = re.search(r"\s*\(([^()]{0,90})\)\s*$", lb)
    if pm:
        hm = _HIST_PAREN_RE.search(pm.group(1))
        if hm:
            d = parse_date(hm.group("d"))
            if d and "전" in hm.group("kind"):
                ref.hist_date = d
            ref.hist_note = pm.group(1).strip()
        lb = lb[:pm.start()].rstrip()
    if not lb:
        return ref
    if lb.endswith(("」", "』")):
        open_idx = max(lb.rfind("「"), lb.rfind("『"))
        if open_idx >= 0:
            name = lb[open_idx + 1:-1].strip()
            ref.name, ref.cands, ref.origin = name, [name], "explicit"
            ref.hist = bool(re.search(r"(?:^|\s)구\s*$", lb[:open_idx]))
            return ref
    m = _ANAPHORA_RE.search(lb)
    if m:
        ref.origin = "anaphora"
        ref.anaphora_kind = m.group("kind") or "법"
        ref.anaphora_suffix = m.group("suffix") or ""
        ref.anaphora_prefix = lb[:m.start()]
        return ref
    lb2 = _strip_tail(lb)
    m = re.search(r"([가-힣A-Za-z0-9ㆍ·\s]{1,90})$", lb2)
    if not m:
        return ref
    tokens = m.group(1).split()
    if not tokens:
        return ref
    last = tokens[-1]
    ref.token = last
    prev = tokens[-2] if len(tokens) >= 2 else ""
    al = alias_at(last, start)
    if al:
        ref.name, ref.cands, ref.origin = al[0], list(al), "alias"
        ref.hist = prev == "구"
        return ref
    if _NONLAW_ALWAYS_RE.search(last) or (_NONLAW_RULE_RE.search(last) and (not prev or prev in _ORG_WORDS or _is_prose_token(prev))):
        ref.origin = "nonlaw"
        return ref
    if not re.search(_LAW_SUFFIX + r"$", last):
        return ref
    tokens = tokens[-8:]
    cands: List[str] = []
    for i in range(len(tokens)):
        t = tokens[i:]
        if t[0] == "구":
            ref.hist = True
            continue
        if len(t) > 1 and (t[0] in _STOP_TOKENS or re.search(r"(?:은|는|을|를|와|과|에서|에게|으로|로써|하며|하고|하여|되어|된|한|인|적)$", t[0])):
            continue
        cands.append(" ".join(t))
    if len(tokens) >= 2 and tokens[-2] == "구":
        ref.hist = True
    solid = [c for c in cands if not re.fullmatch(r"(?:법률|법|령|규칙|규정|시행령|시행규칙|영)", c)]
    if not solid:
        ref.origin = "alias"          # '법 제3조' — 정의된 약칭이 아니면 특정 불가
        ref.cands = cands[-1:] if cands else [last]
        return ref
    ref.name, ref.cands, ref.origin = solid[0], solid, "explicit"
    return ref


# ---------------------------------------------------------------------------
# 추출
# ---------------------------------------------------------------------------


def extract_all(text: str) -> Extraction:
    text = prep_text(text or "")
    spans = find_quote_spans(text)
    # 앞 줄이 '다음과 같이 …:'이고 인용 표기가 있는 블록 인용(>)은 그 자체가 하나의 인용문
    blocks = [b for b in _block_quotes(text) if _block_lead(text, b)]
    spans = [s for s in spans if not any(b.start <= s.start < b.end for b in blocks)] + blocks
    spans.sort(key=lambda s: s.start)
    qualifying = [s for s in spans if len(normalize_for_match(s.text)) >= (12 if s.kind == "single" else 6)]
    masked = _mask(text, qualifying)

    # ---- 약칭 정의(위치 기준: 정의 이후에만, 나중 정의가 앞 정의를 대체) ----
    alias_defs: List[Tuple[int, str, List[str]]] = []
    for am in _ALIAS_DEF_RE.finditer(text):
        if _inside(am.start(), [q for q in qualifying if q.kind != "straight"]):
            continue
        alias = re.sub(r"\s+", " ", am.group("alias").strip())
        if am.group("b"):
            alias_defs.append((am.end(), alias, [am.group("b").strip()]))
        else:
            toks = am.group("p").split()
            alias_defs.append((am.end(), alias, [" ".join(toks[i:]) for i in range(len(toks)) if toks[i] not in _STOP_TOKENS]))

    def alias_at(token: str, pos: int) -> Optional[List[str]]:
        best = None
        for p, a, c in alias_defs:
            if p <= pos and (a == token or a.replace(" ", "") == token.replace(" ", "")):
                best = c
        return best

    # ---- 판례 ----
    cases: List[CaseCite] = []
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
            court = prev_case.court
            if not prev_case.has_tail:
                decided = prev_case.decided     # '… 선고 2015다57904, 2015다57911 판결' — 같은 선고의 병합 사건
        end = m.end()
        tm = re.match(r"\s*(판결|결정|판례|심판|명령)", text[end:end + 12])
        if tm:
            end += tm.end()
        c = CaseCite(raw=text[m.start():end].strip(), start=m.start(), end=end, case_no=no, court=court,
                     decided=decided, en_banc=bool(m.group("enbanc")), has_tail=bool(tm))
        cases.append(c)
        case_spans.append((m.start(), end))
        prev_case = c

    # ---- 해석례 ----
    auths: List[AuthorityCite] = []
    for m in _AUTH_RE.finditer(text):
        if any(s <= m.start("no") < e for s, e in case_spans):
            continue
        auths.append(AuthorityCite(raw=m.group(0).strip(), start=m.start(), end=m.end(), agenda_no=m.group("no")))
    for m in _MINISTRY_RE.finditer(text):
        ctx = text[max(0, m.start() - 60):m.start()] + text[m.end():m.end() + 12]
        if not _MINISTRY_CTX_RE.search(ctx) or any(a.start <= m.start() < a.end for a in auths):
            continue
        auths.append(AuthorityCite(raw=m.group(0).strip(), start=m.start(), end=m.end(),
                                   agenda_no=re.sub(r"\s+", "", m.group("no")), target="ministry", ministry=True))

    def in_case(pos: int) -> bool:
        return any(s <= pos < e for s, e in case_spans)

    # ---- 법령 언급(‘같은 법’의 선행어) ----
    mentions: List[Tuple[int, str, List[str]]] = []
    for bm in re.finditer(r"[「『]([^」』]{2,60})[」』]", text):
        if not _inside(bm.start(), qualifying):
            mentions.append((bm.start(), bm.group(1).strip(), [bm.group(1).strip()]))
    for pm_ in _PROSE_LAW_RE.finditer(masked):
        n = pm_.group("n")
        if _NOT_LAW_WORDS.search(n) and n not in ("민법", "상법", "형법", "헌법"):
            continue
        if len(n) < 4 and n not in ("민법", "상법", "형법", "헌법"):
            continue
        mentions.append((pm_.start(), n, [n]))
    for p, _a, c in alias_defs:
        mentions.append((p, c[0], list(c)))

    def antecedent(pos: int, prefix: str) -> Optional[Tuple[str, List[str]]]:
        # '근로기준법 및 같은 법 시행령' — 바로 앞 나열의 법령명
        pm = re.search(r"([가-힣A-Za-z0-9ㆍ·「」『』\s]{1,70}?)\s*(?:및|과|와|,|ㆍ|또는)\s*$", prefix)
        if pm:
            sub = _lookback_law(pm.group(1) + " 제1조", len(pm.group(1)) + 1, 0, lambda tok, _p: alias_at(tok, pos))
            if sub.origin in ("explicit", "alias") and sub.name:
                return sub.name, sub.cands
        best = None
        pool = mentions + [(s.start, s.law, s.law_candidates) for s in statutes if s.law and not s.in_quote]
        for p, name, cands in pool:
            if p < pos and (best is None or p >= best[0]):
                best = (p, name, cands)
        return (best[1], best[2]) if best else None

    # ---- 조문 ----
    statutes: List[StatuteCite] = []
    prev: Optional[StatuteCite] = None
    group_id = 0

    def new_group() -> int:
        nonlocal group_id
        group_id += 1
        return group_id

    for m in _ART_RE.finditer(text):
        if in_case(m.start()):
            continue
        before_char = text[m.start() - 1] if m.start() > 0 else " "
        if re.match(r"[가-힣]", before_char):
            # '근로기준법제60조'는 허용, '3조원'·'매출3조' 같은 비인용은 제외
            if not (m.group("je") and re.search(_LAW_SUFFIX + r"$", text[max(0, m.start() - 6):m.start()])):
                continue
        jo = int(m.group("jo"))
        sub = int(m.group("sub")) if m.group("sub") else None
        hang = _hang_of(m)
        ho = _ho_of(m)
        mok = m.group("mok")
        inq = _inside(m.start(), qualifying) is not None
        ref = _LawRef()
        law: Optional[str] = None
        cands: List[str] = []
        origin = "none"
        problem = None
        gap = text[prev.end:m.start()] if prev else None
        if prev is not None and gap is not None and _CONT_GAP_RE.match(gap) and prev.in_quote == inq:
            law, cands, origin = prev.law, list(prev.law_candidates), "continuation"
            ref.hist, ref.hist_date = prev.historical, prev.hist_date
            grp = prev.group
            if prev.problem and not prev.law:
                problem = prev.problem
        else:
            grp = new_group()
            floor = max([prev.end if prev else 0] + [e for s_, e in case_spans if e <= m.start()]
                        + [s.end for s in qualifying if s.end <= m.start()])
            ref = _lookback_law(text, m.start(), floor, alias_at)
            origin = ref.origin
            if origin == "nonlaw":
                prev = None
                continue          # 계약서·취업규칙 조항 — 법령 인용 아님
            if origin in ("explicit", "alias") and ref.name:
                law, cands = ref.name, ref.cands
            elif origin == "anaphora":
                ant = antecedent(m.start(), ref.anaphora_prefix)
                if ant:
                    base = re.sub(r"\s*(?:시행령|시행규칙)$", "", ant[0])
                    if ref.anaphora_kind in ("영", "령"):
                        law = ant[0] if ant[0].endswith("시행령") else f"{base} 시행령"
                    elif ref.anaphora_kind == "규칙":
                        law = ant[0] if ant[0].endswith("시행규칙") else f"{base} 시행규칙"
                    else:
                        law = f"{base} {ref.anaphora_suffix}".strip()
                    cands = [law]
                else:
                    problem = "'같은 법/동법/이 법'이 가리키는 법령이 앞에 없음 — 정식 법령명으로 인용하세요"
            elif origin == "alias":
                problem = (f"'{(ref.cands or [ref.token])[0]}'이(가) 가리키는 법령을 특정할 수 없음(문서 안 약칭 정의 없음) "
                           "— 「정식 법령명」으로 인용하거나 '(이하 \"…\"이라 한다)'로 먼저 정의하세요")
            elif not m.group("je"):
                continue          # '750조' 처럼 '제'도 법령명도 없으면 인용으로 보지 않는다
            else:
                problem = "법령명이 없는 조문 인용 — 「정식 법령명」 제N조 형식으로 쓰세요(앞 문장의 법령으로 추정하지 않음)"
        if mok and not ho and not problem:
            problem = f"'{mok}목'은 호 아래에만 있음 — '제N호{mok}목'으로 인용하세요"
        end = m.end()
        title = None
        tm = _TITLE_RE.match(text[end:end + 60])
        if tm and not re.search(r"\d{4}|이하|개정|신설|시행|선고|판결|결정|참조|위반|삭제|단서|본문|전단|후단|각\s*호|중략|생략", tm.group(1)):
            title = tm.group(1).strip()
        c = StatuteCite(raw=text[m.start():end].strip(), start=m.start(), end=end, law=law, jo=jo, sub=sub,
                        hang=hang, ho=ho, mok=mok, claimed_title=title, historical=ref.hist or ref.hist_date is not None,
                        hist_date=ref.hist_date, law_candidates=cands, law_origin=origin, problem=problem, group=grp,
                        in_quote=inq)
        statutes.append(c)
        prev = c
        # 같은 조의 항·호·목 나열: '제15조제1항 및 제2항', '제2조제1호부터 제3호까지', '제1호 가목ㆍ나목'
        pos = end
        cur = c
        while True:
            tail = text[pos:]
            hm = _ENUM_HANG_RE.match(tail)
            if hm and (cur.hang is not None):
                c2 = StatuteCite(raw=tail[:hm.end()].strip(), start=pos, end=pos + hm.end(), law=law, jo=jo, sub=sub,
                                 hang=_hang_of(hm), ho=_ho_of(hm), historical=c.historical, hist_date=c.hist_date,
                                 law_candidates=cands, law_origin="continuation", problem=c.problem if not law else None,
                                 group=grp, in_quote=inq)
            else:
                om = _ENUM_HO_RE.match(tail)
                if om and cur.ho is not None:
                    c2 = StatuteCite(raw=tail[:om.end()].strip(), start=pos, end=pos + om.end(), law=law, jo=jo, sub=sub,
                                     hang=cur.hang, ho=_ho_of(om), mok=om.group("mok"), historical=c.historical,
                                     hist_date=c.hist_date, law_candidates=cands, law_origin="continuation",
                                     problem=c.problem if not law else None, group=grp, in_quote=inq)
                    hm = om
                else:
                    km = _ENUM_MOK_RE.match(tail)
                    if km and cur.mok is not None:
                        c2 = StatuteCite(raw=tail[:km.end()].strip(), start=pos, end=pos + km.end(), law=law, jo=jo,
                                         sub=sub, hang=cur.hang, ho=cur.ho, mok=km.group("mok"), historical=c.historical,
                                         hist_date=c.hist_date, law_candidates=cands, law_origin="continuation",
                                         problem=c.problem if not law else None, group=grp, in_quote=inq)
                        hm = km
                    else:
                        break
            statutes.append(c2)
            pos += hm.end()
            prev = cur = c2

    # ---- '같은 조 제2항', '같은 항 제9호', '동호 가목' ----
    for m in _SAME_UNIT_RE.finditer(text):
        if not (m.group("hang") or m.group("hangc") or m.group("ho") or m.group("mok")):
            continue
        if any(s.start <= m.start() < s.end for s in statutes):
            continue
        inq = _inside(m.start(), qualifying) is not None
        before = [s for s in statutes if s.end <= m.start() and s.in_quote == inq and s.law]
        lvl = m.group("l1") or m.group("l2")
        c = StatuteCite(raw=m.group(0).strip(), start=m.start(), end=m.end(), law=None, jo=0, law_origin="anaphora",
                        in_quote=inq, group=new_group())
        if not before:
            c.problem = f"'같은 {lvl}'이(가) 가리키는 앞 조문이 없음"
            statutes.append(c)
            continue
        b = before[-1]
        c.law, c.law_candidates, c.jo, c.sub = b.law, list(b.law_candidates), b.jo, b.sub
        c.historical, c.hist_date = b.historical, b.hist_date
        if lvl == "조":
            c.hang, c.ho, c.mok = _hang_of(m), _ho_of(m), m.group("mok")
        elif lvl == "항":
            if b.hang is None or m.group("hang") or m.group("hangc"):
                c.problem = "'같은 항'이 가리키는 앞 인용에 항이 없음"
            c.hang, c.ho, c.mok = b.hang, _ho_of(m), m.group("mok")
        else:
            if b.ho is None or _ho_of(m):
                c.problem = "'같은 호'가 가리키는 앞 인용에 호가 없음"
            c.hang, c.ho, c.mok = b.hang, b.ho, m.group("mok")
        if c.mok and not c.ho and not c.problem:
            c.problem = f"'{c.mok}목'은 호 아래에만 있음"
        statutes.append(c)
    statutes.sort(key=lambda s: s.start)

    # ---- 시행예정 문맥 ----
    for c in statutes:
        s0, s1 = _sentence_bounds(masked, c.start)
        window_before = masked[max(s0, c.start - 60):c.start]
        window_after = masked[c.end:min(s1, c.end + 40)]
        for w in (window_before, window_after):
            for fm in _FUTURE_CTX_RE.finditer(w):
                if not _FUTURE_NEG_RE.match(w[fm.end():fm.end() + 10]):
                    c.future_context = True

    for cc in cases:
        cc.in_quote = _inside(cc.start, qualifying) is not None
    for a in auths:
        a.in_quote = _inside(a.start, qualifying) is not None

    orphans = _attach_quotes(text, masked, qualifying, statutes, cases, auths)
    # 붙은 인용문·당사자 말 안의 인용 표기는 인용문의 일부 — 따로 검증하지 않는다
    drop = [s for s in qualifying if s.attached or s.verb == "party"]
    statutes = [s for s in statutes if not (s.in_quote and (_inside(s.start, drop) or s.problem))]
    cases = [s for s in cases if not (s.in_quote and _inside(s.start, drop))]
    auths = [s for s in auths if not (s.in_quote and _inside(s.start, drop))]
    return Extraction(text, statutes, cases, auths, orphans, qualifying)


def extract(text: str) -> Tuple[List[StatuteCite], List[CaseCite], List[AuthorityCite]]:
    ex = extract_all(text)
    return ex.statutes, ex.cases, ex.auths


# ---------------------------------------------------------------------------
# 인용문 연결
# ---------------------------------------------------------------------------


def _classify_quote(text: str, masked: str, q: QuoteSpan, s0: int, s1: int, cites_before: List[object]) -> Optional[str]:
    after = masked[q.end:min(s1, q.end + 40)]
    lm = _LEGAL_VERB_RE.search(after)
    pm = _PARTY_VERB_RE.search(after)
    if lm and (not pm or lm.start() < pm.start()):
        return "legal"
    if pm:
        subj = masked[s0:q.start]
        if _PARTY_SUBJ_RE.search(subj):
            return "party"
        # 출처가 주어이거나 출처를 가리키는 문장('… 판결은 “…”라고 진술하였다', '위 판결문에는 “…”라고 적혀 있다')은
        # 동사와 무관하게 원문 인용으로 본다(당사자 동사로 위장해 대조를 피하는 것 차단)
        if cites_before or _LEGAL_SOURCE_WORD_RE.search(subj):
            return "legal"
        return "party"
    return None


def _attach_quotes(text: str, masked: str, spans: List[QuoteSpan], statutes: List[StatuteCite],
                   cases: List[CaseCite], auths: List[AuthorityCite]) -> List[QuoteSpan]:
    cites = sorted([c for c in list(statutes) + list(cases) + list(auths) if not c.in_quote], key=lambda c: c.start)
    orphans: List[QuoteSpan] = []

    def attach(c, q: QuoteSpan) -> None:
        q.attached = True
        if isinstance(c, StatuteCite):
            members = [s for s in statutes if s.group == c.group and not s.in_quote] or [c]
            head = min(members, key=lambda s: s.start)
            head.quotes.append(q.text.strip())
            for s in members:
                if s.unit not in head.quote_units:
                    head.quote_units.append(s.unit)
        else:
            c.quotes.append(q.text.strip())
            if isinstance(c, CaseCite):
                c.body_ok = c.body_ok or q.body_ok

    for q in spans:
        if q.kind == "block":
            # 콜론·'다음과 같이' 뒤의 블록 인용(>) — 앞 줄의 마지막 인용에 붙인다
            ls, le = _lead_line(text, q.start)
            on_line = [c for c in cites if ls <= c.start < le]
            q.verb = "legal"
            if on_line:
                attach(on_line[-1], q)
            else:
                orphans.append(q)
            continue
        s0, s1 = _sentence_bounds(masked, q.start)
        before = [c for c in cites if s0 <= c.start and c.end <= q.start]
        q.verb = _classify_quote(text, masked, q, s0, s1, before)
        ctx = masked[s0:q.start] + masked[q.end:min(s1, q.end + 40)]
        q.body_ok = bool(_BODY_CTX_RE.search(ctx))
        if q.verb == "party":
            continue
        long_enough = len(normalize_for_match(q.text)) >= 12
        target = None
        # (1) “…”(민법 제750조)
        pm = re.match(r"\s*\(\s*", text[q.end:q.end + 6])
        if pm:
            target = next((c for c in cites if q.end <= c.start <= q.end + pm.end() + 1), None)
        # (2) 같은 문장 안 앞의 인용
        if target is None and before and (q.verb == "legal" or long_enough):
            last = before[-1]
            if q.start - last.end <= 220:
                target = last
        # (3) 같은 문장 안 뒤의 인용('“…”라는 민법 제750조의 규정')
        if target is None and (q.verb == "legal" or long_enough):
            target = next((c for c in cites if q.end <= c.start < s1 and c.start - q.end <= 40), None)
        # (4) 앞 문장의 인용을 가리키는 주어('위 판결은', '이 조항은')
        if target is None:
            am = None
            for am_ in _ANAPH_SUBJ_RE.finditer(masked, s0, q.start):
                am = am_
            if am:
                k = am.group("k")
                want = CaseCite if re.search(r"판결|결정|판례", k) else (AuthorityCite if re.search(r"해석|회신|회답", k) else StatuteCite)
                prior = [c for c in cites if isinstance(c, want) and c.end <= am.start()]
                if prior:
                    target = prior[-1]
        # (5) 앞 줄이 '다음과 같이 …' 또는 ':' 로 끝나고 인용문이 줄 첫머리에서 시작
        if target is None:
            line_start = text.rfind("\n", 0, q.start) + 1
            if not text[line_start:q.start].strip(" \t>-*"):
                pl_start, pl_end = _lead_line(text, line_start)
                prev_line = text[pl_start:pl_end]
                on_line = [c for c in cites if pl_start <= c.start < pl_end]
                if on_line and _NEXT_LINE_LEAD_RE.search(prev_line.strip()):
                    target = on_line[-1]
                    q.verb = q.verb or "legal"
        if target is not None:
            attach(target, q)
        elif q.verb == "legal":
            orphans.append(q)
    return orphans


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
        from law_api import ApiAuthError, ApiError, ApiUnavailable
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
        if c.problem:
            return Finding("statute", cit if c.jo else c.raw, UNRESOLVED, c.problem)
        if not c.law:
            return Finding("statute", cit, UNRESOLVED, "법령명 특정 불가 — 정식 법령명으로 인용하세요")
        warnings: List[str] = []
        as_of = self.as_of
        if c.hist_date:
            as_of = c.hist_date - timedelta(days=1)
            warnings.append(f"구법 인용: {iso(c.hist_date)} 개정 전 판본(기준일 {iso(as_of)})으로 대조")
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
                    fl = self._api_call(self.api.find_law, c.law, as_of)
                    if fl.get("status") in ("ok", "not_in_force") and fl.get("name"):
                        r = self.statutes.resolve(fl["name"])
                        if r.status == "ok":
                            doc = r.doc
                            warnings.append(f"약칭/이칭 '{c.law}' → 정식명 '{fl['name']}' (DRF 확인) — 정식명 인용 권장")
                except ApiUnavailable:
                    pass
        if c.historical and not c.hist_date:
            name = doc.title if doc else c.law
            return Finding("statute", f"구 {name} {c.label}", UNVERIFIABLE,
                           "구법 인용인데 판본이 특정되지 않음 — '구 「법령명」(YYYY. M. D. 법률 제N호로 개정되기 전의 것) 제N조'처럼 "
                           "판본을 밝혀야 대조할 수 있음", warnings=warnings)
        if doc is not None:
            f = self._verify_local(c, doc, warnings, as_of)
            if res_note:
                f.evidence["resolution"] = res_note
            if self.cross_check and self.api is not None and f.status == VERIFIED:
                self._cross_check(c, f, as_of)
            return f
        if self.api is not None:
            try:
                return self._verify_api(c, warnings, as_of)
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

    @staticmethod
    def _adjust_units(art, hang, ho, warnings: List[str]):
        """'제1항'을 항 없는 조문에 붙인 경우, 항 없이 쓴 '제N호'가 한 항에만 있는 경우를 보정(경고)."""
        if art is None:
            return hang, ho
        if hang == 1 and not art.paragraphs:
            warnings.append("항이 하나뿐인 조문에 '제1항' 표기")
            hang = None
        if ho and hang is None and art.paragraphs and ho not in art.items:
            owners = [p.no for p in art.paragraphs.values() if ho in p.items]
            if len(owners) == 1:
                warnings.append(f"'제{ho}호'는 제{owners[0]}항 소속 — '제{owners[0]}항제{ho}호'로 표기 권장")
                hang = owners[0]
        return hang, ho

    @staticmethod
    def _missing_what(art, hang, ho, mok, label: str) -> str:
        if art is None:
            return "해당 조문 없음"
        if hang is not None and hang not in art.paragraphs:
            return f"제{hang}항 없음 ({'항 ' + str(sorted(art.paragraphs)) if art.paragraphs else '번호 있는 항 없음'})"
        items = art.paragraphs[hang].items if hang is not None else art.items
        if ho is not None and ho not in items:
            return f"제{ho}호 없음 (호 {list(items) or '없음'})"
        if mok is not None and ho is not None and ho in items and mok not in items[ho].subitems:
            return f"{mok}목 없음 (목 {list(items[ho].subitems) or '없음'})"
        return "해당 단위 없음"

    def _quote_source(self, doc, c: StatuteCite, primary_text: str, title: Optional[str], as_of: date) -> str:
        head = f"제{c.jo}조" + (f"의{c.sub}" if c.sub else "") + (f"({title})" if title else "")
        parts = [head + " " + (primary_text or "")]
        for u in c.quote_units:
            if u == c.unit:
                continue
            st2 = self.statutes.unit_status(doc, u[0], u[1], u[2], u[3], u[4], as_of)
            if st2.state == "in_force" and st2.text:
                parts.append(st2.text)
        return "\n".join(parts)

    def _verify_local(self, c: StatuteCite, doc, warnings: List[str], as_of: date) -> Finding:
        from law_api import ApiUnavailable
        m = self.statutes
        cit = f"{doc.title} {c.label}" + (f"({c.claimed_title})" if c.claimed_title else "")
        gi = m.git_info()
        mode = m.history_mode()
        src = (f"legalize-kr {doc.rel} (최신 공포 {iso(doc.promulgated)}·시행 {iso(doc.effective)}; 미러 {gi.get('head_date', '?')}, "
               f"이력 {'전체' if mode == 'full' else ('일부' if mode == 'partial' else '없음')})")
        ev: Dict[str, object] = {"law": doc.title, "file": doc.rel, "공포일자": iso(doc.promulgated),
                                 "시행일자": iso(doc.effective), "as_of": iso(as_of)}
        if doc.status == "폐지" and not c.historical:
            return Finding("statute", cit, MISMATCH, f"「{doc.title}」은(는) 폐지된 법령 — 구법 인용이면 '구'와 판본을 명시", src, warnings, ev)
        art = m.find_article(doc, c.jo, c.sub)
        hang, ho = self._adjust_units(art, c.hang, c.ho, warnings)
        st = m.unit_status(doc, c.jo, c.sub, hang, ho, c.mok, as_of)
        notes = [x for x in st.notes if x]
        ev["temporal"] = {"state": st.state, "precise": st.precise, "version": st.version, "notes": notes}
        label = c.label
        if st.state == "unknown":
            if self.api is not None:
                try:
                    f = self._verify_api(c, list(warnings), as_of)
                    f.warnings.insert(0, "로컬 미러로 기준일 문언을 확정하지 못해 DRF(시행일 기준 판본)로 확인: " + "; ".join(notes))
                    return f
                except ApiUnavailable:
                    pass
            if st.exists and not c.quotes and not c.claimed_title:
                return Finding("statute", cit, VERIFIED,
                               f"「{doc.title}」 {label} 기준일({iso(as_of)}) 존재 확인 — 문언은 시행 전 개정과 섞여 확정 못 함",
                               src, warnings + notes, ev)
            if c.future_context and st.head_exists:
                return self._check_head(c, doc, art, hang, ho, cit, src, warnings + notes + ["시행예정 조항(문맥상 명시) — 최신 공포본 기준으로 대조"], ev)
            return Finding("statute", cit, UNVERIFIABLE,
                           f"기준일({iso(as_of)}) 시행 문언을 확인하지 못함: {'; '.join(notes) or '판본 정보 부족'} "
                           "— 전체 이력 미러(`legal.py setup`) 또는 DRF 로 확인", src, warnings, ev)
        if st.state == "absent":
            if st.head_exists:
                if c.future_context:
                    return self._check_head(c, doc, art, hang, ho, cit, src, warnings + ["시행예정 조항(문맥상 명시): " + "; ".join(notes)], ev)
                return Finding("statute", cit, MISMATCH,
                               f"기준일({iso(as_of)}) 현재 시행 전인 조항(최신 공포본에만 있음): {'; '.join(notes) or '시행일 미도래'} "
                               "— 현행 조문으로 바꾸거나 '시행 예정'임을 밝히세요", src, warnings, ev)
            if art is None:
                arts = m.articles(doc)
                rng = f"제{arts[0].jo}조~제{arts[-1].jo}조" if arts else "조문 없음"
                what = f"제{c.jo}조" + (f"의{c.sub}" if c.sub else "")
                return Finding("statute", cit, NOT_FOUND, f"「{doc.title}」에 {what} 없음 (조문 범위 {rng})", src, warnings + notes, ev)
            return Finding("statute", cit, NOT_FOUND,
                           f"「{doc.title}」 {art.label}({art.title or '제목 없음'})에 {self._missing_what(art, hang, ho, c.mok, label)}"
                           + (f" — 기준일 {iso(as_of)}" if notes else ""), src, warnings + notes, ev)
        if st.state == "deleted":
            return Finding("statute", cit, MISMATCH, f"삭제된 조항 — 원문: “{(st.text or '').strip()[:60]}” (기준일 {iso(as_of)})",
                           src, warnings + notes, ev)
        # in_force
        title = st.title if st.title is not None else (art.title if art else "")
        ev.update(article=f"제{c.jo}조" + (f"의{c.sub}" if c.sub else ""), title=title, version=st.version)
        if c.claimed_title and normalize_for_match(c.claimed_title) != normalize_for_match(title or ""):
            extra = ""
            if art is not None and art.title and normalize_for_match(c.claimed_title) == normalize_for_match(art.title):
                extra = " (인용한 제목은 시행 전 개정 후 제목)"
            return Finding("statute", cit, MISMATCH, f"조문 제목 불일치: 인용 '({c.claimed_title})' ↔ 원문 '({title or '제목 없음'})'{extra}",
                           src, warnings, ev)
        if c.quotes:
            source = self._quote_source(doc, c, st.text or "", title, as_of)
            for q in c.quotes:
                if not quote_found(q, source):
                    where = label if not c.quote_units or len(c.quote_units) <= 1 else ", ".join(
                        f"제{u[0]}조" + (f"의{u[1]}" if u[1] else "") + (f"제{u[2]}항" if u[2] else "") + (f"제{u[3]}호" if u[3] else "")
                        for u in c.quote_units)
                    return Finding("statute", cit, MISMATCH,
                                   f"인용 문구가 {where}의 기준일({iso(as_of)}) 원문과 불일치: “{q[:70]}{'…' if len(q) > 70 else ''}”",
                                   src, warnings, ev)
        if st.pending_change:
            warnings.append("이 조항에는 시행 전 개정이 공포되어 있음(최신 공포본 문언과 다름) — 기준일 문언으로 대조함")
        warnings += [n for n in notes if "추정" in n or "생략" in n or "다름" in n]
        return Finding("statute", cit, VERIFIED, f"「{doc.title}」 {label}({title or '제목 없음'}) 확인 (기준일 {iso(as_of)}"
                       + (", 판본 이력 대조" if st.precise else ", 개정표시 기준") + ")", src, warnings, ev)

    def _check_head(self, c: StatuteCite, doc, art, hang, ho, cit: str, src: str, warnings: List[str], ev) -> Finding:
        """시행예정임을 문맥에서 밝힌 인용: 최신 공포본(HEAD) 문언으로 대조."""
        unit = art.unit_text(hang, ho, c.mok) if art is not None else None
        if unit is None:
            return Finding("statute", cit, NOT_FOUND, "최신 공포본에도 해당 단위 없음", src, warnings, ev)
        if c.claimed_title and normalize_for_match(c.claimed_title) != normalize_for_match(art.title or ""):
            return Finding("statute", cit, MISMATCH, f"조문 제목 불일치: 인용 '({c.claimed_title})' ↔ 최신 공포본 '({art.title})'", src, warnings, ev)
        head = art.label + (f"({art.title})" if art.title else "")
        for q in c.quotes:
            if not quote_found(q, head + " " + unit):
                return Finding("statute", cit, MISMATCH, f"인용 문구가 최신 공포본 {c.label}과 불일치: “{q[:70]}”", src, warnings, ev)
        return Finding("statute", cit, VERIFIED, f"「{doc.title}」 {c.label} — 시행예정 조항으로 확인", src, warnings, ev)

    def _verify_api(self, c: StatuteCite, warnings: List[str], as_of: Optional[date] = None) -> Finding:
        from temporal import is_deleted_unit
        as_of = as_of or self.as_of
        fl = self._api_call(self.api.find_law, c.law, as_of)
        cit = f"{c.law} {c.label}" + (f"({c.claimed_title})" if c.claimed_title else "")
        if fl.get("status") == "not_in_force":
            return Finding("statute", cit, MISMATCH if not c.future_context else UNVERIFIABLE,
                           f"기준일({iso(as_of)})에 시행 중인 판본이 없음(최초 시행 {fl.get('first_effective')})", "DRF eflaw", warnings)
        if fl.get("status") != "ok":
            return Finding("statute", cit, NOT_FOUND,
                           f"국가법령정보 목록에 정확히 일치하는 법령명 없음(후보: {', '.join(x for x in fl.get('candidates', []) if x) or '없음'})",
                           "DRF eflaw", warnings)
        cit = f"{fl['name']} {c.label}" + (f"({c.claimed_title})" if c.claimed_title else "")
        body = self._api_call(self.api.law_articles, fl["mst"], fl.get("efYd"))
        src = f"DRF eflaw MST={fl['mst']} 시행 {fl.get('efYd')} ({fl.get('state')})"
        rows = [a for a in body.get("articles", []) if a.get("jo") == c.jo and a.get("sub") == c.sub]
        if not rows:
            return Finding("statute", cit, NOT_FOUND, f"「{fl['name']}」에 제{c.jo}조" + (f"의{c.sub}" if c.sub else "") + " 없음", src, warnings)
        row = rows[0]
        art = row.get("article") or _article_from_row(row)
        hang, ho = self._adjust_units(art, c.hang, c.ho, warnings)
        unit = art.unit_text(hang, ho, c.mok)
        if unit is None:
            return Finding("statute", cit, NOT_FOUND, f"「{fl['name']}」 {art.label}에 {self._missing_what(art, hang, ho, c.mok, c.label)}",
                           src, warnings)
        if (art.deleted or row.get("deleted") or is_deleted_unit(unit)) and not c.historical:
            return Finding("statute", cit, MISMATCH, f"삭제된 조항 — 원문: “{unit.strip()[:60]}”", src, warnings)
        if c.claimed_title and normalize_for_match(c.claimed_title) != normalize_for_match(art.title or ""):
            return Finding("statute", cit, MISMATCH, f"조문 제목 불일치: '({c.claimed_title})' ↔ '({art.title})'", src, warnings)
        eff = parse_date(row.get("시행일자"))
        if eff and eff > as_of and not c.future_context:
            return Finding("statute", cit, MISMATCH, f"조문 시행일 {iso(eff)} 미도래(기준일 {iso(as_of)})", src, warnings)
        head = art.label + (f"({art.title})" if art.title else "")
        for q in c.quotes:
            if not quote_found(q, head + " " + unit):
                return Finding("statute", cit, MISMATCH, f"인용 문구가 {c.label} 원문과 불일치: “{q[:70]}”", src, warnings)
        return Finding("statute", cit, VERIFIED, f"「{fl['name']}」 {c.label}({art.title or '제목 없음'}) 확인", src, warnings,
                       {"law": fl["name"], "title": art.title, "조문시행일자": row.get("시행일자")})

    def _cross_check(self, c: StatuteCite, f: Finding, as_of: date) -> None:
        from law_api import ApiUnavailable
        try:
            g = self._verify_api(c, [], as_of)
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
            return self._verify_constitutional(c, cit)
        if c.court and norm_court(c.court) == "헌법재판소":
            return Finding("case", cit, MISMATCH, f"법원 사건번호({c.case_no})인데 '헌법재판소'로 표기됨")
        if c.decided and c.decided > self.as_of:
            return Finding("case", cit, MISMATCH, f"선고일({iso(c.decided)})이 기준일({iso(self.as_of)}) 이후 — 불가능한 날짜")
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

    def _verify_constitutional(self, c: CaseCite, cit: str) -> Finding:
        from law_api import ApiUnavailable
        if c.court and norm_court(c.court) != "헌법재판소":
            return Finding("case", cit, MISMATCH, f"헌법재판소 사건번호인데 법원이 '{c.court}'로 표기됨")
        if self.api is None:
            return Finding("case", cit, UNVERIFIABLE, "헌재 결정은 precedent-kr 에 없어 DRF(detc)가 필요(비활성)")
        try:
            hits = self._api_call(self.api.constitutional_by_number, c.case_no)
        except ApiUnavailable as e:
            return Finding("case", cit, UNVERIFIABLE, f"헌재 결정은 precedent-kr 에 없어 DRF(detc) 필요 — {e}")
        if not hits:
            return Finding("case", cit, NOT_FOUND, "국가법령정보 헌재결정례에서 사건번호 미확인", "DRF detc")
        h = hits[0]
        d = parse_date(h.get("종국일자"))
        if c.decided and d and c.decided != d:
            return Finding("case", cit, MISMATCH, f"선고일 불일치: 인용 {iso(c.decided)} ↔ 실제 {iso(d)}", "DRF detc")
        warnings: List[str] = []
        if c.quotes or c.en_banc:
            try:
                body = self._api_call(self.api.constitutional_body, h.get("헌재결정례일련번호", "")) or {}
            except ApiUnavailable as e:
                return Finding("case", cit, UNVERIFIABLE, f"결정문 본문을 받지 못해 인용문 대조 불가({e})", "DRF detc")
            summary = "\n".join(body.get(k, "") for k in ("판시사항", "결정요지"))
            full = "\n".join([summary, body.get("전문", "")])
            src_text = full if c.body_ok else (summary or full)
            if not src_text.strip():
                return Finding("case", cit, UNVERIFIABLE, "결정문 본문이 비어 있어 인용문 대조 불가", "DRF detc")
            for q in c.quotes:
                if not quote_found(q, src_text):
                    return Finding("case", cit, MISMATCH, f"인용 문구가 결정문{'' if c.body_ok else '(판시사항·결정요지)'}과 불일치: “{q[:60]}…”", "DRF detc")
        return Finding("case", cit, VERIFIED, f"헌법재판소 {iso(d)} {h.get('사건번호')} {h.get('사건명', '')}", "DRF detc",
                       warnings, {"사건명": h.get("사건명"), "종국일자": iso(d)})

    def _case_missing_local(self, c: CaseCite, cit: str, extra: str) -> Finding:
        head = self.precedents.latest_decision()
        if c.decided and head and c.decided > head:
            return Finding("case", cit, UNVERIFIABLE,
                           f"precedent-kr 의 최신 선고일({iso(head)})보다 뒤의 선고 — 미러에 아직 없을 수 있음 {extra}".strip())
        return Finding("case", cit, NOT_FOUND,
                       "precedent-kr(국가법령정보 공개판례 미러)에서 사건번호 미확인 — 가공·오기 의심, 미공개 판결이면 원문 확보 전 인용 금지 "
                       + extra, "precedent-kr")

    def _case_body_text(self, doc: Optional[Dict[str, object]], body_ok: bool) -> Tuple[str, str, bool]:
        """(요지 텍스트, 전체 텍스트, 요지 없음). 요지 = 판시사항 + 판결요지."""
        if not doc:
            return "", "", True
        s = doc.get("sections", {})
        summary = "\n".join(s.get(k, "") for k in ("판시사항", "판결요지")).strip()
        full = "\n".join(s.get(k, "") for k in ("판시사항", "판결요지", "판례내용")).strip() or str(doc.get("text", ""))
        return summary, full, not summary

    def _verify_case_local(self, c: CaseCite, cit: str) -> Optional[Finding]:
        entries = self.precedents.lookup(c.case_no)
        if not entries:
            return None
        src = "precedent-kr"
        cands = entries
        warnings: List[str] = []
        if c.court:
            want = norm_court(c.court)
            exact = [e for e in entries if norm_court(e.court) == want or (want == "대법원" and e.grade == "대법원")]
            loose = [e for e in entries if e not in exact and (norm_court(e.court).startswith(want) or want.startswith(norm_court(e.court)))]
            if not exact and not loose:
                return Finding("case", cit, MISMATCH, "법원 불일치: 인용 '%s' ↔ 실제 %s" % (
                    c.court, ", ".join(sorted({f'{e.court}({iso(e.date)})' for e in entries}))), src)
            if not exact:
                warnings.append("법원 표기가 원문과 다름(지원·재판부 표기 차이): " + ", ".join(sorted({e.court for e in loose})))
            cands = exact or loose
        elif all(e.grade != "대법원" for e in entries):
            return Finding("case", cit, UNRESOLVED, "하급심 판결은 법원명을 함께 표기해야 특정됨(같은 사건번호가 여러 법원에 있을 수 있음): 후보 "
                           + ", ".join(sorted({f"{e.court} {iso(e.date)}" for e in entries})), src)
        if c.decided:
            dated = [e for e in cands if e.date == c.decided]
            if not dated:
                return Finding("case", cit, MISMATCH, "선고일 불일치: 인용 %s ↔ 실제 %s" % (
                    iso(c.decided), ", ".join(sorted({f'{e.court} {iso(e.date)}' for e in cands}))), src)
            cands = dated
        if len({(e.court, e.date) for e in cands}) > 1 and not c.court:
            warnings.append("동일 사건번호가 여러 법원에 존재 — 법원·선고일을 함께 표기하세요: " + ", ".join(
                sorted({f"{e.court} {iso(e.date)}" for e in cands})))
        e = sorted(cands, key=lambda x: (not x.primary, x.path))[0]
        doc = None
        need_body = bool(c.quotes) or not e.primary or c.en_banc
        if need_body:
            doc = self.precedents.read(e)
        if not e.primary:
            # 병합·부번호(파일명의 두 번째 번호)는 판례일련번호 접미일 수도 있다 → 본문 메타로만 인정
            if doc is None:
                return Finding("case", cit, UNVERIFIABLE, f"사건번호가 파일명의 부번호로만 확인됨({e.path}) — 본문 메타 확인이 필요한데 본문을 읽지 못함", src)
            if c.case_no not in self.precedents.case_numbers_in_meta(doc):
                return None
        title = str((doc or {}).get("meta", {}).get("사건명", "")) if doc else ""
        if c.en_banc:
            if doc is None:
                return Finding("case", cit, UNVERIFIABLE, "'전원합의체' 표기를 확인하려면 판결 본문이 필요한데 읽지 못함", src, warnings)
            if "전원합의체" not in str(doc.get("text", "")) and "전원합의체" not in title:
                return Finding("case", cit, MISMATCH, "전원합의체 판결이 아님(판결문에 '전원합의체' 없음)", src, warnings)
        if c.quotes:
            if doc is None and self.api is not None:
                try:
                    return self._verify_case_api(c, cit, prefix_warnings=warnings + ["로컬 본문을 읽지 못해 DRF 본문으로 대조"])
                except Exception:   # noqa: BLE001 — DRF 실패는 아래 fail-closed 로 처리
                    pass
            if doc is None:
                return Finding("case", cit, UNVERIFIABLE, "인용문 대조에 판결 본문이 필요한데 읽지 못함(부분 클론 오프라인·지연 내려받기 실패)", src, warnings)
            summary, full, no_summary = self._case_body_text(doc, c.body_ok)
            for q in c.quotes:
                if summary and quote_found(q, summary):
                    continue
                if quote_found(q, full):
                    if c.body_ok or no_summary:
                        warnings.append("판결 이유(본문)에서 확인한 문구 — 법원의 판단인지 당사자 주장·사실 인용인지 감사 시 확인")
                        continue
                    return Finding("case", cit, MISMATCH,
                                   f"판시사항·판결요지에 없는 문구(판결 이유 본문에만 있음): “{q[:60]}…” — 판결 이유를 인용한 것이면 "
                                   "'위 판결은 이유에서 …'처럼 밝히세요(당사자 주장·사실관계 문구를 판시로 인용하는 오류 방지)", src, warnings)
                return Finding("case", cit, MISMATCH, f"인용 문구가 판결문과 불일치: “{q[:60]}…” (사건명: {title})", src, warnings)
        return Finding("case", cit, VERIFIED, f"{e.court} {iso(e.date)} 선고 {c.case_no}" + (f" [{title}]" if title else ""),
                       f"{src} {e.path}", warnings, {"court": e.court, "date": iso(e.date), "path": e.path, "사건명": title})

    def _verify_case_api(self, c: CaseCite, cit: str, prefix_warnings: Optional[List[str]] = None) -> Finding:
        from law_api import ApiUnavailable
        hits = self._api_call(self.api.precedent_by_number, c.case_no)
        hits = [h for h in hits if (h.get("사건번호") or "").replace(" ", "").find(c.case_no) >= 0] or []
        if not hits:
            if self.precedents:
                return self._case_missing_local(c, cit, "(DRF 판례 목록에서도 미확인)")
            return Finding("case", cit, NOT_FOUND, "국가법령정보 판례 목록(nb 사건번호 검색)에서 미확인 — 미공개 판결이면 원문 확보 전 인용 금지", "DRF prec")
        if c.court:
            want = norm_court(c.court)
            hh = [h for h in hits if norm_court(h.get("법원명", "")) == want or norm_court(h.get("법원명", "")).startswith(want)]
            if not hh:
                return Finding("case", cit, MISMATCH, f"법원 불일치: 인용 '{c.court}' ↔ 실제 {', '.join(h.get('법원명') or '?' for h in hits)}", "DRF prec")
            hits = hh
        if c.decided:
            hh = [h for h in hits if parse_date(h.get("선고일자")) == c.decided]
            if not hh:
                return Finding("case", cit, MISMATCH, f"선고일 불일치: 인용 {iso(c.decided)} ↔ 실제 {', '.join(iso(parse_date(h.get('선고일자'))) for h in hits)}", "DRF prec")
            hits = hh
        h = hits[0]
        warnings: List[str] = list(prefix_warnings or [])
        if c.quotes or c.en_banc:
            try:
                body = self._api_call(self.api.precedent_body, h.get("판례일련번호", "")) or {}
            except ApiUnavailable as e:
                return Finding("case", cit, UNVERIFIABLE, f"판결 본문을 받지 못해 대조 불가({e})", "DRF prec", warnings)
            summary = "\n".join(body.get(k, "") for k in ("판시사항", "판결요지")).strip()
            full = "\n".join(body.get(k, "") for k in ("판시사항", "판결요지", "판례내용")).strip()
            if not full:
                return Finding("case", cit, UNVERIFIABLE, "판결 본문 미제공(데이터출처에 따라 본문 없음) — 대조 불가", "DRF prec", warnings)
            if c.en_banc and "전원합의체" not in full and "전원합의체" not in h.get("사건명", ""):
                return Finding("case", cit, MISMATCH, "전원합의체 판결이 아님", "DRF prec", warnings)
            for q in c.quotes:
                if summary and quote_found(q, summary):
                    continue
                if quote_found(q, full) and (c.body_ok or not summary):
                    warnings.append("판결 이유(본문)에서 확인한 문구")
                    continue
                return Finding("case", cit, MISMATCH, f"판시 인용문이 판결문{'' if c.body_ok else '(판시사항·판결요지)'}과 불일치: “{q[:60]}…”",
                               "DRF prec", warnings)
        return Finding("case", cit, VERIFIED, f"{h.get('법원명') or ''} {iso(parse_date(h.get('선고일자')))} {h.get('사건번호')} [{h.get('사건명', '')}]",
                       f"DRF prec ID={h.get('판례일련번호')}", warnings, {"사건명": h.get("사건명")})

    # ---- 해석례 ----
    def verify_authority(self, a: AuthorityCite) -> Finding:
        from law_api import ApiUnavailable
        if a.ministry:
            return Finding("authority", f"부처 행정해석 {a.agenda_no}", UNVERIFIABLE,
                           "부처 행정해석 문서번호는 공개 DB 에서 번호로 확인할 수 없음 — 회시 원문 사본을 확보해 첨부하거나 인용을 빼세요")
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
        if a.quotes:
            try:
                body = self._api_call(self.api.interpretation_body, h.get("법령해석례일련번호", ""), a.target) or {}
            except ApiUnavailable as e:
                return Finding("authority", cit, UNVERIFIABLE, f"해석례 본문을 받지 못해 인용문 대조 불가({e})", "DRF expc")
            text = "\n".join(body.get(k, "") for k in ("질의요지", "회답", "이유"))
            if not text.strip():
                return Finding("authority", cit, UNVERIFIABLE, "해석례 본문이 비어 있어 인용문 대조 불가", "DRF expc")
            for q in a.quotes:
                if not quote_found(q, text):
                    return Finding("authority", cit, MISMATCH, f"인용 문구가 해석례와 불일치: “{q[:60]}…”", "DRF expc")
        return Finding("authority", cit, VERIFIED, f"{h.get('안건명', '')} (회신 {h.get('회신일자', '')})", "DRF expc")

    # ---- 단건(게이트용) ----
    def verify_one(self, c) -> Finding:
        if isinstance(c, StatuteCite):
            return self.verify_statute(c)
        if isinstance(c, CaseCite):
            return self.verify_case(c)
        return self.verify_authority(c)

    def verify_entry(self, citation: str, quote: Optional[str] = None, kind: Optional[str] = None) -> Tuple[Optional[Finding], List[str]]:
        """증거 JSON 한 항목: citation 필드에 인용이 정확히 하나, quote 는 그 인용에 명시적으로 붙여 대조.
        반환: (판정, 형식 문제 목록)"""
        problems: List[str] = []
        if not isinstance(citation, str) or not citation.strip():
            return None, ["citation 이 문자열이 아님/비어 있음"]
        ex = extract_all(citation)
        cites = ex.citations()
        if kind == "statute":
            cites = [x for x in cites if isinstance(x, StatuteCite)]
        elif kind == "case":
            cites = [x for x in cites if not isinstance(x, StatuteCite)]
        if len(cites) != 1:
            return None, [f"citation 필드에는 인용 하나만: '{citation[:80]}' ({len(cites)}건 인식)"]
        c = cites[0]
        if quote is not None:
            if not isinstance(quote, str) or not quote.strip():
                return None, ["quote 가 문자열이 아님/비어 있음"]
            q = prep_text(quote).strip()
            c.quotes = [q]
            if isinstance(c, StatuteCite):
                c.quote_units = [c.unit]
            if isinstance(c, CaseCite):
                c.body_ok = bool(_BODY_CTX_RE.search(citation))
        return self.verify_one(c), problems

    # ---- 문서 ----
    def verify_text(self, text: str, evidence_text: Optional[str] = None, workers: int = 6) -> Dict[str, object]:
        ex = extract_all(text)
        jobs: List[object] = []
        seen: Dict[Tuple, int] = {}
        for c in ex.citations():
            k = c.key()
            if k in seen:
                continue
            seen[k] = len(jobs)
            jobs.append(c)
        if jobs:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
                findings = list(pool.map(self.verify_one, jobs))
        else:
            findings = []
        for q in ex.orphans:
            findings.append(Finding("quote", f"인용문 “{q.text[:40]}{'…' if len(q.text) > 40 else ''}”", UNRESOLVED,
                                    "원문을 인용했다고 하나(규정·판시 등) 어느 조문·판결의 문언인지 특정할 수 없음 — "
                                    "같은 문장에 출처를 밝히거나 “…”(「법령」 제N조)처럼 쓰세요"))
        closed = []
        if evidence_text is not None:
            ev = extract_all(evidence_text)

            def skeys(x: StatuteCite):
                return {(normalize_law_name(n), x.jo, x.sub) for n in ([x.law] if x.law else []) + list(x.law_candidates)}
            ev_keys = set().union(*[skeys(x) for x in ev.statutes]) if ev.statutes else set()
            ev_cases = {x.case_no for x in ev.cases}
            for c, f in zip(jobs, findings):
                if isinstance(c, CaseCite):
                    outside = c.case_no not in ev_cases
                elif isinstance(c, StatuteCite):
                    outside = bool(c.law) and not (skeys(c) & ev_keys)
                else:
                    outside = False
                if outside:
                    f.warnings.append("증거 패킷(evidence)에 없는 인용 — 조사관 검증 범위 밖")
                    closed.append(f.citation)
        return self.summarize(findings, closed)

    def summarize(self, findings: List[Finding], closed: Optional[List[str]] = None) -> Dict[str, object]:
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
                "outside_evidence": list(closed or []), "sources": self.sources(),
                "findings": [asdict(f) for f in findings]}

    _summarize = summarize


def _article_from_row(row: Dict[str, object]):
    """(하위호환) DRF 결과가 조문 객체 없이 dict 로만 올 때: paragraphs {n: text}, items {'항:호': text}."""
    from legal_engine import Article, Item, Paragraph
    art = Article(jo=int(row.get("jo") or 0), sub=row.get("sub"), title=str(row.get("title") or ""), heading="",
                  text=str(row.get("text") or ""), line=0, deleted=bool(row.get("deleted")))
    for n, t in (row.get("paragraphs") or {}).items():
        art.paragraphs[int(n)] = Paragraph(no=int(n), text=str(t).split("\n")[0])
    for k, t in (row.get("items") or {}).items():
        hk, _, ino = str(k).partition(":")
        item = Item(no=ino, text=str(t).split("\n")[0])
        for line in str(t).split("\n")[1:]:
            mm = re.match(r"^\s*([가-힣])\s*\.", line)
            if mm:
                item.subitems[mm.group(1)] = line.strip()
        h = int(hk or 0)
        if h and h in art.paragraphs:
            art.paragraphs[h].items[ino] = item
        else:
            art.items[ino] = item
    return art


# ---------------------------------------------------------------------------
# 보고서
# ---------------------------------------------------------------------------

_BADGE = {"PASS": "✅ PASS — 모든 인용 확인", "FAIL": "❌ FAIL — 불일치·미확인·특정불가 인용 존재",
          "INCOMPLETE": "⚠️ INCOMPLETE — 일부 인용을 출처 부재로 검증하지 못함",
          "NO_CITATIONS": "⚠️ NO_CITATIONS — 검증할 인용이 없음(통과 아님)"}
_ICON = {VERIFIED: "✅ 확인", MISMATCH: "❌ 불일치", NOT_FOUND: "❌ 미확인", UNRESOLVED: "❌ 특정불가", UNVERIFIABLE: "⚠️ 검증불가"}


def render_markdown(rep: Dict[str, object], title: str = "인용 검증 보고서") -> str:
    src = rep["sources"]
    lk, pk, api = src.get("legalize-kr", {}), src.get("precedent-kr", {}), src.get("drf_api", {})
    c = rep["counts"]
    hist = lk.get("history_mode") or ("full" if lk.get("history") else "none")
    lines = [f"## 🛡️ {title} (legal-ultra citation verifier)", "",
             f"- **판정**: {_BADGE[rep['verdict']]}",
             f"- **기준일(as-of)**: {rep['as_of']}",
             f"- **출처**: legalize-kr {'사용(미러 ' + str(lk.get('head_date')) + ', 판본 이력 ' + {'full': '전체', 'partial': '일부', 'none': '없음'}.get(hist, hist) + ')' if lk.get('available') else '없음'}"
             f" · precedent-kr {'사용(최신 선고 ' + str(pk.get('latest_decision')) + ')' if pk.get('available') else '없음'}"
             f" · DRF API {api.get('state') if api.get('enabled') else '비활성'}",
             f"- **인용 {rep['total']}건**: 확인 {c[VERIFIED]} · 불일치 {c[MISMATCH]} · 미확인 {c[NOT_FOUND]} · 특정불가 {c[UNRESOLVED]} · 검증불가 {c[UNVERIFIABLE]}",
             ""]
    if rep["findings"]:
        lines += ["| # | 인용 | 판정 | 근거·사유 | 출처 |", "|---:|---|:---:|---|---|"]
        for i, f in enumerate(rep["findings"], 1):
            detail = f["detail"].replace("|", "\\|").replace("\n", " ")
            if f["warnings"]:
                detail += "<br>⚠ " + "<br>⚠ ".join(w.replace("|", "\\|").replace("\n", " ") for w in f["warnings"] if w)
            lines.append(f"| {i} | {f['citation'].replace('|', '/')} | {_ICON[f['status']]} | {detail} | {f['source'].replace('|', '/')} |")
        lines.append("")
    if rep.get("outside_evidence"):
        lines.append(f"- 증거 패킷 밖 인용 {len(rep['outside_evidence'])}건: 감사관 확인 필요")
    lines.append("> 이 보고서는 스크립트가 출처(legalize-kr·precedent-kr·국가법령정보 DRF)와 대조해 자동 생성한 것이며, "
                 "법리 판단의 타당성까지 보증하지 않습니다.")
    return "\n".join(lines)


def dumps(rep: Dict[str, object]) -> str:
    return json.dumps(rep, ensure_ascii=False, indent=2, default=str)
