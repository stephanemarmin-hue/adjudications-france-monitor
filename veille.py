#!/usr/bin/env python3
"""Veille bornée des pages publiques, qualification et rapports auditables."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
    date_known = detail_facts.get("date_vente") or old_facts.get("date_vente")
    expected_no_date = detail_facts.get("retiree") or detail_facts.get("vente_amiable")
    if host in {"avoventes.fr", "licitor.com"} and not date_known and not expected_no_date:
        raise ValueError("structure de fiche non reconnue : date de vente absente")
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
    result["documents"] = list({canonical(n.attrs["href"], page_url) for n in container.all()
                                if n.tag == "a" and ".pdf" in n.attrs.get("href", "").lower()})[:30]
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


def save_report(folder, report):
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / "dernier.json", report)
    rows = report["dossiers"]
    columns = ["id", "statut", "adresse", "titre", "date_vente", "jours_apres_audience", "occupation",
               "prix_adjuge_eur", "motifs_rejet", "a_verifier", "url_annonce"]
    with (folder / "dernier.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            output = {key: row.get(key, "") for key in columns}
            output.update({key: row["faits"].get(key, "") for key in ["date_vente", "occupation", "prix_adjuge_eur"]})
            for key in ["motifs_rejet", "a_verifier"]:
                output[key] = "; ".join(row[key])
            writer.writerow(output)
    with (folder / "sources.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        cols = ["nom", "url", "statut", "liens_detectes", "pages_audiences_lues", "fiches_lues", "erreur", "controle_utc"]
        writer = csv.DictWriter(stream, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["sources"])
    count = report["comptages"]
    lines = ["# Veille adjudications — nouveautés du jour", "", f"Contrôle : **{report['controle_paris']}** (Europe/Paris).",
             "", f"**{count['dossiers_recents']} dossier(s) nouveau(x) ou modifié(s) aujourd'hui** : "
             f"{count['retenus']} bien(s) libre(s) confirmé(s), {count['a_verifier']} à vérifier et "
             f"{count['rejetes_recents']} rejet(s).", "",
             f"Le moteur a observé {count.get('dossiers_recents_observes', 0)} dossier(s) dans la fenêtre J+1 à J+4 ; "
             "seuls les nouveaux ou modifiés figurent ci-dessous. L'historique complet reste conservé dans le catalogue.", "",
             f"{count['sources_ok']}/{count['sources_actives']} pages sources accessibles ; {count['liens_distincts']} liens distincts ; "
             f"{count['audiences_lues']} pages d'audience et {count['fiches_lues']} fiches détaillées lues.", "",
             f"Nouveautés : {count['nouveaux_liens']} liens ; {count.get('fiches_nouvellement_lues', 0)} fiches lues pour la première fois ; "
             f"{count['fiches_modifiees']} fiches modifiées depuis le passage précédent.", "",
             f"Pièces : {count.get('documents_pdf_consultes', 0)} PDF accessibles ; {count.get('pages_pdf_traitees', 0)} pages traitées ce passage, "
             f"{count.get('pages_pdf_extraites', 0)} lues, dont {count.get('pages_ocr', 0)} par OCR. "
             f"{count.get('acces_differes', 0)} requêtes différées après une limite ou un refus d'accès.", "",
             "Le nombre de liens inclut des audiences et des annonces anciennes ou futures. La couverture est limitée aux pages listées et au budget de lecture indiqué ci-dessous.", "",
             "## Dossiers nouveaux ou modifiés aujourd'hui", "",
             "| Bien | Statut | Occupation | Audience | Prix publié (indicatif) | Contrôle | Source |",
             "|---|---|---|---|---|---|---|"]
    labels = {"retenu_sur_donnees_validees": "Libre confirmé", "a_verifier": "À vérifier", "rejete": "Rejeté"}
    for row in rows:
        facts = row["faits"]
        control = "; ".join(row["motifs_rejet"] or row["a_verifier"]) or "Occupation libre confirmée"
        evidence_links = "".join(f" ; [{p['champ']} p. {p['page']}]({p['url']}#page={p['page']})"
                                 for p in row.get("preuves_documentaires", [])[:3])
        values = [row.get("adresse") or row["titre"], labels.get(row["statut"], row["statut"]),
                  facts.get("occupation", "inconnue"), facts.get("date_vente"), money(facts.get("prix_adjuge_eur")),
                  control, f"[Annonce]({row['url_annonce']})" + evidence_links]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    if not rows:
        lines += ["", "Aucun dossier nouveau ou modifié aujourd'hui."]
    lines += ["", "## Sources productives ce passage", "", "| Source | Accès | Liens | Audiences | Fiches | Incident |", "|---|---|---|---|---|---|"]
    productive_sources = [source for source in report["sources"] if source.get("liens_detectes")
                          or source.get("pages_audiences_lues") or source.get("fiches_lues")]
    for source in productive_sources:
        lines.append("| " + " | ".join(cell(x) for x in [f"[{source['nom']}]({source['url']})", source["statut"], source["liens_detectes"],
                     source["pages_audiences_lues"], source["fiches_lues"], source.get("erreur") or "—"]) + " |")
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
               "Seule la preuve que le bien est libre conditionne sa sélection automatique.", ""]
    (folder / "dernier.md").write_text("\n".join(lines), encoding="utf-8")


def run(args):
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("--now doit préciser un fuseau horaire")
    stamp, today = now.isoformat(), now.astimezone(PARIS).date()
    fixture_map = json.loads(Path(args.fixtures).read_text()) if args.fixtures else None
    state_path = Path(args.state)
    state = json.loads(state_path.read_text()) if state_path.exists() else {"liens": {}, "fiches": {}}
    if state.get("version") not in {None, 1}:
        raise ValueError("version d'état non prise en charge")
    fetcher = Fetcher(fixture_map, pause=args.pause, access_state=state.setdefault("acces", {}))
    sources = load_sources()
    records, logs, incidents = {}, [], []

    def listing(source):
        log = {**source, "controle_utc": stamp, "statut": "ok", "liens_detectes": 0,
               "pages_audiences_lues": 0, "fiches_lues": 0, "erreur": ""}
        try:
            markup, resolved = fetcher.get(source["url"])
            incoming, _ = seed_records(markup, resolved, source, stamp)
            log["liens_detectes"] = len(incoming)
            if not incoming:
                log["statut"] = "aucun lien détecté — structure à contrôler"
            return incoming, log
        except Exception as exc:
            log["statut"] = "differe" if isinstance(exc, AccessDeferred) else "echec"
            log.update(incident(exc, source["url"], "source"))
            return {}, log

    with ThreadPoolExecutor(max_workers=4) as executor:
        for incoming, log in executor.map(listing, sources):
            merge_records(records, incoming)
            logs.append(log)
            print(f"{log['nom']} : {log['liens_detectes']} lien(s), {log['statut']}", flush=True)
    log_by_url = {log["url"]: log for log in logs}
    hearing_urls = []
    for url, record in list(records.items()):
        if "licitor.com" in urlparse(url).netloc and urlparse(url).path.startswith("/ventes-judiciaires-immobilieres/"):
            day = french_date(urlparse(url).path.replace("-", " "))
            if day and 1 <= (today - day).days <= 4:
                hearing_urls.append((url, record["sources"][0]))
    remaining_pages = hearing_urls[:args.max_hearings]
    seen_pages = set()
    while remaining_pages and len(seen_pages) < args.max_hearings:
        url, source = remaining_pages.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
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
        date_value = record["faits"].get("date_vente")
        day = None
        if date_value:
            try:
                day = datetime.fromisoformat(date_value).date()
            except ValueError:
                day = None
        age = (today - day).days if day else None
        if age is None or 1 <= age <= 4:
            priority = 0 if age is not None else 1
            previous = state.get("fiches", {}).get(url, {})
            attempted = state.get("tentatives", {}).get(url, previous.get("controle_detail_utc", ""))
            candidates.append((priority, attempted, url))
    candidates.sort()
    chosen = [url for _, _, url in candidates[:args.max_details]]
    failures, modified, details_read, first_details = 0, 0, 0, 0
    updated_urls = set()

    def read_detail(url):
        try:
            markup, resolved = fetcher.get(url)
            return url, parse_detail(markup, resolved, records[url]), None
        except PdfDocument:
            parsed = {**records[url], "support": "pdf_a_lire", "documents": [url],
                      "documents_controles_automatiquement": False, "detail_lu_ce_passage": False}
            return url, parsed, None
        except Exception as exc:
            return url, None, incident(exc, url, "fiche")

    with ThreadPoolExecutor(max_workers=4) as executor:
        for url, parsed, error in executor.map(read_detail, chosen):
            state.setdefault("tentatives", {})[url] = stamp
            previous = state.get("fiches", {}).get(url, {})
            if error:
                failures += 1
                records[url]["erreur_detail"] = error["erreur"]
                incidents.append(error)
            else:
                details_read += bool(parsed.get("detail_lu_ce_passage"))
                first_detail = bool(parsed.get("empreinte_contenu") and not previous.get("empreinte_contenu"))
                changed = bool(previous.get("empreinte_contenu") and previous["empreinte_contenu"] != parsed.get("empreinte_contenu"))
                modified += changed
                first_details += first_detail
                if first_detail or changed:
                    updated_urls.add(url)
                records[url] = parsed
                state.setdefault("fiches", {})[url] = parsed
                for source in parsed["sources"]:
                    log_by_url[source["url"]]["fiches_lues"] += bool(parsed.get("detail_lu_ce_passage"))
            if (details_read + failures) % 20 == 0:
                print(f"Fiches : {details_read} lues, {failures} en échec", flush=True)
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
        is_recent = any(records[owner].get("detail_lu_ce_passage") and records[owner]["faits"].get("date_vente")
                        and 1 <= (today - datetime.fromisoformat(records[owner]["faits"]["date_vente"]).date()).days <= 4 for owner in owners)
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
    recent = [row for row in qualified if row["jours_apres_audience"] is not None and 1 <= row["jours_apres_audience"] <= 4]
    recent.sort(key=lambda x: (x["statut"], -(x.get("jours_apres_audience") or 0), x.get("adresse") or x["titre"]))
    new_urls = {url for url in records if url not in state.get("liens", {})}
    new_count = len(new_urls)
    daily_urls = new_urls | updated_urls
    daily = [row for row in recent if row["url_annonce"] in daily_urls]
    for url, record in records.items():
        state.setdefault("liens", {})[url] = {"premiere_detection": state.get("liens", {}).get(url, {}).get("premiere_detection", stamp), "dernier_controle": stamp}
    state["version"], state["dernier_passage"] = 1, stamp
    atomic_json(state_path, state)
    unknown = sum(row["jours_apres_audience"] is None for row in qualified)
    pdf_count = sum(record.get("support") == "pdf_a_lire" for record in records.values())
    limits = [f"Budget : {args.max_hearings} pages d'audience et {args.max_details} fiches par passage ; "
              f"{max(0, len(candidates) - len(chosen))} fiches candidates non lues ce passage.",
              f"{unknown} liens de fiches sans date d'audience extraite restent hors sélection ; "
              "ils sont explorés par rotation aux passages suivants.",
              "Les paginations Licitor des audiences récentes sont parcourues dans le budget. "
              "Les autres listes ne sont pas paginées automatiquement.",
              "Ces pages ne constituent pas une couverture exhaustive de la France ni des 706 fiches de la base d'avocats.",
              "Le registre central Registre_Adjudications.xlsx n'est pas modifié par ce programme.",
              "Une opportunité n'est retenue que si l'occupation libre est confirmée ; les autres données publiées restent indicatives."]
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
                      "liens_distincts": len(records), "nouveaux_liens": new_count, "audiences_lues": sum(x["pages_audiences_lues"] for x in logs),
                      "fiches_lues": details_read, "fiches_en_echec": failures,
                      "fiches_nouvellement_lues": first_details, "fiches_modifiees": modified,
                      "dossiers_recents_observes": len(recent), "dossiers_recents": len(daily),
                      "retenus": sum(x["statut"] == "retenu_sur_donnees_validees" for x in daily),
                      "a_verifier": sum(x["statut"] == "a_verifier" for x in daily), "rejetes_recents": sum(x["statut"] == "rejete" for x in daily),
                      "sans_date": unknown, "liens_pdf_a_lire": pdf_count, "fiches_candidates_non_lues": max(0, len(candidates) - len(chosen)),
                      "documents_pdf_consultes": sum(x["statut"] in {"lu", "partiel"} for x in documents),
                      "documents_pdf_complets": sum(x["statut"] == "lu" for x in documents),
                      "documents_pdf_detectes": len(tasks), **reader.metrics, **fetcher.metrics},
        "dossiers": daily, "sources": logs, "incidents": incidents, "limites": limits, "documents": documents,
        "delais_acces": state.get("acces", {}), "historique_documents": history,
    }
    save_report(Path(args.output), report)
    # Le catalogue complet reste dans l'artefact, pas dans le rapport public résumé.
    atomic_json(Path(args.catalogue), {"controle_utc": stamp, "liens": qualified})
    print(json.dumps(report["comptages"], ensure_ascii=False), flush=True)
    # Code 2 : collecte terminée mais couverture partielle. Code 1 : aucune
    # source exploitable ou erreur d'extracteur à corriger.
    critical = not any(x["statut"] == "ok" for x in logs) or any("structure" in x["erreur"] for x in incidents)
    return 1 if critical else (2 if any(x["statut"] in {"echec", "differe"} for x in logs) or incidents
                                  or any(x["statut"] != "lu" for x in documents) else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(ROOT / "data/etat.json"))
    parser.add_argument("--output", default=str(ROOT / "rapports"))
    parser.add_argument("--catalogue", default=str(ROOT / "data/catalogue.json"))
    parser.add_argument("--valuations", default=str(ROOT / "estimations.csv"))
    parser.add_argument("--max-hearings", type=int, default=40)
    parser.add_argument("--max-details", type=int, default=120)
    parser.add_argument("--max-documents", type=int, default=12)
    parser.add_argument("--max-pdf-pages", type=int, default=80)
    parser.add_argument("--max-ocr-pages", type=int, default=6)
    parser.add_argument("--pause", type=float, default=0.8)
    parser.add_argument("--now", help="date ISO avec fuseau, pour vérification reproductible")
    parser.add_argument("--fixtures", help="JSON URL -> fichier HTML local pour vérification sans réseau")
    args = parser.parse_args()
    if min(args.max_hearings, args.max_details, args.max_documents, args.max_pdf_pages, args.max_ocr_pages, args.pause) < 0:
        parser.error("budgets et pause doivent être positifs ou nuls")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
