#!/usr/bin/env python3
"""Importe les alertes Gmail d'adjudication sans publier le contenu de la boîte."""
from __future__ import annotations

import argparse
import email
import hashlib
import imaplib
import json
import os
import re
import subprocess
import unicodedata
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "data" / "gmail_annonces.json"
DEFAULT_PIECES = ROOT / "data" / "gmail_pieces"
NEW_LABEL = "Adjudications-Nouveau"
DONE_LABEL = "Adjudications-Traite"
VERIFY_LABEL = "Adjudications-A-Verifier"
HUMAN_LABEL = "Adjudications-Intervention"
OUT_LABEL = "Adjudications-Hors-Perimetre"
MAX_MESSAGES = 250
MAX_ATTACHMENT_BYTES = 20_000_000

AUCTION_TERMS = (
    "adjudication", "vente aux encheres", "vente judiciaire", "enchere immobiliere",
    "encheres immobilieres", "mise a prix", "saisie immobiliere", "surenchere",
)
HUMAN_TERMS = (
    "action requise", "confirmer votre compte", "confirmation de compte", "activez votre compte",
    "valider votre inscription", "mot de passe", "code de verification", "captcha",
)
KNOWN_HOSTS = {
    "avoventes.fr", "licitor.com", "encheresimmobilieres.fr", "encheres-publiques.com",
    "vench.fr", "info-encheres.com", "avocats-valdemarne.com", "tribuca.net",
    "petitesaffiches.fr", "lagazettefrance.fr",
}
IGNORED_URL_MARKERS = (
    "unsubscribe", "desabonnement", "se-desinscrire", "preferences", "facebook.com",
    "instagram.com", "linkedin.com", "twitter.com", "x.com", "mailto:",
)


def normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    return " ".join("".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().split())


def decoded_header(value: str | None) -> str:
    result = []
    for part, charset in decode_header(value or ""):
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result).strip()


class HtmlContent(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.href = ""
        self.anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag.lower() == "a":
            self.href = (attributes.get("href") or "").strip()
            self.anchor = []

    def handle_data(self, data):
        if data.strip():
            self.text_parts.append(data)
            if self.href:
                self.anchor.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.href:
            self.links.append((self.href, " ".join(" ".join(self.anchor).split())))
            self.href, self.anchor = "", []

    @property
    def text(self):
        return " ".join(" ".join(self.text_parts).split())


def decoded_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def message_content(message: Message) -> tuple[str, list[tuple[str, str]]]:
    plain, html = [], []
    for part in message.walk():
        if part.is_multipart() or part.get_filename():
            continue
        kind = part.get_content_type().lower()
        if kind == "text/plain":
            plain.append(decoded_payload(part))
        elif kind == "text/html":
            html.append(decoded_payload(part))
    parser = HtmlContent()
    parser.feed("\n".join(html))
    text = "\n".join(item for item in plain if item.strip())
    if not text.strip() or len(parser.text) > len(text):
        text = parser.text
    links = list(parser.links)
    for found in re.findall(r"https?://[^\s<>\]\[\"']+", text):
        links.append((found.rstrip(".,;:)"), ""))
    return " ".join(text.split()), links


def unwrap_url(url: str) -> str:
    url = url.strip().replace("&amp;", "&")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if host in {"google.com", "google.fr", "click.mail.google.com"}:
        query = parse_qs(parsed.query)
        for key in ("url", "q", "u"):
            candidate = query.get(key, [""])[0]
            if candidate.startswith("http"):
                return unquote(candidate)
    return url


def url_score(url: str, label: str) -> int:
    searchable = normalized(url + " " + label)
    if not url or any(marker in searchable for marker in IGNORED_URL_MARKERS):
        return -100
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    score = 5 if host in KNOWN_HOSTS else 0
    score += 3 * sum(term in searchable for term in AUCTION_TERMS)
    if any(token in searchable for token in ("vente", "enchere", "annonce", "lot", "immobilier")):
        score += 2
    if any(token in searchable for token in ("connexion", "login", "compte", "privacy", "contact")):
        score -= 2
    return score


def candidate_urls(links: list[tuple[str, str]]) -> list[str]:
    scored = {}
    for raw, label in links:
        url = unwrap_url(raw)
        score = url_score(url, label)
        if score > 0:
            scored[url] = max(score, scored.get(url, -100))
    return [url for url, _ in sorted(scored.items(), key=lambda item: (-item[1], item[0]))[:60]]


def decimal_amount(value: str) -> str | None:
    cleaned = re.sub(r"[^\d,.]", "", value).replace(" ", "")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if number <= 0:
        return None
    return f"{number:.2f}"


def extract_price(text: str) -> str | None:
    labelled = re.search(r"mise\s+[aà]\s+prix(?:\s+initiale)?\s*[:\-]?\s*([\d\s.,]+)\s*(?:€|eur)", text, re.I)
    if labelled:
        return decimal_amount(labelled.group(1))
    amounts = {decimal_amount(value) for value in re.findall(r"(?<!\d)([\d][\d\s.,]{1,18})\s*(?:€|eur)\b", text, re.I)} - {None}
    return next(iter(amounts)) if len(amounts) == 1 else None


MONTHS = {normalized(name): index for index, name in enumerate(
    ["", "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
     "septembre", "octobre", "novembre", "décembre"])
}


def extract_date(text: str) -> str | None:
    match = re.search(r"(?:date de (?:la )?(?:vente|ench[eè]re|audience)|audience|vente)\s*(?:du|le|:)\s*"
                      r"(\d{1,2})[\s/.-]+([a-zéûôîèàç]+|\d{1,2})[\s/.-]+(20\d{2})", text, re.I)
    if not match:
        return None
    month = int(match.group(2)) if match.group(2).isdigit() else MONTHS.get(normalized(match.group(2)))
    if not month:
        return None
    try:
        return datetime(int(match.group(3)), month, int(match.group(1))).date().isoformat()
    except ValueError:
        return None


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return cleaned[:120] or "piece-jointe"


def pdf_text(path: Path) -> str:
    try:
        result = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                                check=False, timeout=90)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else ""


