---
name: legal-ultra
description: "대한민국 법률 검토·자문 에이전트 팀 스킬(v2, 구 law + legal-ultra 통합). legalize-kr 법령 미러·precedent-kr 판례 미러·법제처 국가법령정보 OPEN API(DRF)를 교차 사용해 조문·판례·유권해석을 원문으로 확인하고, 6개 역할(수석·실정법·판례·반대논리·집필·독립감사) 작업판에서 FIRAC 의견서를 만든다. 모든 인용은 스크립트가 원문과 대조(조·항·호·목, 제목, 시행일, 선고일·법원, 인용문)해 검증하며 통과 못 하면 발행하지 않는다. 법률 검토, 법률 자문, 법률의견서, 법령·조문 확인, 판례 검색, 계약서·약관 검토, 행정처분 대응, 인용 검증(환각 점검), law, legal, legal-ultra, legalize-kr 요청에 사용."
license: MIT
compatibility: "Python 3.10+ (표준 라이브러리만), git. 선택: legalize-kr/precedent-kr 로컬 미러, law.go.kr 접속(DRF API). Claude Code(Agent 도구로 서브에이전트), 기타 에이전트 런타임(서브에이전트 없으면 순차 역할 수행)."
metadata:
  version: "2.2.0"
  supersedes: "law 1.0.0, legal-ultra 1.0.0"
  architecture: "source-grounded blackboard swarm with enforced citation gates"
---

# legal-ultra v2 — 대한민국 법률 검토 에이전트 팀

추론은 에이전트가, **사실 확인은 스크립트가** 한다. 조문·판례·해석례는 기억으로 쓰지 않고 `scripts/legal.py`로 원문을 열어 확인하며,
완성본의 인용은 `scripts/legal_swarm.py`가 원문과 다시 대조해 통과해야만 발행된다. 스크립트 경로는 이 스킬의 기본 디렉터리 기준이다
(Windows 에서는 `python3` 대신 `python` 또는 `py -3`).

## 0. 시작: 출처 확인 (매 세션 1회)

```bash
python3 scripts/legal.py status
```

| 출처 | 역할 | 없을 때 |
|---|---|---|
| legalize-kr 미러 (`LEGALIZE_KR_PATH`) | 법령 원문·시행일·개정표시, 빠른 오프라인 검색 | DRF 로 대체, 둘 다 없으면 법령 인용이 `UNVERIFIABLE` |
| precedent-kr 미러 (`PRECEDENT_KR_PATH`) | 판례 12.5만 건: 사건번호·법원·선고일·판시 원문 | DRF `prec` 로 대체 |
| 법제처 DRF API (`LAW_OPENAPI_OC`) | 헌재 결정·법령해석례·부처 해석·위원회 결정·행정규칙·자치법규·3단비교·교차확인 | 해당 자료는 `UNVERIFIABLE` |

출처가 없으면 사용자에게 알리고 `python3 scripts/legal.py setup`(미러 설치 안내·실행)을 제안한다. 설치·환경변수·한계는 `references/sources.md`.
`status` 가 "판본 이력 없음"이면 최신 공포본이 기준일에 시행 중이고 먼저 공포된 개정도 모두 시행된 법령만 판정하고 나머지는
`UNVERIFIABLE` 가 된다 — 정밀 판정에는 `setup`(기본: 전체 이력 부분 클론, 약 540MB)이 필요하다고 알린다.

## 1. 철칙 — 모든 역할 공통

1. **기억으로 인용 금지.** 조문·판례·해석례는 `legal.py article|precedent|constitutional|interpretation`으로 원문을 연 뒤에만 쓴다.
2. **인용 형식**(검증기가 읽는 형식): `「정식 법령명」 제N조의M제K항제L호`, 제목을 붙이면 원문 제목 그대로 `제750조(불법행위의 내용)`.
   판례는 `대법원 2021. 9. 16. 선고 2021다219529 판결`처럼 **법원·선고일·사건번호**를 함께. 약칭(중처법, 근퇴법)·법령명 없는 `제15조` 금지. 세부는 `references/citation_rules.md`.
3. **따옴표 안은 원문 그대로**(생략은 `…`, 뜻을 뒤집는 생략 금지). 요약·의역은 따옴표 없이 쓴다. 원문 인용은 출처와 같은 문장에 두거나
   `“…”(「민법」 제750조)`처럼 붙인다 — 검증기가 인용한 조·항(판례는 판시사항·판결요지)의 기준일 문언과 대조하고, 출처를 못 찾는 원문 인용은 실패다.
   판결 이유 본문을 인용할 때는 "위 판결은 이유에서 “…”라고 판단하였다"처럼 밝힌다.
