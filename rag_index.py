from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
from pathlib import Path
from typing import Callable
from typing import Iterable
from typing import Optional
from urllib import error
from urllib import request
from urllib.parse import urlparse

try:
    from pypdf import PdfReader  # type: ignore
except Exception:  # noqa: BLE001
    PdfReader = None  # type: ignore


LogFn = Optional[Callable[[str], None]]


def _log(log_fn: LogFn, message: str) -> None:
    if log_fn:
        log_fn(message)


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _format_bytes(size: int) -> str:
    unit = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, size))
    idx = 0
    while value >= 1024 and idx < len(unit) - 1:
        value /= 1024.0
        idx += 1
    return f"{value:.2f}{unit[idx]}"


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except Exception:  # noqa: BLE001
            return 0

    total = 0
    for fp in path.rglob("*"):
        if not fp.is_file():
            continue
        try:
            total += fp.stat().st_size
        except Exception:  # noqa: BLE001
            continue
    return total


def _prune_old_index_files(index_dir: Path, preserve: set[Path], need_free: int, log_fn: LogFn = None) -> int:
    if need_free <= 0 or not index_dir.exists():
        return 0

    candidates: list[Path] = []
    for fp in index_dir.glob("*"):
        if not fp.is_file():
            continue
        if fp.resolve() in preserve:
            continue
        if fp.suffix.lower() not in {".json", ".gz"}:
            continue
        candidates.append(fp)

    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
    freed = 0
    for fp in candidates:
        try:
            size = fp.stat().st_size
        except Exception:  # noqa: BLE001
            continue
        try:
            fp.unlink()
            freed += size
            _log(log_fn, f"용량 확보를 위해 오래된 인덱스 삭제: {fp.name} ({_format_bytes(size)})")
        except Exception:  # noqa: BLE001
            continue
        if freed >= need_free:
            break
    return freed


def _pack_embedding_f16(values: list[float]) -> str:
    if not values:
        return ""
    fmt = "<" + ("e" * len(values))
    packed = struct.pack(fmt, *values)
    return base64.b64encode(packed).decode("ascii")


def _is_safe_http_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class OllamaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11434") -> None:
        self.base_url = base_url.rstrip("/")
        if not _is_safe_http_url(self.base_url):
            raise ValueError(f"Ollama base URL must be http/https: {base_url}")

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
            with request.urlopen(req, timeout=120) as resp:  # noqa: S310  # nosec B310
                body = json.loads(resp.read().decode("utf-8"))
        except error.URLError as exc:
            raise RuntimeError(f"Ollama embedding call failed: {exc}") from exc
        emb = body.get("embedding")
        if not isinstance(emb, list) or not emb:
            raise RuntimeError("Invalid embedding response from Ollama")
        return [float(x) for x in emb]


