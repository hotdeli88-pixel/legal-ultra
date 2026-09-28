# legal-ultra: 대한민국 법률자문 에이전트팀 스웜 오케스트레이션

`legal-ultra`는 `legalize-kr`(대한민국 3,000+개 법률·시행령·시행규칙 전수 Git/Markdown 미러)을 기반으로, 외부 API 종속 없이 100% 로컬 오프라인에서 밀리초 단위로 법률 조문을 검색·슬라이싱하고 5대 전문 역할군 스웜을 통해 무결점 법률 자문 의견서를 생성하는 스웜 오케스트레이션 엔진입니다.

---

## 🏛️ 5대 전문 역할군 (The Legal Swarm)

| 역할 (Role) | 코드명 | 권한 | 핵심 책임 |
|---|---|---|---|
| **수석 법률 지휘관** | `lead_counsel` | 총괄 조율 | 사실관계 청취, 쟁점 분해, 법률 영역 매핑, 최종 자문서 통합 |
| **실정법 전수 조사관** | `statute_analyst` | 쓰기(statute) | `legal_engine.py` 기반 법률·시행령·시행규칙 조문 원문 적시, 경과조치/부칙 대조 |
| **판례·법리 연구관** | `precedent_analyst` | 쓰기(precedent) | 대법원 전합/리딩 판례 추출, 요건사실론 분석, 입증책임 소재 판정 |
| **전략·의견서 집필관** | `counsel_builder` | 쓰기(opinion) | 의뢰인 맞춤 공격/방어 시나리오, 승소 확률 평가, FIRAC 초안 집필 |
| **독립 법률 감사관** | `legal_auditor` | **읽기 전용 (No Write)** | 조문 오인용 0건 감사, 판례 왜곡 감사, 논리적 모순 블라인드 심사 |

---

## ⚡ 빠른 시작 (CLI)

```powershell
# 1. 특정 법률 조문 즉시 조회
python scripts/legal_engine.py get "근로기준법" "76조의2"
python scripts/legal_engine.py get "민법" "750"
python scripts/legal_engine.py get "주택임대차보호법" "6조의3"

# 2. 전체 법률 키워드 초고속 검색
python scripts/legal_engine.py search "직장 내 괴롭힘"
python scripts/legal_engine.py search "임금체불" "근로기준법" 5

# 3. 위임 규정(시행령/규칙) 연계 탐색
python scripts/legal_engine.py delegated "근로기준법" "76조의2"

# 4. 법률 개정 히스토리 조회
python scripts/legal_engine.py history "민법"

# 5. 스웜 작업판 초기화 및 태스크 실행
python scripts/legal_swarm.py init --case-id "CASE-001" --title "사안명" --facts "사실관계" --issues "쟁점1" "쟁점2"
python scripts/legal_swarm.py board
```

---

## 7대 불변 원칙 (Seven Inviolable Legal Principles)

1. **무외비·로컬 100% 원칙**: 외부 유료 API 없이 로컬 legalize-kr 미러 기반 조문 원문 슬라이싱.
2. **배타적 파일 소유권**: 조사관, 연구관, 집필관, 감사관 간 파일 쓰기 영역 엄격 분리.
3. **단일 진실 공급원 (Blackboard)**: SQLite 기반 작업판(`legal_blackboard.db`) 단독 통제.
4. **환각 0% 엄격 수락 게이트**: 실제 법조문 원문, 공포번호, 시행일자 물리적 대조 필수.
5. **독립 법률 감사관 분리**: 파일 쓰기 권한이 박탈된 `legal_auditor`의 블라인드 감사 통과 필수.
6. **유한 재작업 예산**: 결함 발생 시 최대 2회 리비전(총 시도 3회) 내 완결.
7. **ADHD-Friendly & FIRAC 표준 구조**: 최상단 즉시 실행 행동 1개 배치 및 표준 5단 구조(Facts - Issues - Rules - Application - Conclusion).

---

## 📄 License
MIT License
