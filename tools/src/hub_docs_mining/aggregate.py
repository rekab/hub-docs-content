"""Stage D — Aggregate. Emits 4 outputs: proposed manifest, provenance, descriptions JSONL + MD."""
from __future__ import annotations

import json
import re
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import _common
from .classify import _classified_path
from .triage import TRIAGE_PATH

MANIFEST_PATH = _common.REPO_ROOT / "manifest.toml"

PROPOSED_MANIFEST = _common.WORK / "proposed_manifest.toml"
PROVENANCE = _common.WORK / "provenance.jsonl"
DESCRIPTIONS_JSONL = _common.WORK / "drive_descriptions.jsonl"
DESCRIPTIONS_MD = _common.WORK / "drive_descriptions.md"


def _load_existing_categories_in_order() -> list[str]:
    if not MANIFEST_PATH.exists():
        return []
    cats: list[str] = []
    seen: set[str] = set()
    for line in MANIFEST_PATH.read_text().splitlines():
        m = re.match(r'\s*category\s*=\s*"([^"]+)"', line)
        if m and m.group(1) not in seen:
            cats.append(m.group(1))
            seen.add(m.group(1))
    return cats


def _read_classified() -> dict[str, dict[str, Any]]:
    """fileId → full classified record (incl. result + kind + cache_key)."""
    out: dict[str, dict[str, Any]] = {}
    if not _common.CLASSIFIED.exists():
        return out
    for p in _common.CLASSIFIED.glob("*.meta.json"):
        try:
            rec = json.loads(p.read_text())
            out[rec["file_id"]] = rec
        except Exception:
            continue
    return out


def _read_sidecars() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not _common.DOWNLOADS.exists():
        return out
    for d in _common.DOWNLOADS.iterdir():
        meta = d / "_meta.json"
        if meta.exists():
            try:
                rec = json.loads(meta.read_text())
                out[rec["file_id"]] = rec
            except Exception:
                continue
    return out


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _slug_with_collision_suffix(slug: str, file_id: str, taken: set[str]) -> str:
    if slug not in taken:
        return slug
    return f"{slug}-{file_id[:6].lower()}"


