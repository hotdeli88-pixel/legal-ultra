"""law_api.py - 법제처 국가법령정보 공동활용 OPEN API(DRF) 클라이언트 (legal-ultra v2).

엔드포인트: https://www.law.go.kr/DRF/lawSearch.do (목록) · lawService.do (본문), 인증 OC.

target 별 응답 스키마(목록 루트·레코드 태그·식별자·본문 파라미터·본문 루트)는 이름에서 유추할 수 없다.
아래 TARGETS 표의 값은 rubatoyd/law-openapi-mcp(MIT, 2026-09-08 라이브 실측, docs/LAW_API_GUIDE.md·
src/law_mcp/config.py)가 공개한 측정 결과를 옮긴 것이다. 이 스킬 작성 환경에서는 law.go.kr 이
네트워크 정책으로 차단되어 직접 재측정하지 못했다 → 사용자 환경에서 `legal.py api-probe` 로 확인할 것.

v1(law 스킬) 대비 수정:
  - 부처 해석 target 순서 오류(cgmExpcMoel → moelCgmExpc), 특별행정심판(specialDeccTt → ttSpecialDecc)
  - 자치법규 레코드 태그(ordin → law)·본문 파라미터(ID → MST)·본문 루트(OrdinService → LawService),
    헌재 레코드 태그(detc → Detc) 오류로 항상 0건이던 문제
  - 판례 사건번호 검색에 nb 파라미터 사용(제목검색 query 로는 사건번호가 잡히지 않음)
  - 모든 실패가 HTTP 200 으로 오는 API 특성: 빈 본문/HTML/인증실패/없음(<Law>…없습니다</Law>)을 구분
    → '접속 불가'가 '존재하지 않음(환각)'으로 둔갑하지 않는다. 비JSON 응답에서 크래시하던 문제 제거
  - 캐시 키에서 OC 제외, 오류 응답은 캐시하지 않음
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_common import cache_root, normalize_law_name, parse_date  # noqa: E402

SEARCH_URL = os.environ.get("LAW_BASE_URL", "https://www.law.go.kr/DRF").rstrip("/") + "/lawSearch.do"
SERVICE_URL = os.environ.get("LAW_BASE_URL", "https://www.law.go.kr/DRF").rstrip("/") + "/lawService.do"


class Target(NamedTuple):
    code: str
    label: str
    list_root: str
    record_tag: str
    id_field: str
    id_param: str
    body_root: str
    title_field: str
    has_body: bool = True


def _t(*a, **k) -> Target:
    return Target(*a, **k)


# 출처: rubatoyd/law-openapi-mcp (MIT) — 2026-09-08 라이브 실측 레지스트리.
TARGETS: Dict[str, Target] = {
    "law": _t("law", "현행법령", "LawSearch", "law", "법령일련번호", "MST", "법령", "법령명한글"),
    "eflaw": _t("eflaw", "시행일법령", "LawSearch", "law", "법령일련번호", "MST", "법령", "법령명한글"),
    "elaw": _t("elaw", "영문법령", "LawSearch", "law", "법령일련번호", "MST", "Law", "법령명한글"),
    "admrul": _t("admrul", "행정규칙", "AdmRulSearch", "admrul", "행정규칙일련번호", "ID", "AdmRulService", "행정규칙명"),
    "ordin": _t("ordin", "자치법규", "OrdinSearch", "law", "자치법규일련번호", "MST", "LawService", "자치법규명"),
    "prec": _t("prec", "판례", "PrecSearch", "prec", "판례일련번호", "ID", "PrecService", "사건명"),
    "detc": _t("detc", "헌재결정례", "DetcSearch", "Detc", "헌재결정례일련번호", "ID", "DetcService", "사건명"),
    "expc": _t("expc", "법령해석례(법제처)", "Expc", "expc", "법령해석례일련번호", "ID", "ExpcService", "안건명"),
    "decc": _t("decc", "행정심판례", "Decc", "decc", "행정심판재결례일련번호", "ID", "PrecService", "사건명"),
    "trty": _t("trty", "조약", "TrtySearch", "Trty", "조약일련번호", "ID", "BothTrtyService", "조약명"),
}
for _code, _label, _title in [
    ("ppc", "개인정보보호위원회", "안건명"), ("eiac", "고용보험심사위원회", "사건명"),
    ("ftc", "공정거래위원회", "사건명"), ("acr", "국민권익위원회", "제목"), ("fsc", "금융위원회", "안건명"),
    ("nlrc", "노동위원회", "제목"), ("kcc", "방송미디어통신위원회", "안건명"),
    ("iaciac", "산업재해보상보험재심사위원회", "사건"), ("oclt", "중앙토지수용위원회", "제목"),
    ("ecc", "중앙환경분쟁조정위원회", "사건명"), ("sfc", "증권선물위원회", "안건명"), ("nhrck", "국가인권위원회", "사건명"),
]:
    TARGETS[_code] = _t(_code, f"{_label} 결정문", _code[0].upper() + _code[1:], _code, "결정문일련번호", "ID",
                        _code[0].upper() + _code[1:] + "Service", _title)
for _code, _label in [("ttSpecialDecc", "조세심판원"), ("kmstSpecialDecc", "해양안전심판원"),
                      ("acrSpecialDecc", "국민권익위원회 특별행정심판"), ("adapSpecialDecc", "인사혁신처 소청심사위원회")]:
    TARGETS[_code] = _t(_code, _label, "Decc", "decc", "특별행정심판재결례일련번호", "ID", "SpecialDeccService", "사건명")
_MINISTRIES = {
    "moe": "교육부", "moel": "고용노동부", "molit": "국토교통부", "moef": "재정경제부", "mof": "해양수산부",
    "mois": "행정안전부", "me": "기후에너지환경부", "kcs": "관세청", "nts": "국세청", "msit": "과학기술정보통신부",
    "mpva": "국가보훈부", "mnd": "국방부", "mafra": "농림축산식품부", "mcst": "문화체육관광부", "moj": "법무부",
    "mohw": "보건복지부", "motie": "산업통상부", "mogef": "성평등가족부", "mofa": "외교부", "mss": "중소벤처기업부",
    "mou": "통일부", "moleg": "법제처", "mfds": "식품의약품안전처", "mpm": "인사혁신처", "kma": "기상청",
    "khs": "국가유산청", "rda": "농촌진흥청", "npa": "경찰청", "dapa": "방위사업청", "mma": "병무청", "kfs": "산림청",
    "nfa": "소방청", "oka": "재외동포청", "pps": "조달청", "kdca": "질병관리청", "kostat": "국가데이터처",
    "kipo": "지식재산처", "kcg": "해양경찰청", "naacc": "행정중심복합도시건설청",
}
for _p, _label in _MINISTRIES.items():
    _code = f"{_p}CgmExpc"
    TARGETS[_code] = _t(_code, f"{_label} 1차 법령해석", "CgmExpc", "cgmExpc", "법령해석일련번호", "ID",
                        "CgmExpcService", "안건명", has_body=_p not in ("moef", "nts"))

COMMITTEES = ["ppc", "eiac", "ftc", "acr", "fsc", "nlrc", "kcc", "iaciac", "oclt", "ecc", "sfc", "nhrck"]


# ---------------------------------------------------------------------------
# 오류
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """응답은 왔지만 쓸 수 없음(빈 본문·HTML·파싱 실패)."""


class ApiUnavailable(ApiError):
    """네트워크/프록시/타임아웃 등으로 확인 자체를 못함 → 검증 상태 UNVERIFIABLE."""


class ApiAuthError(ApiError):
    """OC 검증 실패(미등록 OC·IP 불일치)."""


# ---------------------------------------------------------------------------
# XML 도우미
# ---------------------------------------------------------------------------

_KNOWN_TAG_RE = re.compile(r"</?(?:br|p|div|span|img|b|i|u|font)\b[^>]*>", re.I)


def clean_text(s: Optional[str]) -> str:
    """알려진 HTML 태그만 제거한다. '<개정 2010.7.6>' 같은 본문 꺾쇠는 보존."""
    s = _KNOWN_TAG_RE.sub("\n", s or "")
    return re.sub(r"[ \t]+\n", "\n", s).strip()


def _flat(el: ET.Element) -> Dict[str, str]:
    return {c.tag: clean_text(c.text) for c in el if len(c) == 0}


def _classify(raw: bytes, content_type: str = "") -> Tuple[str, Optional[ET.Element], str]:
    """(kind, root, message). kind: ok | not_found | auth | empty | html | bad."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return "empty", None, "빈 응답(0바이트) — 존재하지 않는 target 이거나 서버 오류"
    low = text[:200].lower()
    if low.startswith("<!doctype") or low.startswith("<html") or "text/html" in content_type.lower():
        return "html", None, "HTML 응답 — 파라미터 부족(예: eflaw 본문은 efYd 필요) 또는 XML 미지원 target"
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return "bad", None, f"XML 파싱 실패: {e}"
    if root.tag == "Response":
        msg = " ".join((c.text or "").strip() for c in root)
        if "검증에 실패" in msg or "인증" in msg:
            return "auth", root, msg
        return "bad", root, msg
    if root.tag == "Law" and len(root) == 0 and "없습니다" in (root.text or ""):
        return "not_found", root, (root.text or "").strip()
    return "ok", root, ""


