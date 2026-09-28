"""legal.py - legal-ultra v2 통합 CLI (조사·검증 도구). 추론은 에이전트가, 사실 확인은 이 도구가 한다.

  python legal.py status                              출처 상태(legalize-kr · precedent-kr · 법제처 DRF API)
  python legal.py setup [--dest DIR] [--shallow] [--precedent-bodies]
  python legal.py law <법령명>                        법령 해석(정식명·판본·시행일)
  python legal.py article <법령명> <조문> [--hang N] [--ho N] [--mok 가] [--as-of YYYY-MM-DD] [--api]
  python legal.py search <키워드> [--law 법령명] [--limit N]
  python legal.py delegated <법령명> <조문> [--api]     위임 조문(시행령·시행규칙) 연계
  python legal.py history <법령명> [-n N]
  python legal.py precedent <사건번호|키워드> [--full]
  python legal.py constitutional <사건번호|키워드>      헌재 결정례(DRF)
  python legal.py interpretation <안건번호|키워드> [--target expc|moelCgmExpc|…]
  python legal.py decision <위원회코드> <키워드>        ppc ftc nlrc fsc acr kcc iaciac eiac oclt ecc sfc nhrck
  python legal.py admrule <키워드> | ordinance <키워드> | thdcmp <법령명>
  python legal.py research --keywords K1 K2 … [--laws L1 …] [--json]
  python legal.py verify <파일|텍스트|-> [--as-of D] [--evidence F …] [--json] [--out F] [--no-api] [--cross-check]
  python legal.py targets | api-probe

종료 코드(verify): 0=PASS, 1=FAIL, 2=INCOMPLETE/NO_CITATIONS, 3=사용 오류
종료 코드(article): 0=기준일 시행 중, 1=없음·삭제·시행 전, 2=판정 불가(이력 부족), 3=사용 오류
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_common import configure_stdout, iso, parse_date, today  # noqa: E402
from legal_engine import StatuteMirror, parse_article_no  # noqa: E402
from precedent_engine import PrecedentMirror, is_constitutional, normalize_case_no  # noqa: E402
from law_api import COMMITTEES, TARGETS, ApiError, DrfClient, clean_text, probe  # noqa: E402
from citations import Verifier, dumps, render_markdown  # noqa: E402


def _out(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    elif isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


class UsageError(Exception):
    """사용 오류 → 종료 코드 3"""


def _as_of(v: Optional[str]):
    if not v:
        return today()
    d = parse_date(v)
    if not d:
        raise UsageError(f"--as-of 형식 오류: {v} (YYYY-MM-DD)")
    return d


def _api(args) -> DrfClient:
    return DrfClient(offline=True if getattr(args, "no_api", False) else None)


# ---------------------------------------------------------------------------


def cmd_status(args) -> int:
    sm, pm = StatuteMirror(), PrecedentMirror()
    api = _api(args)
    st = {"legalize-kr": sm.status(), "precedent-kr": pm.status(), "drf_api": api.ping(), "today": iso(today())}
    if args.json:
        _out(st, True)
        return 0
    lk, pk, ap = st["legalize-kr"], st["precedent-kr"], st["drf_api"]
    print("legal-ultra 출처 상태")
    if lk["available"]:
        mode = lk.get("history_mode")
        hist = {"full": "전체(기준일 정밀 판정)" + ("" if lk.get("commit_graph") else " — 경로 색인 없음: 첫 검증 때 1회 생성"),
                "partial": "일부(얕은 클론)", "none": "없음"}.get(mode, str(mode))
        print("- legalize-kr : ✅ %s (법령 %s개, 미러 %s, 판본 이력 %s)" % (lk["root"], lk["law_dirs"], lk.get("head_date"), hist))
        if mode != "full":
            print("  ⚠ " + lk.get("history_note", ""))
    else:
        print("- legalize-kr : ❌ " + lk["hint"])
    if pk["available"]:
        print("- precedent-kr: ✅ %s (사건번호 %s개 색인, 최신 선고 %s)" % (pk["root"], pk["case_numbers_indexed"], pk.get("latest_decision")))
    else:
        print("- precedent-kr: ❌ " + pk["hint"])
    if ap["available"]:
        print("- DRF API     : ✅ 사용 가능 (%s)" % ap.get("oc", ""))
    else:
        print("- DRF API     : ❌ " + str(ap.get("reason", "")))
    if not lk["available"] and not ap["available"]:
        print("⚠ 법령 출처가 하나도 없습니다 — 인용 검증은 INCOMPLETE 가 됩니다. `legal.py setup` 을 실행하세요.")
    return 0


def cmd_setup(args) -> int:
    dest = Path(args.dest or (Path.home())).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    plans = []
    lk = dest / "legalize-kr"
    if not lk.exists():
        if args.shallow:   # 이력 없음: 빠르지만 기준일 판정이 개정표시 기준 추정(시행 전 개정이 걸린 조항은 검증불가)
            plans.append(["git", "clone", "--depth", "1", "https://github.com/legalize-kr/legalize-kr.git", str(lk)])
        else:
            # 전체 이력 + 지연 blob: 커밋 10만여 개 이력 196MB(실측 25초) + 현행 본문 체크아웃(31초, 합계 약 540MB).
            # 과거 판본 본문은 기준일 판정에 필요할 때만 내려받는다. 경로별 이력 조회용 commit-graph(Bloom 필터)도 만든다.
            plans.append(["git", "clone", "--filter=blob:none", "--no-checkout",
                          "https://github.com/legalize-kr/legalize-kr.git", str(lk)])
            plans.append(["git", "-C", str(lk), "checkout"])
            plans.append(["git", "-C", str(lk), "commit-graph", "write", "--reachable", "--changed-paths"])
    pk = dest / "precedent-kr"
    if not args.no_precedents and not pk.exists():
        if args.precedent_bodies:
            plans.append(["git", "clone", "--depth", "1", "https://github.com/legalize-kr/precedent-kr.git", str(pk)])
        else:  # 색인(파일명)만: 사건번호·법원·선고일 검증은 완전 오프라인, 본문은 필요 시 지연 로드
            plans.append(["git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout",
                          "https://github.com/legalize-kr/precedent-kr.git", str(pk)])
    for cmd in plans:
        print("$ " + " ".join(cmd))
        if args.dry_run:
            continue
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print("❌ 실패 — 네트워크/권한을 확인하세요.")
            return 1
    print("\n다음 환경변수를 설정하세요(셸 프로필 등):")
    print(f"  LEGALIZE_KR_PATH={lk}")
    if not args.no_precedents:
        print(f"  PRECEDENT_KR_PATH={pk}")
    print("  LAW_OPENAPI_OC=<open.law.go.kr 에서 신청한 OC>   # 미설정 시 공용 시험계정 'test' 사용")
    return 0


def cmd_law(args) -> int:
    sm = StatuteMirror()
    r = sm.resolve(args.name)
    out = {"query": args.name, "status": r.status, "candidates": r.candidates, "note": r.note}
    if r.doc:
        out["doc"] = r.doc.summary()
        arts = sm.articles(r.doc)
        out["articles"] = len(arts)
        out["deleted"] = sum(a.deleted for a in arts)
        eff = r.doc.effective
        out["as_of_note"] = (f"⚠ 시행예정 판본(시행 {iso(eff)}) — 현행 조문 확인 시 article --as-of 사용"
                             if eff and eff > today() else "시행 중 판본")
    elif not sm.available:
        try:
            out["drf"] = _api(args).find_law(args.name, today())
        except ApiError as e:
            out["drf_error"] = str(e)
    _out(out, True)
    return 0 if r.status == "ok" or out.get("drf", {}).get("status") == "ok" else 1


_STATE_LABEL = {"in_force": "기준일 시행 중", "absent": "기준일에 없음(미신설·시행 전)", "deleted": "삭제됨",
                "unknown": "판정 불가(이력 부족)"}


def cmd_article(args) -> int:
    sm = StatuteMirror()
    as_of = _as_of(args.as_of)
    jo, sub = parse_article_no(args.number)
    ho = str(args.ho).replace("호", "") if args.ho else None
    res: dict = {"query": f"{args.law} {args.number}", "as_of": iso(as_of)}
    if sm.available:
        r = sm.resolve(args.law)
        if r.status != "ok":
            res.update(status="not_found", candidates=r.candidates, note=r.note)
        else:
            a = sm.find_article(r.doc, jo, sub)
            st = sm.unit_status(r.doc, jo, sub, args.hang, ho, args.mok, as_of)
            head_unit = a.unit_text(args.hang, ho, args.mok) if a is not None else None
            res.update(status=st.state, doc=r.doc.summary(), article=f"제{jo}조" + (f"의{sub}" if sub else ""),
                       title=st.title or (a.title if a else None), precise=st.precise, version=st.version,
                       notes=st.notes, pending_change=st.pending_change,
                       text=st.text if st.state in ("in_force", "deleted") else None,
                       head_text=head_unit if (st.state != "in_force" or st.pending_change) else None)
    if args.api or not sm.available:
        try:
            api = _api(args)
            fl = api.find_law(args.law, as_of)
            if fl.get("status") == "ok":
                body = api.law_articles(fl["mst"], fl.get("efYd"))
                hit = [x for x in body["articles"] if x["jo"] == jo and x["sub"] == sub]
                res["drf"] = {"law": fl["name"], "mst": fl["mst"], "efYd": fl.get("efYd"),
                              "found": bool(hit), "title": hit[0]["title"] if hit else None,
                              "조문시행일자": hit[0].get("시행일자") if hit else None,
                              "text": hit[0]["text"] if hit and not sm.available else None}
            else:
                res["drf"] = fl
        except ApiError as e:
            res["drf_error"] = str(e)
    if args.json:
        _out(res, True)
    elif res.get("doc"):
        d = res["doc"]
        unit = res["article"] + (f"제{args.hang}항" if args.hang else "") + (f"제{ho}호" if ho else "") + (f"{args.mok}목" if args.mok else "")
        print(f"[{d['title']} {unit} ({res.get('title') or '제목 없음'})]  {d['file']}")
        print(f"최신 공포 {d['공포일자']} · 시행 {d['시행일자']} · 기준일 {res['as_of']} → {_STATE_LABEL.get(res['status'], res['status'])}"
              + (" (판본 이력 대조)" if res.get("precise") else " (개정표시 기준 추정)"))
        for n in res.get("notes") or []:
            print(f"⚠ {n}")
        if res.get("text"):
            print("-" * 60)
            print(res["text"])
        if res.get("head_text"):
            print("-" * 60 + "\n[최신 공포본 문언 — 기준일에 아직 시행 전일 수 있음]")
            print(res["head_text"])
    else:
        _out(res, True)
    if res.get("status") == "in_force" or (not sm.available and res.get("drf", {}).get("found")):
        return 0
    return 2 if res.get("status") == "unknown" else 1


def cmd_search(args) -> int:
    sm = StatuteMirror()
    hits = sm.search(args.keyword, args.law, args.limit)
    if args.json:
        _out(hits, True)
    else:
        print(f"'{args.keyword}' — {len(hits)}건 (legalize-kr, 고정문자열)")
        for h in hits:
            loc = h["article"] + (f"({h['article_title']})" if h["article_title"] else "") if h["article"] else f"L{h['line']}"
            print(f"- {h['title']} [{h['kind']}] {loc}: {h['text'][:160]}")
    return 0


def cmd_delegated(args) -> int:
    sm = StatuteMirror()
    jo, sub = parse_article_no(args.number)
    out = sm.delegated(args.law, jo, sub) if sm.available else {"error": "legalize-kr 미러 없음"}
    if args.api:
        try:
            api = _api(args)
            fl = api.find_law(args.law, today())
            if fl.get("status") == "ok":
                tt = api.three_tier(fl["mst"], "2")
                arts = (tt.get("위임조문삼단비교") or {}).get("법률조문") or []
                arts = arts if isinstance(arts, list) else [arts]
                key = str(jo)
                out["drf_three_tier"] = [a for a in arts if str(a.get("조번호", "")).lstrip("0") == key][:3]
        except ApiError as e:
            out["drf_error"] = str(e)
    _out(out, True)
    return 0 if "error" not in out else 1


def cmd_history(args) -> int:
    _out(StatuteMirror().history(args.law, args.n), True)
    return 0


def cmd_precedent(args) -> int:
    q = args.query.strip()
    no = normalize_case_no(q)
    pm = PrecedentMirror()
    out: dict = {"query": q}
    if no and is_constitutional(no):
        return cmd_constitutional(args)
    if no:
        entries = pm.lookup(no) if pm.available else []
        out["case_no"] = no
        out["local"] = [e.to_dict() for e in entries]
        if entries:
            doc = pm.read(sorted(entries, key=lambda e: not e.primary)[0])
            if doc:
                out["meta"] = doc["meta"]
                secs = doc["sections"]
                keep = ("판시사항", "판결요지", "참조조문", "참조판례") + (("판례내용",) if args.full else ())
                out["sections"] = {k: secs.get(k, "") for k in keep if secs.get(k)}
            else:
                out["note"] = "본문 미확보(부분 클론 오프라인) — 파일명 색인으로 실재·법원·선고일만 확인됨"
        if not entries or args.api:
            try:
                out["drf"] = _api(args).precedent_by_number(no)
            except ApiError as e:
                out["drf_error"] = str(e)
    else:
        out["local_grep"] = pm.search_titles(q, 20) if pm.available else []
        try:
            out["drf"] = _api(args).search("prec", q, display=20)["items"]
        except ApiError as e:
            out["drf_error"] = str(e)
    _out(out, True)
    return 0


def cmd_constitutional(args) -> int:
    api = _api(args)
    q = args.query.strip()
    try:
        no = normalize_case_no(q)
        if no:
            hits = api.constitutional_by_number(no)
            out = {"case_no": no, "hits": hits}
            if hits and hits[0].get("헌재결정례일련번호"):
                body = api.constitutional_body(hits[0]["헌재결정례일련번호"]) or {}
                out["body"] = {k: body.get(k, "")[:4000] for k in ("판시사항", "결정요지", "심판대상조문", "참조조문") if body.get(k)}
        else:
            out = api.search("detc", q, display=20)
    except ApiError as e:
        out = {"error": str(e), "note": "헌재 결정은 precedent-kr 에 없어 DRF API 가 필요합니다"}
    _out(out, True)
    return 0 if "error" not in out else 1


def cmd_interpretation(args) -> int:
    api = _api(args)
    q = args.query.strip()
    try:
        import re as _re
        if _re.fullmatch(r"\d{2}-\d{3,4}", q):
            hits = api.interpretation_by_number(q, args.target)
            out = {"agenda_no": q, "hits": hits}
            if hits:
                idf = TARGETS[args.target].id_field
                out["body"] = api.interpretation_body(hits[0].get(idf) or hits[0].get("_id", ""), args.target)
        else:
            out = api.search(args.target, q, display=20)
    except ApiError as e:
        out = {"error": str(e)}
    _out(out, True)
    return 0 if "error" not in out else 1


def cmd_generic_search(target: str, query: str, args) -> int:
    try:
        out = _api(args).search(target, query, display=args.limit)
    except ApiError as e:
        out = {"error": str(e)}
    _out(out, True)
    return 0 if "error" not in out else 1


def cmd_thdcmp(args) -> int:
    api = _api(args)
    try:
        fl = api.find_law(args.law, today())
        out = {"law": fl}
        if fl.get("status") == "ok":
            out["three_tier"] = api.three_tier(fl["mst"], args.knd)
    except ApiError as e:
        out = {"error": str(e)}
    _out(out, True)
    return 0 if "error" not in out else 1


def cmd_research(args) -> int:
    """후보 출처 수집(분석 없음). 서브에이전트가 읽을 증거 후보 패킷."""
    sm, pm, api = StatuteMirror(), PrecedentMirror(), _api(args)
    packet: dict = {"keywords": args.keywords, "laws": args.laws or [], "statute_hits": [], "precedent_hits": [],
                    "interpretation_hits": [], "errors": []}
    for kw in args.keywords:
        for law in (args.laws or [None]):
            for h in sm.search(kw, law, args.limit):
                packet["statute_hits"].append({"keyword": kw, **h})
        if pm.available:
            for h in pm.search_titles(kw, args.limit):
                packet["precedent_hits"].append({"keyword": kw, "source": "precedent-kr", **h})
        try:
            for it in api.search("prec", kw, display=args.limit, search_scope=2)["items"]:
                packet["precedent_hits"].append({"keyword": kw, "source": "DRF", "사건번호": it.get("사건번호"),
                                                 "사건명": it.get("사건명"), "선고일자": it.get("선고일자"),
                                                 "법원명": it.get("법원명"), "id": it.get("판례일련번호")})
            for it in api.search("expc", kw, display=min(args.limit, 10))["items"]:
                packet["interpretation_hits"].append({"keyword": kw, "안건번호": it.get("안건번호"), "안건명": it.get("안건명"),
                                                      "회신일자": it.get("회신일자"), "id": it.get("법령해석례일련번호")})
        except ApiError as e:
            if str(e) not in packet["errors"]:
                packet["errors"].append(str(e))
    packet["note"] = "후보일 뿐이다 — 인용하려면 article/precedent 로 원문을 열어 확인하고 verify 를 통과시킬 것"
    if args.json:
        _out(packet, True)
    else:
        print(f"법령 적중 {len(packet['statute_hits'])} · 판례 후보 {len(packet['precedent_hits'])} · 해석례 후보 {len(packet['interpretation_hits'])}")
        for h in packet["statute_hits"][:40]:
            print(f"- [{h['keyword']}] {h['title']} {h['article'] or 'L' + str(h['line'])}: {h['text'][:120]}")
        for h in packet["precedent_hits"][:20]:
            print(f"- [{h['keyword']}] 판례 {h.get('court', h.get('법원명', ''))} {h.get('date', h.get('선고일자', ''))} {h.get('case', h.get('사건번호', ''))} {h.get('사건명', '')}")
        for h in packet["interpretation_hits"][:10]:
            print(f"- [{h['keyword']}] 해석 {h['안건번호']} {h['안건명']}")
        for e in packet["errors"]:
            print(f"⚠ {e}")
    return 0


def build_verifier(args) -> Verifier:
    return Verifier(StatuteMirror(), PrecedentMirror(), None if args.no_api else DrfClient(),
                    as_of=_as_of(args.as_of), cross_check=args.cross_check)


_PATHLIKE_RE = re.compile(r"\.(?:md|markdown|txt|text|json|html?|docx?|hwpx?|pdf|rtf|csv)$", re.I)


def _decode_input(data: bytes, where: str) -> str:
    """UTF-8(BOM 허용) → UTF-16(BOM) → CP949 순. 어느 것도 아니면 오류 — 깨진 글자로 검증하지 않는다."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            raise UsageError(f"{where}: UTF-16 표시(BOM)가 있으나 내용이 깨짐 — UTF-8 로 저장해 다시 실행")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode("cp949")
    except UnicodeDecodeError:
        raise UsageError(f"{where}: 문자 인코딩을 알 수 없음(UTF-8·UTF-16·CP949 아님) — UTF-8 로 저장해 다시 실행")
    print(f"[legal-ultra] {where}: UTF-8 이 아니어서 CP949(EUC-KR)로 읽음", file=sys.stderr)
    return text


