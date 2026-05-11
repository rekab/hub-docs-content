"""Stage B — Download. OAuth + Drive API, mimeType branching, atomic writes, paced + resumable."""
from __future__ import annotations

import io
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from . import _common
from .triage import TRIAGE_PATH

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TOKEN_PATH = _common.TOOLS_ROOT / "token.json"
DRIVE_STATE_PATH = _common.WORK / "drive_state.json"

GDOC_MIME = "application/vnd.google-apps.document"
GSLIDE_MIME = "application/vnd.google-apps.presentation"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FOLDER_MIME = "application/vnd.google-apps.folder"

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

EXPORT_FOR = {
    GDOC_MIME: ("application/pdf", ".pdf"),
    GSLIDE_MIME: ("application/pdf", ".pdf"),
    GSHEET_MIME: (XLSX_MIME, ".xlsx"),
}

BASE_INTERVAL_S = 4.0
JITTER_RATIO = 0.5  # ±50%, so 2–6 s
DAILY_SOFT_CAP = 1000

# Backoff tiers for rate-limit-style errors.
BACKOFF_S = [60, 300, 1800]


class HaltForDay(Exception):
    """Raised when persistent rate-limit backoff escalates past the last tier.
    Re-runs sleep until drive_state.json's next_request_not_before before resuming.
    """


def _client_secrets_path() -> Path:
    p = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS")
    if not p:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_SECRETS is not set. Download an OAuth Desktop client_secret.json "
            "from Cloud Console and point this env var at it."
        )
    path = Path(p)
    if not path.is_absolute():
        path = _common.REPO_ROOT / p
    if not path.exists():
        raise RuntimeError(f"OAuth client_secret.json not found at {path}")
    return path


