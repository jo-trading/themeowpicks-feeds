# TheMeowPicks product feed

Public product feed for [TheMeowPicks](https://www.themeowpicks.com), a curated cat-only marketplace.
This repo exists to host the feed at a stable URL so ad networks can fetch it on a schedule.

## The feed

**AWIN advertiser feed:** [`awin_feed.csv`](awin_feed.csv)

Raw URL for network fetch:
```
https://raw.githubusercontent.com/jo-trading/themeowpicks-feeds/main/awin_feed.csv
```

One row per buyable variant. Columns: `product_id, product_name, description, product_url,
image_url, price, currency, brand, category, colour, size, ean, mpn, in_stock, condition`.

What is left out on purpose:
- Accessories and spares (replacement, refill, insert, carton inlay) are not advertised separately.
- Off-site affiliate-partner products (tagged `affiliate` / `partner`) are not included.
- No cost price. Only the public selling price ships in the feed.

## How it updates

`build_awin_feed.py` reads the live Shopify store (the single source of truth) and rewrites
`awin_feed.csv`. A GitHub Actions workflow ([`.github/workflows/awin-feed.yml`](.github/workflows/awin-feed.yml))
runs it every day at 06:00 UTC and commits any change, so the feed refreshes without depending on a
local machine. You can also run it on demand from the repo's **Actions** tab with **Run workflow**.

Shopify credentials are stored as encrypted GitHub Actions secrets (`SHOPIFY_STORE`,
`SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`) and are never committed to the repo.
