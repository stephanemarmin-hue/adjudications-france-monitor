#!/usr/bin/env python3
"""Prépare et envoie une alerte e-mail quand une intervention humaine est requise."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=str(ROOT / "rapports" / "interventions.md"))
    parser.add_argument("--state", default=str(ROOT / "data" / "alertes_humaines.json"))
    args = parser.parse_args()
    report_path, state_path = Path(args.report), Path(args.state)
    if not report_path.exists():
        print("Aucun rapport d'intervention à envoyer.")
        return 0
    body = report_path.read_text(encoding="utf-8")
    if "Aucune intervention requise." in body:
        print("Aucune intervention humaine requise.")
        return 0
    signature = hashlib.sha256(body.encode("utf-8")).hexdigest()
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if previous.get("derniere_signature_envoyee") == signature:
        print("Alerte inchangée : aucun doublon envoyé.")
        return 0
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    if not address or not password:
        print("Alerte e-mail impossible tant que les secrets Gmail ne sont pas configurés ; l'alerte GitHub reste active.")
        return 2
    message = EmailMessage()
    message["From"] = address
    message["To"] = address
    message["Subject"] = "[ALERTE MOTEUR] Intervention humaine requise"
    message.set_content(body + "\n\nRapport en ligne :\nhttps://github.com/stephanemarmin-hue/adjudications-france-monitor/blob/main/rapports/interventions.md\n")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=45) as smtp:
            smtp.login(address, password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"Envoi de l'alerte impossible : {type(exc).__name__}")
        return 2
    atomic_json(state_path, {"derniere_signature_envoyee": signature})
    print("Alerte d'intervention envoyée par e-mail.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


