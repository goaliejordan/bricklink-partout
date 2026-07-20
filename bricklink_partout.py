#!/usr/bin/env python3
"""
bricklink_partout.py
--------------------
Part out a LEGO set into a BrickLink Wanted List XML file.

Given a set number, this script:
  1. Looks up the set name via the BrickLink API.
  2. Parts the set out (the catalog "subsets" endpoint).
  3. Writes a BrickLink Wanted List XML file you can upload at
     bricklink.com -> Wanted -> Upload -> "Upload BrickLink XML".

The list is written "complete": every item's QTYFILLED equals its MINQTY,
so once uploaded the list shows 100% owned (have count == want count).

WHY XML AND NOT A DIRECT API PUSH?
  The public BrickLink API has NO endpoint for creating or editing wanted
  lists (it only covers catalog, store inventory, orders, coupons, etc.).
  Uploading the generated XML on the website is the supported path. During
  upload BrickLink lets you create a new list -- name it after the set.

DEFAULTS (change with flags):
  - Minifigs are kept whole (listed as minifig items), not broken to parts.
  - Extra / spare parts are excluded.

AUTH -- you need all four BrickLink OAuth1 credentials (from
https://www.bricklink.com/v2/api/register_consumer.page). Set them as
environment variables:

  BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN_VALUE, BL_TOKEN_SECRET

...or fill them into the CONFIG block below.

USAGE:
  pip install requests requests_oauthlib
  python bricklink_partout.py 75192
  python bricklink_partout.py 75192-1 --break-minifigs --include-extras
  python bricklink_partout.py 10276 -o ~/Desktop --condition N
"""

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

try:
    import requests
    from requests_oauthlib import OAuth1
except ImportError:
    sys.exit(
        "Missing dependencies. Run:\n"
        "    pip install requests requests_oauthlib"
    )

# --------------------------------------------------------------------------
# CONFIG -- either set the environment variables, or paste your keys here.
# --------------------------------------------------------------------------
CONFIG = {
    "consumer_key":    os.environ.get("BL_CONSUMER_KEY", ""),
    "consumer_secret": os.environ.get("BL_CONSUMER_SECRET", ""),
    "token_value":     os.environ.get("BL_TOKEN_VALUE", ""),
    "token_secret":    os.environ.get("BL_TOKEN_SECRET", ""),
}

API_BASE = "https://api.bricklink.com/api/store/v1"

# BrickLink item type -> Wanted List XML ITEMTYPE code
TYPE_CODE = {
    "PART": "P",
    "MINIFIG": "M",
    "SET": "S",
    "GEAR": "G",
    "BOOK": "B",
    "CATALOG": "C",
    "INSTRUCTION": "I",
    "ORIGINAL_BOX": "O",
    "UNSORTED_LOT": "U",
}


def get_auth():
    c = CONFIG
    missing = [k for k, v in c.items() if not v]
    if missing:
        sys.exit(
            "Missing BrickLink credentials: " + ", ".join(missing) + "\n"
            "Set env vars BL_CONSUMER_KEY, BL_CONSUMER_SECRET, "
            "BL_TOKEN_VALUE, BL_TOKEN_SECRET (or edit CONFIG in the script).\n"
            "Get them at https://www.bricklink.com/v2/api/register_consumer.page"
        )
    return OAuth1(
        c["consumer_key"],
        c["consumer_secret"],
        c["token_value"],
        c["token_secret"],
    )


def api_get(path, auth, params=None):
    """GET a BrickLink API resource and return the 'data' payload."""
    url = API_BASE + path
    resp = requests.get(url, auth=auth, params=params, timeout=30)
    try:
        payload = resp.json()
    except ValueError:
        sys.exit(f"Non-JSON response from {url} (HTTP {resp.status_code}):\n{resp.text[:500]}")
    meta = payload.get("meta", {})
    if meta.get("code") not in (200, 201, 204):
        sys.exit(
            f"BrickLink API error on {path}: "
            f"{meta.get('code')} {meta.get('message')} - {meta.get('description')}"
        )
    return payload.get("data")


def normalize_set_no(raw):
    """Ensure the set number carries a sequence suffix, e.g. 75192 -> 75192-1."""
    raw = raw.strip()
    if re.search(r"-\d+$", raw):
        return raw
    return raw + "-1"


