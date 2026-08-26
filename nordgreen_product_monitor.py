"""
Nordgreen Competitor Monitor — Reference Implementation
=========================================================
Mirrors the live, deployed n8n workflow 1:1 (as of the final submitted version).
Use this as a readable reference for how the pipeline works, or run it directly
as a standalone alternative to the n8n workflow.
 
Requirements:
    pip install requests
 
Environment variables needed:
    ANTHROPIC_API_KEY   - your Claude API key
    SLACK_WEBHOOK_URL   - Slack incoming webhook URL
 
Note: this reference version logs to a local JSONL file (see log_run()).
The live n8n workflow logs the same fields to a Google Sheet instead.
 
Usage:
    python nordgreen_monitor.py
"""
 
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
 
import requests
 
# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
 
BRAND_NAME = "Nordgreen"
PRODUCTS_URL = "https://nordgreen.com/products.json?limit=250"
STATE_FILE = Path("nordgreen_state.json")   # mirrors n8n's workflow static data
LOG_FILE = Path("nordgreen_run_log.jsonl")  # mirrors the Google Sheets log
 
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
 
CLAUDE_MODEL = "claude-sonnet-5"
MAX_TOKENS = 8000  # generous headroom — extended thinking blocks eat into this
 
# Deterministic pre-filter: ignore price changes below this threshold
# (protects against float rounding / currency-conversion noise triggering
# false positives before it ever reaches Claude)
MIN_PRICE_CHANGE_USD = 1.00
 
 
# ---------------------------------------------------------------------------
# Step 1: Fetch (deterministic, zero AI cost)
# ---------------------------------------------------------------------------
 
def fetch_products() -> list[dict]:
    """
    Hits the public Shopify products.json endpoint.
    Raises on failure — the caller sends the degraded-run alert.
    """
    resp = requests.get(PRODUCTS_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    products = data.get("products", [])
    if not products:
        raise ValueError("Fetched 0 products — source may have changed shape")
    return products
 
 
# ---------------------------------------------------------------------------
# Step 2: Normalize + hash (deterministic, zero AI cost)
# ---------------------------------------------------------------------------
 
def simple_hash(s: str) -> str:
    """
    djb2 hash — matches the pure-JS version used in the n8n Code node
    (n8n's sandbox blocks Node's built-in 'crypto' module, so the production
    workflow uses this same algorithm instead of hashlib/sha256).
    """
    h = 5381
    for ch in s:
        h = ((h << 5) + h) + ord(ch)
        h &= 0xFFFFFFFF
    return format(h, "x")
 
 
def normalize_product(p: dict) -> dict:
    """Extract and shape the fields we track — mirrors 'Normalize + Hash' node."""
    variants = p.get("variants", [])
    prices = [float(v["price"]) for v in variants if v.get("price")]
    compare_at_prices = [
        float(v["compare_at_price"]) for v in variants if v.get("compare_at_price")
    ]
 
    min_price = min(prices) if prices else None
    max_compare_at = max(compare_at_prices) if compare_at_prices else None
    on_sale = (
        max_compare_at is not None
        and min_price is not None
        and max_compare_at > min_price
    )
 
    return {
        "id": str(p["id"]),
        "title": p.get("title", ""),
        "handle": p.get("handle", ""),
        "product_type": p.get("product_type", ""),
        "tags": p.get("tags", ""),
        "min_price": min_price,
        "max_price": max(prices) if prices else None,
        "compare_at_price": max_compare_at,
        "on_sale": on_sale,
        "variant_count": len(variants),
        "available": any(v.get("available") for v in variants),
    }
 
 
def hash_record(record: dict) -> str:
    """Stable hash of the fields that matter for change detection."""
    fingerprint = json.dumps(record, sort_keys=True)
    return simple_hash(fingerprint)
 
 
# ---------------------------------------------------------------------------
# Step 3: State store (local JSON file — mirrors n8n's workflow static data)
# ---------------------------------------------------------------------------
 
def load_previous_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text())
 
 
def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))
 
 
# ---------------------------------------------------------------------------
# Step 4: Diff (deterministic — "only report what's new" + stock/sale typing)
# ---------------------------------------------------------------------------
 
