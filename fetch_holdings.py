#!/usr/bin/env python3
"""Preuzima holdings CSV-ove za VWCE, VGLA (Vanguard GraphQL) i IWMO, SEC0 (iShares)."""

from __future__ import annotations

import csv
import json
import sys
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
OUT = Path(__file__).parent

ISHARES = {
    "sec0_holdings.csv": "https://www.ishares.com/ch/individual/en/products/319084/fund/1495092304805.ajax?fileType=csv&fileName=SEMI_holdings&dataType=fund",
    "iwmo_holdings.csv": "https://www.ishares.com/ch/professionals/en/products/270051/ishares-msci-world-momentum-factor-ucits-etf/1495092304805.ajax?fileType=csv&fileName=IWMO_holdings&dataType=fund",
}

# Isti upit koji koristi dugme Download na Vanguard stranici; X-Consumer-ID je javni ID same stranice
VANGUARD = {"vwce_holdings.csv": "9679", "vgla_holdings.csv": "E161"}
VANGUARD_URL = "https://www.nl.vanguard/gpx/graphql"
CONSUMER_ID = "nl0"
QUERY = """query FundsHoldingsQuery($portIds: [String!], $securityTypes: [String!], $lastItemKey: String) {
 funds(portIds: $portIds) { profile { fundFullName fundCurrency } }
 borHoldings(portIds: $portIds) { holdings(limit: 1500, securityTypes: $securityTypes, lastItemKey: $lastItemKey) {
  items { issuerName securityLongDescription gicsSectorDescription icbSectorDescription icbIndustryDescription marketValuePercentage sedol1 quantity ticker securityType marketValueBaseCurrency bloombergIsoCountry }
  totalHoldings lastItemKey } } }"""


def fetch_ishares(name: str, url: str) -> None:
    data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60).read()
    if b"Asset Class" not in data:
        raise RuntimeError(f"{name}: odgovor ne lici na iShares holdings CSV")
    (OUT / name).write_bytes(data)
    print(f"{name}: {len(data):,} B")


def fetch_vanguard(name: str, port_id: str) -> None:
    rows, key = [], None
    while True:
        body = json.dumps({
            "query": QUERY, "operationName": "FundsHoldingsQuery",
            "variables": {"portIds": [port_id], "securityTypes": None, "lastItemKey": key},
        }).encode()
        req = urllib.request.Request(VANGUARD_URL, body, {
            "Content-Type": "application/json", "User-Agent": UA, "X-Consumer-ID": CONSUMER_ID,
        })
        page = json.load(urllib.request.urlopen(req, timeout=60))["data"]["borHoldings"][0]["holdings"]
        rows += page["items"]
        key = page["lastItemKey"]
        if not key or not page["items"]:
            break
    if not rows:
        raise RuntimeError(f"{name}: nema redova")
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{name}: {len(rows):,} redova")


def main() -> int:
    for name, url in ISHARES.items():
        fetch_ishares(name, url)
    for name, port in VANGUARD.items():
        fetch_vanguard(name, port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
