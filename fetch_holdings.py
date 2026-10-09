#!/usr/bin/env python3
"""Preuzima holdings CSV-ove za VWCE, VGLA (Vanguard GraphQL) i IWMO, SEC0 (iShares)."""

from __future__ import annotations

import csv
import http.cookiejar
import json
import sys
import urllib.request
import zipfile
from xml.etree import ElementTree as ET
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


# VanEck UCITS SMH: sajt trazi kolacice sesije, pa se prvo otvori stranica fonda, pa tek onda Download
VANECK_PAGE = "https://www.vaneck.com/lu/en/investments/semiconductor-etf"
VANECK_DOWNLOAD = VANECK_PAGE + "/downloads/holdings/"
XLSX_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _xlsx_rows(data: bytes) -> list[list[str]]:
    """Minimalno citanje prvog sheet-a iz .xlsx (stdlib, bez dodatnih paketa)."""
    import io

    z = zipfile.ZipFile(io.BytesIO(data))
    strings = [
        "".join(si.itertext())
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("x:si", XLSX_NS)
    ]
    rows = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(f"{{{XLSX_NS['x']}}}row"):
        vals = []
        for c in row.findall("x:c", XLSX_NS):
            v = c.find("x:v", XLSX_NS)
            vals.append("" if v is None else strings[int(v.text)] if c.get("t") == "s" else v.text)
        rows.append(vals)
    return rows


def fetch_vaneck_smh(name: str = "smh_holdings.csv") -> None:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", UA)]
    opener.open(VANECK_PAGE, timeout=60).read()
    resp = opener.open(VANECK_DOWNLOAD, timeout=60)
    data = resp.read()
    if not data.startswith(b"PK"):
        raise RuntimeError(f"{name}: odgovor nije .xlsx ({resp.geturl()})")
    rows = _xlsx_rows(data)
    header = next(i for i, r in enumerate(rows) if "Holding Name" in r)
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["number", "name", "ticker", "isin", "shares", "market_value", "weight"])
        w.writerows(r for r in rows[header + 1:] if any(r))
    print(f"{name}: {len(rows) - header - 1} redova")


def main() -> int:
    for name, url in ISHARES.items():
        fetch_ishares(name, url)
    for name, port in VANGUARD.items():
        fetch_vanguard(name, port)
    fetch_vaneck_smh()
    return 0


if __name__ == "__main__":
    sys.exit(main())
