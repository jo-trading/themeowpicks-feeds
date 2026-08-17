#!/usr/bin/env python3
"""
TheMeowPicks - MiaCara Shopify sync
-----------------------------------
Creates the product metafield DEFINITIONS and uploads/attaches all product IMAGES
to an existing Shopify store over the Admin API. Run this AFTER importing
themeowpicks-miacara-import.csv (the products must already exist; images are
matched to them by Handle).

WHAT IT DOES
  1. Creates the 16 custom.* metafield definitions (so the imported values show up).
  2. For every product in image_manifest.json, uploads each local image file and
     attaches it in order, with alt text, and pins variant images by color.

SETUP (one time)
  1. Shopify admin > Settings > Apps > Develop apps > Build apps in Dev Dashboard.
  2. Create app. In the app version's Access section, add scope  write_products  and Release.
  3. Install the app on your store (Installs section > Install app).
  4. On the app's Settings page, copy the Client ID and the Secret (starts with shpss_).
  5. pip install requests

  Note (2026 change): Dev Dashboard apps no longer give a copyable shpat_ token. This
  script exchanges your Client ID + Secret for a 24-hour access token automatically,
  using Shopify's client credentials grant. You just supply the ID and secret.

RUN
  export SHOPIFY_STORE="your-store.myshopify.com"
  export SHOPIFY_CLIENT_ID="your_client_id"
  export SHOPIFY_CLIENT_SECRET="shpss_your_secret"

  python3 shopify_sync.py --check          # offline: verify every image file exists, print the plan
  python3 shopify_sync.py --defs-only      # create the 16 metafield definitions only
  python3 shopify_sync.py --only perla-cat-cave   # do a single product first as a test
  python3 shopify_sync.py                   # full run: definitions + all images

  (If you have a legacy shpat_ token from an older custom app, you can instead set
   SHOPIFY_TOKEN and skip the client ID/secret.)

USEFUL FLAGS
  --images-only          skip definitions, just do images
  --defs-only            just create metafield definitions
  --only HANDLE          restrict to one product handle (repeatable)
  --limit N              only process the first N products (smoke test)
  --force                upload images even if the product already has some
  --api-version 2024-10  Admin API version (default 2024-10)

The script is safe to re-run: by default it skips any product that already has
images, so a re-run only fills in the ones that failed.
"""

import os, sys, json, time, base64, argparse, mimetypes, csv, re

try:
    import requests
