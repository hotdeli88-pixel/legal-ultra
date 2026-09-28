# 게이트 — 코드로 강제되는 수락 기준

v1 의 "4대 게이트"는 문서에만 있었고 코드는 **산출물 파일이 비어 있지 않은지**만 확인했다(재현: `docs/REVIEW-2026-09-28.md`).
v2 는 아래를 `scripts/legal_swarm.py` 가 직접 검사한다. v2.0 에서 2차 적대적 검토로 뚫린 경로(같은 작업자의 자가 감사, 승인 후 보고서
바꿔치기, 문장 조립식 증거 검증의 인용문 누락, 게이트 실행 중 재배정 경쟁)는 v2.1 에서, 3차 검토로 뚫린 경로(`--plan` 으로 집필 태스크
이름을 바꿔 직무 분리 회피, 폭 없는 문자·전각·닮은꼴 글자로 위장한 판정 문구와 가짜 부록, 증거 citation 에 '이유'를 넣어 판결 본문 대조 열기)는
v2.2 에서 막았다. 남은 전제는 아래 '한계'에 적었다.

| 게이트 | 시점 | 검사 (실패 시 태스크는 running 으로 남아 같은 token 으로 재제출) |
|---|---|---|
| G0 접수 | `init` | 제목·사실관계·쟁점 1개 이상, 올바른 as_of·mode, 같은 case_id 덮어쓰기 금지, `--plan`·`--draft`·`--facts-file` 존재·형식. `--plan` 은 역할·종류·게이트의 짝(아래 표), 게이트 태스크 이름 고정(`T4_DRAFT`·`T5_AUDIT`·`T6_FINAL`, 초안 산출물 `draft_opinion.md`), 감사는 초안에·발행은 감사에 의존, 없는 태스크 의존·순환 금지 |
| G1 실정법 | T1 `submit` | `provisions[]` 필수 · 항목마다 `citation` 에 인용 **하나**, `quote` 는 그 인용에 직접 짝지어 대조 → 전부 `VERIFIED` |
| G2 판례 | T2 `submit` | 판례마다 `holding_quote`(정규화 10자 이상) 필수 · 법원·선고일·사건번호·판시 인용문 `VERIFIED` — `holding_quote` 는 **판시사항·판결요지와만** 대조(citation 문구로 판결 이유 본문 대조를 열 수 없음) · 선례 없음은 `no_precedent_reason` 로만 허용 |
| G3 리스크 | T3 `submit` | 각 risk 의 `risk`·`severity`·`counter_argument`·`mitigation` · `basis[]` 의 인용 하나씩 + 서술 속 인용 전부 `VERIFIED` |
| G4 초안 | T4 `submit` | 모든 인용 `VERIFIED` · 판정·시스템 문구(PASS, APPROVED, 무결점, 인용 검증 보고서, 검증 판정, 감사 승인·감사 결과, 시스템 생성, 수정 금지, `✅ 확인`, `부록 A~C`, 원문 대조 완료, 🛡 …) 금지 — 검사는 **독자가 보는 글자**로: HTML 주석·태그·마크다운 강조·폭 없는 문자 제거, 전각 → 반각(NFKC), 키릴·그리스 닮은꼴 → 라틴(`P<ZWSP>ASS`·`ＰＡＳＳ`·`РАSS` 차단) · 승소확률 %(승소율·가능성·확률) 금지 · 결론이 첫 1,200자 안 · FIRAC 요소 · 증거 밖 인용은 경고 |
| G5 감사 | `claim`·`audit` | **직무 분리**: 조사·집필 태스크(역할 실정법·판례·반대논리·집필, 또는 종류 research·review·build — 이름이 아니라 역할·종류로 판단)를 한 번이라도 claim 한 작업자는 감사관이 될 수 없다(감사관도 이후 조사·집필 불가). 시스템이 초안을 **다시** 검증하고 그 순간의 SHA-256 을 고정. `approve` 는 검증 PASS·형식 문제 0건일 때만. 검증 보고서는 DB 에 저장. `revise` → T4 `needs_revision`, T5 `pending`. 리비전 예산(기본 2회) 초과 → 사안 `escalated`(동결, 인간 검토) |
| G6 발행 | `finalize` | 최신 감사가 approve · 승인된 SHA-256 과 현재 초안이 같을 것 · 부록 A 는 **DB 에 고정된 보고서만**(작업 폴더의 `verification_report.md` 는 참고용 사본이라 바꿔도 발행물에 반영되지 않음) · 감사 독립성·기준일·한계·면책을 시스템이 첨부 |

