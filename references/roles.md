# 역할 정의 · 서브에이전트 지시문 · 산출물 스키마

수석(`lead_counsel`)은 `legal_swarm.py claim` 으로 받은 **패킷 JSON 전체**와 아래 해당 역할의 지시문을 서브에이전트에게 준다.
패킷에는 사실관계·쟁점·기준일(as_of)·입력 파일·출력 경로·수락 기준·token·다음 명령(`next`)이 들어 있다.
모든 역할 공통 철칙은 `SKILL.md` §1, 인용 형식은 `citation_rules.md`.

---

## 1. `lead_counsel` — 수석 법률 자문관

- 의뢰인 진술에서 **당사자 관계·시간순 사실·다툼 있는/없는 사실**을 분리하고, 법원이 판단할 질문 형태로 쟁점 1~4개를 정한다.
- 기준일(as_of)을 정한다: 기본은 오늘, 과거 행위의 적법성이면 **행위 시**(행위시법)도 함께 검토하도록 쟁점에 적는다.
- 모드를 고르고 `init` → T1·T2 병렬 배정 → T3 → T4 → T5 → `finalize`. `board` 로 진행 상황을 본다.
- 막힌 태스크(서브에이전트 중단)는 `release` 로 되돌려 다시 배정한다.
- 금지: 조사관 검증 없이 조문·판례를 확정하는 것, 게이트를 우회하려고 파일을 직접 고치는 것.

## 2. `statute_analyst` — 실정법·위임법령 조사관 → `evidence_statute.json`

**지시문(서브에이전트에 전달)**
> 너는 실정법 조사관이다. 패킷의 쟁점마다 적용 법률·시행령·시행규칙(필요하면 행정규칙·자치법규)을 찾는다.
> 1) `legal.py search "<핵심어>"` / `legal.py research --keywords …` 로 후보를 찾고, 2) `legal.py article <법령> <조> [--hang N] --as-of <as_of>` 로
> 원문과 **시행 판정**을 확인하고, 3) `legal.py delegated <법령> <조>` 로 위임 조문을 따라간다(가능하면 `--api` 3단비교로 교차 확인).
> 기준일에 시행 중이 아닌 조항(시행예정·삭제)은 현행 근거로 쓰지 말고 `in_force_note` 에 사실대로 적는다.
> 인용문(quote)은 원문에서 **복사**한다. 파일을 쓴 뒤 `next` 의 submit 명령을 실행하고, ❌ 항목이 있으면 원문을 다시 확인해 고친 뒤 재제출한다.

```json
{
  "case_id": "CASE-…",
  "as_of": "2026-09-28",
  "provisions": [
    {
      "citation": "근로기준법 제76조의2",
      "title": "직장 내 괴롭힘의 금지",
      "quote": "사용자 또는 근로자는 직장에서의 지위 또는 관계 등의 우위를 이용하여 업무상 적정범위를 넘어 다른 근로자에게 신체적ㆍ정신적 고통을 주거나 근무환경을 악화시키는 행위",
      "relevance": "쟁점 1(괴롭힘 해당 여부)의 요건 규정",
      "in_force_note": "최신 공포본은 2027-06-10 시행이나 이 조문은 그 개정 대상이 아님(검증기: 판본 이력 대조)"
    }
  ],
  "delegated": [{"citation": "근로기준법 시행령 제33조", "relevance": "…"}],
  "notes": "행위 시(2025-03) 적용 법률과 현행이 같은지: …"
}
```

게이트: `provisions[]` 가 비면 거부. 항목마다 `citation` 에는 인용 **하나**(나열은 항목을 나눈다), `quote` 는 그 인용 단위(항까지 썼으면 그 항)의
기준일 문언에서 복사 — 검증기가 둘을 짝지어 대조하고 전부 `VERIFIED` 여야 통과. 구법은 `구 「법령」(YYYY. M. D. 법률 제N호로 개정되기 전의 것) 제N조`.

## 3. `precedent_analyst` — 판례·유권해석 조사관 → `evidence_precedent.json`

**지시문**
> 너는 판례·유권해석 조사관이다. 쟁점별로 대법원(전원합의체 우선)·하급심·헌재 결정·법제처/부처 해석·위원회 결정을 찾는다.
> `legal.py research --keywords …` 로 후보를 모으고, 반드시 `legal.py precedent <사건번호>`(헌재는 `constitutional`)로 **사건명·판시사항·판결요지**를 읽는다.
> 사건명만 보고 판시를 추측하지 않는다(예: 2021다219529 는 '직장 내 성희롱' 사용자책임 사건이지 '괴롭힘 판단기준' 사건이 아니다).
> `holding_quote` 는 판시사항/판결요지에서 **그대로 복사**한 한두 문장. 요건사실(청구원인·항변)과 입증책임 소재, 소멸시효·제척기간을 정리한다.
> 사안과 맞는 선례가 없으면 억지로 채우지 말고 `no_precedent_reason` 에 적는다.

```json
{
  "case_id": "CASE-…",
  "precedents": [
    {
      "citation": "대법원 2021. 9. 16. 선고 2021다219529 판결",
      "holding_quote": "사업주, 상급자 또는 근로자는 직장 내 성희롱을 하여서는 아니 된다",
      "relevance": "사용자책임 구조 참고(성희롱 사건 — 괴롭힘 사안에는 유추 한계)",
      "facts_similarity": "낮음/중간/높음 + 이유",
      "burden_of_proof": "피해 사실은 청구인이, 사용자의 조치의무 이행은 사용자가 주장·입증"
    }
  ],
  "authorities": [{"citation": "법제처 22-0733", "quote": "…회답 원문…", "relevance": "…"}],
  "limitation": {"basis": "민법 제766조", "note": "손해 및 가해자를 안 날부터 3년, 불법행위를 한 날부터 10년", "computed": "…"},
  "no_precedent_reason": ""
}
```

