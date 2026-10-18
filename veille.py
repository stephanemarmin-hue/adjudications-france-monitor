#!/usr/bin/env python3
"""Veille bornée des pages publiques, qualification et rapports auditables."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urldefrag, urljoin, urlparse
from monitor import is_candidate_link, load_sources
from acces import AccessDeferred, Fetcher, PdfDocument, incident
from pieces import PdfReader
from qualification import PARIS, amount, classify, french_date, labelled_amount, labelled_date, normalize, occupancy, works

ROOT = Path(__file__).resolve().parent
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
IGNORED = {"script", "style", "nav", "footer", "form", "noscript"}


def is_pdf_url(url):
    parsed = urlparse(url)
    return parsed.path.lower().endswith(".pdf") or parse_qs(parsed.query).get("justify") == ["1"]


class Node:
    def __init__(self, tag="root", attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, attrs or {}, parent
        self.children: list[Node | str] = []

    def all(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.all()

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()

    def text(self):
        if self.tag in IGNORED:
            return ""
        return " ".join(" ".join(child.text() if isinstance(child, Node) else child for child in self.children).split())

    def closest(self, predicate):
        node = self
        while node:
            if predicate(node):
                return node
            node = node.parent
        return None


class Document(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(markup)
        self.close()
        self.nodes = list(self.root.all())

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def find(self, predicate):
        return next((node for node in self.nodes if predicate(node)), None)


def canonical(url, base=""):
    parsed = urldefrag(urljoin(base, url.strip()))[0]
    return parsed if urlparse(parsed).scheme in {"http", "https"} else ""


def digest(url):
    return hashlib.sha256(url.encode()).hexdigest()


def parsed_stamp(value):
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def source_due(source, attempts, now, interval_hours):
    previous = parsed_stamp(attempts.get(source["url"], ""))
    return previous is None or now - previous >= timedelta(hours=interval_hours)


def network_order(sources):
    """Entrelace les domaines sans faire passer une source secondaire avant la veille directe."""
    ordered = []
    usages = ("veille_quotidienne", "veille_rapide", "decouverte_nationale", "veille_decouverte")
    remaining = list(sources)
    for usage in usages + (None,):
        group = ([source for source in remaining if (source.get("usage") or "veille_quotidienne") == usage]
                 if usage is not None else remaining)
        if usage is not None:
            selected_ids = {id(source) for source in group}
            remaining = [source for source in remaining if id(source) not in selected_ids]
        queues = {}
        for source in group:
            host = (urlparse(source["url"]).hostname or source["url"]).lower().removeprefix("www.")
            queues.setdefault(host, []).append(source)
        while queues:
            for host in list(queues):
                ordered.append(queues[host].pop(0))
                if not queues[host]:
                    del queues[host]
    return ordered


def network_order_urls(urls):
    """Entrelace des URL déjà prioritaires pour utiliser les domaines en parallèle."""
    queues = {}
    for url in urls:
        host = (urlparse(url).hostname or url).lower().removeprefix("www.")
        queues.setdefault(host, []).append(url)
    ordered = []
    while queues:
        for host in list(queues):
            ordered.append(queues[host].pop(0))
            if not queues[host]:
                del queues[host]
    return ordered


def count_detail_for_selected_sources(log_by_url, sources, detail_read):
    """Compte la fiche seulement pour les sources effectivement contrôlées ce passage."""
    for source in sources:
        log = log_by_url.get(source.get("url"))
        if log is not None:
            log["fiches_lues"] += bool(detail_read)


def select_sources(database, state, now, *, max_profiles=700, max_directories=160,
                   max_discovered=100, profile_interval_hours=5,
                   directory_interval_hours=23, discovered_interval_hours=24):
    """Sélectionne toute la veille directe et fait tourner les deux grands référentiels."""
    attempts = state.setdefault("tentatives_sources", {})
    active = [source for source in database if source.get("active", True)]
    daily, profiles, directories = [], [], []
    for source in active:
        usage = source.get("usage") or "veille_quotidienne"
        if usage == "veille_rapide":
            profiles.append(source)
        elif usage == "decouverte_nationale":
            directories.append(source)
        else:
            daily.append(source)
    remembered = []
    for url, item in state.get("sources_decouvertes", {}).items():
        if not canonical(url) or any(source["url"] == url for source in active):
            continue
        remembered.append({"nom": item.get("nom") or url, "categorie": "source_decouverte",
                           "url": url, "active": True, "usage": "veille_decouverte",
                           "motif": f"Découverte depuis {item.get('provenance', 'une source surveillée')}"})

    def rotating(rows, interval, maximum):
        due = [row for row in rows if source_due(row, attempts, now, interval)]
        due.sort(key=lambda row: (attempts.get(row["url"], ""), row["url"]))
        return due[:maximum], len(due)

    chosen_profiles, due_profiles = rotating(profiles, profile_interval_hours, max_profiles)
    chosen_directories, due_directories = rotating(directories, directory_interval_hours, max_directories)
    chosen_discovered, due_discovered = rotating(remembered, discovered_interval_hours, max_discovered)
    selected = daily + chosen_profiles + chosen_directories + chosen_discovered
    selected = list({source["url"]: source for source in selected}.values())
    stats = {
        "sources_base_total": len(database), "sources_actives_total": len(active),
        "veille_quotidienne_total": len(daily), "veille_rapide_total": len(profiles),
        "decouverte_nationale_total": len(directories), "sources_decouvertes_total": len(remembered),
        "veille_quotidienne_selectionnees": len(daily),
        "veille_rapide_selectionnees": len(chosen_profiles),
        "decouverte_nationale_selectionnees": len(chosen_directories),
        "sources_decouvertes_selectionnees": len(chosen_discovered),
        "veille_rapide_en_retard": max(0, due_profiles - len(chosen_profiles)),
        "decouverte_nationale_en_attente": max(0, due_directories - len(chosen_directories)),
        "sources_decouvertes_en_retard": max(0, due_discovered - len(chosen_discovered)),
        "cycle_decouverte_passages": math.ceil(len(directories) / max_directories) if max_directories else None,
    }
    return selected, stats


DISCOVERY_TERMS = (
    "adjudication", "enchere immobiliere", "encheres immobilieres", "vente judiciaire",
    "ventes judiciaires", "vente aux encheres", "saisie immobiliere",
)
GENERIC_SITE_LABELS = {"site internet", "site web", "acceder au site", "voir le site", "website"}
BLOCKED_DISCOVERY_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
    "google.com", "google.fr", "goo.gl", "maps.google.com",
}


def discover_source_links(markup, page_url, source, timestamp):
    """Repère les rubriques de ventes et les sites externes indiqués par les annuaires/profils."""
    document = Document(markup)
    page_host = (urlparse(page_url).hostname or "").removeprefix("www.")
    found = {}
    for node in document.nodes:
        if node.tag != "a" or not node.attrs.get("href"):
            continue
        url = canonical(node.attrs["href"], page_url)
        parsed = urlparse(url)
        host = (parsed.hostname or "").removeprefix("www.")
        if not url or not host or host == page_host or host in BLOCKED_DISCOVERY_HOSTS:
            continue
        if parsed.path.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".zip")):
            continue
        label = normalize(node.text()).strip(" .:;-")
        searchable = normalize(f"{label} {parsed.path} {parsed.query}")
        explicit_sale_page = any(term in searchable for term in DISCOVERY_TERMS)
        directory_or_profile = source.get("usage") in {"veille_rapide", "decouverte_nationale"}
        generic_site = directory_or_profile and (label in GENERIC_SITE_LABELS or label.startswith("site internet"))
        if not explicit_sale_page and not generic_site:
            continue
        found[url] = {
            "nom": node.text().strip() or f"Site découvert — {host}", "url": url,
            "provenance": source["url"], "premiere_detection_utc": timestamp,
            "derniere_detection_utc": timestamp,
        }
    return list(found.values())


def wilson_lower(successes, total, z=1.6448536269514722):
    """Borne basse unilatérale à 95 %, utilisée seulement sur un échantillon indépendant."""
    if total <= 0:
        return None
    ratio = successes / total
    denominator = 1 + z * z / total
    centre = ratio + z * z / (2 * total)
    margin = z * math.sqrt((ratio * (1 - ratio) + z * z / (4 * total)) / total)
    return (centre - margin) / denominator


def coverage_audit(path, known_urls):
    """Mesure la détection contre un échantillon externe, sans inventer de pourcentage."""
    if not path.exists():
        return {"statut": "non_mesure", "motif": "fichier de contrôle indépendant absent",
                "echantillon": 0, "detectees": 0, "taux_pct": None, "borne_basse_95_pct": None,
                "objectif_95": False}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    eligible = [row for row in rows if normalize(row.get("statut", "")) not in {"hors perimetre", "hors_perimetre"}
                and (row.get("url_reference") or row.get("detectee"))]
    successes = 0
    for row in eligible:
        declared = normalize(row.get("detectee", "")) in {"oui", "true", "1", "detectee"}
        candidate_urls = {canonical(row.get("url_reference", "")), canonical(row.get("url_detectee", ""))} - {""}
        successes += bool(declared or candidate_urls.intersection(known_urls))
    total = len(eligible)
    lower = wilson_lower(successes, total)
    return {
        "statut": "mesure" if total else "non_mesure",
        "motif": "échantillon externe indépendant" if total else "échantillon externe vide",
        "echantillon": total, "detectees": successes,
        "taux_pct": round(successes * 100 / total, 2) if total else None,
        "borne_basse_95_pct": round(lower * 100, 2) if lower is not None else None,
        "objectif_95": bool(total >= 500 and lower is not None and lower >= .95),
    }


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def facts_from_text(text, host, *, detail=False, occupancy_labels=()):
    normalized = normalize(text)
    facts = {
        "date_vente": labelled_date(text, r"(?:date de (?:la )?vente|date de l'audience|vente(?: aux encheres(?: publiques)?| sur (?:liquidation judiciaire|licitation|saisie immobiliere|surenchere))?(?: du| le)?|audience)"),
        "date_limite_surenchere": labelled_date(text, r"surenchere possible jusqu'au"),
        "prix_adjuge_eur": labelled_amount(text, r"(?:adjuge|adjudication)"),
        "mise_a_prix_eur": labelled_amount(text, r"mise a prix(?: initiale)?"),
        "frais_prealables_publies_eur": labelled_amount(text, r"frais pre(?:a)?lables"),
        "occupation": "inconnue", "travaux": "a_verifier",
        "retiree": bool(re.search(r"\b(?:retiree|non requise|vente reportee|vente annulee)\b", normalized)),
        "seconde_adjudication": bool(re.search(r"\b(?:vente sur surenchere|adjudication sur surenchere|seconde adjudication)\b", normalized)),
        "surenchere_impossible": "aucune surenchere possible" in normalized or "surenchere impossible" in normalized,
        "vente_amiable": "vente amiable" in normalized,
    }
    surface = re.search(r"\b(\d+(?:[.,]\d+)?)\s*m[²2]\b", text)
    if surface:
        facts["surface_m2"] = surface.group(1).replace(",", ".")
    if detail:
        facts["occupation"], facts["preuve_occupation"] = occupancy(text, labels=occupancy_labels)
        facts["travaux"], facts["preuve_travaux"] = works(text)
    # Ne pas prendre les prix de DVF, les mises à prix ou la date de publication
    # pour le prix/date de l'adjudication du dossier.
    return {key: value for key, value in facts.items() if value is not None}


def seed_records(markup, page_url, source, timestamp):
    doc = Document(markup)
    host = (urlparse(page_url).hostname or "").removeprefix("www.")
    records = {}
    for node in doc.nodes:
        href = node.attrs.get("data-link") or (node.attrs.get("href") if node.tag == "a" else "")
        if not href:
            continue
        url = canonical(href or "", page_url)
        if not url or not is_candidate_link(page_url, url, node.text()):
            continue
        card = node if node.attrs.get("data-link") else node.closest(
            lambda x: x.has_class("featured-item") or x.tag == "article" or x.tag == "li"
        ) or node
        text = card.text()
        title = node.text() or urlparse(url).path.rsplit("/", 1)[-1]
        if node.attrs.get("data-link"):
            heading = next((x for x in node.all() if x.has_class("font-bold") and x.has_class("text-16")), None)
            title = heading.text() if heading else title
        facts = facts_from_text(text, host)
        if facts.get("date_vente"):
            facts["date_vente_source"] = page_url
        if host == "licitor.com":
            inferred_date = french_date(urlparse(url).path.replace("-", " "))
            if inferred_date:
                facts["date_vente"] = inferred_date.isoformat()
        address = ""
        if host == "avoventes.fr":
            # L'adresse est affichée entre le titre et la mise à prix sur la carte.
            before_price = text.split("Mise à prix", 1)[0]
            if title in before_price:
                address = before_price.split(title, 1)[1].strip()
        record = {"id": digest(url), "url_annonce": url, "titre": title[:350],
                  "adresse": address, "sources": [source], "faits": facts,
                  "controle_liste_utc": timestamp, "detail_lu_ce_passage": False}
        if url not in records or len(text) > records[url].get("_text_length", 0):
            record["_text_length"] = len(text)
            records[url] = record
    for record in records.values():
        record.pop("_text_length", None)
    return records, doc


def pagination_pages(markup, page_url):
    """Trouve uniquement le lien explicite vers la page suivante d'une liste."""
    doc = Document(markup)
    current = urlparse(page_url)
    pages = set()
    labels = {"suivant", "suivante", "page suivante", "next", ">", ">>", "›", "»"}
    for node in doc.nodes:
        if node.tag != "a" or not node.attrs.get("href"):
            continue
        url = canonical(node.attrs["href"], page_url)
        parsed = urlparse(url)
        if not url or parsed.netloc != current.netloc or url == canonical(page_url, page_url):
            continue
        rel = normalize(node.attrs.get("rel", ""))
        label = normalize(node.text()).strip()
        if "next" in rel.split() or label in labels:
            pages.add(url)
    return sorted(pages)


