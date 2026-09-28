# 게이트 — 코드로 강제되는 수락 기준

v1 의 "4대 게이트"는 문서에만 있었고 코드는 **산출물 파일이 비어 있지 않은지**만 확인했다(재현: `docs/REVIEW-2026-09-28.md`).
v2 는 아래를 `scripts/legal_swarm.py` 가 직접 검사한다. 사람이나 에이전트가 판정 문구를 써 넣어 통과시킬 방법은 없다.

| 게이트 | 시점 | 검사 (실패 시 태스크는 running 으로 남아 같은 token 으로 재제출) |
|---|---|---|
| G0 접수 | `init` | 제목·사실관계·쟁점 1개 이상, 올바른 as_of·mode, 같은 case_id 덮어쓰기 금지 |
| G1 실정법 | T1 `submit` | `provisions[]` 필수 · 각 `citation`(+`quote`)을 검증기로 대조 → 전부 `VERIFIED` |
| G2 판례 | T2 `submit` | 판례마다 `holding_quote` 필수 · 법원·선고일·사건번호·인용문 `VERIFIED` · 선례 없음은 `no_precedent_reason` 로만 허용 |
| G3 리스크 | T3 `submit` | 각 risk 의 `risk`·`severity`·`counter_argument`·`mitigation` · 포함된 인용 전부 `VERIFIED` |
| G4 초안 | T4 `submit` | 모든 인용 `VERIFIED` · 자기 판정(PASS/APPROVED/무결점) 금지 · 승소확률 % 금지 · 결론이 첫 1,200자 안 · FIRAC 요소 · 증거 밖 인용은 경고 |
| G5 감사 | `audit` | 시스템이 초안을 **다시** 검증. `approve` 는 검증 PASS·형식 문제 0건일 때만. `revise` → T4 `needs_revision`(재배정 가능), T5 `pending`. 리비전 예산(기본 2회) 초과 → 사안 `escalated`(동결, 인간 검토) |
| G6 발행 | `finalize` | 최신 감사가 approve · 승인된 초안 SHA-256 과 현재 초안이 같을 것 · 검증 보고서·감사 기록·기준일·한계·면책을 시스템이 부록으로 첨부 |

## INCOMPLETE 처리

출처가 없어서 확인 못 한 인용(`UNVERIFIABLE`)만 있고 실패가 없으면 판정은 `INCOMPLETE` 다.

- `submit --accept-incomplete` / `audit --accept-incomplete` 로 **명시적으로** 받아들일 수 있다(이벤트 로그에 기록).
- 이렇게 승인된 최종본에는 "검증 미완료" 경고가 부록 A 맨 위에 붙는다.
- `FAIL`(불일치·미확인·특정불가)은 어떤 플래그로도 통과시킬 수 없다.

## 리비전 루프

```
T4 done → T5 claim → audit revise ─┬─ retry ≤ budget → T4 needs_revision → counsel_builder claim(피드백 포함) → submit → T5 pending → …
                                   └─ retry > budget → case escalated, 남은 태스크 blocked
```

## 이벤트 로그

`python3 scripts/legal_swarm.py log --case-id <ID>` — claim·gate·audit·finalize·release 가 시각·행위자와 함께 남는다.
