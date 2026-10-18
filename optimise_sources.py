#!/usr/bin/env python3
"""Sépare la veille quotidienne du référentiel national de contacts."""
from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
SOURCE_FILE = ROOT / "sources.csv"
FIELDS = ["nom", "categorie", "url", "active", "usage", "motif"]


def is_generated_reference(row: dict[str, str]) -> bool:
    """Reconnaît les lignes ajoutées automatiquement depuis les annuaires."""
    name = (row.get("nom") or "").casefold()
    host = (urlsplit(row.get("url") or "").hostname or "").casefold().removeprefix("www.")
    return row.get("categorie") in {"avocat", "juridiction_greffe"} and "national" in name and host in name


def active_reason(row: dict[str, str]) -> str:
    return {
        "site_pro": "Page nationale ou régionale de ventes",
        "journal": "Rubrique d'annonces légales et de ventes",
        "barreau": "Page de ventes immobilières d'un barreau",
        "avocat": "Page de ventes d'un cabinet identifiée directement",
    }.get(row.get("categorie") or "", "Source de ventes ajoutée manuellement")


def reference_reason(row: dict[str, str]) -> str:
    host = (urlsplit(row.get("url") or "").hostname or "").lower().removeprefix("www.")
    if row.get("categorie") == "juridiction_greffe":
        return "Annuaire ou contact officiel, sans calendrier de ventes vérifié"
    if host in {"info-encheres.com", "licitor.com", "immoenchere.fr", "encheres-publiques.com"}:
        return "Profil de praticien couvert par la page nationale de la plateforme"
    return "Référence de praticien à qualifier avant activation"


def optimise_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, int]]:
    output = []
    active = references = 0
    for original in rows:
        row = dict(original)
        if is_generated_reference(row):
            row.update(active="0", usage="referentiel", motif=reference_reason(row))
            references += 1
        else:
            row.update(active="1", usage="veille_quotidienne", motif=active_reason(row))
            active += 1
        output.append(row)
    return output, {"veille_quotidienne": active, "referentiel": references, "total": len(output)}


def optimise(path: Path = SOURCE_FILE) -> dict[str, int]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    output, counts = optimise_rows(rows)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output)
    temporary.replace(path)
    return counts


if __name__ == "__main__":
    counts = optimise()
    print(
        f"Sources : {counts['veille_quotidienne']} en veille quotidienne ; "
        f"{counts['referentiel']} conservées comme références ; {counts['total']} au total."
    )
