# Gastric watch

News and clinical trials for metastatic gastric and gastroesophageal cancer.

Trials load live from the ClinicalTrials.gov v2 API in the browser. News comes from
`news.json`, which a Python script builds from RSS feeds on a schedule.

## First run

```bash
pip install -r requirements.txt
python fetch_news.py --check
```

**Do the `--check` step before anything else.** It tests every feed URL and prints what
came back. Feed URLs move around, and I could not verify them all — the two NCI feeds are
confirmed working, the rest are best guesses at the current URLs. Anything that reports
broken should be fixed or switched off in `sources.json`.

Then:

```bash
python fetch_news.py
python3 -m http.server
```

Open `localhost:8000`. Opening `index.html` straight from disk will not work — browsers
block local file reads, so the news tab comes up empty. The page tells you this if it
happens.

## Putting it on a schedule

Push the folder to a GitHub repo and turn on Pages (Settings → Pages → deploy from
branch). The workflow in `.github/workflows/update-news.yml` then runs three times a day,
refreshes `news.json`, and commits it. Nothing to host and nothing to pay for.

You can also trigger it by hand from the Actions tab, which is the quickest way to confirm
it works.

## Editing sources

Everything lives in `sources.json`, no code changes needed.

- `feeds` — name, URL, and `enabled`. Add or remove outlets freely.
- `keywords` — an item is kept only if one of these appears in its title or teaser. The
  feeds are general oncology, so this is what narrows them to gastric disease. Add drug
  names as new ones become relevant.
- `retention_days` — how long items stay in the archive.
- `fetch_meta_description` — set to `false` to skip fetching article pages for blurbs.
  Faster, but more items end up headline-only.

Useful while tuning: `python fetch_news.py --loose` ignores the keyword filter entirely,
so you can see everything a feed carries and judge whether your keywords are too tight.

## Blurbs

Publisher's own words only, in this order: the feed's description, then its full-content
field, then the article page's own share blurb. If none exists, the item stays
headline-only rather than getting filler. Nothing is machine-generated, so nothing can
quietly misstate a result.

## Re-centring the map

Distances are straight-line from Lower Manhattan. To change that, edit `HOME` near the top
of the script block in `index.html`.

## What this is not

A tracker, not an advisor. Trial eligibility depends on prior lines of therapy, biomarker
status, and lab values that no filter here can see — a trial showing as recruiting nearby
may still not be a fit. It is a way to spot things worth asking the oncology team about.
