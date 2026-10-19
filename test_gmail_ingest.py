import email
import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

import gmail_ingest
import gmail_digest


class GmailIngestTests(unittest.TestCase):
    def test_single_auction_is_parsed(self):
        message = EmailMessage()
        message["From"] = "Alertes <noreply@avoventes.fr>"
        message["To"] = "alerts@example.test"
        message["Subject"] = "Nouvelle vente aux enchères"
        message["Message-ID"] = "<vente-1@example.test>"
        message.set_content("Appartement à Lille. Mise à prix : 36 000 €. Vente le 25 octobre 2026. https://avoventes.fr/enchere/appartement-1")
        with tempfile.TemporaryDirectory() as folder:
            result = gmail_ingest.process_message(message.as_bytes(), "1", Path(folder), "2026-10-19T12:00:00+00:00")
        self.assertEqual(result["label"], gmail_ingest.DONE_LABEL)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["faits"]["mise_a_prix_eur"], "36000.00")
        self.assertEqual(result["records"][0]["faits"]["date_vente"], "2026-10-25")

    def test_confirmation_requests_human_help(self):
        message = EmailMessage()
        message["From"] = "bonjour@encheres-publiques.com"
        message["Subject"] = "Action requise pour confirmer votre compte Membre"
        message["Message-ID"] = "<confirm@example.test>"
        message.set_content("Veuillez valider votre inscription.")
        with tempfile.TemporaryDirectory() as folder:
            result = gmail_ingest.process_message(message.as_bytes(), "2", Path(folder), "2026-10-19T12:00:00+00:00")
        self.assertEqual(result["label"], gmail_ingest.HUMAN_LABEL)
        self.assertEqual(len(result["interventions"]), 1)
        self.assertFalse(result["records"])

    def test_multiple_prices_are_not_assigned_to_every_link(self):
        message = EmailMessage()
        message["From"] = "noreply@licitor.com"
        message["Subject"] = "Deux ventes judiciaires"
        message["Message-ID"] = "<digest@example.test>"
        message.set_content("Lot A 20 000 € https://www.licitor.com/ventes-judiciaires-immobilieres/a Lot B 30 000 € https://www.licitor.com/ventes-judiciaires-immobilieres/b")
        with tempfile.TemporaryDirectory() as folder:
            result = gmail_ingest.process_message(message.as_bytes(), "3", Path(folder), "2026-10-19T12:00:00+00:00")
        self.assertEqual(len(result["records"]), 2)
        self.assertTrue(all("mise_a_prix_eur" not in row["faits"] for row in result["records"]))

    def test_atomic_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.json"
            gmail_ingest.atomic_json(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})

    def test_private_digest_keeps_unknown_prices_for_review(self):
        payload = {"messages_lus": 2, "pieces_email": 1, "records": [
            {"titre": "Bien A", "url_annonce": "https://example.test/a",
             "faits": {"mise_a_prix_eur": "60000", "date_vente": "2026-11-02"}, "pieces_email": []},
            {"titre": "Bien B", "url_annonce": "https://example.test/b", "faits": {}, "pieces_email": []},
            {"titre": "Bien C", "url_annonce": "https://example.test/c",
             "faits": {"mise_a_prix_eur": "70000"}, "pieces_email": []},
        ], "interventions": []}
        body, retained, unknown, interventions = gmail_digest.render(payload)
        self.assertEqual((retained, unknown, interventions), (1, 1, 0))
        self.assertIn("Bien A", body)
        self.assertIn("Bien B", body)
        self.assertNotIn("Bien C", body)


if __name__ == "__main__":
    unittest.main()