def _load_credentials() -> Credentials:
    creds: Credentials | None = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        _save_credentials(creds)
        return creds
    flow = InstalledAppFlow.from_client_secrets_file(str(_client_secrets_path()), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_credentials(creds)
    return creds


def _save_credentials(creds: Credentials) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


# ----- Drive state (persisted backoff) ----------------------------------------


@dataclass
class DriveState:
    tier: int = 0
    next_request_not_before: str | None = None
    reason: str | None = None
    requests_today: int = 0
    requests_day: str = ""

    @classmethod
    def load(cls) -> "DriveState":
        if not DRIVE_STATE_PATH.exists():
            return cls()
        try:
            data = json.loads(DRIVE_STATE_PATH.read_text())
        except Exception:
            return cls()
        return cls(**{k: data.get(k, getattr(cls(), k)) for k in cls.__dataclass_fields__})

    def save(self) -> None:
        _common.atomic_write_json(DRIVE_STATE_PATH, {
            "tier": self.tier,
            "next_request_not_before": self.next_request_not_before,
            "reason": self.reason,
            "requests_today": self.requests_today,
            "requests_day": self.requests_day,
        })

    def wait_until_allowed(self) -> None:
        if not self.next_request_not_before:
            return
        try:
            ts = datetime.fromisoformat(self.next_request_not_before)
        except ValueError:
            return
        now = datetime.now(timezone.utc)
        if ts > now:
            wait = (ts - now).total_seconds()
            print(f"[download] honoring persisted backoff: sleeping {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)

    def hit_rate_limit(self, reason: str) -> int:
        """Advance tier, persist, return seconds to sleep (or -1 if halt-for-day)."""
        if self.tier >= len(BACKOFF_S):
            self.next_request_not_before = _isoformat_in(86400)
            self.reason = f"halt-for-day ({reason})"
            self.save()
            return -1
        wait_s = BACKOFF_S[self.tier]
        self.tier += 1
        self.next_request_not_before = _isoformat_in(wait_s)
        self.reason = reason
        self.save()
        return wait_s

    def clear_backoff(self) -> None:
        if self.tier or self.next_request_not_before:
            self.tier = 0
            self.next_request_not_before = None
            self.reason = None
            self.save()

    def tick_request(self) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.requests_day != today:
            self.requests_day = today
            self.requests_today = 0
        self.requests_today += 1
        self.save()
        return self.requests_today <= DAILY_SOFT_CAP


def _isoformat_in(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# ----- Resume check -----------------------------------------------------------


def _meta_path(file_id: str) -> Path:
    return _common.DOWNLOADS / file_id / "_meta.json"


def _already_downloaded(file_id: str) -> bool:
    meta_path = _meta_path(file_id)
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return False
    saved = meta.get("saved_filename")
    expected_hash = meta.get("bytes_sha256")
    if not saved or not expected_hash:
        return False
    saved_path = _common.DOWNLOADS / file_id / saved
    if not saved_path.exists():
        return False
    return _common.sha256_of_path(saved_path) == expected_hash


# ----- Per-row download -------------------------------------------------------


@dataclass
class TriageEntry:
    csv_row_index: int
    file_id: str
    resource_key: str | None
    name: str
    path: str
    url: str
    owner: str


def _iter_keepers() -> list[TriageEntry]:
    rows = _common.read_jsonl(TRIAGE_PATH)
    out: list[TriageEntry] = []
    for r in rows:
        if not r.get("keep_for_library"):
            continue
        fid = r.get("file_id")
        if not fid:
            continue
        out.append(TriageEntry(
            csv_row_index=int(r["csv_row_index"]),
            file_id=fid,
            resource_key=r.get("resource_key"),
            name=r.get("name", ""),
            path=r.get("path", ""),
            url=r.get("url", ""),
            owner=r.get("owner", ""),
        ))
    return out


def _attach_resource_key(request: Any, file_id: str, resource_key: str | None) -> None:
    if resource_key:
        request.headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"


def _classify_error(e: HttpError) -> str:
    """Return one of: 'rate_limit', 'forbidden', 'not_found', '5xx', 'other'."""
    status = getattr(e.resp, "status", 0)
    try:
        body = json.loads(e.content.decode("utf-8")) if e.content else {}
    except Exception:
        body = {}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    errors = err.get("errors") or []
    reasons = {entry.get("reason") for entry in errors if isinstance(entry, dict)}
    if reasons & {"userRateLimitExceeded", "rateLimitExceeded", "quotaExceeded"}:
        return "rate_limit"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if 500 <= int(status) < 600:
        return "5xx"
    return "other"


def _get_metadata(service: Any, file_id: str, resource_key: str | None) -> dict[str, Any]:
    req = service.files().get(
        fileId=file_id,
        fields="id,name,mimeType,size,md5Checksum,modifiedTime,owners,shortcutDetails",
        supportsAllDrives=False,
    )
    _attach_resource_key(req, file_id, resource_key)
    return req.execute()


def _download_to_partial(
    service: Any,
    file_id: str,
    resource_key: str | None,
    mime_type: str,
    out_path: Path,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_suffix(out_path.suffix + ".partial")
    if partial.exists():
        partial.unlink()

    export_target = EXPORT_FOR.get(mime_type)
    if export_target:
        export_mime, _ext = export_target
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        request = service.files().get_media(fileId=file_id)
    _attach_resource_key(request, file_id, resource_key)

    fd = partial.open("wb")
    try:
        downloader = MediaIoBaseDownload(fd, request, chunksize=4 * 1024 * 1024)
        done = False
        while not done:
            _status, done = downloader.next_chunk(num_retries=2)
        fd.flush()
        os.fsync(fd.fileno())
    finally:
        fd.close()
    os.replace(partial, out_path)


def _save_filename(original_name: str, mime_type: str) -> str:
    export = EXPORT_FOR.get(mime_type)
    if export:
        _mime, ext = export
        stem = Path(original_name).stem or "untitled"
        return _common.slugify(stem) + ext
    p = Path(original_name)
    ext = p.suffix
    stem = p.stem or "untitled"
    return _common.slugify(stem) + ext


def _download_one(
    service: Any,
    state: DriveState,
    entry: TriageEntry,
) -> str:
    """Return a status: 'downloaded' | 'skipped_existing' | 'skipped_spreadsheet_only_if_we_did_so' |
    'skipped_shortcut_chain' | 'skipped_shortcut_folder' | 'not_found' | 'forbidden'."""
    if _already_downloaded(entry.file_id):
        return "skipped_existing"

    meta = _get_metadata(service, entry.file_id, entry.resource_key)
    state.clear_backoff()  # successful call resets the tier

    if meta.get("mimeType") == SHORTCUT_MIME:
        details = meta.get("shortcutDetails") or {}
        target_id = details.get("targetId")
        target_mime = details.get("targetMimeType")
        if not target_id:
            return "forbidden"
        if target_mime == FOLDER_MIME:
            return "skipped_shortcut_folder"
        # Resolve once; refuse to chain.
        meta = _get_metadata(service, target_id, None)
        state.clear_backoff()
        if meta.get("mimeType") == SHORTCUT_MIME:
            return "skipped_shortcut_chain"
        effective_id = target_id
        effective_resource_key = None
    else:
        effective_id = entry.file_id
        effective_resource_key = entry.resource_key

    mime = meta.get("mimeType", "")
    save_name = _save_filename(meta.get("name") or entry.name, mime)
    out_path = _common.DOWNLOADS / entry.file_id / save_name

    _download_to_partial(service, effective_id, effective_resource_key, mime, out_path)
    state.clear_backoff()

    sha = _common.sha256_of_path(out_path)
    size = out_path.stat().st_size

    sidecar = {
        "csv_row_index": entry.csv_row_index,
        "file_id": entry.file_id,
        "effective_file_id": effective_id,
        "resource_key": entry.resource_key,
        "original_name": meta.get("name") or entry.name,
        "saved_filename": save_name,
        "mimeType": mime,
        "modifiedTime": meta.get("modifiedTime"),
        "owners": [o.get("emailAddress") for o in (meta.get("owners") or []) if o.get("emailAddress")],
        "owner": entry.owner,
        "original_url": entry.url,
        "original_path": entry.path,
        "bytes_size": size,
        "bytes_sha256": sha,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }
    _common.atomic_write_json(_meta_path(entry.file_id), sidecar)
    return "downloaded"


def _sleep_with_jitter() -> None:
    low = BASE_INTERVAL_S * (1.0 - JITTER_RATIO)
    high = BASE_INTERVAL_S * (1.0 + JITTER_RATIO)
    time.sleep(random.uniform(low, high))


def _execute_with_retries(state: DriveState, fn, *, max_5xx_retries: int = 4) -> Any:
    """Wrap a Drive API call. Handles rate-limit backoff (persisted) and 5xx exp backoff.
    Raises HttpError for forbidden/not_found so caller can classify per-file."""
    delay = 1.0
    attempts_5xx = 0
    while True:
        try:
            return fn()
        except HttpError as e:
            kind = _classify_error(e)
            if kind == "rate_limit":
                wait = state.hit_rate_limit("rate_limit")
                if wait < 0:
                    raise HaltForDay("Drive rate-limit halt-for-day reached") from e
                print(f"[download] rate-limited; sleeping {wait}s (tier now {state.tier})", file=sys.stderr)
                time.sleep(wait)
                continue
            if kind == "5xx":
                if attempts_5xx >= max_5xx_retries:
                    raise
                print(f"[download] 5xx; sleeping {delay:.0f}s and retrying", file=sys.stderr)
                time.sleep(delay)
                delay *= 2
                attempts_5xx += 1
                continue
            raise  # forbidden / not_found / other → caller handles


def run(*, limit: int | None = None) -> None:
    _common.load_env()
    _common.ensure_dirs()

    keepers = _iter_keepers()
    if not keepers:
        print("[download] no library-candidate rows in triage.jsonl yet. Run `triage` first.", file=sys.stderr)
        return
    print(f"[download] {len(keepers)} library candidates from triage.jsonl", file=sys.stderr)

    state = DriveState.load()
    state.wait_until_allowed()

    creds = _load_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    stop = _common.StopRequested()
    stop.install()

    counters: dict[str, int] = {}
    processed = 0
    for entry in keepers:
        if limit is not None and processed >= limit:
            break
        if stop.flagged:
            print("[download] stop requested; exiting clean", file=sys.stderr)
            break
        if not state.tick_request():
            print(f"[download] daily soft cap {DAILY_SOFT_CAP} reached; stop", file=sys.stderr)
            break

        try:
            status = _execute_with_retries(state, lambda: _download_one(service, state, entry))
        except HaltForDay as e:
            print(f"[download] {e}; persisted state will sleep on next run", file=sys.stderr)
            break
        except HttpError as e:
            kind = _classify_error(e)
            if kind in {"forbidden", "not_found"}:
                status = kind
                print(f"[download] {entry.file_id} ({entry.name!r}): {kind}, skipping", file=sys.stderr)
            else:
                raise
        except Exception as e:
            print(f"[download] {entry.file_id} ({entry.name!r}): unexpected error {e!r}", file=sys.stderr)
            status = "error"

        counters[status] = counters.get(status, 0) + 1
        processed += 1
        if status != "skipped_existing":
            print(f"[download] {processed}/{len(keepers)} {status}: {entry.name!r}", file=sys.stderr)
            _sleep_with_jitter()

    print(f"[download] done. counters: {counters}", file=sys.stderr)