4. **기준일(as-of) 시행 조문만 현행으로.** 미러에는 시행예정 판본이 들어 있을 수 있다(예: 2027-06-10 시행 근로기준법 개정). 시행 전 조항은
   "시행 예정"이라고 밝히고, 구법은 `구 「법령」(YYYY. M. D. 법률 제N호로 개정되기 전의 것) 제N조`로 판본을 특정한다.
5. **판정은 시스템이 붙인다.** 초안에 PASS/APPROVED/무결점을 쓰지 않는다. 검증·감사 결과는 `finalize`가 부록으로 붙인다.
6. **확률 숫자 금지.** 가능성은 `높음/중간/낮음` + 근거 2~3개 + 불확실 요인으로 쓴다(근거 없는 %는 환각이다).
7. **검증 결과를 숨기지 않는다.** `FAIL`은 고치고, `INCOMPLETE`(출처 부재)는 사용자에게 그대로 알린다.
8. **사실관계가 부족하면** 핵심 사실을 물어보거나, 가정을 명시하고 가정별 결론을 나눈다.

## 2. 모드

| 모드 | 용도 | 태스크 |
|---|---|---|
| `full` | 복합 사안 종합 의견서 | T1 실정법 · T2 판례 → T3 반대논리 → T4 초안 → T5 감사 → T6 발행 |
| `contract` | 계약서·약관·취업규칙 검토(조항별 수정안) | full 과 같음, 초안에 조항별 redline 표 |
| `administrative` | 과태료·시정명령·영업정지 대응, 이의신청·행정심판 | full 과 같음, 불복 기간을 결론에 |
| `statute` | 요건·위임체계 분석 | T1 → T3 → T4 → T5 → T6 |
| `precedent` | 선례 조사 | T2 → T4 → T5 → T6 |
| `verify` | 이미 있는 문서의 인용 감사(형식 검사 없음) | T5 → T6 (`init --mode verify --draft 파일`) |

**간단한 질문**(조문 하나 확인, 사건번호 확인)은 작업판 없이 `legal.py article` / `legal.py precedent`로 답하고, 답변 초안을
`python3 scripts/legal.py verify -` 에 넣어 PASS 를 확인한 뒤 보낸다.

## 3. 팀 (6개 역할)

| 역할 | 코드 | 쓰는 파일 | 핵심 책임 |
|---|---|---|---|
| 수석 법률 자문관 | `lead_counsel` | (작업판 조작만) | 사실·쟁점 정리, 모드·기준일 결정, 서브에이전트 배정, 최종 발행 |
| 실정법·위임법령 조사관 | `statute_analyst` | `evidence_statute.json` | 법률·시행령·시행규칙·행정규칙·자치법규, 시행일·삭제·개정 확인 |
| 판례·유권해석 조사관 | `precedent_analyst` | `evidence_precedent.json` | 대법원·하급심·헌재·법제처/부처 해석·위원회 결정, 요건사실·입증책임·시효 |
| 반대논리·리스크 감사관 | `risk_advocate` | `risk_memo.json` | 상대방·규제기관 논리, 벌칙·과태료·행정제재, 입증 공백 |
| 전략·의견서 집필관 | `counsel_builder` | `draft_opinion.md` | 결론 우선 FIRAC 의견서, 대응 로드맵, 증거 체크리스트 |
| 독립 감사관 | `legal_auditor` | (쓰기 없음 — `audit` 명령만) | 인용 재검증 결과 확인, 논리·입증책임·반대논리 반영 심사 |

역할별 서브에이전트 지시문·산출물 스키마: `references/roles.md`. 예시 산출물(검증 통과본): `templates/examples/`.

## 4. 실행 절차 (full)

