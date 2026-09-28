"""legal_swarm.py - legal-ultra v2 작업판(Blackboard): 사안별 DAG · 역할 · 게이트 · 리비전 예산을 코드로 강제한다.

  python legal_swarm.py init --title T --facts F --issues I1 I2 [--case-id ID] [--domain D] [--as-of D] [--mode full]
                             [--single-agent]
  python legal_swarm.py claim --case-id ID --role ROLE --worker NAME        → 작업 패킷(JSON, token 포함)
  python legal_swarm.py submit --case-id ID --task-id T --token TOK [--accept-incomplete]
  python legal_swarm.py audit --case-id ID --token TOK --verdict approve|revise [--issues …] [--accept-incomplete]
  python legal_swarm.py finalize --case-id ID --token TOK
  python legal_swarm.py board [--case-id ID] | cases | log --case-id ID | release --case-id ID --task-id T

작업공간: --workspace 또는 $LEGAL_ULTRA_WORKSPACE (기본 ./legal_workspace). 모든 산출물 경로는 작업공간 기준이라
실행 위치(CWD)와 무관하다. v1 대비 수정(재현 근거 docs/REVIEW-2026-09-28.md):
  - revise 후 T3/T4 교착(needs_revision 을 아무도 claim 못함) → 리비전 루프 정상화, 예산 초과 시 escalated
  - revise 판정이어도 최종본(T5)이 열리던 게이트 우회 → 승인은 검증기 PASS 일 때만, 최종본은 승인된 초안 해시에만
  - 사안 간 태스크 ID 충돌(두 번째 init 이 첫 사안을 덮어씀) → (case_id, task_id) 복합키
  - '읽기 전용 감사관'인데 감사 파일을 직접 써야 하던 모순 → audit 명령이 시스템 기록을 생성
v2.1(2차 적대적 검토 반영):
  - 감사관은 T1~T4 작업자와 달라야 한다(직무 분리). 단일 에이전트 런타임은 init --single-agent 로 명시하고 최종본에 공개
  - 검증 보고서는 감사 시점에 DB 에 저장하고 최종본은 그것만 붙인다(승인 후 verification_report.md 를 바꿔치기하던 위조 차단)
  - 증거 항목은 citation 하나 + quote 를 명시적으로 짝지어 검증(문장 조립 방식에서 인용문이 누락·잘리던 문제)
  - 제출·감사·발행 커밋 시 토큰을 다시 확인(게이트 실행 중 재배정된 작업을 옛 작업자가 완료 처리하던 경쟁 조건)
  - verify 모드는 형식(FIRAC) 검사 없이 인용만 검증, UTF-8 BOM 허용, 잘못된 JSON 구조·누락 파일은 트레이스백 대신 안내
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_common import configure_stdout, iso, normalize_for_match, parse_date, today  # noqa: E402

ROLES = ("lead_counsel", "statute_analyst", "precedent_analyst", "risk_advocate", "counsel_builder", "legal_auditor")
AUTHOR_TASKS = ("T1_STATUTE", "T2_PRECEDENT", "T3_RISK", "T4_DRAFT")

DEFAULT_TASKS: List[Dict[str, object]] = [
    {"id": "T1_STATUTE", "role": "statute_analyst", "kind": "research", "deps": [], "outputs": ["evidence_statute.json"],
     "gate": "statute_evidence", "title": "실정법·위임법령 조사(시행일 기준 현행 조문 확정)",
     "acceptance": ["모든 provisions[].citation 이 검증기 VERIFIED(인용 하나씩)", "quote 는 원문 그대로(해당 항·호 문언)",
                    "시행예정·삭제·폐지 여부를 in_force_note 에 기록"]},
    {"id": "T2_PRECEDENT", "role": "precedent_analyst", "kind": "research", "deps": [], "outputs": ["evidence_precedent.json"],
     "gate": "precedent_evidence", "title": "판례·헌재결정·유권해석 조사(요건사실·입증책임·시효)",
     "acceptance": ["모든 precedents[].citation 이 법원·선고일·사건번호까지 VERIFIED",
                    "holding_quote 는 판시사항/판결요지 원문 발췌(10자 이상)", "적합한 선례가 없으면 no_precedent_reason 으로 명시"]},
    {"id": "T3_RISK", "role": "risk_advocate", "kind": "review", "deps": ["T1_STATUTE", "T2_PRECEDENT"], "outputs": ["risk_memo.json"],
     "gate": "risk_memo", "title": "반대논리·제재·입증책임 리스크 감사(Devil's Advocate)",
     "acceptance": ["risks[] 각 항목에 severity(high|medium|low)·근거·반대논리·완화책", "basis 의 벌칙·과태료 조항 인용은 VERIFIED"]},
    {"id": "T4_DRAFT", "role": "counsel_builder", "kind": "build", "deps": ["T1_STATUTE", "T2_PRECEDENT", "T3_RISK"],
     "outputs": ["draft_opinion.md"], "gate": "draft", "title": "결론 우선 FIRAC 의견서 초안",
     "acceptance": ["결론·즉시 행동이 맨 위", "FIRAC 5단", "모든 인용 VERIFIED", "검증 판정·승소확률(%)을 스스로 쓰지 않음"]},
    {"id": "T5_AUDIT", "role": "legal_auditor", "kind": "audit", "deps": ["T4_DRAFT"], "outputs": [], "gate": "audit",
     "title": "독립 감사(인용 재검증 + 논리·입증책임·반대논리 반영 심사)",
     "acceptance": ["legal_swarm.py audit 로만 판정 기록(초안 수정 금지)", "approve 는 검증기 PASS 일 때만 가능",
                    "T1~T4 를 맡은 작업자는 감사할 수 없음"]},
    {"id": "T6_FINAL", "role": "lead_counsel", "kind": "deliver", "deps": ["T5_AUDIT"], "outputs": [], "gate": "final",
     "title": "최종본 발행(시스템이 검증·감사 부록을 붙임)", "acceptance": ["legal_swarm.py finalize 로만 발행"]},
]

MODES = {
    "full": ["T1_STATUTE", "T2_PRECEDENT", "T3_RISK", "T4_DRAFT", "T5_AUDIT", "T6_FINAL"],
    "contract": ["T1_STATUTE", "T2_PRECEDENT", "T3_RISK", "T4_DRAFT", "T5_AUDIT", "T6_FINAL"],
    "administrative": ["T1_STATUTE", "T2_PRECEDENT", "T3_RISK", "T4_DRAFT", "T5_AUDIT", "T6_FINAL"],
    "statute": ["T1_STATUTE", "T3_RISK", "T4_DRAFT", "T5_AUDIT", "T6_FINAL"],
    "precedent": ["T2_PRECEDENT", "T4_DRAFT", "T5_AUDIT", "T6_FINAL"],
    "verify": ["T5_AUDIT", "T6_FINAL"],
}

# 초안이 스스로 쓰면 안 되는 판정·시스템 문구(가짜 검증 보고서·감사 기록 끼워 넣기 차단). '검증'은 법률 용어(현장 검증)이기도
# 해서 '인용/출처 검증', '검증 판정' 같은 조합만 잡는다.
_SELF_VERDICT_RE = re.compile(
    r"\bPASS(?:ED)?\b|\bAPPROVED?\b|무결점|citation\s*verifier|(?:인용|출처)\s*검증\s*(?:보고서|판정|결과|완료|통과)|"
    r"검증기?\s*판정|감사\s*(?:판정|승인)|독립\s*감사\s*[:：]|시스템\s*생성|수정\s*금지|변호사\s*검토\s*완료|🛡", re.I)
_WIN_PCT_RE = re.compile(r"(?:승소|인용|구제|승인)\s*(?:가능성|확률|률)[^\n%]{0,20}?\d{1,3}(?:\.\d+)?\s*%")
_FIRAC_KEYS = [("결론",), ("사실",), ("쟁점",), ("법령", "법리", "Rules", "규정"), ("포섭", "적용", "Application", "검토"),
               ("대응", "전략", "권고", "행동")]
MIN_HOLDING_QUOTE = 10


class GateError(Exception):
    pass


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


class Board:
    def __init__(self, workspace: Optional[str] = None):
        self.ws = Path(workspace or os.environ.get("LEGAL_ULTRA_WORKSPACE") or "legal_workspace").expanduser().resolve()
        self.ws.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.ws / "blackboard.db"), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS cases (
            case_id TEXT PRIMARY KEY, title TEXT NOT NULL, facts TEXT NOT NULL, issues TEXT NOT NULL,
            domain TEXT, as_of TEXT NOT NULL, mode TEXT NOT NULL, budget INTEGER NOT NULL DEFAULT 2,
            status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
            case_id TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT, role TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', deps TEXT NOT NULL, outputs TEXT NOT NULL, gate TEXT NOT NULL,
            acceptance TEXT, worker TEXT, token TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
            claimed_at TEXT, completed_at TEXT, PRIMARY KEY (case_id, task_id));
        CREATE TABLE IF NOT EXISTS audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, auditor TEXT, verdict TEXT NOT NULL,
            verifier_verdict TEXT, draft_sha TEXT, issues TEXT, accepted_incomplete INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, ts TEXT, actor TEXT, action TEXT, detail TEXT);
        """)
        # v2.0 작업판 DB 이관: 없는 열을 추가한다
        for table, col, decl in (("cases", "single_agent", "INTEGER NOT NULL DEFAULT 0"), ("audits", "report_md", "TEXT")):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # ---- 공통 ----
    def case_dir(self, case_id: str) -> Path:
        return self.ws / "cases" / case_id

    def event(self, case_id: str, actor: str, action: str, detail: object = "") -> None:
        self.conn.execute("INSERT INTO events (case_id, ts, actor, action, detail) VALUES (?,?,?,?,?)",
                          (case_id, _now(), actor, action, json.dumps(detail, ensure_ascii=False, default=str)))

    def case(self, case_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise GateError(f"사안 없음: {case_id}")
        return row

    def task(self, case_id: str, task_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM tasks WHERE case_id=? AND task_id=?", (case_id, task_id)).fetchone()
        if not row:
            raise GateError(f"태스크 없음: {case_id}/{task_id}")
        return row

    def authors(self, case_id: str) -> Dict[str, List[str]]:
        """작업자 → 이 사안에서 claim 한 조사·집필 태스크(T1~T4). release 된 claim 도 포함(한 번이라도 맡았으면 저자)."""
        out: Dict[str, List[str]] = {}
        for r in self.conn.execute("SELECT actor, detail FROM events WHERE case_id=? AND action='claim'", (case_id,)):
            try:
                tid = json.loads(r["detail"])
            except ValueError:
                continue
            if tid in AUTHOR_TASKS:
                out.setdefault(r["actor"], []).append(tid)
        return out

    # ---- init ----
    def init_case(self, title: str, facts: str, issues: List[str], domain: str = "", as_of: Optional[str] = None,
                  mode: str = "full", budget: int = 2, case_id: Optional[str] = None,
                  plan: Optional[List[Dict[str, object]]] = None, draft: Optional[str] = None,
                  single_agent: bool = False) -> str:
        if not title.strip() or not facts.strip() or not [i for i in issues if i.strip()]:
            raise GateError("Gate 0(접수): 제목·사실관계·쟁점(1개 이상)이 모두 필요")
        if mode not in MODES:
            raise GateError(f"알 수 없는 mode: {mode} ({', '.join(MODES)})")
        d = parse_date(as_of) if as_of else today()
        if d is None:
            raise GateError(f"as_of 형식 오류: {as_of}")
        case_id = case_id or "CASE-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        if not re.fullmatch(r"[A-Za-z0-9_\-]{3,64}", case_id):
            raise GateError("case_id 는 영문·숫자·-·_ 3~64자")
        if self.conn.execute("SELECT 1 FROM cases WHERE case_id=?", (case_id,)).fetchone():
            raise GateError(f"이미 있는 사안: {case_id} (덮어쓰지 않음)")
        if plan is not None:
            if not isinstance(plan, list) or not all(isinstance(t, dict) and t.get("id") and t.get("role") in ROLES
                                                     and t.get("kind") for t in plan):
                raise GateError("--plan 형식 오류: tasks[] 각 항목에 id·role(역할 코드)·kind 가 필요")
        tasks = plan or [t for t in DEFAULT_TASKS if t["id"] in MODES[mode]]
        ids = {t["id"] for t in tasks}
        if mode == "verify" and (not draft or not Path(draft).is_file()):
            raise GateError("verify 모드는 --draft <검증할 문서> 가 필요")
        cd = self.case_dir(case_id)
        cd.mkdir(parents=True, exist_ok=True)
        if mode == "verify":
            (cd / "draft_opinion.md").write_text(Path(draft).read_text(encoding="utf-8-sig"), encoding="utf-8")
        (cd / "case.json").write_text(json.dumps({"case_id": case_id, "title": title, "facts": facts, "issues": issues,
                                                  "domain": domain, "as_of": iso(d), "mode": mode,
                                                  "single_agent": bool(single_agent)}, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT INTO cases (case_id,title,facts,issues,domain,as_of,mode,budget,created_at,single_agent) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (case_id, title, facts, json.dumps(issues, ensure_ascii=False), domain, iso(d), mode, budget,
                               _now(), int(bool(single_agent))))
            for t in tasks:
                deps = [x for x in t.get("deps", []) if x in ids]
                self.conn.execute(
                    "INSERT INTO tasks (case_id,task_id,title,role,kind,deps,outputs,gate,acceptance) VALUES (?,?,?,?,?,?,?,?,?)",
                    (case_id, t["id"], t.get("title", ""), t["role"], t["kind"], json.dumps(deps), json.dumps(t.get("outputs", [])),
                     t.get("gate", "none"), json.dumps(t.get("acceptance", []), ensure_ascii=False)))
            self.event(case_id, "system", "init", {"mode": mode, "tasks": sorted(ids), "as_of": iso(d),
                                                   "single_agent": bool(single_agent)})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return case_id

    # ---- claim ----
    def claim(self, case_id: str, role: str, worker: str) -> Optional[Dict[str, object]]:
        if role not in ROLES:
            raise GateError(f"알 수 없는 역할: {role} ({', '.join(ROLES)})")
        if not worker or not worker.strip():
            raise GateError("worker 이름이 필요")
        c = self.case(case_id)
        if c["status"] in ("escalated", "delivered"):
            raise GateError(f"사안 상태 {c['status']} — 더 이상 작업을 배정하지 않음")
        if role == "legal_auditor" and not c["single_agent"]:
            mine = self.authors(case_id).get(worker)
            if mine:
                raise GateError(f"직무 분리: '{worker}'는 이 사안의 {', '.join(sorted(set(mine)))} 작업자라 감사관이 될 수 없음 "
                                "— 새 컨텍스트의 다른 서브에이전트로 감사하거나, 단일 에이전트 런타임이면 init --single-agent 로 명시")
        if role in ("statute_analyst", "precedent_analyst", "risk_advocate", "counsel_builder") and not c["single_agent"]:
            auditors = {r["auditor"] for r in self.conn.execute("SELECT auditor FROM audits WHERE case_id=?", (case_id,))}
            if worker in auditors:
                raise GateError(f"직무 분리: '{worker}'는 이 사안의 감사관이라 조사·집필을 맡을 수 없음")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute("SELECT * FROM tasks WHERE case_id=? AND role=? AND status IN ('pending','needs_revision') ORDER BY task_id",
                                     (case_id, role)).fetchall()
            done = {r["task_id"] for r in self.conn.execute("SELECT task_id FROM tasks WHERE case_id=? AND status='done'", (case_id,))}
            for r in rows:
                if all(d in done for d in json.loads(r["deps"])):
                    token = secrets.token_hex(8)
                    self.conn.execute("UPDATE tasks SET status='running', worker=?, token=?, claimed_at=? WHERE case_id=? AND task_id=?",
                                      (worker, token, _now(), case_id, r["task_id"]))
                    self.event(case_id, worker, "claim", r["task_id"])
                    self.conn.execute("COMMIT")
                    return self._packet(c, r, token, worker)
            self.conn.execute("COMMIT")
            return None
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _packet(self, c: sqlite3.Row, r: sqlite3.Row, token: str, worker: str) -> Dict[str, object]:
        cd = self.case_dir(c["case_id"])
        inputs = [str(cd / "case.json")]
        for dep in json.loads(r["deps"]):
            inputs += [str(cd / o) for o in json.loads(self.task(c["case_id"], dep)["outputs"])]
        if r["task_id"] in ("T5_AUDIT", "T6_FINAL"):
            inputs.append(str(cd / "draft_opinion.md"))
        feedback = None
        if r["status"] == "needs_revision":
            a = self.conn.execute("SELECT * FROM audits WHERE case_id=? ORDER BY id DESC LIMIT 1", (c["case_id"],)).fetchone()
            if a:
                feedback = {"verdict": a["verdict"], "issues": json.loads(a["issues"] or "[]"),
                            "verification_report": str(cd / "verification_report.md")}
        return {"case_id": c["case_id"], "task_id": r["task_id"], "role": r["role"], "title": r["title"], "token": token,
                "worker": worker, "as_of": c["as_of"], "mode": c["mode"], "facts": c["facts"], "issues": json.loads(c["issues"]),
                "inputs": [p for p in inputs if Path(p).exists()], "outputs": [str(cd / o) for o in json.loads(r["outputs"])],
                "acceptance": json.loads(r["acceptance"] or "[]"), "revision_feedback": feedback,
                "retry_count": r["retry_count"], "budget": c["budget"],
                "next": self._next_hint(r["task_id"], c["case_id"], token)}

    @staticmethod
    def _next_hint(task_id: str, case_id: str, token: str) -> str:
        if task_id == "T5_AUDIT":
            return f"python legal_swarm.py audit --case-id {case_id} --token {token} --verdict approve|revise --issues '…'"
        if task_id == "T6_FINAL":
            return f"python legal_swarm.py finalize --case-id {case_id} --token {token}"
        return f"python legal_swarm.py submit --case-id {case_id} --task-id {task_id} --token {token}"

    def _check_token(self, case_id: str, task_id: str, token: str) -> sqlite3.Row:
        t = self.task(case_id, task_id)
        if t["status"] != "running" or not token or t["token"] != token:
            raise GateError(f"{task_id}: 토큰 불일치 또는 실행 중이 아님(status={t['status']})")
        return t

    def _recheck_token(self, case_id: str, task_id: str, token: str) -> None:
        """트랜잭션 안에서 토큰을 다시 확인(검증이 도는 동안 release→재claim 된 경우 옛 작업자의 커밋을 막는다)."""
        r = self.conn.execute("SELECT status, token FROM tasks WHERE case_id=? AND task_id=?", (case_id, task_id)).fetchone()
        if not r or r["status"] != "running" or r["token"] != token:
            raise GateError(f"{task_id}: 처리 중에 태스크가 재배정됨(토큰 변경) — 이 작업자의 결과는 반영하지 않음")

    # ---- submit (Gate 1~4) ----
    def submit(self, case_id: str, task_id: str, token: str, accept_incomplete: bool = False,
               verifier=None) -> Dict[str, object]:
        t = self._check_token(case_id, task_id, token)
        if t["gate"] in ("audit", "final"):
            raise GateError(f"{task_id} 는 submit 이 아니라 {'audit' if t['gate'] == 'audit' else 'finalize'} 명령으로 처리")
        c = self.case(case_id)
        cd = self.case_dir(case_id)
        outs = [cd / o for o in json.loads(t["outputs"])]
        for p in outs:
            if not p.is_file() or p.stat().st_size == 0:
                raise GateError(f"산출물 없음/빈 파일: {p}")
        result = run_gate(t["gate"], outs[0], cd, c["as_of"], verifier, mode=c["mode"])
        ok = result["verdict"] == "PASS" or (result["verdict"] == "INCOMPLETE" and accept_incomplete)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._recheck_token(case_id, task_id, token)     # 게이트 실행 중 재배정됐으면 이 제출은 무효
            self.event(case_id, t["worker"], "gate", {"task": task_id, "gate": t["gate"], "verdict": result["verdict"],
                                                      "accept_incomplete": accept_incomplete, "problems": result["problems"]})
            if ok:
                cur = self.conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE case_id=? AND task_id=? "
                                        "AND token=? AND status='running'", (_now(), case_id, task_id, token))
                if cur.rowcount != 1:
                    raise GateError(f"{task_id}: 제출 중 태스크 상태가 바뀜 — 제출 무효")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        (cd / f"gate_{task_id}.md").write_text(result.get("report_md", ""), encoding="utf-8")
        result["accepted"] = bool(ok)
        return result

    # ---- audit (Gate 5) ----
    def audit(self, case_id: str, token: str, verdict: str, issues: List[str], accept_incomplete: bool = False,
              verifier=None) -> Dict[str, object]:
        if verdict not in ("approve", "revise"):
            raise GateError("verdict 는 approve 또는 revise")
        t = self._check_token(case_id, "T5_AUDIT", token)
        c = self.case(case_id)
        if not c["single_agent"]:
            mine = self.authors(case_id).get(t["worker"])
            if mine:
                raise GateError(f"직무 분리: 감사관 '{t['worker']}'가 이 사안의 {', '.join(sorted(set(mine)))} 작업자임")
        cd = self.case_dir(case_id)
        draft = cd / "draft_opinion.md"
        if not draft.is_file():
            raise GateError("감사할 초안(draft_opinion.md)이 없음")
        sha = _sha(draft)        # 검증한 바로 그 내용의 해시를 고정한다(검증 중 파일이 바뀌어도 승인 대상은 검증본)
        res = run_gate("draft", draft, cd, c["as_of"], verifier, mode=c["mode"])   # 감사관 판단과 무관하게 시스템이 재검증
        if _sha(draft) != sha:
            raise GateError("감사 중 초안이 바뀜 — 다시 감사하세요")
        vv = res["verdict"]
        if verdict == "approve":
            if vv == "FAIL" or res["problems"]:
                raise GateError(f"승인 불가: 검증기 판정 {vv}, 문제 {len(res['problems'])}건 — revise 로 반려하세요")
            if vv == "INCOMPLETE" and not accept_incomplete:
                raise GateError("승인 불가: 검증 INCOMPLETE(출처 부재). 출처를 갖추거나 --accept-incomplete 로 명시 승인")
            if vv == "NO_CITATIONS":
                raise GateError("승인 불가: 인용이 하나도 없는 의견서")
        report_md = res.get("report_md", "")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._recheck_token(case_id, "T5_AUDIT", token)
            self.conn.execute("INSERT INTO audits (case_id,auditor,verdict,verifier_verdict,draft_sha,issues,accepted_incomplete,"
                              "created_at,report_md) VALUES (?,?,?,?,?,?,?,?,?)",
                              (case_id, t["worker"], verdict, vv, sha, json.dumps(issues + res["problems"], ensure_ascii=False),
                               int(accept_incomplete and vv == "INCOMPLETE"), _now(), report_md))
            out: Dict[str, object] = {"case_id": case_id, "verdict": verdict, "verifier_verdict": vv, "draft_sha256": sha}
            if verdict == "approve":
                self.conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE case_id=? AND task_id='T5_AUDIT'", (_now(), case_id))
                self.conn.execute("UPDATE cases SET status='approved' WHERE case_id=?", (case_id,))
            else:
                d = self.conn.execute("SELECT retry_count FROM tasks WHERE case_id=? AND task_id='T4_DRAFT'", (case_id,)).fetchone()
                retries = (d["retry_count"] if d else 0) + 1
                if d is None:  # verify 모드: 고칠 집필 태스크가 없으므로 사람에게 넘긴다
                    retries = c["budget"] + 1
                if retries > c["budget"]:
                    self.conn.execute("UPDATE tasks SET status='blocked' WHERE case_id=? AND status NOT IN ('done')", (case_id,))
                    self.conn.execute("UPDATE cases SET status='escalated' WHERE case_id=?", (case_id,))
                    out["escalated"] = True
                    out["message"] = f"리비전 예산({c['budget']}회) 초과 — 작업판 동결, 변호사(인간) 검토 필요"
                else:
                    self.conn.execute("UPDATE tasks SET status='needs_revision', retry_count=?, token=NULL WHERE case_id=? AND task_id='T4_DRAFT'",
                                      (retries, case_id))
                    self.conn.execute("UPDATE tasks SET status='pending', token=NULL, worker=NULL WHERE case_id=? AND task_id='T5_AUDIT'", (case_id,))
                    out["message"] = f"반려 — T4_DRAFT 재작업 {retries}/{c['budget']}"
            self.event(case_id, t["worker"], "audit", {**out, "issues": issues})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        # 참고용 사본(발행에는 쓰지 않는다 — finalize 는 DB 에 고정된 보고서만 붙인다)
        (cd / "verification_report.md").write_text(report_md, encoding="utf-8")
        audit_json = {**out, "auditor": t["worker"], "issues": issues, "system_problems": res["problems"],
                      "verification": res.get("report"), "created_at": _now(), "generated_by": "legal_swarm.py audit"}
        (cd / "audit_verdict.json").write_text(json.dumps(audit_json, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return out

    # ---- finalize (Gate 6) ----
    def finalize(self, case_id: str, token: str) -> Dict[str, object]:
        t = self._check_token(case_id, "T6_FINAL", token)
        c = self.case(case_id)
        cd = self.case_dir(case_id)
        a = self.conn.execute("SELECT * FROM audits WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
        if not a or a["verdict"] != "approve":
            raise GateError("최종 발행 불가: 최신 감사 판정이 approve 가 아님")
        draft = cd / "draft_opinion.md"
        body = draft.read_bytes() if draft.is_file() else b""
        if hashlib.sha256(body).hexdigest() != a["draft_sha"]:
            raise GateError("최종 발행 불가: 승인 이후 초안이 바뀜(재감사 필요)")
        if not a["report_md"]:
            raise GateError("최종 발행 불가: 감사 기록에 검증 보고서가 없음(구버전 기록) — 다시 감사하세요")
        retries = self.conn.execute("SELECT retry_count FROM tasks WHERE case_id=? AND task_id='T4_DRAFT'", (case_id,)).fetchone()
        report = a["report_md"]        # 파일(verification_report.md)이 아니라 감사 시점에 DB 에 고정한 보고서만 쓴다
        issues = json.loads(a["issues"] or "[]")
        warn = ""
        if a["accepted_incomplete"]:
            warn = ("\n> ⚠️ **검증 미완료(INCOMPLETE) 상태로 승인됨** — 일부 인용을 출처 부재로 확인하지 못했습니다. "
                    "부록 A의 '검증불가' 항목은 원문을 직접 확인하기 전까지 신뢰하지 마십시오.\n")
        appendix = [
            "", "---", "", "## 부록 A. 인용 검증 보고서 (시스템 생성 — 수정 금지)", warn, report, "",
            "## 부록 B. 독립 감사 기록 (시스템 생성)",
            f"- 감사관: `{a['auditor']}` · 판정: **{a['verdict']}** · 검증기 판정: **{a['verifier_verdict']}** · 일시: {a['created_at']}",
            ("- 감사 독립성: ⚠️ 단일 에이전트 실행(init --single-agent) — 작성자와 감사자가 같은 에이전트일 수 있음"
             if c["single_agent"] else "- 감사 독립성: 감사관은 조사·집필(T1~T4) 작업자와 다름(시스템 확인)"),
            f"- 리비전: {retries['retry_count'] if retries else 0}/{c['budget']}회 · 승인 초안 SHA-256: `{a['draft_sha']}`",
        ]
        if issues:
            appendix += ["- 감사 의견:"] + [f"  - {x}" for x in issues]
        appendix += [
            "", "## 부록 C. 기준일·출처·한계",
            f"- 기준일(as-of): {c['as_of']} — 이 날짜에 시행 중인 법령을 기준으로 검토함",
            "- 출처: legalize-kr(법령 Git 미러) · precedent-kr(판례 미러) · 법제처 국가법령정보 공동활용 OPEN API(DRF) 중 부록 A에 표시된 것",
            "- 한계: 사실관계는 의뢰인 진술에 의존하며, 미공개 판결·하급심 동향·실무 관행은 반영되지 않았을 수 있음",
            "- 본 문서는 AI 에이전트 팀이 작성한 법률 정보 분석이며 변호사의 법률자문을 대체하지 않습니다. "
            "중요한 결정 전 변호사 검토를 받으십시오.",
        ]
        final = cd / "final_legal_opinion.md"
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._recheck_token(case_id, "T6_FINAL", token)
            final.write_text(body.decode("utf-8").rstrip() + "\n" + "\n".join(appendix) + "\n", encoding="utf-8")
            self.conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE case_id=? AND task_id='T6_FINAL'", (_now(), case_id))
            self.conn.execute("UPDATE cases SET status='delivered' WHERE case_id=?", (case_id,))
            self.event(case_id, t["worker"], "finalize", str(final))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"case_id": case_id, "final": str(final), "sha256": _sha(final)}

    # ---- 운영 ----
    def release(self, case_id: str, task_id: str, reason: str = "") -> None:
        t = self.task(case_id, task_id)
        if t["status"] != "running":
            raise GateError(f"{task_id} 는 running 이 아님")
        prev = "needs_revision" if t["retry_count"] and task_id == "T4_DRAFT" else "pending"
        self.conn.execute("UPDATE tasks SET status=?, token=NULL, worker=NULL WHERE case_id=? AND task_id=?", (prev, case_id, task_id))
        self.event(case_id, "lead", "release", {"task": task_id, "reason": reason})

    def board(self, case_id: Optional[str] = None) -> Dict[str, object]:
        if not case_id:
            r = self.conn.execute("SELECT case_id FROM cases ORDER BY created_at DESC LIMIT 1").fetchone()
            if not r:
                return {"case": None, "tasks": []}
            case_id = r["case_id"]
        c = dict(self.case(case_id))
        tasks = [dict(r) for r in self.conn.execute("SELECT task_id,role,status,retry_count,worker,deps FROM tasks WHERE case_id=? ORDER BY task_id", (case_id,))]
        for t in tasks:
            t.pop("token", None)
        audits = [dict(r) for r in self.conn.execute("SELECT id,auditor,verdict,verifier_verdict,created_at FROM audits WHERE case_id=? ORDER BY id", (case_id,))]
        return {"case": c, "tasks": tasks, "audits": audits, "dir": str(self.case_dir(case_id))}


# ---------------------------------------------------------------------------
# 게이트
# ---------------------------------------------------------------------------


def _default_verifier(as_of: str):
    from legal_engine import StatuteMirror
    from precedent_engine import PrecedentMirror
    from law_api import DrfClient
    from citations import Verifier
    return Verifier(StatuteMirror(), PrecedentMirror(), DrfClient(), as_of=parse_date(as_of))


def _strings(obj) -> List[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return []


def _load_json(path: Path) -> Tuple[Optional[object], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None   # BOM 허용(PowerShell 5.1 기본 저장 형식)
    except (OSError, UnicodeDecodeError) as e:
        return None, f"파일을 읽을 수 없음: {e}"
    except ValueError as e:
        return None, f"JSON 파싱 실패: {e}"


def _entries(data: Dict[str, object], key: str, problems: List[str], required: bool) -> List[Dict[str, object]]:
    v = data.get(key)
    if v is None:
        if required:
            problems.append(f"{key}[] 필드 없음")
        return []
    if not isinstance(v, list):
        problems.append(f"{key} 는 배열이어야 함")
        return []
    out = []
    for i, e in enumerate(v):
        if not isinstance(e, dict):
            problems.append(f"{key}[{i}] 는 객체여야 함")
            continue
        out.append(e)
    return out


def run_gate(gate: str, path: Path, case_dir: Path, as_of: str, verifier=None, mode: str = "full") -> Dict[str, object]:
    from citations import render_markdown
    v = verifier or _default_verifier(as_of)
    problems: List[str] = []
    if gate in ("statute_evidence", "precedent_evidence", "risk_memo"):
        data, err = _load_json(path)
        if err:
            return {"verdict": "FAIL", "problems": [err], "report_md": f"**형식 문제**\n- {err}"}
        if not isinstance(data, dict):
            return {"verdict": "FAIL", "problems": ["최상위는 JSON 객체여야 함(스키마: references/roles.md)"],
                    "report_md": "**형식 문제**\n- 최상위는 JSON 객체여야 함"}
        findings = []
        allowed_without = False

        def check(entry: Dict[str, object], where: str, quote_key: Optional[str], kind: Optional[str],
                  quote_required: bool = False, min_quote: int = 0) -> None:
            cit = entry.get("citation")
            q = entry.get(quote_key) if quote_key else None
            if q is not None and not isinstance(q, str):
                problems.append(f"{where}.{quote_key} 는 문자열이어야 함")
                return
            if quote_required and not (q or "").strip():
                problems.append(f"{where}.{quote_key} 없음(원문 발췌 필수)")
            if q is not None and min_quote and len(normalize_for_match(q)) < min_quote:
                problems.append(f"{where}.{quote_key} 가 너무 짧음({min_quote}자 이상 원문 발췌)")
            f, probs = v.verify_entry(cit, q if (q or "").strip() else None, kind)
            problems.extend(f"{where}: {p}" for p in probs)
            if f is not None:
                findings.append(f)

        if gate == "statute_evidence":
            provs = _entries(data, "provisions", problems, True)
            if not provs and not problems:
                problems.append("provisions[] 가 비어 있음")
            for i, p in enumerate(provs):
                check(p, f"provisions[{i}]", "quote", "statute")
            for i, p in enumerate(_entries(data, "delegated", problems, False)):
                check(p, f"delegated[{i}]", "quote", "statute")
        elif gate == "precedent_evidence":
            precs = _entries(data, "precedents", problems, True)
            if not precs and not data.get("no_precedent_reason"):
                problems.append("선례가 없으면 no_precedent_reason 으로 사유를 적을 것")
            if not precs and data.get("no_precedent_reason"):
                allowed_without = True
            for i, p in enumerate(precs):
                check(p, f"precedents[{i}]", "holding_quote", "case", quote_required=True, min_quote=MIN_HOLDING_QUOTE)
            for i, a in enumerate(_entries(data, "authorities", problems, False)):
                check(a, f"authorities[{i}]", "quote", "case")
        else:  # risk_memo
            risks = _entries(data, "risks", problems, True)
            if not risks and not problems:
                problems.append("risks[] 가 비어 있음")
            prose: List[str] = []
            for i, r in enumerate(risks):
                if r.get("severity") not in ("high", "medium", "low"):
                    problems.append(f"risks[{i}].severity 는 high|medium|low")
                for k in ("risk", "counter_argument", "mitigation"):
                    if not isinstance(r.get(k), str) or not r.get(k, "").strip():
                        problems.append(f"risks[{i}].{k} 없음")
                basis = r.get("basis", [])
                basis = [basis] if isinstance(basis, str) else basis
                if not isinstance(basis, list):
                    problems.append(f"risks[{i}].basis 는 인용 문자열 배열이어야 함")
                    basis = []
                for j, b in enumerate(basis):
                    check({"citation": b}, f"risks[{i}].basis[{j}]", None, None)
                prose += [r.get(k, "") for k in ("risk", "counter_argument", "mitigation") if isinstance(r.get(k), str)]
            # 서술 속 인용도 검증한다(있으면 전부 통과해야 함). 리스크 메모는 인용이 없어도 된다.
            findings += [f for f in _findings(v.verify_text("\n".join(prose)))]
            allowed_without = True
        rep = v.summarize(findings)
        rv = rep["verdict"]
        if rv == "NO_CITATIONS":
            if allowed_without:
                rv = "PASS"
            else:
                problems.append("검증할 인용이 없음")
        if problems and rv in ("PASS", "INCOMPLETE", "NO_CITATIONS"):
            rv = "FAIL"
        md = render_markdown(rep, "게이트 검증 보고서")
        if problems:
            md += "\n\n**형식 문제**\n" + "\n".join(f"- {x}" for x in problems)
        return {"verdict": rv, "problems": problems, "report": rep, "report_md": md}
    if gate == "draft":
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            return {"verdict": "FAIL", "problems": [f"초안을 읽을 수 없음: {e}"], "report_md": ""}
        visible = re.sub(r"<!--.*?-->", "", text, flags=re.S)   # 템플릿 주석은 형식 검사에서 제외
        m = _SELF_VERDICT_RE.search(visible)
        if m:
            problems.append(f"초안에 검증·감사 판정이나 시스템 문구('{m.group(0)}')를 쓰지 말 것 — 판정·보고서는 발행 시 시스템이 부록으로 붙임")
        if mode != "verify":    # verify 모드: 이미 있는 문서의 인용 감사 — 의견서 형식 요구 없음
            m = _WIN_PCT_RE.search(visible)
            if m:
                problems.append(f"근거 없는 수치 확률 금지: '{m.group(0)}' — 높음/중간/낮음 + 근거로 서술")
            missing = [k[0] for k in _FIRAC_KEYS if not any(x in visible for x in k)]
            if len(missing) > 1:
                problems.append(f"FIRAC 구성 요소 누락: {', '.join(missing)}")
            if "결론" not in visible[:1200]:
                problems.append("결론(및 즉시 행동)을 문서 맨 앞 1,200자 안에 둘 것")
        evidence = []
        for name in ("evidence_statute.json", "evidence_precedent.json", "risk_memo.json"):
            p = case_dir / name
            if p.is_file():
                data, err = _load_json(p)
                if not err:
                    evidence += _strings(data)
        rep = v.verify_text(text, evidence_text="\n".join(evidence) if evidence else None)
        verdict = rep["verdict"]
        if problems and verdict in ("PASS", "INCOMPLETE", "NO_CITATIONS"):
            verdict = "FAIL"
        return {"verdict": verdict, "problems": problems, "report": rep,
                "report_md": render_markdown(rep) + ("\n\n**형식 문제**\n" + "\n".join(f"- {p}" for p in problems) if problems else "")}
    return {"verdict": "PASS", "problems": [], "report_md": ""}


def _findings(rep: Dict[str, object]):
    """verify_text 보고서의 findings(dict) → Finding 객체로 되돌린다(게이트에서 합산용)."""
    from citations import Finding
    for f in rep.get("findings", []):
        yield Finding(**f)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_text_arg(path: Optional[str], what: str) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        raise GateError(f"{what} 파일이 없음: {path}")
    try:
        return p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise GateError(f"{what} 파일을 읽을 수 없음: {e}")


def main(argv: Optional[List[str]] = None) -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(prog="legal_swarm.py", description="legal-ultra v2 작업판")
    ap.add_argument("--workspace")
    sp = ap.add_subparsers(dest="cmd")
    p = sp.add_parser("init")
    p.add_argument("--case-id")
    p.add_argument("--title", required=True)
    p.add_argument("--facts")
    p.add_argument("--facts-file")
    p.add_argument("--issues", nargs="+", required=True)
    p.add_argument("--domain", default="")
    p.add_argument("--as-of")
    p.add_argument("--mode", default="full", choices=list(MODES))
    p.add_argument("--budget", type=int, default=2)
    p.add_argument("--plan", help="태스크 목록 JSON(templates/legal_plan.json 형식)")
    p.add_argument("--draft", help="verify 모드: 검증할 문서")
    p.add_argument("--single-agent", action="store_true",
                   help="서브에이전트가 없는 런타임: 감사 직무 분리를 강제하지 않고 최종본에 그 사실을 공개")
    p = sp.add_parser("claim")
    p.add_argument("--case-id", required=True)
    p.add_argument("--role", required=True, choices=ROLES)
    p.add_argument("--worker", required=True)
    p = sp.add_parser("submit")
    p.add_argument("--case-id", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--accept-incomplete", action="store_true")
    p = sp.add_parser("audit")
    p.add_argument("--case-id", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--verdict", required=True, choices=["approve", "revise"])
    p.add_argument("--issues", nargs="*", default=[])
    p.add_argument("--issues-file")
    p.add_argument("--accept-incomplete", action="store_true")
    p = sp.add_parser("finalize")
    p.add_argument("--case-id", required=True)
    p.add_argument("--token", required=True)
    p = sp.add_parser("board")
    p.add_argument("--case-id")
    sp.add_parser("cases")
    p = sp.add_parser("log")
    p.add_argument("--case-id", required=True)
    p = sp.add_parser("release")
    p.add_argument("--case-id", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--reason", default="")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 3
    b = Board(args.workspace)
    try:
        if args.cmd == "init":
            facts = args.facts or _read_text_arg(args.facts_file, "--facts-file")
            plan = None
            if args.plan:
                try:
                    plan = json.loads(_read_text_arg(args.plan, "--plan")).get("tasks")
                except (ValueError, AttributeError) as e:
                    raise GateError(f"--plan JSON 형식 오류: {e}")
            cid = b.init_case(args.title, facts, args.issues, args.domain, args.as_of, args.mode, args.budget,
                              args.case_id, plan, args.draft, args.single_agent)
            print(json.dumps({"case_id": cid, "dir": str(b.case_dir(cid)), "board": b.board(cid)["tasks"]}, ensure_ascii=False, indent=2))
        elif args.cmd == "claim":
            pk = b.claim(args.case_id, args.role, args.worker)
            print(json.dumps(pk, ensure_ascii=False, indent=2) if pk else "null")
        elif args.cmd == "submit":
            r = b.submit(args.case_id, args.task_id, args.token, args.accept_incomplete)
            print(r.get("report_md", ""))
            print(json.dumps({"accepted": r["accepted"], "verdict": r["verdict"], "problems": r["problems"]}, ensure_ascii=False, indent=2))
            return 0 if r["accepted"] else 1
        elif args.cmd == "audit":
            issues = list(args.issues)
            if args.issues_file:
                issues += [l.strip("- ").strip() for l in _read_text_arg(args.issues_file, "--issues-file").splitlines() if l.strip()]
            print(json.dumps(b.audit(args.case_id, args.token, args.verdict, issues, args.accept_incomplete), ensure_ascii=False, indent=2))
        elif args.cmd == "finalize":
            print(json.dumps(b.finalize(args.case_id, args.token), ensure_ascii=False, indent=2))
        elif args.cmd == "board":
            bd = b.board(args.case_id)
            if not bd.get("case"):
                print("사안 없음")
                return 0
            c = bd["case"]
            print(f"[{c['case_id']}] {c['title']} · mode={c['mode']} · as-of={c['as_of']} · status={c['status']} · 예산 {c['budget']}"
                  + (" · 단일 에이전트" if c.get("single_agent") else ""))
            print(f"{'task':<14}{'role':<19}{'status':<16}{'retry':<6}worker")
            for t in bd["tasks"]:
                print(f"{t['task_id']:<14}{t['role']:<19}{t['status']:<16}{t['retry_count']:<6}{t['worker'] or ''}")
            for a in bd["audits"]:
                print(f"  audit#{a['id']} {a['verdict']} (검증기 {a['verifier_verdict']}) by {a['auditor']} {a['created_at']}")
            print(f"dir: {bd['dir']}")
        elif args.cmd == "cases":
            for r in b.conn.execute("SELECT case_id,title,status,mode,as_of,created_at FROM cases ORDER BY created_at DESC"):
                print(f"{r['case_id']}  [{r['status']}] {r['title']} (mode={r['mode']}, as-of {r['as_of']})")
        elif args.cmd == "log":
            for r in b.conn.execute("SELECT ts,actor,action,detail FROM events WHERE case_id=? ORDER BY id", (args.case_id,)):
                print(f"{r['ts']} {r['actor']:<16} {r['action']:<9} {r['detail'][:200]}")
        elif args.cmd == "release":
            b.release(args.case_id, args.task_id, args.reason)
            print("released")
    except GateError as e:
        print(f"⛔ {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