def read_input(target: str) -> str:
    """'-' = 표준입력, 있는 파일 경로면 그 내용, 아니면 인자 자체를 텍스트로 본다.
    파일처럼 보이는데 없거나(오타) 인코딩을 알 수 없으면 오류(종료 코드 3) — 파일 이름을 '본문'으로 검증해
    '인용 없음'으로 끝내던 fail-open 을 막는다. (긴 한국어 텍스트를 경로로 검사하다 'File name too long' 으로 죽던 문제도 처리)"""
    if target == "-":
        data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
        return _decode_input(data, "표준입력")
    if len(target) < 1024 and "\n" not in target:
        p = Path(target).expanduser()
        try:
            is_file = p.is_file()
        except (OSError, ValueError):
            is_file = False
        if is_file:
            try:
                data = p.read_bytes()
            except OSError as e:
                raise UsageError(f"파일을 읽을 수 없음: {target} ({e})")
            return _decode_input(data, target)
        t = target.strip()
        if not re.search(r"\s", t) and (_PATHLIKE_RE.search(t) or t.startswith(("./", "../", "~/", "/", ".\\"))):
            raise UsageError(f"파일이 없음: {target} — 텍스트를 직접 검사하려면 '-'(표준입력)로 넘기세요")
    return target


def cmd_verify(args) -> int:
    text = read_input(args.target)
    ev = None
    if args.evidence:
        ev = "\n".join(read_input(f) for f in args.evidence)
    rep = build_verifier(args).verify_text(text, evidence_text=ev)
    md = render_markdown(rep)
    if args.out:
        Path(args.out).write_text(md + "\n", encoding="utf-8")
    _out(dumps(rep) if args.json else md, False)
    return {"PASS": 0, "FAIL": 1}.get(rep["verdict"], 2)


