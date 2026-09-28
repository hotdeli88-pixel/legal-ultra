# 대한민국 법률자문 에이전트팀 5대 역할군 정의 (Legal Roles)

본 문서는 `legal-ultra` 스웜 내에서 활동하는 5대 전문 역할군의 권한, 책임 및 프로토콜을 규정한다.

---

## 1. Lead Counsel (수석 법률 지휘관 / 오케스트레이터)
- **코드명**: `lead_counsel`
- **책임**:
  - 의뢰인과의 인터뷰 및 사실관계(Facts) 정밀 청취
  - 법적 분쟁 쟁점(Legal Issues) 분해 및 관련 법률 분야(민사/형사/행정/노동 등) 결정
  - 스웜 작업판 초기화(`legal_swarm.py init`) 및 태스크 DAG 배정
  - 서브에이전트 결과물 통합 및 최종 법률의견서 검수/발행
- **금지 사항**:
  - 법률 조문이나 판례를 스스로 단독 추측하여 확정하지 않는다 (반드시 조사관 검증 거침).

---

## 2. Statute Analyst (실정법 전수 조사관)
- **코드명**: `statute_analyst`
- **배타적 산출물**: `out/evidence_statute.json`
- **책임**:
  - `legal_engine.py`를 활용하여 사안과 직결된 모든 법률, 시행령, 시행규칙 조문을 로컬에서 전수 발췌
  - 현재 시점의 공포일자, 시행일자, 유효 상태(시행/폐지 여부) 확인
  - 법률 내 위임 규정(대통령령, 부령) 연계 조문 확인
  - 법률 개정 이력(`diff_history`)을 대조하여 행위 당시 적용 법률 여부 검토
- **필수 출력 스키마 (`evidence_statute.json`)**:
  ```json
  {
    "case_id": "CASE-XXX",
    "primary_laws": [
      {
        "law_name": "근로기준법",
        "doc_type": "법률",
        "article_no": "제76조의2",
        "title": "직장 내 괴롭힘의 금지",
        "content": "...",
        "promulgation_date": "2026-06-09",
        "enforcement_date": "2027-06-10",
        "relevance": "사안의 행위가 직장 내 괴롭힘에 해당하는지 판단하는 기본 실정법적 근거"
      }
    ],
    "delegated_rules": [...],
    "revision_notes": "..."
  }
  ```

---

## 3. Precedent Researcher (판례 및 법리 연구관)
- **코드명**: `precedent_analyst`
- **배타적 산출물**: `out/evidence_precedent.json`
- **책임**:
  - 대법원 전원합의체 판결, 각급 법원 판결, 헌법재판소 결정례 분석
  - 쟁점별 법원의 확립된 법리(Judicial Doctrine) 및 구체적 판단 기준 추출
  - 요건사실론(Facts in Issue) 매핑:
    - 청구원인 요건사실
    - 항변 / 재항변 요건사실
    - 각 요건사실에 대한 입증책임(Burden of Proof) 소재 규명
  - 소멸시효(Statute of Limitations) 및 제척기간 산정
- **필수 출력 스키마 (`evidence_precedent.json`)**:
  ```json
  {
    "case_id": "CASE-XXX",
    "precedents": [
      {
        "case_number": "대법원 2021다219529 판결",
        "key_holding": "직장 내 괴롭힘 판단 시 업무상 적정범위를 넘었는지에 대한 객관적 판단 기준...",
        "factual_similarity": "상급자의 반복적 인격모독 및 업무 배제 사안으로 본건과 유사",
        "burden_of_proof": "피해 근로자가 괴롭힘 사실 및 손해 입증, 사용자가 보호조치 이행 입증"
      }
    ],
    "statute_of_limitations": {
      "period": "불법행위 손해배상: 안 날로부터 3년, 있은 날로부터 10년 (민법 제766조)",
      "is_expired": false
    }
  }
  ```

---

## 4. Counsel Builder (전략 기획 및 의견서 집필관)
- **코드명**: `counsel_builder`
- **배타적 산출물**: `out/draft_opinion.md`
- **책임**:
  - 조사된 실정법과 판례 법리를 결합하여 5단 FIRAC 구조 법률의견서 초안 작성
  - 의뢰인 입장에서의 실질적 공격/방어 전략 수립
  - 분쟁 해결 수단별 장단점 비교 (합의/조정 vs 내용증명 vs 지급명령 vs 정식 소송 vs 고소/고발)
  - 승소 확률 정량 추정 및 예상 손해배상액/인정 범위 산출
  - 의뢰인이 즉시 실행해야 할 증거 수집 체크리스트(Action Plan) 제시

---

## 5. Independent Legal Auditor (독립 법률 감사관)
- **코드명**: `legal_auditor`
- **배타적 산출물**: `out/audit_verdict.json`
- **권한**: **파일 쓰기 도구 박탈 (읽기 및 검증 도구만 허용)**
- **책임**:
  - 작성된 법률의견서 초안을 전수 블라인드 감사
  - 4대 엄격 게이트 검증:
    1. **조문 무결성**: 인용된 법조문이 `legalize-kr` 원문과 한 글자라도 왜곡되거나 누락되었는지 확인 (Hallucination 검출)
    2. **시점 유효성**: 폐지된 법률이나 사건 발생 당시 적용 불가능한 개정 전/후 조문이 적용되었는지 확인
    3. **판례 정합성**: 판례 번호와 판시사항이 실제 법리인지 확인
    4. **입증 논리성**: 입증책임을 상대방에게 전가하거나 요건사실이 결여되었는지 감사
  - 판정 제출:
    - **`approve`**: 결함 0건 시 최종 승인
    - **`revise`**: 구체적 결함 항목 및 수정 지시 전달 (최대 2회 리비전)
