name: Daily feed

on:
  schedule:
    - cron: "0 13 * * *"
  workflow_dispatch:

permissions:
  contents: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: pip install feedparser anthropic
      - run: python feed.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          FEED_PROFILE: ${{ secrets.FEED_PROFILE }}
      - name: Publish today's page
        run: |
          git config user.name "dumb-feed-bot"
          git config user.email "actions@users.noreply.github.com"
          git add docs
          git commit -m "Feed for $(date -u +%F)" || echo "Nothing changed"
          git push
