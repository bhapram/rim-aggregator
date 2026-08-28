# RIM — Option 2 Implementation Guide (GitHub Actions)

This kit moves the heavy feed-fetching **off** the RIM cloud run and onto a GitHub
Actions job that has open internet (no proxy). The job fetches all your RSS feeds,
keeps recent + risk-relevant items, and publishes one file — `feed_items.json` —
that the RIM run reads with a single WebFetch. Works for **both** the incremental
and daily-digest runs.

**You do not need to run anything on your own computer, and you do not need any API
keys.** Setup is done in the GitHub website; after that it runs itself in GitHub's
cloud. The only account required is a free GitHub account.

---

## What's in this kit

| File | Purpose |
|------|---------|
| `feeds.csv` | Your 710 sources (source, country, tier, type, url). The aggregator fetches the 491 marked `type=rss`. |
| `aggregator.py` | Python + `feedparser` job: fetch → filter to recent + risk-relevant → dedup → tag country/tier → write `output/feed_items.json`. |
| `requirements.txt` | The one dependency (`feedparser`). |
| `.github/workflows/aggregate.yml` | GitHub Action: runs the job hourly on a cron and commits the output back to the repo. |
| `output/feed_items.json` | Placeholder, overwritten on the first run. |

---

## Part A — Stand up the aggregator on GitHub

**Step 1 — Create a repository.**
On github.com → **New repository**. Name it e.g. `rim-aggregator`. **Make it Public.**
(Public matters for two reasons: the RIM run reads the output over a public URL and
can't log in; and public repos get unlimited Actions minutes. The repo only holds
code + public news links — nothing sensitive.)

**Step 2 — Add the files.**
Upload everything in this kit, preserving the folder layout — especially
`.github/workflows/aggregate.yml`. Easiest path: **Add file → Upload files**, drag
the lot in, commit. (The `.github/workflows/` folder must keep that exact path or
the Action won't be detected.)

**Step 3 — Enable Actions.**
Open the **Actions** tab. If prompted, click **"I understand my workflows, enable
them."** You'll see the "RIM feed aggregator" workflow.

**Step 4 — Run it once manually.**
Actions → **RIM feed aggregator** → **Run workflow**. Wait for it to finish (a few
minutes). It will fetch the feeds and commit an updated `output/feed_items.json`.
Open that file in the repo and confirm it now has real `items` and a `feeds_ok` /
`feeds_failed` count. **Because this runs on open internet, it will succeed on many
feeds the RIM sandbox couldn't reach** — this is where the ~300 previously-blocked
feeds finally get verified. Anything genuinely dead is listed under `failures`.

**Step 5 — Copy the public URL of the output.**
It is:

```
https://raw.githubusercontent.com/<your-username>/rim-aggregator/main/output/feed_items.json
```

Open it in a normal browser tab to confirm it loads without logging in. Keep this URL.

The cron in `aggregate.yml` now runs the job **hourly** on its own — no further action.

---

## Part B — Point RIM at the file

In `RIM_Master_Prompt.md`, replace the fetching instruction in **SOURCES → list C**
so RIM reads the one file instead of polling feeds. Use this block (paste your URL):

> **C. Aggregated feed (Option 2).** Fetch the pre-built feed once per run:
> `https://raw.githubusercontent.com/<your-username>/rim-aggregator/main/output/feed_items.json`
> It is a JSON object whose `items[]` array holds recent, risk-relevant news items,
> each already tagged with `country`, `tier`, `title`, `link`, `published`, and
> `summary`. **Do not fetch individual feeds.** From `items`, select those whose
> `published` falls within this run's window — **last ~4 hours for an incremental
> run, last 24 hours for a digest run** — then apply the tier corroboration rules,
> dedup against the ledger, translate, assess severity, and build the email as usual.
> Tier 1 institutional sources (list B) and the general web search (list A) still run
> as the corroborating / supplementary layer.

Everything else in the Master Prompt stays the same — RIM still does translation,
severity, tier-corroboration, grouping, the email, and the commit-after-send ledger.
It just gets its raw items from one clean fetch.

---

## Part C — Run incremental AND digest

You'll have **two scheduled tasks** (triggers), both reading the same file:

- **Incremental** — every 4 hours. Its prompt must state it's the incremental run
  (e.g. start with: *"This is the RIM incremental run."*) so it windows to ~4 hours
  and only sends if there are new items.
- **Digest** — daily at 05:00 UTC. Its prompt must state it's the digest run
  (*"This is the RIM daily digest run."*) so it windows to 24 hours and always sends.

Because the Master Prompt currently **defaults to digest**, be explicit in each
trigger's prompt about which mode it is — the default only applies if a run doesn't
say. (If you'd rather, I can set the default back to incremental; either works as
long as each trigger declares its mode.)

The aggregator's hourly cron keeps `feed_items.json` fresh enough for a 4-hourly
incremental. If you want it fresher, change the cron in `aggregate.yml` (e.g.
`"*/30 * * * *"` for every 30 min).

---

## Tuning (all at the top of `aggregator.py`)

- `KEYWORD_FILTER = True` keeps only risk-relevant items (smaller, sharper file).
  Set `False` to keep everything recent. Expand the multilingual `KEYWORDS` list
  any time — the more languages, the better the non-English catch.
- `WINDOW_HOURS = 30` — rolling window stored in the file (a little over 24h so the
  digest never misses edge items). RIM narrows to 4h / 24h itself.
- `PER_FEED_CAP` / `TOTAL_CAP` — keep the file WebFetch-friendly. If your item
  volume grows, lower these, or split the output by region into
  `europe.json` / `mena.json` / `ssa.json` and have RIM fetch the relevant ones.
- `MAX_WORKERS = 20` — parallel fetches; raise for speed if the runner allows.

---

## Maintenance

- **Dead feeds:** each run logs them under `failures` in `feed_items.json`. Review
  occasionally and prune or fix in `feeds.csv`.
- **Add / remove sources:** edit `feeds.csv` (columns: `source_name, country, tier,
  type, url`) and commit — no code change needed.
- **RSF tiers:** refresh the `tier` column annually when the new RSF index publishes.
- **Cost:** free. A public repo has unlimited Actions minutes; each run is a few
  minutes.

---

## Quick checklist

1. [ ] Create **public** repo `rim-aggregator`
2. [ ] Upload kit files (keep `.github/workflows/` path)
3. [ ] Enable Actions
4. [ ] Run workflow once manually; check `output/feed_items.json`
5. [ ] Copy the raw `feed_items.json` URL
6. [ ] Paste it into Master Prompt list C
7. [ ] Create the incremental trigger (every 4h, prompt says "incremental")
8. [ ] Create the digest trigger (daily 05:00 UTC, prompt says "digest")
9. [ ] Watch the first live digest land, then relax