```bash
# 1) 접수 — 수석이 사실관계·쟁점·기준일을 정리한 뒤
python3 scripts/legal_swarm.py init --title "사안명" --facts "사실관계" --issues "쟁점1" "쟁점2" --domain 노동 --as-of 2026-09-28 --mode full
# 2) 배정 — 역할별로 claim 하면 작업 패킷(JSON: 사실·쟁점·입력·출력 경로·수락기준·token)이 나온다
python3 scripts/legal_swarm.py claim --case-id <ID> --role statute_analyst --worker statute-1
python3 scripts/legal_swarm.py claim --case-id <ID> --role precedent_analyst --worker precedent-1
# 3) 각 역할은 산출물을 쓴 뒤 제출 — 게이트가 인용을 원문과 대조해 통과해야 done
python3 scripts/legal_swarm.py submit --case-id <ID> --task-id T1_STATUTE --token <token>
# 4) 감사관: 시스템 재검증 + 판단 → approve(검증 PASS 일 때만 가능) 또는 revise(최대 2회, 초과 시 escalated)
python3 scripts/legal_swarm.py audit --case-id <ID> --token <token> --verdict revise --issues "반대논리 미반영" "…"
# 5) 발행 — 승인된 초안(SHA-256 고정)에 검증 보고서·감사 기록·기준일·한계를 시스템이 붙인다
python3 scripts/legal_swarm.py finalize --case-id <ID> --token <token>
python3 scripts/legal_swarm.py board --case-id <ID>
```

- **병렬**: T1·T2 는 의존성이 없으니 동시에 배정한다. Claude Code 에서는 Agent 도구로 역할별 서브에이전트를 **한 메시지에서 함께** 띄우고,
  각자에게 claim 패킷 전체와 `references/roles.md`의 해당 역할 지시문을 준다. 서브에이전트가 없으면 `init --single-agent` 로 사안을 만들고
  수석이 역할을 순서대로 수행하되 반드시 claim/submit 을 거친다(게이트는 동일하게 강제되고, 최종본에 '단일 에이전트 실행'이 공개된다).
- **감사 독립성**: 감사관은 T1~T4 를 맡지 않은 **새 서브에이전트**다(작업판이 같은 작업자 이름의 감사를 거부한다). 초안을 고치지 않고,
  문제는 `--issues`로 적어 반려하며, 집필관이 `revision_feedback`을 받아 고친다.
- **게이트 실패 시**: submit 출력의 검증 보고서에서 ❌ 항목을 원문으로 다시 확인해 고친 뒤 같은 token 으로 다시 submit 한다.
  무엇이 강제되는지: `references/gates.md`.
- **발행 후**: `final_legal_opinion.md` 경로와 결론 요약, 검증 판정(PASS/INCOMPLETE), 사용자가 확인할 한계를 알린다.

## 5. 명령 요약

```bash
python3 scripts/legal.py law "개인정보 보호법"                          # 정식명·판본·시행일
python3 scripts/legal.py article 근로기준법 60 --hang 9 --as-of 2026-09-28   # 조문 + 시행 판정
python3 scripts/legal.py search "직장 내 괴롭힘" [--law 근로기준법]       # 법령 본문 검색(조문 단위)
python3 scripts/legal.py delegated "개인정보 보호법" 15 [--api]           # 시행령·시행규칙 연계(+DRF 3단비교)
python3 scripts/legal.py precedent 2021다219529                         # 판례 메타·판시사항·판결요지
python3 scripts/legal.py constitutional 2013헌마576                     # 헌재(DRF)
python3 scripts/legal.py interpretation "개인정보" [--target moelCgmExpc] # 법제처·부처 해석(DRF)
python3 scripts/legal.py decision ppc "CCTV"                            # 위원회 결정문(DRF)
python3 scripts/legal.py research --keywords 통상임금 재직조건            # 후보 출처 수집(분석 없음)
python3 scripts/legal.py verify draft.md [--as-of D] [--evidence …]     # 인용 검증: 0=PASS 1=FAIL 2=INCOMPLETE 3=입력 오류
```

## 6. 산출물

- 의견서 틀: `templates/legal_opinion.md` (결론·즉시 행동 1개 → FIRAC 5단 → 대응 로드맵 → 증거 체크리스트)
- 작성 요령·가능성 등급 기준: `references/firac_guide.md`
- 검증 판정: `PASS`(전부 확인) · `FAIL`(불일치·미확인·특정불가 존재) · `INCOMPLETE`(출처 부재로 일부 미검증) · `NO_CITATIONS`(통과 아님)

## 참고 문서

- `references/sources.md` — 출처 설치·최신성·한계, OC 설정
- `references/citation_rules.md` — 인용 형식, 검증 상태별 의미와 고치는 법
- `references/roles.md` — 역할별 지시문, 산출물 JSON 스키마
- `references/gates.md` — 코드로 강제되는 게이트
- `references/api_targets.md` — DRF target 표(출처 표기)
- `docs/REVIEW-2026-09-28.md` — v1 두 스킬 적대적 검토 결과와 v2 수정 내역
