#!/usr/bin/env python3
"""Classe toute la base nationale dans un rythme de surveillance utile."""
from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
SOURCE_FILE = ROOT / "sources.csv"
FIELDS = ["nom", "categorie", "url", "active", "usage", "motif"]
NON_COLLECTED_USAGES = {"acces_authentifie", "hors_perimetre", "source_donnees"}


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


def scheduled_usage(row: dict[str, str]) -> tuple[str, str]:
    """Les profils tournent vite ; les annuaires servent à découvrir de nouvelles pages."""
    if row.get("categorie") == "juridiction_greffe":
        return "decouverte_nationale", "Annuaire officiel balayé par rotation hebdomadaire pour découvrir les pages de ventes"
    return "veille_rapide", "Profil de praticien balayé à chaque passage pour détecter annonces et sites de cabinets"


def optimise_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, int]]:
    output = []
    daily = rapid = discovery = non_collected = 0
    for original in rows:
        row = dict(original)
        usage = (row.get("usage") or "").strip()
        if usage in NON_COLLECTED_USAGES:
            row["active"] = "0"
            non_collected += 1
        elif is_generated_reference(row):
            usage, reason = scheduled_usage(row)
            row.update(active="1", usage=usage, motif=reason)
            if usage == "veille_rapide":
                rapid += 1
            else:
                discovery += 1
        elif usage in {"veille_rapide", "decouverte_nationale"}:
            row["active"] = "1"
            if usage == "veille_rapide":
                rapid += 1
            else:
                discovery += 1
        else:
            row.update(active="1", usage="veille_quotidienne", motif=active_reason(row))
            daily += 1
        output.append(row)
    return output, {"veille_quotidienne": daily, "veille_rapide": rapid,
                    "decouverte_nationale": discovery, "non_collectees": non_collected,
                    "total": len(output)}


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
        f"{counts['veille_rapide']} profils en veille rapide ; "
        f"{counts['decouverte_nationale']} annuaires en découverte nationale ; "
        f"{counts['non_collectees']} références documentées mais non collectées ; "
        f"{counts['total']} références au total."
    )