def compute_diffs(current_products: list[dict], previous_state: dict) -> tuple[list[dict], bool, int]:
    """
    Returns (diffs, is_baseline_run, baseline_count).
 
    On the very first run (empty previous_state), every product looks "new" —
    that's not a real signal, just the system establishing its starting point.
    So diffs are suppressed on baseline runs, exactly like the n8n workflow.
    """
    is_first_run = len(previous_state) == 0
    diffs = []
    normalized_count = 0
 
    for p in current_products:
        record = normalize_product(p)
        pid = record["id"]
        new_hash = hash_record(record)
        normalized_count += 1
 
        prev = previous_state.get(pid)
 
        if prev is None:
            diffs.append({
                "type": "new_product",
                "product_id": pid,
                "title": record["title"],
                "old": None,
                "new": record,
            })
        elif prev["hash"] != new_hash:
            old_record = prev["record"]
            price_delta = None
            if old_record.get("min_price") is not None and record.get("min_price") is not None:
                price_delta = abs(record["min_price"] - old_record["min_price"])
 
            # Explicit diff typing — higher signal than a generic "other_change"
            if old_record.get("available") is True and record.get("available") is False:
                diff_type = "stock_out"
            elif old_record.get("available") is False and record.get("available") is True:
                diff_type = "restock"
            elif old_record.get("on_sale") is False and record.get("on_sale") is True:
                diff_type = "sale_started"
            elif old_record.get("on_sale") is True and record.get("on_sale") is False:
                diff_type = "sale_ended"
            elif price_delta and price_delta > 0:
                diff_type = "price_change"
            else:
                diff_type = "other_change"
 
            # Deterministic pre-filter: tiny price rounding noise never reaches Claude
            if price_delta is not None and 0 < price_delta < MIN_PRICE_CHANGE_USD:
                price_only_diff = (
                    {**old_record, "min_price": None, "max_price": None}
                    == {**record, "min_price": None, "max_price": None}
                )
                if price_only_diff:
                    previous_state[pid] = {"hash": new_hash, "record": record}
                    continue
 
            diffs.append({
                "type": diff_type,
                "product_id": pid,
                "title": record["title"],
                "old": old_record,
                "new": record,
            })
 
        previous_state[pid] = {"hash": new_hash, "record": record}
 
    if is_first_run:
        return [], True, normalized_count
    return diffs, False, normalized_count
 
 
# ---------------------------------------------------------------------------
# Step 5: Claude judgment (the ONLY step that costs tokens — batched, not looped)
# ---------------------------------------------------------------------------
 
JUDGE_SYSTEM_PROMPT = """You are a competitive intelligence analyst for a marketing agency.
You will be shown a batch of detected changes on a competitor's e-commerce site.
 
For EACH change, decide if it is worth alerting a human about.
 
Report as material (worth_reporting: true):
- New product launches
- Price changes of real magnitude (not rounding/currency noise)
- Products going out of stock or back in stock
- Changes signaling a strategic shift (new product category, new tags suggesting new positioning)
- A product going on sale or a sale ending
- Restocks after being out of stock
 
Do NOT report as material (worth_reporting: false):
- Cosmetic text changes (typo fixes, minor description rewording)
- Negligible price fluctuations
- Metadata-only changes with no customer-facing impact
 
Respond ONLY with valid JSON, no preamble, in this exact shape:
{
  "results": [
    {"product_id": "...", "worth_reporting": true/false, "category": "new_product|price_change|stock_change|other", "reasoning": "one sentence"}
  ]
}
"""
 
 
def judge_diffs_with_claude(diffs: list[dict]) -> list[dict]:
    """
    Sends ALL diffs from this run in a single batched Claude call.
    Returns the diffs annotated with Claude's verdict.
 
    Note: Claude Sonnet 5 returns an internal 'thinking' content block before
    the actual 'text' answer block — the response is parsed by searching for
    the block where type == 'text', not by assuming a fixed index.
    """
    if not diffs:
        return []
 
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
 
    diff_summaries = []
    for d in diffs:
        summary = {
            "product_id": d["product_id"],
            "title": d["title"],
            "type": d["type"],
            "old_price": d["old"]["min_price"] if d["old"] else None,
            "new_price": d["new"].get("min_price"),
            "on_sale": d["new"].get("on_sale"),
            "compare_at_price": d["new"].get("compare_at_price"),
            "old_available": d["old"]["available"] if d["old"] else None,
            "new_available": d["new"].get("available"),
        }
        diff_summaries.append(summary)
 
    user_message = "Batch of detected changes:\n\n" + json.dumps(diff_summaries, indent=2)
 
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": MAX_TOKENS,
            "system": JUDGE_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    response_json = resp.json()
 
    # Find the text block dynamically — do NOT assume content[0] is the answer
    content_blocks = response_json.get("content", [])
    text_block = next((b for b in content_blocks if b.get("type") == "text"), None)
 
    if response_json.get("stop_reason") == "max_tokens":
        print("WARNING: Claude response was truncated (stop_reason: max_tokens). "
              "Consider raising MAX_TOKENS or splitting the batch.")
 
    verdicts_by_id = {}
    if text_block and text_block.get("text"):
        raw_text = text_block["text"].strip()
        raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(raw_text)
            verdicts_by_id = {v["product_id"]: v for v in parsed.get("results", [])}
        except json.JSONDecodeError:
            print("WARNING: Could not parse Claude's response as JSON — "
                  "flagging all items for manual review.")
 
    annotated = []
    for d in diffs:
        verdict = verdicts_by_id.get(d["product_id"], {
            "worth_reporting": True,  # fail safe: unclassified = report it, don't silently drop
            "category": "unclassified",
            "reasoning": "Claude did not return a verdict for this item — flagging for manual review.",
        })
        annotated.append({**d, "verdict": verdict})
 
    return annotated
 
 
