"""Weekly: pull LastCost from Unleashed, overwrite Shopify Cost per item by SKU."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import time
from decimal import Decimal, ROUND_HALF_UP

CENTS = Decimal("0.01")


def to_cents(x: Decimal) -> Decimal:
    return x.quantize(CENTS, rounding=ROUND_HALF_UP)

import requests

UNLEASHED_API_ID = os.environ["UNLEASHED_API_ID"]
UNLEASHED_API_KEY = os.environ["UNLEASHED_API_KEY"]
UNLEASHED_BASE = "https://api.unleashedsoftware.com"

SHOPIFY_STORE = os.environ["SHOPIFY_STORE"]
SHOPIFY_TOKEN = os.environ["SHOPIFY_ADMIN_TOKEN"]
SHOPIFY_API_VERSION = "2025-01"
SHOPIFY_GQL = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
MAX_DELTA_PCT = Decimal(os.environ.get("MAX_DELTA_PCT", "50"))  # safety: skip swings > 50%
SAMPLE_SIZE = int(os.environ.get("SAMPLE_SIZE", "0"))  # 0 = no cap; in live mode, only write first N
REPORT_PATH = os.environ.get(
    "REPORT_PATH",
    f"cost_sync_report_{dt.date.today().isoformat()}.csv",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cost-sync")


# Unleashed signs the query string after '?' (no leading ?).
def _unleashed_sig(qs: str) -> str:
    return base64.b64encode(
        hmac.new(UNLEASHED_API_KEY.encode(), qs.encode(), hashlib.sha256).digest()
    ).decode()


def fetch_unleashed_costs() -> dict[str, Decimal]:
    # Keyed by upper-cased SKU. Falls back to DefaultPurchasePrice when LastCost is null/0.
    costs: dict[str, Decimal] = {}
    page, page_size = 1, 200
    while True:
        qs = f"pageSize={page_size}"
        r = requests.get(
            f"{UNLEASHED_BASE}/Products/{page}?{qs}",
            headers={
                "api-auth-id": UNLEASHED_API_ID,
                "api-auth-signature": _unleashed_sig(qs),
                "Accept": "application/json",
            },
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        for p in body.get("Items", []):
            if p.get("IsObsoleted") or p.get("IsSellable") is False:
                continue
            sku = (p.get("ProductCode") or "").strip().upper()
            if not sku:
                continue
            avg = p.get("AverageLandPrice")
            last = p.get("LastCost")
            default = p.get("DefaultPurchasePrice")
            chosen: Decimal | None = None
            if avg is not None and Decimal(str(avg)) > 0:
                chosen = Decimal(str(avg))
            elif last is not None and Decimal(str(last)) > 0:
                chosen = Decimal(str(last))
            elif default is not None and Decimal(str(default)) > 0:
                chosen = Decimal(str(default))
            if chosen is not None:
                costs[sku] = to_cents(chosen)
        if page >= body.get("Pagination", {}).get("NumberOfPages", 1):
            break
        page += 1
    log.info("Unleashed: %d SKUs with cost (AvgLandPrice→LastCost→DefaultPurchasePrice)", len(costs))
    return costs


def shopify_gql(query: str, variables: dict | None = None) -> dict:
    r = requests.post(
        SHOPIFY_GQL,
        headers={"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]


BULK_QUERY = """
{ products(query: "status:active") { edges { node {
  id
  variants { edges { node {
    id sku
    inventoryItem { id unitCost { amount } }
  } } }
} } } }
"""


def fetch_shopify_variants() -> list[dict]:
    shopify_gql(
        """
        mutation ($q: String!) {
          bulkOperationRunQuery(query: $q) {
            bulkOperation { id status }
            userErrors { field message }
          }
        }
        """,
        {"q": BULK_QUERY},
    )

    while True:
        op = shopify_gql("{ currentBulkOperation { id status errorCode url } }")["currentBulkOperation"]
        if op["status"] in ("COMPLETED", "FAILED", "CANCELED"):
            break
        time.sleep(5)
    if op["status"] != "COMPLETED":
        raise RuntimeError(f"Bulk op {op['status']}: {op.get('errorCode')}")

    variants: list[dict] = []
    if not op.get("url"):
        return variants
    with requests.get(op["url"], stream=True, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            v = json.loads(line)
            if "sku" not in v:
                continue
            inv = v.get("inventoryItem") or {}
            cur = (inv.get("unitCost") or {}).get("amount")
            variants.append(
                {
                    "sku": (v.get("sku") or "").strip(),
                    "inventory_item_id": inv.get("id"),
                    "current_cost": to_cents(Decimal(str(cur))) if cur is not None else None,
                }
            )
    log.info("Shopify: %d variants pulled", len(variants))
    return variants


UPDATE = """
mutation ($id: ID!, $cost: Decimal) {
  inventoryItemUpdate(id: $id, input: {cost: $cost}) {
    inventoryItem { id unitCost { amount } }
    userErrors { field message }
  }
}
"""


def update_cost(inventory_item_id: str, cost: Decimal) -> None:
    res = shopify_gql(UPDATE, {"id": inventory_item_id, "cost": str(cost)})
    if res["inventoryItemUpdate"]["userErrors"]:
        raise RuntimeError(res["inventoryItemUpdate"]["userErrors"])


def _swing_too_big(old: Decimal | None, new: Decimal) -> bool:
    if old is None or old == 0:
        return False
    pct = abs((new - old) / old) * 100
    return pct > MAX_DELTA_PCT


def main() -> None:
    costs = fetch_unleashed_costs()
    variants = fetch_shopify_variants()
    variants.sort(key=lambda x: x["sku"])  # deterministic sample order

    rows: list[dict] = []
    shopify_skus: set[str] = set()
    live_writes = 0

    for v in variants:
        sku, inv_id = v["sku"], v["inventory_item_id"]
        cur = v["current_cost"]

        if not sku:
            rows.append({"sku": "", "status": "blank_sku_in_shopify",
                         "current_cost": cur, "new_cost": "", "note": inv_id or ""})
            continue
        shopify_skus.add(sku.upper())

        new = costs.get(sku.upper())
        if new is None:
            rows.append({"sku": sku, "status": "no_unleashed_match",
                         "current_cost": cur, "new_cost": "", "note": ""})
            continue
        if cur is not None and cur == new:
            rows.append({"sku": sku, "status": "unchanged",
                         "current_cost": cur, "new_cost": new, "note": ""})
            continue
        if _swing_too_big(cur, new):
            rows.append({"sku": sku, "status": "skipped_swing",
                         "current_cost": cur, "new_cost": new,
                         "note": f"exceeds {MAX_DELTA_PCT}%"})
            continue

        sample_capped = SAMPLE_SIZE > 0 and live_writes >= SAMPLE_SIZE
        if DRY_RUN or sample_capped:
            rows.append({"sku": sku, "status": "would_update",
                         "current_cost": cur, "new_cost": new,
                         "note": "sample_cap_reached" if sample_capped else ""})
            continue
        try:
            update_cost(inv_id, new)
            rows.append({"sku": sku, "status": "updated",
                         "current_cost": cur, "new_cost": new, "note": ""})
            live_writes += 1
            time.sleep(0.05)
        except Exception as e:
            rows.append({"sku": sku, "status": "error",
                         "current_cost": cur, "new_cost": new, "note": str(e)[:200]})

    # Unleashed SKUs absent from Shopify
    for sku in sorted(set(costs) - shopify_skus):
        rows.append({"sku": sku, "status": "no_shopify_match",
                     "current_cost": "", "new_cost": costs[sku], "note": ""})

    with open(REPORT_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "status", "current_cost", "new_cost", "note"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    summary: dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    log.info("Report: %s", REPORT_PATH)
    log.info("Summary: %s dry_run=%s", summary, DRY_RUN)


if __name__ == "__main__":
    main()
