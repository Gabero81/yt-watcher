# yt-watcher v1.0

Watches your YouTube channels, pulls transcripts for new uploads, and drops them in `inbox/`.
A scheduled Claude task sweeps `inbox/` every morning, runs `/video-breakdown`, files the report to Drive, and moves the transcript to `done/`.

```
channels.txt  →  GitHub Actions (every 2h)  →  TranscriptAPI  →  inbox/*.md
                                                                      ↓
                              Drive breakdowns/  ←  Claude 6 AM task  ←┘
                                                                      ↓
                                                                   done/*.md
```

## One-time setup (≈15 min)

1. **TranscriptAPI key** — sign up at https://transcriptapi.com (100 free credits, no card). Copy your API key.
2. **Create a private repo** on GitHub named `yt-watcher`, then push these files:
   ```
   git init
   git add .
   git commit -m "yt-watcher v1.0"
   git branch -M main
   git remote add origin https://github.com/<YOUR_USERNAME>/yt-watcher.git
   git push -u origin main
   ```
3. **Add the secret** — repo → Settings → Secrets and variables → Actions → New repository secret
   Name: `TRANSCRIPTAPI_KEY`  Value: your key.
4. **Let Actions push** — repo → Settings → Actions → General → Workflow permissions → *Read and write permissions* → Save.
5. **Add channels** — edit `channels.txt`, one per line, commit.
6. **Run it once by hand** — Actions tab → yt-watcher → Run workflow.
   First run only *bootstraps* each channel (remembers the existing backlog, pulls nothing). From then on, only new uploads get pulled.
7. **Connect GitHub to Claude** — claude.ai → Settings → Connectors → GitHub, and allow the `yt-watcher` repo. The scheduled Claude task needs this to read `inbox/` and move files to `done/`.

## Knobs

- `MIN_SECONDS` in `watch.yml` — skip anything shorter (default 600 = 10 min). Shorts and live streams are always skipped.
- `MAX_PER_RUN` — safety cap on transcripts per run (default 15).
- Cron — `17 */2 * * *` = every 2 hours. Change to `17 */4 * * *` for every 4.

## Costs

- GitHub Actions: free (well under the free-tier minutes).
- TranscriptAPI: channel checks are free; 1 credit per transcript. $5/mo = 1,000 credits.

## When something doesn't get pulled

Check `seen.json`. Each video has a status:
- `inbox` — pulled, waiting for Claude
- `no_captions` — YouTube had no captions → run it through MediaScribe by hand
- `too_short_or_live` — under MIN_SECONDS, a Short, or a live stream
- `bootstrap` — existed before the channel was added; intentionally skipped

To force a video through, delete its entry from `seen.json` and re-run the workflow.
