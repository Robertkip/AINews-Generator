#!/usr/bin/env python3
"""
post_ai_news.py
Fetches the latest AI news via the public Google News RSS feed, then:

  1. Posts the top-N most recent stories (within the last WINDOW_DAYS) as
     individual messages to a Slack channel using an Incoming Webhook.
  2. Builds a rolling WINDOW_DAYS-day archive (merged across runs so stories
     don't churn out of the feed, auto-pruned once older than the window).
  3. Generates a self-contained landing page (index.html) that shows the
     archived stories and lets visitors filter by date.

The webhook URL is read from the first non-comment, non-empty line in:
    ~/.hermes/slack_webhook.txt

Usage:
    python3 post_ai_news.py            # post to Slack + build site
    python3 post_ai_news.py --dry-run  # no Slack post; still builds site (preview)
    python3 post_ai_news.py --no-site  # post only, skip the landing page
    python3 post_ai_news.py --build-only  # build site only, no Slack post (CI/deploy)

No API keys required. Safe to run from cron in a fresh environment.

Paths are overridable via environment variables so the script works both
locally (~/.hermes/...) and in CI (repo root):
    AI_NEWS_SITE_DIR   directory that will contain index.html + data.json
    AI_NEWS_WEBHOOK_FILE  file containing the Slack Incoming Webhook URL
"""

import os
import sys
import json
import time
import datetime
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

SITE_DIR = os.environ.get("AI_NEWS_SITE_DIR",
                          os.path.expanduser("~/.hermes/ai-news-site"))
CONFIG_PATH = os.environ.get("AI_NEWS_WEBHOOK_FILE",
                             os.path.expanduser("~/.hermes/slack_webhook.txt"))
DATA_PATH = os.path.join(SITE_DIR, "data.json")
INDEX_PATH = os.path.join(SITE_DIR, "index.html")

NUM_POSTS = 5
WINDOW_DAYS = 3
USER_AGENT = "Mozilla/5.0 (compatible; hermes-ai-news/1.0)"
RSS_URL = ("https://news.google.com/rss/search?q="
           "artificial%20intelligence%20OR%20machine%20learning%20OR%20LLM%20OR%20GPT%20OR%20AI%20model"
           "&hl=en-US&gl=US&ceid=US:en")

MONTHS = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
          7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------- #
# Loading / fetching
# --------------------------------------------------------------------------- #
def load_webhook():
    try:
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("http"):
                    return line
    except FileNotFoundError:
        pass
    return None


def fetch_news():
    """Fetch and parse Google News RSS. Returns list of dicts: headline, link, source, pubdate."""
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pubdate = (item.findtext("pubDate") or "").strip()
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        # Google News titles look like "Headline - Source"; split off the source suffix.
        if source and title.endswith(" - " + source):
            headline = title[: -(len(source) + 3)].strip()
        else:
            headline = title
        items.append({
            "headline": headline,
            "link": link,
            "source": source,
            "pubdate": pubdate,
        })
    return items


def parse_pubdate(s):
    """Parse an RSS pubDate into a timezone-aware datetime, or return None."""
    if not s:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z",
                "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
def build_message(story, index, total, today):
    text = (
        f"\U0001F916 *AI News* — {today}  ({index}/{total})\n"
        f"*{story['headline']}*\n"
        f"{story['source']}"
        + (f"  •  {story['pubdate']}" if story['pubdate'] else "")
        + f"\n{story['link']}"
    )
    return {"text": text}


