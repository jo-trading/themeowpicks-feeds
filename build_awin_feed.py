#!/usr/bin/env python3
"""Self-contained AWIN feed builder for GitHub Actions (no private-folder dependency).

Runs on GitHub's servers on a daily schedule, so the feed refreshes whether the laptop is on or off.
Reads Shopify credentials from environment (GitHub Actions secrets):
  SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET
Writes awin_feed.csv, which the workflow commits so AWIN can fetch the raw URL.

Same rules as the store's build: one row per variant; EXCLUDE accessories/spares (tag `hidden`,
or replacement/refill/spare/insert titles) and off-site affiliate products (tag `affiliate`/
`partner`); NO cost price; availability from availableForSale so continue-policy items read in stock.
product_id is kept unique: if two variants share a SKU, the later one gets the variant id appended.
"""
import csv, html, os, re, sys, time, json
import urllib.request

DOMAIN = "https://www.themeowpicks.com"
CURRENCY = "USD"
OUT = "awin_feed.csv"
ACCESSORY_WORDS = ("replacement", "refill", "spare", "insert", "carton inlay")
COLUMNS = ["product_id", "product_name", "description", "product_url", "image_url",
           "price", "currency", "brand", "category", "colour", "size",
           "ean", "mpn", "in_stock", "condition"]

Q = """query($c:String){
  products(first:50, after:$c){
    edges{ cursor node{
      handle title vendor productType status publishedAt onlineStoreUrl tags
      category { fullName } descriptionHtml featuredImage { url }
      variants(first:100){ edges{ node{
        id sku barcode price availableForSale image { url } selectedOptions { name value }
      } } }
    } }
    pageInfo{ hasNextPage } } }"""


def norm_store(s):
    s = (s or "").strip().replace("https://", "").replace("http://", "").rstrip("/")
    return s if "." in s else s + ".myshopify.com"


def post(url, data, headers=None):
    body = json.dumps(data).encode() if isinstance(data, dict) and headers else \
           "&".join(f"{k}={v}" for k, v in data.items()).encode()
    req = urllib.request.Request(url, data=body, headers=headers or
                                 {"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def access_token(store, cid, csec):
    d = post(f"https://{store}/admin/oauth/access_token",
             {"grant_type": "client_credentials", "client_id": cid, "client_secret": csec})
    return d["access_token"]


def gql(store, token, query, variables):
    for attempt in range(6):
        try:
            return post(f"https://{store}/admin/api/2024-10/graphql.json",
                        {"query": query, "variables": variables},
                        {"Content-Type": "application/json", "X-Shopify-Access-Token": token})
        except Exception as e:
            if attempt == 5:
                raise
            time.sleep(2 * (attempt + 1))


def text(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def excluded(n):
    tags = {t.strip().lower() for t in (n.get("tags") or [])}
    if "hidden" in tags:
        return True
    if tags & {"affiliate", "partner"}:
        return True
    tl = (n.get("title") or "").lower()
    return any(w in tl for w in ACCESSORY_WORDS)


def main():
    store = norm_store(os.environ["SHOPIFY_STORE"])
    token = access_token(store, os.environ["SHOPIFY_CLIENT_ID"], os.environ["SHOPIFY_CLIENT_SECRET"])
    rows, seen, cursor = [], set(), None
    while True:
        d = gql(store, token, Q, {"c": cursor}) or {}
        conn = (d.get("data") or {}).get("products") or {}
        for e in conn.get("edges", []):
            n = e["node"]; cursor = e["cursor"]
            if (n.get("status") or "").upper() != "ACTIVE" or not n.get("publishedAt") or excluded(n):
                continue
            desc = text(n.get("descriptionHtml"))
            cat = (n.get("category") or {}).get("fullName") or n.get("productType") or ""
            base = n.get("onlineStoreUrl") or f"{DOMAIN}/products/{n['handle']}"
            for ve in (n.get("variants") or {}).get("edges", []):
                v = ve["node"]; vid = (v.get("id") or "").rsplit("/", 1)[-1]
                pid = v.get("sku") or vid
                if pid in seen:            # duplicate SKU across variants/products -> keep row unique
                    pid = f"{pid}-{vid}"
                seen.add(pid)
                o = {x["name"].lower(): x["value"] for x in v.get("selectedOptions", [])}
                extra = " / ".join(x["value"] for x in v.get("selectedOptions", []) if x["value"] != "Default Title")
                img = ((v.get("image") or {}).get("url")) or ((n.get("featuredImage") or {}).get("url")) or ""
                rows.append({
                    "product_id": pid,
                    "product_name": n["title"] + (f" - {extra}" if extra else ""),
                    "description": desc, "product_url": f"{base}?variant={vid}", "image_url": img,
                    "price": v.get("price", ""), "currency": CURRENCY, "brand": n.get("vendor", ""),
                    "category": cat, "colour": o.get("color") or o.get("colour") or "", "size": o.get("size") or "",
                    "ean": v.get("barcode") or "", "mpn": v.get("sku") or "",
                    "in_stock": "1" if v.get("availableForSale") else "0", "condition": "new",
                })
        if not conn.get("pageInfo", {}).get("hasNextPage"):
            break
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS); w.writeheader(); w.writerows(rows)
    print(f"Wrote {OUT}: {len(rows)} rows, {sum(1 for r in rows if r['in_stock']=='1')} in stock.")


if __name__ == "__main__":
    main()
