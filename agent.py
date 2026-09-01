#!/usr/bin/env python3
"""
Power Electronics & EV News Agent
=================================
Fetches news on power electronics, power modules, automotive EV, and
AMB/DCB ceramic substrates from public sources (Google News RSS +
industry feeds), scores relevance, deduplicates across days, and renders
a self-contained HTML daily digest.

Optionally merges posts captured from your LinkedIn feed by
linkedin_reader.py (run on your own machine; see README).

Pure standard library - no pip installs required.

Usage:
    python3 agent.py                 # generate today's digest
    python3 agent.py --fresh         # ignore cross-day dedupe memory
    python3 agent.py --keep-all      # ignore relevance filter (debug)
"""

import argparse
import datetime as dt
import difflib
import email.utils
import gzip
import hashlib
import html as htmllib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
DIGEST_DIR = os.path.join(BASE_DIR, "digests")
SEEN_PATH = os.path.join(DATA_DIR, "seen.json")


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get(url, cfg, retries=2):
    """Fetch a URL with a browser-ish UA; returns decoded text."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": cfg["user_agent"],
                    "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Encoding": "gzip",
                },
            )
            with urllib.request.urlopen(req, timeout=cfg["request_timeout_seconds"]) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed for {url}: {last_err}")


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = TAG_RE.sub(" ", text)
    text = htmllib.unescape(text)
    return WS_RE.sub(" ", text).strip()


def parse_date(s):
    """Best-effort date parsing for RSS pubDate / Atom timestamps."""
    if not s:
        return None
    s = s.strip()
    try:
        return email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        pass
    try:  # ISO 8601 (Atom)
        s2 = s.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(s2)
    except ValueError:
        pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


def local_tag(el):
    return el.tag.rsplit("}", 1)[-1].lower()


def parse_feed(xml_text, feed_name):
    """Minimal RSS 2.0 / Atom / RDF parser -> list of item dicts."""
    items = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", errors="replace"))
    except ET.ParseError as e:
        log(f"  ! XML parse error in {feed_name}: {e}")
        return items

    def walk(node, out):
        for el in node:
            tag = local_tag(el)
            if tag in ("item", "entry"):
                out.append(el)
            else:
                walk(el, out)

    entries = []
    walk(root, entries)

    for entry in entries:
        title, link, desc, date_str, src = "", "", "", "", feed_name
        for el in entry:
            tag = local_tag(el)
            if tag == "title":
                title = strip_html(el.text or "")
            elif tag == "link":
                link = (el.get("href") or el.text or "").strip()
            elif tag in ("description", "summary", "content"):
                if not desc:
                    desc = strip_html(el.text or "")
                elif el.text and len(strip_html(el.text)) > len(desc):
                    desc = strip_html(el.text)
            elif tag in ("pubdate", "published", "updated", "date"):
                if not date_str:
                    date_str = (el.text or "").strip()
            elif tag == "source":
                src = (el.text or "").strip() or feed_name
        if not link and not title:
            continue
        items.append(
            {
                "title": WS_RE.sub(" ", title).strip(),
                "url": link,
                "snippet": desc,
                "source": src,
                "feed": feed_name,
                "published": parse_date(date_str),
                "date_str": date_str,
            }
        )
    return items


def google_news_url(query):
    q = urllib.parse.quote_plus(query)  # type: ignore[attr-defined]
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def norm_title_hash(title):
    norm = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()


def age_str(published, now):
    if not published:
        return ""
    d = now - published
    hours = d.total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


# ----------------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------------

class Scorer:
    def __init__(self, categories):
        self.cats = []
        for cat in categories:
            compiled = [(re.compile(r"\b(?:" + pat + r")s?\b", re.IGNORECASE), w, label)
                        for pat, w, label in cat["keywords"]]
            self.cats.append({**cat, "compiled": compiled})

    def best_category(self, text):
        """Return (category, score, matched_labels) for the best-matching category."""
        best = (None, 0.0, [])
        for cat in self.cats:
            total, labels = 0, []
            for rx, weight, label in cat["compiled"]:
                hits = rx.findall(text)
                if hits:
                    # repeat bonus only for strong terms (weak terms must not
                    # cross the threshold just by appearing often in a snippet)
                    bonus = min(len(hits) - 1, 2) if weight >= 8 else 0
                    total += weight + bonus
                    labels.append((weight, label))
            priority = cat.get("priority", 1.0)
            if total * priority > best[1] and total > 0:
                labels.sort(reverse=True)
                best = (cat, float(total), [l for _, l in labels])
        return best


# ----------------------------------------------------------------------------
# seen-memory (cross-day dedupe)
# ----------------------------------------------------------------------------

def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_seen(seen, keep_days):
    today = dt.date.today().isoformat()
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    seen = {h: d for h, d in seen.items() if d >= cutoff}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    return seen


# ----------------------------------------------------------------------------
# linkedin posts (captured by linkedin_reader.py)
# ----------------------------------------------------------------------------

def load_linkedin_posts(cfg, lookback):
    li_cfg = cfg.get("linkedin", {})
    if not li_cfg.get("enabled", False):
        return []
    path = os.path.join(BASE_DIR, li_cfg.get("posts_file", "data/linkedin_posts.json"))
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    captured = parse_date(data.get("captured_at", ""))
    if captured:
        age = (dt.datetime.now(dt.timezone.utc) - captured).total_seconds() / 86400
        if age > lookback + 1:
            return []
    return data.get("posts", [])


# ----------------------------------------------------------------------------
# optional LLM summarisation (OpenAI-compatible endpoint)
# ----------------------------------------------------------------------------

def llm_summarize(items, cfg):
    llm = cfg.get("llm_summary", {})
    api_key = os.environ.get(llm.get("api_key_env", "OPENAI_API_KEY"), "")
    if not llm.get("enabled") or not api_key:
        return
    url = llm["api_base"].rstrip("/") + "/chat/completions"
    limit = min(len(items), llm.get("max_items", 12))
    for item in items[:limit]:
        try:
            payload = {
                "model": llm.get("model", "gpt-4o-mini"),
                "temperature": 0.2,
                "messages": [
                    {"role": "system",
                     "content": "You write one crisp sentence (max 25 words) summarising why a power-electronics professional cares about this news item."},
                    {"role": "user",
                     "content": f"Title: {item['title']}\nSnippet: {item['snippet'][:700]}"},
                ],
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                out = json.loads(resp.read().decode())
            item["summary"] = out["choices"][0]["message"]["content"].strip()
            time.sleep(0.4)
        except Exception:  # noqa: BLE001 - summarisation is best-effort
            continue


# ----------------------------------------------------------------------------
# HTML rendering
# ----------------------------------------------------------------------------

def esc(s):
    return htmllib.escape(s or "", quote=True)


def render_card(item, now):
    title = esc(item["title"])
    meta_bits = [f'<span class="src">{esc(item["source"])}</span>']
    a = age_str(item.get("published"), now)
    if a:
        meta_bits.append(f'<span>{a}</span>')
    chips = "".join(f'<span class="chip">{esc(c)}</span>' for c in item.get("chips", [])[:3])
    if chips:
        meta_bits.append(chips)
    meta = ' <span class="dot">·</span> '.join(meta_bits)
    body = ""
    text = item.get("summary") or item.get("snippet") or ""
    if text:
        if len(text) > 320:
            text = text[:317].rsplit(" ", 1)[0] + "…"
        body = f'<p class="snippet">{esc(text)}</p>'
    url = item.get("url") or ""
    if url:
        title = f'<a href="{esc(url)}" target="_blank" rel="noopener">{title}</a>'
    return (
        f'<div class="card"><h3>{title}</h3>'
        f'<div class="meta">{meta}</div>{body}</div>'
    )


def render_html(doc, categories, today, counts, linkedin_items, stats):
    now = doc["now"]
    secs = []
    if doc["top"]:
        cards = "".join(render_card(i, now) for i in doc["top"])
        secs.append(
            f'<section><h2><span class="ico">🔥</span> Top stories today</h2>{cards}</section>'
        )
    for cat in categories:
        items = doc["categories"].get(cat["id"], [])
        if items:
            cards = "".join(render_card(i, now) for i in items)
            secs.append(
                f'<section><h2><span class="ico">{cat["icon"]}</span> {esc(cat["name"])}'
                f'<span class="count">{len(items)}</span></h2>{cards}</section>'
            )
        else:
            look = cat.get("lookback_days", 4)
            secs.append(
                f'<section><h2><span class="ico">{cat["icon"]}</span> {esc(cat["name"])}'
                f'<span class="count">0</span></h2>'
                f'<div class="card quiet"><p class="snippet">Nothing new in the last {look} days — '
                f"this niche is often quiet. Anything that publishes will appear here tomorrow.</p></div></section>"
            )
    if linkedin_items:
        cards = "".join(render_card(i, now) for i in linkedin_items)
        secs.append(
            f'<section class="li"><h2><span class="ico">💼</span> From your LinkedIn feed'
            f'<span class="count">{len(linkedin_items)}</span></h2>{cards}</section>'
        )
    if not secs:
        secs = ['<section><h2>No matching news found</h2>'
                '<p class="snippet">Try increasing <code>lookback_days</code> or lowering '
                '<code>min_score</code> in config.json.</p></section>']

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Power &amp; EV Digest — {today}</title>
<style>
 :root {{ --ink:#182430; --mut:#6b7a89; --line:#e3e9ef; --acc:#0b62c4; --bg:#f4f7fa; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; font:15px/1.55 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
        color:var(--ink); background:var(--bg); }}
 .wrap {{ max-width:880px; margin:0 auto; padding:28px 20px 60px; }}
 header.hero {{ background:linear-gradient(135deg,#0b3d7a 0%,#0b62c4 60%,#128f6b 100%);
   color:#fff; border-radius:16px; padding:26px 28px; margin-bottom:26px; }}
 header.hero h1 {{ margin:0 0 6px; font-size:26px; }}
 header.hero .sub {{ opacity:.9; font-size:14px; }}
 header.hero .stats {{ margin-top:12px; font-size:13px; opacity:.95; }}
 section {{ margin-bottom:30px; }}
 h2 {{ font-size:19px; margin:0 0 12px; padding-bottom:8px; border-bottom:2px solid var(--line);
      display:flex; align-items:center; gap:9px; }}
 h2 .ico {{ font-size:20px; }}
 .count {{ background:#e2eefc; color:#0b62c4; font-size:12px; font-weight:700;
   border-radius:20px; padding:2px 9px; }}
 .card {{ background:#fff; border:1px solid var(--line); border-radius:12px;
   padding:14px 18px; margin-bottom:10px; box-shadow:0 1px 2px rgba(16,42,67,.04); }}
 .card h3 {{ margin:0 0 6px; font-size:15.5px; line-height:1.4; }}
 .card a {{ color:var(--acc); text-decoration:none; }}
 .card a:hover {{ text-decoration:underline; }}
 .meta {{ font-size:12.5px; color:var(--mut); display:flex; flex-wrap:wrap; gap:6px; align-items:center; }}
 .meta .dot {{ opacity:.6; }}
 .src {{ font-weight:600; color:#41546a; }}
 .chip {{ background:#eef3f7; border:1px solid var(--line); border-radius:12px; padding:0 8px; font-size:11.5px; }}
 .snippet {{ margin:8px 0 0; color:#3d4c5c; font-size:13.5px; }}
 section.li .card {{ border-left:4px solid #0a66c2; }}
 footer {{ color:var(--mut); font-size:12px; text-align:center; margin-top:34px; line-height:1.7; }}
</style></head><body><div class="wrap">
<header class="hero">
  <h1>⚡ Power Electronics &amp; EV Daily Digest</h1>
  <div class="sub">{today} · 1\u20df EV &amp; on-board chargers \u00b7 2\u20df module packaging &amp; efficiency \u00b7 3\u20df substrates, bonding wires, solder &amp; sinter pastes</div>
  <div class="stats">{stats}</div>
</header>
{''.join(secs)}
<footer>Generated {doc['now'].strftime('%d %b %Y %H:%M')} by your local news agent ·
{counts} sources · public web + LinkedIn capture ·
public sources incl. Google News, {', '.join(sorted({f['name'] for f in doc['feeds']}))}</footer>
</div></body></html>"""


