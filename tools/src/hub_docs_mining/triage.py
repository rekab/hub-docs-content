"""Stage A — Triage. Two-tier heuristic filter + batched LLM description/keep calls."""
from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import _common

if False:  # pragma: no cover — for type checkers only
    from .llm import LMStudioClient

TRIAGE_PATH = _common.WORK / "triage.jsonl"

# Tier 1: drop entirely, never emitted.
TIER1_EXTS = {"mp4", "mov", "mp3", "vtt", "key", "qgz"}
DROP_STATUSES = {"NOT_LINK_SHARED", "ERROR"}

# Tier 2: won't propose for library, but still describe.
TIER2_PATH_PATTERNS = [
    r"\bMinutes\b",
    r"\bAgendas?\b",
    r"\bPhotos?\b",
    r"\bTabletops?\b",
    r"\bPast agendas?\b",
    r"\bArchive\b",
    r"/archive\b",
    r"\bWebEOC\b",
    r"\bHistory of the Hubs?\b",
    r"\bYearly Activity Reports?\b",
    r"\bTranslated handouts?\b",
    r"/Translated/",
]
_TIER2_RE = re.compile("|".join(TIER2_PATH_PATTERNS), re.IGNORECASE)

PATH_SEP = " / "  # the audit CSV uses spaces around the slash

BATCH_SIZE = 10


@dataclass
class CsvRow:
    csv_row_index: int  # 0-based index into the data rows (header excluded)
    path: str
    row_type: str
    name: str
    status: str
    access: str
    permission: str
    owner: str
    url: str

    @property
    def parent_folder(self) -> str:
        if PATH_SEP in self.path:
            return self.path.rsplit(PATH_SEP, 1)[-1]
        return self.path

    @property
    def ext(self) -> str:
        return Path(self.name).suffix.lstrip(".").lower()


def _drive_file_id_and_resource_key(url: str) -> tuple[str | None, str | None]:
    """Return (file_id, resource_key). file_id is None for unparseable URLs."""
    try:
        p = urlparse(url)
    except Exception:
        return None, None
    qs = parse_qs(p.query)
    resource_key = qs.get("resourcekey", [None])[0]

    # /file/d/{id}/...
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", p.path)
    if m:
        return m.group(1), resource_key
    # /folders/{id}
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", p.path)
    if m:
        return m.group(1), resource_key
    return None, resource_key


def _is_tier1(row: CsvRow) -> tuple[bool, str]:
    if row.row_type.lower() == "folder":
        return True, "folder"
    if row.access != "ANYONE_WITH_LINK":
        return True, "not_link_shared"
    if row.status in DROP_STATUSES:
        return True, "not_link_shared"
    if row.ext in TIER1_EXTS:
        return True, f"tier1_ext:{row.ext}"
    return False, ""


def _is_tier2(row: CsvRow) -> bool:
    return bool(_TIER2_RE.search(row.path))


def _load_csv(path: Path) -> list[CsvRow]:
    rows: list[CsvRow] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return rows
        # Expected: Path,Type,Name,Status,Access,Permission,Owner,URL
        idx_map = {col: i for i, col in enumerate(header)}
        for i, row in enumerate(reader):
            def col(name: str) -> str:
                j = idx_map.get(name)
                if j is None or j >= len(row):
                    return ""
                return row[j].strip()
            rows.append(CsvRow(
                csv_row_index=i,
                path=col("Path"),
                row_type=col("Type"),
                name=col("Name"),
                status=col("Status"),
                access=col("Access"),
                permission=col("Permission"),
                owner=col("Owner"),
                url=col("URL"),
            ))
    return rows


def _dedupe(rows: list[CsvRow]) -> list[CsvRow]:
    """Keep first occurrence by (filename, parent_folder)."""
    seen: set[tuple[str, str]] = set()
    out: list[CsvRow] = []
    for r in rows:
        key = (r.name, r.parent_folder)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ----- LLM prompts -----------------------------------------------------------

