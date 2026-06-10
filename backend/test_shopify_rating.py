"""
Run this script directly to test if rating is saving to Shopify.
Place it in your project root and run:
  python test_shopify_rating.py

It will show exactly what's happening at each step.
"""
import os
import time
import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

CLIENT_ID     = os.getenv("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
STORE         = os.getenv("SHOPIFY_STORE", "").replace(".myshopify.com", "").strip()
API_VERSION   = os.getenv("SHOPIFY_API_VERSION", "2025-01")

BASE = f"https://{STORE}.myshopify.com/admin/api/{API_VERSION}"

print(f"\n{'='*60}")
print(f"  Shopify Rating Debug Tool")
print(f"{'='*60}")
print(f"  Store      : {STORE}.myshopify.com")
print(f"  API Version: {API_VERSION}")
print(f"  Client ID  : {CLIENT_ID[:8]}...{CLIENT_ID[-4:] if len(CLIENT_ID)>12 else ''}")
print(f"{'='*60}\n")

# ── Step 1: Get Token ─────────────────────────────────────────
print("STEP 1: Getting Shopify access token...")
try:
    res = requests.post(
        f"https://{STORE}.myshopify.com/admin/oauth/access_token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=15,
    )
    print(f"  Status : {res.status_code}")
    if res.status_code != 200:
        print(f"  ERROR  : {res.text}")
        exit(1)
    TOKEN = res.json()["access_token"]
    print(f"  Token  : {TOKEN[:12]}...  ✅")
except Exception as e:
    print(f"  FAILED : {e}")
    exit(1)

H = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}

# ── Step 2: Get latest product ────────────────────────────────
print("\nSTEP 2: Fetching latest imported product from Shopify...")
try:
    res = requests.get(f"{BASE}/products.json", params={"limit": 1, "fields": "id,title,tags,body_html"}, headers=H, timeout=15)
    products = res.json().get("products", [])
    if not products:
        print("  ERROR: No products found in Shopify. Import a product first.")
        exit(1)
    p = products[0]
    PROD_ID = str(p["id"])
    print(f"  Product: [{PROD_ID}] {p['title'][:60]}")
    print(f"  Tags   : {p.get('tags','(none)')}")
    desc = (p.get("body_html") or "")[:200].replace("\n","")
    print(f"  Desc   : {desc}...")
except Exception as e:
    print(f"  FAILED : {e}")
    exit(1)

# ── Step 3: Check existing metafields ────────────────────────
print(f"\nSTEP 3: Checking existing metafields on product {PROD_ID}...")
try:
    res = requests.get(f"{BASE}/products/{PROD_ID}/metafields.json", headers=H, timeout=15)
    print(f"  Status : {res.status_code}")
    mfs = res.json().get("metafields", [])
    if mfs:
        for mf in mfs:
            print(f"  Found  : {mf['namespace']}.{mf['key']} = {mf['value']} (type: {mf['type']})")
    else:
        print("  Result : No metafields found on this product")
except Exception as e:
    print(f"  FAILED : {e}")

# ── Step 4: Try creating metafield ───────────────────────────
TEST_RATING = "4.7"
print(f"\nSTEP 4: Creating metafield custom.rating = {TEST_RATING}...")
try:
    res = requests.post(
        f"{BASE}/products/{PROD_ID}/metafields.json",
        json={"metafield": {
            "namespace": "custom",
            "key":       "rating",
            "value":     TEST_RATING,
            "type":      "number_decimal",
        }},
        headers=H, timeout=15,
    )
    print(f"  Status : {res.status_code}")
    body = res.json()
    if res.status_code in (200, 201):
        mf = body.get("metafield", {})
        print(f"  Created: id={mf.get('id')}  value={mf.get('value')}  ✅")
    else:
        print(f"  ERROR  : {body}")
except Exception as e:
    print(f"  FAILED : {e}")

# ── Step 5: Try updating tags ─────────────────────────────────
print(f"\nSTEP 5: Adding tag 'rating:{TEST_RATING}' to product...")
try:
    res = requests.get(f"{BASE}/products/{PROD_ID}.json", params={"fields": "id,tags"}, headers=H, timeout=15)
    tags_str  = res.json().get("product", {}).get("tags", "") or ""
    tags_list = [t.strip() for t in tags_str.split(",") if t.strip() and not t.strip().startswith("rating:")]
    tags_list.append(f"rating:{TEST_RATING}")
    new_tags  = ", ".join(tags_list)

    res2 = requests.put(
        f"{BASE}/products/{PROD_ID}.json",
        json={"product": {"id": PROD_ID, "tags": new_tags}},
        headers=H, timeout=15,
    )
    print(f"  Status : {res2.status_code}")
    if res2.status_code == 200:
        saved_tags = res2.json().get("product", {}).get("tags", "")
        print(f"  Tags   : {saved_tags}  ✅")
    else:
        print(f"  ERROR  : {res2.text[:200]}")
except Exception as e:
    print(f"  FAILED : {e}")

# ── Step 6: Verify everything saved ──────────────────────────
print(f"\nSTEP 6: Verifying all changes saved on product {PROD_ID}...")
try:
    res = requests.get(f"{BASE}/products/{PROD_ID}.json", params={"fields": "id,title,tags,body_html"}, headers=H, timeout=15)
    p2 = res.json().get("product", {})
    print(f"  Title  : {p2.get('title','')[:60]}")
    print(f"  Tags   : {p2.get('tags','')}")

    res2 = requests.get(f"{BASE}/products/{PROD_ID}/metafields.json", params={"namespace": "custom"}, headers=H, timeout=15)
    mfs2 = res2.json().get("metafields", [])
    for mf in mfs2:
        print(f"  MField : {mf['namespace']}.{mf['key']} = {mf['value']}")
    if not mfs2:
        print("  MField : (none found — metafield definition may be needed in Shopify Settings)")
except Exception as e:
    print(f"  FAILED : {e}")

print(f"\n{'='*60}")
print("  Done! Check results above.")
print(f"  To see metafields in Shopify Admin:")
print(f"  Settings → Custom data → Products → Add definition")
print(f"  Namespace: custom  Key: rating  Type: Number (decimal)")
print(f"{'='*60}\n")