# 법제처 DRF target 표

> **출처**: [rubatoyd/law-openapi-mcp](https://github.com/rubatoyd/law-openapi-mcp)(MIT) `docs/LAW_API_GUIDE.md`·`docs/DRF_CATALOG.md`·`src/law_mcp/config.py` —
> 2026-09-08 open.law.go.kr 안내 195건 판독 + 라이브 왕복 실측. 이 스킬 작성 환경에서는 law.go.kr 이 네트워크 정책으로 차단되어
> 재측정하지 못했다. 사용자 환경에서 `python3 scripts/legal.py api-probe` 로 확인하라. 코드 레지스트리: `scripts/law_api.py` `TARGETS`.

## 1. 공통

- 목록 `lawSearch.do` · 본문 `lawService.do` · 파라미터 `OC`, `target`, `type=XML`, `query`, `display`(문서상 최대 100, 실측 500), `page`, `search`(1=제목, 2=본문 포함)
- `query` 를 빼면 오류가 아니라 **전체 카탈로그**가 온다 → 클라이언트는 빈 검색어를 거부한다.
- 판례 사건번호는 `nb` 파라미터로 찾는다(`query` 는 사건명 검색).

## 2. 주요 target

| target | 자료 | 목록 루트 / 레코드 | 식별자 → 본문 파라미터 | 본문 루트 |
|---|---|---|---|---|
| `law` | 현행법령(공포일) | `LawSearch` / `law` | 법령일련번호 → `MST` | `법령` |
| `eflaw` | 시행일법령(시행예정·현행·연혁 판본) | `LawSearch` / `law` | 법령일련번호 → `MST` **+ `efYd`** | `법령` |
| `prec` | 판례 | `PrecSearch` / `prec` | 판례일련번호 → `ID` | `PrecService` |
| `detc` | 헌재 결정례 | `DetcSearch` / **`Detc`** | 헌재결정례일련번호 → `ID` | `DetcService` |
| `expc` | 법제처 법령해석례 | **`Expc`** / `expc` | 법령해석례일련번호 → `ID` | `ExpcService` |
| `admrul` | 행정규칙(훈령·예규·고시) | `AdmRulSearch` / `admrul` | 행정규칙일련번호 → `ID` | `AdmRulService` |
| `ordin` | 자치법규 | `OrdinSearch` / **`law`** | 자치법규일련번호 → **`MST`** | **`LawService`** |
| `decc` | 행정심판례 | `Decc` / `decc` | → `ID` | **`PrecService`** |
| `thdCmp` | 3단비교(`knd` 1=인용, 2=위임) | — | `MST` | JSON `LspttnThdCmpLawXService` |

굵게 표시한 것이 v1 `law` 스킬이 틀렸던 곳이다(자치법규·헌재 검색이 항상 0건이었음).

## 3. 위원회 결정문 12종 — 목록 루트 `Ppc`·`Ftc`…, 레코드 = 코드, 본문 `…Service`, 식별자 `결정문일련번호`

`ppc` 개인정보보호위 · `eiac` 고용보험심사위 · `ftc` 공정거래위 · `acr` 국민권익위 · `fsc` 금융위 · `nlrc` 노동위 ·
`kcc` 방송미디어통신위 · `iaciac` 산재보험재심사위 · `oclt` 중앙토지수용위 · `ecc` 중앙환경분쟁조정위 · `sfc` 증권선물위 · `nhrck` 국가인권위

## 4. 특별행정심판 — 목록 `Decc`/`decc`, 본문 `SpecialDeccService`

`ttSpecialDecc` 조세심판원 · `kmstSpecialDecc` 해양안전심판원 · `acrSpecialDecc` 국민권익위 · `adapSpecialDecc` 인사혁신처 소청심사위
(v1 문서의 `specialDeccTt`·`specialDeccAdap` 는 순서가 뒤집힌 코드)

## 5. 부처 1차 법령해석 39종 — `<부처>CgmExpc`, 목록 `CgmExpc`/`cgmExpc`, 본문 `CgmExpcService`

v1 문서의 `cgmExpcMoel` 식 코드는 **순서가 뒤집혀** 있고(실제 `moelCgmExpc`), 부처 수도 30개가 아니라 39개다.
`moefCgmExpc`(재정경제부)·`ntsCgmExpc`(국세청)는 목록만 있고 본문 조회가 없다.

moe 교육부 · moel 고용노동부 · molit 국토교통부 · moef 재정경제부 · mof 해양수산부 · mois 행정안전부 · me 기후에너지환경부 · kcs 관세청 ·
nts 국세청 · msit 과학기술정보통신부 · mpva 국가보훈부 · mnd 국방부 · mafra 농림축산식품부 · mcst 문화체육관광부 · moj 법무부 ·
mohw 보건복지부 · motie 산업통상부 · mogef 성평등가족부 · mofa 외교부 · mss 중소벤처기업부 · mou 통일부 · moleg 법제처 ·
mfds 식품의약품안전처 · mpm 인사혁신처 · kma 기상청 · khs 국가유산청 · rda 농촌진흥청 · npa 경찰청 · dapa 방위사업청 · mma 병무청 ·
kfs 산림청 · nfa 소방청 · oka 재외동포청 · pps 조달청 · kdca 질병관리청 · kostat 국가데이터처 · kipo 지식재산처 · kcg 해양경찰청 ·
naacc 행정중심복합도시건설청

## 6. 응답 판별 (모두 HTTP 200)

| 응답 | 뜻 | 클라이언트 처리 |
|---|---|---|
| 0바이트 | 없는 target 등 | `ApiError` (결과 0건으로 취급하지 않음) |
| HTML | 파라미터 부족(예: eflaw 본문에 efYd 없음)·XML 미지원 target | `ApiError` |
| `<Response><result>사용자 정보 검증에 실패…` | OC 미등록·IP 불일치 | `ApiAuthError` → 검증 상태 `UNVERIFIABLE` |
| `<Law>일치하는 …이 없습니다</Law>` | 해당 식별자 없음(target 무관하게 루트가 `Law`) | `None`(없음) |
| 네트워크 오류·프록시 403 | 접속 불가 | `ApiUnavailable` → `UNVERIFIABLE`, 같은 실행에서 재시도 폭주 없이 즉시 실패 |