_MANIFEST_CATEGORIES = [
    "Hub Locations",
    "Utilities & Home Safety",
    "Water",
    "Sanitation",
    "Air Quality",
    "Power",
    "Communications",
]

_AUDIENCES = ["citizen", "hub_captain", "both"]

LIBRARY_SYSTEM = f"""You triage files from a Seattle Emergency Hubs community
Google Drive for inclusion in a Hub Reference Library (handouts served
via captive portal at hub sites during emergencies).

Existing manifest categories (anchors — prefer these):
{chr(10).join('  - ' + c for c in _MANIFEST_CATEGORIES)}

Scope rules:
  - KEEP English citizen-facing emergency handouts AND hub-captain
    operational docs (org charts, contact templates, run-of-show, USSF
    talking points).
  - SKIP translations of any kind (Spanish/Vietnamese/Korean/Tigrinya/etc.).
  - SKIP internal admin: rosters, contact lists, supplies tracking,
    distribution logs, meeting minutes, board agendas.
  - SKIP files whose name suggests they are tracking spreadsheets,
    photos of events, presentation slides for internal training that
    aren't citizen-facing handouts.

You see only filename + folder path. Be conservative — when ambiguous,
KEEP and the next stage will read the actual content.

Audience taxonomy: one of {_AUDIENCES}.

Reply with a JSON object: {{"results": [<row>, ...]}}. Each <row> is:
{{
  "csv_row_index": <int from input>,
  "keep_for_library": <bool>,
  "audience": "<citizen|hub_captain|both>",
  "category_guess": "<one of the existing categories, or a short new one>",
  "description": "<one sentence describing what the file appears to be>",
  "rationale": "<one short phrase explaining the keep/skip decision>",
  "confidence": <0.0..1.0>
}}
Return rows in the same order as input.""".strip()

TIER2_SYSTEM = """You describe files from a Seattle Emergency Hubs community
Google Drive audit. These files are in archive / meeting-minutes /
historical-records folders and are NOT being considered for the reference
library. Your only job is to produce a one-sentence description for each
file based on its filename and folder path, so an operator can later scan
the corpus for reorganization.

Reply with a JSON object: {"results": [<row>, ...]}. Each <row> is:
{
  "csv_row_index": <int from input>,
  "description": "<one sentence describing what the file appears to be>",
  "category_guess": "<short topical label, e.g. 'meeting minutes', 'event photos', 'training materials'>"
}
Return rows in the same order as input.""".strip()


def _format_user_block(rows: list[CsvRow]) -> str:
    lines = ["Rows:"]
    for r in rows:
        lines.append(
            f"- csv_row_index={r.csv_row_index} | name={r.name!r} | "
            f"ext=.{r.ext or '(none)'} | path={r.path!r} | owner={r.owner}"
        )
    return "\n".join(lines)


