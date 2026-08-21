"""
==========================================================================
HELPER: generate_nifty200_symbols.json
==========================================================================
Aa script tamari stock_sector_map.json (symbol list) leine Angel One ni
official scrip master file sathe match kare chhe, ane dareek symbol nu
correct "token" shodhi ne nifty200_symbols.json banave chhe.

Run karta pehla:
    pip install requests

Run:
    python generate_nifty200_symbols.py

Output:
    nifty200_symbols.json   -> [{"symbol": "...", "token": "..."}, ...]

Note: Aa script fakt EK VAAR run karvani chhe (ya jyare tamari stock
list update thay tyare). Roj-roj chalavani jarur nathi - tokens
normally change nathi thata.
==========================================================================
"""

import json
import requests

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
SECTOR_MAP_FILE = "stock_sector_map.json"
OUTPUT_FILE = "nifty200_symbols.json"


def load_symbol_list():
    with open(SECTOR_MAP_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [s["symbol"] for s in data if "symbol" in s]


def download_scrip_master():
    print("Downloading Angel One scrip master (may take a bit, file is large)...")
    resp = requests.get(SCRIP_MASTER_URL, timeout=60)
    resp.raise_for_status()
    return resp.json()


def build_token_lookup(scrip_master):
    # Only NSE cash-market equities, keyed by exact tradingsymbol (e.g. "SBIN-EQ")
    lookup = {}
    for row in scrip_master:
        if row.get("exch_seg") == "NSE" and row.get("symbol", "").endswith("-EQ"):
            lookup[row["symbol"]] = row["token"]
    return lookup


def main():
    symbols = load_symbol_list()
    print(f"{len(symbols)} symbols found in {SECTOR_MAP_FILE}")

    scrip_master = download_scrip_master()
    lookup = build_token_lookup(scrip_master)
    print(f"{len(lookup)} NSE -EQ instruments found in scrip master")

    matched = []
    unmatched = []
    for sym in symbols:
        token = lookup.get(sym)
        if token:
            matched.append({"symbol": sym, "token": token})
        else:
            unmatched.append(sym)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(matched, f, indent=2, ensure_ascii=False)

    print(f"\n✅ {OUTPUT_FILE} banai gai — {len(matched)} symbols match thaya.")

    if unmatched:
        print(f"\n⚠️ {len(unmatched)} symbols match NA thaya (naam different hovu joiye):")
        for s in unmatched:
            print(f"   - {s}")
        print(
            "\nAa symbols mate scrip master ma sacha 'symbol' field manually "
            "shodhi ne stock_sector_map.json ma naam sudharo, pachi script "
            "pacha chalavo."
        )


if __name__ == "__main__":
    main()
