#!/usr/bin/env python3
"""
Look-through analiza UCITS portfolija sastavljenog od 4 ETF-a.

Tok obrade:
  1. Definicija ciljnih ponderа ETF-ova (zbir mora biti 1.0).
  2. Ucitavanje holdings CSV fajlova bez ISIN-a: iShares (meta-redovi, ’ kao separator
     hiljada, samo Asset Class == Equity) i Vanguard (EQ.* hartije).
  3. Normalizacija tezina na decimalni format [0, 1].
  4. Outer join po normalizovanom nazivu kompanije (bez Inc/Corp/Ltd...), prazno -> 0.
  5. Realni ponderi kompanija = matricno mnozenje (holdings matrica @ ponderi ETF-ova).
  6. Ispis Top 20 pozicija.
"""

from __future__ import annotations

import io
import logging
import re
import sys
import unicodedata
from difflib import get_close_matches
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("lookthrough")

# --------------------------------------------------------------------------- #
# 1. Konfiguracija
# --------------------------------------------------------------------------- #

# Ciljni ponderi ETF-ova u portfoliju (zbir mora biti 1.0)
PORTFOLIO_WEIGHTS = pd.Series(
    {
        "VWCE": 0.83,
        "VGLA": 0.07,
        "IWMO": 0.065,
        "SEC0": 0.035,
    },
    name="etf_weight",
)

# Putanje do lokalnih CSV fajlova
HOLDINGS_FILES = {
    "VWCE": Path("vwce_holdings.csv"),
    "VGLA": Path("vgla_holdings.csv"),
    "IWMO": Path("iwmo_holdings.csv"),
    "SEC0": Path("sec0_holdings.csv"),
}

TOP_N = 20

# Izvori: iShares (Asset Class == Equity) i Vanguard (securityType EQ.*)
ISHARES = {"IWMO", "SEC0"}

# Sufiksi i sluzbene reci koje se skidaju pri normalizaciji naziva kompanije
NAME_NOISE = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc",
    "llc", "lp", "sa", "ag", "nv", "se", "ab", "asa", "oyj", "spa", "sab", "de", "cv", "bhd",
    "pcl", "tbk", "kgaa", "holding", "holdings", "group", "the", "ord", "adr", "gdr", "shs",
    "cl", "class", "series", "common", "stock", "reg", "registered", "sponsored", "pref",
    "preferred", "reit", "ps", "par", "rep", "representing", "dummy", "subordinate", "voting",
    "n", "a", "b", "c", "d", "ads", "dr", "units", "unit", "new", "com", "cdi",
}

# Ucitavanje i spajanje se radi samo nad hartijama vrednosti (cash, FX, futures se izbacuju)
ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


# --------------------------------------------------------------------------- #
# 2. Ucitavanje
# --------------------------------------------------------------------------- #

def normalize_name(name: str) -> str:
    """Naziv kompanije -> kljuc za spajanje (bez akcenata, interpunkcije i pravnih sufiksa)."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t not in NAME_NOISE]
    return " ".join(tokens)


def _read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Fajl ne postoji: {path.resolve()}")
    if path.stat().st_size == 0:
        raise ValueError(f"Fajl je prazan: {path}")
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Nije moguce dekodirati fajl: {path}")


def _to_number(series: pd.Series) -> pd.Series:
    """Tekst -> float; podrzava separator hiljada ’ , ' i razmake, kao i decimalnu tacku/zarez."""
    s = series.astype(str).str.strip().str.replace("%", "", regex=False)
    s = s.str.replace("[’'\u00a0\\s]", "", regex=True)
    both = s.str.contains(",", regex=False) & s.str.contains(".", regex=False)
    s = s.where(~both, s.str.replace(",", "", regex=False))
    only_comma = s.str.contains(",", regex=False) & ~s.str.contains(".", regex=False)
    s = s.where(~only_comma, s.str.replace(",", ".", regex=False))
    return pd.to_numeric(s, errors="coerce")