def hearing_records(markup, page_url, source, timestamp):
    doc = Document(markup)
    container = doc.find(lambda x: x.attrs.get("id") == "hearings-list")
    if not container:
        raise ValueError("structure Licitor : bloc hearings-list absent")
    header = next((n for n in container.all() if n.tag == "h1"), None)
    sale_date = french_date(header.text()) if header else None
    records, pages = {}, set()
    for node in container.all():
        if node.tag != "a" or not node.attrs.get("href"):
            continue
        url = canonical(node.attrs["href"], page_url)
        if "/annonce/" in urlparse(url).path:
            text = node.text()
            facts = facts_from_text(text, "licitor.com")
            if sale_date:
                facts["date_vente"] = sale_date.isoformat()
                facts["date_vente_source"] = page_url
                # Le libellé du résultat de cette audience comprend sa date.
                result = re.search(r"\b\d{2}-\d{2}-\d{4}\s*:\s*([\d\s.,]+)\s*€", text)
                if result and amount(result.group(1)):
                    facts["prix_adjuge_eur"] = str(amount(result.group(1)))
            records[url] = {"id": digest(url), "url_annonce": url, "titre": text[:350],
                            "adresse": "", "sources": [source], "faits": facts,
                            "controle_liste_utc": timestamp, "detail_lu_ce_passage": False}
        elif urlparse(url).path == urlparse(page_url).path and "p=" in urlparse(url).query:
            pages.add(url)
    return records, sorted(pages)