except ImportError:
    sys.exit("Please install requests first:  pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))

METAFIELD_DEFS = [
    ("Lead", "lead", "multi_line_text_field"),
    ("Benefits", "benefits", "multi_line_text_field"),
    ("Features", "features", "multi_line_text_field"),
    ("Why we chose", "why_we_chose", "multi_line_text_field"),
    ("FAQ", "faq", "multi_line_text_field"),
    ("Dimensions", "dimensions", "single_line_text_field"),
    ("Weight", "weight", "single_line_text_field"),
    ("Material", "material", "single_line_text_field"),
    ("Made in", "made_in", "single_line_text_field"),
    ("Care", "care", "single_line_text_field"),
    ("Designer", "designer", "single_line_text_field"),
    ("Color", "color", "single_line_text_field"),
    ("Accolades", "accolades", "single_line_text_field"),
    ("HS code", "hs_code", "single_line_text_field"),
    ("Supplier SKU", "supplier_sku", "single_line_text_field"),
    ("Package dimensions", "package_dimensions", "single_line_text_field"),
    ("Country of origin", "country_of_origin", "single_line_text_field"),
    ("Partner URL", "partner_url", "url"),
    ("Partner name", "partner_name", "single_line_text_field"),
]


def _norm_store(s):
    """Accept 'sjzryc-2w' or the full domain and always return the myshopify host."""
    s = (s or "").strip().replace("https://", "").replace("http://", "").rstrip("/")
    return s if "." in s else s + ".myshopify.com"


def _load_dotenv():
    """Load KEY=VALUE lines from a .env beside this script, so scripts run without `source .env`."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                if "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()


def get_access_token(store, client_id, client_secret):
    """Exchange client ID + secret for a 24h access token (client credentials grant)."""
    store = _norm_store(store)
    url = f"https://{store}/admin/oauth/access_token"
    r = requests.post(url, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=60)
    if r.status_code != 200:
        sys.exit(f"Could not get an access token ({r.status_code}): {r.text[:300]}\n"
                 "Check the client ID/secret are correct, the app is installed on the store, "
                 "and it has the write_products scope released.")
    return r.json()["access_token"]


class Shopify:
    def __init__(self, store, token, version):
        self.base = f"https://{_norm_store(store)}/admin/api/{version}"
        self.s = requests.Session()
        self.s.headers.update({"X-Shopify-Access-Token": token,
                               "Content-Type": "application/json"})

    def _req(self, method, url, **kw):
        kw.setdefault("timeout", 120)
        last = None
        for attempt in range(6):
            try:
                r = self.s.request(method, url, **kw)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last = e; time.sleep(3 * (attempt + 1)); continue
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2))
                time.sleep(wait); continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1)); continue
            return r
        if last:
            raise last
        return r

    def gql(self, query, variables=None):
        r = self._req("POST", f"{self.base}/graphql.json",
                      data=json.dumps({"query": query, "variables": variables or {}}))
        r.raise_for_status()
        data = r.json()
        # respect GraphQL cost throttling
        cost = data.get("extensions", {}).get("cost", {})
        avail = cost.get("throttleStatus", {}).get("currentlyAvailable")
        if avail is not None and avail < 200:
            time.sleep(1.0)
        return data

    def rest(self, method, path, payload=None):
        r = self._req(method, f"{self.base}{path}",
                      data=json.dumps(payload) if payload is not None else None)
        time.sleep(0.55)  # stay under the REST leaky bucket
        return r


def create_definitions(sp):
    q = """
    mutation($def: MetafieldDefinitionInput!) {
      metafieldDefinitionCreate(definition: $def) {
        createdDefinition { id }
        userErrors { field message code }
      }
    }"""
    created = skipped = failed = 0
    for name, key, ftype in METAFIELD_DEFS:
        var = {"def": {"name": name, "namespace": "custom", "key": key,
                       "type": ftype, "ownerType": "PRODUCT"}}
        data = sp.gql(q, var)
        errs = data.get("data", {}).get("metafieldDefinitionCreate", {}).get("userErrors", [])
        if not errs:
            created += 1; print(f"  + defined custom.{key}")
        elif any(e.get("code") == "TAKEN" for e in errs):
            skipped += 1; print(f"  = custom.{key} already exists")
        else:
            failed += 1; print(f"  ! custom.{key}: {errs}")
    print(f"Definitions: {created} created, {skipped} already existed, {failed} failed.")


# Metafield keys to NOT push to the store (theme hides a row when its value is absent).
SKIP_KEYS = {"designer"}


def load_fields_csv(path):
    """Return {handle: (product_type, tags)} from the product CSV (first row per handle)."""
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = {}
    for r in rows:
        if not r.get("Title"):
            continue
        out[r["Handle"]] = (r.get("Type", ""), r.get("Tags", ""))
    return out


def update_fields(sp, handle, ptype, tags):
    product = get_product(sp, handle)
    if not product:
        print(f"  ! product not found for handle '{handle}'")
        return 0
    payload = {"product": {"id": product["id"], "product_type": ptype, "tags": tags}}
    r = sp.rest("PUT", f"/products/{product['id']}.json", payload)
    if r.status_code == 200:
        print(f"  + {handle}: Type='{ptype}'")
        return 1
    print(f"  ! {handle} field update failed: {r.status_code} {r.text[:150]}")
    return 0


def load_prices_csv(path):
    """Return {handle: [(sku, price), ...]} from every variant row that carries a price."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            vp = (r.get("Variant Price") or "").strip()
            if not vp:
                continue
            out.setdefault(r["Handle"], []).append(((r.get("Variant SKU") or "").strip(), vp))
    return out


def update_prices(sp, handle, entries):
    """Push Variant Price. Single-variant products get the sole price; multi-variant match by SKU."""
    product = get_product(sp, handle)
    if not product:
        print(f"  ! product not found for handle '{handle}'")
        return 0
    variants = product.get("variants", [])
    by_sku = {s: p for s, p in entries if s}
    if len(variants) == 1 and entries:
        targets = [(variants[0]["id"], entries[0][1])]
    else:
        targets = [(v["id"], by_sku[(v.get("sku") or "").strip()])
                   for v in variants if (v.get("sku") or "").strip() in by_sku]
    n = 0
    for vid, price in targets:
        r = sp.rest("PUT", f"/variants/{vid}.json", {"variant": {"id": vid, "price": price}})
        if r.status_code == 200:
            n += 1
        else:
            print(f"  ! {handle} price update failed: {r.status_code} {r.text[:120]}")
    if n:
        print(f"  + {handle}: set price on {n} variant(s)")
    return n


def load_weights_csv(path):
    """Return {handle: [(sku, grams), ...]} from every variant row that carries a weight."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            g = (r.get("Variant Grams") or "").strip()
            if not g:
                continue
            out.setdefault(r["Handle"], []).append(((r.get("Variant SKU") or "").strip(), g))
    return out


def update_weights(sp, handle, entries):
    """Push variant weight (grams). Single-variant products get the sole weight; multi match by SKU."""
    product = get_product(sp, handle)
    if not product:
        print(f"  ! product not found for handle '{handle}'")
        return 0
    variants = product.get("variants", [])
    by_sku = {s: g for s, g in entries if s}
    if len(variants) == 1 and entries:
        targets = [(variants[0]["id"], entries[0][1])]
    else:
        targets = [(v["id"], by_sku[(v.get("sku") or "").strip()])
                   for v in variants if (v.get("sku") or "").strip() in by_sku]
    n = 0
    for vid, grams in targets:
        try:
            gi = int(float(grams))
        except Exception:
            continue
        r = sp.rest("PUT", f"/variants/{vid}.json",
                    {"variant": {"id": vid, "grams": gi, "weight": round(gi / 1000, 3), "weight_unit": "kg"}})
        if r.status_code == 200:
            n += 1
        else:
            print(f"  ! {handle} weight update failed: {r.status_code} {r.text[:120]}")
    if n:
        print(f"  + {handle}: set weight on {n} variant(s)")
    return n


def load_seo_csv(path):
    """Return {handle: (title_tag, description_tag)} from the product CSV."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("Title"):
                out[r["Handle"]] = (r.get("SEO Title", "").strip(), r.get("SEO Description", "").strip())
    return out


def push_seo(sp, handle, title_tag, desc_tag):
    """Push meta title / meta description as the global.title_tag / global.description_tag metafields."""
    product = get_product(sp, handle)
    if not product:
        print(f"  ! product not found for handle '{handle}'"); return 0
    gid = f"gid://shopify/Product/{product['id']}"
    triples = []
    if title_tag:
        triples.append(("global", "title_tag", "single_line_text_field", title_tag))
    if desc_tag:
        triples.append(("global", "description_tag", "single_line_text_field", desc_tag))
    if not triples:
        return 0
    q = """mutation($m:[MetafieldsSetInput!]!){
      metafieldsSet(metafields:$m){ metafields{ id } userErrors{ field message } } }"""
    inp = [{"ownerId": gid, "namespace": ns, "key": k, "type": t, "value": v} for ns, k, t, v in triples]
    data = sp.gql(q, {"m": inp})
    for e in data.get("data", {}).get("metafieldsSet", {}).get("userErrors", []):
        print(f"  ! {handle} SEO: {e}")
    print(f"  + {handle}: SEO title/description")
    return 1


def load_body_csv(path):
    """Return {handle: Body (HTML)} from the title row of each product."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("Title"):
                out[r["Handle"]] = r.get("Body (HTML)", "")
    return out


def push_body(sp, handle, body_html):
    """Update the product description (Body HTML) in place, no re-import."""
    product = get_product(sp, handle)
    if not product:
        print(f"  ! product not found for handle '{handle}'"); return 0
    gid = f"gid://shopify/Product/{product['id']}"
    q = """mutation($input:ProductInput!){
      productUpdate(input:$input){ product{ id } userErrors{ field message } } }"""
    data = sp.gql(q, {"input": {"id": gid, "descriptionHtml": body_html}})
    errs = data.get("data", {}).get("productUpdate", {}).get("userErrors", [])
    for e in errs:
        print(f"  ! {handle} body: {e}")
    if not errs:
        print(f"  + {handle}: description")
    return 0 if errs else 1


def load_alt_csv(path):
    """Return {handle: alt} using the product's Image Alt Text (first non-empty) or its Title."""
    rowsby = {}
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rowsby.setdefault(r["Handle"], []).append(r)
    out = {}
    for h, rs in rowsby.items():
        title = next((r["Title"] for r in rs if r.get("Title")), h)
        alt = next((r["Image Alt Text"].strip() for r in rs if r.get("Image Alt Text", "").strip()), "")
        out[h] = alt or title
    return out


def update_alt(sp, handle, alt):
    """Set alt text on every existing image of the product (no re-upload)."""
    product = get_product(sp, handle)
    if not product:
        print(f"  ! product not found for handle '{handle}'"); return 0
    n = 0
    for im in product.get("images", []):
        r = sp.rest("PUT", f"/products/{product['id']}/images/{im['id']}.json",
                    {"image": {"id": im["id"], "alt": alt}})
        if r.status_code == 200:
            n += 1
    if n:
        print(f"  + {handle}: alt on {n} image(s)")
    return 1 if n else 0


def get_location(sp):
    r = sp.rest("GET", "/locations.json")
    locs = r.json().get("locations", []) if r.status_code == 200 else []
    active = [l for l in locs if l.get("active")]
    picked = (active or locs)
    return picked[0]["id"] if picked else None


def set_inventory(sp, handle, qty, location_id):
    product = get_product(sp, handle)
    if not product:
        print(f"  ! product not found for handle '{handle}'")
        return 0
    variants = product.get("variants", [])
    n = 0
    for v in variants:
        # turn on Shopify inventory tracking for the variant, then set the level
        sp.rest("PUT", f"/variants/{v['id']}.json",
                {"variant": {"id": v["id"], "inventory_management": "shopify"}})
        iid = v.get("inventory_item_id")
        if not iid:
            continue
        r = sp.rest("POST", "/inventory_levels/set.json",
                    {"location_id": location_id, "inventory_item_id": iid, "available": qty})
        if r.status_code in (200, 201):
            n += 1
        else:
            print(f"  ! {handle} variant {v['id']} inventory failed: {r.status_code} {r.text[:150]}")
    print(f"  + {handle}: set {n}/{len(variants)} variant(s) to {qty}")
    return n


def load_metafields_csv(path):
    """Return {handle: [(key, type, value), ...]} from the product CSV's Metafield columns."""
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    hdr_re = re.compile(r"Metafield:\s*custom\.(\w+)\s*\[(\w+)\]")
    cols = []  # (header, key, type)
    for h in (rows[0].keys() if rows else []):
        m = hdr_re.match(h)
        if m:
            cols.append((h, m.group(1), m.group(2)))
    out = {}
    for r in rows:
        if not r.get("Title"):   # metafields live on each product's first (title) row
            continue
        vals = []
        for hdr, key, typ in cols:
            if key in SKIP_KEYS:
                continue
            v = (r.get(hdr) or "").strip()
            if v:
                vals.append((key, typ, v))
        out[r["Handle"]] = vals
    return out


def set_metafields(sp, handle, mfvals):
    product = get_product(sp, handle)
    if not product:
        print(f"  ! product not found for handle '{handle}'")
        return 0
    if not mfvals:
        return 0
    gid = f"gid://shopify/Product/{product['id']}"
    def clean(t, v):
        # single-line metafields cannot contain newlines; flatten to commas
        if t == "single_line_text_field":
            return re.sub(r"\s*\n\s*", ", ", v.replace("\r\n", "\n")).strip()
        return v
    inputs = [{"ownerId": gid, "namespace": "custom", "key": k, "type": t, "value": clean(t, v)}
              for (k, t, v) in mfvals]
    q = """
    mutation($m:[MetafieldsSetInput!]!){
      metafieldsSet(metafields:$m){ metafields{ id } userErrors{ field message code } }
    }"""
    ok = 0
    for i in range(0, len(inputs), 25):
        chunk = inputs[i:i + 25]
        data = sp.gql(q, {"m": chunk})
        res = data.get("data", {}).get("metafieldsSet", {})
        errs = res.get("userErrors", [])
        ok += len(res.get("metafields", []) or [])
        if errs:
            print(f"  ! {handle} metafield errors: {errs}")
    print(f"  + {handle}: set {ok}/{len(inputs)} metafield(s)")
    return ok


def delete_metafield(sp, handle, key):
    product = get_product(sp, handle)
    if not product:
        return 0
    gid = f"gid://shopify/Product/{product['id']}"
    q = """
    mutation($m:[MetafieldIdentifierInput!]!){
      metafieldsDelete(metafields:$m){ deletedMetafields{ key } userErrors{ field message } }
    }"""
    data = sp.gql(q, {"m": [{"ownerId": gid, "namespace": "custom", "key": key}]})
    res = data.get("data", {}).get("metafieldsDelete", {})
    deleted = res.get("deletedMetafields") or []
    if deleted:
        print(f"  - {handle}: removed custom.{key}")
    return len(deleted)


def get_product(sp, handle):
    r = sp.rest("GET", f"/products.json?handle={handle}"
                       f"&fields=id,handle,title,variants,images")
    if r.status_code != 200:
        return None
    prods = r.json().get("products", [])
    return prods[0] if prods else None


def variant_ids_for_color(product, color):
    if not color:
        return []
    ids = []
    for v in product.get("variants", []):
        if color in (v.get("option1"), v.get("option2")):
            ids.append(v["id"])
    return ids


def upload_images(sp, handle, imgs, feed_root, force):
    product = get_product(sp, handle)
    if not product:
        print(f"  ! product not found for handle '{handle}' (import the CSV first?)")
        return 0
    existing = product.get("images", [])
    if existing and not force:
        print(f"  = {handle}: already has {len(existing)} image(s), skipping (use --force to replace)")
        return 0
    if existing and force:
        # clean replace: delete what's there so the product ends up with exactly the manifest images
        for img in existing:
            sp.rest("DELETE", f"/products/{product['id']}/images/{img['id']}.json")
        print(f"  ~ {handle}: cleared {len(existing)} existing image(s) before re-upload")
    done = 0
    for im in imgs:
        if im.get("url"):
            # vendor CDN image: Shopify fetches it by URL and rehosts it
            payload = {"image": {"src": im["url"], "alt": im["alt"], "position": im["position"]}}
        else:
            path = os.path.join(feed_root, im["file"])
            if not os.path.isfile(path):
                print(f"  ! missing file: {im['file']}")
                continue
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            payload = {"image": {
                "attachment": b64,
                "filename": os.path.basename(path),
                "alt": im["alt"],
                "position": im["position"],
            }}
        vids = variant_ids_for_color(product, im.get("variant_color"))
        if vids:
            payload["image"]["variant_ids"] = vids
        r = sp.rest("POST", f"/products/{product['id']}/images.json", payload)
        if r.status_code in (200, 201):
            done += 1
        else:
            print(f"  ! {handle} pos {im['position']} upload failed: {r.status_code} {r.text[:200]}")
    print(f"  + {handle}: uploaded {done}/{len(imgs)} image(s)")
    return done


def check(manifest, feed_root, handles):
    total = missing = 0
    for h, imgs in manifest.items():
        if handles and h not in handles:
            continue
        for im in imgs:
            total += 1
            if im.get("url"):
                continue  # vendor URL, fetched by Shopify at import time
            if not os.path.isfile(os.path.join(feed_root, im["file"])):
                missing += 1
                print(f"  MISSING: {im['file']}")
    print(f"\nPlan: {len(manifest)} products, {total} images. Missing files: {missing}.")
    print("Files look good." if missing == 0 else "Fix missing files before running.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed-root", default=HERE,
                    help="folder that contains the miacara image folders (default: this script's folder)")
    ap.add_argument("--manifest", default=os.path.join(HERE, "image_manifest.json"))
    ap.add_argument("--api-version", default="2024-10")
    ap.add_argument("--check", action="store_true", help="offline: verify files + print plan, no network")
    ap.add_argument("--defs-only", action="store_true")
    ap.add_argument("--metafields-only", action="store_true", help="only push metafield values from the CSV")
    ap.add_argument("--fields-only", action="store_true", help="only update Type and Tags from the CSV")
    ap.add_argument("--prices-only", action="store_true", help="only update Variant Price from the CSV")
    ap.add_argument("--weights-only", action="store_true", help="only update variant weight (grams) from the CSV")
    ap.add_argument("--alt-only", action="store_true", help="only set image alt text on existing images")
    ap.add_argument("--seo-only", action="store_true", help="only push SEO title/description (meta tags)")
    ap.add_argument("--body-only", action="store_true", help="only update the product description (Body HTML) from the CSV")
    ap.add_argument("--images-only", action="store_true")
    ap.add_argument("--csv", default=os.path.join(HERE, "themeowpicks-miacara-import.csv"))
    ap.add_argument("--skip-metafields", action="store_true", help="in a full run, skip the metafield push")
    ap.add_argument("--delete-key", help="remove one custom.<key> metafield from products (e.g. designer), then exit")
    ap.add_argument("--skip-qa", action="store_true", help="push even if the QA gate reports errors")
    ap.add_argument("--set-inventory", action="store_true", help="set every variant's stock to --qty, then exit")
    ap.add_argument("--qty", type=int, default=5, help="quantity for --set-inventory (default 5)")
    ap.add_argument("--only", action="append", default=[], help="restrict to handle(s)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    # manifest is only needed for image work; load it if present, else run without it
    manifest = {}
    if os.path.isfile(args.manifest):
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
    handles = set(args.only)

    if args.check:
        check(manifest, args.feed_root, handles)
        return

    store = os.environ.get("SHOPIFY_STORE")
    if not store:
        sys.exit("Set the SHOPIFY_STORE environment variable first (e.g. your-store.myshopify.com).")
    cid = os.environ.get("SHOPIFY_CLIENT_ID")
    csec = os.environ.get("SHOPIFY_CLIENT_SECRET")
    token = os.environ.get("SHOPIFY_TOKEN")
    # Client credentials take priority so a stale SHOPIFY_TOKEN can't override them.
    if cid and csec:
        print("Getting a 24-hour access token from your client credentials...")
        token = get_access_token(store, cid, csec)
        print("  got access token.")
    elif token:
        print("Using SHOPIFY_TOKEN from the environment.")
    else:
        sys.exit("Set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET (from the app's Settings page), "
                 "or a legacy SHOPIFY_TOKEN.")
    sp = Shopify(store, token, args.api_version)

    if args.set_inventory:
        loc = get_location(sp)
        if not loc:
            sys.exit("No location found. The app needs the read_locations scope.")
        print(f"Setting inventory to {args.qty} at location {loc}...")
        hs = [h for h in load_fields_csv(args.csv).keys() if not handles or h in handles]
        if args.limit:
            hs = hs[:args.limit]
        tot = sum(set_inventory(sp, h, args.qty, loc) for h in hs)
        print(f"Set inventory to {args.qty} on {tot} variants.")
        return

    if args.delete_key:
        print(f"Removing custom.{args.delete_key} from products...")
        hs = [h for h in load_metafields_csv(args.csv).keys() if not handles or h in handles]
        if args.limit:
            hs = hs[:args.limit]
        tot = sum(delete_metafield(sp, h, args.delete_key) for h in hs)
        print(f"Removed from {tot} product(s).")
        return

    only_one = (args.defs_only or args.metafields_only or args.images_only
                or args.fields_only or args.prices_only or args.alt_only or args.seo_only
                or args.body_only or args.weights_only)
    do_defs = args.defs_only or not only_one
    do_meta = args.metafields_only or not only_one
    do_imgs = args.images_only or not only_one
    do_fields = args.fields_only or not only_one
    do_prices = args.prices_only or not only_one
    do_alt = args.alt_only or not only_one
    do_seo = args.seo_only or not only_one
    do_body = args.body_only  # explicit only; the full import already carries body copy
    do_weights = args.weights_only  # explicit only; the full import already carries weight

    # QA gate: never push product content that fails validation.
    if (do_fields or do_meta or do_imgs or do_prices or do_alt or do_seo or do_body or do_weights) and not args.skip_qa:
        try:
            import qa_feed
        except Exception as e:
            sys.exit(f"Could not load qa_feed.py for the QA gate: {e} (or pass --skip-qa).")
        passed, issues, _ = qa_feed.run(args.csv, args.manifest, write_report=True)
        n_err = sum(1 for _, s, _ in issues if s == "ERROR")
        n_warn = sum(1 for _, s, _ in issues if s == "WARN")
        print(f"QA gate: {n_err} errors, {n_warn} warnings. Details in qa-report.md.")
        if not passed:
            sys.exit("QA FAILED. Open qa-report.md to see which product pages have errors, "
                     "fix them, then re-run. To override, add --skip-qa.")
        print("QA passed.\n")

    if do_defs:
        print("Creating metafield definitions...")
        create_definitions(sp)

    if do_fields:
        print("\nUpdating Type and Tags from the CSV...")
        ff = load_fields_csv(args.csv)
        fitems = [(h, t, g) for h, (t, g) in ff.items() if not handles or h in handles]
        if args.limit:
            fitems = fitems[:args.limit]
        ftot = sum(update_fields(sp, h, t, g) for h, t, g in fitems)
        print(f"Updated Type/Tags on {ftot} products.")

    if do_prices:
        print("\nUpdating Variant Price from the CSV...")
        pf = load_prices_csv(args.csv)
        pitems = [(h, e) for h, e in pf.items() if not handles or h in handles]
        if args.limit:
            pitems = pitems[:args.limit]
        ptot = sum(update_prices(sp, h, e) for h, e in pitems)
        print(f"Updated prices on {ptot} products.")

    if do_weights:
        print("\nUpdating variant weight (grams) from the CSV...")
        wf = load_weights_csv(args.csv)
        witems = [(h, e) for h, e in wf.items() if not handles or h in handles]
        if args.limit:
            witems = witems[:args.limit]
        wtot = sum(update_weights(sp, h, e) for h, e in witems)
        print(f"Updated weight on {wtot} products.")

    if do_alt:
        print("\nSetting image alt text on existing images...")
        af = load_alt_csv(args.csv)
        aitems = [(h, a) for h, a in af.items() if not handles or h in handles]
        if args.limit:
            aitems = aitems[:args.limit]
        atot = sum(update_alt(sp, h, a) for h, a in aitems)
        print(f"Updated alt text on {atot} products.")

    if do_seo:
        print("\nPushing SEO title/description...")
        sf = load_seo_csv(args.csv)
        sitems = [(h, v) for h, v in sf.items() if not handles or h in handles]
        if args.limit:
            sitems = sitems[:args.limit]
        stot = sum(push_seo(sp, h, t, d) for h, (t, d) in sitems)
        print(f"Pushed SEO on {stot} products.")

    if do_body:
        print("\nUpdating product description (Body HTML) from the CSV...")
        bf = load_body_csv(args.csv)
        bitems = [(h, b) for h, b in bf.items() if not handles or h in handles]
        if args.limit:
            bitems = bitems[:args.limit]
        btot = sum(push_body(sp, h, b) for h, b in bitems)
        print(f"Updated description on {btot} products.")

    if do_meta and not args.skip_metafields:
        print("\nPushing metafield values from the CSV...")
        mf = load_metafields_csv(args.csv)
        mitems = [(h, v) for h, v in mf.items() if not handles or h in handles]
        if args.limit:
            mitems = mitems[:args.limit]
        mtot = 0
        for h, vals in mitems:
            mtot += set_metafields(sp, h, vals)
        print(f"Set {mtot} metafields across {len(mitems)} products.")

    if do_imgs:
        print("\nUploading images...")
        items = [(h, imgs) for h, imgs in manifest.items() if not handles or h in handles]
        if args.limit:
            items = items[:args.limit]
        grand = 0
        for h, imgs in items:
            grand += upload_images(sp, h, imgs, args.feed_root, args.force)
        print(f"Uploaded {grand} images across {len(items)} products.")

    print("\nDone.")


if __name__ == "__main__":
    main()
