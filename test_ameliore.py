"""Délais persistants et pièces progressives ; aucun accès réseau pendant les tests."""
import argparse
import contextlib
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from email.utils import formatdate
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from acces import AccessDeferred, Fetcher, PdfDocument, retry_after
from pieces import PdfReader, page_evidence, summarize_evidence
from qualification import classify
from test_veille import DETAIL, LIST, NOW, SOURCE, URL, record
from veille import document_history, run

PDF_URL = "https://avoventes.fr/public/document.pdf"
PDF_TEXT = "Vente le 6 octobre 2026. Mise à prix : 10 000 euros. Le bien est libre de toute occupation. Frais préalables : 4 250,00 euros."


class Response:
    def __init__(self, body, kind="text/html"):
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = kind

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        return self.body[:limit]

    def geturl(self):
        return "https://exemple.fr/resultat"


def denied(code, after=None):
    headers = Message()
    if after is not None:
        headers["Retry-After"] = after
    return HTTPError("https://exemple.fr/fiche", code, "limite", headers, None)


class AccessTests(unittest.TestCase):
    def test_retry_after_seconds_http_date_and_invalid(self):
        self.assertEqual(retry_after("7200", 1000, 3600), 7200)
        self.assertEqual(retry_after(formatdate(8000, usegmt=True), 1000, 3600), 7000)
        self.assertEqual(retry_after("invalide", 1000, 3600), 3600)
        self.assertEqual(retry_after("0", 1000, 3600), 60)

    def test_429_stops_pending_requests_on_same_host(self):
        state = {}
        fetcher = Fetcher(pause=0, access_state=state, clock=lambda: 1000)
        def get(index):
            try:
                fetcher.get(f"https://exemple.fr/fiche-{index}")
            except Exception as exc:
                return type(exc)
        with patch("acces.urlopen", side_effect=denied(429, "7200")) as network:
            with ThreadPoolExecutor(max_workers=4) as executor:
                outcomes = list(executor.map(get, range(4)))
        self.assertEqual(network.call_count, 1)
        self.assertEqual(outcomes.count(HTTPError), 1)
        self.assertEqual(outcomes.count(AccessDeferred), 3)
        self.assertEqual(fetcher.metrics["reponses_429"], 1)
        self.assertIn("exemple.fr", state)

    def test_delay_persists_across_runs_and_then_expires(self):
        state = {}
        with patch("acces.urlopen", side_effect=denied(429, "600")):
            with self.assertRaises(HTTPError):
                Fetcher(pause=0, access_state=state, clock=lambda: 1000).get("https://exemple.fr/a")
        state = json.loads(json.dumps(state))
        with patch("acces.urlopen", return_value=Response(b"<main>Reprise</main>")) as network:
            with self.assertRaises(AccessDeferred):
                Fetcher(pause=0, access_state=state, clock=lambda: 1200).get("https://exemple.fr/b")
            self.assertEqual(network.call_count, 0)
            content, _ = Fetcher(pause=0, access_state=state, clock=lambda: 1601).get("https://exemple.fr/b")
        self.assertIn("Reprise", content)
        self.assertEqual(state, {})

    def test_denied_host_does_not_stop_other_sites(self):
        state = {}
        fetcher = Fetcher(pause=0, access_state=state, clock=lambda: 1000)
        with patch("acces.urlopen", side_effect=[denied(403), Response(b"<main>Autre source</main>")]) as network:
            with self.assertRaises(HTTPError):
                fetcher.get("https://exemple.fr/a")
            content, _ = fetcher.get("https://autre.fr/b")
        self.assertEqual(network.call_count, 2)
        self.assertIn("Autre source", content)
        self.assertEqual(state["exemple.fr"]["attendre_jusqua_utc"], "1970-01-01T06:16:40+00:00")

    def test_pdf_detected_by_signature_and_not_downloaded_twice(self):
        body = b"%PDF-1.4\ncontenu"
        fetcher = Fetcher(pause=0)
        with patch("acces.urlopen", return_value=Response(body, "application/octet-stream")) as network:
            with self.assertRaises(PdfDocument):
                fetcher.get("https://exemple.fr/document")
            self.assertEqual(fetcher.get_pdf("https://exemple.fr/document")[0], body)
        self.assertEqual(network.call_count, 1)