def post_to_slack(webhook, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Site / archive
# --------------------------------------------------------------------------- #
def load_existing():
    """Load the previous archive from data.json -> {date: {link: story}}."""
    try:
        with open(DATA_PATH) as f:
            doc = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out = {}
    for day in doc.get("days", []):
        bucket = out.setdefault(day["date"], {})
        for s in day.get("stories", []):
            bucket[s["link"]] = s
    return out


def merge_archive(fresh, existing, cutoff_iso):
    """Merge fresh + existing, keep only dates >= cutoff, dedup by headline.

    Google News can serve the same article under several different redirect
    URLs, so we dedup on a normalized headline rather than the link.
    Accepts either list-shaped ({date: [story,...]}) or dict-shaped
    ({date: {key: story}}) inputs and normalizes to dict-of-dicts keyed by
    normalized headline.
    """
    def norm_key(s):
        return " ".join(s["headline"].lower().split())

    def norm(src):
        out = {}
        for d, bucket in src.items():
            ob = out.setdefault(d, {})
            if isinstance(bucket, dict):
                for s in bucket.values():
                    ob[norm_key(s)] = s
            else:
                for s in bucket:
                    ob[norm_key(s)] = s
        return out

    fresh_n, existing_n = norm(fresh), norm(existing)
    out = {}
    for src in (existing_n, fresh_n):
        for d, bucket in src.items():
            if d < cutoff_iso:
                continue
            out.setdefault(d, {}).update(bucket)
    return out


def human_date(iso):
    y, m, d = (int(x) for x in iso.split("-"))
    wd = WEEKDAYS[datetime.date(y, m, d).weekday()]
    return f"{wd} {MONTHS[m]} {d}, {y}"


def build_archive(stories, today_date):
    """Build a merged, window-limited archive from freshly fetched stories."""
    cutoff = today_date - datetime.timedelta(days=WINDOW_DAYS - 1)
    cutoff_iso = cutoff.isoformat()

    fresh = {}
    for s in stories:
        dt = parse_pubdate(s["pubdate"])
        if not dt:
            continue
        d = dt.date()
        if d < cutoff:
            continue
        fresh.setdefault(d.isoformat(), []).append({
            "headline": s["headline"],
            "link": s["link"],
            "source": s["source"],
            "pub": dt.strftime("%a, %d %b %Y %H:%M %Z"),
            "ts": dt.timestamp(),
        })

    merged = merge_archive(fresh, load_existing(), cutoff_iso)

    days = []
    for d in sorted(merged, reverse=True):
        stories_sorted = sorted(merged[d].values(), key=lambda x: x["ts"], reverse=True)
        days.append({
            "date": d,
            "label": human_date(d),
            "count": len(stories_sorted),
            "stories": stories_sorted,
        })

    total_stories = sum(len(d["stories"]) for d in days)
    return {
        "updated": datetime.datetime.now().strftime("%a, %d %b %Y %H:%M"),
        "window_days": WINDOW_DAYS,
        "total": total_stories,
        "days": days,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI News — Daily Digest</title>
<style>
  :root{
    --bg:#0b1020; --panel:#121a30; --panel2:#0f1626; --line:#22304f;
    --txt:#e7ecf5; --muted:#93a1c0; --accent:#5b8cff; --accent2:#7c5bff;
    --chipbg:#1a2540;
  }
  *{box-sizing:border-box}
  body{margin:0;background:linear-gradient(160deg,#0b1020,#0d1426 60%,#0b1020);
    color:var(--txt);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    display:flex;flex-direction:column;min-height:100vh}
  header{margin:0}
  #navbar{position:sticky;top:0;z-index:5;
    padding:22px 12px 16px;text-align:center;
    background:linear-gradient(90deg,#0f7a3d,#12a14f);
    border-bottom:2px solid #0b5e2f;box-shadow:0 2px 12px rgba(0,0,0,.25)}
  #navbar h1{margin:0;font-size:28px;letter-spacing:.3px;color:#fff;
    text-shadow:0 1px 8px rgba(0,0,0,.25)}
  #navbar h1 .bot{filter:drop-shadow(0 0 8px rgba(255,255,255,.5))}
  #navbar .sub{margin:6px 0 14px;color:#d7ffe7;font-size:14px}
  .wrap{max-width:980px;width:100%;margin:0 auto;padding:0 16px 60px;flex:1 0 auto}
  #filters{display:flex;flex-wrap:wrap;gap:10px;justify-content:center;align-items:center}
  .chip{border:1px solid rgba(255,255,255,.35);background:rgba(255,255,255,.08);color:#eafff1;
    padding:8px 16px;border-radius:999px;cursor:pointer;font-size:13px;
    transition:.15s;white-space:nowrap}
  .chip:hover{border-color:#fff;background:rgba(255,255,255,.18);color:#fff}
  .chip.active{background:#fff;color:#0f7a3d;border-color:#fff;font-weight:600}
  .chip .n{opacity:.75;margin-left:6px;font-variant-numeric:tabular-nums}
  .day{margin:26px 0 6px;display:flex;align-items:center;gap:12px}
  .day h2{margin:0;font-size:18px}
  .day .rule{flex:1;height:1px;background:var(--line)}
  .day .badge{font-size:12px;color:var(--muted);background:var(--panel);
    border:1px solid var(--line);padding:2px 9px;border-radius:999px}
  .grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));
    border:1px solid var(--line);border-radius:14px;padding:16px 16px 14px;
    display:flex;flex-direction:column;gap:8px;transition:.15s;text-decoration:none;color:inherit}
  .card:hover{transform:translateY(-2px);border-color:var(--accent);
    box-shadow:0 8px 30px rgba(91,140,255,.15)}
  .card h3{margin:0;font-size:16px;line-height:1.35}
  .card .meta{display:flex;justify-content:space-between;gap:10px;
    color:var(--muted);font-size:12.5px;margin-top:auto}
  .card .src{color:var(--accent);font-weight:600}
  .empty{text-align:center;color:var(--muted);padding:40px}
  footer{flex-shrink:0;text-align:center;color:#d7ffe7;font-size:13px;padding:18px 12px;
    background:linear-gradient(90deg,#0f7a3d,#12a14f);border-top:2px solid #0b5e2f}
  footer a{color:#fff;text-decoration:none}
</style>
</head>
<body>
<header>
  <div id="navbar">
    <h1><span class="bot">&#129302;</span> AI News Daily</h1>
    <p class="sub">Last __WINDOW__ days &middot; updated __UPDATED__ &middot; __TOTAL__ stories</p>
    <nav id="filters"></nav>
  </div>
</header>
<main class="wrap" id="content"></main>
<footer>&copy; @all &mdash; All rights reserved 2026</footer>

<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
  var doc = JSON.parse(document.getElementById('data').textContent);
  var content = document.getElementById('content');
  var filters = document.getElementById('filters');
  var active = 'all';

  function esc(s){return (s||'').replace(/[&<>"']/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}

  function buildFilters(){
    filters.innerHTML='';
    var all = mkChip('all','All',doc.total);
    filters.appendChild(all);
    doc.days.forEach(function(d){
      filters.appendChild(mkChip(d.date, d.label, d.count));
    });
    setActive(active);
  }
  function mkChip(key,label,n){
    var b=document.createElement('button');
    b.className='chip'; b.dataset.key=key;
    b.innerHTML=esc(label)+'<span class="n">'+n+'</span>';
    b.onclick=function(){active=key; setActive(key); render();};
    return b;
  }
  function setActive(key){
    Array.prototype.forEach.call(filters.children,function(c){
      c.classList.toggle('active', c.dataset.key===key);
    });
  }
  function render(){
    content.innerHTML='';
    var shown=0;
    doc.days.forEach(function(d){
      if(active!=='all' && d.date!==active) return;
      shown+=d.stories.length;
      var head=document.createElement('div');
      head.className='day';
      head.innerHTML='<h2>'+esc(d.label)+'</h2><span class="rule"></span>'+
        '<span class="badge">'+d.stories.length+' stories</span>';
      content.appendChild(head);
      var grid=document.createElement('div');
      grid.className='grid';
      d.stories.forEach(function(s){
        var a=document.createElement('a');
        a.className='card'; a.href=s.link; a.target='_blank'; a.rel='noopener';
        a.innerHTML='<h3>'+esc(s.headline)+'</h3>'+
          '<div class="meta"><span class="src">'+esc(s.source||'Source')+'</span>'+
          '<span>'+(s.pub||'')+'</span></div>';
        grid.appendChild(a);
      });
      content.appendChild(grid);
    });
    if(shown===0){
      content.innerHTML='<div class="empty">No stories for this date.</div>';
    }
  }
  buildFilters();
  render();
})();
</script>
</body>
</html>
"""


def render_html(archive):
    """Render the self-contained landing page with the archive embedded inline."""
    html = HTML_TEMPLATE
    html = html.replace("__WINDOW__", str(archive["window_days"]))
    html = html.replace("__UPDATED__", archive["updated"])
    html = html.replace("__TOTAL__", str(archive["total"]))
    # Compact JSON, safe to embed (no closing-script sequence possible in our data,
    # but guard anyway).
    payload = json.dumps(archive, ensure_ascii=False)
    payload = payload.replace("</", "<\\/")
    html = html.replace("__DATA__", payload)
    return html


def write_site(archive):
    os.makedirs(SITE_DIR, exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)
    with open(INDEX_PATH, "w") as f:
        f.write(render_html(archive))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    dry_run = "--dry-run" in sys.argv
    no_site = "--no-site" in sys.argv
    build_only = "--build-only" in sys.argv
    webhook = load_webhook() if not build_only else None

    if not webhook and not dry_run and not build_only:
        msg = (f"No webhook URL found in {CONFIG_PATH}. "
               f"Create that file with your Slack Incoming Webhook URL on the first line.")
        print("ERROR: " + msg)
        sys.exit(2)

    try:
        stories = fetch_news()
    except Exception as e:
        print(f"ERROR: failed to fetch news: {e}")
        sys.exit(3)

    if not stories:
        print("No stories found.")
        sys.exit(0)

    today_date = datetime.date.today()
    today_label = today_date.strftime("%a %b %d, %Y")

    # ---- Slack: top N most recent within the window ----
    cutoff = today_date - datetime.timedelta(days=WINDOW_DAYS - 1)
    windowed = []
    for s in stories:
        dt = parse_pubdate(s["pubdate"])
        if dt and dt.date() >= cutoff:
            windowed.append((dt, s))
    windowed.sort(key=lambda x: x[0], reverse=True)
    selected = [s for _, s in windowed[:NUM_POSTS]]

    posted = 0
    if build_only:
        print("(build-only mode: skipping Slack posting)")
    else:
        for i, story in enumerate(selected, start=1):
            payload = build_message(story, i, len(selected), today_label)
            if dry_run:
                print("=" * 60)
                print(payload["text"])
                print("=" * 60)
                posted += 1
            else:
                try:
                    result = post_to_slack(webhook, payload)
                    status = "ok" if result.strip() == "ok" else f"resp={result!r}"
                    print(f"Posted {i}/{len(selected)}: {story['headline'][:60]} -> {status}")
                    posted += 1
                    time.sleep(1.2)
                except Exception as e:
                    print(f"FAILED {i}/{len(selected)}: {story['headline'][:60]} -> {e}")

    # ---- Site / archive ----
    if no_site:
        print(f"\nDone. {posted}/{len(selected)} Slack messages handled (dry_run={dry_run}). Site skipped.")
        return

    archive = build_archive(stories, today_date)
    write_site(archive)
    print(f"\nDone. {posted}/{len(selected)} Slack messages handled (dry_run={dry_run}).")
    print(f"Landing page: {INDEX_PATH}  ({archive['total']} stories over {len(archive['days'])} days)")


if __name__ == "__main__":
    main()
