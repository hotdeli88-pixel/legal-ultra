# 출처: 설치 · 최신성 · 한계

## 1. legalize-kr — 법령 Git 미러

- 저장소: <https://github.com/legalize-kr/legalize-kr> — 법령마다 `kr/{법령명(공백 제거)}/{법률|시행령|시행규칙|…}.md`, 개정 1건 = 커밋 1개(공포일 기준).
- 규모(2026-09-23 HEAD 실측): 법령 디렉터리 3,050개, 파일 5,757개.
- 설치: `python3 scripts/legal.py setup` → **전체 이력 + 지연 blob** 부분 클론(`git clone --filter=blob:none --no-checkout` → `checkout`)과
  경로별 이력 색인(`git commit-graph write --reachable --changed-paths`). 실측: 커밋 104,984개 이력 196MB(25초) + 현행 본문 체크아웃(31초),
  합계 약 540MB. 색인 생성 24초(없으면 첫 검증 때 자동 생성). 과거 판본 본문은 기준일 판정에 필요할 때 한 파일씩 내려받는다(네트워크 필요,
  한 번 받으면 로컬에 남음). 색인이 있으면 법령 하나의 판본 목록 조회가 11초 → 0.3초.
- 빠른 설치 `setup --shallow`(`--depth 1`, 최신본만)는 판본 이력이 없어 기준일 판정이 **개정표시 기준 추정**이 되고, 최신 공포본이
  기준일에 시행 중이며 먼저 공포된 개정도 모두 시행된 법령만 판정한다 — 나머지는 `UNVERIFIABLE`(`references/citation_rules.md` 4.2).
  최신 공포본이 아직 시행 전인 법령(예: 근로기준법)은 이 경우가 많다.
- 환경변수: `LEGALIZE_KR_PATH=<클론 경로>`(저장소 루트 또는 `kr/` 둘 다 가능).
- `legal.py status` 가 판본 이력(전체/일부/없음)과 색인 여부를 보여 준다.
- 주의
  - HEAD 는 **최신 공포본**이라 시행예정 판본일 수 있다(예: 근로기준법 2026-06-09 공포·2027-06-10 시행). 검증기가 기준일로 판정한다.
  - 같은 디렉터리에 다른 법령ID 의 동명 파일이 있을 수 있다(예: `근로기준법/법률.md` 는 1997년 폐지 법률, 현행은 `법률(법률).md`).
    엔진은 제목이 같은 후보 중 공포일자가 가장 늦은 것을 고른다.
  - 약칭 디렉터리는 없다. 약칭은 DRF(`법령약칭명`)가 있을 때만 정식명으로 풀린다.
  - 저장소는 파이프라인 개선 시 force-push 될 수 있다(README 공지) → `git fetch --all && git reset --hard origin/main`.
  - legalize-kr 은 국가법령정보센터 OpenAPI 에서 가져온 데이터다. 최종 확인은 국가법령정보센터 원문으로 한다.

## 2. precedent-kr — 판례 Git 미러

- 저장소: <https://github.com/legalize-kr/precedent-kr> — `{사건종류}/{대법원|하급심}/{법원명}_{선고일자}_{사건번호}.md`.
- 규모(실측): 파일 124,980개(대법원 68,699 · 하급심 56,280), 색인되는 사건번호 약 12.9만 개(병합 포함), 최신 선고 2026-07-16.
- 설치: 기본은 **파일명 색인만** 받는 부분 클론(`--filter=blob:none --no-checkout`) — 사건번호·법원·선고일 검증은 완전 오프라인, 판시 본문은
  필요할 때 `git show` 로 한 건씩 내려받는다(이때만 네트워크 필요). 본문 전체를 받으려면 `setup --precedent-bodies`(수 GB).
- 오프라인에서 본문이 필요한 검증(판시 인용문 대조, '전원합의체' 표기 확인, 파일명 부번호로만 잡힌 병합 사건번호 확인)은 본문을 못 읽으면
  `UNVERIFIABLE` 다(통과시키지 않는다). 자주 인용하는 판결은 한 번 온라인에서 검증해 두면 로컬에 남는다.
- 환경변수: `PRECEDENT_KR_PATH=<클론 경로>`.
- 한계: 헌법재판소 결정은 없다(→ DRF `detc`). 공개되지 않은 판결은 없다 → "미러에 없음"은 "존재하지 않음"과 같지 않다.
  그래서 검증기는 이를 `NOT_FOUND`(공식 출처에서 확인 불가)로 표시하고 **원문 확보 전 인용 금지**를 안내한다.
  미러의 최신 선고일(색인 기준)보다 **뒤**에 선고된 판결만 `UNVERIFIABLE`(미러에 아직 없을 수 있음)로 처리한다 — 그 이전 선고일인데 없으면 `NOT_FOUND`.

## 3. 법제처 국가법령정보 공동활용 OPEN API (DRF)

- 목록 `https://www.law.go.kr/DRF/lawSearch.do`, 본문 `…/lawService.do`, 인증 `OC`.
- `OC`: <https://open.law.go.kr> 에서 OPEN API 신청 시 정하는 식별자(가입 이메일 @ 앞부분). `LAW_OPENAPI_OC` 로 설정.
  설정하지 않으면 공용 시험계정 `test` 를 쓴다(제3자 실측상 동작했으나 공용이므로 안정성을 보장하지 않는다).
- 쓰임: 헌재 결정(`detc`), 법제처 해석(`expc`), 부처 1차 해석 39종(`…CgmExpc`), 위원회 결정문 12종, 특별행정심판, 행정규칙(`admrul`),
  자치법규(`ordin`), 3단비교(`thdCmp`), 약칭 해석, 조문별 시행일자(`조문시행일자`), 미러 교차확인(`verify --cross-check`).
- target 별 응답 태그는 이름에서 유추할 수 없다 → `references/api_targets.md`. 사용자 환경에서 `python3 scripts/legal.py api-probe` 로 확인.
- 이 API 의 모든 실패는 HTTP 200 으로 온다(빈 본문=없는 target, `<Response>…검증에 실패…` = OC 문제, `<Law>…없습니다</Law>` = 없음).
  클라이언트는 이를 구분해 "접속 불가"를 "존재하지 않음"으로 바꾸지 않는다.
- 오프라인 강제: `LEGAL_ULTRA_OFFLINE=1` 또는 `legal.py --no-api …`.

## 4. 캐시

- `$LEGAL_ULTRA_CACHE`(기본 `~/.cache/legal-ultra`): DRF 응답(OC 제외 키, 성공 응답만, 법령 1일·판례/해석 7일), 판례 색인(HEAD 별), 법령 제목 색인.
- 최신성이 중요하면 캐시 디렉터리를 지우거나 `LEGAL_ULTRA_CACHE` 를 새 경로로 지정한다.

## 5. 선택 대안

- [`legalize-cli`](https://github.com/legalize-kr/cli-tools)(`pip install legalize-cli`): 클론 없이 GitHub REST API 로 같은 데이터를 조회(`--date` 시행일 기준 조회 지원).
  미인증 시간당 60회 제한이 있어 이 스킬은 로컬 git 미러를 기본으로 쓴다.