def _chunk_text(text: str, chunk_size: int = 900, overlap: int = 150) -> list[str]:
    src = _normalize_text(text)
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
    min_chunk_chars: int = 80,
    max_chunks: int = 50000,
    max_total_size_gb: float = 20.0,
    prune_old_indexes: bool = True,
    embedding_dtype: str = "f16",
    ollama_base_url: str = "http://127.0.0.1:11434",
    log_fn: LogFn = None,
) -> dict[str, int | str | float]:
    src_dir = Path(docs_dir)
    if not src_dir.exists():
        raise FileNotFoundError(f"docs_dir not found: {docs_dir}")

    files = list(_iter_source_files(src_dir))
    if not files:
        raise RuntimeError("No .txt/.md/.pdf files found in docs_dir")

    safe_chunk_size = max(200, int(chunk_size))
    safe_overlap = max(0, min(int(overlap), safe_chunk_size - 1))
    safe_min_chunk_chars = max(20, min(int(min_chunk_chars), safe_chunk_size))
    safe_max_chunks = max(100, int(max_chunks))
    emb_dtype = (embedding_dtype or "f16").lower().strip()
    if emb_dtype not in {"f16", "fp32"}:
        emb_dtype = "f16"

    client = OllamaClient(base_url=ollama_base_url)
    chunks: list[dict[str, object]] = []
    sources: list[str] = []
    source_to_sid: dict[str, int] = {}
    seen_text_hash: set[str] = set()

    skipped_short = 0
    skipped_dup = 0
    reached_limit = False
    _log(log_fn, f"RAG 인덱스 빌드 시작: files={len(files)}, max_chunks={safe_max_chunks}")

    for file_idx, fp in enumerate(files, start=1):
        _log(log_fn, f"[{file_idx}/{len(files)}] 처리: {fp}")
        text = _read_pdf(fp) if fp.suffix.lower() == ".pdf" else _read_text(fp)
        if not text.strip():
            _log(log_fn, f"  - 텍스트 없음, 스킵: {fp.name}")
            continue

        source_key = str(fp)
        if source_key not in source_to_sid:
            source_to_sid[source_key] = len(sources)
            sources.append(source_key)
        sid = source_to_sid[source_key]

        parts = _chunk_text(text, chunk_size=safe_chunk_size, overlap=safe_overlap)
        for idx, part in enumerate(parts):
            clean = _normalize_text(part)
            if len(clean) < safe_min_chunk_chars:
                skipped_short += 1
                continue

            h = hashlib.sha1(clean.encode("utf-8"), usedforsecurity=False).hexdigest()
            if h in seen_text_hash:
                skipped_dup += 1
                continue
            seen_text_hash.add(h)

            emb = client.embed(embed_model, clean)
            norm = math.sqrt(sum(x * x for x in emb)) or 1.0
            chunk: dict[str, object] = {
                "id": f"{fp.name}#{idx}",
                "sid": sid,
                "text": clean,
                "norm": norm,
            }
            if emb_dtype == "f16":
                chunk["emb_f16"] = _pack_embedding_f16(emb)
                chunk["dim"] = len(emb)
            else:
                chunk["embedding"] = [round(float(x), 6) for x in emb]
            chunks.append(chunk)

            if len(chunks) >= safe_max_chunks:
                reached_limit = True
                break
        if reached_limit:
            _log(log_fn, f"최대 청크 수({safe_max_chunks})에 도달해 인덱싱을 종료합니다.")
            break

    if not chunks:
        raise RuntimeError("No chunks were indexed")

    payload: dict[str, object] = {
        "meta": {
            "embed_model": embed_model,
            "chunk_size": safe_chunk_size,
            "overlap": safe_overlap,
            "min_chunk_chars": safe_min_chunk_chars,
            "max_chunks": safe_max_chunks,
            "embedding_dtype": emb_dtype,
            "ollama_base_url": ollama_base_url,
            "skipped_short": skipped_short,
            "skipped_duplicate": skipped_dup,
        },
        "sources": sources,
        "chunks": chunks,
    }

    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    serialized_bytes = serialized.encode("utf-8")

    out = Path(index_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    old_out_bytes = out.stat().st_size if out.exists() else 0

    docs_bytes = _dir_size_bytes(src_dir)
    index_bytes = _dir_size_bytes(out.parent)
    predicted_total = docs_bytes + index_bytes - old_out_bytes + len(serialized_bytes)
    limit_bytes = int(max(1.0, float(max_total_size_gb)) * (1024**3))

    if predicted_total > limit_bytes and prune_old_indexes:
        need_free = predicted_total - limit_bytes
        freed = _prune_old_index_files(
            out.parent,
            preserve={out.resolve()},
            need_free=need_free,
            log_fn=log_fn,
        )
        if freed > 0:
            index_bytes = _dir_size_bytes(out.parent)
            predicted_total = docs_bytes + index_bytes - old_out_bytes + len(serialized_bytes)

    if predicted_total > limit_bytes:
        raise RuntimeError(
            "용량 상한 초과 예상: "
            f"예상={_format_bytes(predicted_total)} > 제한={_format_bytes(limit_bytes)}. "
            "RAG_CHUNK_SIZE↑, RAG_MAX_CHUNKS↓, 문서 정리로 용량을 줄여주세요."
        )

    out.write_bytes(serialized_bytes)
    _log(
        log_fn,
        "RAG 인덱스 저장 완료: "
        f"{out} (chunks={len(chunks)}, size={_format_bytes(len(serialized_bytes))}, "
        f"pred_total={_format_bytes(predicted_total)}/{_format_bytes(limit_bytes)})",
    )
    return {
        "files": len(files),
        "chunks": len(chunks),
        "index_path": str(out),
        "index_size_mb": round(len(serialized_bytes) / (1024**2), 2),
        "predicted_total_gb": round(predicted_total / (1024**3), 3),
        "storage_limit_gb": float(max_total_size_gb),
    }