def attachments(message: Message, directory: Path) -> tuple[list[dict], str]:
    metadata, extracted = [], []
    directory.mkdir(parents=True, exist_ok=True)
    for part in message.walk():
        filename = decoded_header(part.get_filename())
        disposition = (part.get("Content-Disposition") or "").casefold()
        if not filename or ("attachment" not in disposition and part.get_content_maintype() == "image"):
            continue
        body = part.get_payload(decode=True) or b""
        if not body or len(body) > MAX_ATTACHMENT_BYTES:
            metadata.append({"nom": filename, "type": part.get_content_type(), "taille_octets": len(body),
                             "statut": "trop_volumineuse" if body else "vide"})
            continue
        fingerprint = hashlib.sha256(body).hexdigest()
        path = directory / f"{fingerprint[:16]}-{safe_filename(filename)}"
        if not path.exists():
            path.write_bytes(body)
        item = {"nom": filename, "type": part.get_content_type(), "taille_octets": len(body),
                "sha256": fingerprint, "chemin_artifact": str(path.relative_to(ROOT)).replace("\\", "/"),
                "statut": "conservee"}
        metadata.append(item)
        if part.get_content_type() == "application/pdf" or path.suffix.casefold() == ".pdf":
            extracted.append(pdf_text(path))
    return metadata, "\n".join(extracted)


def gmail_label_url(label: str) -> str:
    return f"https://mail.google.com/mail/u/0/#label/{quote(label, safe='')}"


def relevance(subject: str, text: str, sender: str) -> bool:
    searchable = normalized(f"{subject} {text[:12000]} {sender}")
    return any(term in searchable for term in AUCTION_TERMS) or any(host in searchable for host in KNOWN_HOSTS)


def requires_human(subject: str, text: str) -> bool:
    searchable = normalized(f"{subject} {text[:4000]}")
    return any(term in searchable for term in HUMAN_TERMS)


def intervention(source: str, url: str, problem: str, action: str, priority="à vérifier") -> dict:
    return {"priorite": priority, "source": source, "url": url, "statut": "intervention_email",
            "passages_consecutifs": 1, "probleme": problem, "action": action}