def property_occupancy_labels(container, host):
    """Lire LIBRE/Occupé/Loué dans le bloc du lot, sans les filtres ni les liens."""
    scopes = {"licitor.com": lambda n: n.has_class("Lot"),
              "avoventes.fr": lambda n: n.has_class("text-content"),
              "vench.fr": lambda n: n.has_class("descriptionContener")}
    predicate = scopes.get(host)
    if predicate is None:
        return []
    labels = []
    for node in container.all():
        label = normalize(node.text()).strip(" .:;")
        if label not in {"libre", "occupe", "loue", "vacant"} or node.tag not in {"p", "div", "span", "b", "strong", "em"}:
            continue
        if not node.closest(predicate) or node.closest(lambda n: n.tag in IGNORED | {"a", "button", "select", "option", "aside"}):
            continue
        context = node.parent.closest(lambda n: n.tag in {"p", "div", "li"}) if node.parent else None
        labels.append((label, context.text() if context else node.text()))
    return labels


def parse_detail(markup, page_url, record):
    doc = Document(markup)
    host = (urlparse(page_url).hostname or "").removeprefix("www.")
    if host == "licitor.com":
        container = doc.find(lambda x: x.tag == "article" and x.has_class("LegalAd") and len(x.text()) > 50)
    elif host == "avoventes.fr":
        container = doc.find(lambda x: x.attrs.get("id") == "content")
    else:
        container = doc.find(lambda x: x.tag == "main") or doc.find(lambda x: x.tag == "body")
    if not container or len(container.text()) < 80:
        raise ValueError("structure de fiche non reconnue ou contenu vide")
    text = container.text()
    if host == "avoventes.fr":
        primary = text.split("Cadastre", 1)[0]
        extras = text.split("Informations complémentaires :", 1)
        if len(extras) == 2:
            primary += " " + extras[1].split("Documents", 1)[0]
        text = primary
    elif host == "licitor.com":
        text = text.split("Prix constatés", 1)[0]
    else:
        # Menus, formulaires, biens voisins et simulateurs de frais sont exclus
        # dès que le site fournit un bloc de description propre au dossier.
        description = doc.find(lambda x: x.has_class("descriptionContener"))
        if description:
            text = container.text().split("Signaler une erreur", 1)[0]
    result = dict(record)
    old_facts = record.get("faits", {})
    detail_facts = facts_from_text(text, host, detail=True,
                                  occupancy_labels=property_occupancy_labels(container, host))
    if detail_facts.get("date_vente"):
        detail_facts["date_vente_source"] = page_url
    # Les faits manquants restent inconnus; la liste du même passage peut
    # compléter une fiche qui n'affiche pas elle-même le résultat.
    result["faits"] = {**old_facts, **detail_facts}
    for key in ["retiree", "seconde_adjudication", "surenchere_impossible"]:
        result["faits"][key] = bool(old_facts.get(key) or detail_facts.get(key))
    result["detail_lu_ce_passage"] = True
    result["controle_detail_utc"] = datetime.now(timezone.utc).isoformat()
    result["erreur_detail"] = None
    result["niveau_preuve"] = "C — publication, documents non vérifiés automatiquement"
    detected_documents = {canonical(n.attrs["href"], page_url) for n in container.all()
                          if n.tag == "a" and n.attrs.get("href")
                          and is_pdf_url(canonical(n.attrs["href"], page_url))}
    result["documents"] = sorted((set(record.get("documents", [])) | detected_documents) - {""})
    result["documents_controles_automatiquement"] = False
    headings = [n.text() for n in container.all() if n.tag in {"h1", "h2"} and n.text()]
    if headings:
        result["titre"] = headings[0][:350]
    if host == "licitor.com":
        location = doc.find(lambda x: x.has_class("Address"))
        if location:
            result["adresse"] = location.text()
    # Pour la métropole, exclure les codes ultramarins explicites dans l'adresse.
    if re.search(r"\b(?:97[0-9]{3}|98[0-9]{3})\b", result.get("adresse", "")):
        result["faits"]["hors_metropole"] = True
    result["extrait_dossier"] = text[:6500]
    result["empreinte_contenu"] = hashlib.sha256(text.encode()).hexdigest()
    if host == "vench.fr" and "devez etre abonne" in normalize(text):
        result["restriction_acces"] = "description complète réservée aux abonnés"
    return result


def merge_records(target, incoming):
    for url, record in incoming.items():
        if url in target:
            sources = target[url]["sources"] + record["sources"]
            record["sources"] = list({s["url"]: s for s in sources}.values())
            # Préférer une valeur explicite à un champ non renseigné.
            previous_facts = target[url]["faits"]
            record["faits"] = {**previous_facts, **record["faits"]}
            for key in ["retiree", "seconde_adjudication", "surenchere_impossible"]:
                record["faits"][key] = bool(previous_facts.get(key) or record["faits"].get(key))
            record["adresse"] = record.get("adresse") or target[url].get("adresse", "")
        target[url] = record


def meaningful(value):
    return value not in (None, "", [], {})


def track_record(previous, current, stamp):
    """Conserver le dossier cumulatif et journaliser chaque information nouvelle."""
    result = dict(current)
    result["documents"] = sorted(set(previous.get("documents", [])) | set(current.get("documents", [])))
    changes = []
    previous_facts, current_facts = previous.get("faits", {}), result.get("faits", {})
    for key in sorted(set(previous_facts) | set(current_facts)):
        old, new = previous_facts.get(key), current_facts.get(key)
        if meaningful(new) and old != new:
            changes.append({"champ": f"faits.{key}", "ancienne_valeur": old, "nouvelle_valeur": new})
    for key in ["titre", "adresse"]:
        old, new = previous.get(key), result.get(key)
        if meaningful(new) and old != new:
            changes.append({"champ": key, "ancienne_valeur": old, "nouvelle_valeur": new})
    new_documents = sorted(set(result["documents"]) - set(previous.get("documents", [])))
    if new_documents:
        changes.append({"champ": "documents", "ancienne_valeur": None, "nouvelle_valeur": new_documents})
    history = list(previous.get("historique_mises_a_jour", []))
    if not previous or changes:
        history.append({"controle_utc": stamp, "type": "premiere_detection" if not previous else "mise_a_jour",
                        "nouveaux_elements": changes})
    result["historique_mises_a_jour"] = history
    result["nouveaux_elements_ce_passage"] = changes
    return result


