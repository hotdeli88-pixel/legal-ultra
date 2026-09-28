# DRF(법제처 국가법령정보 OPEN API) 응답 픽스처

- `list_*.xml`, `body_*.xml`, `LICENSE-law-openapi-mcp`: [rubatoyd/law-openapi-mcp](https://github.com/rubatoyd/law-openapi-mcp)
  `tests/fixtures/` 의 실제 응답(2026-09-08 라이브 캡처, OC 는 `__OC__` 로 가려짐)을 MIT 라이선스에 따라 그대로 복사했다.
- `auth_fail_synthetic.xml`: 같은 저장소 `docs/LAW_API_GUIDE.md` §4 에 기록된 "잘못된 OC" 응답 형식을 본떠 **직접 만든 합성 파일**이다.

이 스킬 작성 환경에서는 law.go.kr 이 네트워크 정책으로 차단되어 직접 캡처하지 못했다.
사용자 환경에서 `python scripts/legal.py api-probe` 로 레지스트리를 재확인할 수 있다.