모든 커밋(제출 완료·감사 기록·발행)은 트랜잭션 안에서 **토큰을 다시 확인**한다. 검증이 도는 동안 수석이 태스크를 `release` 하고
다른 작업자가 다시 claim 했다면 옛 작업자의 제출은 반영되지 않는다.

### 사용자 정의 계획(`--plan`)의 역할 규칙

| 역할 | kind | gate |
|---|---|---|
| statute_analyst | research | statute_evidence 또는 none |
| precedent_analyst | research | precedent_evidence 또는 none |
| risk_advocate | review | risk_memo 또는 none |
| counsel_builder | build | draft (id `T4_DRAFT`) |
| legal_auditor | audit | audit (id `T5_AUDIT`) |
| lead_counsel | deliver | final (id `T6_FINAL`) |

## verify 모드

`init --mode verify --draft 문서` 는 이미 있는 문서의 인용 감사다. 의견서 형식(FIRAC·결론 위치·확률 표기) 검사는 하지 않고, 인용 검증과
판정 문구 금지만 적용한다. 고칠 집필 태스크가 없으므로 `revise` 는 곧바로 `escalated`(사람 검토)다.

## 단일 에이전트 런타임

서브에이전트가 없는 런타임에서는 같은 에이전트가 모든 역할을 순서대로 맡을 수밖에 없다. 이때는 `init --single-agent` 로 **명시**해야
직무 분리 검사를 건너뛰며, 최종본 부록 B 에 "감사 독립성: 단일 에이전트 실행"이 공개된다. 명시하지 않으면 분리가 강제된다.

## INCOMPLETE 처리

출처가 없어서 확인 못 한 인용(`UNVERIFIABLE`)만 있고 실패가 없으면 판정은 `INCOMPLETE` 다.

- `submit --accept-incomplete` / `audit --accept-incomplete` 로 **명시적으로** 받아들일 수 있다(이벤트 로그에 기록).
- 이렇게 승인된 최종본에는 "검증 미완료" 경고가 부록 A 맨 위에 붙는다.
- `FAIL`(불일치·미확인·특정불가)은 어떤 플래그로도 통과시킬 수 없다.

## 리비전 루프

```
T4 done → T5 claim(조사·집필 작업자 제외) → audit revise ─┬─ retry ≤ budget → T4 needs_revision → counsel_builder claim(피드백 포함) → submit → T5 pending → …
                                                     └─ retry > budget → case escalated, 남은 태스크 blocked
```

## 한계(코드로 막을 수 없는 것)

- 작업자 이름은 스스로 밝히는 값이다. 한 에이전트가 이름을 바꿔 가며 claim 하는 것까지 막지는 못한다 — 직무 분리는
  서브에이전트를 실제로 따로 띄우는 운영 규칙(SKILL.md 4절)과 함께 동작한다.
- 작업판 DB 와 작업 폴더는 같은 사용자 권한으로 쓸 수 있다. DB 를 직접 고치는 공격은 막지 않는다(이벤트 로그로 사후 추적).

## 이벤트 로그

`python3 scripts/legal_swarm.py log --case-id <ID>` — claim·gate·audit·finalize·release 가 시각·행위자와 함께 남는다.