def cmd_targets(args) -> int:
    rows = [{"target": t.code, "label": t.label, "list_root": t.list_root, "record": t.record_tag,
             "id_param": t.id_param, "body": t.has_body} for t in TARGETS.values()]
    _out(rows, True)
    return 0


def cmd_probe(args) -> int:
    rows = probe(_api(args))
    _out(rows, True)
    return 0 if all(r.get("ok") for r in rows) else 1


# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(prog="legal.py", description="legal-ultra v2 조사·검증 CLI")
    ap.add_argument("--no-api", action="store_true", help="법제처 DRF API 사용 안 함(오프라인)")
    sp = ap.add_subparsers(dest="cmd")

    def add(name, fn, **kw):
        p = sp.add_parser(name, **kw)
        p.set_defaults(fn=fn)
        p.add_argument("--json", action="store_true")
        return p

    add("status", cmd_status, help="출처 상태")
    p = add("setup", cmd_setup, help="legalize-kr / precedent-kr 미러 설치")
    p.add_argument("--dest")
    p.add_argument("--shallow", action="store_true", help="legalize-kr 이력 없이 최신본만(빠름, 기준일 판정은 추정·검증불가 증가)")
    p.add_argument("--full-history", action="store_true", help=argparse.SUPPRESS)   # v2.0 호환: 이제 기본값
    p.add_argument("--precedent-bodies", action="store_true", help="precedent-kr 본문까지 전부 받기(수 GB)")
    p.add_argument("--no-precedents", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p = add("law", cmd_law, help="법령명 해석")
    p.add_argument("name")
    p = add("article", cmd_article, help="조문 조회(+시행일 판정)")
    p.add_argument("law")
    p.add_argument("number")
    p.add_argument("--hang", type=int)
    p.add_argument("--ho", help="호 번호(예: 3, 1의2)")
    p.add_argument("--mok", help="목(예: 가)")
    p.add_argument("--as-of")
    p.add_argument("--api", action="store_true", help="DRF 로 교차 확인")
    p = add("search", cmd_search, help="법령 본문 검색(legalize-kr)")
    p.add_argument("keyword")
    p.add_argument("--law")
    p.add_argument("--limit", type=int, default=20)
    p = add("delegated", cmd_delegated, help="위임 조문 연계")
    p.add_argument("law")
    p.add_argument("number")
    p.add_argument("--api", action="store_true")
    p = add("history", cmd_history, help="개정 이력")
    p.add_argument("law")
    p.add_argument("-n", type=int, default=10)
    p = add("precedent", cmd_precedent, help="판례(사건번호/키워드)")
    p.add_argument("query")
    p.add_argument("--full", action="store_true")
    p.add_argument("--api", action="store_true")
    p = add("constitutional", cmd_constitutional, help="헌재 결정례(DRF)")
    p.add_argument("query")
    p = add("interpretation", cmd_interpretation, help="법령해석례(DRF)")
    p.add_argument("query")
    p.add_argument("--target", default="expc", choices=["expc"] + sorted(t for t in TARGETS if t.endswith("CgmExpc")))
    p = add("decision", lambda a: cmd_generic_search(a.committee, a.query, a), help="위원회 결정문(DRF)")
    p.add_argument("committee", choices=COMMITTEES)
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p = add("admrule", lambda a: cmd_generic_search("admrul", a.query, a), help="행정규칙(DRF)")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p = add("ordinance", lambda a: cmd_generic_search("ordin", a.query, a), help="자치법규(DRF)")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p = add("thdcmp", cmd_thdcmp, help="3단비교(DRF)")
    p.add_argument("law")
    p.add_argument("--knd", default="2", choices=["1", "2"])
    p = add("research", cmd_research, help="후보 출처 수집(분석 없음)")
    p.add_argument("--keywords", nargs="+", required=True)
    p.add_argument("--laws", nargs="*")
    p.add_argument("--limit", type=int, default=10)
    p = add("verify", cmd_verify, help="인용 검증(Zero-Hallucination Gate)")
    p.add_argument("target", help="파일 경로, '-'(표준입력) 또는 텍스트")
    p.add_argument("--as-of")
    p.add_argument("--evidence", nargs="*")
    p.add_argument("--out")
    p.add_argument("--cross-check", action="store_true", help="로컬 확인 결과를 DRF 로 교차 확인")
    add("targets", cmd_targets, help="DRF target 레지스트리")
    add("api-probe", cmd_probe, help="DRF 레지스트리 실측 확인(사용자 OC 필요)")

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 3
    for k in ("no_api", "cross_check", "as_of", "api"):
        if not hasattr(args, k):
            setattr(args, k, False if k != "as_of" else None)
    try:
        return args.fn(args)
    except (UsageError, ValueError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
