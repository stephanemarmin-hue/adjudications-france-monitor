#!/usr/bin/env python3
"""Envoie à la boîte Gmail le résultat privé du traitement des alertes."""
from __future__ import annotations

import argparse
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def amount(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def render(payload):
    retained, unknown, ignored = [], [], 0
    for record in payload.get("records", []):
        price = amount(record.get("faits", {}).get("mise_a_prix_eur"))
        if price is not None and price > 60_000:
            ignored += 1
        elif price is None:
            unknown.append(record)
        else:
            retained.append(record)
    lines = ["Résultat privé des alertes d'adjudication reçues par Gmail", "",
             f"Messages traités : {payload.get('messages_lus', 0)}",
             f"Biens à 60 000 € maximum : {len(retained)}",
             f"Biens dont le prix doit être vérifié : {len(unknown)}",
             f"Biens écartés au-dessus de 60 000 € : {ignored}",
             f"Pièces jointes restées dans Gmail : {payload.get('pieces_email', 0)}", ""]
    for heading, rows in (("BIENS RETENUS", retained), ("PRIX À VÉRIFIER", unknown)):
        if not rows:
            continue
        lines += [heading, "=" * len(heading), ""]
        for index, record in enumerate(rows, 1):
            facts = record.get("faits", {})
            price = amount(facts.get("mise_a_prix_eur"))
            lines += [f"{index}. {record.get('titre') or 'Annonce sans titre'}",
                      f"Mise à prix : {price:,.2f} €".replace(",", " ") if price is not None else "Mise à prix : à vérifier",
                      f"Date de l'enchère : {facts.get('date_vente') or 'à vérifier'}",
                      f"Lien public : {record.get('url_annonce')}",
                      f"Pièces : {len(record.get('pieces_email', []))} conservée(s) dans Gmail", ""]
    interventions = payload.get("interventions", [])
    if interventions:
        lines += ["INTERVENTIONS HUMAINES", "======================", ""]
        for index, item in enumerate(interventions, 1):
            lines += [f"{index}. {item.get('source')}", item.get("probleme", ""),
                      f"Action : {item.get('action', '')}", f"Lien : {item.get('url', '')}", ""]
    return "\n".join(lines), len(retained), len(unknown), len(interventions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(ROOT / "data" / "gmail_annonces.json"))
    args = parser.parse_args()
    path = Path(args.input)
    if not path.exists():
        print("Aucun résultat Gmail à envoyer.")
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    body, retained, unknown, interventions = render(payload)
    if not retained and not unknown and not interventions:
        print("Aucune nouvelle alerte Gmail utile.")
        return 0
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    if not address or not password:
        print("Résumé privé non envoyé : secrets Gmail absents.")
        return 2
    message = EmailMessage()
    message["From"] = address
    message["To"] = address
    message["Subject"] = f"[MOTEUR ADJUDICATIONS] {retained} bien(s) retenu(s), {unknown} à vérifier"
    message.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=45) as smtp:
            smtp.login(address, password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"Envoi du résumé Gmail impossible : {type(exc).__name__}")
        return 2
    print("Résumé privé Gmail envoyé.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
