"""Shared utilities: paths, env, slugify, atomic write, append-with-fsync, SIGINT."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS_ROOT = REPO_ROOT / "tools"
WORK = TOOLS_ROOT / "work"
DOWNLOADS = WORK / "downloads"
EXTRACTED = WORK / "extracted"
CLASSIFIED = WORK / "classified"
INPUTS = TOOLS_ROOT / "inputs"


def load_env() -> None:
    env_path = TOOLS_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return
    load_dotenv(env_path)


def ensure_dirs() -> None:
    for d in (WORK, DOWNLOADS, EXTRACTED, CLASSIFIED, INPUTS):
        d.mkdir(parents=True, exist_ok=True)


def lm_studio_url() -> str:
    url = os.environ.get("LM_STUDIO_URL")
    if not url:
        raise RuntimeError("LM_STUDIO_URL is not set; copy tools/.env.example to tools/.env and fill it in.")
    return url.rstrip("/")


def lm_studio_model() -> str:
    return os.environ.get("LM_STUDIO_MODEL", "google/gemma-4-31b")


def audit_csv_path() -> Path:
    explicit = os.environ.get("AUDIT_CSV")
    if explicit:
        return Path(explicit)
    candidates = sorted(INPUTS.glob("william-hub-audit-*.csv"))
    if not candidates:
        raise RuntimeError(f"No audit CSV found under {INPUTS}/. Set AUDIT_CSV or drop the file there.")
    return candidates[-1]


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_len: int = 80) -> str:
    """Lowercase, hyphenated, ASCII-only. Empty input → 'untitled'."""
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    norm = norm.lower()
    norm = _SLUG_RE.sub("-", norm).strip("-")
    return (norm or "untitled")[:max_len].rstrip("-") or "untitled"


def sha256_of_path(path: Path, *, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".partial")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def atomic_write_text(target: Path, text: str) -> None:
    atomic_write_bytes(target, text.encode("utf-8"))


def atomic_write_json(target: Path, obj: Any) -> None:
    atomic_write_text(target, json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Append rows and fsync — called per batch boundary, not per row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


@dataclass
class StopRequested:
    """One ctrl-c → set flag, finish current item, exit clean. Two ctrl-c → die."""
    flagged: bool = False
    _printed: bool = False
    _hits: int = 0

    def install(self) -> None:
        def _handler(signum: int, frame: Any) -> None:
            self._hits += 1
            if self._hits >= 2:
                raise KeyboardInterrupt
            self.flagged = True
            if not self._printed:
                print("\nstopping after current item...", file=sys.stderr, flush=True)
                self._printed = True
        signal.signal(signal.SIGINT, _handler)