# ---------------------------------------------------------------------------
# 클라이언트
# ---------------------------------------------------------------------------


class DrfClient:
    def __init__(self, oc: Optional[str] = None, timeout: float = 12.0, retries: int = 2,
                 use_cache: bool = True, offline: Optional[bool] = None):
        self.oc = (oc or os.environ.get("LAW_OPENAPI_OC") or os.environ.get("LAW_OC") or "test").strip()
        self.oc_is_default = not (oc or os.environ.get("LAW_OPENAPI_OC") or os.environ.get("LAW_OC"))
        self.timeout = timeout
        self.retries = retries
        self.use_cache = use_cache
        self.offline = (os.environ.get("LEGAL_ULTRA_OFFLINE") == "1") if offline is None else offline
        self._down_reason: Optional[str] = None  # 한 번 접속 불가면 같은 실행에서 재시도 폭주 방지
        self.cache_dir = cache_root() / "drf"

    # ---- 저수준 --------------------------------------------------------------

    def _cache_path(self, url_wo_oc: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url_wo_oc.encode("utf-8")).hexdigest() + ".xml")

    def fetch(self, endpoint: str, params: Dict[str, object], ttl: int = 86400,
              fmt: str = "XML") -> Tuple[str, Optional[ET.Element], bytes]:
        """(kind, root, raw). ApiUnavailable/ApiAuthError/ApiError 를 던질 수 있다."""
        if self.offline:
            raise ApiUnavailable("오프라인 모드(LEGAL_ULTRA_OFFLINE=1)")
        if self._down_reason:
            raise ApiUnavailable(self._down_reason)
        url = SEARCH_URL if endpoint == "search" else SERVICE_URL
        q = {k: v for k, v in params.items() if v is not None and v != ""}
        q["type"] = fmt
        key_url = url + "?" + urllib.parse.urlencode(sorted(q.items()))
        cpath = self._cache_path(key_url)
        if self.use_cache and ttl > 0 and cpath.is_file() and time.time() - cpath.stat().st_mtime < ttl:
            raw = cpath.read_bytes()
            kind, root, _ = _classify(raw) if fmt == "XML" else ("ok", None, "")
            if kind == "ok":
                return kind, root, raw
        full = url + "?" + urllib.parse.urlencode({"OC": self.oc, **q})
        req = urllib.request.Request(full, headers={"User-Agent": "legal-ultra/2.0 (+skill)",
                                                    "Accept": "application/xml, application/json, */*"})
        raw: Optional[bytes] = None
        ctype = ""
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                break
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (400, 401, 403, 404, 407):  # 재시도해도 같은 결과
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if "403" in str(e) or "407" in str(e):  # 프록시 CONNECT 거부(정책 차단)
                    break
            if attempt < self.retries:
                time.sleep(0.6 * (2 ** attempt))
        if raw is None:
            self._down_reason = f"법제처 API 접속 불가: {type(last).__name__}: {last}"
            raise ApiUnavailable(self._down_reason)
        if fmt != "XML":
            return "ok", None, raw
        kind, root, msg = _classify(raw, ctype)
        if kind == "auth":
            raise ApiAuthError(f"OC 인증 실패({self.oc}): {msg}")
        if kind in ("empty", "html", "bad"):
            raise ApiError(msg)
        if kind == "ok" and self.use_cache and ttl > 0:
            try:
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_bytes(raw)
            except OSError:
                pass
        return kind, root, raw

    def ping(self) -> Dict[str, object]:
        """가벼운 목록 호출로 사용 가능 여부 확인."""
        try:
            self.search("law", "민법", display=1, ttl=0)
            return {"available": True, "oc": "test(공용 시험계정)" if self.oc_is_default else "사용자 OC"}
        except ApiAuthError as e:
            return {"available": False, "reason": str(e)}
        except ApiError as e:
            return {"available": False, "reason": str(e)}

    # ---- 목록/본문 -------------------------------------------------------------

    def search(self, target: str, query: str, display: int = 20, page: int = 1,
               search_scope: Optional[int] = None, ttl: int = 86400, **extra) -> Dict[str, object]:
        t = TARGETS.get(target)
        if t is None:
            raise ApiError(f"알 수 없는 target: {target} (없는 target 은 API가 빈 응답을 준다 — 호출 전 차단)")
        if not (query or "").strip() and not extra:
            raise ApiError("빈 검색어: 이 API 는 query 가 없으면 전체 카탈로그를 반환한다")
        params: Dict[str, object] = {"target": target, "display": min(int(display), 100), "page": page}
        if query:
            params["query"] = query
        if search_scope:
            params["search"] = search_scope
        params.update(extra)
        kind, root, _ = self.fetch("search", params, ttl=ttl)
        if kind == "not_found" or root is None:
            return {"total": 0, "items": []}
        total = 0
        tc = root.find("totalCnt")
        if tc is not None and (tc.text or "").strip().isdigit():
            total = int(tc.text.strip())
        recs = root.findall(t.record_tag)
        if not recs:  # 레지스트리 불일치 대비: 자식이 있는 첫 레코드 태그를 사용
            recs = [c for c in root if len(c) > 0]
        items = []
        for r in recs:
            d = _flat(r)
            d["_id"] = d.get(t.id_field, "")
            d["_title"] = d.get(t.title_field, "")
            items.append(d)
        return {"total": total, "items": items}

    def body(self, target: str, id_value: str, ttl: int = 7 * 86400, **extra) -> Optional[ET.Element]:
        t = TARGETS[target]
        if not t.has_body:
            raise ApiError(f"{t.label}({target})는 본문 조회를 지원하지 않는다(목록만)")
        kind, root, _ = self.fetch("service", {"target": target, t.id_param: id_value, **extra}, ttl=ttl)
        if kind == "not_found":
            return None
        return root

    # ---- 법령 --------------------------------------------------------------

    def find_law(self, name: str, as_of=None) -> Dict[str, object]:
        """정식 법령명 또는 약칭의 **정확 일치**만. eflaw 목록에서 as_of 기준 시행 판본을 고른다."""
        q = normalize_law_name(name)
        res = self.search("eflaw", name, display=100)
        exact = [i for i in res["items"]
                 if normalize_law_name(i.get("법령명한글", "")) == q or normalize_law_name(i.get("법령약칭명", "")) == q]
        if not exact:
            return {"status": "not_found", "candidates": [i.get("법령명한글") for i in res["items"][:5]]}
        official = exact[0].get("법령명한글", "")
        versions = [i for i in exact if normalize_law_name(i.get("법령명한글", "")) == normalize_law_name(official)]
        chosen = None
        if as_of is not None:
            ok = [i for i in versions if (parse_date(i.get("시행일자")) or as_of) <= as_of]
            ok.sort(key=lambda i: i.get("시행일자", ""), reverse=True)
            chosen = ok[0] if ok else None
        if chosen is None:
            cur = [i for i in versions if "현행" in i.get("현행연혁코드", "")]
            chosen = cur[0] if cur else versions[0]
        return {"status": "ok", "name": official, "mst": chosen.get("법령일련번호"),
                "efYd": chosen.get("시행일자"), "state": chosen.get("현행연혁코드"),
                "versions": [(i.get("시행일자"), i.get("현행연혁코드")) for i in versions]}

    def law_articles(self, mst: str, ef_yd: Optional[str] = None) -> Dict[str, object]:
        """법령 본문 → {'info': 기본정보, 'articles': [{jo, sub, title, 시행일자, text, paragraphs{n: text}, items}]}"""
        if ef_yd:
            root = self.body("eflaw", mst, efYd=ef_yd)
        else:
            root = self.body("law", mst)
        if root is None:
            return {"info": {}, "articles": []}
        info = _flat(root.find("기본정보")) if root.find("기본정보") is not None else {}
        arts = []
        units = root.find("조문")
        for u in (units.findall("조문단위") if units is not None else []):
            f = _flat(u)
            if f.get("조문여부") != "조문":   # '전문' = 장·절 표제 (v1 은 이것을 조문으로 반환)
                continue
            try:
                jo = int(f.get("조문번호", "0"))
            except ValueError:
                continue
            sub = int(f["조문가지번호"]) if f.get("조문가지번호", "").strip("0") else None
            paragraphs: Dict[int, str] = {}
            items: Dict[str, str] = {}
            texts = [f.get("조문내용", "")]
            for h in u.findall("항"):
                hf = _flat(h)
                hno = hf.get("항번호", "")
                n = _circled_to_int(hno)
                ptxt = hf.get("항내용", "")
                sub_items = []
                for ho in h.findall("호"):
                    hof = _flat(ho)
                    ino = re.sub(r"[.\s]", "", hof.get("호번호", ""))
                    itxt = hof.get("호내용", "")
                    for mok in ho.findall("목"):
                        itxt += "\n" + _flat(mok).get("목내용", "")
                    sub_items.append((ino, itxt))
                if n:
                    paragraphs[n] = "\n".join([ptxt] + [t for _, t in sub_items])
                for ino, itxt in sub_items:
                    items[f"{n or 0}:{ino}"] = itxt
                texts.append(ptxt)
                texts += [t for _, t in sub_items]
            arts.append({"jo": jo, "sub": sub, "title": f.get("조문제목", ""),
                         "시행일자": f.get("조문시행일자", ""), "text": "\n".join(t for t in texts if t),
                         "paragraphs": paragraphs, "items": items,
                         "deleted": bool(re.match(r"^제\d+조(?:의\d+)?\s*삭제", f.get("조문내용", "")))})
        return {"info": info, "articles": arts}

    def three_tier(self, mst: str, knd: str = "2") -> Dict[str, object]:
        """3단비교(knd 1=인용조문, 2=위임조문). JSON 루트: LspttnThdCmpLawXService."""
        _, _, raw = self.fetch("service", {"target": "thdCmp", "MST": mst, "knd": knd}, fmt="JSON")
        try:
            data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except ValueError:
            raise ApiError("3단비교 응답이 JSON 이 아님")
        svc = data.get("LspttnThdCmpLawXService") if isinstance(data, dict) else None
        if not isinstance(svc, dict):
            return {"found": False, "raw_keys": list(data.keys()) if isinstance(data, dict) else []}
        return {"found": True, "기본정보": svc.get("기본정보", {}), "위임조문삼단비교": svc.get("위임조문삼단비교", {})}

    # ---- 판례·결정례·해석례 ---------------------------------------------------

    def precedent_by_number(self, case_no: str) -> List[Dict[str, str]]:
        """사건번호 정확 일치 목록(nb 파라미터). 국세법령정보시스템 출처는 '서울고등법원-2025-누-7507' 꼴이라 정규화 비교."""
        from precedent_engine import normalize_case_no
        want = normalize_case_no(case_no)
        res = self.search("prec", "", display=20, nb=case_no)
        return [i for i in res["items"] if normalize_case_no(i.get("사건번호", "")) == want]

    def precedent_body(self, prec_id: str) -> Optional[Dict[str, str]]:
        root = self.body("prec", prec_id)
        return _flat(root) if root is not None else None

    def constitutional_by_number(self, case_no: str) -> List[Dict[str, str]]:
        """헌재 사건번호 — 목록 검색 파라미터가 문서화되지 않아 본문검색(search=2)으로 찾고 정확 일치만 채택."""
        from precedent_engine import normalize_case_no
        want = normalize_case_no(case_no)
        hits: List[Dict[str, str]] = []
        for scope in (2, 1):
            res = self.search("detc", case_no, display=50, search_scope=scope)
            hits = [i for i in res["items"] if normalize_case_no(i.get("사건번호", "")) == want]
            if hits:
                break
        return hits

    def constitutional_body(self, detc_id: str) -> Optional[Dict[str, str]]:
        root = self.body("detc", detc_id)
        return _flat(root) if root is not None else None

    def interpretation_by_number(self, agenda_no: str, target: str = "expc") -> List[Dict[str, str]]:
        hits: List[Dict[str, str]] = []
        for scope in (2, 1):
            res = self.search(target, agenda_no, display=50, search_scope=scope)
            hits = [i for i in res["items"] if (i.get("안건번호") or "").replace(" ", "") == agenda_no.replace(" ", "")]
            if hits:
                break
        return hits

    def interpretation_body(self, expc_id: str, target: str = "expc") -> Optional[Dict[str, str]]:
        root = self.body(target, expc_id)
        return _flat(root) if root is not None else None


