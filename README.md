# e-khnp automation project

Playwright + Streamlit 기반 e-khnp 수강 자동화 도구입니다.

## 0) 현재 상태 (2026-03-11)

- 원클릭 실행 지원: `인덱스 확인(필요 시 생성) → 수료 자동 워크플로우` 단일 버튼 실행
- 수료 루프: 한 과정 수료 후 `나의 학습현황 > 수강과정`에서 다음 과정 자동 진입 반복
- 사용자 계정 동기화 흐름: `ID/PW 입력 + Enter(또는 로그인/동기화)` 후 `START` 실행으로 분리
- 재접속 복구: 브라우저 종료/세션 만료 후에도 같은 계정 동기화 시 기존 큐 상태 즉시 조회
- 종합평가 예외 처리:
  - 시험평가 수료기준이 공란/`-`인 과정은 시험 자동 생략
  - 각 강의당 마지막 1회 응시는 보존(`EXAM_ATTEMPT_RESERVE=1`)
- 정답률 개선 흐름: 웹 검색 강제 참조 + 점수 미달 시 결과지 정답 인덱싱 후 자동 재응시
- 시간보충 안정화:
  - `학습 진행현황` 버튼 미탐지 시 `학습 차시 1차시 학습하기` 직접 진입 fallback
  - 학습시간 부족 체크 주기 동적화(남은 시간 기준 3~10분)
- OCR 폴백 활성화 완료: 서버에 `tesseract + kor` 데이터 설치 후 문항 OCR 경로 정상화
- RAG 안정화: 저신뢰도 경계값(0.53) 비교 보정 + LLM JSON/텍스트 파싱 관용성 강화
- 검색 고도화: `!g` 우선 + 법령형(`site:law.go.kr`) 교차검증 + 핵심구문(따옴표) 질의
- 장애복구 보강: 브라우저 종료/세션 만료성 오류 시 자동 재로그인/재결합 재시도(`APP_RESUME_RETRY_MAX`)
- 모델 운영: `qwen2.5:3b` 우선, `qwen2.5:7b` 폴백, `EEVE-Korean-10.8B` 보조 폴백 체인 적용
- 2026-03-10 실주행 확인:
  - 원클릭 경로에서 `학습시간 보충용 학습창` 팝업 진입 성공
  - 학습시간 `00:05:08 -> 00:14:22` 증가 확인
  - 완주 런은 사용자 요청으로 중단(강제 중단 전 정상 진행 확인)

## Engineering Case Study (Portfolio)

### Problem

- e-khnp 학습 완료는 `진도율`, `미완료 차시`, `종합평가`, `학습시간`을 모두 충족해야 하며, 웹 UI/팝업/프레임 구조가 자주 흔들립니다.
- 실제 운영에서 가장 큰 리스크는 "정체 상태 무한 대기", "시험 단계 예외", "과정 간 반복 처리 실패"였습니다.
- 결과적으로 단순 매크로로는 안정적 완주가 어려워, 상태판독 기반의 제어 시스템이 필요했습니다.

### Approach

- 단일 실행 버튼(원클릭)으로 `인덱스 확인/생성 -> 수료 워크플로우(진도 -> 시간 -> 시험)`를 오케스트레이션했습니다.
- 각 단계를 상태 기반으로 분리하고, 실패 시 복구 경로(재진입/새로고침/폴백 선택자)를 명시적으로 구현했습니다.
- 시험 단계는 로컬 RAG + 웹 검색 + 결과지 정답 인덱싱(피드백 루프)으로 재응시 품질을 개선했습니다.
- 운영 안전장치로 `응시횟수 reserve`, `동적 체크 주기`, `안전 제한(과정 수/체크 횟수)`를 적용했습니다.

### Architecture

- **Orchestrator**: `automation.py`의 `EKHNPAutomator`가 상태 전이와 복구 로직을 담당
- **UI Layer**: `app.py`(Streamlit)에서 원클릭/개별 실행/로그 확인 제공
- **Browser Worker**: Playwright(Chromium)로 로그인, 강의실, 팝업, 시험 DOM 자동화
- **RAG Subsystem**: `rag_index.py`(문서 인덱싱) + `rag_solver.py`(문항 추론)
- **Feedback Loop**: 시험 결과지에서 정답 인덱싱 후 `exam_answer_bank`에 반영, 다음 응시에 우선 적용
- **Ops/Observability**: `logs/`, `artifacts/player_debug/`, `overnight_runner.py`로 장시간 상태 추적

