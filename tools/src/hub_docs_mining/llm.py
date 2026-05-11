"""LM Studio client wrapper. One model, two input modes (text-only / text+image)."""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI

from . import _common


@dataclass
class LMStudioClient:
    url: str
    model: str
    _client: OpenAI | None = None

    @classmethod
    def from_env(cls) -> "LMStudioClient":
        return cls(url=_common.lm_studio_url(), model=_common.lm_studio_model())

    def _openai(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(base_url=f"{self.url}/v1", api_key="lm-studio")
        return self._client

    def probe(self) -> None:
        """Verify endpoint is reachable and the model is loaded. Raise on failure."""
        try:
            r = httpx.get(f"{self.url}/v1/models", timeout=5.0)
            r.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"LM Studio not reachable at {self.url}: {e}") from e
        models = {m.get("id") for m in r.json().get("data", [])}
        if self.model not in models:
            raise RuntimeError(
                f"Model {self.model!r} not loaded in LM Studio. Loaded: {sorted(models)}"
            )

    def classify_json(
        self,
        *,
        system: str,
        user: str,
        images: list[Path] | None = None,
        visual_token_budget: int = 0,
        max_tokens: int = 1024,
        temperature: float = 0.1,
        retries: int = 3,
    ) -> tuple[dict[str, Any], str]:
        """Call the model, expect a JSON object back. Returns (parsed, raw_text).

        Images, when provided, are sent as data URLs. `visual_token_budget` is
        forwarded as an `extra_body` knob; LM Studio passes it through to the
        backend if the runtime supports it (Gemma 4 honors it).
        """
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for img_path in images or []:
            data = img_path.read_bytes()
            mime = _image_mime(img_path)
            b64 = base64.b64encode(data).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })

        extra: dict[str, Any] = {}
        if images and visual_token_budget:
            extra["visual_token_budget"] = visual_token_budget
        # Gemma's "thinking" mode is verbose for classification; disable.
        extra["thinking"] = False

        last_err: Exception | None = None
        delay = 5.0
        for attempt in range(retries):
            try:
                resp = self._openai().chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": content},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    extra_body=extra or None,
                )
                raw = resp.choices[0].message.content or ""
                return _extract_json_object(raw), raw
            except Exception as e:
                last_err = e
                if attempt + 1 >= retries:
                    break
                time.sleep(delay)
                delay *= 3
        assert last_err is not None
        raise last_err


def _image_mime(path: Path) -> str:
    suf = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
    }.get(suf, "application/octet-stream")


def _strip_code_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _extract_json_object(raw: str) -> dict[str, Any]:
    """Best-effort: parse `raw` as JSON, falling back through fence-stripping
    and brace-matching extraction. Raises ValueError if no JSON object can be found.
    """
    text = raw.strip()
    # Direct parse.
    try:
        out = json.loads(text)
        if isinstance(out, dict):
            return out
    except json.JSONDecodeError:
        pass

    # Strip ``` fences.
    inner = _strip_code_fences(text)
    if inner != text:
        try:
            out = json.loads(inner)
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            text = inner

    # Brace-match: find the first `{` and locate its matching `}`, accounting
    # for nested braces and strings (skip braces inside string literals).
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in response: {raw[:200]!r}")
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    out = json.loads(candidate)
                    if isinstance(out, dict):
                        return out
                except json.JSONDecodeError as e:
                    raise ValueError(f"JSON parse failed near {candidate[:120]!r}: {e}") from e
    raise ValueError(f"unterminated JSON object in response: {raw[:200]!r}")
