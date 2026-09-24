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
# Who the feed is for. Set the private FEED_PROFILE secret on GitHub to tune it
# to you; this generic line is only used if that secret is missing.
PROFILE = os.environ.get("FEED_PROFILE", "").strip() or (
    "a product designer who works on AI video and storytelling tools, "
    "quit social media, and wants to stay current without the noise"
)

TIMEZONE = "America/Los_Angeles"
MODEL = os.environ.get("FEED_MODEL", "claude-haiku-4-5-20251001")
LOOKBACK_HOURS = 24   # only items published in the last day
PER_SOURCE = 10       # max items taken from any one source

# Mixed on purpose: different countries, different political leans, hype and
# skeptics, builders and researchers. The agent summarizes facts, not framing.
SOURCES = {
    "world": [
        ("bbc", "https://feeds.bbci.co.uk/news/world/rss.xml"),                 # UK
        ("npr", "https://feeds.npr.org/1001/rss.xml"),                          # US, public radio
        ("the hill", "https://thehill.com/feed/"),                              # US politics, center
        ("fox news", "https://moxie.foxnews.com/google-publisher/latest.xml"),  # US, right-leaning
        ("guardian", "https://www.theguardian.com/world/rss"),                  # UK, left-leaning
        ("al jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),            # Middle East, Global South
        ("france 24", "https://www.france24.com/en/rss"),                       # Europe
        ("dw", "https://rss.dw.com/rdf/rss-en-all"),                            # Germany, Europe
        ("scmp", "https://www.scmp.com/rss/91/feed"),                           # Hong Kong, Asia
        ("seattle times", "https://www.seattletimes.com/feed/"),                # local
    ],
    "ai": [
        ("verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),  # consumer tech
        ("techcrunch", "https://techcrunch.com/category/artificial-intelligence/feed/"), # industry
        ("ars technica", "https://arstechnica.com/ai/feed/"),                   # technical, measured
        ("mit tech review", "https://www.technologyreview.com/feed/"),          # research and policy
        ("404 media", "https://www.404media.co/rss/"),                          # skeptical, AI slop
        ("willison", "https://simonwillison.net/atom/everything/"),             # hands-on builder
        ("hugging face", "https://huggingface.co/blog/feed.xml"),               # open models
        ("hn", "https://hnrss.org/frontpage?points=150"),                       # what developers read
        ("hackaday", "https://hackaday.com/blog/feed/"),                        # indie hardware
    ],
    "adobe": [
        ("google news", "https://news.google.com/rss/search?q=Adobe+when:1d&hl=en-US&gl=US&ceid=US:en"),
        ("google news", "https://news.google.com/rss/search?q=%28Runway+OR+Pika+OR+Sora+OR+Veo+OR+Midjourney+OR+Canva+OR+CapCut+OR+Descript+OR+Figma%29+AI+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ],
    "trending": [
        ("google trends", "https://trends.google.com/trending/rss?geo=US"),     # what people search
        ("pew", "https://www.pewresearch.org/feed/"),                           # survey data
        ("vox", "https://www.vox.com/rss/index.xml"),                           # explainers
        ("atlantic", "https://www.theatlantic.com/feed/all/"),                  # ideas and culture
        ("npr culture", "https://feeds.npr.org/1008/rss.xml"),
    ],
}

# Search feeds that collect stories from many outlets. Posts show the real outlet's name.
AGGREGATORS = {"google news"}

# Readers hit a paywall on these, so the agent prefers other outlets for the same story.
PAYWALLED = {"atlantic", "seattle times", "scmp"}

SECTIONS = [("world", "World"), ("ai", "AI and tools"), ("adobe", "Adobe and rivals"), ("trending", "Trending")]

# Sources that show what people are searching for, but aren't news themselves.
# The agent can read them; posts must link to a real outlet instead.
SIGNAL_ONLY = {"google trends"}

SYSTEM = f"""You edit a once-a-day, text-only news feed for one reader.

About the reader: {PROFILE}

They left social media, which is where people their age get their news. Give them what
social media did well: explain the news instead of just reporting it, surface what people
are actually talking about, and pick by what touches their life and work. Leave out what it
did badly: outrage, hype, and claims without sources.

You get today's items, one per line:
id | source | suggested section | title | snippet
Every item was published in the last 24 hours.

Sections:
- world: 5 to 7 posts. What a well-informed person needs to know today, ranked by how much it
  touches this reader's life: economy and cost of living, jobs, tech policy, climate, rights,
  major conflicts.
- ai: 5 to 7 posts. Rank by relevance to the reader's work and interests. New tools they could
  actually try come first. Skip funding rounds and minor product updates.
- adobe: 0 to 4 posts. Real news about Adobe (products, Firefly and AI features, business
  results, leadership, lawsuits, policy) and meaningful moves by rivals in creative and video
  AI. Skip stock-price chatter, "best tools" listicles, and minor updates. If nothing
  meaningful happened, post nothing.
- trending: 3 to 5 posts. What people are searching for and talking about. Use the
  google trends items and stories that many outlets are covering at once as signals.
  Explain why it's trending. Skip celebrity news, sports, and entertainment gossip.
  Only post a trend if a news item from a real outlet in the list explains it, and use
  that news item's id. Never use a google trends id for a post.

Rules:
- The sources span countries and political leans on purpose. Report what happened, not any
  outlet's framing. When outlets frame a story differently, write the most factual version.
- Local Seattle stories only when they affect daily life there (transit, housing, weather,
  big local news).
- Skip items that only rehash older news.
- If several items cover the same story, post it once, from whichever source covers it best.
  Prefer sources not marked (paywall).
- No more than 3 posts from any one source in a section.
- Tailor what you pick and the angle you explain, but never mention the reader, their job,
  or their employer in the text.
- If a section is thin today, post fewer. Never pad.
- Only use ids from the list.

Writing each post:
- headline: plain words, sentence case, under 90 characters, no clickbait.
- why: 1 or 2 sentences, under 220 characters. What happened and why it matters to this
  reader. A smart friend catching them up: plain, clear, no hype, no opinions stated as fact.
- context: for ongoing stories whose start they may have missed, one sentence of backstory
  under 160 characters. Otherwise an empty string.

Reply with JSON only, no other text:
{{"world": [{{"id": "...", "headline": "...", "why": "...", "context": "..."}}], "ai": [...], "adobe": [...], "trending": [...]}}"""
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
                if not t or datetime(*t[:6], tzinfo=timezone.utc) < cutoff:
                    continue  # undated or older than a day: can't prove it's today's
                link, title = entry.get("link"), clean(entry.get("title"))
                if not link or not title:
                    continue
                shown = name
                if name in AGGREGATORS:
                    outlet = clean((entry.get("source") or {}).get("title", ""))
                    if outlet:
                        shown = outlet.lower()
                        if title.endswith(" - " + outlet):
                            title = title[: -len(outlet) - 3]
                items.append({
                    "id": f"{cat[0]}{len(items)}",
                    "cat": cat,
                    "source": shown,
                    "title": title,
                    "snippet": clean(entry.get("summary") or entry.get("ht_news_item_title"))[:300],
                    "url": link,
                })
                kept += 1
            print(f"[ok] {name}: {kept} new")
    return items, failed


def curate(items):
    from anthropic import Anthropic

    # Phones often add an invisible space or line break when copying the key.
    # That breaks the request before it's sent and shows up as "Connection error".
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY secret is missing or empty")
    if not key.isascii():
        raise RuntimeError("API key contains an unusual character; paste a fresh key")
    client = Anthropic(api_key=key)
    by_id = {i["id"]: i for i in items}
    listing = "\n".join(
        f'{i["id"]} | {i["source"]}{" (paywall)" if i["source"] in PAYWALLED else ""} | {i["cat"]} | {i["title"]} | {i["snippet"]}'
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
                    if src and src["source"] not in SIGNAL_ONLY:
                        # links always come from the feed, never from the model
                        posts[key].append({
                            "source": src["source"],
                            "url": src["url"],
                            "headline": p.get("headline") or src["title"],
                            "why": p.get("why", ""),
                            "context": p.get("context", ""),
                        })
            if any(posts.values()):
                return posts
            print(f"[agent] attempt {attempt}: empty result")
        except Exception as e:
            cause = f" (cause: {e.__cause__!r})" if e.__cause__ else ""
            print(f"[agent] attempt {attempt} failed: {e}{cause}")
    raise RuntimeError("curating step returned nothing usable")


def fallback(items):
    posts = {}
    for key, _ in SECTIONS:
        by_source = {}
        for i in items:
            if i["cat"] == key and i["source"] not in SIGNAL_ONLY:
                by_source.setdefault(i["source"], []).append(i)
        mixed, queues = [], list(by_source.values())
        while queues and len(mixed) < 8:  # take one from each source in turn
            for q in list(queues):
                if q:
                    mixed.append(q.pop(0))
                if not q:
                    queues.remove(q)
        posts[key] = [
            {"source": i["source"], "url": i["url"], "headline": i["title"],
             "why": i["snippet"][:200], "context": ""}
            for i in mixed[:8]
        ]
    return posts


def esc(s):
    return html.escape(s or "", quote=True)


PER_PAGE = 4


def render(posts, alerts, notes, now_utc):
    local = now_utc.astimezone(ZoneInfo(TIMEZONE))
    day = f"{local:%a} {local.day} {local:%b}"
    clock = f"{local.hour % 12 or 12}:{local:%M}{'am' if local.hour < 12 else 'pm'}"

    content = []  # (section label, [posts]) per page
    for key, label in SECTIONS:
        chunk = posts.get(key) or []
        for n in range(0, len(chunk), PER_PAGE):
            content.append((label, chunk[n:n + PER_PAGE]))
    total_posts = sum(len(p) for _, p in content)
    total = len(content)

    pages = []
    alert_html = "".join(f'<p class="alert">{esc(a)}</p>' for a in alerts)
    if total:
        count = (f"{total_posts} stories on {total} pages" if total > 1
                 else f"{total_posts} stories on 1 page")
    else:
        count = "Nothing to read today"
    pages.append(
        '<section class="page cover" data-label="">'
        f'<h1 class="pixel" tabindex="-1">{esc(day)}</h1>'
        f'<p class="meta">Updated {esc(clock)}</p>'
        f'<p class="count">{esc(count)}</p>{alert_html}</section>'
    )
    for n, (label, chunk) in enumerate(content, 1):
        items = []
        for p in chunk:
            url = p["url"] if p["url"].startswith(("http://", "https://")) else "#"
            summary = f'<p>{esc(p.get("why"))}</p>' if p.get("why") else ""
            if p.get("context"):
                summary += f'<p class="ctx"><span>Backstory</span> {esc(p["context"])}</p>'
            items.append(
                '<li><details>'
                f'<summary><span class="src">{esc(p["source"])}</span>'
                f'<span class="hed">{esc(p["headline"])}</span></summary>'
                f'<div class="more">{summary}'
                f'<a href="{esc(url)}">Read on {esc(p["source"])}</a></div>'
                '</details></li>'
            )
        pages.append(
            f'<section class="page" data-label="{n} of {total}">'
            f'<h2 tabindex="-1">{esc(label)}</h2><ul>{"".join(items)}</ul></section>'
        )
    note_html = "".join(f"<p>{esc(t)}</p>" for t in notes)
    pages.append(
        '<section class="page end" data-label="">'
        '<h2 class="pixel" tabindex="-1">That&#39;s everything.</h2>'
        f'<p class="meta">Back tomorrow morning.</p><div class="notes">{note_html}</div></section>'
    )

    return (TEMPLATE
            .replace("{{GENERATED}}", now_utc.isoformat())
            .replace("{{DAY}}", esc(day))
            .replace("{{PAGES}}", "\n".join(pages)))


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
    alerts, notes = [], []
    if not items:
        posts = {k: [] for k, _ in SECTIONS}
        alerts.append("No sources loaded today. Open the Actions tab on GitHub to see why.")
    else:
        try:
            posts = curate(items)
        except Exception as e:
            print(f"[agent] giving up: {e}")
            posts = fallback(items)
            alerts.append("The curating step failed, so these are raw headlines. "
                          "Usually that means the API key or credit. Check the Actions log.")
    if failed:
        notes.append("Didn't load today: " + ", ".join(failed) + ".")

    docs = os.path.join(HERE, "docs")
    os.makedirs(docs, exist_ok=True)
    write_app_files(docs)
    with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as f:
        f.write(render(posts, alerts, notes, now))
    print("[done] docs/index.html written")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Feed</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Feed">
<meta name="theme-color" content="#E3E4DF" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1A1B19" media="(prefers-color-scheme: dark)">
<link rel="apple-touch-icon" href="icon-180.png">
<link rel="manifest" href="manifest.json">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible+Next:wght@400;700&family=Silkscreen&display=swap" rel="stylesheet">
<style>
  :root { --paper:#E3E4DF; --ink:#1B1C1A; --faded:#595A55; --rule:#B7B8B1; }
  @media (prefers-color-scheme: dark) {
    :root { --paper:#1A1B19; --ink:#DCDDD7; --faded:#9A9B95; --rule:#3B3C39; }
  }
  * { box-sizing: border-box; }
  html { background: var(--paper); -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font: 17px/1.45 "Atkinson Hyperlegible Next", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    padding-top: env(safe-area-inset-top, 0px);
  }
  main { max-width: 34rem; margin: 0 auto; padding: 1.25rem 1.5rem 7rem; }
  .pixel { font-family: "Silkscreen", ui-monospace, Menlo, monospace; font-weight: 400; letter-spacing: .02em; }
  :focus { outline: none; }
  :focus-visible { outline: 3px solid var(--ink); outline-offset: 3px; }

  /* Top strip: like the status line on an e-reader */
  .strip { display: flex; justify-content: space-between; font-size: .8rem; color: var(--faded);
           padding-bottom: .6rem; border-bottom: 1px solid var(--rule); margin-bottom: 1.5rem; }
  #stale { background: var(--ink); color: var(--paper); padding: .75rem 1rem; margin: 0 0 1.25rem; }

  /* Cover */
  .cover { padding-top: 18vh; }
  .cover h1 { font-size: clamp(2.4rem, 12vw, 3.4rem); line-height: 1; margin: 0 0 1rem; }
  .meta { color: var(--faded); margin: 0; }
  .count { margin: .25rem 0 0; }
  .alert { background: var(--ink); color: var(--paper); padding: .75rem 1rem; margin: 2rem 0 0; }

  /* Story pages */
  h2 { font-size: 1.35rem; margin: 0 0 .5rem; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { border-bottom: 1px solid var(--rule); }
  summary { list-style: none; cursor: pointer; padding: 1rem 2rem 1rem 0; position: relative; min-height: 44px; }
  summary::-webkit-details-marker { display: none; }
  summary::after { content: "+"; position: absolute; right: .25rem; top: 1rem; color: var(--faded); font-size: 1.2rem; line-height: 1.2; }
  details[open] summary::after { content: "\2212"; }
  .src { display: block; font-size: .8rem; color: var(--faded); margin-bottom: .15rem; }
  .hed { display: block; font-weight: 700; line-height: 1.3; }
  .more { padding: 0 0 1.1rem; }
  .more p { margin: 0 0 .6rem; }
  .more a { color: inherit; text-underline-offset: .2em; }
  .ctx { color: var(--faded); font-size: .92rem; }
  .ctx span { font-weight: 700; }

  /* End */
  .end { padding-top: 18vh; }
  .end h2 { font-size: 1.6rem; }
  .notes p { margin: 2rem 0 0; color: var(--faded); font-size: .85rem; }

  /* Page turning (only when JS runs; without it, everything shows as one list) */
  .js .page[hidden] { display: none; }
  nav { display: none; }
  .js nav {
    display: flex; align-items: center; justify-content: space-between; gap: 1rem;
    position: fixed; left: 0; right: 0; bottom: 0; background: var(--paper);
    border-top: 1px solid var(--rule);
    padding: .75rem 1.5rem calc(.75rem + env(safe-area-inset-bottom, 0px));
  }
  nav button {
    font: inherit; font-weight: 700; color: var(--ink); background: none;
    border: 2px solid var(--ink); padding: .6rem 1.25rem; min-width: 6.5rem; min-height: 48px; cursor: pointer;
  }
  nav button:active { background: var(--ink); color: var(--paper); }
  nav button[disabled] { visibility: hidden; }
  #where { color: var(--faded); font-size: .85rem; }

  /* The one flourish: an e-ink style flash when the page turns */
  .flash main { visibility: hidden; }
  @media (prefers-reduced-motion: reduce) { .flash main { visibility: visible; } }
</style>
</head>
<body data-generated="{{GENERATED}}">
<main>
  <p id="stale" hidden>This feed hasn't updated since {{DAY}}. Open the Actions tab on GitHub to see what broke.</p>
  <div class="strip" aria-hidden="true"><span>{{DAY}}</span></div>
{{PAGES}}
</main>
<nav aria-label="Pages">
  <button id="back" type="button">Back</button>
  <span id="where" aria-live="polite"></span>
  <button id="next" type="button">Next</button>
</nav>
<script>
(function () {
  var made = new Date(document.body.dataset.generated);
  if (isNaN(made) || Date.now() - made > 30 * 3600 * 1000) {
    document.getElementById("stale").hidden = false;
  }

  document.documentElement.classList.add("js");
  var pages = Array.prototype.slice.call(document.querySelectorAll(".page"));
  var back = document.getElementById("back"), next = document.getElementById("next");
  var where = document.getElementById("where");
  var key = "page:" + document.body.dataset.generated, i = 0;
  var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  try { i = Math.min(parseInt(localStorage.getItem(key), 10) || 0, pages.length - 1); } catch (e) {}

  function show(n, turned) {
    i = Math.max(0, Math.min(n, pages.length - 1));
    pages.forEach(function (p, k) { p.hidden = k !== i; });
    var label = pages[i].dataset.label || "";
    where.textContent = label;
    back.disabled = i === 0;
    next.disabled = i === pages.length - 1;
    next.textContent = i === 0 ? "Start" : "Next";
    try { localStorage.setItem(key, i); } catch (e) {}
    window.scrollTo(0, 0);
    if (turned) {
      var h = pages[i].querySelector("[tabindex='-1']");
      if (h) h.focus({ preventScroll: true });
      if (!still) {
        document.body.classList.add("flash");
        setTimeout(function () { document.body.classList.remove("flash"); }, 90);
      }
    }
  }

  back.addEventListener("click", function () { show(i - 1, true); });
  next.addEventListener("click", function () { show(i + 1, true); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "ArrowRight") show(i + 1, true);
    if (e.key === "ArrowLeft") show(i - 1, true);
  });
  var x0 = null, y0 = null;
  document.addEventListener("touchstart", function (e) {
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
  }, { passive: true });
  document.addEventListener("touchend", function (e) {
    if (x0 === null) return;
    var dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
    if (Math.abs(dx) > 60 && Math.abs(dy) < 50) show(dx < 0 ? i + 1 : i - 1, true);
    x0 = null;
  }, { passive: true });

  show(i, false);

  // From the home screen there's no pull-to-refresh, so reload after an hour away.
  var loaded = Date.now();
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && Date.now() - loaded > 3600 * 1000) location.reload();
  });
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