def _read_ishares(text: str) -> pd.DataFrame:
    """iShares: meta-redovi pa zaglavlje (Ticker,Name,...,Asset Class,...,Weight (%))."""
    lines = text.splitlines()
    idx = next(
        (i for i, l in enumerate(lines)
         if "asset class" in l.lower() and "weight" in l.lower() and "name" in l.lower()),
        None,
    )
    if idx is None:
        raise ValueError("iShares zaglavlje (Name/Asset Class/Weight) nije pronadjeno.")
    df = pd.read_csv(io.StringIO("\n".join(lines[idx:])), dtype=str, on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]
    df = df[df["Asset Class"].str.strip().eq("Equity")]
    return pd.DataFrame({"name": df["Name"], "weight": _to_number(df["Weight (%)"])})


def _read_vanguard(text: str) -> pd.DataFrame:
    """Vanguard izvoz: issuerName, securityType, marketValuePercentage... (zadrzavaju se EQ.* tipovi)."""
    lines = text.splitlines()
    idx = next(
        (i for i, l in enumerate(lines)
         if re.search(r"holding name|issuername|^name\b", l, re.I)
         and re.search(r"% of market value|marketvaluepercentage", l, re.I)),
        None,
    )
    if idx is None:
        raise ValueError("Vanguard zaglavlje (naziv + % of market value) nije pronadjeno.")
    df = pd.read_csv(io.StringIO("\n".join(lines[idx:])), dtype=str, on_bad_lines="skip")
    cols = {c.strip().lower(): c for c in df.columns}
    name_col = next(cols[k] for k in ("issuername", "holding name", "name") if k in cols)
    w_col = next(cols[k] for k in ("marketvaluepercentage", "% of market value") if k in cols)
    if "securitytype" in cols:
        df = df[df[cols["securitytype"]].str.upper().str.startswith("EQ")]
    return pd.DataFrame({"name": df[name_col], "weight": _to_number(df[w_col])})


def load_holdings(etf: str, path: Path) -> tuple[pd.Series, pd.Series]:
    """Vraca (tezine, nazivi): index = normalizovan naziv, tezine u [0, 1], nazivi za prikaz."""
    text = _read_text(path)
    raw = _read_ishares(text) if etf in ISHARES else _read_vanguard(text)
    raw = raw.dropna(subset=["name", "weight"])
    raw["name"] = raw["name"].astype(str).str.strip()
    raw["key"] = raw["name"].map(normalize_name)
    raw = raw[raw["key"] != ""]
    if raw.empty:
        raise ValueError(f"{etf}: nema validnih redova nakon ciscenja.")

    raw["weight"] = raw["weight"] / 100.0   # svi izvori su u procentima
    total = raw["weight"].sum()
    log.info("%s: %d akcijskih pozicija, zbir tezina = %.4f", etf, len(raw), total)
    if not 0.80 <= total <= 1.02:
        log.warning("%s: zbir tezina izvan ocekivanog opsega, proveriti fajl.", etf)

    # Vise listinga / klasa akcija iste kompanije se sabiraju
    series = raw.groupby("key")["weight"].sum().rename(etf)
    # Za prikaz: naziv pozicije sa najvecom tezinom
    names = raw.sort_values("weight", ascending=False).drop_duplicates("key").set_index("key")["name"]
    return series, names


# --------------------------------------------------------------------------- #
# 3-5. Spajanje i look-through
# --------------------------------------------------------------------------- #

def _stem(key: str) -> str:
    """Skida mnozinu (instruments -> instrument) da bi 'Texas Instrument' uparilo 'Texas Instruments'."""
    return " ".join(t[:-1] if len(t) > 4 and t.endswith("s") else t for t in key.split())


