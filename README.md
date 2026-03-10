# e-khnp automation project

Playwright + Streamlit 기반 e-khnp 수강 자동화 도구입니다.

## 0) 현재 상태 (2026-03-10)

- 학습진도율: `100%`
- 미완료: `0개`
- 종합평가: LLM 준비 대기(현재 Ollama 미구동)
- 상태 파일: `logs/overnight_status_live.json`

## 1) macOS(집 맥미니) 실행

```bash
cd "/Users/maegbug-eeo/Documents/New project/e-khnp automation project"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

`.env`에 계정 정보를 넣습니다.

```dotenv
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
cd "/Users/maegbug-eeo/Documents/New project/e-khnp automation project"
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
- 계정 정보는 로그에 저장하지 않습니다.

## 6) 로컬 LLM RAG(무료/로그인 불필요)

종합평가 문제 자동풀이를 위해 Ollama + 로컬 RAG를 지원합니다.

준비:

```bash
# Ollama 설치 후 모델 받기
ollama pull nomic-embed-text
ollama pull qwen2.5:7b-instruct
```

`.env` 예시:

```dotenv
OLLAMA_BASE_URL=http://127.0.0.1:11434
RAG_DOCS_DIR=rag_data
RAG_INDEX_PATH=rag/index.json
RAG_EMBED_MODEL=nomic-embed-text
RAG_GENERATE_MODEL=qwen2.5:7b-instruct
RAG_TOP_K=6
RAG_CONF_THRESHOLD=0.65
RAG_CHUNK_SIZE=900
RAG_CHUNK_OVERLAP=150
RAG_MIN_CHUNK_CHARS=80
RAG_MAX_CHUNKS=50000
RAG_STORAGE_LIMIT_GB=20
RAG_PRUNE_OLD_INDEXES=true
RAG_PASS_SCORE=75
RAG_LOW_CONF_FLOOR=0.55
RAG_WEB_SEARCH_ENABLED=true
RAG_WEB_TOP_N=4
RAG_WEB_TIMEOUT_SEC=8
RAG_WEB_WEIGHT=0.35
EXAM_ANSWER_BANK_PATH=rag/exam_answer_bank.json
EXAM_AUTO_RETRY_MAX=2
EXAM_RETRY_REQUIRES_ANSWER_INDEX=true
EXAM_ATTEMPT_RESERVE=1
COMPLETION_MAX_COURSES=20
```

실행 순서(Streamlit UI):

1. `RAG 문서 폴더`에 PDF/TXT/MD 파일을 넣습니다.
2. `RAG 인덱스 생성` 버튼으로 인덱스를 만듭니다.
3. `종합평가 LLM 풀이(RAG)` 버튼으로 한 문제씩 풉니다.

참고:

- 신뢰도(`RAG 신뢰도 임계치`)보다 낮으면 자동풀이를 중단하도록 설계되었습니다.
- 신뢰도 미달 시 재질문 1회를 추가 시도합니다.
- 기본 신뢰도 임계치는 `0.65`(75점 보수 운용 기준)이며 필요 시 조정하세요.
- 보수 운용 기준은 `RAG_PASS_SCORE=75`로 설정되어 저신뢰 문항 허용량을 자동 제한합니다.
- 시험 문항 DOM 추출 실패 시 OCR 폴백을 시도합니다. (`tesseract` 설치 시 활성)
- Ollama가 실행 중이 아니면 인덱싱/풀이가 실패합니다.
- `EXAM_ATTEMPT_RESERVE=1`이면 각 강의당 마지막 1회 응시는 자동화에서 남겨둡니다.
- 문항 풀이 시 웹 검색은 항상 참조되도록 고정되어 있습니다.
- 웹 검색은 DuckDuckGo HTML 결과를 사용합니다(인터넷 연결 필요).
- `RAG_WEB_WEIGHT`는 로컬 RAG 점수와 웹 검색 점수 결합 비율입니다.
- 점수 미달 시 결과지(`item result`)에서 정답을 추출해 `EXAM_ANSWER_BANK_PATH` 인덱스에 저장합니다.
- 인덱스된 정답은 다음 응시에서 RAG보다 우선 적용됩니다.
- `EXAM_AUTO_RETRY_MAX` 범위 내에서 자동 재응시를 수행합니다.
- `EXAM_RETRY_REQUIRES_ANSWER_INDEX=true`면 정답 인덱싱 실패 시 재응시를 중단합니다.
- `COMPLETION_MAX_COURSES`는 한 번 실행에서 자동 처리할 최대 과정 수(안전 상한)입니다.
- `수료 순서 자동(진도→시험→시간)`은 종합평가를 RAG 자동풀이로 진행한 뒤 수료 판정을 확인합니다.
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
