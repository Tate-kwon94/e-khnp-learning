from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib import error, request


@dataclass
class SolveResult:
    choice: int
    confidence: float
    reason: str
    evidence_ids: list[str]


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

    def generate(self, model: str, prompt: str, temperature: float = 0.1) -> str:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=180) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
        except error.URLError as exc:
            raise RuntimeError(f"Ollama generate call failed: {exc}") from exc
        txt = body.get("response")
        if not isinstance(txt, str) or not txt.strip():
            raise RuntimeError("Invalid generate response from Ollama")
        return txt


class RagExamSolver:
    def __init__(
        self,
        index_path: str,
        generate_model: str = "qwen2.5:7b-instruct",
        embed_model: Optional[str] = None,
        ollama_base_url: str = "http://127.0.0.1:11434",
    ) -> None:
        src = Path(index_path)
        if not src.exists():
            raise FileNotFoundError(f"RAG index not found: {index_path}")
        raw = json.loads(src.read_text(encoding="utf-8"))
        chunks = raw.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise RuntimeError("RAG index has no chunks")
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        self.embed_model = embed_model or str(meta.get("embed_model") or "nomic-embed-text")
        self.generate_model = generate_model
        self.chunks = chunks
        self.client = OllamaClient(base_url=ollama_base_url)

    def _retrieve(self, query: str, top_k: int = 6) -> list[dict[str, object]]:
        q_emb = self.client.embed(self.embed_model, query)
        q_norm = math.sqrt(sum(x * x for x in q_emb)) or 1.0
        scored: list[tuple[float, dict[str, object]]] = []
        for ch in self.chunks:
            emb = ch.get("embedding")
            if not isinstance(emb, list) or not emb:
                continue
            vec = [float(x) for x in emb]
            dot = sum(a * b for a, b in zip(q_emb, vec))
            norm = float(ch.get("norm") or 1.0)
            score = dot / (q_norm * norm)
            scored.append((score, ch))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [x[1] for x in scored[: max(1, top_k)]]

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
        try:
            obj = json.loads(cleaned)
        except Exception:  # noqa: BLE001
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _fallback_choice(options: list[str], contexts: list[dict[str, object]]) -> int:
        joined = " ".join(str(c.get("text", "")) for c in contexts)
        best_idx = 1
        best_score = -1
        for i, opt in enumerate(options, start=1):
            words = [w for w in re.findall(r"[0-9A-Za-z가-힣]+", opt) if len(w) >= 2]
            score = sum(joined.count(w) for w in words)
            if score > best_score:
                best_score = score
                best_idx = i
        return best_idx

    def solve(self, question: str, options: list[str], top_k: int = 6) -> SolveResult:
        if not question.strip():
            raise RuntimeError("Empty question text")
        if len(options) < 2:
            raise RuntimeError("Need at least 2 options")

        option_lines = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, start=1))
        query = f"{question}\n{option_lines}"
        contexts = self._retrieve(query=query, top_k=top_k)
        evidence_lines = []
        for idx, c in enumerate(contexts, start=1):
            evidence_lines.append(f"[{idx}] id={c.get('id')} source={c.get('source')}\n{c.get('text')}")
        prompt = (
            "당신은 객관식 문제 풀이 도우미다.\n"
            "반드시 근거 문서만 기반으로 답하라.\n"
            "출력은 JSON 한 개만:\n"
            '{"choice": <1-5 정수>, "confidence": <0~1>, "reason": "<짧은 근거>", "evidence_ids": ["id1","id2"]}\n\n'
            f"문제:\n{question}\n\n"
            f"선지:\n{option_lines}\n\n"
            f"근거 문서:\n{'\n\n'.join(evidence_lines)}\n"
        )
        raw = self.client.generate(self.generate_model, prompt, temperature=0.1)
        parsed = self._parse_model_json(raw)

        if parsed is None:
            fallback = self._fallback_choice(options, contexts)
            return SolveResult(
                choice=fallback,
                confidence=0.45,
                reason="LLM JSON 파싱 실패, 근거 단어 겹침 기반 폴백",
                evidence_ids=[str(c.get("id", "")) for c in contexts[:2] if c.get("id")],
            )

        choice = int(parsed.get("choice", 0) or 0)
        confidence = float(parsed.get("confidence", 0.0) or 0.0)
        reason = str(parsed.get("reason", "") or "")
        eids_raw = parsed.get("evidence_ids", [])
        evidence_ids = [str(x) for x in eids_raw] if isinstance(eids_raw, list) else []

        if choice < 1 or choice > len(options):
            choice = self._fallback_choice(options, contexts)
            confidence = min(confidence, 0.55)
            if not reason:
                reason = "선택지 번호 비정상으로 폴백 적용"

        if not evidence_ids:
            evidence_ids = [str(c.get("id", "")) for c in contexts[:2] if c.get("id")]

        return SolveResult(choice=choice, confidence=confidence, reason=reason, evidence_ids=evidence_ids)
