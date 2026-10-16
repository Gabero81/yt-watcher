#!/usr/bin/env python3
"""
yt-watcher v1.0 — polls a list of YouTube channels through TranscriptAPI,
pulls the transcript for every new video, and drops one Markdown file per
video into inbox/. A scheduled Claude task sweeps inbox/ and runs the
video-breakdown skill on each file.

Env vars:
  TRANSCRIPTAPI_KEY  required
  MIN_SECONDS        skip videos shorter than this (default 600 = 10 min)
  MAX_PER_RUN        cap transcripts pulled per run (default 15)
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://transcriptapi.com/api/v2"
KEY = os.environ.get("TRANSCRIPTAPI_KEY")
MIN_SECONDS = int(os.environ.get("MIN_SECONDS", "600"))
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "15"))
# "1" = on a channel's first sight, pull its single most recent video instead of
# skipping the whole backlog. Use this to test the pipeline end to end.
PULL_LATEST_ON_BOOTSTRAP = os.environ.get("PULL_LATEST_ON_BOOTSTRAP", "0") == "1"
# N > 0: on a channel's first sight, pull everything published in the last N days
# instead of skipping the backlog. Overrides PULL_LATEST_ON_BOOTSTRAP.
BOOTSTRAP_DAYS = int(os.environ.get("BOOTSTRAP_DAYS", "0") or 0)
# A single YouTube video ID or URL to pull right now, skipping the channel list.
TEST_VIDEO = os.environ.get("TEST_VIDEO", "").strip()
# Inbox files older than this are deleted. The Claude sweep tracks what it has
# processed on its own side (Drive), so the inbox is just a rolling window.
INBOX_KEEP_DAYS = int(os.environ.get("INBOX_KEEP_DAYS", "14"))

ROOT = Path(__file__).resolve().parent
CHANNELS_FILE = ROOT / "channels.txt"
SEEN_FILE = ROOT / "seen.json"
INBOX = ROOT / "inbox"

HEADERS = {"Authorization": f"Bearer {KEY}"}


LOG_FILE = ROOT / "last_run.log"
_log_lines: list[str] = []


def log(msg: str) -> None:
    line = f"[watcher] {msg}"
    print(line, flush=True)
    _log_lines.append(line)


def flush_log() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    LOG_FILE.write_text(f"# last run {stamp}\n" + "\n".join(_log_lines) + "\n")


def channel_label(channel: str) -> str:
    """'https://www.youtube.com/@KenMcElroy' -> 'KenMcElroy'; '@x' -> 'x'; 'UC...' as is."""
    m = re.search(r"@([A-Za-z0-9._-]+)", channel)
    if m:
        return m.group(1)
    return channel.rstrip("/").split("/")[-1]


def load_channels() -> list[str]:
    if not CHANNELS_FILE.exists():
        log("channels.txt missing — nothing to do")
        return []
    out = []
    for line in CHANNELS_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {"videos": {}, "bootstrapped": []}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True) + "\n")


def latest_uploads(channel: str) -> list[dict]:
    import time
    for attempt in range(3):
        r = requests.get(
            f"{API}/youtube/channel/latest",
            params={"channel": channel},
            headers=HEADERS,
            timeout=20,
        )
        if r.status_code == 429 and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
        return r.json().get("results", [])
    return []


def get_transcript(video_id: str) -> dict | None:
    r = requests.get(
        f"{API}/youtube/transcript",
        params={"video_url": video_id, "format": "text", "include_timestamp": "true"},
        headers=HEADERS,
        timeout=60,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def is_recent(published, days: int) -> bool:
    """published is an ISO-8601 string like 2026-10-14T15:00:12+00:00 (or date only)."""
    if not published:
        return False
    s = str(published).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).total_seconds() <= days * 86400


def free_length_check(video_id: str) -> int | None:
    """Read the video's length off its public YouTube page (no API credit).
    Returns seconds, or None if it couldn't be read."""
    try:
        r = requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en"},
            timeout=15,
        )
        m = re.search(r'"lengthSeconds":"(\d+)"', r.text)
        if m:
            return int(m.group(1))
        if '"isLiveNow":true' in r.text or '"isLive":true' in r.text:
            return 0
    except Exception:  # noqa: BLE001
        pass
    return None


def safe_name(s: str, limit: int = 60) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit].rstrip(" .-")


def fmt_len(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "unknown length"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m} min"