class DocumentTests(unittest.TestCase):
    def test_evidence_has_page_method_and_separate_prices(self):
        proof = page_evidence(PDF_TEXT, PDF_URL, 3, "texte")
        facts, conflicts = summarize_evidence(proof)
        self.assertEqual(facts["occupation"], "libre")
        self.assertEqual(facts["date_vente"], "2026-10-06")
        self.assertEqual(facts["frais_prealables_publies_eur"], "4250.00")
        self.assertNotIn("prix_adjuge_eur", facts)
        self.assertEqual(conflicts, {})
        self.assertTrue(all(p["page"] == 3 and p["url"] == PDF_URL and p["validation"] == "a_verifier" for p in proof))

    def test_negative_and_conflicting_occupation_remain_unverified(self):
        proof = page_evidence("Le bien n'est pas libre de toute occupation.", PDF_URL, 1, "ocr")
        self.assertEqual(proof[0]["valeur"], "a_verifier")
        other = page_evidence("Le bien est libre de toute occupation.", PDF_URL, 1, "texte")
        other += page_evidence("Le bien est occupé par un locataire.", PDF_URL, 2, "texte")
        facts, conflicts = summarize_evidence(other)
        self.assertNotIn("occupation", facts)
        self.assertIn("occupation", conflicts)

    @patch("pieces.shutil.which", return_value="outil")
    def test_page_budget_and_resumption_do_not_duplicate_evidence(self, _):
        reader = PdfReader(NOW.isoformat(), max_pages=1, max_ocr_pages=0)
        with patch.object(reader, "_run", side_effect=["Pages: 2\nEncrypted: no\n", PDF_TEXT]):
            first = reader.read(b"%PDF-1.4\nfixture", PDF_URL)
        self.assertEqual(first["statut"], "partiel")
        self.assertEqual(first["pages_lues"], [1])
        reader = PdfReader(NOW.isoformat(), max_pages=1, max_ocr_pages=0)
        with patch.object(reader, "_run", side_effect=["Pages: 2\nEncrypted: no\n", PDF_TEXT]) as process:
            second = reader.read(b"%PDF-1.4\nfixture", PDF_URL, first)
        self.assertEqual(second["statut"], "lu")
        self.assertEqual(second["pages_lues"], [1, 2])
        self.assertEqual(process.call_args_list[1].args[0][2], "2")
        self.assertEqual(len(second["preuves"]), 2 * len(first["preuves"]))

    @patch("pieces.shutil.which", return_value="outil")
    def test_ocr_budget_and_next_run_read_pending_scan(self, _):
        reader = PdfReader(NOW.isoformat(), max_pages=1, max_ocr_pages=0)
        with patch.object(reader, "_run", side_effect=["Pages: 1\n", ""]):
            first = reader.read(b"%PDF-1.4\nscan", PDF_URL)
        self.assertEqual(first["pages_lues"], [])
        self.assertEqual(first["pages_lues_ce_passage"], 0)
        self.assertEqual(first["pages_traitees_ce_passage"], 1)
        self.assertEqual(reader.metrics["pages_pdf_extraites"], 0)
        self.assertEqual(first["pages_restantes"], 1)
        reader = PdfReader(NOW.isoformat(), max_pages=1, max_ocr_pages=1)
        with patch.object(reader, "_run", return_value="Pages: 1\n"), patch.object(reader, "_ocr", return_value=PDF_TEXT):
            second = reader.read(b"%PDF-1.4\nscan", PDF_URL, first)
        self.assertEqual(second["statut"], "lu")
        self.assertEqual(second["pages_ocr_ce_passage"], 1)
        self.assertEqual(second["pages_lues_ce_passage"], 1)
        self.assertEqual(reader.metrics["pages_pdf_extraites"], 1)
        self.assertTrue(all(x["methode"] == "ocr" for x in second["preuves"]))

    @patch("pieces.shutil.which", return_value="outil")
    def test_changed_document_discards_previous_evidence(self, _):
        reader = PdfReader(NOW.isoformat(), max_pages=10, max_ocr_pages=0)
        with patch.object(reader, "_run", side_effect=["Pages: 1\n", PDF_TEXT]):
            first = reader.read(b"%PDF-1.4\nancien", PDF_URL)
        new_text = "Le bien est occupé par un locataire. Travaux nécessaires. " * 3
        with patch.object(reader, "_run", side_effect=["Pages: 1\n", new_text]):
            second = reader.read(b"%PDF-1.4\nnouveau", PDF_URL, first)
        self.assertFalse(second["cache_reutilise"])
        self.assertEqual(second["faits_candidats"]["occupation"], "loue")
        self.assertNotIn("date_vente", second["faits_candidats"])

    @patch("pieces.shutil.which", return_value="outil")
    def test_protected_document_is_not_processed(self, _):
        reader = PdfReader(NOW.isoformat())
        with patch.object(reader, "_run", return_value="Pages: 1\nEncrypted: yes\n") as process:
            with self.assertRaisesRegex(ValueError, "protégé"):
                reader.read(b"%PDF-1.4\nprotege", PDF_URL)
        self.assertEqual(process.call_count, 1)

    def test_document_evidence_never_validates_opportunity_alone(self):
        row = record()
        row["faits"]["occupation"] = "inconnue"
        row["preuves_documentaires"] = page_evidence(PDF_TEXT, PDF_URL, 1, "texte")
        selected = classify(row, NOW)
        self.assertEqual(selected["statut"], "a_verifier")
        self.assertIn("preuve d'un bien libre", selected["a_verifier"])

    def test_previous_proofs_remain_available_with_their_actual_control_date(self):
        records = {URL: {"documents": [PDF_URL]}}
        cache = {PDF_URL: {"empreinte_pdf": "hash", "url": PDF_URL, "statut": "lu", "pages_lues": [1],
                          "pages_total": 1, "controle_acces_utc": "2026-10-07T09:00:00+00:00",
                          "preuves": page_evidence(PDF_TEXT, PDF_URL, 1, "ocr"), "extractions": {}}}
        history = document_history(records, cache, set())
        self.assertFalse(history[0]["consulte_ce_passage"])
        self.assertTrue(records[URL]["preuves_documentaires"])
        self.assertEqual(records[URL]["preuves_documentaires"][0]["controle_acces_utc"], "2026-10-07T09:00:00+00:00")
        self.assertFalse(records[URL]["preuves_documentaires"][0]["consulte_ce_passage"])
        self.assertTrue(document_history(records, cache, {PDF_URL})[0]["consulte_ce_passage"])

    @patch("pieces.shutil.which", return_value="outil")
    def test_report_and_state_keep_pdf_page_proofs(self, _):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "liste.html").write_text(LIST)
            (root / "fiche.html").write_text(DETAIL)
            (root / "piece.pdf").write_bytes(b"%PDF-1.4\nfixture")
            mapping = {SOURCE["url"]: str(root / "liste.html"), URL: str(root / "fiche.html"), PDF_URL: str(root / "piece.pdf")}
            (root / "mapping.json").write_text(json.dumps(mapping))
            args = argparse.Namespace(now=NOW.isoformat(), fixtures=str(root / "mapping.json"), pause=0,
                                      state=str(root / "etat.json"), output=str(root / "rapports"), catalogue=str(root / "catalogue.json"),
                                      valuations=str(root / "estimations.csv"), max_hearings=5, max_details=5,
                                      max_documents=1, max_pdf_pages=5, max_ocr_pages=0)
            with patch("veille.load_sources", return_value=[SOURCE]), patch("pieces.PdfReader._run", side_effect=["Pages: 1\n", PDF_TEXT]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(args), 0)
            report = json.loads((root / "rapports/dernier.json").read_text())
            state = json.loads((root / "etat.json").read_text())
            self.assertEqual(report["comptages"]["documents_pdf_complets"], 1)
            self.assertEqual(report["comptages"]["retenus"], 1)
            self.assertEqual(report["dossiers"][0]["preuves_documentaires"][0]["page"], 1)
            self.assertIn(PDF_URL, state["pieces"])
            self.assertIn("Pièces PDF liées aux dossiers du jour", (root / "rapports/dernier.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