# ----------------------------------------------------------------------------
# main pipeline
# ----------------------------------------------------------------------------

def run(fresh=False, keep_all=False):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    now = dt.datetime.now(dt.timezone.utc)
    today = dt.date.today()
    lookback = cfg.get("lookback_days", 2)
    min_score = 0 if keep_all else cfg.get("min_score", 8)
    per_cat = cfg.get("max_items_per_category", 8)
    scorer = Scorer(cfg["categories"])

    # 1. collect
    feeds = [("Google News", google_news_url(q)) for q in cfg["google_news_queries"]]
    feeds += [(f["name"], f["url"]) for f in cfg["direct_feeds"]]
    all_items = []
    for name, url in feeds:
        try:
            items = parse_feed(http_get(url, cfg), name)
            # Google News: strip " - Publisher" / " | Publisher" suffixes and noisy descriptions
            if name == "Google News":
                for it in items:
                    if it["source"] != "Google News":
                        for sep in (" - ", " | "):
                            suffix = sep + it["source"]
                            if it["title"].endswith(suffix):
                                it["title"] = it["title"][: -len(suffix)]
                                break
                    if it["snippet"].startswith("View Full Coverage") or it["snippet"].startswith("List"):
                        it["snippet"] = ""
            log(f"  + {name:<28} {len(items):>3} items")
            all_items.extend(items)
        except Exception as e:  # noqa: BLE001
            log(f"  ! {name}: {e}")

    # 1b. drop research publications & blocked domains
    excl_domains = [d.lower() for d in cfg.get("exclude_domains", [])]
    res_re = [re.compile(p, re.IGNORECASE) for p in cfg.get("research_title_patterns", [])]
    dom_re = re.compile(r"https?://([^/]+)/")
    kept = []
    for it in all_items:
        m = dom_re.match(it["url"] or "")
        domain = (m.group(1).lower() if m else "").removeprefix("www.")
        if any(domain == d or domain.endswith("." + d) for d in excl_domains):
            continue
        if any(rx.search(it["title"]) for rx in res_re):
            continue
        kept.append(it)
    log(f"= {len(all_items)} fetched -> {len(kept)} after research/domain filter")
    all_items = kept

    # 2. normalise dates, apply per-category recency + scoring
    for it in all_items:
        if it["published"] is None:
            it["published"] = now  # undated -> assume fresh
        elif it["published"].tzinfo is None:
            it["published"] = it["published"].replace(tzinfo=dt.timezone.utc)
    max_age = max(14, lookback)
    cat_lookback = {c["id"]: c.get("lookback_days", lookback) for c in cfg["categories"]}
    penalty_res = [(re.compile(p, re.IGNORECASE), pen)
                   for p, pen in cfg.get("global_penalty_patterns", [])]

    scored = []
    for it in all_items:
        age_days = (now - it["published"]).total_seconds() / 86400
        if not (-1 <= age_days <= max_age):
            continue
        cat, score, chips = scorer.best_category(f"{it['title']} {it['snippet']}")
        for rx, pen in penalty_res:
            if rx.search(it["title"]):
                score += pen
        if cat and age_days <= cat_lookback[cat["id"]] and score >= cat.get("min_score", min_score):
            it["age_days"] = max(age_days, 0)
            it["category"] = cat["id"]
            it["score"] = score
            it["chips"] = chips
            scored.append(it)
    log(f"= {len(all_items)} candidates -> {len(scored)} relevant & fresh")

    # 4. dedupe (within run: fuzzy titles + token overlap)
    def toks(t):
        return {w for w in re.split(r"[^a-z0-9]+", t.lower()) if len(w) > 2}

    scored.sort(key=lambda x: -x["score"])
    deduped = []
    for it in scored:
        norm = re.sub(r"[^a-z0-9 ]", "", it["title"].lower())
        tset = toks(it["title"])
        dup = False
        for kept in deduped:
            kept_norm = re.sub(r"[^a-z0-9 ]", "", kept["title"].lower())
            if difflib.SequenceMatcher(None, norm, kept_norm).ratio() > 0.85:
                dup = True
                break
            kset = toks(kept["title"])
            union = tset | kset
            if union and len(tset & kset) / len(union) >= 0.55:
                dup = True
                break
        if not dup:
            deduped.append(it)
    log(f"= {len(scored)} relevant -> {len(deduped)} after dedupe")

    # 4b. brand flood control (max N stories per company/brand per digest)
    brand_re = re.compile(
        r"\b(" + "|".join(cfg.get("flood_brands", [])) + r")\b", re.IGNORECASE
    )
    max_per_brand = cfg.get("max_per_brand", 2)
    brand_alias = {"texas instruments": "ti", "mitsubishi": "mitsubishi electric"}
    brand_counts = {}
    flood_filtered = []
    for it in deduped:
        m = brand_re.search(it["title"])
        if m:
            b = brand_alias.get(m.group(1).lower(), m.group(1).lower())
            brand_counts[b] = brand_counts.get(b, 0) + 1
            if brand_counts[b] > max_per_brand:
                continue
        flood_filtered.append(it)
    deduped = flood_filtered
    log(f"= {len(deduped)} after brand flood control")

    # 5. cross-day dedupe memory
    seen = load_seen()
    today_iso = today.isoformat()
    final = []
    if fresh:
        final = deduped
    else:
        for it in deduped:
            h = norm_title_hash(it["title"])
            first = seen.get(h)
            if first is None or first == today_iso:
                seen[h] = today_iso
                final.append(it)
    log(f"= {len(final)} after cross-day memory ({len(seen)} hashes tracked)")

    # 6. assemble digest structure (per-category cap + sort order)
    by_cat = {}
    for it in final:
        by_cat.setdefault(it["category"], []).append(it)
    cat_cfg = {c["id"]: c for c in cfg["categories"]}
    for cid, lst in by_cat.items():
        c = cat_cfg[cid]
        if c.get("sort") == "latest":
            lst.sort(key=lambda x: x["age_days"])          # newest first
        else:
            lst.sort(key=lambda x: (-x["score"], x["age_days"]))
        del lst[c.get("max_items", per_cat):]

    # top stories: lead story of each tier, in priority order
    top = [lst[0] for cid in [c["id"] for c in cfg["categories"]]
           if len(by_cat.get(cid, [])) > 0 for lst in [by_cat[cid]]]
    n_top = cfg.get("top_stories", 3)
    if len(top) < n_top:
        chosen = {id(t) for t in top}
        rest = sorted((i for i in final if id(i) not in chosen), key=lambda x: -x["score"])
        top += rest[: n_top - len(top)]
    top = top[:n_top]

    # 7. LinkedIn capture
    li_items = []
    li_posts = load_linkedin_posts(cfg, lookback)
    if li_posts:
        for p in li_posts:
            text = f"{p.get('author', '')} {p.get('text', '')}"
            cat, score, chips = scorer.best_category(text)
            if cat and score >= min_score:
                li_items.append(
                    {
                        "title": p.get("text", "")[:220] or "LinkedIn post",
                        "url": p.get("url", ""),
                        "snippet": f"by {p.get('author', 'unknown')}"
                        + (f" · {p.get('time', '')}" if p.get("time") else ""),
                        "source": "LinkedIn",
                        "published": parse_date(p.get("time", "")) or now,
                        "chips": chips,
                        "score": score,
                    }
                )
        li_items.sort(key=lambda x: -x["score"])
        del li_items[cfg.get("max_linkedin_items", 6):]

    # 8. optional LLM one-liners
    llm_summarize(top + [i for c in by_cat.values() for i in c], cfg)

    # 9. render + write
    os.makedirs(DIGEST_DIR, exist_ok=True)
    save_seen(seen, cfg.get("dedupe_memory_days", 7))
    total = sum(len(v) for v in by_cat.values())
    stats = f"{total} stories across {len(by_cat)} topic areas"
    if li_items:
        stats += f" + {len(li_items)} from your LinkedIn feed"
    doc = {"now": now, "categories": by_cat, "top": top, "feeds": cfg["direct_feeds"]}
    html = render_html(doc, cfg["categories"], today.strftime("%A, %d %B %Y"), len(feeds), li_items, stats)
    out_path = os.path.join(DIGEST_DIR, f"digest-{today.isoformat()}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    latest = os.path.join(DIGEST_DIR, "latest.html")
    with open(latest, "w", encoding="utf-8") as f:
        f.write(html)

    # archive index (landing page for GitHub Pages / local browsing)
    try:
        days = sorted(
            (fn for fn in os.listdir(DIGEST_DIR)
             if re.fullmatch(r"digest-\d{4}-\d{2}-\d{2}\.html", fn)),
            reverse=True,
        )
        rows = "".join(
            f"<li><a href='{fn}'>{fn[len('digest-'):-len('.html')]}</a></li>" for fn in days
        )
        index_html = (
            '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="0;url=latest.html">'
            "<title>Power &amp; EV Daily Digest</title></head>"
            '<body><p><a href="latest.html">Open today\'s digest</a></p>'
            f"<p>Past editions:</p><ul>{rows}</ul></body></html>"
        )
        with open(os.path.join(DIGEST_DIR, "index.html"), "w", encoding="utf-8") as f:
            f.write(index_html)
    except OSError:
        pass

    log(f"DONE -> {out_path}")
    for cat in cfg["categories"]:
        n = len(by_cat.get(cat["id"], []))
        log(f"    {cat['icon']} {cat['name']}: {n}")
    return out_path


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used by google_news_url)

    ap = argparse.ArgumentParser(description="Power Electronics & EV daily news digest")
    ap.add_argument("--fresh", action="store_true", help="ignore cross-day dedupe memory")
    ap.add_argument("--keep-all", action="store_true", help="skip relevance scoring (debug)")
    args = ap.parse_args()
    try:
        path = run(fresh=args.fresh, keep_all=args.keep_all)
        print(f"\nOpen your digest: {path}")
    except Exception as e:  # noqa: BLE001
        log(f"FATAL: {e}")
        sys.exit(1)