# ---------------------------------------------------------------------------
# Step 6: Delivery
# ---------------------------------------------------------------------------
 
def format_slack_message(material_diffs: list[dict]) -> dict:
    lines = [f"🔍 *{BRAND_NAME} monitor — {len(material_diffs)} change(s) detected*\n"]
    for d in material_diffs:
        v = d["verdict"]
        lines.append(f"• *{d['title']}* — `{v['category']}`\n   {v['reasoning']}")
    return {"text": "\n".join(lines)}
 
 
def format_degraded_message(error: str) -> dict:
    return {
        "text": f"🚨 *{BRAND_NAME} monitor degraded*\nFetch returned no products or failed: "
                f"{error}. Source may be blocked, rate-limited, or changed shape. Check manually."
    }
 
 
def send_to_slack(payload: dict) -> None:
    if not SLACK_WEBHOOK_URL:
        print("[WARN] SLACK_WEBHOOK_URL not set — printing payload instead:")
        print(json.dumps(payload, indent=2))
        return
    requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
 
 
# ---------------------------------------------------------------------------
# Step 7: Logging (every run, so you can prove it fired multiple times)
# ---------------------------------------------------------------------------
 
def log_run(status: str, detail: str, diffs_detected: int = 0, diffs_reported: int = 0) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,  # "OK" | "baseline_established" | "no_changes" | "Degraded"
        "diffs_detected": diffs_detected,
        "diffs_reported": diffs_reported,
        "detail": detail,
    }
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[{entry['timestamp']}] {status.upper()}: {detail}")
 
 
# ---------------------------------------------------------------------------
# Main run cycle
# ---------------------------------------------------------------------------
 
def run() -> None:
    previous_state = load_previous_state()
 
    # --- Step 1: fetch, with degraded-path handling ---
    try:
        products = fetch_products()
    except Exception as e:
        log_run("Degraded", "Fetch failed or returned no products — see Slack alert for details")
        send_to_slack(format_degraded_message(str(e)))
        sys.exit(1)
 
    # --- Step 2-4: normalize, hash, diff ---
    diffs, is_baseline, baseline_count = compute_diffs(products, previous_state)
    save_state(previous_state)  # persist regardless of branch taken
 
    if is_baseline:
        log_run(
            "baseline_established",
            f"Baseline established — captured {baseline_count} products, nothing to compare yet",
        )
        return
 
    if not diffs:
        log_run("no_changes", "No diffs detected this run")
        return
 
    # --- Step 5: Claude judgment (batched) ---
    try:
        annotated = judge_diffs_with_claude(diffs)
    except Exception as e:
        log_run("Degraded", f"Claude judgment failed: {e}",
                 diffs_detected=len(diffs), diffs_reported=0)
        sys.exit(1)
 
    material = [d for d in annotated if d["verdict"]["worth_reporting"]]
    noise = [d for d in annotated if not d["verdict"]["worth_reporting"]]
 
    # --- Step 6: deliver only material changes ---
    if material:
        send_to_slack(format_slack_message(material))
 
    # --- Step 7: log every outcome ---
    log_run(
        "OK",
        f"{len(diffs)} diff(s) detected, {len(material)} reported, {len(noise)} filtered as noise",
        diffs_detected=len(diffs),
        diffs_reported=len(material),
    )
 
 
if __name__ == "__main__":
    run()
 