def load_valuations(path):
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    return {canonical(row.get("url_annonce", "")): row for row in rows if row.get("url_annonce")}


def money(value):
    parsed = amount(value)
    return (f"{parsed:,.2f}".replace(",", " ") + " €") if parsed is not None else "À calculer — données manquantes"


def document_history(records, cache, current_urls):
    """Les indices restent consultables, sans faire passer une lecture ancienne pour une nouvelle."""
    history = {}
    for record in records.values():
        record["preuves_documentaires"] = []
        record["documents_extraits"] = []
        for url in record.get("documents", []):
            doc = cache.get(url, {})
            if not doc.get("empreinte_pdf"):
                continue
            current = url in current_urls
            proofs = [{**p, "controle_acces_utc": doc.get("controle_acces_utc"),
                       "controle_extraction_utc": doc.get("extractions", {}).get(str(p["page"]), {}).get("controle_extraction_utc"),
                       "consulte_ce_passage": current} for p in doc.get("preuves", [])]
            item = {key: doc.get(key) for key in ["url", "statut", "pages_lues", "pages_total", "pages_restantes",
                                                 "controle_acces_utc", "faits_candidats", "contradictions"]}
            item.update(preuves=proofs, consulte_ce_passage=current, validation="a_verifier",
                        derniere_erreur=doc.get("derniere_erreur"))
            history[url] = item
            record["preuves_documentaires"].extend(proofs)
            record["documents_extraits"].append({key: item[key] for key in ["url", "statut", "pages_lues", "pages_total",
                                                                         "controle_acces_utc", "consulte_ce_passage", "validation"]})
    return list(history.values())


