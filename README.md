# legal-ultra v2 — 대한민국 법률 검토 에이전트 팀 스킬 (law + legal-ultra 통합)

두 스킬을 하나로 합쳤다.

- **law (v1)**: 법제처 국가법령정보 OPEN API 기반 5인 에이전트 팀 + 인용 검증기
- **legal-ultra (v1)**: legalize-kr 로컬 법령 미러 + SQLite 작업판 스웜

v2 는 **로컬 미러(legalize-kr 법령·precedent-kr 판례)를 먼저, 법제처 DRF API 를 보완·교차확인용으로** 쓰고,
모든 인용을 스크립트가 원문과 대조해 통과해야만 의견서를 발행한다. 두 v1 스킬의 적대적 검토 결과와 수정 내역은
[`docs/REVIEW-2026-09-28.md`](docs/REVIEW-2026-09-28.md).

## 무엇이 달라졌나 (요약)

| 영역 | v1 | v2 |
|---|---|---|
| 인용 검증 | `제76조의9` 를 `제76조`로 검증, 사건번호가 달라도 첫 검색결과로 통과, 나열 인용 누락, 인용 0건이면 PASS | 조·항·호·목·제목·인용문·시행일·선고일·법원까지 대조, 5단계 상태, fail-closed |
| 시행일 | 시행예정 판본을 현행으로 제시 | 기준일(as-of) 판정 — 미시행 신설 조항은 불일치로 표시 |
| 법령 파일 선택 | 디렉터리 순서에 따라 1997년 폐지 근로기준법을 읽을 수 있음 | 제목·공포일자로 결정론적 선택 |
| 판례 | 로컬 판례 출처 없음 / API 사건번호 검색 오류 | precedent-kr 12.9만 사건번호 오프라인 색인 + DRF `nb` 검색 |
| API | 부처 해석·특별행정심판 target 코드 뒤집힘, 자치법규·헌재 항상 0건, 비JSON 응답에서 크래시 | 실측 레지스트리(72종), 오류 유형 구분(접속 불가 ≠ 없음) |
| 작업판 | revise 후 교착, revise 여도 최종본 열림, 사안 간 태스크 덮어쓰기 | 리비전 루프·예산·승인 게이트 코드로 강제, 사안 격리 |
| "5인 팀" 자문서 | 키워드 검색 + 고정 문구(모든 사안 "리스크 중등도~고도") | 추론은 에이전트(역할별 서브에이전트), 사실 확인은 도구 |

## 설치

```bash
# Claude Code (사용자 전역)
git clone https://github.com/hotdeli88-pixel/legal-ultra ~/.claude/skills/legal-ultra
# 또는 프로젝트 단위: <project>/.claude/skills/legal-ultra

# 데이터 출처(권장): 법령·판례 미러 설치 + 환경변수 안내 출력
python3 ~/.claude/skills/legal-ultra/scripts/legal.py setup            # --full-history: 시행일 정밀 판정
export LEGALIZE_KR_PATH=~/legalize-kr PRECEDENT_KR_PATH=~/precedent-kr
export LAW_OPENAPI_OC=<open.law.go.kr 에서 신청한 OC>                     # 선택(없으면 공용 'test')

python3 ~/.claude/skills/legal-ultra/scripts/legal.py status            # 출처 확인
```

요구사항: Python 3.10+(표준 라이브러리만), git. 이전 `law` 스킬을 함께 설치해 두었다면 제거한다(같은 요청에서 두 스킬이 경합한다).

## 빠른 사용

```bash
python3 scripts/legal.py article 근로기준법 76조의2                      # 조문 + 시행 판정
python3 scripts/legal.py precedent 2021다219529                         # 판례 메타·판시사항
python3 scripts/legal.py verify 의견서.md --as-of 2026-09-28             # 인용 검증 (0=PASS 1=FAIL 2=INCOMPLETE)
python3 scripts/legal_swarm.py init --title "…" --facts "…" --issues "…" --mode full   # 팀 작업판
```

에이전트용 전체 절차는 [`SKILL.md`](SKILL.md), 역할 지시문은 [`references/roles.md`](references/roles.md).

## 구조

```
SKILL.md                      스킬 본문(에이전트가 읽음)
scripts/
  legal.py                    통합 CLI(조사·검증)
  legal_swarm.py              작업판: 사안별 DAG·역할·게이트·리비전 예산
  citations.py                인용 추출·다중 출처 검증·보고서
  legal_engine.py             legalize-kr 법령 엔진(조·항·호·목, 시행일 판정, 위임, 검색) — v1 CLI 호환
  precedent_engine.py         precedent-kr 판례 엔진(사건번호 색인, 본문 지연 로드)
  law_api.py                  법제처 DRF API 클라이언트(실측 레지스트리)
  kr_common.py                공용 유틸(정규화·프론트매터·날짜·출력 인코딩)
references/                   출처·인용 규칙·역할·게이트·API target·FIRAC
templates/                    의견서 틀, 작업 계획, 검증 통과 예시 산출물(examples/)
tests/                        단위·통합 테스트(실데이터 발췌 픽스처, DRF 실측 응답 픽스처)
docs/REVIEW-2026-09-28.md     v1 적대적 검토 보고서
```

## 테스트

```bash
python3 -m unittest discover -s tests        # 네트워크 없이 실행(84개)
```

## 한계

- 검증기는 인용의 **실재와 속성**을 보증할 뿐 법리 판단의 타당성을 보증하지 않는다.
- 이 버전 작성 환경에서는 law.go.kr 이 차단되어 DRF 경로는 제3자 실측 응답 픽스처와 모의 응답으로만 시험했다. 사용 전 `legal.py api-probe` 를 권장한다.
- 전체 이력 없는 미러에서의 시행일 판정은 개정 표시에 기댄 추정이다(보고서에 '추정'으로 표시).
- 본 스킬의 산출물은 법률 정보 분석이며 변호사의 법률자문을 대체하지 않는다.

## License

MIT (legalize-kr 법령 원문은 공공저작물, 구조·메타데이터 MIT. `tests/fixtures/drf/` 는 rubatoyd/law-openapi-mcp MIT 픽스처)
