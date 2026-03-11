from __future__ import annotations

import ast
import base64
import html
import json
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Optional
from urllib import error, request
from urllib.parse import parse_qs, quote_plus, unquote, urlparse


@dataclass
class SolveResult:
    choice: int
    confidence: float
    reason: str
    evidence_ids: list[str]


def _is_safe_http_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class OllamaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11434") -> None:
        normalized = base_url.rstrip("/")
        if normalized.endswith("/v1"):
            normalized = normalized[:-3].rstrip("/")
        self.base_url = normalized
        if not _is_safe_http_url(self.base_url):
            raise ValueError(f"Ollama base URL must be http/https: {base_url}")

    def _request_json(
        self,
        *,
        path: str,
        payload: dict[str, Any],
        timeout: int,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # nosec B310
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="ignore").strip()
            except Exception:  # noqa: BLE001
                body = ""
            if allow_404 and int(getattr(exc, "code", 0)) == 404:
                return None
            detail = f"HTTP {getattr(exc, 'code', '?')} {getattr(exc, 'reason', '')}".strip()
            if body:
                detail += f" / body={body[:240]}"
            raise RuntimeError(f"Ollama API call failed ({path}): {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Ollama API call failed ({path}): {exc}") from exc

    @staticmethod
    def _model_candidates(model: str) -> list[str]:
        src = str(model or "").strip()
        if not src:
            return []
        out = [src]
        lowered = src.lower()
        if "-instruct" in lowered:
            out.append(src.replace("-instruct", ""))
        if ":" in src:
            head, tail = src.split(":", 1)
            if "-instruct" in tail:
                out.append(f"{head}:{tail.replace('-instruct', '')}")
        seen: set[str] = set()
        deduped: list[str] = []
        for item in out:
            key = item.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped

    def embed(self, model: str, text: str) -> list[float]:
        primary_payload = {"model": model, "prompt": text}
        body = self._request_json(path="/api/embeddings", payload=primary_payload, timeout=120, allow_404=True)
        if isinstance(body, dict):
            emb = body.get("embedding")
            if isinstance(emb, list) and emb:
                return [float(x) for x in emb]

        # Newer/alternate Ollama route
        fallback_payload = {"model": model, "input": text}
        fb = self._request_json(path="/api/embed", payload=fallback_payload, timeout=120, allow_404=False)
        embeddings = fb.get("embeddings") if isinstance(fb, dict) else None
        if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
            return [float(x) for x in embeddings[0]]
        raise RuntimeError("Invalid embedding response from Ollama (/api/embeddings, /api/embed 모두 실패)")

    def generate(self, model: str, prompt: str, temperature: float = 0.1) -> str:
        tried_models: list[str] = []
        for candidate_model in self._model_candidates(model):
            tried_models.append(candidate_model)
            payload = {
                "model": candidate_model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": temperature},
            }
            body = self._request_json(path="/api/generate", payload=payload, timeout=180, allow_404=True)
            if isinstance(body, dict):
                txt = body.get("response")
                if isinstance(txt, str) and txt.strip():
                    return txt

            # Fallback for environments exposing chat-style generation
            chat_payload = {
                "model": candidate_model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": temperature},
            }
            chat = self._request_json(path="/api/chat", payload=chat_payload, timeout=180, allow_404=True)
            if isinstance(chat, dict):
                msg = chat.get("message")
                if isinstance(msg, dict):
                    txt = msg.get("content")
                    if isinstance(txt, str) and txt.strip():
                        return txt

        tried = ", ".join(tried_models) if tried_models else str(model)
        raise RuntimeError(
            "Invalid generate response from Ollama "
            f"(/api/generate, /api/chat 모두 실패, tried_models=[{tried}])"
        )