def cell(value):
    return str("À vérifier" if value is None or value == "" else value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def source_interventions(state, logs, timestamp):
    """Mémorise la santé des sources et ne demande une action qu'en cas de blocage utile."""
    health = state.setdefault("sante_sources", {})
    # Une partie de la base tourne par lots. Conserver la santé des sources non
    # sélectionnées aujourd'hui évite de perdre leur historique entre deux lots.
    interventions = []
    grouped_references = {}
    for log in logs:
        url = log["url"]
        previous = health.get(url, {})
        failures = int(previous.get("echecs_consecutifs", 0))
        empty = int(previous.get("sans_lien_consecutifs", 0))
        ever_productive = bool(previous.get("a_deja_fourni_des_liens")) or bool(log.get("liens_detectes"))
        status = log.get("statut", "")
        if status == "ok":
            failures, empty = 0, 0
        elif status.startswith("aucun lien"):
            failures, empty = 0, empty + 1
        else:
            failures, empty = failures + 1, 0
        current = {
            "nom": log["nom"], "url": url, "dernier_statut": status,
            "derniere_erreur": log.get("erreur", ""), "dernier_controle_utc": timestamp,
            "echecs_consecutifs": failures, "sans_lien_consecutifs": empty,
            "a_deja_fourni_des_liens": ever_productive,
        }
        if status == "ok" and log.get("liens_detectes"):
            current["derniere_reussite_utc"] = timestamp
        elif previous.get("derniere_reussite_utc"):
            current["derniere_reussite_utc"] = previous["derniere_reussite_utc"]
        health[url] = current

        error = normalize(log.get("erreur", ""))
        permanent = any(marker in error for marker in ("http error 404", "name or service not known", "no such host", "getaddrinfo failed"))
        requires_help = permanent or failures >= 3 or (empty >= 7 and ever_productive)
        if not requires_help:
            continue
        if "404" in error:
            problem = "La page surveillée n'existe plus (HTTP 404)."
            action = "Ouvrir le lien, retrouver la nouvelle rubrique de ventes et transmettre sa nouvelle adresse."
        elif any(marker in error for marker in ("name or service not known", "no such host", "getaddrinfo failed")):
            problem = "Le nom de domaine ne répond plus."
            action = "Vérifier si le site a changé d'adresse et transmettre le nouveau lien."
        elif status.startswith("aucun lien"):
            problem = f"Aucune annonce détectée depuis {empty} passages consécutifs alors que cette source en fournissait."
            action = "Ouvrir la page et vérifier si la rubrique a changé ou si elle ne publie simplement aucune vente."
        elif "403" in error:
            problem = f"Accès refusé ou différé depuis {failures} passages consécutifs."
            action = "Ouvrir le lien dans le navigateur, accepter les éventuels cookies ou confirmer si une connexion est requise."
        else:
            problem = f"Lecture impossible ou partielle depuis {failures} passages consécutifs : {log.get('erreur') or status}."
            action = "Ouvrir le lien et confirmer s'il fonctionne normalement dans le navigateur."
        item = {
            "priorite": "urgente" if permanent else "à vérifier",
            "source": log["nom"], "url": url, "statut": status,
            "passages_consecutifs": max(failures, empty), "probleme": problem, "action": action,
        }
        if log.get("usage") in {"veille_rapide", "decouverte_nationale"}:
            host = (urlparse(url).hostname or url).removeprefix("www.")
            group = grouped_references.setdefault(host, {**item, "source": f"Référentiel — {host}",
                                                          "nombre_sources": 0, "exemples": []})
            group["nombre_sources"] += 1
            group["passages_consecutifs"] = max(group["passages_consecutifs"], item["passages_consecutifs"])
            group["exemples"].append(url)
            if item["priorite"] == "urgente":
                group["priorite"] = "urgente"
        else:
            interventions.append(item)
    for host, item in grouped_references.items():
        count = item["nombre_sources"]
        item["probleme"] = (f"{count} adresse(s) du référentiel {host} présentent un blocage persistant. "
                            f"Exemple : {item['probleme']}")
        item["action"] = ("Ouvrir l'exemple, vérifier si le domaine a changé sa structure ou exige une connexion, "
                          "puis transmettre la nouvelle rubrique nationale si elle existe.")
        item["url"] = item["exemples"][0]
        del item["exemples"]
        interventions.append(item)
    return sorted(interventions, key=lambda item: (item["priorite"] != "urgente", item["source"]))


def save_report(folder, report):
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / "dernier.json", report)
    rows = report["dossiers"]
    columns = ["id", "statut", "date_detection", "adresse", "titre", "date_vente", "jours_avant_enchere",
               "occupation", "mise_a_prix_eur", "motifs_rejet", "a_verifier", "nouveaux_elements",
               "pieces_dossier", "url_annonce"]
    with (folder / "dernier.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            output = {key: row.get(key, "") for key in columns}
            output.update({key: row["faits"].get(key, "") for key in ["date_vente", "occupation", "mise_a_prix_eur"]})
            for key in ["motifs_rejet", "a_verifier"]:
                output[key] = "; ".join(row[key])
            output["nouveaux_elements"] = json.dumps(row.get("nouveaux_elements_ce_passage", []), ensure_ascii=False)
            output["pieces_dossier"] = "; ".join(row.get("documents", []))
            writer.writerow(output)
    with (folder / "sources.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        cols = ["nom", "categorie", "usage", "url", "statut", "liens_detectes", "pages_listes_lues",
                "pages_audiences_lues", "fiches_lues", "erreur", "controle_utc"]
        writer = csv.DictWriter(stream, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["sources"])
    count = report["comptages"]
    lines = ["# Veille adjudications — nouveautés du jour", "", f"Contrôle : **{report['controle_paris']}** (Europe/Paris).",
             "", f"**{count['dossiers_du_jour']} dossier(s) détecté(s) ou enrichi(s) aujourd'hui** : "
             f"{count['retenus']} bien(s) libre(s) confirmé(s), {count['a_verifier']} à vérifier et "
             f"{count['rejetes']} rejet(s).", "",
             f"Le moteur suit {count.get('dossiers_suivis_observes', 0)} dossier(s), quelle que soit la date de l'enchère ; "
             "seuls ceux détectés ou enrichis pendant ce passage figurent ci-dessous. L'historique complet reste conservé dans le catalogue.", "",
             f"{count.get('sources_accessibles', count['sources_ok'])}/{count['sources_actives']} pages sources accessibles ; "
             f"{count['liens_distincts']} liens distincts ; "
             f"{count['audiences_lues']} pages d'audience et {count['fiches_lues']} fiches détaillées lues.", "",
             f"Base nationale : **{count.get('sources_base_total', count['sources_actives'])} adresses actives** ; "
             f"{count.get('veille_quotidienne_selectionnees', 0)} pages directes, "
             f"{count.get('veille_rapide_selectionnees', 0)} profils et "
             f"{count.get('decouverte_nationale_selectionnees', 0)} annuaires contrôlés pendant ce passage. "
             f"{count.get('sources_decouvertes_total', 0)} nouvelle(s) source(s) mémorisée(s).", "",
             f"Nouveautés : {count['nouveaux_liens']} liens ; {count.get('fiches_nouvellement_lues', 0)} fiches lues pour la première fois ; "
             f"{count['fiches_modifiees']} fiches modifiées depuis le passage précédent.", "",
             f"Pièces : {count.get('documents_pdf_consultes', 0)} PDF accessibles ; {count.get('pages_pdf_traitees', 0)} pages traitées ce passage, "
             f"{count.get('pages_pdf_extraites', 0)} lues, dont {count.get('pages_ocr', 0)} par OCR. "
             f"{count.get('acces_differes', 0)} requêtes différées après une limite ou un refus d'accès.", "",
             f"Interventions : **{count.get('interventions_requises', 0)} demande(s)** nécessitent une vérification humaine.", "",
             "Le nombre de liens inclut des audiences et des annonces anciennes ou futures. La couverture est limitée aux pages listées et au budget de lecture indiqué ci-dessous.", "",
             "## Mesure indépendante de la fiabilité", ""]
    audit = report.get("controle_couverture", {})
    if audit.get("statut") == "mesure":
        lines += [f"Échantillon externe : **{audit['detectees']}/{audit['echantillon']} détectées** "
                  f"({audit['taux_pct']} % ; borne basse unilatérale à 95 % : {audit['borne_basse_95_pct']} %).",
                  "", ("Objectif de fiabilité 95 % démontré." if audit.get("objectif_95") else
                       "Objectif de fiabilité 95 % non encore démontré : il faut au moins 500 annonces indépendantes et une borne basse d'au moins 95 %."), ""]
    else:
        lines += ["Aucun pourcentage de fiabilité n'est annoncé : l'échantillon externe indépendant est encore vide. "
                  "Le moteur publiera la mesure dès que ce fichier de contrôle sera alimenté.", ""]
    lines += [
             "## Interventions requises", "",
             "| Priorité | Source | Problème | Action demandée |", "|---|---|---|---|"]
    for item in report.get("interventions", []):
        lines.append("| " + " | ".join(cell(value) for value in [item["priorite"], f"[{item['source']}]({item['url']})",
                     item["probleme"], item["action"]]) + " |")
    if not report.get("interventions"):
        lines += ["", "Aucune intervention requise."]
    lines += ["",
             "## Dossiers détectés ou enrichis aujourd'hui", "",
             "| Bien | Statut | Mise à prix | Date de l'enchère | Occupation | Nouveaux éléments | Pièces du dossier | Source |",
             "|---|---|---|---|---|---|---|---|"]
    labels = {"retenu_sur_donnees_validees": "Libre confirmé", "a_verifier": "À vérifier", "rejete": "Rejeté"}
    for row in rows:
        facts = row["faits"]
        starting_price = money(facts.get("mise_a_prix_eur")) if facts.get("mise_a_prix_eur") not in {None, ""} else "Non publiée"
        changes = "; ".join(f"{item['champ']} : {item.get('nouvelle_valeur')}"
                            for item in row.get("nouveaux_elements_ce_passage", [])) or "Nouvelle détection"
        pieces = "; ".join(f"[Pièce {index}]({url})" for index, url in enumerate(row.get("documents", []), 1)) or "Aucune pièce détectée"
        values = [row.get("adresse") or row["titre"], labels.get(row["statut"], row["statut"]), starting_price,
                  facts.get("date_vente") or "Non publiée", facts.get("occupation", "inconnue"), changes,
                  pieces, f"[Annonce]({row['url_annonce']})"]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    if not rows:
        lines += ["", "Aucun dossier détecté ou enrichi aujourd'hui."]
    lines += ["", "## Sources productives ce passage", "", "| Source | Accès | Liens | Listes | Audiences | Fiches | Incident |", "|---|---|---|---|---|---|---|"]
    productive_sources = [source for source in report["sources"] if source.get("liens_detectes")
                          or source.get("pages_audiences_lues") or source.get("fiches_lues")]
    for source in productive_sources:
        lines.append("| " + " | ".join(cell(x) for x in [f"[{source['nom']}]({source['url']})", source["statut"], source["liens_detectes"],
                     source.get("pages_listes_lues", 0), source["pages_audiences_lues"], source["fiches_lues"], source.get("erreur") or "—"]) + " |")
    if not productive_sources:
        lines += ["", "Aucune source productive pendant ce passage."]
    daily_urls = {row["url_annonce"] for row in rows}
    daily_documents = [doc for doc in report.get("documents", []) if daily_urls.intersection(doc.get("annonces", []))]
    if daily_documents:
        lines += ["", "## Pièces PDF liées aux dossiers du jour", "", "Les indices ci-dessous restent à vérifier.", "",
                  "| Pièce | Lecture | Pages lues | Indices et pages | Incident |", "|---|---|---|---|---|"]
    for doc in daily_documents:
        extracts = "; ".join(f"{p['champ']} : {p['valeur']} (p. {p['page']}, {p['methode']})" for p in doc.get("preuves", [])[:5])
        if doc.get("contradictions"):
            extracts += "; mentions multiples à contrôler : " + ", ".join(doc["contradictions"])
        lines.append("| " + " | ".join(cell(x) for x in [f"[Pièce]({doc['url']})", doc["statut"],
                     f"{len(doc.get('pages_lues', []))}/{doc.get('pages_total', '?')}",
                     extracts or "Aucun indice ciblé extrait", doc.get("erreur") or "; ".join(x["erreur"] for x in doc.get("incidents_pages", [])) or "—"]) + " |")
    lines += ["", "## Couverture restante et contrôles", ""]
    lines += ["- " + warning for warning in report["limites"]]
    lines += ["", "Les pages PDF sont extraites dans les budgets indiqués, avec OCR pour les scans lorsque nécessaire. "
               "Les indices gardent la référence de page ; leur attribution au bien et leur actualité restent à contrôler. "
               "La sélection automatique exige une occupation libre confirmée et une mise à prix publiée inférieure ou égale à 60 000 €.", ""]
    (folder / "dernier.md").write_text("\n".join(lines), encoding="utf-8")
    alert_lines = ["# Interventions requises", "", f"Contrôle : **{report['controle_paris']}** (Europe/Paris).", ""]
    if report.get("interventions"):
        for index, item in enumerate(report["interventions"], 1):
            alert_lines += [f"## {index}. {item['source']} — {item['priorite']}", "", item["probleme"], "",
                            f"Action : {item['action']}", "", f"Lien direct : [{item['url']}]({item['url']})", ""]
    else:
        alert_lines.append("Aucune intervention requise.")
    (folder / "interventions.md").write_text("\n".join(alert_lines), encoding="utf-8")


def run(args):
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("--now doit préciser un fuseau horaire")
    stamp, today = now.isoformat(), now.astimezone(PARIS).date()
    fixture_map = json.loads(Path(args.fixtures).read_text(encoding="utf-8")) if args.fixtures else None
    state_path = Path(args.state)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"liens": {}, "fiches": {}}
    if state.get("version") not in {None, 1}:
        raise ValueError("version d'état non prise en charge")
    known_link_urls = set(state.get("liens", {}))
    previous_fiches = dict(state.get("fiches", {}))
    fetcher = Fetcher(fixture_map, pause=args.pause, access_state=state.setdefault("acces", {}))
    source_database = load_sources()
    max_profile_sources = getattr(args, "max_profile_sources", 700)
    max_directory_sources = getattr(args, "max_directory_sources", 160)
    max_discovered_sources = getattr(args, "max_discovered_sources", 100)
    profile_interval_hours = getattr(args, "profile_interval_hours", 6)
    directory_interval_hours = getattr(args, "directory_interval_hours", 23)
    discovered_interval_hours = getattr(args, "discovered_interval_hours", 24)
    sources, source_coverage = select_sources(
        source_database, state, now, max_profiles=max_profile_sources,
        max_directories=max_directory_sources, max_discovered=max_discovered_sources,
        profile_interval_hours=profile_interval_hours,
        directory_interval_hours=directory_interval_hours,
        discovered_interval_hours=discovered_interval_hours,
    )
    records, logs, incidents = {}, [], []
    max_listing_pages = getattr(args, "max_listing_pages", 3)
    max_profile_pages = getattr(args, "max_profile_pages", 2)
    max_discovery_pages = getattr(args, "max_discovery_pages", 1)

    def listing(source):
        log = {**source, "controle_utc": stamp, "statut": "ok", "liens_detectes": 0,
               "pages_listes_lues": 0, "pages_audiences_lues": 0, "fiches_lues": 0, "erreur": ""}
        combined, page_incidents, discoveries = {}, [], []
        pending, seen = [source["url"]], set()
        usage = source.get("usage") or "veille_quotidienne"
        page_budget = (max_profile_pages if usage == "veille_rapide" else
                       max_discovery_pages if usage == "decouverte_nationale" else
                       max_listing_pages)
        while pending and len(seen) < page_budget:
            url = pending.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                markup, resolved = fetcher.get(url)
                log["pages_listes_lues"] += 1
                incoming, _ = seed_records(markup, resolved, source, stamp)
                merge_records(combined, incoming)
                discoveries.extend(discover_source_links(markup, resolved, source, stamp))
                pending.extend(page for page in pagination_pages(markup, resolved) if page not in seen and page not in pending)
            except Exception as exc:
                item = incident(exc, url, "source" if url == source["url"] else "pagination_source")
                page_incidents.append(item)
                if url == source["url"] and not combined:
                    log["statut"] = "differe" if isinstance(exc, AccessDeferred) else "echec"
                else:
                    log["statut"] = "partiel"
                log["erreur"] = str(exc)
        log["liens_detectes"] = len(combined)
        if not combined and log["statut"] == "ok":
            log["statut"] = "aucun lien détecté — structure à contrôler"
        return combined, log, page_incidents, discoveries

    discovered_during_passage = []

    def scan(batch):
        if not batch:
            return
        with ThreadPoolExecutor(max_workers=4) as executor:
            for incoming, log, listing_incidents, discoveries in executor.map(listing, network_order(batch)):
                merge_records(records, incoming)
                logs.append(log)
                incidents.extend(listing_incidents)
                discovered_during_passage.extend(discoveries)
                state.setdefault("tentatives_sources", {})[log["url"]] = stamp
                print(f"{log['nom']} : {log['liens_detectes']} lien(s), {log['statut']}", flush=True)

    scan(sources)
    known_source_urls = {source["url"] for source in source_database}
    remembered = state.setdefault("sources_decouvertes", {})
    newly_discovered = []
    for item in discovered_during_passage:
        was_known = item["url"] in remembered
        previous = remembered.get(item["url"], {})
        remembered[item["url"]] = {**previous, **item,
                                   "premiere_detection_utc": previous.get("premiere_detection_utc", item["premiere_detection_utc"])}
        if not was_known and item["url"] not in known_source_urls:
            newly_discovered.append(item)
    already_selected = {source["url"] for source in sources}
    remaining_discovered_budget = max(0, max_discovered_sources - source_coverage["sources_decouvertes_selectionnees"])
    immediate = [{"nom": item["nom"], "categorie": "source_decouverte", "url": item["url"],
                  "active": True, "usage": "veille_decouverte", "motif": f"Découverte depuis {item['provenance']}"}
                 for item in {row["url"]: row for row in newly_discovered}.values()
                 if item["url"] not in already_selected][:remaining_discovered_budget]
    first_discovery_count = len(discovered_during_passage)
    scan(immediate)
    for item in discovered_during_passage[first_discovery_count:]:
        previous = remembered.get(item["url"], {})
        remembered[item["url"]] = {**previous, **item,
                                   "premiere_detection_utc": previous.get("premiere_detection_utc", item["premiere_detection_utc"])}
    sources += immediate
    source_coverage["sources_decouvertes_selectionnees"] += len(immediate)
    source_coverage["sources_decouvertes_total"] = len(remembered)
    source_coverage["sources_selectionnees_ce_passage"] = len(sources)
    log_by_url = {log["url"]: log for log in logs}
    hearing_urls = []
    for url, record in list(records.items()):
        if "licitor.com" in urlparse(url).netloc and urlparse(url).path.startswith("/ventes-judiciaires-immobilieres/"):
            priority = 0 if url not in known_link_urls else 1
            attempted = state.get("tentatives_audiences", {}).get(url, "")
            hearing_urls.append((priority, attempted, url, record["sources"][0]))
    hearing_urls.sort(key=lambda item: (item[0], item[1], item[2]))
    remaining_pages = [(url, source) for _, _, url, source in hearing_urls[:args.max_hearings]]
    seen_pages = set()
    while remaining_pages and len(seen_pages) < args.max_hearings:
        url, source = remaining_pages.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        state.setdefault("tentatives_audiences", {})[url] = stamp
        try:
            markup, resolved = fetcher.get(url)
            incoming, pages = hearing_records(markup, resolved, source, stamp)
            merge_records(records, incoming)
            log_by_url[source["url"]]["pages_audiences_lues"] += 1
            remaining_pages.extend((page, source) for page in pages if page not in seen_pages)
        except Exception as exc:
            incidents.append(incident(exc, url, "audience"))
    print(f"Audiences lues : {len(seen_pages)}", flush=True)
    candidates = []
    for url, record in records.items():
        if urlparse(url).path.startswith("/ventes-judiciaires-immobilieres/"):
            continue
        if is_pdf_url(url):
            record["support"] = "pdf_a_lire"
            record["documents"] = [url]
            record["documents_controles_automatiquement"] = False
            continue
        if record["faits"].get("vente_amiable"):
            continue
        previous = previous_fiches.get(url, {})
        priority = 0 if url not in known_link_urls else (1 if not previous.get("empreinte_contenu") else 2)
        attempted = state.get("tentatives", {}).get(url, previous.get("controle_detail_utc", ""))
        candidates.append((priority, attempted, url))
    candidates.sort()
    chosen = network_order_urls([url for _, _, url in candidates[:args.max_details]])
    failures, details_read, first_details = 0, 0, 0
    updated_urls = set()

    def read_detail(url):
        try:
            markup, resolved = fetcher.get(url)
            current = records[url]
            previous = previous_fiches.get(url, {})
            if previous:
                current = {**previous, **current,
                           "faits": {**previous.get("faits", {}), **current.get("faits", {})},
                           "sources": list({source["url"]: source for source in previous.get("sources", []) + current.get("sources", [])}.values()),
                           "documents": sorted(set(previous.get("documents", [])) | set(current.get("documents", [])))}
            return url, parse_detail(markup, resolved, current), None
        except PdfDocument:
            parsed = {**records[url], "support": "pdf_a_lire", "documents": [url],
                      "documents_controles_automatiquement": False, "detail_lu_ce_passage": False}
            return url, parsed, None
        except Exception as exc:
            return url, None, incident(exc, url, "fiche")

    with ThreadPoolExecutor(max_workers=4) as executor:
        for url, parsed, error in executor.map(read_detail, chosen):
            state.setdefault("tentatives", {})[url] = stamp
            previous = previous_fiches.get(url, {})
            if error:
                failures += 1
                records[url]["erreur_detail"] = error["erreur"]
                incidents.append(error)
            else:
                details_read += bool(parsed.get("detail_lu_ce_passage"))
                first_detail = bool(parsed.get("empreinte_contenu") and not previous.get("empreinte_contenu"))
                changed = bool(previous.get("empreinte_contenu") and previous["empreinte_contenu"] != parsed.get("empreinte_contenu"))
                first_details += first_detail
                if first_detail or changed:
                    updated_urls.add(url)
                records[url] = parsed
                count_detail_for_selected_sources(
                    log_by_url, parsed["sources"], parsed.get("detail_lu_ce_passage")
                )
            if (details_read + failures) % 20 == 0:
                print(f"Fiches : {details_read} lues, {failures} en échec", flush=True)
    new_urls = {url for url in records if url not in known_link_urls}
    for url, current in list(records.items()):
        previous = previous_fiches.get(url, {})
        if previous:
            current_error = current.get("erreur_detail")
            current = {**previous, **current,
                       "faits": {**previous.get("faits", {}), **current.get("faits", {})},
                       "sources": list({source["url"]: source for source in previous.get("sources", []) + current.get("sources", [])}.values()),
                       "documents": sorted(set(previous.get("documents", [])) | set(current.get("documents", [])))}
            current["erreur_detail"] = current_error
        current["date_detection"] = state.get("liens", {}).get(url, {}).get("premiere_detection", stamp)
        current = track_record(previous, current, stamp)
        if previous and current["nouveaux_elements_ce_passage"]:
            updated_urls.add(url)
        records[url] = current
        state.setdefault("fiches", {})[url] = current
    modified = len(updated_urls & known_link_urls)
    daily_urls = new_urls | updated_urls
    # Les pièces sont téléchargées au plus une fois par URL dans ce passage.
    # Leur lecture progresse page par page entre passages sans lever la validation humaine.
    max_documents = getattr(args, "max_documents", 0 if fixture_map is not None else 12)
    max_pdf_pages = getattr(args, "max_pdf_pages", 80)
    max_ocr_pages = getattr(args, "max_ocr_pages", 6)
    reader = PdfReader(stamp, max_pdf_pages, max_ocr_pages)
    document_owners, tasks = {}, []
    for url, record in records.items():
        for document in record.get("documents", []):
            document_owners.setdefault(document, []).append(url)
    for url, owners in document_owners.items():
        is_recent = any(owner in daily_urls for owner in owners)
        previous = state.get("pieces", {}).get(url, {})
        tasks.append((0 if is_recent else 1, previous.get("derniere_tentative_utc", ""), url))
    tasks.sort()
    documents = []
    for _, _, url in tasks[:max_documents]:
        previous = state.setdefault("pieces", {}).get(url, {})
        try:
            body, resolved = fetcher.get_pdf(url)
            document = reader.read(body, resolved, previous)
            document["url_origine"] = url
            document["annonces"] = document_owners[url]
            document["derniere_tentative_utc"] = stamp
            state["pieces"][url] = document
            for owner in document_owners[url]:
                records[owner].setdefault("preuves_documentaires", []).extend(document["preuves"])
                records[owner].setdefault("documents_extraits", []).append(
                    {"url": document["url"], "statut": document["statut"], "pages_lues": document["pages_lues"],
                     "pages_total": document["pages_total"], "validation": "a_verifier"})
            for item in document["incidents_pages"]:
                incidents.append({"url": url, "phase": "pdf", **item})
        except Exception as exc:
            error = incident(exc, url, "pdf")
            incidents.append(error)
            document = {**error, "statut": "differe" if isinstance(exc, AccessDeferred) else "echec", "annonces": document_owners[url]}
            state["pieces"][url] = {**previous, "derniere_tentative_utc": stamp, "derniere_erreur": str(exc)}
        documents.append(document)
        print(f"PDF : {len(documents)}/{min(max_documents, len(tasks))}, {document['statut']}", flush=True)
    history = document_history(records, state.get("pieces", {}),
                               {doc.get("url_origine", doc["url"]) for doc in documents if doc["statut"] in {"lu", "partiel"}})
    valuations = load_valuations(Path(args.valuations))
    qualified = [classify(record, now, valuations.get(url)) for url, record in records.items()
                 if not urlparse(url).path.startswith("/ventes-judiciaires-immobilieres/")]
    qualified.sort(key=lambda x: (x["statut"], x.get("faits", {}).get("date_vente") or "9999-12-31",
                                  x.get("adresse") or x["titre"]))
    new_count = len(new_urls)
    daily = [row for row in qualified if row["url_annonce"] in daily_urls]
    for url, record in records.items():
        state.setdefault("liens", {})[url] = {"premiere_detection": state.get("liens", {}).get(url, {}).get("premiere_detection", stamp), "dernier_controle": stamp}
    interventions = source_interventions(state, logs, stamp)
    control_path = Path(getattr(args, "coverage_control", ROOT / "controle_couverture.csv"))
    audit = coverage_audit(control_path, set(state.get("liens", {})))
    state["version"], state["dernier_passage"] = 1, stamp
    atomic_json(state_path, state)
    unknown = sum(not row.get("faits", {}).get("date_vente") for row in qualified)
    pdf_count = sum(record.get("support") == "pdf_a_lire" for record in records.values())
    limits = [f"Budget : {args.max_hearings} pages d'audience et {args.max_details} fiches par passage ; "
              f"{max(0, len(candidates) - len(chosen))} fiches candidates non lues ce passage.",
              f"{unknown} liens de fiches sans date d'enchère extraite restent suivis ; la date est informative et non éliminatoire.",
              "Les nouvelles audiences Licitor sont lues dès leur détection, puis les autres tournent selon la dernière tentative. ",
              f"Pagination des listes : au plus {max_listing_pages} pages par source et par passage.",
              f"Toute la base de {source_coverage['sources_base_total']} adresses est active : "
              f"{source_coverage['veille_quotidienne_total']} pages directes à chaque passage, "
              f"{source_coverage['veille_rapide_total']} profils en rotation rapide et "
              f"{source_coverage['decouverte_nationale_total']} annuaires/juridictions en rotation quotidienne.",
              f"File après sélection : {source_coverage['veille_rapide_en_retard']} profil(s) rapide(s) en retard, "
              f"{source_coverage['decouverte_nationale_en_attente']} annuaire(s) dus en attente et "
              f"{source_coverage['sources_decouvertes_en_retard']} source(s) découverte(s) en retard.",
              "Le registre central Registre_Adjudications.xlsx n'est pas modifié par ce programme.",
              "Une opportunité n'est retenue que si l'occupation libre est confirmée et si la mise à prix publiée ne dépasse pas 60 000 €."]
    limits.append(f"PDF : budget de {max_documents} documents, {max_pdf_pages} pages et {max_ocr_pages} pages OCR ; "
                  f"au plus 8 pages d'un document par passage, avec reprise des pages restantes. "
                  f"{max(0, len(tasks) - max_documents)} documents détectés non consultés ce passage ; {pdf_count} liens directs vers un PDF.")
    limits.append("Les indices extraits des pièces restent à vérifier : des PDF peuvent comporter plusieurs biens ou des mentions anciennes. "
                  "Ils ne remplacent pas la confirmation que le bien est libre.")
    if remaining_pages:
        limits.append(f"{len(remaining_pages)} pages d'audience restent en file après le budget.")
    if incidents:
        limits.append(f"{len(incidents)} incident(s) lors de la lecture des audiences ou des fiches ; voir dernier.json.")
    report = {
        "version": 1, "controle_utc": stamp, "controle_paris": now.astimezone(PARIS).isoformat(),
        "comptages": {"sources_actives": len(sources), "sources_ok": sum(x["statut"] == "ok" for x in logs),
                      "sources_accessibles": sum(x["statut"] in {"ok", "partiel"} or x["statut"].startswith("aucun lien") for x in logs),
                      "pages_listes_lues": sum(x.get("pages_listes_lues", 0) for x in logs),
                      "liens_distincts": len(records), "nouveaux_liens": new_count, "audiences_lues": sum(x["pages_audiences_lues"] for x in logs),
                      "fiches_lues": details_read, "fiches_en_echec": failures,
                      "fiches_nouvellement_lues": first_details, "fiches_modifiees": modified,
                      "dossiers_suivis_observes": len(qualified), "dossiers_du_jour": len(daily),
                      "retenus": sum(x["statut"] == "retenu_sur_donnees_validees" for x in daily),
                      "a_verifier": sum(x["statut"] == "a_verifier" for x in daily), "rejetes": sum(x["statut"] == "rejete" for x in daily),
                      "sans_date_enchere": unknown, "liens_pdf_a_lire": pdf_count, "fiches_candidates_non_lues": max(0, len(candidates) - len(chosen)),
                      "documents_pdf_consultes": sum(x["statut"] in {"lu", "partiel"} for x in documents),
                      "documents_pdf_complets": sum(x["statut"] == "lu" for x in documents),
                      "documents_pdf_detectes": len(tasks), "interventions_requises": len(interventions),
                      **source_coverage, **reader.metrics, **fetcher.metrics},
        "dossiers": daily, "sources": logs, "incidents": incidents, "limites": limits, "documents": documents,
        "delais_acces": state.get("acces", {}), "historique_documents": history, "interventions": interventions,
        "controle_couverture": audit,
    }
    save_report(Path(args.output), report)
    # Le catalogue complet reste dans l'artefact, pas dans le rapport public résumé.
    atomic_json(Path(args.catalogue), {"controle_utc": stamp, "liens": qualified})
    print(json.dumps(report["comptages"], ensure_ascii=False), flush=True)
    # Code 2 : collecte terminée mais couverture partielle. Code 1 : aucune
    # source exploitable ou erreur d'extracteur à corriger.
    critical = not any(x["statut"] in {"ok", "partiel"} or x["statut"].startswith("aucun lien") for x in logs)
    return 1 if critical else (2 if any(x["statut"] in {"echec", "differe"} for x in logs) or incidents
                                  or any(x["statut"] != "lu" for x in documents) else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(ROOT / "data/etat.json"))
    parser.add_argument("--output", default=str(ROOT / "rapports"))
    parser.add_argument("--catalogue", default=str(ROOT / "data/catalogue.json"))
    parser.add_argument("--valuations", default=str(ROOT / "estimations.csv"))
    parser.add_argument("--max-hearings", type=int, default=200)
    parser.add_argument("--max-listing-pages", type=int, default=12)
    parser.add_argument("--max-profile-pages", type=int, default=2)
    parser.add_argument("--max-discovery-pages", type=int, default=1)
    parser.add_argument("--max-profile-sources", type=int, default=700)
    parser.add_argument("--max-directory-sources", type=int, default=160)
    parser.add_argument("--max-discovered-sources", type=int, default=100)
    parser.add_argument("--profile-interval-hours", type=float, default=5)
    parser.add_argument("--directory-interval-hours", type=float, default=23)
    parser.add_argument("--discovered-interval-hours", type=float, default=24)
    parser.add_argument("--max-details", type=int, default=800)
    parser.add_argument("--max-documents", type=int, default=100)
    parser.add_argument("--max-pdf-pages", type=int, default=800)
    parser.add_argument("--max-ocr-pages", type=int, default=80)
    parser.add_argument("--pause", type=float, default=0.8)
    parser.add_argument("--coverage-control", default=str(ROOT / "controle_couverture.csv"))
    parser.add_argument("--now", help="date ISO avec fuseau, pour vérification reproductible")
    parser.add_argument("--fixtures", help="JSON URL -> fichier HTML local pour vérification sans réseau")
    args = parser.parse_args()
    if min(args.max_hearings, args.max_details, args.max_documents, args.max_pdf_pages, args.max_ocr_pages,
           args.max_profile_sources, args.max_directory_sources, args.max_discovered_sources,
           args.profile_interval_hours, args.directory_interval_hours, args.discovered_interval_hours,
           args.pause) < 0:
        parser.error("budgets et pause doivent être positifs ou nuls")
    if min(args.max_listing_pages, args.max_profile_pages, args.max_discovery_pages) < 1:
        parser.error("les budgets de pagination doivent être au moins égaux à 1")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