def _emit_proposed_manifest(
    classified: dict[str, dict[str, Any]],
    sidecars: dict[str, dict[str, Any]],
) -> int:
    """Write proposed_manifest.toml. Returns the count of proposed [[doc]] entries."""
    existing_cats = _load_existing_categories_in_order()

    # Read existing manifest top matter to preserve it visually.
    existing_top = ""
    if MANIFEST_PATH.exists():
        lines = MANIFEST_PATH.read_text().splitlines()
        # Cut at the first `[[doc]]` so we keep title/source_label/note.
        for i, line in enumerate(lines):
            if line.strip().startswith("[[doc]]"):
                existing_top = "\n".join(lines[:i]).rstrip() + "\n"
                break
        else:
            existing_top = MANIFEST_PATH.read_text()

    # Group proposed entries by category. Drop entries that already exist
    # in the real manifest (matched on slug-derived filename) — operator
    # didn't ask for duplicates.
    existing_files = set()
    if MANIFEST_PATH.exists():
        for line in MANIFEST_PATH.read_text().splitlines():
            m = re.match(r'\s*file\s*=\s*"([^"]+)"', line)
            if m:
                existing_files.add(m.group(1))

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    taken_slugs: set[str] = set()
    proposals: list[tuple[str, dict[str, Any]]] = []

    for fid, rec in classified.items():
        result = rec.get("result", {})
        if not result.get("keep"):
            continue
        side = sidecars.get(fid, {})
        kind = rec.get("kind", "document")
        saved_filename = rec.get("saved_filename") or side.get("saved_filename") or ""
        original_ext = Path(saved_filename).suffix or ""
        slug = _common.slugify(result.get("slug") or Path(saved_filename).stem or "untitled")
        slug = _slug_with_collision_suffix(slug, fid, taken_slugs)
        taken_slugs.add(slug)
        file_field = f"{slug}{original_ext}"
        if file_field in existing_files:
            continue
        entry = {
            "category": result.get("category") or "Uncategorized",
            "title": result.get("title") or saved_filename or fid,
            "file": file_field,
            "lang": result.get("lang") or "en",
            "_kind": kind,
            "_confidence": result.get("confidence"),
            "_file_id": fid,
            "_saved_filename": saved_filename,
            "_description": result.get("description") or "",
        }
        by_category[entry["category"]].append(entry)
        proposals.append((fid, entry))

    # Ordering: existing cats first (in file order), then new cats alphabetically.
    new_cats = sorted(c for c in by_category.keys() if c not in existing_cats)
    ordered_cats = [c for c in existing_cats if c in by_category] + new_cats

    # Sort within a category by descending confidence (numeric only; strings → 0).
    def _conf_sort_key(e: dict[str, Any]) -> float:
        c = e.get("_confidence")
        return float(c) if isinstance(c, (int, float)) else 0.0

    out_lines: list[str] = []
    out_lines.append("# Auto-generated by hub-docs-mining aggregate")
    out_lines.append(f"# Generated at: {datetime.now(timezone.utc).isoformat()}")
    out_lines.append("# Each [[doc]] block below is a PROPOSAL — review, then copy")
    out_lines.append("# accepted blocks into ../manifest.toml and copy the corresponding")
    out_lines.append("# file from tools/work/downloads/<fileId>/ into ../<file>.")
    out_lines.append("")
    if existing_top:
        out_lines.append("# --- Existing manifest top matter (verbatim) ----------------------------------")
        out_lines.append(existing_top.rstrip())
        out_lines.append("")

    for cat in ordered_cats:
        entries = sorted(by_category[cat], key=_conf_sort_key, reverse=True)
        if not entries:
            continue
        out_lines.append(f"# --- {cat} " + "-" * max(0, 70 - len(cat)))
        for e in entries:
            out_lines.append("")
            out_lines.append("# proposed")
            if e["_kind"] == "image":
                out_lines.append("# TODO image handling at build")
            conf = e["_confidence"]
            out_lines.append(f"# fileId={e['_file_id']}  confidence={conf}")
            if e["_description"]:
                out_lines.append(f"# description: {e['_description']}")
            out_lines.append("[[doc]]")
            out_lines.append(f'category = "{_toml_escape(e["category"])}"')
            out_lines.append(f'title    = "{_toml_escape(e["title"])}"')
            out_lines.append(f'file     = "{_toml_escape(e["file"])}"')
            out_lines.append(f'lang     = "{_toml_escape(e["lang"])}"')
        out_lines.append("")

    _common.atomic_write_text(PROPOSED_MANIFEST, "\n".join(out_lines).rstrip() + "\n")
    return len(proposals)


def _emit_provenance(
    classified: dict[str, dict[str, Any]],
    sidecars: dict[str, dict[str, Any]],
) -> int:
    rows: list[dict[str, Any]] = []
    run_ts = datetime.now(timezone.utc).isoformat()
    for fid, rec in classified.items():
        result = rec.get("result", {})
        if not result.get("keep"):
            continue
        side = sidecars.get(fid, {})
        rows.append({
            "slug": result.get("slug"),
            "file_id": fid,
            "owner_email": (side.get("owners") or [side.get("owner")])[0] if (side.get("owners") or side.get("owner")) else None,
            "original_drive_url": side.get("original_url"),
            "original_path": side.get("original_path"),
            "original_modified_time": side.get("modifiedTime"),
            "sha256": side.get("bytes_sha256"),
            "model_id": rec.get("cache_key", {}).get("model_id"),
            "pass_taken": rec.get("pass_taken"),
            "run_timestamp": run_ts,
        })
    if PROVENANCE.exists():
        PROVENANCE.unlink()
    _common.append_jsonl(PROVENANCE, rows)
    return len(rows)


def _emit_descriptions(
    triage_rows: list[dict[str, Any]],
    classified: dict[str, dict[str, Any]],
    sidecars: dict[str, dict[str, Any]],
) -> int:
    """Write drive_descriptions.jsonl. Stage C description/title override Stage A's when available."""
    out_rows: list[dict[str, Any]] = []
    for r in triage_rows:
        fid = r.get("file_id")
        side = sidecars.get(fid or "", {})
        cls = classified.get(fid or "", {})
        cls_result = cls.get("result") or {}

        # Stage C overrides Stage A when present.
        description = cls_result.get("description") or r.get("description") or ""
        category_guess = cls_result.get("category") or r.get("category_guess") or ""
        audience = cls_result.get("audience") or r.get("audience") or ""
        would_propose = bool(cls_result.get("keep")) if cls_result else False

        out_rows.append({
            "csv_row_index": r.get("csv_row_index"),
            "path": r.get("path"),
            "filename": r.get("name"),
            "owner": r.get("owner"),
            "modified_time": side.get("modifiedTime"),
            "mimeType": side.get("mimeType"),
            "url": r.get("url"),
            "tier": r.get("tier"),
            "description": description,
            "category_guess": category_guess,
            "audience": audience,
            "would_propose_for_library": would_propose,
        })
    if DESCRIPTIONS_JSONL.exists():
        DESCRIPTIONS_JSONL.unlink()
    _common.append_jsonl(DESCRIPTIONS_JSONL, out_rows)
    return len(out_rows)


