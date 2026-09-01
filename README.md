# ⚡ Power Electronics & EV Daily News Agent

Every day it collects news **from LinkedIn posts and Google News** (plus 2 fast
trade feeds) and writes a short HTML digest in **your priority order**:

1. 🚗 **EV & On-Board Charger Module Technology** — newest first: OBCs, traction
   inverters, e-axles, 800 V, SiC/GaN designed into EVs (business/policy news filtered out)
2. 🔌 **Power Module Packaging & Efficiency** — module makers & their packaging
   tech: sintering, double-sided cooling, planar interconnects, new platforms
3. 🧱 **Packaging Materials Suppliers** — AMB/DCB/DPC substrates (Rogers-curamik,
   Ferrotec, NGK, Toshiba Materials, Denka, Maruwa…), bonding wires (Heraeus,
   Tanaka…), solder & sinter pastes (Indium Corp, Henkel, Kymera, Nihon
   Superior…), plus die-attach films, TIMs, lead frames, molding compounds

Research publications (journals, university studies) and market-report spam are
filtered out automatically. Duplicate stories are removed — and remembered for
7 days so nothing reappears tomorrow.

## What it reads

| Source | Covers |
|---|---|
| Google News — 10 targeted queries | all tiers, incl. **public LinkedIn company posts** (site:linkedin.com) |
| electrive + Electronics Weekly feeds | fast EV & power-electronics trade coverage |
| **your private LinkedIn feed** *(optional)* | `linkedin_reader.py` on your PC → shown as “From your LinkedIn feed” |

## Quick start

```bash
cd power-news-agent
python3 agent.py        # Windows: py agent.py   (or double-click run_digest.bat)
```

Then open **`digests/latest.html`** (a dated copy is kept per day).

## Run it automatically every day

**Windows (Task Scheduler)** — in a command prompt (adjust path & time):

```bat
schtasks /Create /SC DAILY /ST 07:30 /TN "PowerNewsDigest" ^
  /TR "\"C:\path\to\power-news-agent\run_digest.bat\""
```

**Linux / macOS (cron):**

```bash
crontab -e
# add (runs 07:30 daily, logs to agent.log):
30 7 * * * cd /path/to/power-news-agent && ./run_digest.sh >> agent.log 2>&1
```

Tip: point the schedule at `digests/latest.html` afterwards, or just open it
manually with your morning coffee.

## Optional: your real LinkedIn feed (⚠️ ToS risk)

`linkedin_reader.py` opens a real browser **on your PC**, uses your own
logged-in LinkedIn session, scrolls your feed gently and keeps only on-topic
posts. The next `agent.py` run shows them under **“From your LinkedIn feed”**.

> ⚠️ Automated access violates LinkedIn's Terms of Service and can risk your
> account. Run at most once a day. Skip this module entirely if you want zero
> risk — the public sources already cover the industry.

```bash
pip install playwright
playwright install chromium
python3 linkedin_reader.py     # first run: log in once when the window opens
```

Then chain both in your schedule: `linkedin_reader.py && agent.py`.

## Tune it — `config.json`

| Key | Meaning |
|---|---|
| `lookback_days` | how fresh items must be (default 2) |
| `min_score` | relevance threshold — lower = more news (default 8) |
| `max_items_per_category` | cap per section (default 8) |
| `google_news_queries` | the search phrases behind the main source |
| `categories[].keywords` | `[regex, weight, chip-label]` — the relevance brain |
| `llm_summary` | optional: set `enabled: true` + `OPENAI_API_KEY` env var for AI one-line summaries |

Useful flags: `--fresh` (ignore cross-day dedupe), `--keep-all` (debug: skip scoring).

## Files

```
agent.py            the agent (fetch → score → dedupe → HTML)
linkedin_reader.py  optional LinkedIn capture (your PC only)
config.json         sources + keywords + limits
digests/            digest-YYYY-MM-DD.html + latest.html
data/seen.json      remembers what was already shown (7-day memory)
data/linkedin_posts.json   written by linkedin_reader.py
```

## ☁️ Cloud version — GitHub Actions (free, runs without your computer)

The agent runs daily on GitHub's servers, archives every digest in the repo,
and publishes it to a bookmarkable website (GitHub Pages).

1. Create a free account at [github.com](https://github.com) if needed
2. Create a new repository, e.g. `power-news` — choose **Public**
   (Pages websites are free only for public repos; the repo contains only
   public news links, nothing private)
3. In the repo: **Add file → Upload files** → drag in everything from
   `power-news-agent-github.zip` → **Commit changes**
   *(macOS: press* `Cmd+Shift+.` *in Finder to see the `.github` folder; if it
   won't upload, create it manually — step 4)*
4. Fallback for the workflow file: **Add file → Create new file**, type
   `.github/workflows/daily-digest.yml` as the name, paste the content below,
   commit
5. Enable the website: **Settings → Pages → Source: GitHub Actions**
6. Test it now: tab **Actions** → *Daily Power & EV News Digest* →
   **Run workflow** (takes ~1 minute)
7. Bookmark your digest URL — shown under **Settings → Pages**, e.g.
   `https://yourname.github.io/power-news/`

<details><summary>The workflow file (already included as .github/workflows/daily-digest.yml)</summary>

```yaml
name: Daily Power & EV News Digest

on:
  schedule:
    # GitHub uses UTC. 06:00 UTC = 08:00 Berlin (summer) / 07:00 (winter).
    - cron: "0 6 * * *"
  workflow_dispatch: {}   # adds a manual "Run workflow" button

permissions:
  contents: write         # commit digest + dedupe memory back to the repo
  pages: write            # publish to GitHub Pages
  id-token: write

concurrency:
  group: "pages"
  cancel-in-progress: false

jobs:
  digest:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Generate today's digest
        run: python agent.py

      - name: Commit digest + 7-day dedupe memory
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add digests data
          git commit -m "digest: $(date -u +%F)" || echo "nothing new to commit"
          git push

      - name: Configure Pages
        uses: actions/configure-pages@v5

      - name: Upload site artifact
        uses: actions/upload-pages-artifact@v3
        with:
          path: digests

      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v4
```

</details>

**Good to know**
- Runs at 06:00 UTC daily; GitHub may delay it a few minutes (or rarely skip a
  day — the lookback windows catch up on the next run, nothing is lost)
- The daily commits count as repo activity, which keeps the schedule enabled
- Each day is archived in `digests/` and linked on your Pages site
- You can trigger a digest any time via **Actions → Run workflow**
- The private-LinkedIn module cannot run in the cloud (it needs your logged-in
  browser session) — cloud runs use public sources incl. public LinkedIn posts
