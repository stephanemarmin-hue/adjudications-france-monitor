#!/usr/bin/env python3
"""Collecte minimale de liens sur des pages publiques déclarées dans sources.csv."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SOURCE_FILE = ROOT / "sources.csv"
DATA_DIR = ROOT / "data"
ANNOUNCEMENTS = DATA_DIR / "annonces.jsonl"
ERRORS = DATA_DIR / "erreurs.jsonl"
KEYWORDS = (
    "adjudication", "adjudication", "enchere", "enchères", "enchere judiciaire",
    "vente judiciaire", "vente aux encheres", "vente aux enchères", "surenchere",
    "surenchère", "tribunal judiciaire", "audience d'adjudication",
)
MAX_BYTES = 10_000_000
TIMEOUT_SECONDS = 20

# Chemins observés sur les pages sources. Les sources inconnues conservent
# la recherche par mots-clés; ces règles écartent les menus et abonnements.
SOURCE_PATTERNS = {
    "licitor.com": r"^/ventes-judiciaires-immobilieres/",
    "avoventes.fr": r"^/enchere/",
    "vench.fr": r"^/vente-\d+-",
    "encheres-publiques.com": r"^/(?:fr/)?(?:encheres/immobilier/[^/]+/[^/]+/[^/]+_\d+|evenements/[^/]+/[^/]+_\d+)$",
    "info-encheres.com": r"^/\d+-d-",
    "petitesaffiches.fr": r"^/encheres-immobilieres/vente/immobiliere/",
    "lagazettefrance.fr": r"^/annonce-legale/",
    "informateurjudiciaire.fr": r"^/annonces-legales/[^/]+/",
    "7jours.fr": r"^/annonces-legales/[^/]+/",
    "echos-judiciaires.com": r"^/annonces-legales/[^/]+/",
    "vie-economique.com": r"^/annonces-legales/[^/]+/",
    "defis-avocats.com": r"^/vente(?:-judiciaire|-aux-encheres|-du)-",
    "elige-avocats.com": r"^/project/",
    "ahbl-avocats.fr": r"^/ventes-aux-encheres-immobilieres/[^/]+",
    "lca-avocats.fr": r"^/(?:ventes-annonces|ventes-resultats)/[^/]+",
    "legalyon.fr": r"^/fr/post/vente-aux-encheres/",
    "uda-avocats.com": r"^/nos-saisies/",
}


def is_candidate_link(source_url: str, href: str, text: str) -> bool:
    source_host = (urlparse(source_url).hostname or "").removeprefix("www.")
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        return False
    if source_host in SOURCE_PATTERNS:
        link_host = (parsed.hostname or "").removeprefix("www.")
        return link_host == source_host and bool(re.search(SOURCE_PATTERNS[source_host], parsed.path))
    searchable = (text + " " + href).casefold()
    return any(keyword.casefold() in searchable for keyword in KEYWORDS)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        # Avoventes utilise des cartes cliquables plutôt que des balises a.
        if attributes.get("data-link"):
            self.links.append({"href": attributes["data-link"].strip(), "text": ""})
        if tag.lower() == "a":
            self._href = attributes.get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append({"href": self._href.strip(), "text": " ".join(" ".join(self._text).split())})
            self._href = ""
            self._text = []


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def existing_hashes(path: Path) -> set[str]:
    found: set[str] = set()
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    found.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return found


def load_source_database() -> list[dict[str, str]]:
    if not SOURCE_FILE.exists():
        raise FileNotFoundError(f"Fichier absent : {SOURCE_FILE}")
    with SOURCE_FILE.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    sources = []
    for row in rows:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        if urlparse(url).scheme not in {"http", "https"}:
            print(f"Source ignorée, URL invalide : {url}", file=sys.stderr)
            continue
        sources.append({
            "nom": (row.get("nom") or url).strip(),
            "categorie": (row.get("categorie") or "à classer").strip(),
            "url": url,
            "active": (row.get("active") or "").strip().lower() in {"1", "oui", "true", "yes"},
            "usage": (row.get("usage") or "veille_quotidienne").strip(),
            "motif": (row.get("motif") or "").strip(),
        })
    return sources


def load_sources() -> list[dict[str, str]]:
    """Compatibilité avec le collecteur initial : retourne les lignes actives."""
    return [source for source in load_source_database() if source["active"]]


def collect(source: dict[str, str], known: set[str]) -> tuple[int, int]:
    request = Request(source["url"], headers={"User-Agent": "AdjudicationsFranceMonitor/0.1 (public page monitor)"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            page_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                raise ValueError(f"Type de contenu non HTML : {content_type}")
            page = response.read(MAX_BYTES + 1)
            if len(page) > MAX_BYTES:
                raise ValueError(f"Page supérieure à la limite de {MAX_BYTES} octets")
            encoding = response.headers.get_content_charset() or "utf-8"
            html = page.decode(encoding, errors="replace")
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        append_jsonl(ERRORS, {"date_utc": datetime.now(timezone.utc).isoformat(), "source": source, "erreur": str(exc)})
        return 0, 1

    parser = LinkParser()
    parser.feed(html)
    created = 0
    for link in parser.links:
        href = urldefrag(urljoin(page_url, link["href"]))[0]
        if not is_candidate_link(source["url"], href, link["text"]):
            continue
        digest = hashlib.sha256(href.encode("utf-8")).hexdigest()
        if digest in known:
            continue
        append_jsonl(ANNOUNCEMENTS, {
            "id": digest,
            "date_detection_utc": datetime.now(timezone.utc).isoformat(),
            "source": source["nom"],
            "categorie_source": source["categorie"],
            "page_source": source["url"],
            "titre_lien": link["text"] or urlparse(href).path.rsplit("/", 1)[-1],
            "url_annonce": href,
            "statut": "à vérifier manuellement",
        })
        known.add(digest)
        created += 1
    return created, 0


def main() -> int:
    sources = load_sources()
    if not sources:
        print("Aucune source active dans sources.csv. Aucun site n'a été consulté.")
        return 0
    known = existing_hashes(ANNOUNCEMENTS)
    added = errors = 0
    for index, source in enumerate(sources):
        count, failed = collect(source, known)
        added += count
        errors += failed
        print(f"{source['nom']} : {count} nouveau(x) lien(s), {failed} erreur(s)")
        if index < len(sources) - 1:
            time.sleep(2)
    print(f"Terminé : {added} nouveau(x) lien(s), {errors} source(s) en erreur.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
