# e-KHNP Automation - Next Steps

Last updated: 2026-03-12

## 현재 스냅샷
- 핵심 자동화(로그인, 강의실 진입, 차시 처리, 수료표 파싱)까지는 구현됨.
- 종합평가는 팝업/문항 선택자 안정화는 완료, 실세션 점수/합격률 개선이 진행중.
- 로컬 LLM(RAG, Ollama)은 운영 연동 완료, 모델/검색 전략 A/B 검증이 진행중.
- 원격 실행(서버화) 및 동시 5명 제한은 운영 반영 단계로 진입:
  - 기본 워커 수 `APP_WORKER_COUNT=5` 반영
  - 5개 초과 작업 `pending` 대기열 전환 로그/검증 스크립트 추가
- 사용자/관리자 화면 분리 운영 반영:
  - 사용자 화면은 `START` 단일 진입 중심으로 단순화
  - 사용자 화면 마일스톤/상세 표 제거, 로그 스크롤/자동 업데이트 추가
  - 접근코드 입력 플레이스홀더 문구를 안내형으로 변경
- 2026-03-10 실측 현황:
  - 학습진도율 100%
  - 미완료 0개
  - 종합평가 진입은 성공(사전안내 레이어 처리 포함)
  - 수료 워크플로우 1단계 보강: 진도율/미완료가 이미 충족이면 학습창 열기 단계를 생략
  - 종합평가 자동풀이(실세션) 10/10 문항 완주 + 마지막 문항 최종제출 동작 확인
  - 최종제출 후 완료 신호 감지 및 강의실 복귀 확인
  - 제출 결과 판독: `수료점수=fail`로 미수료 처리(의도대로 중단)
  - 응시횟수 재확인: 1/5 (남은 4회, reserve=1 유지)
  - 운영 기준 업데이트: `RAG_CONF_THRESHOLD=0.62`, `RAG_PASS_SCORE=80`, `RAG_LOW_CONF_FLOOR=0.53`
  - 로컬 개인서버 용량 상한: `RAG_STORAGE_LIMIT_GB=20`
  - 선택지 클릭 검증 보강: `li.on`/`choiceAnswers` 기준으로 현재 문항 선택 여부 확인
  - 문항 전환 복구 보강: `문항 답변을 선택하지...` 경고 감지 시 재선택 후 다음 재시도
  - 정체 복구 새로고침 가드: 비시험 페이지에서만 1회 새로고침 재시도, 시험 페이지(`exampaper/eval/test/quiz`)는 새로고침 금지
  - 정답률 개선 1차: 종합평가 풀이 시 웹 검색(DDG) 결과를 매 문항 강제 참조하도록 반영
  - 정답률 개선 2차: 점수 미달 시 `item result(doExamPaperPopup)` 결과지 기반 정답 인덱싱 + 자동 재응시 루프 추가
  - 정답 인덱스 우선 적용: `rag/exam_answer_bank.json` 매칭 시 RAG보다 먼저 선택지 고정
  - 라이브 확인: 현재 계정은 결과지 `확인 3/3` 상태로 실정답 본문 추출 불가(`added=0, found=0`) 확인
  - 기능 검증: 모의 결과지 텍스트 파싱/인덱싱/재매칭(정답 고정 선택) 테스트 통과
  - 원클릭 실주행 보강: `학습 하기`(공백형) 버튼 매칭/선택자 확장 적용
  - 시간보충 안정화: `학습 차시`의 최소 차시(1차시) 직접 진입 fallback 추가
  - 시간보충 체크 주기 동적화: 부족시간 기준 3~10분 자동 결정
  - 원클릭 실주행 확인: 시간보충 팝업 진입 및 학습시간 증가(`00:05:08 -> 00:14:22`) 확인
  - 로그인 안정화 보강: 로그인 URL 오탐 방지 + `나의 학습현황` 직접 URL 폴백 + URL 기반 이동 성공 판정 추가
  - 로컬 LLM 환경 재구성: Ollama 설치/기동, `nomic-embed-text`/`qwen2.5:3b` 모델 준비, `rag/index.json` 재생성(`files=2`, `chunks=15`)
  - 원클릭 실주행(저진도 과정) 확인: 연속 차시 자동진행(예: `12/18 -> 18/18` 반복) 동작 확인, 다음 과정 자동 전환 검증은 진행중
  - 완주 테스트는 사용자 요청으로 중단(중단 직전 정상 흐름 유지)