def clean_timestamps(text: str) -> str:
    """Turn '[123.45s] words' into '[02:03] words', and merge caption fragments into
    one line per ~20 seconds so the transcript reads like prose, not karaoke."""
    def mmss(sec: float) -> str:
        sec = int(sec)
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"[{h}:{m:02d}:{s:02d}]" if h else f"[{m:02d}:{s:02d}]"

    out, buf, bucket = [], [], None
    for line in text.splitlines():
        m = re.match(r"\[(\d+(?:\.\d+)?)s\]\s*(.*)", line)
        if not m:
            if line.strip():
                buf.append(line.strip())
            continue
        t, words = float(m.group(1)), m.group(2).strip()
        b = int(t // 20)
        if bucket is None or b != bucket:
            if buf:
                out.append(" ".join(buf))
            buf = [mmss(t), words] if words else [mmss(t)]
            bucket = b
        elif words:
            buf.append(words)
    if buf:
        out.append(" ".join(buf))
    return "\n".join(out) if out else text


def write_inbox_file(channel: str, video: dict, data: dict) -> Path:
    INBOX.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = video.get("title", "Untitled")
    vid = video["videoId"]
    url = f"https://www.youtube.com/watch?v={vid}"
    published = video.get("published", "unknown")
    length = fmt_len(data.get("length_seconds"))
    transcript = clean_timestamps(data.get("transcript", "").strip())

    label = channel_label(channel)
    body = (
        f"# {title}\n"
        f"Channel: @{label} · Published: {published} · Length: {length} · {url}\n"
        f"Pulled by yt-watcher {today}\n\n"
        f"---\n\n"
        f"{transcript}\n"
    )
    fname = f"{today} - {safe_name(label, 30)} - {safe_name(title)}.md"
    path = INBOX / fname
    path.write_text(body, encoding="utf-8")
    return path


def video_id_from(s: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([A-Za-z0-9_-]{11})", s)
    return m.group(1) if m else s


def pull_one(video_ref: str, seen: dict) -> int:
    vid = video_id_from(video_ref)
    log(f"TEST_VIDEO: pulling {vid}")
    data = get_transcript(vid)
    if data is None:
        log(f"{vid}: no captions available (404)")
        return 1
    meta = {"videoId": vid, "title": data.get("title") or f"Video {vid}", "published": data.get("published", "unknown")}
    path = write_inbox_file(data.get("channel") or "test", meta, data)
    seen["videos"][vid] = {"channel": "test", "status": "inbox", "file": path.name}
    save_seen(seen)
    log(f"pulled → {path.name} ({fmt_len(data.get('length_seconds'))})")
    return 0


def main() -> int:
    if not KEY:
        log("TRANSCRIPTAPI_KEY is not set")
        return 1

    if TEST_VIDEO:
        seen = load_seen()
        seen.setdefault("videos", {})
        seen.setdefault("bootstrapped", [])
        return pull_one(TEST_VIDEO, seen)

    channels = load_channels()
    seen = load_seen()
    seen.setdefault("videos", {})
    seen.setdefault("bootstrapped", [])
    pulled = 0
    skipped = 0

    for channel in channels:
        try:
            uploads = latest_uploads(channel)
        except Exception as e:  # noqa: BLE001
            log(f"{channel}: could not list uploads ({e})")
            continue

        # First time we see a channel, remember its backlog without pulling it.
        # Only videos published AFTER bootstrap get transcribed.
        if channel not in seen["bootstrapped"]:
            if BOOTSTRAP_DAYS > 0:
                keep = [v for v in uploads if is_recent(v.get("published"), BOOTSTRAP_DAYS)]
            elif PULL_LATEST_ON_BOOTSTRAP:
                keep = uploads[:1]
            else:
                keep = []
            for v in uploads:
                if v not in keep:
                    seen["videos"][v["videoId"]] = {"channel": channel, "status": "bootstrap"}
            seen["bootstrapped"].append(channel)
            log(f"{channel}: bootstrapped {len(uploads) - len(keep)} existing videos (not pulled)")
            uploads = keep
            if not uploads:
                continue

        for v in uploads:
            vid = v.get("videoId")
            if not vid or vid in seen["videos"]:
                continue
            if pulled >= MAX_PER_RUN:
                log("hit MAX_PER_RUN — remaining videos will be picked up next run")
                break
            # Free pre-check: skip short videos / live streams before spending a credit.
            pre = free_length_check(vid)
            if pre is not None and pre < MIN_SECONDS:
                seen["videos"][vid] = {"channel": channel, "status": "too_short_or_live"}
                skipped += 1
                continue
            try:
                data = get_transcript(vid)
            except Exception as e:  # noqa: BLE001
                log(f"{channel} {vid}: transcript error ({e}) — will retry next run")
                continue

            if data is None:
                seen["videos"][vid] = {"channel": channel, "status": "no_captions"}
                log(f"{channel} {vid}: no captions — skipped (MediaScribe fallback)")
                skipped += 1
                continue

            secs = data.get("length_seconds")
            if secs in (None, "", 0) or int(secs) < MIN_SECONDS:
                seen["videos"][vid] = {"channel": channel, "status": "too_short_or_live"}
                skipped += 1
                continue

            path = write_inbox_file(channel, v, data)
            seen["videos"][vid] = {"channel": channel, "status": "inbox", "file": path.name}
            pulled += 1
            log(f"{channel}: pulled → {path.name}")

    save_seen(seen)
    prune_inbox()
    log(f"done — pulled {pulled}, skipped {skipped}")
    flush_log()
    return 0


def prune_inbox() -> None:
    """Delete inbox files whose leading YYYY-MM-DD is older than INBOX_KEEP_DAYS."""
    if not INBOX.exists():
        return
    cutoff = datetime.now(timezone.utc).timestamp() - INBOX_KEEP_DAYS * 86400
    for p in INBOX.glob("*.md"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", p.name)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if d.timestamp() < cutoff:
            p.unlink()
            log(f"pruned {p.name} (older than {INBOX_KEEP_DAYS} days)")


if __name__ == "__main__":
    sys.exit(main())