def _circled_to_int(s: str) -> int:
    from kr_common import CIRCLED
    s = (s or "").strip()
    if s and s[0] in CIRCLED:
        return CIRCLED[s[0]]
    m = re.match(r"^제?(\d+)항?$", s)
    return int(m.group(1)) if m else 0


def probe(client: DrfClient) -> List[Dict[str, object]]:
    """레지스트리 실측 확인: 각 target 의 목록 루트·레코드 태그가 기대와 같은지."""
    out = []
    for code in ["law", "eflaw", "prec", "detc", "expc", "admrul", "ordin", "decc", "ppc", "ftc", "nlrc",
                 "moelCgmExpc", "ttSpecialDecc"]:
        t = TARGETS[code]
        row: Dict[str, object] = {"target": code, "expect_root": t.list_root, "expect_record": t.record_tag}
        try:
            kind, root, _ = client.fetch("search", {"target": code, "query": "법", "display": 3}, ttl=0)
            if root is not None:
                row["root"] = root.tag
                tags = sorted({c.tag for c in root if len(c) > 0})
                row["records"] = tags
                row["ok"] = root.tag == t.list_root and (t.record_tag in tags or not tags)
            else:
                row["ok"] = kind == "not_found"
        except ApiError as e:
            row["ok"] = False
            row["error"] = str(e)
        out.append(row)
    return out