### Results

- 학습 정체 복구 로직 적용 후 실측: **학습진도율 `61% -> 100%`, 미완료 `6 -> 0`**
- 원클릭 시간보충 검증: **학습시간 `00:05:08 -> 00:14:22` 증가**
- 종합평가 실세션 자동풀이 검증: **10/10 문항 완주 + 최종제출/완료 신호 감지**
- 운영 안정성: 시험 없는 과정 자동 생략, 응시횟수 reserve 보존, 저신뢰 응답 재질문/중단 기준 적용

## 0-1) 마일스톤 (요약)

- M1 프로젝트 골격: `100%`
- M2 로그인/포털 이동 자동화: `99%`
- M3 강의실 진입/학습차시 탐색: `97%`
- M4 학습 재생/차시 완료 루프: `96%`
- M5 수료 순서 자동화(진도→시간→시험): `95%`
- M6 종합평가 자동화 안정화: `96%`
- M7 LLM(RAG) 기반 시험풀이 고도화: `98%`
- M8 원격 실행 서버화(Streamlit+Tunnel+Worker): `90%`
- M9 동시성 제어(최대 5명)+대기열: `96%`
- M10 운영(로그/모니터링/복구): `94%`

## 1) macOS(집 맥미니) 실행

```bash
cd "/Users/maegbug-eeo/Documents/New project 2"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

`.env`에 계정 정보를 넣습니다.

```dotenv
APP_ACCESS_CODE=kwon
APP_ACCESS_CODE_HASH=
APP_ADMIN_CODE=
APP_ADMIN_CODE_HASH=
APP_DEFAULT_UI_ROLE=user
APP_FORCE_UI_ROLE=
APP_ACCESS_ALLOW_OPEN=false
APP_ACCESS_MAX_ATTEMPTS=5
APP_ACCESS_COOLDOWN_SEC=300
APP_ADMIN_MAX_ATTEMPTS=5
APP_ADMIN_COOLDOWN_SEC=300
APP_ACCESS_SESSION_TTL_MIN=240
APP_WORKER_COUNT=5
APP_QUEUE_MAX_PENDING=20
APP_QUEUE_MAX_HISTORY=200
APP_SECURITY_AUDIT_ENABLED=true
APP_RESUME_RETRY_MAX=2
APP_RESUME_RETRY_BACKOFF_SEC=2
EKHNP_USER_ID=사번
EKHNP_USER_PASSWORD=비밀번호
EKHNP_HEADLESS=false
EKHNP_TIMEOUT_MS=90000
```

실행:

```bash
source .venv/bin/activate
streamlit run app.py
```

또는 실행 스크립트:

```bash
./scripts/start_streamlit_local.sh
# 사용자 전용(포트 8501, 사용자 모드 고정)
./scripts/start_streamlit_user.sh
# 관리자 전용(포트 8502, 관리자 모드 고정)
./scripts/start_streamlit_admin.sh
```

## 2) Windows 실행(회사 PC)

```powershell
cd "C:\work\e-khnp"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

실행:

```powershell
.venv\Scripts\activate
streamlit run app.py
```

## 3) GitHub로 집/회사 동기화

최초 1회(현재 프로젝트 폴더에서):

```bash
cd "/Users/maegbug-eeo/Documents/New project 2"
git init
git add .
git commit -m "init: e-khnp automation"
git branch -M main
git remote add origin https://github.com/Tate-kwon/e-khnp.git
git push -u origin main
```

작업 루틴:

```bash
git pull --rebase
# 작업
git add .
git commit -m "feat: update automation logic"
git push
```

## 4) 현재 자동화 핵심 동작

- 로그인
- `My학습포털 > 나의 학습현황` 이동
- 수강과정 첫 과목 진입
- 학습진행현황의 `학습하기/이어 학습하기` 클릭
- 차시 단계 파란색 완료 확인 후 다음 단계 이동
- 우하단 `다음(Next)` 클릭 시 다음 차시 반복
- 자동 종료 모드 지원:
  - 총 차시 감지 우선(강의실 학습하기 버튼 기준)
  - Next 버튼 기준
  - 사용자 입력 차시 수 기준

## 5) 보안 주의

