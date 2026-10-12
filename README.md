# tribunal-tracker

Tracks the [Australian Competition Tribunal](https://www.competitiontribunal.gov.au/current-matters)
and sends a push notification via [ntfy.sh](https://ntfy.sh) when:

- a new matter appears on the [Current matters](https://www.competitiontribunal.gov.au/current-matters) page, or
- documents are added, changed or removed in a tracked matter.

It runs on GitHub Actions every 30 minutes (at :07 and :37 past the hour).

## Notifications

Subscribe to the topic named in `NTFY_TOPIC` in
[`.github/workflows/scrape.yml`](.github/workflows/scrape.yml) in the ntfy
app. Anyone who knows the topic name can read it. For a private one, set a
repository variable called `NTFY_TOPIC` (Settings → Secrets and variables →
Actions → Variables) and it will be used instead.

## Triggering accc-mergers

When a run commits a change to any `matters/<slug>/documents.json`, the
workflow starts the
[Scrape Tribunal](https://github.com/nwbort/accc-mergers/actions/workflows/scrape-tribunal.yml)
workflow in `nwbort/accc-mergers`, so new filings reach mergers.fyi without
waiting for its hourly weekday schedule. This needs a repository secret
`ACCC_MERGERS_TOKEN`: a fine-grained personal access token with access to
`nwbort/accc-mergers` only and the **Actions: Read and write** permission.
Without it the step is skipped.

## Tracked matters

Listed in [`matters.txt`](matters.txt), one URL slug per line (e.g.
`act-1-of-2026` for `/current-matters/act-1-of-2026`). Add a line to start
tracking another matter. The first scrape of a new matter records a baseline
without sending notifications.

## Output

- [`current-matters.json`](current-matters.json): the Current matters list.
- `matters/<slug>/documents.json`: the matter's filings table.
- `matters/<slug>/documents/`: downloaded copies of the filings, named
  `<asset id>-<filename>` because filenames like `Directions.pdf` repeat.

Only links to the tribunal's own site are downloaded. The tribunal's own
decisions (directions, determinations) are the exception: they link to a
`/decisions/tribunal-decisions?sq_content_src=…` page that wraps a Federal
Court judgment, not to a file. For those the scraper follows the judgment to
its "Original Word Document" on fedcourt.gov.au and saves the DOCX (e.g.
`2026ACOMPT0002.docx`); the row also gets a `judgment_url` naming the
judgment page. A decision already saved isn't followed again.

```json
{
  "matter": {
    "number": "ACT 1 of 2026",
    "title": "Application by Coles Supermarkets Australia Pty Ltd",
    "url": "https://www.competitiontribunal.gov.au/current-matters/act-1-of-2026"
  },
  "documents": [
    {
      "date": "2026-07-21",
      "filed_by": "-",
      "description": "Directions",
      "confidentiality": "Non-confidential",
      "url": "https://www.competitiontribunal.gov.au/__data/assets/pdf_file/0020/600284/Directions.pdf",
      "url_gh": "/matters/act-1-of-2026/documents/600284-Directions.pdf"
    }
  ]
}
```

## How it works

The site sits behind Cloudflare's managed challenge, so the scraper drives a
real Chrome (via nodriver, headful under Xvfb) to clear it, then visits each
page in the same browser session. See [`scrape.py`](scrape.py) (browser and
downloads), [`parse.py`](parse.py) (HTML parsing) and
[`notify.py`](notify.py) (diffing and ntfy).

If a page doesn't load, or its table disappears or comes back empty when it
previously had documents, the saved state for that page is left untouched,
which avoids false alarms. The run is then marked failed in Actions.

## Running locally

```bash
pip install -r requirements.txt
./scrape.sh                          # all matters in matters.txt
xvfb-run -a python scrape.py act-3-of-2026   # just one matter
NTFY_TOPIC=my-topic ./scrape.sh      # also send notifications
```

Without `NTFY_TOPIC` the notifications are only printed. To parse a saved
page without a browser:

```bash
python parse.py documents page.html
python parse.py current-matters page.html
```
