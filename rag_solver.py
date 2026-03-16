from __future__ import annotations

import ast
import base64
import html
import json
import math
import re
import struct
import time
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
        generate_fallback_models: Optional[list[str]] = None,
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
        self.generate_models = self._build_generate_models(generate_model, generate_fallback_models)
        self.client = OllamaClient(base_url=ollama_base_url)
        self.web_search_enabled = bool(web_search_enabled)
        self.web_top_n = max(1, min(int(web_top_n), 8))
        self.web_timeout_sec = max(3, min(int(web_timeout_sec), 20))
        self.web_weight = max(0.0, min(float(web_weight), 0.8))
        self._embed_cache: dict[str, list[float]] = {}
        self._web_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self._chunk_views: list[dict[str, Any]] = []
        self._idf: dict[str, float] = {}
        self._prepare_chunk_views(chunks)

    @staticmethod
    def _split_model_list(raw: str) -> list[str]:
        parts = re.split(r"[,\n;|]", str(raw or ""))
        return [p.strip() for p in parts if p.strip()]

    @classmethod
    def _build_generate_models(cls, primary: str, fallbacks: Optional[list[str]]) -> list[str]:
        candidates: list[str] = []
        candidates.extend(cls._split_model_list(primary))
        if fallbacks:
            for item in fallbacks:
                candidates.extend(cls._split_model_list(str(item)))
        deduped: list[str] = []
        seen: set[str] = set()
        for model in candidates:
            key = model.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        if not deduped:
            deduped = ["qwen2.5:7b", "qwen2.5:3b"]
        return deduped

    def _generate_text(
        self,
        prompt: str,
        temperature: float,
        preferred_models: Optional[list[str]] = None,
    ) -> str:
        last_error: Optional[Exception] = None
        model_chain = list(self.generate_models)
        if preferred_models:
            seen: set[str] = set()
            preferred_chain: list[str] = []
            for raw in preferred_models:
                for model in self._split_model_list(str(raw)):
                    if model and model not in seen:
                        seen.add(model)
                        preferred_chain.append(model)
            if preferred_chain:
                model_chain = preferred_chain
        for model in model_chain:
            try:
                return self.client.generate(model, prompt, temperature=temperature)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        raise RuntimeError(
            "All generate models failed: "
            + ", ".join(model_chain)
            + (f" / last_error={last_error}" if last_error is not None else "")
        )

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
        "은",
        "는",
        "이",
        "가",
        "을",
        "를",
        "의",
        "에",
        "도",
        "와",
        "과",
        "로",
        "으로",
        "부터",
        "까지",
        "에서",
        "에게",
        "께서",
        "보다",
    }
    _DOMAIN_KEYWORDS = {
        "안전",
        "안전관리",
        "안전관리자",
        "산업안전",
        "보건",
        "위험",
        "재해",
        "방사선",
        "원자력",
        "작업",
        "보호구",
        "허가",
        "승인",
        "법",
        "법령",
        "규정",
        "관리",
        "점검",
        "교육",
        "시험",
        "가정폭력",
        "성폭력",
        "직장내",
        "아청법",
        "청소년",
        "아동",
        "피해자",
        "신고",
        "보호",
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
    _NEGATIVE_WEB_WEIGHT_CAP = 0.18
    _SELF_CHECK_CONFIDENCE_TRIGGER = 0.70
    _STRICT_NUMERIC_ACCEPT_CONF = 0.70
    _STRICT_NUMERIC_LOW_CONF_CAP = 0.55
    _WEB_CACHE_TTL_SEC = 1800.0
    _MAX_INPUT_CHARS = 4000
    _MAX_OPTION_CHARS = 600
    _MAX_QUERY_CHARS = 260
    _MAX_EMBED_CACHE_ITEMS = 4096
    _MAX_WEB_CACHE_ITEMS = 1024
    _MAX_MODEL_PARSE_CHARS = 24000
    _MAX_PROMPT_EVIDENCE_CHARS = 520
    _NUMERIC_RE = re.compile(r"\d")
    _NUMERIC_TOKEN_RE = re.compile(
        r"(?:제\s*\d+\s*조(?:\s*제\s*\d+\s*항)?)|(?:\d+(?:\.\d+)?\s*%)|(?:\d+(?:\.\d+)?\s*(?:일|회|시간|개월|년|분|초|명|건))|(?:\d+(?:\.\d+)?)"
    )

    @staticmethod
    def _clean_html_text(src: str) -> str:
        text = re.sub(r"<script.*?>.*?</script>", " ", src, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<style.*?>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _normalize_input_text(cls, text: str, max_chars: int) -> str:
        cleaned = re.sub(r"[\x00-\x08\x0B-\x1F\x7F]+", " ", str(text or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) > max_chars:
            return cleaned[:max_chars].strip()
        return cleaned

    @classmethod
    def _is_negative_question(cls, question: str) -> bool:
        q = (question or "").lower()
        return any(h in q for h in cls._NEGATIVE_HINTS)

    @classmethod
    def _has_numeric_signal(cls, question: str, options: list[str]) -> bool:
        joined = f"{question} {' '.join(options[:5])}"
        return bool(cls._NUMERIC_RE.search(joined))

    @classmethod
    def _normalize_numeric_token(cls, token: str) -> str:
        src = re.sub(r"\s+", "", str(token or "").strip().lower())
        src = src.replace("％", "%")
        src = src.replace("일이내", "일")
        src = src.replace("제", "제")
        return src

    @classmethod
    def _extract_numeric_tokens(cls, text: str, max_items: int = 48) -> list[str]:
        if not text:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for raw in cls._NUMERIC_TOKEN_RE.findall(str(text)):
            tok = cls._normalize_numeric_token(str(raw))
            if not tok or tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
            if len(out) >= max_items:
                break
        return out

    @classmethod
    def _numeric_overlap_score(cls, option_text: str, evidence_tokens: set[str]) -> float:
        opt_tokens = cls._extract_numeric_tokens(option_text, max_items=12)
        if not opt_tokens:
            return -1.0
        if not evidence_tokens:
            return 0.0
        hit = sum(1 for tok in opt_tokens if tok in evidence_tokens)
        return max(0.0, min(1.0, float(hit) / float(len(opt_tokens))))

    @classmethod
    def _deterministic_numeric_recheck(
        cls,
        question: str,
        options: list[str],
        evidence_text: str,
    ) -> dict[str, Any]:
        evidence_tokens = set(cls._extract_numeric_tokens(evidence_text))
        question_tokens = cls._extract_numeric_tokens(question, max_items=20)
        option_scores = [cls._numeric_overlap_score(opt, evidence_tokens) for opt in options]
        numeric_option_indices = [idx for idx, score in enumerate(option_scores) if score >= 0.0]
        best_idx = -1
        best_score = -1.0
        if numeric_option_indices:
            best_idx = max(numeric_option_indices, key=lambda idx: option_scores[idx])
            best_score = float(option_scores[best_idx])
        question_cov = 1.0
        if question_tokens:
            q_hit = sum(1 for tok in question_tokens if tok in evidence_tokens)
            question_cov = max(0.0, min(1.0, float(q_hit) / float(len(question_tokens))))
        return {
            "best_idx": best_idx,
            "best_score": best_score,
            "option_scores": option_scores,
            "question_cov": question_cov,
            "has_numeric_options": bool(numeric_option_indices),
        }

    @staticmethod
    def _is_high_capacity_model_name(model_name: str) -> bool:
        src = str(model_name or "").strip().lower()
        if not src:
            return False
        if "eeve" in src:
            return True
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*b\b", src)
        if match:
            try:
                return float(match.group(1)) >= 7.0
            except Exception:  # noqa: BLE001
                return False
        return any(tok in src for tok in ("7b", "8b", "9b", "10b", "11b", "12b", "13b", "14b"))

    @classmethod
    def _collect_high_capacity_models(
        cls,
        preferred_models: Optional[list[str]],
        configured_models: list[str],
    ) -> list[str]:
        candidates: list[str] = []
        if preferred_models:
            for raw in preferred_models:
                candidates.extend(cls._split_model_list(str(raw)))
        candidates.extend([str(x).strip() for x in configured_models if str(x).strip()])
        deduped: list[str] = []
        seen: set[str] = set()
        for model in candidates:
            key = model.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return [m for m in deduped if cls._is_high_capacity_model_name(m)]

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        tokens = [t.lower() for t in cls._TOKEN_RE.findall(text or "")]
        return [t for t in tokens if len(t) >= 2 and t not in cls._STOPWORDS]

    @classmethod
    def _token_weight(cls, token: str) -> float:
        tok = str(token or "").strip().lower()
        if not tok:
            return 1.0
        weight = 1.0
        if tok in cls._DOMAIN_KEYWORDS:
            weight += 0.8
        if len(tok) >= 4:
            weight += 0.2
        if len(tok) >= 6:
            weight += 0.1
        return weight

    @staticmethod
    def _to_confidence(value: object, default: float = 0.0) -> float:
        try:
            if isinstance(value, str):
                raw = value.strip()
                is_pct = raw.endswith("%")
                cleaned = re.sub(r"[^0-9.\-]", "", raw)
                if not cleaned:
                    return default
                parsed = float(cleaned)
                if is_pct or parsed > 1.0:
                    parsed /= 100.0
                return max(0.0, min(1.0, parsed))
            parsed = float(value)  # type: ignore[arg-type]
            if parsed > 1.0:
                parsed /= 100.0
            return max(0.0, min(1.0, parsed))
        except Exception:  # noqa: BLE001
            return max(0.0, min(1.0, float(default)))

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
        key = self._normalize_input_text(text, self._MAX_INPUT_CHARS)
        if key in self._embed_cache:
            return self._embed_cache[key]
        emb = self.client.embed(self.embed_model, key)
        self._embed_cache[key] = emb
        while len(self._embed_cache) > self._MAX_EMBED_CACHE_ITEMS:
            self._embed_cache.pop(next(iter(self._embed_cache)))
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
        den = sum(self._idf.get(tok, 1.0) * self._token_weight(tok) for tok in qset) or 1.0
        num = sum(self._idf.get(tok, 1.0) * self._token_weight(tok) for tok in matched)
        return max(0.0, min(1.0, num / den))

    def _self_check_pass(
        self,
        *,
        question: str,
        options: list[str],
        option_score_lines: str,
        evidence_block: str,
        draft_choice: int,
        draft_conf: float,
        draft_reason: str,
        negative_question: bool,
        preferred_models: Optional[list[str]] = None,
        strict_numerical_check: bool = False,
    ) -> Optional[dict[str, Any]]:
        q_type = "부정형(아닌/틀린/거리가 먼 유형)" if negative_question else "일반형"
        option_lines = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, start=1))
        numeric_rule = (
            "Strict-Numerical-Check: 문항/선지의 숫자(기한, 비율, 횟수, 조문번호)는 "
            "근거 문서 숫자와 100% 일치하는지 재검토하라. 불일치/근거 없음이면 confidence를 0.55 이하로 낮춰라.\n"
            if strict_numerical_check
            else ""
        )
        prompt = (
            "아래는 객관식 문제 1차 풀이 결과다.\n"
            "1차 결과가 근거 문서와 모순되는지 비판적으로 재검토하라.\n"
            "특히 근거 문서의 특정 문장과 충돌 여부를 먼저 확인하고, 필요 시 답을 수정하라.\n"
            f"{numeric_rule}"
            "출력은 JSON 한 개만:\n"
            '{"choice": <1-5 정수>, "confidence": <0~1>, "reason": "<짧은 근거>", "evidence_ids": ["id1","id2"]}\n\n'
            f"문제:\n{question}\n\n"
            f"문항 유형:\n{q_type}\n\n"
            f"선지:\n{option_lines}\n\n"
            f"1차 답안:\nchoice={int(draft_choice)}, confidence={float(draft_conf):.2f}, reason={draft_reason}\n\n"
            f"선지별 검색 점수(참고):\n{option_score_lines}\n\n"
            f"근거 문서:\n{evidence_block}\n"
        )
        try:
            raw = self._generate_text(prompt, temperature=0.0, preferred_models=preferred_models)
            parsed = self._parse_model_json(raw)
            if parsed is None:
                parsed = self._parse_model_loose(raw)
            return parsed
        except Exception:  # noqa: BLE001
            return None

    def _strict_numeric_pass(
        self,
        *,
        question: str,
        options: list[str],
        option_score_lines: str,
        evidence_block: str,
        draft_choice: int,
        draft_conf: float,
        draft_reason: str,
        preferred_models: list[str],
    ) -> Optional[dict[str, Any]]:
        option_lines = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, start=1))
        prompt = (
            "아래는 객관식 문항의 숫자 검증 단계다.\n"
            "문항/선지의 숫자(기한, 비율, 횟수, 조문번호, 연도)를 근거 문서와 1:1로 대조하라.\n"
            "숫자 불일치 또는 근거 부재가 있으면 반드시 confidence를 0.55 이하로 낮춰라.\n"
            "1차 답안의 숫자 근거가 약하면 다른 선지로 교정하라.\n"
            "출력은 JSON 한 개만:\n"
            '{"choice": <1-5 정수>, "confidence": <0~1>, "reason": "<숫자 근거 중심>", "evidence_ids": ["id1","id2"]}\n\n'
            f"문제:\n{question}\n\n"
            f"선지:\n{option_lines}\n\n"
            f"1차 답안:\nchoice={int(draft_choice)}, confidence={float(draft_conf):.2f}, reason={draft_reason}\n\n"
            f"선지별 검색 점수(참고):\n{option_score_lines}\n\n"
            f"근거 문서:\n{evidence_block}\n"
        )
        try:
            raw = self._generate_text(prompt, temperature=0.0, preferred_models=preferred_models)
            parsed = self._parse_model_json(raw)
            if parsed is None:
                parsed = self._parse_model_loose(raw)
            return parsed
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_cache_text(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip().lower()

    @classmethod
    def _question_options_cache_key(cls, question: str, options: list[str]) -> str:
        qn = cls._normalize_cache_text(question)
        normalized_opts: list[str] = []
        for opt in options:
            norm_opt = cls._normalize_cache_text(opt)
            if norm_opt:
                normalized_opts.append(norm_opt)
        unique_opts = sorted(set(normalized_opts))
        opt_sig = "|".join(unique_opts[:6])
        return f"{qn}||{opt_sig}"

    def _web_cache_get(self, key: str) -> Optional[list[dict[str, str]]]:
        packed = self._web_cache.get(key)
        if not packed:
            return None
        ts, hits = packed
        if (time.time() - float(ts)) > self._WEB_CACHE_TTL_SEC:
            self._web_cache.pop(key, None)
            return None
        return [dict(x) for x in hits]

    def _web_cache_set(self, key: str, hits: list[dict[str, str]]) -> None:
        self._web_cache[key] = (time.time(), [dict(x) for x in hits])
        while len(self._web_cache) > self._MAX_WEB_CACHE_ITEMS:
            self._web_cache.pop(next(iter(self._web_cache)))

    @staticmethod
    def _quote_for_query(text: str) -> str:
        cleaned = re.sub(r'["]+', "", str(text or "")).strip()
        return f"\"{cleaned}\"" if cleaned else ""

    @classmethod
    def _strip_particle(cls, token: str) -> str:
        src = str(token or "").strip().lower()
        if len(src) < 3:
            return src
        suffixes = ("으로", "에서", "에게", "까지", "부터", "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "로", "도")
        for suf in suffixes:
            if src.endswith(suf) and len(src) > len(suf) + 1:
                return src[: -len(suf)]
        return src

    def _extract_keyword_phrases(self, question: str) -> list[str]:
        words = [self._strip_particle(w) for w in self._TOKEN_RE.findall(question or "")]
        words = [w for w in words if len(w) >= 2 and w not in self._STOPWORDS]
        if not words:
            return []
        score_map: dict[str, float] = {}
        for n in (3, 2):
            for i in range(len(words) - n + 1):
                gram_words = words[i : i + n]
                phrase = " ".join(gram_words).strip()
                if len(phrase) < 4:
                    continue
                score = sum(self._token_weight(w) for w in gram_words) + 0.12 * n
                prev = score_map.get(phrase)
                if prev is None or score > prev:
                    score_map[phrase] = score
        ranked = sorted(score_map.items(), key=lambda x: x[1], reverse=True)
        return [p for p, _ in ranked[:3]]

    @classmethod
    def _is_law_focused_question(cls, question: str, options: list[str]) -> bool:
        joined = f"{question} {' '.join(options[:2])}"
        lowered = cls._normalize_cache_text(joined)
        if re.search(r"제\s*\d+\s*조", lowered):
            return True
        hints = {
            "법",
            "법령",
            "조문",
            "시행령",
            "시행규칙",
            "원안법",
            "원자력안전법",
            "산업안전보건법",
            "허가",
            "선량",
            "기준",
            "규정",
            "가정폭력",
            "성폭력",
            "아청법",
        }
        tokens = set(cls._tokenize(joined))
        if any(tok in hints or tok.endswith("법") for tok in tokens):
            return True
        return any(h in lowered for h in ("법령", "시행령", "시행규칙", "원자력안전법", "산업안전보건법"))

    def _build_web_queries(self, question: str, options: list[str]) -> list[str]:
        phrases = self._extract_keyword_phrases(question)
        q_tokens = self._tokenize(question)
        ranked_tokens = sorted(set(q_tokens), key=lambda t: (self._token_weight(t), len(t)), reverse=True)
        quoted_phrases = " ".join(self._quote_for_query(p) for p in phrases[:2] if p)
        quoted_terms = " ".join(self._quote_for_query(t) for t in ranked_tokens[:3] if t)
        core = quoted_phrases or quoted_terms
        law_focused = self._is_law_focused_question(question, options)

        queries: list[str] = []
        if core:
            # 일반 문항은 Google Bang 우선 시도 후 DDG 기본 질의로 폴백합니다.
            queries.append(f"!g {core} 정답")
        if law_focused and core:
            law_query = f"{core} 법령 site:law.go.kr".strip()
            # Bang query를 우선 시도하고, 실패 시 동일 조건의 일반 질의로 폴백합니다.
            queries.append(f"!g {law_query}")
            queries.append(law_query)
        if core:
            queries.append(f"{core} 정답")
        queries.append(f"{self._quote_for_query(question)} 정답".strip())
        if options:
            opt0 = self._quote_for_query(self._normalize_cache_text(options[0])[:80])
            if core and opt0:
                queries.append(f"{core} {opt0} 정답".strip())
            else:
                queries.append(f"{self._normalize_cache_text(question)} {self._normalize_cache_text(options[0])[:80]} 정답".strip())

        deduped: list[str] = []
        seen: set[str] = set()
        for raw in queries:
            q = re.sub(r"\s+", " ", str(raw or "")).strip()
            if not q:
                continue
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(q)
        return deduped[:6]

    def _search_web_once(self, query: str, question_tokens: set[str]) -> list[dict[str, str]]:
        query = self._normalize_input_text(query, self._MAX_QUERY_CHARS)
        if not query:
            return []
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
        try:
            with request.urlopen(req, timeout=self.web_timeout_sec) as resp:  # noqa: S310  # nosec B310
                page_html = resp.read().decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
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

        hits: list[dict[str, str]] = []
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
            hit_tokens = set(self._tokenize(text))
            if question_tokens and not (question_tokens & hit_tokens):
                continue
            hits.append({"source": href, "text": text[:700]})
        return hits

    def _search_web(self, question: str, options: list[str]) -> list[dict[str, str]]:
        if not self.web_search_enabled:
            return []

        scope_key = self._question_options_cache_key(question, options)
        question_tokens = set(self._tokenize(question))
        query_candidates = self._build_web_queries(question, options)
        merged_hits: list[dict[str, str]] = []
        seen_sources: set[str] = set()
        for query in query_candidates:
            if len(merged_hits) >= self.web_top_n:
                break
            key = f"web::{scope_key}::{self._normalize_cache_text(query)}"
            cached = self._web_cache_get(key)
            hits = cached if cached is not None else self._search_web_once(query=query, question_tokens=question_tokens)
            if cached is None:
                self._web_cache_set(key, hits)
            for hit in hits:
                src = str(hit.get("source", "")).strip()
                if not src or src in seen_sources:
                    continue
                seen_sources.add(src)
                merged_hits.append({"id": f"web#{len(merged_hits) + 1}", "source": src, "text": str(hit.get("text", ""))})
                if len(merged_hits) >= self.web_top_n:
                    break
        return merged_hits

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
        cleaned = str(text or "")[: RagExamSolver._MAX_MODEL_PARSE_CHARS].strip()
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

    @classmethod
    def _parse_model_loose(cls, text: str) -> Optional[dict[str, object]]:
        src = str(text or "")[: cls._MAX_MODEL_PARSE_CHARS].strip()
        if not src:
            return None
        parsed: dict[str, object] = {}

        choice_match = re.search(r"(?i)(?:choice|answer|정답)\s*[:=]?\s*([1-5])\b", src)
        if not choice_match:
            choice_match = re.search(r"(?<!\d)([1-5])\s*번(?:이\s*정답)?", src)
        if choice_match:
            parsed["choice"] = int(choice_match.group(1))

        conf_match = re.search(r"(?i)(?:confidence|conf|신뢰도)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?%?)", src)
        if conf_match:
            parsed["confidence"] = cls._to_confidence(conf_match.group(1), default=0.0)

        reason_match = re.search(r"(?im)^(?:reason|근거)\s*[:=]\s*(.+)$", src)
        if reason_match:
            parsed["reason"] = reason_match.group(1).strip()[:280]

        evidence_ids: list[str] = []
        for eid in re.findall(r"\b(?:web#\d+|[A-Za-z0-9_./-]+#\d+)\b", src):
            token = str(eid).strip()
            if token and token not in evidence_ids:
                evidence_ids.append(token)
        if evidence_ids:
            parsed["evidence_ids"] = evidence_ids[:6]

        if "choice" not in parsed:
            return None
        return parsed

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
        self,
        question: str,
        options: list[str],
        web_hits: list[dict[str, str]],
        hit_weights: Optional[dict[str, float]] = None,
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
                hit = web_hits[rank] if rank < len(web_hits) else {}
                hid = str(hit.get("id", "")).strip() if isinstance(hit, dict) else ""
                penalty_weight = 1.0
                if hit_weights and hid:
                    try:
                        penalty_weight = max(0.0, min(1.0, float(hit_weights.get(hid, 1.0))))
                    except Exception:  # noqa: BLE001
                        penalty_weight = 1.0
                w = (1.0 / (1.0 + rank * 0.35)) * penalty_weight
                if w <= 0:
                    continue
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
        evidence_penalties: Optional[dict[str, float]] = None,
        preferred_models: Optional[list[str]] = None,
        strict_numerical_check: bool = False,
    ) -> SolveResult:
        question = self._normalize_input_text(question, self._MAX_INPUT_CHARS)
        options = [self._normalize_input_text(opt, self._MAX_OPTION_CHARS) for opt in options]
        options = [opt for opt in options if opt]
        if not question.strip():
            raise RuntimeError("Empty question text")
        if len(options) < 2:
            raise RuntimeError("Need at least 2 options")

        option_lines = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, start=1))
        excluded_ids = {str(x).strip() for x in (exclude_evidence_ids or []) if str(x).strip()}
        penalty_by_id: dict[str, float] = {}
        if isinstance(evidence_penalties, dict):
            for raw_id, raw_penalty in evidence_penalties.items():
                eid = str(raw_id).strip()
                if not eid:
                    continue
                try:
                    p = max(0.0, min(0.95, float(raw_penalty)))
                except Exception:  # noqa: BLE001
                    continue
                if p > 0:
                    penalty_by_id[eid] = p
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
        if penalty_by_id:
            scored_contexts: list[dict[str, Any]] = []
            for ctx in contexts:
                chunk = ctx.get("chunk", {}) if isinstance(ctx, dict) else {}
                eid = str(chunk.get("id", "")).strip() if isinstance(chunk, dict) else ""
                penalty = penalty_by_id.get(eid, 0.0)
                base_score = float(ctx.get("score", 0.0)) if isinstance(ctx, dict) else 0.0
                adjusted_score = base_score * (1.0 - penalty)
                cloned = dict(ctx)
                cloned["score"] = adjusted_score
                scored_contexts.append(cloned)
            scored_contexts.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
            contexts = scored_contexts[: max(1, int(top_k))]

        local_scores = self._score_options(question, options, contexts)
        negative_question = self._is_negative_question(question)
        web_hits = self._search_web(question=question, options=options)
        if excluded_ids:
            web_hits = [h for h in web_hits if str(h.get("id", "")).strip() not in excluded_ids]
        web_hit_weights: dict[str, float] = {}
        if penalty_by_id:
            for hit in web_hits:
                hid = str(hit.get("id", "")).strip()
                if not hid:
                    continue
                penalty = penalty_by_id.get(hid, 0.0)
                web_hit_weights[hid] = max(0.0, 1.0 - penalty)
        web_scores = self._score_options_from_web_hits(
            question=question,
            options=options,
            web_hits=web_hits,
            hit_weights=web_hit_weights if web_hit_weights else None,
        )
        effective_web_weight = (
            min(self.web_weight, self._NEGATIVE_WEB_WEIGHT_CAP) if negative_question else self.web_weight
        )
        option_scores = self._combine_scores(
            local_scores=local_scores,
            web_scores=web_scores,
            web_weight=effective_web_weight,
        )
        det_choice, det_conf = self._pick_choice_from_scores(option_scores, negative=negative_question)

        evidence_lines = []
        evidence_text_parts: list[str] = []
        for idx, c in enumerate(contexts, start=1):
            chunk = c["chunk"]
            src = self._chunk_source(chunk)
            chunk_text = self._normalize_input_text(str(chunk.get("text", "") or ""), self._MAX_PROMPT_EVIDENCE_CHARS)
            if chunk_text:
                evidence_text_parts.append(chunk_text)
            evidence_lines.append(
                f"[{idx}] id={chunk.get('id')} source={src} score={float(c.get('score', 0.0)):.3f}\n"
                f"{chunk_text}"
            )
        offset = len(evidence_lines)
        for widx, hit in enumerate(web_hits, start=1):
            hit_text = self._normalize_input_text(str(hit.get("text", "") or ""), self._MAX_PROMPT_EVIDENCE_CHARS)
            if hit_text:
                evidence_text_parts.append(hit_text)
            evidence_lines.append(
                f"[{offset + widx}] id={hit.get('id')} source={hit.get('source')}\n"
                f"{hit_text}"
            )
        evidence_block = "\n\n".join(evidence_lines)
        evidence_text_plain = "\n".join(evidence_text_parts)
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
        if negative_question:
            web_rule = (
                "부정형 문항은 웹검색 잡음 비중을 낮춰서 해석하고, "
                "보기에 직접 언급되지 않거나 근거와 모순되는 내용을 우선 탐지"
            )
        else:
            web_rule = "반드시 웹검색 근거를 우선 참조하고, 로컬 근거와 교차검증"
        numeric_rule = (
            "Strict-Numerical-Check: 숫자(기한/퍼센트/횟수/조문번호)가 포함된 문항은 "
            "근거 문서의 숫자와 100% 일치하는 선지를 우선하라. 숫자 불일치나 근거 부재면 confidence를 0.55 이하로 낮춰라.\n"
            if strict_numerical_check
            else ""
        )
        prompt = (
            "당신은 객관식 문제 풀이 도우미다.\n"
            "반드시 근거 문서(웹검색+로컬)를 기반으로 답하라. 근거가 약하면 confidence를 낮춰라.\n"
            "웹검색 스니펫을 반드시 참조하고 로컬 근거와 충돌 여부를 확인하라.\n"
            "문항이 부정형이면(아닌/틀린/거리가 먼) 정답은 일반적으로 '근거와 가장 덜 일치하는' 선지다.\n"
            f"규칙: {web_rule}\n"
            f"{numeric_rule}"
            "출력은 JSON 한 개만:\n"
            '{"choice": <1-5 정수>, "confidence": <0~1>, "reason": "<짧은 근거>", "evidence_ids": ["id1","id2"]}\n\n'
            f"문제:\n{question}\n\n"
            f"문항 유형:\n{q_type}\n\n"
            f"선지:\n{option_lines}\n\n"
            f"선지별 검색 점수(참고):\n{option_score_lines}\n\n"
            f"근거 문서:\n{evidence_block}\n"
        )
        raw = self._generate_text(prompt, temperature=0.1, preferred_models=preferred_models)
        parsed = self._parse_model_json(raw)
        if parsed is None:
            parsed = self._parse_model_loose(raw)

        if parsed is None:
            fallback_eids = [str(c["chunk"].get("id", "")) for c in contexts[:2] if c["chunk"].get("id")]
            if web_hits:
                fallback_eids.append(str(web_hits[0].get("id", "")))
            fallback_eids = [x for x in fallback_eids if x]
            fallback_conf = round(max(0.45, det_conf) + 1e-9, 2)
            return SolveResult(
                choice=det_choice,
                confidence=fallback_conf,
                reason="LLM 응답 파싱 실패(JSON/텍스트), 하이브리드 검색 점수 폴백",
                evidence_ids=fallback_eids,
            )

        choice = int(parsed.get("choice", 0) or 0)
        model_conf = self._to_confidence(parsed.get("confidence", 0.0), default=0.0)
        reason = str(parsed.get("reason", "") or "")
        eids_raw = parsed.get("evidence_ids", [])
        evidence_ids = [str(x) for x in eids_raw] if isinstance(eids_raw, list) else []
        numeric_signal = bool(strict_numerical_check or self._has_numeric_signal(question, options))
        numeric_guard_floor: Optional[float] = None

        if model_conf < self._SELF_CHECK_CONFIDENCE_TRIGGER:
            second_pass = self._self_check_pass(
                question=question,
                options=options,
                option_score_lines=option_score_lines,
                evidence_block=evidence_block,
                draft_choice=choice if 1 <= choice <= len(options) else det_choice,
                draft_conf=model_conf,
                draft_reason=reason,
                negative_question=negative_question,
                preferred_models=preferred_models,
                strict_numerical_check=strict_numerical_check,
            )
            if isinstance(second_pass, dict):
                cand_choice = int(second_pass.get("choice", 0) or 0)
                cand_conf = self._to_confidence(second_pass.get("confidence", 0.0), default=0.0)
                cand_reason = str(second_pass.get("reason", "") or "").strip()
                cand_ids_raw = second_pass.get("evidence_ids", [])
                cand_ids = [str(x) for x in cand_ids_raw] if isinstance(cand_ids_raw, list) else []
                if 1 <= cand_choice <= len(options):
                    if cand_choice != choice or cand_conf >= model_conf:
                        choice = cand_choice
                        model_conf = cand_conf
                        if cand_reason:
                            reason = f"self-check 반영: {cand_reason}"
                        if cand_ids:
                            evidence_ids = cand_ids

        if numeric_signal:
            strict_models = self._collect_high_capacity_models(preferred_models, self.generate_models)
            if strict_models:
                strict_pass = self._strict_numeric_pass(
                    question=question,
                    options=options,
                    option_score_lines=option_score_lines,
                    evidence_block=evidence_block,
                    draft_choice=choice if 1 <= choice <= len(options) else det_choice,
                    draft_conf=model_conf,
                    draft_reason=reason,
                    preferred_models=strict_models,
                )
                if isinstance(strict_pass, dict):
                    cand_choice = int(strict_pass.get("choice", 0) or 0)
                    cand_conf = self._to_confidence(strict_pass.get("confidence", 0.0), default=0.0)
                    cand_reason = str(strict_pass.get("reason", "") or "").strip()
                    cand_ids_raw = strict_pass.get("evidence_ids", [])
                    cand_ids = [str(x) for x in cand_ids_raw] if isinstance(cand_ids_raw, list) else []
                    changed = False
                    if 1 <= cand_choice <= len(options):
                        if cand_conf >= max(self._STRICT_NUMERIC_ACCEPT_CONF, model_conf):
                            choice = cand_choice
                            model_conf = cand_conf
                            changed = True
                        elif cand_conf >= (model_conf + 0.05):
                            choice = cand_choice
                            model_conf = cand_conf
                            changed = True
                    if cand_conf <= self._STRICT_NUMERIC_LOW_CONF_CAP:
                        model_conf = min(model_conf, cand_conf)
                        numeric_guard_floor = cand_conf
                    if cand_reason:
                        prefix = "strict-numeric 반영" if changed else "strict-numeric 검토"
                        reason = f"{prefix}: {cand_reason}"
                    if cand_ids:
                        evidence_ids = cand_ids

            det_numeric = self._deterministic_numeric_recheck(
                question=question,
                options=options,
                evidence_text=evidence_text_plain,
            )
            if bool(det_numeric.get("has_numeric_options", False)):
                best_idx = int(det_numeric.get("best_idx", -1))
                best_score = float(det_numeric.get("best_score", -1.0) or -1.0)
                option_scores = det_numeric.get("option_scores", [])
                chosen_score = -1.0
                if isinstance(option_scores, list) and 1 <= choice <= len(option_scores):
                    try:
                        chosen_score = float(option_scores[choice - 1])
                    except Exception:  # noqa: BLE001
                        chosen_score = -1.0
                if 0 <= best_idx < len(options) and best_score >= 0.0:
                    # 결정론 수치대조: 선택지 수치 근거가 현저히 약하면 보수적으로 교정/감쇠합니다.
                    if chosen_score >= 0.0 and best_score >= 0.80 and (best_score - chosen_score) >= 0.45:
                        choice = best_idx + 1
                        model_conf = max(model_conf, min(0.86, 0.64 + 0.20 * best_score))
                        reason = (
                            f"det-numeric 교정: numeric_overlap best={best_score:.2f}, selected={chosen_score:.2f}. "
                            f"{reason}"
                        ).strip()
                    elif chosen_score >= 0.0 and chosen_score <= 0.01 and best_score >= 0.35:
                        numeric_guard_floor = min(
                            self._STRICT_NUMERIC_LOW_CONF_CAP,
                            numeric_guard_floor if numeric_guard_floor is not None else self._STRICT_NUMERIC_LOW_CONF_CAP,
                        )
                        reason = (
                            f"det-numeric 경고: selected={chosen_score:.2f}, best={best_score:.2f}, "
                            "숫자 근거 약함으로 confidence 상한 적용. "
                            f"{reason}"
                        ).strip()
            q_cov = float(det_numeric.get("question_cov", 1.0) or 1.0) if 'det_numeric' in locals() else 1.0
            if q_cov < 0.20:
                numeric_guard_floor = min(
                    self._STRICT_NUMERIC_LOW_CONF_CAP,
                    numeric_guard_floor if numeric_guard_floor is not None else self._STRICT_NUMERIC_LOW_CONF_CAP,
                )
                if "det-numeric" not in reason:
                    reason = f"det-numeric 경고: 문제 숫자 근거가 희박(q_cov={q_cov:.2f}). {reason}".strip()

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
        if numeric_guard_floor is not None:
            final_conf = min(final_conf, max(0.0, min(1.0, numeric_guard_floor)))
            if "strict-numeric" not in reason.lower():
                reason = f"strict-numeric 경고로 confidence 상한 적용. {reason}".strip()

        allowed_ids = {str(c["chunk"].get("id", "")) for c in contexts if c["chunk"].get("id")}
        allowed_ids.update(str(h.get("id", "")) for h in web_hits if h.get("id"))
        evidence_ids = [eid for eid in evidence_ids if eid in allowed_ids]
        if not evidence_ids:
            evidence_ids = [str(c["chunk"].get("id", "")) for c in contexts[:2] if c["chunk"].get("id")]
            if web_hits:
                evidence_ids.append(str(web_hits[0].get("id", "")))
            evidence_ids = [x for x in evidence_ids if x]

        return SolveResult(choice=final_choice, confidence=final_conf, reason=reason, evidence_ids=evidence_ids)