class RagExamSolver:
    def __init__(
        self,
        index_path: str,
        generate_model: str = "qwen2.5:7b-instruct",
        embed_model: Optional[str] = None,
        ollama_base_url: str = "http://127.0.0.1:11434",
        web_search_enabled: bool = True,
        web_top_n: int = 4,
        web_timeout_sec: int = 8,
        web_weight: float = 0.35,
    ) -> None:
        src = Path(index_path)
        if not src.exists():
            raise FileNotFoundError(f"RAG index not found: {index_path}")
        raw = json.loads(src.read_text(encoding="utf-8"))
        chunks = raw.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise RuntimeError("RAG index has no chunks")
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        sources = raw.get("sources")
        self.sources = [str(x) for x in sources] if isinstance(sources, list) else []
        self.embed_model = embed_model or str(meta.get("embed_model") or "nomic-embed-text")
        self.generate_model = generate_model
        self.client = OllamaClient(base_url=ollama_base_url)
        self.web_search_enabled = bool(web_search_enabled)
        self.web_top_n = max(1, min(int(web_top_n), 8))
        self.web_timeout_sec = max(3, min(int(web_timeout_sec), 20))
        self.web_weight = max(0.0, min(float(web_weight), 0.8))
        self._embed_cache: dict[str, list[float]] = {}
        self._web_cache: dict[str, list[dict[str, str]]] = {}
        self._chunk_views: list[dict[str, Any]] = []
        self._idf: dict[str, float] = {}
        self._prepare_chunk_views(chunks)

    _TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
    _STOPWORDS = {
        "그리고",
        "하지만",
        "또는",
        "대한",
        "관련",
        "것은",
        "문항",
        "문제",
        "다음",
        "정답",
        "선택",
        "보기",
        "이다",
        "하는",
        "한다",
        "있다",
        "없다",
        "에서",
        "으로",
        "하며",
        "대한민국",
        "khnp",
    }
    _NEGATIVE_HINTS = [
        "아닌 것은",
        "아닌것",
        "옳지 않은",
        "틀린 것은",
        "틀린것",
        "부적절한",
        "거리가 먼",
        "해당하지 않는",
        "not",
        "except",
    ]

    @staticmethod
    def _clean_html_text(src: str) -> str:
        text = re.sub(r"<script.*?>.*?</script>", " ", src, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<style.*?>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _is_negative_question(cls, question: str) -> bool:
        q = (question or "").lower()
        return any(h in q for h in cls._NEGATIVE_HINTS)

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        tokens = [t.lower() for t in cls._TOKEN_RE.findall(text or "")]
        return [t for t in tokens if len(t) >= 2 and t not in cls._STOPWORDS]

    def _prepare_chunk_views(self, chunks: list[dict[str, object]]) -> None:
        df: dict[str, int] = {}
        views: list[dict[str, Any]] = []
        for ch in chunks:
            vec = self._extract_embedding(ch)
            if not vec:
                continue
            norm = float(ch.get("norm") or 0.0)
            if norm <= 0:
                norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            text = str(ch.get("text", "") or "")
            tokens = set(self._tokenize(text))
            for tok in tokens:
                df[tok] = df.get(tok, 0) + 1
            views.append({"chunk": ch, "vec": vec, "norm": norm, "tokens": tokens})

        if not views:
            raise RuntimeError("RAG index has no valid chunk vectors")

        n_docs = len(views)
        self._idf = {tok: math.log((n_docs + 1.0) / (freq + 1.0)) + 1.0 for tok, freq in df.items()}
        self._chunk_views = views

    @staticmethod
    def _decode_emb_f16(data: str, dim: int) -> list[float]:
        if not data:
            return []
        raw = base64.b64decode(data.encode("ascii"))
        if dim <= 0:
            dim = len(raw) // 2
        if dim <= 0:
            return []
        need = dim * 2
        if len(raw) < need:
            return []
        fmt = "<" + ("e" * dim)
        vals = struct.unpack(fmt, raw[:need])
        return [float(x) for x in vals]

    def _extract_embedding(self, chunk: dict[str, object]) -> list[float]:
        emb = chunk.get("embedding")
        if isinstance(emb, list) and emb:
            return [float(x) for x in emb]
        emb_f16 = chunk.get("emb_f16")
        if isinstance(emb_f16, str) and emb_f16:
            try:
                dim = int(chunk.get("dim") or 0)
            except Exception:  # noqa: BLE001
                dim = 0
            try:
                return self._decode_emb_f16(emb_f16, dim)
            except Exception:  # noqa: BLE001
                return []
        return []

    def _chunk_source(self, chunk: dict[str, object]) -> str:
        source = chunk.get("source")
        if isinstance(source, str) and source:
            return source
        sid = chunk.get("sid")
        try:
            idx = int(sid)
        except Exception:  # noqa: BLE001
            idx = -1
        if 0 <= idx < len(self.sources):
            return self.sources[idx]
        return ""

    def _embed(self, text: str) -> list[float]:
        key = " ".join((text or "").split())
        if key in self._embed_cache:
            return self._embed_cache[key]
        emb = self.client.embed(self.embed_model, key)
        self._embed_cache[key] = emb
        return emb

    @staticmethod
    def _cosine(q_vec: list[float], q_norm: float, d_vec: list[float], d_norm: float) -> float:
        if q_norm <= 0 or d_norm <= 0:
            return 0.0
        dot = sum(a * b for a, b in zip(q_vec, d_vec))
        return dot / (q_norm * d_norm)

    def _coverage_score(self, query_tokens: list[str], doc_tokens: set[str]) -> float:
        if not query_tokens or not doc_tokens:
            return 0.0
        qset = set(query_tokens)
        matched = qset & doc_tokens
        if not matched:
            return 0.0
        den = sum(self._idf.get(tok, 1.0) for tok in qset) or 1.0
        num = sum(self._idf.get(tok, 1.0) for tok in matched)
        return max(0.0, min(1.0, num / den))

    def _search_web(self, question: str, options: list[str]) -> list[dict[str, str]]:
        if not self.web_search_enabled:
            return []

        query = f"\"{question}\" 정답"
        key = re.sub(r"\s+", " ", query).strip().lower()
        if key in self._web_cache:
            return self._web_cache[key]

        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        req = request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            },
            method="GET",
        )

        hits: list[dict[str, str]] = []
        try:
            with request.urlopen(req, timeout=self.web_timeout_sec) as resp:  # noqa: S310  # nosec B310
                page_html = resp.read().decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            self._web_cache[key] = []
            return []

        title_matches = re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            page_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        snippet_matches = re.findall(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            page_html,
            flags=re.IGNORECASE | re.DOTALL,
        )

        q_tokens = set(self._tokenize(question))
        for idx, (raw_href, raw_title) in enumerate(title_matches):
            if len(hits) >= self.web_top_n:
                break

            href = html.unescape(raw_href)
            if href.startswith("//"):
                href = "https:" + href
            if "duckduckgo.com/l/?" in href:
                try:
                    qd = parse_qs(urlparse(href).query)
                    decoded = unquote(qd.get("uddg", [""])[0])
                    if decoded:
                        href = decoded
                except Exception:  # noqa: BLE001
                    pass
            if not _is_safe_http_url(href):
                continue

            title = self._clean_html_text(raw_title)
            snippet_raw = snippet_matches[idx] if idx < len(snippet_matches) else ""
            snippet = self._clean_html_text(snippet_raw)
            text = f"{title} {snippet}".strip()
            if len(text) < 20:
                continue

            # 문제 토큰과의 최소 교집합이 없는 결과는 제외하여 잡음을 줄입니다.
            hit_tokens = set(self._tokenize(text))
            if q_tokens and not (q_tokens & hit_tokens):
                continue

            hits.append({"id": f"web#{len(hits) + 1}", "source": href, "text": text[:700]})

        self._web_cache[key] = hits
        if hits:
            return hits

        # 2차 질의: 문제 + 보기 일부로 재검색
        alt_query = f"{question} {options[0] if options else ''} 정답"
        alt_key = re.sub(r"\s+", " ", alt_query).strip().lower()
        if alt_key in self._web_cache:
            return self._web_cache[alt_key]
        alt_url = f"https://duckduckgo.com/html/?q={quote_plus(alt_query)}"
        alt_req = request.Request(
            alt_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            },
            method="GET",
        )
        try:
            with request.urlopen(alt_req, timeout=self.web_timeout_sec) as resp:  # noqa: S310  # nosec B310
                alt_html = resp.read().decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            self._web_cache[alt_key] = []
            return []

        alt_titles = re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            alt_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        alt_snippets = re.findall(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            alt_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        alt_hits: list[dict[str, str]] = []
        for idx, (raw_href, raw_title) in enumerate(alt_titles):
            if len(alt_hits) >= self.web_top_n:
                break
            href = html.unescape(raw_href)
            if href.startswith("//"):
                href = "https:" + href
            if "duckduckgo.com/l/?" in href:
                try:
                    qd = parse_qs(urlparse(href).query)
                    decoded = unquote(qd.get("uddg", [""])[0])
                    if decoded:
                        href = decoded
                except Exception:  # noqa: BLE001
                    pass
            if not _is_safe_http_url(href):
                continue
            title = self._clean_html_text(raw_title)
            snippet = self._clean_html_text(alt_snippets[idx] if idx < len(alt_snippets) else "")
            text = f"{title} {snippet}".strip()
            if len(text) < 20:
                continue
            alt_hits.append({"id": f"web#{len(alt_hits) + 1}", "source": href, "text": text[:700]})

        self._web_cache[alt_key] = alt_hits
        return alt_hits

    @staticmethod
    def _combine_scores(local_scores: list[float], web_scores: list[float], web_weight: float) -> list[float]:
        if not local_scores:
            return []
        if not web_scores or len(web_scores) != len(local_scores):
            return list(local_scores)
        w = max(0.0, min(float(web_weight), 0.8))
        return [(1.0 - w) * l + w * wv for l, wv in zip(local_scores, web_scores)]

    @staticmethod
    def _pick_choice_from_scores(scores: list[float], negative: bool) -> tuple[int, float]:
        if not scores:
            return 1, 0.0
        picked = [1.0 - s for s in scores] if negative else list(scores)
        best_idx = 1
        best_score = -1.0
        for i, sc in enumerate(picked, start=1):
            if sc > best_score:
                best_score = sc
                best_idx = i

        sorted_scores = sorted(picked, reverse=True)
        top = sorted_scores[0] if sorted_scores else 0.0
        second = sorted_scores[1] if len(sorted_scores) > 1 else 0.0
        margin = max(0.0, top - second)
        confidence = min(0.97, 0.36 + 0.42 * top + 0.52 * margin)
        return best_idx, confidence

    def _retrieve(self, question: str, options: list[str], top_k: int = 6) -> list[dict[str, Any]]:
        option_lines = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, start=1))
        main_query = f"{question}\n{option_lines}"
        q_tokens_main = self._tokenize(main_query)

        q_vec_main = self._embed(main_query)
        q_norm_main = math.sqrt(sum(x * x for x in q_vec_main)) or 1.0

        q_vec_question = self._embed(question)
        q_norm_question = math.sqrt(sum(x * x for x in q_vec_question)) or 1.0

        option_queries = [f"{question}\n{opt}" for opt in options]
        option_tokens = [self._tokenize(opt) for opt in options]
        option_vecs = [self._embed(q) for q in option_queries]
        option_norms = [math.sqrt(sum(x * x for x in vec)) or 1.0 for vec in option_vecs]

        scored: list[dict[str, Any]] = []
        for view in self._chunk_views:
            vec = view["vec"]
            norm = float(view["norm"])
            tokens: set[str] = view["tokens"]

            dense_main = (self._cosine(q_vec_main, q_norm_main, vec, norm) + 1.0) / 2.0
            dense_q = (self._cosine(q_vec_question, q_norm_question, vec, norm) + 1.0) / 2.0
            dense_opt_best = 0.0
            for i in range(len(option_vecs)):
                dense_opt_best = max(
                    dense_opt_best,
                    (self._cosine(option_vecs[i], option_norms[i], vec, norm) + 1.0) / 2.0,
                )

            lex_main = self._coverage_score(q_tokens_main, tokens)
            lex_opt_best = 0.0
            for toks in option_tokens:
                lex_opt_best = max(lex_opt_best, self._coverage_score(toks, tokens))

            dense_score = 0.55 * dense_main + 0.25 * dense_q + 0.20 * dense_opt_best
            lex_score = 0.70 * lex_main + 0.30 * lex_opt_best
            hybrid_score = 0.72 * dense_score + 0.28 * lex_score

            scored.append(
                {
                    "chunk": view["chunk"],
                    "vec": vec,
                    "norm": norm,
                    "tokens": tokens,
                    "dense": dense_score,
                    "lex": lex_score,
                    "score": hybrid_score,
                }
            )

        scored.sort(key=lambda x: float(x["score"]), reverse=True)
        return scored[: max(1, top_k)]

    @staticmethod
    def _parse_model_json(text: str) -> Optional[dict[str, object]]:
        cleaned = text.strip()
        code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL)
        if code_block:
            cleaned = code_block.group(1).strip()
        if not cleaned.startswith("{"):
            brace = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if brace:
                cleaned = brace.group(0)

        normalized = (
            cleaned.replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2018", "'")
            .replace("\u2019", "'")
        )
        normalized = re.sub(r",\s*([}\]])", r"\1", normalized)

        candidates = [cleaned, normalized]
        seen: set[str] = set()
        for candidate in candidates:
            src = candidate.strip()
            if not src or src in seen:
                continue
            seen.add(src)
            try:
                obj = json.loads(src)
                if isinstance(obj, dict):
                    return obj
            except Exception:  # noqa: BLE001
                pass
            try:
                obj = ast.literal_eval(src)
                if isinstance(obj, dict):
                    return obj
            except Exception:  # noqa: BLE001
                continue
        return None

    def _score_options(self, question: str, options: list[str], contexts: list[dict[str, Any]]) -> list[float]:
        if not options:
            return []

        query_texts = [f"{question}\n{opt}" for opt in options]
        query_tokens = [self._tokenize(qt) for qt in query_texts]
        query_vecs = [self._embed(qt) for qt in query_texts]
        query_norms = [math.sqrt(sum(x * x for x in vec)) or 1.0 for vec in query_vecs]

        scores: list[float] = []
        for idx in range(len(options)):
            total_weight = 0.0
            acc = 0.0
            for rank, ctx in enumerate(contexts):
                weight = 1.0 / (1.0 + rank * 0.30)
                total_weight += weight

                dense = (self._cosine(query_vecs[idx], query_norms[idx], ctx["vec"], float(ctx["norm"])) + 1.0) / 2.0
                lex = self._coverage_score(query_tokens[idx], ctx["tokens"])
                ctx_score = float(ctx.get("score", 0.0))

                local = 0.60 * dense + 0.25 * lex + 0.15 * ctx_score
                acc += weight * local
            scores.append((acc / total_weight) if total_weight > 0 else 0.0)

        return scores

    def _score_options_from_web_hits(
        self, question: str, options: list[str], web_hits: list[dict[str, str]]
    ) -> list[float]:
        if not options:
            return []
        if not web_hits:
            return [0.0 for _ in options]

        hit_tokens = [set(self._tokenize(hit.get("text", ""))) for hit in web_hits]
        scores: list[float] = []
        for idx, opt in enumerate(options):
            q_tokens = self._tokenize(f"{question} {opt}")
            if not q_tokens:
                scores.append(0.0)
                continue
            acc = 0.0
            total_w = 0.0
            for rank, ht in enumerate(hit_tokens):
                w = 1.0 / (1.0 + rank * 0.35)
                total_w += w
                overlap = self._coverage_score(q_tokens, ht)
                acc += w * overlap
            scores.append((acc / total_w) if total_w > 0 else 0.0)
        return scores

    def solve(
        self,
        question: str,
        options: list[str],
        top_k: int = 6,
        exclude_evidence_ids: Optional[list[str]] = None,
    ) -> SolveResult:
        if not question.strip():
            raise RuntimeError("Empty question text")
        if len(options) < 2:
            raise RuntimeError("Need at least 2 options")

        option_lines = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, start=1))
        excluded_ids = {str(x).strip() for x in (exclude_evidence_ids or []) if str(x).strip()}
        retrieve_k = max(1, int(top_k) + len(excluded_ids))
        contexts = self._retrieve(question=question, options=options, top_k=retrieve_k)
        if not contexts:
            raise RuntimeError("No retrieved contexts")
        if excluded_ids:
            filtered_contexts = [
                c for c in contexts if str(c["chunk"].get("id", "")).strip() not in excluded_ids
            ]
            if filtered_contexts:
                contexts = filtered_contexts
            contexts = contexts[: max(1, int(top_k))]
            if not contexts:
                raise RuntimeError("No retrieved contexts after evidence exclusion")

        local_scores = self._score_options(question, options, contexts)
        web_hits = self._search_web(question=question, options=options)
        if excluded_ids:
            web_hits = [h for h in web_hits if str(h.get("id", "")).strip() not in excluded_ids]
        web_scores = self._score_options_from_web_hits(question=question, options=options, web_hits=web_hits)
        option_scores = self._combine_scores(local_scores=local_scores, web_scores=web_scores, web_weight=self.web_weight)
        negative_question = self._is_negative_question(question)
        det_choice, det_conf = self._pick_choice_from_scores(option_scores, negative=negative_question)

        evidence_lines = []
        for idx, c in enumerate(contexts, start=1):
            chunk = c["chunk"]
            src = self._chunk_source(chunk)
            evidence_lines.append(
                f"[{idx}] id={chunk.get('id')} source={src} score={float(c.get('score', 0.0)):.3f}\n"
                f"{chunk.get('text')}"
            )
        offset = len(evidence_lines)
        for widx, hit in enumerate(web_hits, start=1):
            evidence_lines.append(
                f"[{offset + widx}] id={hit.get('id')} source={hit.get('source')}\n"
                f"{hit.get('text')}"
            )
        evidence_block = "\n\n".join(evidence_lines)
        option_score_lines = "\n".join(
            (
                f"{i}. local={local_scores[i - 1]:.3f} "
                f"web={web_scores[i - 1]:.3f} "
                f"combined={option_scores[i - 1]:.3f} "
                f"option={options[i - 1]}"
            )
            for i in range(1, len(options) + 1)
        )
        q_type = "부정형(아닌/틀린/거리가 먼 유형)" if negative_question else "일반형"
        web_rule = "반드시 웹검색 근거를 우선 참조하고, 로컬 근거와 교차검증"
        prompt = (
            "당신은 객관식 문제 풀이 도우미다.\n"
            "반드시 근거 문서(웹검색+로컬)를 기반으로 답하라. 근거가 약하면 confidence를 낮춰라.\n"
            "웹검색 스니펫을 반드시 참조하고 로컬 근거와 충돌 여부를 확인하라.\n"
            "문항이 부정형이면(아닌/틀린/거리가 먼) 정답은 일반적으로 '근거와 가장 덜 일치하는' 선지다.\n"
            f"규칙: {web_rule}\n"
            "출력은 JSON 한 개만:\n"
            '{"choice": <1-5 정수>, "confidence": <0~1>, "reason": "<짧은 근거>", "evidence_ids": ["id1","id2"]}\n\n'
            f"문제:\n{question}\n\n"
            f"문항 유형:\n{q_type}\n\n"
            f"선지:\n{option_lines}\n\n"
            f"선지별 검색 점수(참고):\n{option_score_lines}\n\n"
            f"근거 문서:\n{evidence_block}\n"
        )
        raw = self.client.generate(self.generate_model, prompt, temperature=0.1)
        parsed = self._parse_model_json(raw)

        if parsed is None:
            fallback_eids = [str(c["chunk"].get("id", "")) for c in contexts[:2] if c["chunk"].get("id")]
            if web_hits:
                fallback_eids.append(str(web_hits[0].get("id", "")))
            fallback_eids = [x for x in fallback_eids if x]
            fallback_conf = round(max(0.45, det_conf) + 1e-9, 2)
            return SolveResult(
                choice=det_choice,
                confidence=fallback_conf,
                reason="LLM JSON 파싱 실패, 하이브리드 검색 점수 폴백",
                evidence_ids=fallback_eids,
            )

        choice = int(parsed.get("choice", 0) or 0)
        model_conf = float(parsed.get("confidence", 0.0) or 0.0)
        model_conf = max(0.0, min(1.0, model_conf))
        reason = str(parsed.get("reason", "") or "")
        eids_raw = parsed.get("evidence_ids", [])
        evidence_ids = [str(x) for x in eids_raw] if isinstance(eids_raw, list) else []

        if choice < 1 or choice > len(options):
            choice = det_choice
            model_conf = min(model_conf, det_conf)
            if not reason:
                reason = "선택지 번호 비정상으로 하이브리드 폴백 적용"

        final_choice = choice
        final_conf = model_conf
        if choice == det_choice:
            final_conf = max(model_conf, det_conf)
        elif det_conf >= model_conf + 0.10:
            final_choice = det_choice
            final_conf = det_conf
            reason = f"LLM-검색 불일치로 근거 점수 우세 선택지를 채택. {reason}".strip()
        else:
            final_conf = min(1.0, max(model_conf, det_conf * 0.85))

        allowed_ids = {str(c["chunk"].get("id", "")) for c in contexts if c["chunk"].get("id")}
        allowed_ids.update(str(h.get("id", "")) for h in web_hits if h.get("id"))
        evidence_ids = [eid for eid in evidence_ids if eid in allowed_ids]
        if not evidence_ids:
            evidence_ids = [str(c["chunk"].get("id", "")) for c in contexts[:2] if c["chunk"].get("id")]
            if web_hits:
                evidence_ids.append(str(web_hits[0].get("id", "")))
            evidence_ids = [x for x in evidence_ids if x]

        return SolveResult(choice=final_choice, confidence=final_conf, reason=reason, evidence_ids=evidence_ids)
