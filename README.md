# Priced In

*Visualizing the Entanglements of AI News and Market Valuations*

AI announcements plotted against the stock prices of the companies they touch.
The site is static (HTML, CSS, JavaScript with D3), so it runs on GitHub Pages.
A scheduled GitHub Action refreshes the price data every weekday evening.

## Set up on GitHub

1. Create a repository and push these files to the `main` branch, keeping the folder structure
   (including `.github/workflows/` and the empty `.nojekyll` file).
2. In **Settings → Pages**, set the source to *Deploy from a branch*, branch `main`, folder `/ (root)`.
3. In **Settings → Actions → General → Workflow permissions**, choose *Read and write permissions*
   so the workflow can commit the data it fetches.
4. Open the **Actions** tab, select **Update data**, and click **Run workflow**.
   It creates `data/prices.json` and `data/thumbnails.json`; the timeline appears once they exist.

After that, the workflow runs on its own at 22:30 UTC on weekdays, and again whenever you push a change to
`data/events.json` or `data/config.json`.

## Preview locally

```bash
pip install -r scripts/requirements.txt
python scripts/update_data.py      # fetch prices and thumbnails
python -m http.server 8000         # then open http://localhost:8000
```

Opening `index.html` directly from disk won't work, because browsers block the data requests.

## Adding announcements

Edit `data/events.json`. Each entry looks like this:

```json
{
  "id": "gpt-5",
  "date": "2025-08-07",
  "title": "OpenAI releases GPT-5",
  "source": "OpenAI",
  "url": "https://openai.com/index/introducing-gpt-5/",
  "org": "openai",
  "category": "model",
  "tickers": ["MSFT"],
  "afterClose": false,
  "thumbnail": "https://example.com/optional-image.jpg"
}
```

- `org` must match an `id` in the `orgs` list of `config.json`; it decides the row in the company view.
- `category` must match a category `id`: `model`, `deals`, `compute`, `policy` or `markets`.
- `tickers` are the listed stocks the announcement plausibly affects. This is the editorial mapping
  (for example, Anthropic news to AMZN and GOOGL). Only symbols listed in `config.json` count.
- `afterClose` (optional) moves the announcement to the next trading day when it came out after the US close.
- `thumbnail` (optional) overrides the preview image. Otherwise the workflow reads the page's `og:image`.

When the workflow can't read a link, it leaves a warning on the run. Some news sites block automated requests,
so a warning doesn't always mean the link is broken; open it to check. Events without an image show a lettered placeholder.

## Changing companies or benchmarks

Edit the `tickers` and `benchmarks` lists in `data/config.json` (Yahoo Finance symbols), then push.
The workflow refetches prices. `defaultBenchmark` sets which benchmark each view starts with.

## Method notes

- Prices are daily closes adjusted for splits and dividends, from Yahoo Finance via `yfinance`.
- Market value is close × current shares outstanding, so historical values are approximate.
- The industry total is chain-linked: each day's return uses only companies with prices on both days,
  weighted by market value or equally.
- A reaction runs from the close before the announcement's trading day to the close at the end of the window
  (1, 3 or 5 trading days). "Against benchmark" subtracts the benchmark's move over the same days.
