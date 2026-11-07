name: Update data

on:
  schedule:
    - cron: "30 22 * * 1-5"
    - cron: "45 12 * * 1-5"
  workflow_dispatch:
  push:
    branches: [main]
    paths:
      - "data/events.json"
      - "data/config.json"
      - "data/news_rules.json"

permissions:
  contents: write

concurrency:
  group: update-data
  cancel-in-progress: false

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: scripts/requirements.txt

      - name: Install dependencies
        run: pip install -r scripts/requirements.txt

      - name: Fetch announcements
        env:
          PYTHONUNBUFFERED: "1"
        run: python scripts/fetch_news.py

      # The publishers' own indexes are read on the same schedule as the live
      # feeds, over a window wide enough to absorb a few missed runs. Reading
      # them daily and historically through the same code is what keeps the
      # record from having a seam where the backfill stopped.
      - name: Fetch from publisher APIs
        env:
          PYTHONUNBUFFERED: "1"
          NYT_API_KEY: ${{ secrets.NYT_API_KEY }}
          GUARDIAN_API_KEY: ${{ secrets.GUARDIAN_API_KEY }}
        run: |
          # Whichever keys are configured; skip quietly when neither is, so the
          # daily job keeps working before the keys are added.
          if [ -n "$NYT_API_KEY" ] && [ -n "$GUARDIAN_API_KEY" ]; then SOURCE=both
          elif [ -n "$NYT_API_KEY" ]; then SOURCE=nyt
          elif [ -n "$GUARDIAN_API_KEY" ]; then SOURCE=guardian
          else echo "No publisher API keys configured; skipping."; exit 0; fi
          python scripts/fetch_api.py --source "$SOURCE" \
            --from "$(date -u -d '10 days ago' +%Y-%m-%d)"

      # The fetchers code new arrivals with keyword-v1, which is a weak
      # instrument for something semantic. Re-coding them with the model keeps
      # the corpus on one coder instead of letting a keyword-coded tail grow
      # at the recent end of the series, where the analysis is most sensitive.
      # Items already in llm_codes.jsonl are skipped, so this only touches the
      # day's new rows: a few dozen items, a fraction of a cent.
      - name: Code new arrivals with the model
        env:
          PYTHONUNBUFFERED: "1"
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          ANTHROPIC_WORKSPACE_ID: ${{ secrets.ANTHROPIC_WORKSPACE_ID }}
        run: |
          if [ -z "$ANTHROPIC_API_KEY" ]; then
            echo "No ANTHROPIC_API_KEY configured; new items keep their"
            echo "keyword-v1 codes. Add the secret to close this gap."
            exit 0
          fi
          # Coding enriches data that is already on disk, so a refusal from
          # the API must not take the prices and the commit down with it --
          # those steps come after this one in the same job, and a key that
          # expires overnight would otherwise stop the whole pipeline rather
          # than one pass of it. The guard above only catches a secret that is
          # missing; an expired key is present, answers 401, and
          # code_frames.py rightly treats 4xx as final and returns 3.
          #
          # Nothing is lost by carrying on. New arrivals keep the keyword-v1
          # codes the fetchers gave them, and because code_frames skips items
          # already in llm_codes.jsonl, the next run that gets through codes
          # exactly what this one missed. Warned rather than swallowed, so a
          # key that has genuinely died shows up on every run until it is
          # replaced instead of going quiet.
          status=0
          if ! python scripts/prepare_codes.py; then
            echo "::warning title=Framing not coded::prepare_codes.py failed, so"\
                 "the day's new announcements keep their keyword-v1 codes."
            exit 0
          fi
          # Whatever the coding pass wrote before stopping is already saved,
          # so apply runs either way and partial progress lands now rather
          # than waiting for a clean run.
          python scripts/code_frames.py code --model claude-sonnet-5-5 || status=$?
          python scripts/code_frames.py apply || status=$?
          if [ "$status" -ne 0 ]; then
            echo "::warning title=Framing not fully coded::The model pass exited"\
                 "$status. New announcements keep their keyword-v1 codes and will"\
                 "be re-coded by the next run that succeeds. If this repeats,"\
                 "check the ANTHROPIC_API_KEY secret and the credit balance."
          fi

      - name: Fetch prices and thumbnails
        env:
          PYTHONUNBUFFERED: "1"
        run: python scripts/update_data.py

      - name: Commit updated data
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/prices.json data/thumbnails.json data/events.json \
                  data/news_seen.json data/coverage.json \
                  data/llm_codes.jsonl data/keyword_codes.jsonl
          if git diff --cached --quiet; then
            echo "No data changes."
          else
            git commit -m "Update data $(date -u +%Y-%m-%d)"
            git pull --rebase
            git push
          fi