def record_for(url: str, subject: str, sender: str, message_id: str, stamp: str,
               text: str, pieces: list[dict], price: str | None, sale_date: str | None) -> dict:
    facts = {"occupation": "inconnue", "travaux": "a_verifier", "retiree": False,
             "seconde_adjudication": False, "surenchere_impossible": False, "vente_amiable": False}
    if price:
        facts["mise_a_prix_eur"] = price
    if sale_date:
        facts["date_vente"] = sale_date
        facts["date_vente_source"] = gmail_label_url(DONE_LABEL)
    source_url = gmail_label_url(DONE_LABEL)
    return {
        "id": hashlib.sha256(url.encode("utf-8")).hexdigest(), "url_annonce": url,
        "titre": subject[:350] or "Alerte e-mail d'adjudication", "adresse": "",
        "sources": [{"nom": f"Alerte e-mail — {sender}", "categorie": "email", "url": source_url,
                     "active": True, "usage": "alerte_email", "motif": "Message reçu dans Gmail"}],
        "faits": facts, "controle_liste_utc": stamp, "controle_detail_utc": stamp,
        "detail_lu_ce_passage": True, "support": "email",
        # Le message et les pièces restent dans Gmail. Seul un comptage neutre
        # entre dans l'état du moteur public.
        "pieces_email": [{"nom": f"Pièce jointe e-mail {index}", "statut": "dans_gmail"}
                         for index, _ in enumerate(pieces, 1)], "extrait_dossier": "",
        "empreinte_contenu": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "niveau_preuve": "B — alerte e-mail de la source, pièce à confirmer sur la publication",
        "documents": [], "documents_controles_automatiquement": bool(pieces),
    }


def process_message(raw: bytes, uid: str, pieces_dir: Path, stamp: str) -> dict:
    message = email.message_from_bytes(raw)
    subject = decoded_header(message.get("Subject"))
    sender = decoded_header(message.get("From"))
    sender_address = parseaddr(sender)[1] or sender
    message_id = (message.get("Message-ID") or f"imap-uid-{uid}").strip(" <>")
    text, links = message_content(message)
    pieces, attachment_text = attachments(message, pieces_dir / safe_filename(message_id))
    combined = " ".join(f"{text} {attachment_text}".split())
    inbox_url = gmail_label_url(HUMAN_LABEL)
    if requires_human(subject, combined):
        item = intervention(sender_address, inbox_url,
                            f"Un message demande une validation humaine : {subject or 'sans objet'}.",
                            "Ouvrir le message, vérifier l'expéditeur puis effectuer la confirmation, la connexion ou le captcha demandé.",
                            "urgente")
        return {"label": HUMAN_LABEL, "records": [], "interventions": [item], "attachments": len(pieces)}
    if not relevance(subject, combined, sender_address):
        return {"label": OUT_LABEL, "records": [], "interventions": [], "attachments": len(pieces)}
    urls = candidate_urls(links)
    price, sale_date = extract_price(combined), extract_date(combined)
    if len(urls) != 1:
        # Un récapitulatif multi-lots ne permet pas d'attribuer un prix global
        # à chaque URL sans lire la fiche correspondante.
        price = sale_date = None
    # Sans URL publique, le message reste dans À vérifier dans Gmail afin de ne
    # pas publier un identifiant interne de la boîte dans le catalogue public.
    targets = urls
    records = [record_for(url, subject, sender_address, message_id, stamp, combined, pieces, price, sale_date)
               for url in targets]
    label = DONE_LABEL if price and sale_date else VERIFY_LABEL
    return {"label": label, "records": records, "interventions": [], "attachments": len(pieces)}


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def add_label(connection, uid: str, label: str) -> None:
    connection.uid("STORE", uid, "+X-GM-LABELS", f'("{label}")')
    if label != NEW_LABEL:
        connection.uid("STORE", uid, "-X-GM-LABELS", f'("{NEW_LABEL}")')


def all_mailbox(connection) -> str:
    status, rows = connection.list()
    if status == "OK":
        for row in rows or []:
            decoded = row.decode("utf-8", errors="replace")
            if "\\All" in decoded:
                match = re.search(r'"([^\"]+)"\s*$', decoded)
                if match:
                    return match.group(1)
    return "INBOX"


