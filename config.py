from dataclasses import dataclass
import os
from pathlib import Path


def _load_dotenv_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


_load_dotenv_file()


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class Settings:
    base_url: str = os.getenv("EKHNP_BASE_URL", "https://e-khnp.com")
    login_url: str = os.getenv("EKHNP_LOGIN_URL", "https://e-khnp.com")
    user_id: str = os.getenv("EKHNP_USER_ID", "")
    user_password: str = os.getenv("EKHNP_USER_PASSWORD", "")
    headless: bool = os.getenv("EKHNP_HEADLESS", "true").lower() == "true"
    timeout_ms: int = int(os.getenv("EKHNP_TIMEOUT_MS", "20000"))
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    rag_docs_dir: str = os.getenv("RAG_DOCS_DIR", "rag_data")
    rag_index_path: str = os.getenv("RAG_INDEX_PATH", "rag/index.json")
    rag_embed_model: str = os.getenv("RAG_EMBED_MODEL", "nomic-embed-text")
    rag_generate_model: str = os.getenv("RAG_GENERATE_MODEL", "qwen2.5:7b-instruct")
    rag_top_k: int = int(os.getenv("RAG_TOP_K", "6"))
    rag_conf_threshold: float = float(os.getenv("RAG_CONF_THRESHOLD", "0.65"))
    rag_chunk_size: int = int(os.getenv("RAG_CHUNK_SIZE", "900"))
    rag_chunk_overlap: int = int(os.getenv("RAG_CHUNK_OVERLAP", "150"))
    rag_min_chunk_chars: int = int(os.getenv("RAG_MIN_CHUNK_CHARS", "80"))
    rag_max_chunks: int = int(os.getenv("RAG_MAX_CHUNKS", "50000"))
    rag_storage_limit_gb: float = float(os.getenv("RAG_STORAGE_LIMIT_GB", "20"))
    rag_prune_old_indexes: bool = _env_bool("RAG_PRUNE_OLD_INDEXES", "true")
    rag_pass_score: int = int(os.getenv("RAG_PASS_SCORE", "75"))
    rag_low_conf_floor: float = float(os.getenv("RAG_LOW_CONF_FLOOR", "0.55"))
    rag_web_search_enabled: bool = _env_bool("RAG_WEB_SEARCH_ENABLED", "true")
    rag_web_top_n: int = int(os.getenv("RAG_WEB_TOP_N", "4"))
    rag_web_timeout_sec: int = int(os.getenv("RAG_WEB_TIMEOUT_SEC", "8"))
    rag_web_weight: float = float(os.getenv("RAG_WEB_WEIGHT", "0.35"))
    exam_answer_bank_path: str = os.getenv("EXAM_ANSWER_BANK_PATH", "rag/exam_answer_bank.json")
    exam_auto_retry_max: int = int(os.getenv("EXAM_AUTO_RETRY_MAX", "2"))
    exam_retry_requires_answer_index: bool = _env_bool("EXAM_RETRY_REQUIRES_ANSWER_INDEX", "true")
    exam_attempt_reserve: int = int(os.getenv("EXAM_ATTEMPT_RESERVE", "1"))
    completion_max_courses: int = int(os.getenv("COMPLETION_MAX_COURSES", "20"))