def match_short_names(series: pd.Series, universe: set[str]) -> pd.Series:
    """
    iShares skracuje nazive (npr. 'APPLIED MATERIAL INC', 'TORONTO DOMINION'). Za kljuceve koji ne
    postoje u univerzumu trazi jedinstvenog kandidata: isti kljuc bez mnozine, kljuc kao prefiks
    punog naziva (po recima) ili vrlo slican naziv (difflib >= 0.9). Dvosmislenost -> bez uparivanja.
    """
    stems: dict[str, list[str]] = {}
    for k in universe:
        stems.setdefault(_stem(k), []).append(k)
    mapping: dict[str, str] = {}
    for key in series.index:
        if key in universe:
            continue
        st = _stem(key)
        cands = list(stems.get(st, []))
        if not cands and len(key.split()) >= 2:
            # puni naziv pocinje skracenim; obrnuto (skraceni je duzi) samo ako je Vanguard kljuc >= 2 reci
            cands = [
                k for k in universe
                if (k + " ").startswith(key + " ")
                or ((key + " ").startswith(k + " ") and len(k.split()) >= 2)
            ]
        if not cands:
            cands = get_close_matches(key, universe, n=2, cutoff=0.9)
            if len(cands) > 1:
                cands = []
        if len(cands) == 1:
            mapping[key] = cands[0]
    if mapping:
        log.info("%s: %d skracenih naziva upareno sa Vanguard univerzumom", series.name, len(mapping))
    return series.rename(index=mapping).groupby(level=0).sum().rename(series.name)


def build_holdings_matrix(files: dict[str, Path]) -> tuple[pd.DataFrame, pd.Series]:
    """Ucitava sve fajlove i vraca (matrica kompanija x ETF, mapa kljuc -> naziv)."""
    series_list: list[pd.Series] = []
    names: dict[str, str] = {}
    failed: list[str] = []

    for etf, path in files.items():
        try:
            s, s_names = load_holdings(etf, path)
        except (FileNotFoundError, ValueError, KeyError, pd.errors.ParserError) as exc:
            log.error("%s: ucitavanje nije uspelo: %s", etf, exc)
            failed.append(etf)
            continue
        series_list.append(s)
        for key, name in s_names.items():
            names.setdefault(key, name)

    if failed:
        raise RuntimeError(f"Neuspelo ucitavanje za: {', '.join(failed)}. Analiza prekinuta.")

    # Skraceni iShares nazivi se upare sa nazivima iz Vanguard fondova (VWCE/VGLA)
    universe = {k for s in series_list if s.name in ("VWCE", "VGLA") for k in s.index}
    series_list = [
        match_short_names(s, universe) if s.name in ISHARES and universe else s
        for s in series_list
    ]

    # Outer join po normalizovanom nazivu; nedostajuce vrednosti znace da fond ne drzi hartiju -> 0.0
    matrix = pd.concat(series_list, axis=1, join="outer").fillna(0.0)
    return matrix, pd.Series(names, name="name")


def compute_lookthrough(matrix: pd.DataFrame, etf_weights: pd.Series) -> pd.Series:
    """Realni ponderi = H @ w, gde je H (kompanija x ETF), a w vektor ponderа ETF-ova."""
    w = etf_weights.reindex(matrix.columns)
    if w.isna().any():
        raise ValueError("Ponderi ne pokrivaju sve ETF-ove u matrici.")
    result = pd.Series(matrix.to_numpy() @ w.to_numpy(), index=matrix.index, name="portfolio_weight")
    return result.sort_values(ascending=False)


def report_unmatched(matrix: pd.DataFrame, names: pd.Series, etf_weights: pd.Series) -> None:
    """
    Nepodudarne pozicije = hartije koje fond drzi, a koje ne postoje ni u jednom od globalnih
    Vanguard fondova (VWCE/VGLA), pa se ne mogu upariti po nazivu. Ispisuje broj i ukupan ponder
    u portfoliju (tezina u fondu x ponder fonda).
    """
    base = [c for c in ("VWCE", "VGLA") if c in matrix.columns]
    others = [c for c in matrix.columns if c not in base]
    in_base = (matrix[base] > 0).any(axis=1)
    print("\nNepodudarne pozicije (nema ih u VWCE/VGLA):")
    for etf in others:
        mask = (matrix[etf] > 0) & ~in_base
        w = float((matrix.loc[mask, etf] * etf_weights[etf]).sum())
        print(f"  {etf}: {int(mask.sum())} pozicija, {matrix.loc[mask, etf].sum():.4%} fonda, "
              f"{w:.4%} portfolija")
        for key in matrix.loc[mask, etf].sort_values(ascending=False).index[:10]:
            print(f"      {names.get(key, key)!r:45} {matrix.at[key, etf]:.4%}")
    only_one = (matrix > 0).sum(axis=1) == 1
    total_unmatched = float(
        (matrix.loc[only_one].to_numpy() @ etf_weights.reindex(matrix.columns).to_numpy()).sum()
    )
    print(f"Hartije prisutne u samo jednom fondu: {int(only_one.sum())} "
          f"({total_unmatched:.4%} portfolija)")