def connect(address: str, password: str):
    connection = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=45)
    connection.login(address, password.replace(" ", ""))
    mailbox = all_mailbox(connection)
    status, _ = connection.select(f'"{mailbox}"')
    if status != "OK":
        status, _ = connection.select("INBOX")
    if status != "OK":
        raise RuntimeError("Impossible d'ouvrir la boîte Gmail")
    return connection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--pieces", default=str(DEFAULT_PIECES))
    parser.add_argument("--max-messages", type=int, default=MAX_MESSAGES)
    parser.add_argument("--raw-message", help="fichier .eml pour vérification locale sans Gmail")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).isoformat()
    output, pieces_dir = Path(args.output), Path(args.pieces)
    payload = {"controle_utc": stamp, "compte": "", "messages_lus": 0, "records": [],
               "pieces_email": 0, "interventions": []}
    if args.raw_message:
        result = process_message(Path(args.raw_message).read_bytes(), "test", pieces_dir, stamp)
        payload.update(messages_lus=1, records=result["records"], pieces_email=result["attachments"],
                       interventions=result["interventions"])
        atomic_json(output, payload)
        print(json.dumps({key: payload[key] for key in ("messages_lus", "pieces_email")}, ensure_ascii=False))
        return 0
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    payload["compte"] = address
    if not address or not password:
        payload["interventions"].append(intervention(
            "Connexion Gmail", "https://myaccount.google.com/apppasswords",
            "Le moteur GitHub ne possède pas encore les deux secrets Gmail nécessaires.",
            "Créer un mot de passe d'application Google puis enregistrer GMAIL_ADDRESS et GMAIL_APP_PASSWORD dans les secrets du dépôt.",
            "urgente"))
        atomic_json(output, payload)
        print("Connexion Gmail non configurée : intervention ajoutée au rapport.")
        return 2
    connection = None
    try:
        connection = connect(address, password)
        query = f'label:{NEW_LABEL} -label:{DONE_LABEL} newer_than:730d'
        status, data = connection.uid("SEARCH", None, "X-GM-RAW", f'"{query}"')
        if status != "OK":
            raise RuntimeError("Recherche Gmail refusée")
        uids = (data[0] or b"").split()[-args.max_messages:]
        for uid_bytes in uids:
            uid = uid_bytes.decode("ascii", errors="ignore")
            status, fetched = connection.uid("FETCH", uid, "(RFC822)")
            raw = next((item[1] for item in fetched or [] if isinstance(item, tuple)), None)
            if status != "OK" or not raw:
                payload["interventions"].append(intervention(
                    "Gmail", "https://mail.google.com/", f"Le message IMAP {uid} n'a pas pu être lu.",
                    "Ouvrir Gmail et vérifier si le message est toujours accessible."))
                continue
            result = process_message(raw, uid, pieces_dir, stamp)
            payload["messages_lus"] += 1
            payload["records"].extend(result["records"])
            payload["pieces_email"] += result["attachments"]
            payload["interventions"].extend(result["interventions"])
            add_label(connection, uid, result["label"])
    except (imaplib.IMAP4.error, OSError, RuntimeError) as exc:
        payload["interventions"].append(intervention(
            "Connexion Gmail", "https://mail.google.com/",
            f"La lecture automatique de Gmail a échoué : {type(exc).__name__}.",
            "Vérifier que le mot de passe d'application est encore valable et que l'accès IMAP est autorisé.",
            "urgente"))
    finally:
        if connection is not None:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    # Un URL peut figurer plusieurs fois dans un récapitulatif ; fusionner les
    # métadonnées e-mail sans exposer le contenu complet.
    merged = {}
    for record in payload["records"]:
        url = record["url_annonce"]
        if url in merged:
            old = merged[url]
            record["pieces_email"] = old.get("pieces_email", []) + record.get("pieces_email", [])
        merged[url] = record
    payload["records"] = list(merged.values())
    atomic_json(output, payload)
    print(json.dumps({"messages_lus": payload["messages_lus"], "dossiers_email": len(payload["records"]),
                      "pieces_email": payload["pieces_email"], "interventions": len(payload["interventions"])},
                     ensure_ascii=False))
    return 2 if payload["interventions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())