def _emit_descriptions_md(rows: list[dict[str, Any]]) -> None:
    """Hierarchical markdown grouped by folder path, designed for end-to-end reading."""
    # Build a folder tree. Path uses " / " separator in the audit CSV.
    tree: dict[str, Any] = {"_files": [], "_children": OrderedDict()}

    def _insert(node: dict[str, Any], parts: list[str], row: dict[str, Any]) -> None:
        if not parts:
            node["_files"].append(row)
            return
        head, *tail = parts
        child = node["_children"].setdefault(head, {"_files": [], "_children": OrderedDict()})
        _insert(child, tail, row)

    for r in sorted(rows, key=lambda x: (x.get("path") or "", x.get("filename") or "")):
        path = r.get("path") or ""
        parts = [p.strip() for p in path.split(" / ") if p.strip()]
        _insert(tree, parts, r)

    lines: list[str] = []
    lines.append("# Drive corpus descriptions")
    lines.append("")
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()} by `hub-docs-mining aggregate`._")
    lines.append("")
    lines.append("Every file below was reachable in the Seattle Emergency Hubs audit CSV.")
    lines.append("Files marked **[propose]** are candidates for the Hub Reference Library;")
    lines.append("the others are described to help with corpus reorganization.")
    lines.append("")

    def _emit_node(name: str, node: dict[str, Any], depth: int) -> None:
        heading = "#" * min(6, depth + 1)
        if name:
            lines.append(f"{heading} {name}")
            lines.append("")
        for f in node["_files"]:
            tag = " **[propose]**" if f.get("would_propose_for_library") else ""
            desc = (f.get("description") or "").strip().replace("\n", " ")
            cat = f.get("category_guess") or ""
            cat_tag = f" _(category: {cat})_" if cat else ""
            url = f.get("url") or ""
            filename = f.get("filename") or ""
            if url:
                lines.append(f"- [{filename}]({url}){tag}{cat_tag} — {desc}")
            else:
                lines.append(f"- {filename}{tag}{cat_tag} — {desc}")
        if node["_files"]:
            lines.append("")
        for child_name, child in node["_children"].items():
            _emit_node(child_name, child, depth + 1)

    # Top-level: print each first-segment folder as an H1.
    for top_name, top_node in tree["_children"].items():
        _emit_node(top_name, top_node, depth=0)
    # Files directly under root (no path) — unusual but handle.
    if tree["_files"]:
        _emit_node("(root)", tree, depth=0)

    _common.atomic_write_text(DESCRIPTIONS_MD, "\n".join(lines).rstrip() + "\n")


def run() -> None:
    _common.load_env()
    _common.ensure_dirs()

    triage_rows = _common.read_jsonl(TRIAGE_PATH)
    classified = _read_classified()
    sidecars = _read_sidecars()

    print(
        f"[aggregate] triage rows: {len(triage_rows)} | "
        f"classified: {len(classified)} | sidecars: {len(sidecars)}",
        file=sys.stderr,
    )

    n_manifest = _emit_proposed_manifest(classified, sidecars)
    n_prov = _emit_provenance(classified, sidecars)
    n_desc = _emit_descriptions(triage_rows, classified, sidecars)
    desc_rows = _common.read_jsonl(DESCRIPTIONS_JSONL)
    _emit_descriptions_md(desc_rows)

    print(f"[aggregate] wrote {PROPOSED_MANIFEST.name}: {n_manifest} proposed [[doc]] entries", file=sys.stderr)
    print(f"[aggregate] wrote {PROVENANCE.name}: {n_prov} rows", file=sys.stderr)
    print(f"[aggregate] wrote {DESCRIPTIONS_JSONL.name}: {n_desc} rows", file=sys.stderr)
    print(f"[aggregate] wrote {DESCRIPTIONS_MD.name}", file=sys.stderr)
