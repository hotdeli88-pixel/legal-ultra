# Changelog

## 2.0.0 — 2026-09-28 (law + legal-ultra 통합)

적대적 검토 결과와 결함별 재현 근거: `docs/REVIEW-2026-09-28.md`.

### 추가
- `scripts/legal.py` 통합 CLI: status · setup · law · article(시행 판정) · search · delegated · history · precedent · constitutional ·
  interpretation · decision · admrule · ordinance · thdcmp · research · verify · targets · api-probe
- `scripts/citations.py` 인용 추출·다중 출처 검증: 조·의·항·호·목, 나열·같은 법·같은 조·약칭 정의, 제목·인용문 대조,
  판례 법원·선고일·전원합의체, 헌재·법제처 해석, 5단계 상태와 fail-closed 문서 판정, 병렬 검증
- `scripts/precedent_engine.py` precedent-kr 판례 색인(12.9만 사건번호, 부분 클론으로 오프라인 검증, TSV 캐시)
- `scripts/law_api.py` 법제처 DRF 클라이언트(v1 law 에서 이식·재작성): 실측 레지스트리 72종, 응답 유형 구분, `nb` 사건번호 검색
- 시행일(as-of) 판정: 시행예정 판본의 신설·개정·삭제 표시, 전체 이력 미러에서는 시행 중 커밋으로 정밀 판정
- 반대논리·리스크 감사관(T3, v1 law 의 Devil's Advocate)을 작업판에 편입 → 6개 역할
- 검증 통과가 테스트로 보장되는 예시 산출물 `templates/examples/`
- 테스트 84개(실데이터 발췌·DRF 실측 픽스처, 네트워크 불필요)

### 변경
- `legal_swarm.py` 재작성: (case_id, task_id) 격리, 리비전 루프·예산(초과 시 escalated), 게이트를 코드로 강제,
  감사 approve 는 검증 PASS 일 때만, 최종본은 승인 초안 SHA-256 에만 + 검증·감사 부록 시스템 생성, 작업공간 기준 경로
- `legal_engine.py` 재작성(v1 CLI 호환 유지): 결정론적 판본 선택, 스트리밍 프론트매터, 삭제 조문, 부칙·장 제목 분리, 위임 조문 정규식 수정,
  git grep 한글 경로, 정확 일치 법령명
- 문서 전면 개정: 승인 문구가 미리 채워진 템플릿 제거, 판례 왜곡 예시(2021다219529) 교체, 승소확률 % 폐지(가능성 등급)

### 제거
- 외부 API 의존 금지 원칙("100% 로컬") → 로컬 우선 + DRF 보완·교차확인으로 대체
- Antigravity 전용 서술 → 런타임 중립(Claude Code 는 Agent 도구, 없으면 순차 수행)