- `.env`는 커밋하지 않습니다.
- 앱 접속 코드는 `.env`의 `APP_ACCESS_CODE`로 관리합니다. (기본: `kwon`)
- 운영 권장: 평문 대신 `APP_ACCESS_CODE_HASH`(sha256) 사용
- `APP_ACCESS_ALLOW_OPEN=false`면 코드/해시 미설정 시 앱 진입 자체를 차단
- 화면 분리: 사용자 모드(원클릭/내 작업), 관리자 모드(전체 기능)
- 관리자 모드는 `APP_ADMIN_CODE` 또는 `APP_ADMIN_CODE_HASH` 잠금 해제 필요
- `APP_DEFAULT_UI_ROLE`, `APP_FORCE_UI_ROLE`로 화면 모드 기본값/강제값 제어
- 접속코드 실패 `APP_ACCESS_MAX_ATTEMPTS`회 시 `APP_ACCESS_COOLDOWN_SEC` 동안 잠금
- 접속 인증 세션은 `APP_ACCESS_SESSION_TTL_MIN`분 후 재인증
- 실패 횟수/잠금은 서버 전체 세션에 공통 적용되며 재시작 후에도 `.runtime/access_guard.json`으로 이어집니다.
- 자동화 실행은 작업 큐/워커 분리 구조(`APP_WORKER_COUNT`)로 백그라운드 처리
- 운영 기본값은 `APP_WORKER_COUNT=5`이며, 5개를 초과한 작업은 자동으로 `pending(대기열)`로 전환됩니다.
- 큐 폭주 방지: `APP_QUEUE_MAX_PENDING`, `APP_QUEUE_MAX_HISTORY`로 등록/보관 한도 제어
- 보안 감사로그: `APP_SECURITY_AUDIT_ENABLED=true`일 때 `logs/security_audit.log` 기록
- 작업 로그는 비밀번호/접속코드 패턴을 자동 마스킹 처리
- 계정 정보는 로그에 저장하지 않습니다.
- 운영 정책: `접속 코드(kwon) 통과 후`, 각 사용자는 본인 e-khnp 계정으로 로그인합니다.

동시 처리 부하 점검(운영값 5 기준):

```bash
source .venv/bin/activate
python scripts/queue_load_check.py --workers 5 --jobs 15 --sleep-sec 1.2 \
  --report-path logs/queue_load_report.json
```

연속 우회 이력 영속/다음 강좌 선택 점검:

```bash
source .venv/bin/activate
python scripts/deferred_course_history_check.py \
  --report-path logs/deferred_course_history_check.json
```

보안/성능 감사(로그 마스킹 + 워커5 부하) 리포트:

```bash
source .venv/bin/activate
python scripts/security_perf_audit.py \
  --report-path logs/security_perf_audit_report.json \
  --queue-workers 5 --queue-jobs 25 --queue-sleep-sec 1.0
```

원클릭 전체 회귀 스모크(시간보충→시험→우회 마커 포함):

```bash
source .venv/bin/activate
python scripts/full_regression_smoke.py \
  --user-id "<ID>" --user-password "<PW>" \
  --completion-max-courses 2 --check-interval-minutes 3 \
  --max-timefill-checks 1 --safety-max-lessons 12 \
  --report-path logs/full_smoke_report_latest.json
```

`APP_ACCESS_CODE_HASH` 생성 예시:

```bash
python3 - <<'PY'
import hashlib
print(hashlib.sha256("kwon".encode("utf-8")).hexdigest())
PY
```

## 5-1) Cloudflare Tunnel 운영 (kwon.work)

도메인 권장:

- 사용자: `app.kwon.work`
- 관리자: `admin.kwon.work`

최초 1회:

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create khnp-app
cloudflared tunnel route dns khnp-app app.kwon.work
cloudflared tunnel route dns khnp-app admin.kwon.work
```

설정 파일:

```bash
mkdir -p ~/.cloudflared
cp ops/cloudflared/config.yml.example ~/.cloudflared/config.yml
```

`~/.cloudflared/config.yml`에서 아래 2개를 실제 값으로 바꿉니다.

- `REPLACE_WITH_TUNNEL_ID`
- `REPLACE_WITH_USER`

실행(터널 + 앱 2개):

```bash
./scripts/start_streamlit_user.sh
./scripts/start_streamlit_admin.sh
cloudflared tunnel run khnp-app
```

`start_streamlit_local.sh`는 운영 최적화를 위해 기본적으로 아래를 적용합니다.

- `STREAMLIT_BROWSER_GATHER_USAGE_STATS=false`
- `STREAMLIT_SERVER_FILE_WATCHER_TYPE=none`

사용자 접속 경로:

1. `https://app.kwon.work` 접속
2. 접속 코드 `kwon` 입력
3. 본인 e-khnp 사번/비밀번호 입력