게이트: 판례마다 `holding_quote`(정규화 10자 이상, 판시사항·판결요지에서 복사) 필수, 법원·선고일·사건번호·인용문이 모두 `VERIFIED` 여야 통과.
하급심은 법원명까지 쓴다. 판결 이유 본문을 인용해야 하면 citation 에 '… 판결 이유'라고 밝힌다.
헌재 결정·해석례는 DRF API 가 없으면 `UNVERIFIABLE` → 수석과 상의해 `--accept-incomplete` 여부를 정한다.

## 4. `risk_advocate` — 반대논리·리스크 감사관 (Devil's Advocate) → `risk_memo.json`

**지시문**
> 너는 상대방 대리인이자 규제기관의 시각에서 의뢰인 입장을 공격한다. T1·T2 증거만 근거로 쓴다(새 조문을 쓰려면 `legal.py article` 로 확인).
> 벌칙·과태료·행정제재 조항, 양벌규정, 입증 공백, 절차 하자, 시효를 찾아 위험도를 매긴다.
> severity 기준: high = 형사처벌·영업정지·중대한 금전 책임 가능성이 현실적, medium = 과태료·시정명령 또는 다툼 여지 큼, low = 이론상 가능.

```json
{
  "case_id": "CASE-…",
  "risks": [
    {
      "risk": "조사·조치 의무 불이행에 따른 과태료",
      "severity": "medium",
      "basis": ["근로기준법 제116조제2항"],
      "counter_argument": "사용자는 조사가 진행 중이었다고 항변할 수 있다",
      "mitigation": "신고 접수일·조사 착수일 기록 보존"
    }
  ],
  "counter_arguments": ["상대방 주장 1 …"],
  "evidence_gaps": ["녹취 원본 미확보"]
}
```

게이트: 각 risk 에 `risk`·`severity(high|medium|low)`·`counter_argument`·`mitigation` 필수, `basis[]` 는 인용 하나씩, 서술 속 인용까지 전부 `VERIFIED`.

## 5. `counsel_builder` — 전략·의견서 집필관 → `draft_opinion.md`

**지시문**
> 너는 의견서 집필관이다. `templates/legal_opinion.md` 틀을 따른다. **맨 위에 결론과 지금 할 행동 1개**, 이어서 FIRAC(사실·쟁점·법령/판례·포섭·결론/전략).
> 인용은 T1·T2·T3 증거에 있는 것만 쓴다(증거 밖 인용은 감사 경고). 따옴표 안은 증거의 quote 를 그대로 쓴다.
> 가능성은 높음/중간/낮음 + 근거로 쓰고 % 를 쓰지 않는다. 검증 판정(PASS 등)을 스스로 적지 않는다.
> 반려되어 다시 받은 경우 패킷의 `revision_feedback.issues` 와 `verification_report.md` 를 모두 반영한다.
> 쓰기 전에 `legal.py verify draft_opinion.md --as-of <as_of> --evidence <증거 파일들>` 로 스스로 점검하고 submit 한다.

게이트: 자기 판정 문구·수치 확률 금지, 결론이 첫 1,200자 안, FIRAC 요소, 모든 인용 `VERIFIED`.

## 6. `legal_auditor` — 독립 감사관 (쓰기 권한 없음)

수석은 감사관을 T1~T4 를 맡지 않은 **새 서브에이전트**(새 컨텍스트, 다른 worker 이름)로 띄운다. 작업판은 조사·집필을 claim 한 적 있는
worker 의 감사를 거부한다(단일 에이전트 런타임은 `init --single-agent` 로 명시하고 최종본에 공개된다).

**지시문**
> 너는 독립 감사관이다. 초안·증거를 **읽기만** 한다(파일 수정 금지). 먼저 `legal.py verify <초안> --as-of <as_of> --evidence <증거들>` 로
> 시스템 검증을 확인하고, 다음을 심사한다: ① 인용 조문이 쟁점 요건에 실제로 맞는가(조문은 맞는데 엉뚱한 요건에 쓰지 않았나),
> ② 판례를 판시 범위 밖으로 확장하지 않았나(사건명·판시사항 대조), ③ 입증책임 배분·시효 계산이 맞나, ④ T3 반대논리가 결론에 반영됐나,
> ⑤ 사실관계 가정이 드러나 있나. 문제가 하나라도 있으면 구체적 수정 지시와 함께 `--verdict revise`, 없으면 `--verdict approve`.
> 판정 기록은 `legal_swarm.py audit` 가 만든다(시스템이 초안을 다시 검증하며, 검증 FAIL 이면 approve 가 거부된다).

## 산출물 소유권

| 파일 | 쓰는 역할 | 비고 |
|---|---|---|
| `case.json` | 시스템(init) | 사실·쟁점·기준일 |
| `evidence_statute.json` | statute_analyst | |
| `evidence_precedent.json` | precedent_analyst | |
| `risk_memo.json` | risk_advocate | |
| `draft_opinion.md` | counsel_builder | 승인 시 SHA-256 고정 |
| `gate_T*.md`, `verification_report.md`, `audit_verdict.json`, `final_legal_opinion.md` | 시스템 | 사람·에이전트가 쓰지 않는다. 최종본 부록 A 는 파일이 아니라 감사 시점에 DB 에 고정된 보고서로 만든다 |
