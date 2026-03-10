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
RAG_CONF_THRESHOLD=0.72
```

실행 순서(Streamlit UI):

1. `RAG 문서 폴더`에 PDF/TXT/MD 파일을 넣습니다.
2. `RAG 인덱스 생성` 버튼으로 인덱스를 만듭니다.
3. `종합평가 LLM 풀이(RAG)` 버튼으로 한 문제씩 풉니다.

참고:

- 신뢰도(`RAG 신뢰도 임계치`)보다 낮으면 자동풀이를 중단하도록 설계되었습니다.
- 신뢰도 미달 시 재질문 1회를 추가 시도합니다.
- 시험 문항 DOM 추출 실패 시 OCR 폴백을 시도합니다. (`tesseract` 설치 시 활성)
- Ollama가 실행 중이 아니면 인덱싱/풀이가 실패합니다.

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
