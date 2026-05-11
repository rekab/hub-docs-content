# tools/ — Drive-mining for the Hub Reference Library

One-shot pipeline that turns the 2,127-row community Drive audit into:

1. `work/proposed_manifest.toml` — library candidates for hand-merge
   into `../manifest.toml`.
2. `work/provenance.jsonl` — per-proposal provenance (owner, URL, sha256,
   model, timestamps).
3. `work/drive_descriptions.jsonl` — one-line-per-file LLM descriptions
   covering the entire reachable corpus (incl. archive/minutes folders),
   to support Drive reorganization.
4. `work/drive_descriptions.md` — same data, rendered as a nested
   folder-tree document for end-to-end human scanning.

All intermediate state (downloads, extracted text, classified meta) is
under `work/` and gitignored. The audit CSV lives in `inputs/`,
also gitignored.

## Setup (one-time)

### 1. Throwaway Google account + OAuth client

Create (or use) a throwaway Google account — **not** your primary
Gmail. This pipeline makes ~600 Drive API calls in sequence from one
identity over a short window, and that pattern can trip Google's
automated abuse detection. If they flag the account, you want them
flagging a throwaway, not the Gmail you actually use.

In Google Cloud Console under that account:

- Create a new project (or reuse). Enable the **Drive API**.
- Configure the **OAuth consent screen** (Testing status is fine). Add
  the throwaway account itself as a Test User.
- Create OAuth client credentials of type **Desktop app**. Download
  the resulting `client_secret*.json` into this `tools/` directory.
- The scope used is `https://www.googleapis.com/auth/drive.readonly`
  only — read-only. The code never calls `update`, `copy`, `create`,
  or `delete`.

### 2. `tools/.env`

```
cp .env.example .env
$EDITOR .env
```

Required fields:

