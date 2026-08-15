# AI News Landing Page

A daily AI-news publisher that posts the top stories to a Slack channel **and**
builds a self-contained landing page showing the last 3 days of news with a
date filter.

- Fetches AI news from the public Google News RSS feed (no API key needed).
- Posts the 5 freshest stories to Slack via an Incoming Webhook.
- Builds a rolling 3-day archive and renders it into a single static
  `index.html` (news embedded inline, so it works opened directly from disk or
  hosted anywhere).
- The landing page has a green navbar (title + subtitle + date-filter chips) and
  a green footer pinned to the bottom.

## Files

| File | Purpose |
|------|---------|
| `post_ai_news.py` | The generator. Fetches news, posts to Slack, builds `site/`. |
| `site/index.html` | The landing page (generated). |
| `site/data.json` | The news archive backing the page (generated). |
| `.github/workflows/deploy.yml` | Builds and deploys the site to GitHub Pages daily. |

## Local usage

```bash
# Post to Slack AND build the site (normal mode)
python3 post_ai_news.py

# Build the site only, no Slack post (preview / CI)
python3 post_ai_news.py --build-only

# Generate but don't post (dry run prints Slack messages)
python3 post_ai_news.py --dry-run

# Post only, skip the site
python3 post_ai_news.py --no-site
```

The Slack webhook URL is read from the first non-comment line of
`~/.hermes/slack_webhook.txt` (override with `AI_NEWS_WEBHOOK_FILE`).
The output directory defaults to `~/.hermes/ai-news-site` (override with
`AI_NEWS_SITE_DIR`).

## Deploy

GitHub Actions builds and deploys to GitHub Pages:

- on every push to `main`,
- every day at 09:00 UTC (the cron keeps the 3-day window fresh),
- or manually from the Actions tab (`workflow_dispatch`).

After the first run, enable Pages in **Settings → Pages** (source:
"GitHub Actions") if it isn't already on. The live URL is shown in the
workflow's `deploy` job.

> Note: the Slack post only happens in local/cron runs where the webhook file is
> present. The Pages deploy uses `--build-only`, so the public site never needs
> the Slack credential.
