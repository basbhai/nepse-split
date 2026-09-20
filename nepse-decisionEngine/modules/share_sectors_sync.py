"""
modules/share_sectors_sync.py

Keeps share_sectors in sync with newly-listed NEPSE companies. share_sectors
was originally populated as a one-off bulk import and had no ongoing sync —
new listings (e.g. KHPL, TPKHL) were silently missing a sector mapping until
caught manually.

Source: chukul.com/api/stock/ (same API modules/fundamentals_scraper.py
already trusts and already discovers new listings from) + chukul.com/api/sector/
for the id → name lookup.

Purely additive: only INSERTs symbols missing from share_sectors. Never
touches existing rows, since those came from a richer original import
(companyname, securityname, companyemail, website, regulatorybody) that this
source can't reproduce — externalid, securityname, companyemail, website and
regulatorybody are left NULL on new rows (chukul's own `id` is a different
numbering scheme than share_sectors.externalid's NEPSE-native IDs, so it is
deliberately not written there to avoid a misleading value).

Run:
    python -m modules.share_sectors_sync
    python -m modules.share_sectors_sync --dry-run
"""

import logging
import argparse
import requests
from datetime import datetime, timezone
from db.connection import _db

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

STOCK_URL  = "https://chukul.com/api/stock/"
SECTOR_URL = "https://chukul.com/api/sector/"

# chukul sector `name` → share_sectors' existing canonical sectorname.
# Only the entries that differ need mapping; anything not listed here falls
# back to chukul's own name unchanged (Finance, Investment, Life Insurance,
# Microfinance, Others, Tradings, Mutual Fund already match verbatim).
SECTOR_NAME_MAP = {
    "Banking":       "Commercial Banks",
    "Dev Banks":     "Development Banks",
    "Hotels":        "Hotels And Tourism",
    "Hydropower":    "Hydro Power",
    "Manufacturing": "Manufacturing And Processing",
    "NonLife":       "Non Life Insurance",
}

# Non-equity chukul sector ids to skip outright (promoter shares, debenture,
# mutual fund units) — share_sectors only tracks ordinary equity today.
EXCLUDED_SECTOR_IDS = {13, 14, 15}  # Mutual Fund, Promoter, Debenture


def _fetch_json(url: str):
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("fetch failed for %s: %s", url, e)
        return None


def _load_sector_map() -> dict:
    """chukul sector id -> canonical sectorname used in share_sectors."""
    data = _fetch_json(SECTOR_URL)
    if not data:
        return {}
    out = {}
    for s in data:
        sid = s.get("id")
        name = s.get("name", "")
        if sid is None:
            continue
        out[sid] = SECTOR_NAME_MAP.get(name, name)
    return out


def _load_existing_symbols() -> set:
    with _db() as cur:
        cur.execute("SELECT symbol FROM share_sectors")
        rows = cur.fetchall()
    return {r["symbol"].upper() for r in rows if r.get("symbol")}


def _insert_row(row: dict, dry_run: bool) -> bool:
    if dry_run:
        log.info("[DRY RUN] Would insert: %s | %s | %s",
                  row["symbol"], row["companyname"], row["sectorname"])
        return True
    sql = """
        INSERT INTO share_sectors (
            companyname, symbol, sectorname, instrumenttype, status, inserted_at
        ) VALUES (
            %(companyname)s, %(symbol)s, %(sectorname)s, %(instrumenttype)s,
            %(status)s, %(inserted_at)s
        )
    """
    try:
        with _db() as cur:
            cur.execute(sql, row)
        return True
    except Exception as e:
        log.error("insert failed for %s: %s", row["symbol"], e)
        return False


def run(dry_run: bool = False):
    log.info("=== share_sectors_sync started (dry_run=%s) ===", dry_run)

    stocks = _fetch_json(STOCK_URL)
    if not stocks:
        log.error("No stock list from chukul — aborting")
        return

    sector_map = _load_sector_map()
    existing = _load_existing_symbols()
    now = datetime.now(timezone.utc)

    added, skipped_known, skipped_filtered, failed = 0, 0, 0, 0
    added_symbols = []

    for s in stocks:
        name = (s.get("name") or "").strip()
        symbol = (s.get("symbol") or "").strip().upper()
        if not symbol or not name:
            continue
        name_lower = name.lower()
        if any(kw in name_lower for kw in ("debenture", "mutual", "promoter", "promotor")):
            skipped_filtered += 1
            continue
        if s.get("sector") in EXCLUDED_SECTOR_IDS:
            skipped_filtered += 1
            continue
        if symbol in existing:
            skipped_known += 1
            continue

        row = {
            "companyname":    name,
            "symbol":         symbol,
            "sectorname":     sector_map.get(s.get("sector"), "Others"),
            "instrumenttype": "Equity",
            "status":         "D" if (s.get("is_delisted") or s.get("is_merged")) else "A",
            "inserted_at":    now,
        }
        if _insert_row(row, dry_run):
            added += 1
            added_symbols.append(symbol)
        else:
            failed += 1

    log.info("share_sectors_sync complete — added=%d skipped_known=%d "
              "skipped_non_equity=%d failed=%d", added, skipped_known,
              skipped_filtered, failed)
    if added_symbols:
        log.info("New symbols added: %s", ", ".join(added_symbols))


if __name__ == "__main__":
    from log_config import attach_file_handler
    attach_file_handler(__name__)
    parser = argparse.ArgumentParser(description="Sync new NEPSE listings into share_sectors")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