- `LM_STUDIO_URL` — e.g. `http://<mac-host-lan-ip>:1234` (the Mac host's
  LAN address from inside the Linux VM; find it with
  `ipconfig getifaddr en0` on the host or check LM Studio's Server tab).
- `LM_STUDIO_MODEL` — defaults to `google/gemma-4-31b`; override if you
  load a different one.
- `GOOGLE_OAUTH_CLIENT_SECRETS` — path to the file you downloaded
  above; relative paths are resolved against the repo root.

### 3. LM Studio (on the Mac host)

Load **google/gemma-4-31b** (one model handles both text-only and
text+image classification). Start the server so the OpenAI-compat
endpoint is reachable from the VM. The `classify` and `triage`
subcommands probe `/v1/models` at startup and exit cleanly if the
model isn't loaded.

### 4. Python project

```
cd tools/
uv sync
```

`uv` reads `pyproject.toml` and creates `.venv/` here. The CLI is
installed as `hub-docs-mining`:

```
uv run hub-docs-mining --help
```

## Run order

Each subcommand is independently runnable, idempotent, and safe to
**ctrl-c** at any moment. One ctrl-c finishes the current item and
exits clean; two ctrl-c dies immediately. Re-run to resume.

```
# Stage A — triage CSV rows (LM Studio, text-only, fast)
uv run hub-docs-mining triage

# Stage B — download library candidates from Drive (slow + paced;
# expect ~30–40 min for ~400 files at the default 4 s ±50 % cadence)
uv run hub-docs-mining download --limit 8     # smoke test first
uv run hub-docs-mining download                # full run

# Stage C — classify downloaded files (LM Studio, text + optional vision)
uv run hub-docs-mining classify

# Stage D — emit proposed manifest, provenance, drive descriptions
uv run hub-docs-mining aggregate
```

The first `download` invocation will pop a browser window for OAuth
consent. The refresh token persists at `tools/token.json` (chmod 600,
gitignored); subsequent runs use it silently.

## How rows are classified (Tier 1 / Tier 2 / library-candidate)

Stage A buckets every CSV row into one of three categories. Knowing
which is which explains why `triage.jsonl` is shorter than the CSV,
why some rows have richer fields than others, and which rows Stage B
will try to download.

**Tier 1 — dropped entirely, never appears in any output.** Currently:

- `Type == folder` rows (170)
- Files not shared via `ANYONE_WITH_LINK` (`Status` in `NOT_LINK_SHARED` /
  `ERROR`, or `Access != ANYONE_WITH_LINK`) — 72 rows
- Extensions in `mp4 | mov | mp3 | vtt | key | qgz` (audio, video,
  Keynote, QGIS) — 18 rows
- Dedupe by `(filename, parent_folder)` removes 13 duplicates

Total: 260 rows of the 2,127 in the CSV. None of these get described,
downloaded, or classified — they're invisible after Stage A.

**Tier 2 — described but never proposed for the library.** Folder
paths matching any of these regex fragments (case-insensitive):

```
Minutes | Agendas? | Photos? | Tabletops? | Past agendas? |
Archive | /archive | WebEOC | History of the Hubs? |
Yearly Activity Reports? | Translated handouts? | /Translated/
```

~1,255 rows. The LLM produces a one-sentence description and a category
guess; the row lands in `triage.jsonl` with `tier="tier2"` and
`keep_for_library=false`. Stage B never downloads them. Stage D includes
them in `drive_descriptions.md` so the operator can see what's in those
folders during a Drive reorganization pass.

**Library candidates — fully classified.** ~599 rows that pass both
filters. Full LLM triage produces `keep_for_library`, `audience`,
`category_guess`, `description`, `rationale`, `confidence`. Stage B
downloads those marked `keep_for_library=true`; Stage C then reads
the actual content and re-decides.

You can tell which bucket a row landed in by the `tier` field in
`triage.jsonl`: `library_candidate` or `tier2`. Tier 1 rows are absent
entirely.

**Tuning the rules.** Both tier definitions live in
`src/hub_docs_mining/triage.py`:

- `TIER1_EXTS` — extensions to drop without describing
- `TIER2_PATH_PATTERNS` — regex fragments that route a row to
  description-only

Edit, then re-run `triage`. Existing rows are kept (resume by
`csv_row_index`); newly-included rows fill in. To force re-classification
of rows whose tier definition you changed, delete the affected lines
from `triage.jsonl` first — or wipe the file and start over (see
"Re-runs and cache" below).

## Outputs

After running all four stages, inspect:

- **`work/proposed_manifest.toml`** — each `[[doc]]` block is prefixed
  by `# proposed` and a `# fileId=… confidence=…` line. Image-kind
  candidates also get `# TODO image handling at build` (the current
  captive-portal build pipeline ships PDFs; image handling is out of
  scope for this tool and needs a separate decision).
- **`work/drive_descriptions.md`** — read end-to-end; this is where
  the reorganization-pass insights live.
- **`work/provenance.jsonl`** — keep this somewhere durable per
  proposed addition; six months from now you'll want to answer
  "where did `slug.pdf` come from".

## Merge into the real manifest

Manual, by design. The pipeline produces *proposals*; the operator
decides what ships:

1. Open `work/proposed_manifest.toml`. For each `[[doc]]` block you
   accept, copy the four lines (category/title/file/lang) into
   `../manifest.toml` under the matching category heading.
2. Copy the downloaded file from
   `tools/work/downloads/<fileId>/<saved_filename>` to
   `../<file>` (where `<file>` matches the manifest's `file = "..."`).
3. Re-run the parent project's manifest validator (see `../README.md`)
   to confirm everything resolves.

## Pacing / rate limits

The download stage is paced at base 4 s between Drive requests, with
±50 % uniform jitter (so 2–6 s). Daily soft cap of 1,000 requests.
Rate-limit responses trigger a persistent backoff in
`work/drive_state.json` that survives ctrl-c — restarting won't
immediately re-trigger the limit. If you see `halt-for-day` in that
file, wait until the next day before re-running.

## Re-runs and cache

- Stage A reads `work/triage.jsonl` and resumes from the next CSV
  row index not yet present.
- Stage B reads each `work/downloads/<fileId>/_meta.json` and skips
  files whose `bytes_sha256` matches what's on disk now.
- Stage C caches by `(file_id, content_sha256, model_id, prompt_sha,
  visual_token_budget)`. Re-runs are free unless prompts, model, or
  visual-budget parameters change.
- Stage D is idempotent; it always regenerates all four output files.

### Starting over

Each stage's resume state lives in a different place. Delete what you
want to throw away; the rest survives.

| To restart… | Delete |
|---|---|
| Stage A | `work/triage.jsonl` |
| Stage B (forget all downloads) | `work/downloads/`, `work/drive_state.json` |
| Stage B (forget one file) | `work/downloads/<fileId>/` |
| Stage C (forget classifications) | `work/classified/`, `work/extracted/` |
| Stage D | nothing — re-running overwrites all four outputs |
| Everything | `rm -rf work/` (preserves OAuth `token.json`) |

OAuth state (`tools/token.json`) and the audit CSV (`inputs/`) are
outside `work/` and survive `rm -rf work/`. To re-do the OAuth dance,
delete `tools/token.json`.
