#!/usr/bin/env python3
"""Build the AWIN advertiser product feed from live Shopify data.

We are the advertiser on AWIN; publishers promote these products and link to our store, so the feed
must be public products that a shopper can buy on themeowpicks.com. Rules:
  - Source of truth is Shopify (live), one row per variant.
  - EXCLUDE accessories / replacements / spares (anything tagged `hidden`, plus replacement/refill/
    spare/insert titles that may not be hidden yet).
  - EXCLUDE off-site affiliate products (tagged `affiliate` / `partner`): they redirect off our store,
    so there is no on-site sale for a publisher to earn on.
  - NO cost price (it is a public affiliate feed).
  - Availability from Shopify's availableForSale, so made-to-order / continue-policy items read
    in_stock (fixes the "everything out of stock" problem in the raw channel export).

Runs on the Mac (Admin API not reachable from Cowork). Writes awin_feed.csv, which you host at a
public URL / SFTP that AWIN fetches daily.

  python3 build_awin_feed.py
"""
import csv, os, re, sys, html

import shopify_sync as ss

HERE = os.path.dirname(os.path.abspath(__file__))
DOMAIN = "https://www.themeowpicks.com"
CURRENCY = "USD"
OUT = os.path.join(HERE, "awin_feed.csv")

# Titles that mark an accessory/spare even if the product was never tagged hidden.
ACCESSORY_WORDS = ("replacement", "refill", "spare", "insert", "carton inlay")

COLUMNS = ["product_id", "product_name", "description", "product_url", "image_url",
           "price", "currency", "brand", "category", "colour", "size",
           "ean", "mpn", "in_stock", "condition"]

Q = """query($c:String){
  products(first:50, after:$c){
    edges{ cursor node{
      handle title vendor productType status publishedAt onlineStoreUrl tags
      category { fullName }
      descriptionHtml
      featuredImage { url }
      variants(first:100){ edges{ node{
        id sku barcode price availableForSale
        image { url }
        selectedOptions { name value }
      } } }
    } }
    pageInfo{ hasNextPage } } }"""


def text(hmtl):
    s = re.sub(r"<[^>]+>", " ", hmtl or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def excluded(node):
    tags = {t.strip().lower() for t in (node.get("tags") or [])}
    if "hidden" in tags:
        return "hidden"
    if tags & {"affiliate", "partner"}:
        return "affiliate"
    tl = (node.get("title") or "").lower()
    if any(w in tl for w in ACCESSORY_WORDS):
        return "accessory-title"
    return None


def main():
    ss._load_dotenv()
    store = ss._norm_store(os.environ.get("SHOPIFY_STORE", ""))
    cid, csec = os.environ.get("SHOPIFY_CLIENT_ID"), os.environ.get("SHOPIFY_CLIENT_SECRET")
    token = os.environ.get("SHOPIFY_TOKEN")
    if cid and csec:
        token = ss.get_access_token(store, cid, csec)
    elif not token:
        sys.exit("Set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET (or SHOPIFY_TOKEN).")
    sp = ss.Shopify(store, token, "2024-10")

    rows, skipped = [], {"not-active": 0, "hidden": 0, "affiliate": 0, "accessory-title": 0}
    seen_ids = set()   # product_id must be unique; duplicate SKUs get the variant id appended
    cursor = None
    while True:
        d = sp.gql(Q, {"c": cursor}) or {}
        conn = (d.get("data") or {}).get("products") or {}
        for e in conn.get("edges", []):
            n = e["node"]; cursor = e["cursor"]
            if (n.get("status") or "").upper() != "ACTIVE" or not n.get("publishedAt"):
                skipped["not-active"] += 1
                continue
            why = excluded(n)
            if why:
                skipped[why] += 1
                continue
            desc = text(n.get("descriptionHtml"))
            cat = (n.get("category") or {}).get("fullName") or n.get("productType") or ""
            base_url = n.get("onlineStoreUrl") or f"{DOMAIN}/products/{n['handle']}"
            for ve in (n.get("variants") or {}).get("edges", []):
                v = ve["node"]
                vid = (v.get("id") or "").rsplit("/", 1)[-1]
                opts = {o["name"].lower(): o["value"] for o in v.get("selectedOptions", [])}
                colour = opts.get("color") or opts.get("colour") or ""
                size = opts.get("size") or ""
                img = ((v.get("image") or {}).get("url")) or ((n.get("featuredImage") or {}).get("url")) or ""
                pid = v.get("sku") or vid
                if pid in seen_ids:      # duplicate SKU across variants/products -> keep the row unique
                    pid = f"{pid}-{vid}"
                seen_ids.add(pid)
                rows.append({
                    "product_id": pid,
                    "product_name": n["title"] + (f" - {' / '.join(o['value'] for o in v.get('selectedOptions', []) if o['value'] != 'Default Title')}"
                                                  if any(o['value'] != 'Default Title' for o in v.get('selectedOptions', [])) else ""),
                    "description": desc,
                    "product_url": f"{base_url}?variant={vid}",
                    "image_url": img,
                    "price": v.get("price", ""),
                    "currency": CURRENCY,
                    "brand": n.get("vendor", ""),
                    "category": cat,
                    "colour": colour,
                    "size": size,
                    "ean": v.get("barcode") or "",
                    "mpn": v.get("sku") or "",
                    "in_stock": "1" if v.get("availableForSale") else "0",
                    "condition": "new",
                })
        if not conn.get("pageInfo", {}).get("hasNextPage"):
            break

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {OUT}: {len(rows)} variant rows.")
    print("Excluded:", ", ".join(f"{k}={v}" for k, v in skipped.items() if v))
    instock = sum(1 for r in rows if r["in_stock"] == "1")
    print(f"In stock: {instock}/{len(rows)} | brands: {len({r['brand'] for r in rows})}")


if __name__ == "__main__":
    main()
