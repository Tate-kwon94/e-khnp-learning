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
    rag_conf_threshold: float = float(os.getenv("RAG_CONF_THRESHOLD", "0.72"))