- 2026-03-11 반영/검증:
  - 답안 인덱스 매칭 고도화:
    - 문항 정규화/시그니처(`question_match_norm`, `question_signature`) 인덱스 추가
    - 보기 순서 변경 불변 매칭(`option_set_signature`) 반영
  - 오답 반복 방지:
    - 동일 근거 연속 실패(streak>=2) 시 Negative Evidence(soft penalty) 적용 + 2순위 근거 자동 전환
  - 우회 강좌 이력 영속화:
    - `.runtime/deferred_exam_courses.json` 로드/저장 추가(재시작 후 유지)
  - 시험 결과 파싱 품질 리포트:
    - 문항별 근거/신뢰도/선택답/정답/정오 JSON 리포트 자동 저장
  - 동시 처리 부하 점검:
    - `scripts/queue_load_check.py` 실행 결과 `max_running_observed=5`, `pending_seen=true` 확인
  - 실계정 상태 확인:
    - 종합평가 응시횟수 `used=3/5, remaining=2` 감지 및 우회조건 입력값 검증
  - 큐/운영 UI 고도화:
    - 계정 ID 기반 큐 소유자 키 적용(브라우저 재접속 후 동일 ID로 기존 작업 조회)
    - 계정당 활성 작업 1개 정책 적용(중복 원클릭/중복 실행 등록 차단)
    - 관리자 계정별 실시간 큐 현황 표 + 계정 필터 + 상세 로그 조회 추가
    - 실패 작업 재시도 버튼 추가(활성 작업 존재 시 재시도 대기 안내)
    - 사용자 화면 신규 로그 강조 표시(새 로그 블록 + 최근 로그 모드)
    - 작업 종료 스냅샷(`.runtime/job_history/<job_id>.json`) 경로 표기
  - 자동화 안정화:
    - 장시간 학습시간 보충 대기를 chunk 방식으로 분할하고, 팝업 종료 감지 시 자동 재오픈
    - 시험/학습 `다음` 버튼에서 화살표(`>`, `›`, `→`, `aria-label=next`) 패턴 추가 인식
    - 수료 워크플로우 예외 시 `artifacts/player_debug/*` 디버그 산출물 자동 저장 강화
  - 최적화/보안 점검:
    - 큐 목록 조회 시 기본 로그 페이로드 제외(`include_logs=False`)로 관리자/사용자 상태 화면 렌더 부하 감소
    - `datetime.utcnow()` 제거 및 UTC aware timestamp 적용(파이썬 3.14 deprecation 정리)
    - Streamlit `use_container_width` 전면 치환(`width='stretch'/'content'`)으로 경고 로그 제거
    - 관리자 잠금해제 실패 횟수 제한/쿨다운 추가(`APP_ADMIN_MAX_ATTEMPTS`, `APP_ADMIN_COOLDOWN_SEC`)
    - 보안 로그/가드 파일/작업 스냅샷 파일 권한을 `0600`으로 강제
    - URL 요청 보강: `urlopen` 호출 전 `http/https` 스킴 검증 적용
    - 보안 스캔(Bandit) 재점검: High 0건, Medium 0건 확인
  - 사용자 UX/복구 동선 보강:
    - `ID/PW 입력 + Enter`를 `로그인/동기화` 제출과 동일하게 처리(form submit)
    - `START`는 계정 동기화 완료 후에만 활성화(실행/조회 동선 분리)
    - 로그인/동기화 시 해당 계정의 `running/pending` 또는 최근 작업을 즉시 포커싱
  - OCR 폴백 비활성 이슈 해소:
    - 서버에 `tesseract 5.5.2` 설치, `kor.traineddata` 배치 완료
    - `eng/kor/osd/snum` 언어셋 확인 및 `_ensure_tesseract=True` 검증 완료
  - RAG 실패 패턴 완화:
    - `LLM JSON 파싱 실패`에 대한 관용 파서(ast/스마트쿼트/후행콤마) 반영
    - 저신뢰도 floor 비교를 반올림 게이트로 보정(표시값/실제값 불일치 완화)
  - 검색/추론 고도화 2차:
    - 핵심 키워드 가중(BM25 유사): 도메인 키워드/토큰 길이 가중 반영
    - 부정형 문항(web_weight 동적 하향): `0.35 -> 0.18 cap`
    - 저신뢰 self-check(모순 검토) 패스 추가
    - `!g` 우선 + 법령형(`site:law.go.kr`) 교차검증 + 핵심구문 따옴표 쿼리 적용
  - 모델 운영 고도화:
    - 생성모델 체인 도입: `qwen2.5:3b(우선) -> qwen2.5:7b -> EEVE` 자동 폴백
    - `.env.example`/README 운영 기본값 동기화
    - EEVE 설치 후 스모크 A/B(4문항) 결과: `EEVE 0.50 < qwen7b 1.00 = qwen3b 1.00`
  - 최적화/보안 강화 2차:
    - 입력/쿼리 길이 상한, 제어문자 정리, 프롬프트 근거 길이 제한
    - embed/web 캐시 상한 도입(메모리 보호)
    - 모델 응답 파싱 길이 상한 도입
    - 일일 로그 파일 권한 `0600` 강제