def check_nvidia(matrix: pd.DataFrame) -> None:
    """Poredi NVIDIA u VWCE matrici sa vrednoscu iz izvornog fajla (vwce_holdings.csv)."""
    key = normalize_name("NVIDIA Corp")
    if "VWCE" not in matrix.columns or key not in matrix.index:
        print("\nNVIDIA provera: nije pronadjena u matrici.")
        return
    src = pd.read_csv(HOLDINGS_FILES["VWCE"], dtype=str)
    row = src[src.iloc[:, 0].str.contains("NVIDIA", case=False, na=False)]
    file_w = float(row.iloc[0]["marketValuePercentage"]) / 100.0 if not row.empty else float("nan")
    got = float(matrix.at[key, "VWCE"])
    ok = np.isclose(got, file_w, atol=1e-9)
    print(f"\nNVIDIA u VWCE: matrica = {got:.6%}, fajl = {file_w:.6%} -> {'OK' if ok else 'NE ODGOVARA'}")


def export_matrix(matrix: pd.DataFrame, names: pd.Series, path: str = "holdings_matrix.csv") -> None:
    """Matrica kompanija x ETF (tezine kao decimale) za Google Sheet (IMPORTDATA + MMULT)."""
    out = matrix.join(names).rename_axis("key").reset_index(drop=True)
    out = out[["name", *matrix.columns]].sort_values(matrix.columns[0], ascending=False)
    out.to_csv(path, index=False, float_format="%.8f")
    log.info("Matrica za Sheet sacuvana u %s (%d redova)", path, len(out))


def validate_weights(w: pd.Series) -> None:
    if (w < 0).any():
        raise ValueError("Ponderi ETF-ova ne smeju biti negativni.")
    if not np.isclose(w.sum(), 1.0, atol=1e-9):
        raise ValueError(f"Zbir ponderа mora biti 1.0, a iznosi {w.sum():.6f}.")


# --------------------------------------------------------------------------- #
# 6. Glavni program
# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        validate_weights(PORTFOLIO_WEIGHTS)
        matrix, names = build_holdings_matrix(HOLDINGS_FILES)
    except (ValueError, RuntimeError) as exc:
        log.error("%s", exc)
        return 1

    lookthrough = compute_lookthrough(matrix, PORTFOLIO_WEIGHTS)

    report = (
        pd.concat([lookthrough, matrix.reindex(lookthrough.index)], axis=1)
        .join(names)
        .rename_axis("key")
    )

    top = report.head(TOP_N).copy()
    fmt = {"portfolio_weight": "{:.4%}".format}
    fmt.update({etf: "{:.4%}".format for etf in matrix.columns})

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", None)
    print(f"\nTop {TOP_N} pozicija (look-through):\n")
    print(top[["name", "portfolio_weight", *matrix.columns]].to_string(formatters=fmt))

    covered = lookthrough.sum()
    print(f"\nBroj jedinstvenih hartija: {len(report)}")
    print(f"Pokrivenost portfolija (zbir look-through ponderа): {covered:.4%}")
    print(f"Top {TOP_N} ukupno: {top['portfolio_weight'].sum():.4%}")

    report_unmatched(matrix, names, PORTFOLIO_WEIGHTS)
    check_nvidia(matrix)

    export_matrix(matrix, names)
    report.to_csv("lookthrough_result.csv", float_format="%.8f")
    log.info("Kompletan rezultat sacuvan u lookthrough_result.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())