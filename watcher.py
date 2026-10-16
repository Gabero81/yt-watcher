#!/usr/bin/env python3
"""
yt-watcher v1.0 — polls a list of YouTube channels through TranscriptAPI,
pulls the transcript for every new video, and drops one Markdown file per
video into inbox/. A scheduled Claude task sweeps inbox/ and runs the
video-breakdown skill on each file.

Env vars:
  TRANSCRIPTAPI_KEY  required
  MIN_SECONDS        skip videos shorter than this (default 480 = 8 min)
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
MIN_SECONDS = int(os.environ.get("MIN_SECONDS", "480"))
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "15"))

ROOT = Path(__file__).resolve().parent
CHANNELS_FILE = ROOT / "channels.txt"
SEEN_FILE = ROOT / "seen.json"
INBOX = ROOT / "inbox"

HEADERS = {"Authorization": f"Bearer {KEY}"}


def log(msg: str) -> None:
    print(f"[watcher] {msg}", flush=True)


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
    r = requests.get(
        f"{API}/youtube/channel/latest",
        params={"channel": channel},
        headers=HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("results", [])


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


def write_inbox_file(channel: str, video: dict, data: dict) -> Path:
    INBOX.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = video.get("title", "Untitled")
    vid = video["videoId"]
    url = f"https://www.youtube.com/watch?v={vid}"
    published = video.get("published", "unknown")
    length = fmt_len(data.get("length_seconds"))
    transcript = data.get("transcript", "").strip()

    body = (
        f"# {title}\n"
        f"Channel: {channel} · Published: {published} · Length: {length} · {url}\n"
        f"Pulled by yt-watcher {today}\n\n"
        f"---\n\n"
        f"{transcript}\n"
    )
    fname = f"{today} - {safe_name(channel.lstrip('@'), 30)} - {safe_name(title)}.md"
    path = INBOX / fname
    path.write_text(body, encoding="utf-8")
    return path


def main() -> int:
    if not KEY:
        log("TRANSCRIPTAPI_KEY is not set")
        return 1

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
            for v in uploads:
                seen["videos"][v["videoId"]] = {"channel": channel, "status": "bootstrap"}
            seen["bootstrapped"].append(channel)
            log(f"{channel}: bootstrapped {len(uploads)} existing videos (not pulled)")
            continue

        for v in uploads:
            vid = v.get("videoId")
            if not vid or vid in seen["videos"]:
                continue
            if pulled >= MAX_PER_RUN:
                log("hit MAX_PER_RUN — remaining videos will be picked up next run")
                break
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
    log(f"done — pulled {pulled}, skipped {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