- 2026-03-12 반영/검증:
  - 차시 이동 로직 보강:
    - 우측 하단 동그라미 화살표(`#nextPage`)를 우선 클릭 경로로 반영
    - 단, 기본 다형태(`다음/Next`, `>`, `›`, `»`, `→`, 아이콘/이미지`) 탐지 로직은 그대로 유지
    - 성공 판정은 고정 `1/2 -> 2/2`가 아닌 `pageInfoDiv`의 현재 페이지 번호 증가(`cur` 상승) 기준으로 반영
  - 강제 전환 보강:
    - `다음 클릭 무변화` 케이스에서도 `#nextPage + pageInfoDiv` 증가 검증 경로를 우선 사용하도록 반영
  - 실검증(특수 케이스):
    - `공공 통일교육`에서 `2번째 학습하기` 진입 후 우측 하단 동그라미 `>` 클릭 시 `pageInfoDiv: 1 / 2 -> 2 / 2` 확인
  - 테스트용 임시 분기 정리:
    - `미완료 2차시 우선` 테스트용 임시 코드 추가/검증 후 원복 완료

## 마일스톤 (업데이트)
- M1 프로젝트 골격: 100% 완료
- M2 로그인/포털 이동 자동화: 99% 완료
- M3 강의실 진입/학습차시 탐색: 97% 완료
- M4 학습 재생/차시 완료 루프: 97% 완료
- M5 수료 순서 자동화(진도→시간→시험): 96% 진행중
- M6 종합평가 자동화 안정화: 97% 진행중
- M7 LLM(RAG) 기반 시험풀이 고도화: 98% 진행중
- M8 원격 실행 서버화(Streamlit+Tunnel+Worker): 91% 진행중
- M9 동시성 제어(최대 5명) + 대기열: 97% 진행중
- M10 운영(로그/모니터링/복구): 96% 진행중

## 완료된 작업
- 로그인, `My학습포털 > 나의 학습현황` 이동.
- 첫 과목 강의실 진입 및 학습창 열기.
- 긴 페이지에서 학습 버튼/차시 탐색 로직 보강.
- `학습 차시` 중심 총차시/미완료 판독 강화.
- 학습종료 안내 문구(`학습이 종료되었습니다` 계열) 감지.
- 수료표 파싱:
  - 학습진도율 기준/실적 판독
  - 학습시간 기준/실적 판독 및 부족분 계산
- 종합평가 응시 제한 팝업(`학습 진도율 80 이상`) 감지/해제.
- `미완료` 우선 보완 흐름 반영.
- Streamlit 기능 추가:
  - 종합평가 탐침
  - 수료 순서 자동 실행
  - RAG 인덱스 생성
  - 종합평가 LLM 풀이(RAG)
- 로컬 RAG 파일 추가:
  - `rag_index.py` (PDF/TXT/MD 인덱싱)
  - `rag_solver.py` (Ollama 기반 답안 추론)
- 종합평가 안정화 1차 반영:
  - 문항 스냅샷 추출을 다중 스코프 평가 방식으로 개선(최적 후보 선택)
  - 보기 추출 실패 시 OCR(tesseract) 폴백 추가
  - LLM 저신뢰(confidence 미달) 시 재질문 1회 후 판단 로직 추가
- 실세션 게이트 검증 완료:
  - 로그인 → 학습현황 → 강의실 진입 성공
  - 학습진도율 37% 판독
  - 종합평가 80% 게이트 차단 동작 확인
- 학습 정체 복구 로직 추가 및 실검증:
  - `all_blue` 실패 시 빨간 단계 탐지/우선 진입
  - 장시간 정체 시 팝업 종료 → 강의실 복귀 → `미완료 학습하기` 재진입
  - `미완료 우선 진입`을 기본 학습 시작 경로에 반영
  - 실측 변화: 학습진도율 61% → 100%, 미완료 6 → 0
  - `all_blue` 실패 시에도 마지막 `Next` 강행 시도
- 장시간 러너 실운영 검증:
  - 80% 목표를 넘어 최종 100%/미완료 0 도달
  - LLM 선조건(`require_llm_before_exam`)이 켜진 상태에서 Ollama 미구동이면 응시 보류됨 확인
  - 상태 리포트 파일(`logs/overnight_status_live.json`) 기준으로 `paused_llm_required` 기록
- 수강과정 순차 완료 로직 반영:
  - `나의 학습현황 > 수강과정` 표에서 수강 가능한 첫 행(`학습하기/이어 학습하기`) 자동 진입
  - 한 과정 완료 시 목록 복귀 후 다음 수강 가능 과정으로 자동 반복
- 종합평가 응시 선택자 2차 보강:
  - `응시하기 click`, `재응시`, `평가응시` 텍스트 케이스 추가
  - `onclick/href`의 `Eval/Exam/Test` 힌트 기반 클릭 폴백 추가
  - 팝업 없이 같은 탭 직접 이동되는 케이스 감지 추가
- 종합평가 시작 경로 보강:
  - `응시하기` 클릭 후 사전안내 레이어(`시험 시작하기`) 자동 처리
  - 시험 시작 `confirm` 대화상자 자동 수락 처리
  - 클릭/레이어 탐색 스코프를 `page + frames`로 확장
- 종합평가 응시횟수 보호 규칙 반영:
  - 각 강의당 `응시횟수(used/max)` 판독
  - `EXAM_ATTEMPT_RESERVE=1` 기본값으로 마지막 1회는 자동화에서 보존
  - 남은 횟수 `<= reserve`일 때 자동 응시 중단
- 종합평가 문항 반복 감지 안정화:
  - 문항 키 생성 시 타이머/동적 값 제거
  - 동일 문항 반복 시 탐침/풀이 루프 조기 종료
- RAG 검색/추론 고도화:
  - 하이브리드 검색(임베딩 + 키워드 커버리지) 반영
  - 선지별 점수화 기반 결정값(`det_choice/det_conf`) 추가
  - LLM 결과와 검색 점수 불일치 시 근거 점수 우세 선택지 채택
- RAG 인덱스 경량화/용량제어:
  - 청크 중복 제거(텍스트 해시)
  - 임베딩 `f16(base64)` 저장 지원(저장공간 절감)
  - `sources + sid` 구조로 source 중복 문자열 저장 최소화
  - 인덱스 생성 시 전체 저장공간 상한(`RAG_STORAGE_LIMIT_GB`) 사전 검증
  - 필요 시 오래된 인덱스 파일 자동 정리(`RAG_PRUNE_OLD_INDEXES`)
- 합격점수 연계 저신뢰 제어:
  - `RAG_PASS_SCORE=80` 기준으로 저신뢰 허용량 동적 계산
  - `RAG_LOW_CONF_FLOOR=0.55` 미만은 자동 중단
  - 기본 임계치 `RAG_CONF_THRESHOLD=0.65`로 조정
- Ollama 로컬 셋업 진행:
  - 로컬 서버 기동 확인 (`127.0.0.1:11434`)
  - `nomic-embed-text` 모델 pull 완료
  - `qwen2.5:7b-instruct` 모델 pull 완료
- RAG 솔버 코드 안정화:
  - `rag_solver.py` f-string 문법 오류 수정
  - `python3 -m py_compile` 스모크 테스트 통과
- 실세션 종합평가 검증:
  - 종합평가 진입 성공 URL 확인: `/usr/classroom/exampaper/detail/layer.do`
  - DOM 옵션 추출 파서 보강 후 탐침 재검증: DOM판독 5/5, OCR필요추정 0
  - 문항 키 안정화 반영 후 탐침 재검증: DOM판독 1/1, `다음 클릭 후 문항 변화 없음`으로 안전 종료
  - RAG 자동풀이 시작 검증: `Q 1/10 -> choice=2, conf=0.70` 생성까지 성공
  - 신뢰도 규칙 검증: 임계치(0.65) 미달 시 재질문 1회 후 자동 중단 동작 확인
  - 중단 후 응시횟수 재확인: `attempted=0, max_attempt=5, remaining=5`
- RAG 경량화 검증:
  - 샘플 문서 인덱싱 결과: `chunks=1`, `index_size_mb=0.0`, `predicted_total_gb=0.0/20.0`
  - 샘플 질의 추론 결과: `choice=1`, `confidence=0.832`
- 실세션(신규 기준) 검증:
  - 기준값: `conf>=0.65`, `pass_score=80`, `low_conf_floor=0.55`
  - 선택/전환 보강 후 결과: Q1~Q10 연속 진행 성공, 마지막 문항에서 최종 제출 실행
  - 제출 후 완료 감지 성공: `시험평가 완료 신호` 확인
  - 수료 판독 결과: `수료점수=fail`로 과정 미수료 판정(사이트 기준 반영)
  - 제출 후 응시횟수 재조회: `used=1/5`, `remaining=4`
- 점수 미달 후 재학습/재응시 자동화:
  - 결과 패널(`item result`, `resultYn=Y`) 파싱에 `onclick`/주변텍스트 추출 추가
  - 결과지 텍스트에서 문제/보기/정답 추출 파서 추가(번호형/원형숫자형 지원)
  - 추출 정답을 `EXAM_ANSWER_BANK_PATH`에 저장/누적 후 다음 응시에서 우선 적용
  - `EXAM_AUTO_RETRY_MAX`, `EXAM_RETRY_REQUIRES_ANSWER_INDEX` 설정 추가
  - 수료 워크플로우 및 단독 종합평가 실행 모두 자동 재응시 루프 연동
- 재응시 제어 강화:
  - 재응시 직전 사유/점수 로그 출력
  - 점수 비개선(동일/하락) 연속 감지 시 조기 중단(`EXAM_RETRY_NO_IMPROVE_LIMIT`)
- 문항/보기 추출 우선순위 조정:
  - `Structured(셀렉터 기반) -> DOM -> OCR` 순서로 시도하도록 반영
  - Structured 탐색은 지연 렌더링 대응을 위해 다회 시도(`attempts=2`) 추가
- 저신뢰 교차검증 모델 스위칭:
  - 1차/재질문 후 임계치 미달 시 폴백 모델(`qwen2.5:7b`, `EEVE`) 순차 검증
  - `RAG_CONF_ESCALATE_MARGIN` 이상 신뢰도 개선 시 상위 모델 답안 채택
- answer-bank 보기 셔플 재매핑 검증:
  - `scripts/answer_bank_shuffle_check.py` 추가
  - 실데이터 스모크: `trials=100, pass=100, fail=0`
- 실세션 E2E 검증(2026-03-11 밤):
  - `logs/completion_e2e_report_run1.json`:
    - 1개 과정 우회(`remaining=2`) + 다음 과정 시험 자동진행 확인
    - 저신뢰 모델 스위칭 실발동(`qwen2.5:7b`로 승격) 확인
    - 3차 재응시에서 `conf=0.52 < floor=0.53`로 안전 중단
  - `logs/completion_e2e_report_run2.json`:
    - 우회 순서 패치 검증 성공: `remaining=2` 시점에서 즉시 과정 우회 후 3번째 과정 진입
    - 장시간 차시 진행 중 정체 복구 로직 발동(디버그 스냅샷 저장 포함)
    - 복구 중 `학습창 재오픈 실패` 케이스 확인(추가 보강 필요)
- 임계치/마진 A/B 스윕:
  - `scripts/conf_threshold_sweep.py` 추가
  - 샘플셋(6문항) 스윕 결과: 현 구간(`th=0.58~0.65`, `margin=0.05~0.10`)에서 동일 지표
    - `accuracy_accepted=0.8333`, `accept_rate=1.0`, `switched_count=0`
- 우회 이력 계정 격리 패치:
  - 전역 우회 이력 파일을 `user_id` 기반 `account_scope`로 분리 저장/로딩하도록 수정
  - 다계정 격리 검증 스크립트 결과: `isolation_ok=true`
- 숫자 문항 보정:
  - Strict-Numerical-Check 기본 적용 + 7B+/EEVE 숫자 재검증 패스 추가
  - 기한/비율/횟수/조문번호 숫자 불일치 시 confidence 상한(<=0.55) 보수 적용
- 2026-03-11 운영/검증 추가:
  - Negative Evidence 고도화: 감점값 시간감쇠(half-life) + 최대점수 cap + 법령근거 보호 + answer-bank 강차단 유지
  - 숫자 검증 강화: LLM strict pass + 정규식 기반 deterministic numeric recheck 병행
  - 장애복구 강화: 원클릭 일시 오류 시 자동 재로그인/재결합 다회 재시도(`APP_RESUME_RETRY_MAX`)
  - 런타임 동기화/재시작 완료: `.khnp-launch-runtime` 해시 일치 + 8501/8502 헬스체크 200
  - 성능/보안 감사 리포트: `logs/security_perf_audit_report.json` (`security_findings=0`, worker5 부하 통과)
  - 큐 부하 최종 리포트: `logs/queue_load_report_final.json` (`max_running_observed=5`, `pending_seen=true`)
  - 실계정 시험 진입 점검: `logs/exam_live_run_report1.json`, `logs/exam_live_run_report2.json` 모두 `학습진도율 75%(응시조건 미달)`로 시험 단계 미진입 확인
- 2026-03-12 운영/검증 추가:
  - answer-bank 셔플 대량 회귀: `scripts/answer_bank_shuffle_check.py --shuffles 500` 결과 `trials=19000, pass=19000, fail=0`
  - 우회 이력 계정 격리 재검증: `logs/deferred_course_history_check_latest.json` (`isolation_ok=true`, `skip_chain_ok=true`)
  - 워커5 장시간 부하: `logs/queue_load_report_longrun.json` (`jobs=300`, `max_running_observed=5`, `all_succeeded=true`)
  - 백프레셔(대기열 상한) 검증: `max_pending=12` 설정에서 `submitted=30 -> accepted=12 / rejected=18`
  - 실시간 로그/관리자 집계 정합 검증(큐 단위): 로그 카운트 단조증가, owner 집계와 전체 stats 일치 확인
  - RAG 문서셋 동기화 후 재인덱싱: `rag_data/project/{README,NEXT_STEPS}.md` 최신화 + `logs/rag_reindex_report_latest.json`
  - OCR 폴백 경로 보강: `shutil.which` 실패 시 `/opt/homebrew/bin/tesseract`, `/usr/local/bin/tesseract` 자동 탐색
  - 차시전환 루프 보강:
    - 우하단 동그라미 `>` 클릭 성공 판정을 `click 수행`이 아니라 `pageInfoDiv 증가`로 고정
    - `pageInfoDiv`와 `next` 버튼이 서로 다른 frame에 있어도 교차 프레임으로 증가 여부 검증
    - 원형 아이콘-only 플레이어 대응: 우하단 원형 버튼군에서 가장 오른쪽 버튼을 `next`로 우선 선택
  - 학습평가(퀴즈) 게이트 처리 추가:
    - `선택지 선택 -> 정답확인 -> 다음문제/결과보기`를 단계 루프에서 자동 처리
    - 단일 액션이 아니라 다회(최대 6회) 연속 처리로 후속 버튼까지 소진
  - strict E2E 재검증(최신):
    - `통합보안교육`에서 퀴즈 게이트(`quiz-next-btn`, `quiz-result-btn`) 처리 로그는 확인
    - 그럼에도 `1/2 -> 2/2` 단계 증가 실패가 반복되어 `진도율 단계 중단`으로 종료
    - 최신 리포트: `logs/full_smoke_report_latest.json`
  - strict E2E 실주행:
    - 1차: 시간보충 체크 제한(1회)으로 중단 (`logs/full_smoke_report_latest.json`)
    - 2차: 공공 통일교육 1개 수료 후 통합보안교육 진입, 차시 자동진행 중 `nextPage 클릭 성공 후 단계 미증가` 정체 반복 재현
    - 정체 시 디버그 아티팩트 확보: `artifacts/player_debug/progress_not_found*.png`, `step_not_blue*.png`, `recovery_failed*.png`
  - 서버 반영/재시작/헬스체크 완료:
    - `scripts/install_launchagents.sh` 재실행 후 user/admin/cloudflared running
    - `http://127.0.0.1:8501` / `:8502` HTTP 200
    - `.khnp-launch-runtime` 핵심 파일 해시 일치 확인
- 2026-03-15 운영/검증 추가:
  - `학습 차시` 직접 진입 보강:
    - `통합보안교육` 24차시 목록을 구조적으로 파싱하고 `미완료 일반 차시의 학습 하기` 우선 선택
    - `학습완료` 차시 재수강 방지, `학습평가`는 게이트 처리용으로만 분리
  - 차시 내부/차시 완료 버튼 분리:
    - `round-next-clicked`와 `final-next-clicked`를 실제 런타임 로그에서 분리 확인
    - 내부 페이지는 `동그라미 > / nextPage` 우선, 마지막에만 `우하단 Next`
  - 시험 인덱싱/매칭 보강:
    - 활성 `quiz_li` 기반 structured 문항 추출, option-set fallback, 결과지 dedupe 보강
    - `통합보안교육` 종합평가 재검증 결과 `solved=10`, `matched_result_entries=10`, answer-bank `9 -> 10`
    - 품질 리포트: `logs/exam_quality_reports/exam_quality_20260315_023103_통합보안교육_try01.json`
    - `알기쉬운 이해충돌방지법(courseActiveSeq=12390)` 신규 케이스에서 hidden radio/label 기반 보기 DOM을 추가 대응
    - 재검증 결과 `try01` 학습 후 `try02` 자동 재응시 성공 + 수료 확인
    - 품질 리포트: `logs/exam_quality_reports/exam_quality_20260315_073340_알기쉬운_이해충돌방지법_try01.json`, `logs/exam_quality_reports/exam_quality_20260315_073416_알기쉬운_이해충돌방지법_try02.json`
  - 시험 품질 fail-fast 추가:
    - 결과지 매칭 수가 문항 수와 다르면 `시험 파싱 품질 경고` 로그를 남기고 자동 재응시 전 중단/확인 가능
    - 품질 묶음 최신 집계: `logs/exam_quality_report_check_latest.json` (`reports=8`, `alignment_ok=3`, `warnings=5`)
  - 실패 작업 진단 번들 추가:
    - 작업 실패 시 `.runtime/job_diagnostics/<job_id>/summary.json`, `logs_tail.txt`, `traceback.txt` 자동 저장
    - 플레이어 디버그는 `artifacts/player_debug/<run_id>/...` 로 run 단위 분리
    - Streamlit 작업 상세에서 `작업 스냅샷`, `진단 번들`, `진단 정보` 바로 확인 가능
    - 관리자 화면에 `최근 실패 작업`, `시험 품질 리포트` 대시보드 추가
    - 묶음 점검 스크립트: `scripts/exam_quality_report_check.py` → `logs/exam_quality_report_check_latest.json`
  - 2026-03-15 추가 운영 보강:
    - 큐 싱글턴 재설정 지원: 런타임 중 `APP_WORKER_COUNT` 증분 반영, 활성 워커 수 UI 노출
    - 시간 표시 정책 고정: 저장은 UTC, UI/작업 로그는 접속자 브라우저 로컬 시간대로 렌더링
    - 현재 작업 로그 전체 스크롤 지원: compact/detail 모두 과거 로그 확인 가능
    - 플레이어 상태 분리: `내부 페이지 진행`과 `차시 단계 상태`를 별도로 판정, `counter-source-mismatch` 로그/디버그 추가
    - 학습창 복구 시 `이어 학습하기`보다 마지막 미완료 차시 직접 재오픈 우선
    - 프록시/KR egress 프리플라이트 추가: `.runtime/proxy_preflight_latest.json`
    - 시험 경고 과정은 `warning_snapshots`로 별도 저장
  - 2사이클 실주행:
    - `자살예방 생명지킴이 양성교육(보고 듣고 말하기)` 자동 진입 확인 후 큰 시간 부족(`01:34:11`)으로 우회
    - `인터넷 및 스마트폰 과의존 예방교육` 미완료 차시 진입/진행 확인 후 시간 부족(`00:46:01`)으로 우회
    - 리포트: `logs/completion_e2e_report_two_cycles.json` (생성 시점상 `max_courses` 성공 처리 직전 파일)
  - 서버 반영/재시작:
    - 8501/8502 재기동 후 둘 다 HTTP 200 확인

## 진행중 작업
- `anpigon/eeve-korean-10.8b` 모델 다운로드 완료(설치됨). 운영 기본은 qwen 우선 체인으로 유지.
- 종합평가 정답률 개선(외부 문서/사내 자료 인덱스 확대, 현재는 샘플 문서 편중).
- 종합평가 LLM 응시 실세션 E2E 합격률 검증(신규 기준: conf=0.62, pass_score=80).
- OCR 정확도 튜닝(언어모델 `kor+eng`, psm 조합, 난독 케이스 재검증).

## 정답률 개선 로직(추가 고도화 후보)
1. 문항 정규화 키 강화(고도화):
   - 불필요한 기호/번호/순서 의존성을 제거한 `question_norm_v2`를 만들어
     동일문항 재등장 시 매칭률을 높임.
2. 선택지 정렬 불변 매칭(고도화):
   - 보기 순서가 바뀌어도 의미 유사도(토큰/Jaccard + 부분문자열)로 정답 인덱스를 재매핑.
3. 해설/정답지 우선순위 명시(고도화):
   - `answer-bank > 해설문서 > 일반 RAG > 웹검색` 고정 우선순위를 로그로 남겨 추적 가능화.
4. 오답 회피 제어(고도화):
   - 연속 실패 문항은 동일 근거 반복 선택을 막고 2순위 근거 후보로 자동 전환.
5. 시험 후 학습 피드백:
   - 결과지 파싱 시 정답뿐 아니라 오답 보기 패턴도 저장해 다음 회차에서 오답 선택 확률을 낮춤.

## 우선순위 실행 순서 (지금부터)
1. RAG 문서셋 확장(외부 문서 + 내부 문서) 후 인덱스 재생성.
2. 종합평가 LLM 자동 응시 재실행(목표: 60점 이상/수료 통과).
3. 통과 시 수료 처리→강의목록 복귀→다음 강의 자동 진입 루프 최종 검증.
4. 학습시간 부족 시 1차시 유지 + 10분 주기 체크로 수료시간 충족.
5. 서버화 착수(FastAPI + Redis 큐 + 동시 5명 semaphore, 인덱스 캐시 재사용 포함).

## 추가할 해야할 것 (신규 반영)
1. 종합평가 페이지 실제 문제 난이도/유형 샘플 1회 수집.
2. OCR 인식률 개선(저해상도/난독 문항에 대한 전처리·재시도 튜닝).
3. LLM 저신뢰 답변(confidence 미달) 시:
   - 재질문 1회
   - 그래도 미달이면 수동 확인 대기
4. RAG 문서셋 정리:
   - 원안법/사내자료/기출 유사문항 우선 인덱싱
   - 청크 길이와 중복 제거 튜닝
5. 최종 완료판정 룰 고정:
   - 학습진도율 수료기준 충족
   - `미완료` 0개
   - 시험평가 기준점수 충족
   - 학습시간 기준 충족
6. 강의 팝업은 유지하고 강의실만 주기 새로고침하는 분리 로직 최종 검증.

## 원격 서비스 마일스톤 (동시 5명 제한)
- 목표: 외부(휴대폰/사외PC)에서 요청, 실행은 맥미니/서버에서만 처리.
- 현재 구현 구조(완료/운영중):
  - Web UI: Streamlit 사용자/관리자 화면 분리
  - Queue/Worker: 인메모리 작업큐 + 백그라운드 워커(`task_queue.py`)
  - 외부접속: Cloudflare Tunnel + `app.kwon.work`/`admin.kwon.work`
  - 실행지속: LaunchAgent 자동기동(user/admin/tunnel)
  - Job log/status 저장: 큐 상태 + 작업별 로그 + 보안 감사로그
- 확장 목표(미완료):
  - API(FastAPI)
  - Queue(Redis) 및 분산 워커
- 동시성 정책:
  - 실행 슬롯 5개(semaphore=5)
  - 초과 요청은 대기열
  - 대기순번/예상대기시간 표시
- 계정정보 정책:
  - 저장 금지(요청 메모리에서만 사용)
  - 로그 마스킹
  - 작업 종료 즉시 폐기

## 원격 서비스 TODO
1. 동시 실행 슬롯을 운영값 `APP_WORKER_COUNT=5`로 고정/검증(현재 코드는 지원, 운영 튜닝 필요).
2. 작업 취소/재시도 API 또는 UI 액션 추가.
3. 대기순번/예상대기시간 표기 추가.
4. Redis 기반 영속 큐 + 분산 워커 전환(필요 시).
5. FastAPI 게이트웨이(토큰 인증/레이트 제한) 추가(필요 시).
6. 배포 방식 고도화:
   - 단일 맥미니 Docker Compose
   - 또는 Linux 서버 이전
7. 운영 대시보드:
   - 현재 실행 수
   - 대기열 길이
   - 평균 처리시간
   - 실패율

## Debug Artifacts
- Folder: `artifacts/player_debug/<run_id>/`
- Purpose: popup/page/frame text/screenshot inspection on failures, run 단위 분리 저장.
- Failure bundle: `.runtime/job_diagnostics/<job_id>/summary.json`
- Bundle contents: 작업 메타데이터, 최근 로그 tail, traceback, 관련 artifact 경로 목록