def _llm_batch(
    client: "LMStudioClient",
    rows: list[CsvRow],
    *,
    system: str,
    extra_fields: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    user = _format_user_block(rows)
    parsed, _raw = client.classify_json(system=system, user=user, max_tokens=4096)
    results = parsed.get("results", [])
    by_idx = {int(r["csv_row_index"]): r for r in results if "csv_row_index" in r}
    out: list[dict[str, Any]] = []
    for r in rows:
        llm_row = by_idx.get(r.csv_row_index, {})
        merged = {
            "csv_row_index": r.csv_row_index,
            "path": r.path,
            "name": r.name,
            "ext": r.ext,
            "owner": r.owner,
            "url": r.url,
            **extra_fields.get(r.csv_row_index, {}),
            **llm_row,
        }
        out.append(merged)
    return out


def run(*, limit: int | None = None) -> None:
    from .llm import LMStudioClient  # lazy: avoid forcing httpx/openai at import time

    _common.load_env()
    _common.ensure_dirs()

    csv_path = _common.audit_csv_path()
    print(f"[triage] reading {csv_path}", file=sys.stderr)
    all_rows = _load_csv(csv_path)
    print(f"[triage] csv rows: {len(all_rows)}", file=sys.stderr)

    # Tier 1: drop entirely. Tier 2: describe-only. Else: library-candidate.
    survivors: list[CsvRow] = []
    tier1_count = 0
    for r in all_rows:
        is_t1, _reason = _is_tier1(r)
        if is_t1:
            tier1_count += 1
            continue
        survivors.append(r)
    survivors = _dedupe(survivors)
    print(
        f"[triage] tier1 dropped: {tier1_count}; survivors (after dedupe): {len(survivors)}",
        file=sys.stderr,
    )

    library_candidates = [r for r in survivors if not _is_tier2(r)]
    tier2_rows = [r for r in survivors if _is_tier2(r)]
    print(
        f"[triage] library-candidate rows: {len(library_candidates)}; tier2 rows: {len(tier2_rows)}",
        file=sys.stderr,
    )

    # Resume: find rows already processed.
    existing = _common.read_jsonl(TRIAGE_PATH)
    done_indices = {int(r["csv_row_index"]) for r in existing if "csv_row_index" in r}
    if done_indices:
        print(f"[triage] resuming; {len(done_indices)} rows already in triage.jsonl", file=sys.stderr)

    pending_lib = [r for r in library_candidates if r.csv_row_index not in done_indices]
    pending_t2 = [r for r in tier2_rows if r.csv_row_index not in done_indices]

    if limit is not None:
        pending_lib = pending_lib[: max(0, limit)]
        pending_t2 = pending_t2[: max(0, limit)]

    client = LMStudioClient.from_env()
    print(f"[triage] probing LM Studio at {client.url} (model={client.model})", file=sys.stderr)
    client.probe()

    stop = _common.StopRequested()
    stop.install()

    file_id_cache: dict[int, dict[str, Any]] = {}
    for r in library_candidates + tier2_rows:
        fid, rkey = _drive_file_id_and_resource_key(r.url)
        file_id_cache[r.csv_row_index] = {"file_id": fid, "resource_key": rkey}

    def _run_batches(rows: list[CsvRow], *, system: str, tier: str) -> None:
        for start in range(0, len(rows), BATCH_SIZE):
            if stop.flagged:
                print(f"[triage] stopping ({tier}) at batch boundary", file=sys.stderr)
                return
            batch = rows[start : start + BATCH_SIZE]
            try:
                results = _llm_batch(
                    client, batch,
                    system=system,
                    extra_fields={r.csv_row_index: {"tier": tier, **file_id_cache[r.csv_row_index]} for r in batch},
                )
            except Exception as e:
                print(
                    f"[triage] batch failed at csv_row_index={batch[0].csv_row_index} "
                    f"(rows {[r.csv_row_index for r in batch]}): {e!r}",
                    file=sys.stderr,
                )
                # Skip this batch but continue. Re-running fills in the gaps.
                continue
            # Tier-2 prompt never emits keep_for_library; hard-set false.
            if tier == "tier2":
                for row in results:
                    row["keep_for_library"] = False
            _common.append_jsonl(TRIAGE_PATH, results)
            print(
                f"[triage] {tier} batch {start // BATCH_SIZE + 1}/"
                f"{(len(rows) + BATCH_SIZE - 1) // BATCH_SIZE} wrote {len(results)} rows",
                file=sys.stderr,
            )

    _run_batches(pending_lib, system=LIBRARY_SYSTEM, tier="library_candidate")
    if stop.flagged:
        return
    _run_batches(pending_t2, system=TIER2_SYSTEM, tier="tier2")

    print(f"[triage] done. total in triage.jsonl: {len(_common.read_jsonl(TRIAGE_PATH))}", file=sys.stderr)
