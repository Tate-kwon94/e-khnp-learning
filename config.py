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