관리자 접속 경로:

1. `https://admin.kwon.work` 접속
2. 접속 코드 `kwon` 입력
3. 관리자 비밀번호(`APP_ADMIN_CODE` 또는 `APP_ADMIN_CODE_HASH` 기준) 입력

## 5-2) macOS 자동시작(LaunchAgent)

설치:

```bash
./scripts/install_launchagents.sh
```

상태 확인:

```bash
./scripts/launchagents_status.sh
```

제거:

```bash
./scripts/uninstall_launchagents.sh
```

설치 후 재부팅해도 아래 3개가 자동 기동됩니다.

- 사용자 앱(8501)
- 관리자 앱(8502)
- cloudflared tunnel(`khnp-app`)

## 6) 로컬 LLM RAG(무료/로그인 불필요)

종합평가 문제 자동풀이를 위해 Ollama + 로컬 RAG를 지원합니다.

준비:

```bash
# Ollama 설치 후 모델 받기
ollama pull nomic-embed-text
ollama pull anpigon/eeve-korean-10.8b
ollama pull qwen2.5:7b
ollama pull qwen2.5:3b
```

`.env` 예시:

```dotenv
OLLAMA_BASE_URL=http://127.0.0.1:11434
RAG_DOCS_DIR=rag_data
RAG_INDEX_PATH=rag/index.json
RAG_EMBED_MODEL=nomic-embed-text
RAG_GENERATE_MODEL=qwen2.5:3b
RAG_GENERATE_MODEL_FALLBACKS=qwen2.5:7b,anpigon/eeve-korean-10.8b
RAG_TOP_K=6
RAG_CONF_THRESHOLD=0.62
RAG_CONF_ESCALATE_MARGIN=0.08
RAG_CHUNK_SIZE=900
RAG_CHUNK_OVERLAP=150
RAG_MIN_CHUNK_CHARS=80
RAG_MAX_CHUNKS=50000
RAG_STORAGE_LIMIT_GB=20
RAG_PRUNE_OLD_INDEXES=true
RAG_PASS_SCORE=80
RAG_LOW_CONF_FLOOR=0.53
RAG_WEB_SEARCH_ENABLED=true
RAG_WEB_TOP_N=4
RAG_WEB_TIMEOUT_SEC=8
RAG_WEB_WEIGHT=0.35
RAG_NEG_EVIDENCE_DECAY_SEC=7200
RAG_NEG_EVIDENCE_MAX_SCORE=6.0
RAG_NEG_EVIDENCE_BASE_PENALTY=0.18
RAG_NEG_EVIDENCE_STEP_PENALTY=0.12
RAG_NEG_EVIDENCE_MAX_PENALTY=0.75
EXAM_ANSWER_BANK_PATH=rag/exam_answer_bank.json
EXAM_DEFERRED_COURSES_PATH=.runtime/deferred_exam_courses.json
EXAM_QUALITY_REPORT_DIR=logs/exam_quality_reports
EXAM_AUTO_RETRY_MAX=2
EXAM_RETRY_REQUIRES_ANSWER_INDEX=true
EXAM_RETRY_NO_IMPROVE_LIMIT=2
EXAM_SKIP_COURSE_REMAINING_THRESHOLD=2
EXAM_ATTEMPT_RESERVE=1
COMPLETION_MAX_COURSES=20
```

실행 순서(Streamlit UI):

1. `원클릭 전체 자동 실행 (인덱스 확인 → 수료 자동)` 버튼을 누르면, 인덱스가 없을 때 자동 생성 후 수료 워크플로우를 실행합니다.
2. 세부 점검이 필요할 때만 개별 버튼(`RAG 인덱스 생성`, `종합평가 LLM 풀이(RAG)`)을 사용합니다.

참고:

