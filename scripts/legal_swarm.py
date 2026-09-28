"""legal_swarm.py - Ultra 스웜 기반 대한민국 법률자문 에이전트팀 오케스트레이션 엔진.

SQLite 기반 단일 작업판(Blackboard)을 단독 관리하며,
사안 접수 -> 배타적 DAG 생성 -> 태스크 선점/패킷 발행 -> 증거 제출 -> 독립 법률 감사 -> 최종 의견서 빌드
전 과정을 완벽히 추적하고 무결성을 강제합니다.
"""

import os
import sys
import json
import sqlite3
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

DEFAULT_DB_PATH = "out/legal_blackboard.db"

class LegalSwarmDB:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS case_meta (
                case_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                facts TEXT NOT NULL,
                issues TEXT NOT NULL,
                domain TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'in_progress'
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                title TEXT NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                deps TEXT, -- JSON array of task IDs
                write_paths TEXT, -- JSON array of file paths
                acceptance TEXT, -- JSON array of criteria
                worker TEXT,
                token TEXT,
                result TEXT, -- JSON
                retry_count INTEGER DEFAULT 0,
                claimed_at TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (case_id) REFERENCES case_meta(case_id)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                auditor TEXT NOT NULL,
                verdict TEXT NOT NULL, -- 'approve' or 'revise'
                evidence TEXT, -- JSON
                comments TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

    def close(self):
        """데이터베이스 연결 닫기."""
        if hasattr(self, "conn") and self.conn:
            self.conn.close()

    def init_case(self, case_id: str, title: str, facts: str, issues: List[str], domain: str, plan_file: Optional[str] = None):
        """사안 등록 및 기본 5대 태스크 DAG 자동 구성."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO case_meta (case_id, title, facts, issues, domain) VALUES (?, ?, ?, ?, ?)",
                (case_id, title, facts, json.dumps(issues, ensure_ascii=False), domain)
            )

        if plan_file and os.path.isfile(plan_file):
            with open(plan_file, "r", encoding="utf-8") as fp:
                tasks_data = json.load(fp)
            for t in tasks_data.get("tasks", []):
                self._insert_task(case_id, t)
        else:
            # 기본 표준 법률 자문 DAG 자동 구성
            default_tasks = [
                {
                    "id": "T1_STATUTE",
                    "title": "실정법 조문 전수 조사 및 위임규정/개정이력 대조",
                    "role": "statute_analyst",
                    "kind": "research",
                    "deps": [],
                    "write_paths": ["out/evidence_statute.json"],
                    "acceptance": [
                        "legalize-kr 로컬 법령에서 쟁점 관련 법률, 시행령, 시행규칙 조문 원문 전수 발췌",
                        "공포일/시행일 기준 현재 유효 조문 여부 확인",
                        "위임 규정 및 하위법령 일치 확인"
                    ]
                },
                {
                    "id": "T2_PRECEDENT",
                    "title": "대법원 리딩 판례 및 요건사실론/입증책임 법리 분석",
                    "role": "precedent_analyst",
                    "kind": "research",
                    "deps": [],
                    "write_paths": ["out/evidence_precedent.json"],
                    "acceptance": [
                        "쟁점별 대법원 판례 및 판단 기준 2건 이상 확보",
                        "청구원인 및 항변사유의 요건사실과 입증책임 배분 명시",
                        "소멸시효 및 제척기간 검토 완료"
                    ]
                },
                {
                    "id": "T3_STRATEGY",
                    "title": "5단 FIRAC 구조 법률의견서 초안 및 분쟁 대응 전략 수립",
                    "role": "counsel_builder",
                    "kind": "build",
                    "deps": ["T1_STATUTE", "T2_PRECEDENT"],
                    "write_paths": ["out/draft_opinion.md"],
                    "acceptance": [
                        "Facts(사실관계), Issues(쟁점), Rules(법령/판례), Application(포섭), Conclusion(결론) 5단 완성",
                        "의뢰인 공격/방어 시나리오 및 승소/패소 리스크 정량 평가",
                        "내용증명/증거수집/소송 등 단계별 Action Plan 체크리스트 포함"
                    ]
                },
                {
                    "id": "T4_AUDIT",
                    "title": "독립 법률 감사 (조문/판례 왜곡 및 논리모순 블라인드 검증)",
                    "role": "legal_auditor",
                    "kind": "review",
                    "deps": ["T3_STRATEGY"],
                    "write_paths": ["out/audit_verdict.json"],
                    "acceptance": [
                        "조문 허위인용(Hallucination) 0건 확인",
                        "판례 인용 왜곡 및 오독 0건 확인",
                        "입증책임 배분 및 법적 논리 모순 여부 독립 감사"
                    ]
                },
                {
                    "id": "T5_FINAL",
                    "title": "최종 정본 법률의견서 통합 및 의뢰인 보고서 발행",
                    "role": "lead_counsel",
                    "kind": "deliver",
                    "deps": ["T4_AUDIT"],
                    "write_paths": ["out/final_legal_opinion.md"],
                    "acceptance": [
                        "독립 감사의 지적사항 100% 반영된 무결점 최종 자문서 생성",
                        "ADHD-Friendly 초간결 핵심 결론 및 상세 전문 듀얼 레이아웃"
                    ]
                }
            ]
            for t in default_tasks:
                self._insert_task(case_id, t)

    def _insert_task(self, case_id: str, t: Dict[str, Any]):
        with self.conn:
            self.conn.execute("""
            INSERT OR REPLACE INTO tasks 
            (id, case_id, title, role, kind, status, deps, write_paths, acceptance)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """, (
                t["id"],
                case_id,
                t["title"],
                t["role"],
                t["kind"],
                json.dumps(t.get("deps", []), ensure_ascii=False),
                json.dumps(t.get("write_paths", []), ensure_ascii=False),
                json.dumps(t.get("acceptance", []), ensure_ascii=False)
            ))

    def claim_task(self, worker: str, role: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """의존성이 모두 충족(done)된 pending 태스크 1건 선점."""
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE status = 'pending'")
            pending = cursor.fetchall()

            cursor.execute("SELECT id FROM tasks WHERE status = 'done'")
            done_ids = {row["id"] for row in cursor.fetchall()}

            for row in pending:
                deps = json.loads(row["deps"] or "[]")
                if all(d in done_ids for d in deps):
                    if role and row["role"] != role:
                        continue
                    token = f"tok_{int(datetime.now().timestamp())}_{row['id']}"
                    cursor.execute("""
                    UPDATE tasks SET status = 'running', worker = ?, token = ?, claimed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """, (worker, token, row["id"]))
                    return dict(row) | {"token": token, "status": "running", "worker": worker}
        return None

    def submit_task(self, task_id: str, token: str, result_data: Dict[str, Any]) -> bool:
        """작업 완료 제출 및 검증."""
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE id = ? AND token = ?", (task_id, token))
            row = cursor.fetchone()
            if not row:
                return False

            # 배타적 파일 소유권 검증 (실제 파일 생성 여부 확인)
            write_paths = json.loads(row["write_paths"] or "[]")
            for wp in write_paths:
                p = Path(wp)
                if not p.is_file() or p.stat().st_size == 0:
                    raise ValueError(f"수락 실패: 배정된 산출물 파일 {wp}이 생성되지 않았거나 크기가 0입니다.")

            cursor.execute("""
            UPDATE tasks SET status = 'done', result = ?, completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """, (json.dumps(result_data, ensure_ascii=False), task_id))
            return True

    def submit_audit(self, task_id: str, auditor: str, verdict: str, evidence: Dict[str, Any], comments: str) -> bool:
        """독립 법률 감사 결과 기록."""
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("""
            INSERT INTO audit_logs (task_id, auditor, verdict, evidence, comments)
            VALUES (?, ?, ?, ?, ?)
            """, (task_id, auditor, verdict, json.dumps(evidence, ensure_ascii=False), comments))

            if verdict == "revise":
                cursor.execute("""
                UPDATE tasks SET status = 'needs_revision', retry_count = retry_count + 1
                WHERE id = 'T3_STRATEGY'
                """)
            return True

    def get_board(self) -> Dict[str, Any]:
        """현재 작업판 현황."""
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM case_meta ORDER BY created_at DESC LIMIT 1")
            case_row = cursor.fetchone()
            cursor.execute("SELECT * FROM tasks ORDER BY id")
            tasks = [dict(r) for r in cursor.fetchall()]
            cursor.execute("SELECT * FROM audit_logs ORDER BY id DESC")
            audits = [dict(r) for r in cursor.fetchall()]

            return {
                "case": dict(case_row) if case_row else None,
                "tasks": tasks,
                "audits": audits
            }

def main():
    parser = argparse.ArgumentParser(description="Legal Swarm Harness")
    subparsers = parser.add_subparsers(dest="command")

    # init
    p_init = subparsers.add_parser("init")
    p_init.add_argument("--case-id", required=True)
    p_init.add_argument("--title", required=True)
    p_init.add_argument("--facts", required=True)
    p_init.add_argument("--issues", nargs="+", required=True)
    p_init.add_argument("--domain", default="민사/노동")
    p_init.add_argument("--db", default=DEFAULT_DB_PATH)

    # claim
    p_claim = subparsers.add_parser("claim")
    p_claim.add_argument("--worker", required=True)
    p_claim.add_argument("--role")
    p_claim.add_argument("--db", default=DEFAULT_DB_PATH)

    # submit
    p_submit = subparsers.add_parser("submit")
    p_submit.add_argument("--task-id", required=True)
    p_submit.add_argument("--token", required=True)
    p_submit.add_argument("--result", required=True)
    p_submit.add_argument("--db", default=DEFAULT_DB_PATH)

    # board
    p_board = subparsers.add_parser("board")
    p_board.add_argument("--db", default=DEFAULT_DB_PATH)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    db = LegalSwarmDB(args.db)

    if args.command == "init":
        db.init_case(args.case_id, args.title, args.facts, args.issues, args.domain)
        print(f"[Swarm] 사안 '{args.title}'(ID: {args.case_id}) 작업판 초기화 완료 (5대 태스크 DAG 수립).")

    elif args.command == "claim":
        task = db.claim_task(args.worker, args.role)
        if task:
            print(json.dumps(task, ensure_ascii=False, indent=2))
        else:
            print("null")

    elif args.command == "submit":
        with open(args.result, "r", encoding="utf-8") as fp:
            res_data = json.load(fp)
        ok = db.submit_task(args.task_id, args.token, res_data)
        if ok:
            print(f"[Swarm] 태스크 {args.task_id} 제출 및 수락 게이트 통과 완료.")
        else:
            print(f"[Swarm] 태스크 {args.task_id} 제출 실패 (토큰 불일치).")

    elif args.command == "board":
        board = db.get_board()
        print("=" * 60)
        case = board["case"]
        if case:
            print(f"사안: [{case['domain']}] {case['title']} (ID: {case['case_id']})")
            print(f"사실관계: {case['facts']}")
            print(f"쟁점: {case['issues']}")
        print("-" * 60)
        print(f"{'ID':<12} | {'Role':<18} | {'Status':<12} | {'Title'}")
        print("-" * 60)
        for t in board["tasks"]:
            print(f"{t['id']:<12} | {t['role']:<18} | {t['status']:<12} | {t['title']}")
        print("=" * 60)

if __name__ == "__main__":
    main()
