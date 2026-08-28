name: RIM feed aggregator

on:
  schedule:
    - cron: "15 * * * *"
  workflow_dispatch: {}

permissions:
  contents: write

concurrency:
  group: rim-aggregator
  cancel-in-progress: false

jobs:
  aggregate:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run aggregator
        run: python aggregator.py

      - name: Commit updated feed_items.json
        run: |
          git config user.name  "rim-bot"
          git config user.email "rim-bot@users.noreply.github.com"
          git add output/feed_items.json
          if git diff --cached --quiet; then
            echo "No changes to commit."
            exit 0
          fi
          git commit -m "Update feed_items.json ($(date -u +%FT%TZ))"
          for i in 1 2 3; do
            git pull --rebase --autostash origin "${GITHUB_REF_NAME}" && \
            git push origin "HEAD:${GITHUB_REF_NAME}" && exit 0
            echo "Push attempt $i failed; re-syncing and retrying..."
            sleep 3
          done
          echo "Push failed after 3 attempts."
          exit 1