def safe_filename(name):
    name = re.sub(r"[^\w\s.-]", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name or "wanted_list"


def fetch_set_name(set_no, auth):
    data = api_get(f"/items/SET/{set_no}", auth)
    if not data:
        sys.exit(f"Set {set_no} not found in the BrickLink catalog.")
    return data.get("name", set_no)


def fetch_subsets(set_no, auth, break_minifigs):
    """Return the list of match-groups for the set's subsets (parts)."""
    params = {
        "break_minifigs": "true" if break_minifigs else "false",
        # keep sub-sets (like a set bundled in a set) as their own items:
        "break_subsets": "false",
        "instruction": "false",
        "box": "false",
    }
    data = api_get(f"/items/SET/{set_no}/subsets", auth, params=params)
    if not data:
        sys.exit(f"No subset (part-out) data returned for {set_no}.")
    return data


def build_items(match_groups, include_extras):
    """
    Collapse the subset match-groups into a list of wanted-list line items.

    - Only the primary entry of each match-group is used (alternates skipped).
    - Quantities for identical (type, no, color) lines are merged.
    """
    merged = defaultdict(int)  # (type, no, color_id) -> quantity
    order = []                 # preserve first-seen order

    for group in match_groups:
        entries = group.get("entries", [])
        # primary = the non-alternate entry; fall back to first entry
        primary = next((e for e in entries if not e.get("is_alternate")), None)
        if primary is None and entries:
            primary = entries[0]
        if primary is None:
            continue

        item = primary.get("item", {})
        itype = item.get("type", "PART").upper()
        ino = item.get("no")
        if not ino:
            continue
        color = primary.get("color_id", 0)

        qty = int(primary.get("quantity", 0) or 0)
        if include_extras:
            qty += int(primary.get("extra_quantity", 0) or 0)
        if qty <= 0:
            continue

        key = (itype, ino, color)
        if key not in merged:
            order.append(key)
        merged[key] += qty

    return [(t, n, c, merged[(t, n, c)]) for (t, n, c) in order]


def build_xml(items, condition):
    """Build the BrickLink Wanted List XML tree (QTYFILLED == MINQTY)."""
    root = ET.Element("INVENTORY")
    for itype, ino, color, qty in items:
        el = ET.SubElement(root, "ITEM")
        ET.SubElement(el, "ITEMTYPE").text = TYPE_CODE.get(itype, "P")
        ET.SubElement(el, "ITEMID").text = str(ino)
        ET.SubElement(el, "COLOR").text = str(color)
        ET.SubElement(el, "MINQTY").text = str(qty)
        # QTYFILLED == MINQTY  -> list shows as complete (have == want)
        ET.SubElement(el, "QTYFILLED").text = str(qty)
        if condition in ("N", "U"):
            ET.SubElement(el, "CONDITION").text = condition
    # pretty print (Python 3.9+: ET.indent)
    try:
        ET.indent(root, space="  ")
    except AttributeError:
        pass
    return ET.ElementTree(root)


def main():
    ap = argparse.ArgumentParser(description="Part out a LEGO set into a complete BrickLink Wanted List XML.")
    ap.add_argument("set_no", help="Set number, e.g. 75192 or 75192-1")
    ap.add_argument("-o", "--outdir", default=".", help="Output directory (default: current dir)")
    ap.add_argument("--break-minifigs", action="store_true", help="Break minifigs into individual parts")
    ap.add_argument("--include-extras", action="store_true", help="Include the set's extra/spare parts")
    ap.add_argument("--condition", choices=["N", "U", ""], default="", help="Wanted condition: N=new, U=used, blank=any")
    args = ap.parse_args()

    auth = get_auth()
    set_no = normalize_set_no(args.set_no)

    print(f"Looking up set {set_no} ...")
    set_name = fetch_set_name(set_no, auth)
    print(f"  {set_no}: {set_name}")

    print("Parting out (fetching subsets) ...")
    groups = fetch_subsets(set_no, auth, break_minifigs=args.break_minifigs)
    items = build_items(groups, include_extras=args.include_extras)
    if not items:
        sys.exit("No items to write after processing subsets.")

    total_lines = len(items)
    total_pieces = sum(q for *_ , q in items)
    print(f"  {total_lines} unique lots, {total_pieces} pieces total.")

    tree = build_xml(items, condition=args.condition)

    os.makedirs(args.outdir, exist_ok=True)
    fname = f"{safe_filename(set_name)}_{set_no}_wanted.xml"
    fpath = os.path.join(args.outdir, fname)
    tree.write(fpath, encoding="utf-8", xml_declaration=True)

    print(f"\nWrote: {fpath}")
    print("\nNext step -- upload to BrickLink:")
    print("  1. Go to bricklink.com -> Want -> Upload.")
    print("  2. Choose 'Upload BrickLink XML' and paste/select this file.")
    print(f"  3. Create a NEW wanted list and name it: {set_name}")
    print("  The list will show as 100% complete (have == want).")


if __name__ == "__main__":
    main()
