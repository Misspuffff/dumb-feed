"""Dumb feed: pull RSS, let Claude pick and summarize, write one plain page.

Run by GitHub Actions once a day. Output: docs/index.html
"""
import html
import json
import os
import re
import socket
import struct
import zlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser

# ---------------------------------------------------------------- edit here
# Who the feed is for. This steers what the agent keeps. Keep it vague:
# the page and this repo are public.
PROFILE = (
    "a product designer who works on AI video and storytelling tools, "
    "quit social media, and wants to stay current without the noise"
)

TIMEZONE = "America/Los_Angeles"
MODEL = os.environ.get("FEED_MODEL", "claude-haiku-4-5-20251001")
LOOKBACK_HOURS = 36   # how far back to look for new items
PER_SOURCE = 12       # max items taken from any one source

SOURCES = {
    "world": [
        ("bbc", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("bbc us", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
        ("npr", "https://feeds.npr.org/1001/rss.xml"),
        ("guardian", "https://www.theguardian.com/world/rss"),
    ],
    "ai": [
        ("verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        ("willison", "https://simonwillison.net/atom/everything/"),
        ("hn", "https://hnrss.org/frontpage?points=150"),
        ("techcrunch", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("hugging face", "https://huggingface.co/blog/feed.xml"),
    ],
    "culture": [
        ("pew", "https://www.pewresearch.org/feed/"),
        ("atlantic", "https://www.theatlantic.com/feed/all/"),
        ("vox", "https://www.vox.com/rss/index.xml"),
        ("npr culture", "https://feeds.npr.org/1008/rss.xml"),
        ("garbage day", "https://www.garbageday.email/feed"),
    ],
}

SECTIONS = [("world", "World"), ("ai", "AI and tools"), ("culture", "Culture")]

SYSTEM = f"""You edit a once-a-day, text-only news feed for one reader: {PROFILE}.

You get today's items from RSS feeds, one per line:
id | source | suggested section | title | snippet

Pick what this reader needs to stay current, and write each pick as a plain post.

Sections:
- world: 6 to 8 posts. The stories a well-informed person would know today.
- ai: 6 to 8 posts. New models, new tools, and real shifts in how AI is built or used.
  Favor anything touching video, audio, creative tools, or storytelling.
  Skip funding rounds and minor product updates unless they change something.
- culture: 4 to 6 posts. Societal trends, movements, and what people are talking about.
  Skip celebrity gossip unless it is a real cultural moment.

Rules:
- If several items cover the same story, post it once.
- You may move an item to a different section than suggested.
- headline: plain words, sentence case, under 90 characters, no clickbait.
- summary: 1 or 2 sentences, under 240 characters. What happened and why it matters.
  No hype. Don't state opinions as fact.
- If a section is thin today, post fewer. Never pad.
- Only use ids from the list.

Reply with JSON only, no other text:
{{"world": [{{"id": "...", "headline": "...", "summary": "..."}}], "ai": [...], "culture": [...]}}"""
# --------------------------------------------------------------------------

socket.setdefaulttimeout(20)
UA = "dumb-feed/1.0 (personal daily RSS digest)"
HERE = os.path.dirname(os.path.abspath(__file__))


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def fetch_all():
    items, failed = [], []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    for cat, sources in SOURCES.items():
        for name, url in sources:
            try:
                parsed = feedparser.parse(url, agent=UA)
                if not parsed.entries:
                    raise ValueError(parsed.get("bozo_exception") or "no entries")
            except Exception as e:
                print(f"[fail] {name}: {e}")
                failed.append(name)
                continue
            kept = 0
            for entry in parsed.entries:
                if kept >= PER_SOURCE:
                    break
                t = entry.get("published_parsed") or entry.get("updated_parsed")
                if t and datetime(*t[:6], tzinfo=timezone.utc) < cutoff:
                    continue
                link, title = entry.get("link"), clean(entry.get("title"))
                if not link or not title:
                    continue
                items.append({
                    "id": f"{cat[0]}{len(items)}",
                    "cat": cat,
                    "source": name,
                    "title": title,
                    "snippet": clean(entry.get("summary"))[:300],
                    "url": link,
                })
                kept += 1
            print(f"[ok] {name}: {kept} new")
    return items, failed


def curate(items):
    from anthropic import Anthropic

    client = Anthropic()
    by_id = {i["id"]: i for i in items}
    listing = "\n".join(
        f'{i["id"]} | {i["source"]} | {i["cat"]} | {i["title"]} | {i["snippet"]}'
        for i in items
    )
    for attempt in (1, 2):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=4000,
                system=SYSTEM,
                messages=[{"role": "user", "content": "Today's items:\n" + listing}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
            posts = {}
            for key, _ in SECTIONS:
                posts[key] = []
                for p in data.get(key, []):
                    src = by_id.get(p.get("id"))
                    if src:  # links always come from the feed, never from the model
                        posts[key].append({
                            "source": src["source"],
                            "url": src["url"],
                            "headline": p.get("headline") or src["title"],
                            "summary": p.get("summary", ""),
                        })
            if any(posts.values()):
                return posts
            print(f"[agent] attempt {attempt}: empty result")
        except Exception as e:
            print(f"[agent] attempt {attempt} failed: {e}")
    raise RuntimeError("curating step returned nothing usable")


def fallback(items):
    posts = {}
    for key, _ in SECTIONS:
        posts[key] = [
            {"source": i["source"], "url": i["url"], "headline": i["title"],
             "summary": i["snippet"][:200]}
            for i in items if i["cat"] == key
        ][:8]
    return posts


def esc(s):
    return html.escape(s or "", quote=True)


def render(posts, notes, now_utc):
    local = now_utc.astimezone(ZoneInfo(TIMEZONE))
    day = f"{local:%a} {local.day} {local:%b}"
    clock = f"{local.hour % 12 or 12}:{local:%M}{'am' if local.hour < 12 else 'pm'}"

    body = []
    for key, label in SECTIONS:
        if not posts.get(key):
            continue
        body.append(f'<section><h2>{esc(label)}</h2>')
        for p in posts[key]:
            url = p["url"] if p["url"].startswith(("http://", "https://")) else "#"
            summary = f'<span class="sum">{esc(p["summary"])}</span>' if p["summary"] else ""
            body.append(
                f'<a class="post" href="{esc(url)}">'
                f'<span class="src">{esc(p["source"])}</span>'
                f'<span class="hed">{esc(p["headline"])}</span>{summary}</a>'
            )
        body.append("</section>")

    note_html = "".join(f"<p>{esc(n)}</p>" for n in notes)
    return (TEMPLATE
            .replace("{{GENERATED}}", now_utc.isoformat())
            .replace("{{DAY}}", esc(day))
            .replace("{{CLOCK}}", esc(clock))
            .replace("{{POSTS}}", "\n".join(body))
            .replace("{{NOTES}}", note_html))


def write_app_files(docs):
    """Home-screen icon and manifest, so the repo only needs feed.py to start."""
    grid, ink, paper = 18, (0x1B, 0x1C, 0x1A), (0xE3, 0xE4, 0xDF)
    on = set()
    for y, end in ((5, 13), (9, 13), (13, 11)):   # three pixel "posts"
        for yy in (y - 1, y):
            on.update({(4, yy), (5, yy)})           # bullet
            on.update({(x, yy) for x in range(7, end + 1)})  # line

    def png(size, path):
        scale = size / grid
        rows = b"".join(
            b"\x00" + b"".join(
                bytes(ink if (int(x / scale), int(y / scale)) in on else paper)
                for x in range(size))
            for y in range(size))
        chunk = lambda t, d: (struct.pack(">I", len(d)) + t + d
                              + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))

    png(180, os.path.join(docs, "icon-180.png"))
    png(512, os.path.join(docs, "icon-512.png"))
    manifest = {
        "name": "Feed", "short_name": "Feed", "start_url": "./", "scope": "./",
        "display": "standalone", "background_color": "#E3E4DF", "theme_color": "#E3E4DF",
        "icons": [
            {"src": "icon-180.png", "sizes": "180x180", "type": "image/png"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any maskable"},
        ],
    }
    with open(os.path.join(docs, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)


def main():
    now = datetime.now(timezone.utc)
    items, failed = fetch_all()
    notes = []
    if not items:
        posts = {k: [] for k, _ in SECTIONS}
        notes.append("No sources loaded today. Open the Actions tab on GitHub to see why.")
    else:
        try:
            posts = curate(items)
        except Exception as e:
            print(f"[agent] giving up: {e}")
            posts = fallback(items)
            notes.append("The curating step failed today, so these are raw headlines.")
    if failed:
        notes.append("Didn't load today: " + ", ".join(failed) + ".")

    docs = os.path.join(HERE, "docs")
    os.makedirs(docs, exist_ok=True)
    write_app_files(docs)
    with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as f:
        f.write(render(posts, notes, now))
    print("[done] docs/index.html written")


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Feed</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Feed">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#E3E4DF" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1A1B19" media="(prefers-color-scheme: dark)">
<link rel="apple-touch-icon" href="icon-180.png">
<link rel="manifest" href="manifest.json">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible+Mono:wght@400;700&family=Silkscreen&display=swap" rel="stylesheet">
<style>
  :root { --paper:#E3E4DF; --ink:#1B1C1A; --faded:#62635E; --rule:#B7B8B1; }
  @media (prefers-color-scheme: dark) {
    :root { --paper:#1A1B19; --ink:#D9DAD4; --faded:#8E8F89; --rule:#3B3C39; }
  }
  * { box-sizing: border-box; }
  html { background: var(--paper); -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font: 15px/1.5 "Atkinson Hyperlegible Mono", ui-monospace, Menlo, Consolas, monospace;
    padding: env(safe-area-inset-top, 0px) 0 env(safe-area-inset-bottom, 0px);
  }
  main { max-width: 23rem; margin: 0 auto; padding: 1.5rem 1.25rem 3rem; }
  .pixel { font-family: "Silkscreen", ui-monospace, Menlo, monospace; font-weight: 400; }
  header { padding-bottom: 1rem; border-bottom: 2px solid var(--ink); }
  header h1 { font-size: 1.75rem; line-height: 1.1; margin: 0 0 .25rem; }
  header p { margin: 0; color: var(--faded); font-size: .8rem; }
  #stale { background: var(--ink); color: var(--paper); padding: .6rem .75rem; margin: 0 0 1rem; font-size: .85rem; }
  section { margin-top: 2rem; }
  h2 { font-size: 1rem; margin: 0 0 .25rem; }
  .post {
    display: block; color: inherit; text-decoration: none;
    padding: .7rem .5rem; margin: 0 -.5rem; border-bottom: 1px dotted var(--rule);
  }
  .post:hover, .post:focus-visible, .post:active { background: var(--ink); color: var(--paper); outline: none; }
  .post:hover .src, .post:focus-visible .src, .post:active .src { color: var(--paper); }
  .src { display: block; color: var(--faded); font-size: .75rem; }
  .hed { display: block; font-weight: 700; }
  .sum { display: block; margin-top: .2rem; font-size: .875rem; }
  footer { margin-top: 2.5rem; padding-top: 1rem; border-top: 2px solid var(--ink); }
  footer .end { margin: 0; font-size: 1rem; }
  footer .sub { margin: .25rem 0 0; color: var(--faded); font-size: .8rem; }
  .notes p { margin: 1rem 0 0; color: var(--faded); font-size: .75rem; }
</style>
</head>
<body data-generated="{{GENERATED}}">
<main>
  <p id="stale" hidden>This feed hasn't updated since {{DAY}}. Open the Actions tab on GitHub to see what broke.</p>
  <header>
    <h1 class="pixel">{{DAY}}</h1>
    <p>Updated {{CLOCK}}</p>
  </header>
{{POSTS}}
  <footer>
    <p class="end pixel">That's everything.</p>
    <p class="sub">Back tomorrow morning.</p>
    <div class="notes">{{NOTES}}</div>
  </footer>
</main>
<script>
  var made = new Date(document.body.dataset.generated);
  if (isNaN(made) || Date.now() - made > 30 * 3600 * 1000) {
    document.getElementById("stale").hidden = false;
  }
  // Opened from the home screen, there's no pull-to-refresh.
  // So when you come back to it after an hour away, it fetches the latest page.
  var loaded = Date.now();
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && Date.now() - loaded > 3600 * 1000) {
      location.reload();
    }
  });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
