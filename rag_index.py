from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib import error, request

try:
    from pypdf import PdfReader  # type: ignore
except Exception:  # noqa: BLE001
    PdfReader = None  # type: ignore


LogFn = Optional[Callable[[str], None]]


def _log(log_fn: LogFn, message: str) -> None:
    if log_fn:
        log_fn(message)


class OllamaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11434") -> None:
        self.base_url = base_url.rstrip("/")

    def embed(self, model: str, text: str) -> list[float]:
        payload = {"model": model, "prompt": text}
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/embeddings",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=120) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
        except error.URLError as exc:
            raise RuntimeError(f"Ollama embedding call failed: {exc}") from exc
        emb = body.get("embedding")
        if not isinstance(emb, list) or not emb:
            raise RuntimeError("Invalid embedding response from Ollama")
        return [float(x) for x in emb]


def _chunk_text(text: str, chunk_size: int = 900, overlap: int = 150) -> list[str]:
    src = " ".join(text.split())
    if len(src) <= chunk_size:
        return [src] if src else []
    out: list[str] = []
    step = max(1, chunk_size - overlap)
    i = 0
    while i < len(src):
        part = src[i : i + chunk_size].strip()
        if part:
            out.append(part)
        if i + chunk_size >= len(src):
            break
        i += step
    return out


def _read_pdf(path: Path) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception:  # noqa: BLE001
        return ""
    parts: list[str] = []
    for pg in reader.pages:
        try:
            txt = pg.extract_text() or ""
        except Exception:  # noqa: BLE001
            txt = ""
        if txt.strip():
            parts.append(txt)
    return "\n".join(parts)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        try:
            return path.read_text(encoding="cp949")
        except Exception:  # noqa: BLE001
            return ""


def _iter_source_files(docs_dir: Path) -> Iterable[Path]:
    exts = {".txt", ".md", ".pdf"}
    for p in sorted(docs_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts:
            yield p


def build_rag_index(
    docs_dir: str,
    index_path: str,
    embed_model: str = "nomic-embed-text",
    chunk_size: int = 900,
    overlap: int = 150,
    ollama_base_url: str = "http://127.0.0.1:11434",
    log_fn: LogFn = None,
) -> dict[str, int | str]:
    src_dir = Path(docs_dir)
    if not src_dir.exists():
        raise FileNotFoundError(f"docs_dir not found: {docs_dir}")

    files = list(_iter_source_files(src_dir))
    if not files:
        raise RuntimeError("No .txt/.md/.pdf files found in docs_dir")

    client = OllamaClient(base_url=ollama_base_url)
    chunks: list[dict[str, object]] = []
    _log(log_fn, f"RAG 인덱스 빌드 시작: files={len(files)}")

    for file_idx, fp in enumerate(files, start=1):
        _log(log_fn, f"[{file_idx}/{len(files)}] 처리: {fp}")
        text = _read_pdf(fp) if fp.suffix.lower() == ".pdf" else _read_text(fp)
        if not text.strip():
            _log(log_fn, f"  - 텍스트 없음, 스킵: {fp.name}")
            continue
        parts = _chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        for idx, part in enumerate(parts):
            emb = client.embed(embed_model, part)
            norm = math.sqrt(sum(x * x for x in emb)) or 1.0
            chunks.append(
                {
                    "id": f"{fp.name}#{idx}",
                    "source": str(fp),
                    "text": part,
                    "embedding": emb,
                    "norm": norm,
                }
            )

    if not chunks:
        raise RuntimeError("No chunks were indexed")

    payload: dict[str, object] = {
        "meta": {
            "embed_model": embed_model,
            "chunk_size": chunk_size,
            "overlap": overlap,
            "ollama_base_url": ollama_base_url,
        },
        "chunks": chunks,
    }

    out = Path(index_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _log(log_fn, f"RAG 인덱스 저장 완료: {out} (chunks={len(chunks)})")
    return {"files": len(files), "chunks": len(chunks), "index_path": str(out)}