- 신뢰도(`RAG 신뢰도 임계치`)보다 낮으면 자동풀이를 중단하도록 설계되었습니다.
- 신뢰도 미달 시 재질문 + self-check(모순 검토) 1회를 추가 시도합니다.
- 저신뢰 문항은 생성 모델 체인(예: `qwen2.5:3b -> qwen2.5:7b -> eeve`)으로 교차검증하며, `RAG_CONF_ESCALATE_MARGIN` 이상 개선 시 상위 모델 답안을 채택합니다.
- 숫자/기한/비율 문항은 Strict-Numerical-Check를 기본 적용하고, 7B+ 모델(또는 EEVE)로 숫자 일치 여부를 추가 재검증합니다.
- 반복 오답 근거는 하드 차단 대신 Negative Evidence(문항별 soft penalty)로 점수 하향하며, 법령/조문 계열 근거는 보호합니다.
- 기본 신뢰도 임계치는 `0.62`, 저신뢰 하한은 `0.53`(80점 운용 기준)입니다.
- 보수 운용 기준은 `RAG_PASS_SCORE=80`으로 설정되어 저신뢰 문항 허용량을 자동 제한합니다.
- 시험 보기 추출은 `Structured(셀렉터 기반) -> DOM 텍스트 -> OCR` 순으로 시도합니다. (현재 운영 서버는 `tesseract + kor` 설치로 OCR 활성 상태)
- Ollama가 실행 중이 아니면 인덱싱/풀이가 실패합니다.
- `EXAM_ATTEMPT_RESERVE=1`이면 각 강의당 마지막 1회 응시는 자동화에서 남겨둡니다.
- 문항 풀이 시 웹 검색은 항상 참조되도록 고정되어 있습니다.
- 웹 검색은 DuckDuckGo HTML을 사용하되 `!g` Bang 질의를 우선 시도합니다(인터넷 연결 필요).
- 법령형 문항은 `site:law.go.kr` 교차검증 질의를 추가로 수행합니다.
- `RAG_WEB_WEIGHT`는 로컬 RAG 점수와 웹 검색 점수 결합 비율입니다.
- 부정형 문항(아닌/틀린/거리가 먼)은 웹 가중치를 동적으로 낮춰 잡음 영향을 줄입니다.
- 점수 미달 시 결과지(`item result`)에서 정답을 추출해 `EXAM_ANSWER_BANK_PATH` 인덱스에 저장합니다.
- 우회 강좌 이력은 `EXAM_DEFERRED_COURSES_PATH`에 저장되어 재시작 후에도 유지됩니다.
- 우회 강좌 이력은 `EKHNP_USER_ID` 기준 계정 스코프로 분리 저장/조회되어, 다른 계정(다른 클라이언트)의 우회 목록이 섞이지 않습니다.
- 시험 결과 파싱 품질 리포트(문항별 근거/신뢰도/정오)는 `EXAM_QUALITY_REPORT_DIR`에 JSON으로 저장됩니다.
- 인덱스된 정답은 다음 응시에서 RAG보다 우선 적용됩니다.
- 보기 순서 변경 매핑 점검은 `PYTHONPATH=. .venv/bin/python scripts/answer_bank_shuffle_check.py`로 검증할 수 있습니다.
- `EXAM_AUTO_RETRY_MAX` 범위 내에서 자동 재응시를 수행합니다.
- `EXAM_RETRY_REQUIRES_ANSWER_INDEX=true`면 정답 인덱싱 실패 시 재응시를 중단합니다.
- `EXAM_RETRY_NO_IMPROVE_LIMIT`만큼 점수 비개선(동일/하락)이 연속되면 재응시를 조기 중단합니다.
- `EXAM_SKIP_COURSE_REMAINING_THRESHOLD=2`이면 잔여 응시 2회 이하에서 해당 과정 시험을 우회하고 다음 강좌로 이동합니다.
- `COMPLETION_MAX_COURSES`는 한 번 실행에서 자동 처리할 최대 과정 수(안전 상한)입니다.
- `수료 순서 자동(진도→시간→시험)`은 잔여 학습시간을 먼저 보충한 뒤 종합평가를 진행합니다.
- 인덱싱 시 중복 청크 제거 + `f16` 임베딩 저장 + 저장공간 상한(`RAG_STORAGE_LIMIT_GB`)을 적용합니다.

## 7) 야간 자동 러너

장시간 자동 진행/상태기록용 러너:

```bash
source .venv/bin/activate
EKHNP_USER_ID=사번 EKHNP_USER_PASSWORD=비밀번호 \
python -u overnight_runner.py \
  --target-percent 80 \
  --max-cycles 80 \
  --lessons-per-cycle 1 \
  --sleep-seconds 10 \
  --report-path logs/overnight_status_live.json
```

동작 요약:

- 진도율이 목표 미만이면 차시를 계속 진행합니다.
- 목표 도달 후에는 LLM 준비(Ollama + RAG 인덱스)를 확인합니다.
- `require_llm_before_exam` 기본값이 켜져 있어 LLM 미준비 시 응시는 보류됩니다.
