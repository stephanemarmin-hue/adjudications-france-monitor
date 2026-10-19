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
from http.client import RemoteDisconnected
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from acces import AccessDeferred, Fetcher, PdfDocument, retry_after
from pieces import PdfReader, page_evidence, summarize_evidence
from qualification import classify
from monitor import is_candidate_link
from optimise_sources import optimise_rows
from test_veille import DETAIL, LIST, NOW, SOURCE, URL, record
from veille import (count_detail_for_selected_sources, coverage_audit, discover_source_links,
                    document_history, network_order, network_order_urls, pagination_pages, run,
                    select_sources, source_interventions)

PDF_URL = "https://avoventes.fr/public/document.pdf"
PDF_TEXT = "Vente le 6 octobre 2026. Mise à prix : 10 000 euros. Le bien est libre de toute occupation. Frais préalables : 4 250,00 euros."


class Response:
    def __init__(self, body, kind="text/html"):
        self.body = body
        self.headers = Message()
        if kind is not None:
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

    def test_temporary_disconnect_is_retried_then_succeeds(self):
        fetcher = Fetcher(pause=0, retries=2, sleeper=lambda _: None)
        with patch("acces.urlopen", side_effect=[RemoteDisconnected("coupure"), Response(b"<main>Reprise</main>")]) as network:
            content, _ = fetcher.get("https://exemple.fr/fiche")
        self.assertIn("Reprise", content)
        self.assertEqual(network.call_count, 2)
        self.assertEqual(fetcher.metrics["tentatives_reprises"], 1)
        self.assertEqual(fetcher.metrics["reprises_reussies"], 1)

    def test_exhausted_network_failure_defers_rest_of_same_domain(self):
        state = {}
        fetcher = Fetcher(pause=0, access_state=state, retries=0, clock=lambda: 1000)
        with patch("acces.urlopen", side_effect=TimeoutError("délai dépassé")) as network:
            with self.assertRaises(TimeoutError):
                fetcher.get("https://exemple.fr/a")
            with self.assertRaises(AccessDeferred):
                fetcher.get("https://exemple.fr/b")
        self.assertEqual(network.call_count, 1)
        self.assertEqual(state["exemple.fr"]["attendre_jusqua_utc"], "1970-01-01T00:31:40+00:00")
        self.assertEqual(state["exemple.fr"]["code_http"], "reseau")

    def test_html_without_content_type_is_accepted_by_signature(self):
        fetcher = Fetcher(pause=0)
        with patch("acces.urlopen", return_value=Response(b"<!doctype html><main>Annonce</main>", None)):
            content, _ = fetcher.get("https://exemple.fr/fiche")
        self.assertIn("Annonce", content)


class SourceOptimisationTests(unittest.TestCase):
    def test_all_rows_are_active_with_useful_cadences(self):
        rows = [
            {"nom": "Licitor", "categorie": "site_pro", "url": "https://www.licitor.com/", "active": "1"},
            {"nom": "Référentiel national — licitor.com", "categorie": "avocat", "url": "https://www.licitor.com/avocat/1", "active": "1"},
            {"nom": "Référentiel national — lannuaire.service-public.gouv.fr", "categorie": "juridiction_greffe", "url": "https://lannuaire.service-public.gouv.fr/tribunal/1", "active": "1"},
        ]
        output, counts = optimise_rows(rows)
        self.assertEqual(counts, {"veille_quotidienne": 1, "veille_rapide": 1,
                                  "decouverte_nationale": 1, "non_collectees": 0, "total": 3})
        self.assertEqual([row["active"] for row in output], ["1", "1", "1"])
        self.assertEqual([row["usage"] for row in output],
                         ["veille_quotidienne", "veille_rapide", "decouverte_nationale"])

    def test_non_collectable_references_stay_documented_but_inactive(self):
        rows = [
            {"nom": "Accès privé", "categorie": "site_pro", "url": "https://prive.fr/",
             "active": "1", "usage": "acces_authentifie", "motif": "Connexion requise"},
            {"nom": "Hors France", "categorie": "journal", "url": "https://hors-france.test/",
             "active": "1", "usage": "hors_perimetre", "motif": "Hors France"},
        ]
        output, counts = optimise_rows(rows)
        self.assertEqual([row["active"] for row in output], ["0", "0"])
        self.assertEqual(counts["non_collectees"], 2)

    def test_new_direct_sources_only_accept_property_detail_pages(self):
        self.assertTrue(is_candidate_link(
            "https://encheresimmobilieres.fr/biens-en-vente",
            "https://encheresimmobilieres.fr/ventes/9601-appartement-exemple", "Appartement"))
        self.assertTrue(is_candidate_link(
            "https://avocats-valdemarne.com/services/ventes-immobilieres",
            "https://avocats-valdemarne.com/services/ventes-immobilieres/1084-maison-1008", "Maison"))
        self.assertFalse(is_candidate_link(
            "https://tribuca.net/", "https://tribuca.net/credit-immobilier", "Crédit immobilier"))

    def test_scheduler_scans_direct_pages_and_rotates_oldest_sources(self):
        rows = [
            {"nom": "Direct", "url": "https://direct.fr/ventes", "active": True, "usage": "veille_quotidienne"},
            {"nom": "Profil A", "url": "https://profils.fr/a", "active": True, "usage": "veille_rapide"},
            {"nom": "Profil B", "url": "https://profils.fr/b", "active": True, "usage": "veille_rapide"},
            {"nom": "Greffe A", "url": "https://greffes.fr/a", "active": True, "usage": "decouverte_nationale"},
            {"nom": "Greffe B", "url": "https://greffes.fr/b", "active": True, "usage": "decouverte_nationale"},
        ]
        state = {"tentatives_sources": {"https://profils.fr/a": "2026-10-01T00:00:00+00:00",
                                         "https://profils.fr/b": "2026-10-02T00:00:00+00:00",
                                         "https://greffes.fr/a": "2026-09-01T00:00:00+00:00"}}
        selected, stats = select_sources(rows, state, NOW, max_profiles=1, max_directories=1,
                                         max_discovered=0, profile_interval_hours=6,
                                         directory_interval_hours=168)
        self.assertEqual({row["url"] for row in selected},
                         {"https://direct.fr/ventes", "https://profils.fr/a", "https://greffes.fr/b"})
        self.assertEqual(stats["veille_rapide_en_retard"], 1)
        self.assertEqual(stats["decouverte_nationale_en_attente"], 1)

    def test_network_order_keeps_priority_and_interleaves_domains(self):
        rows = [
            {"url": "https://a.fr/direct", "usage": "veille_quotidienne"},
            {"url": "https://a.fr/profil-1", "usage": "veille_rapide"},
            {"url": "https://a.fr/profil-2", "usage": "veille_rapide"},
            {"url": "https://b.fr/profil", "usage": "veille_rapide"},
            {"url": "https://c.fr/profil", "usage": "veille_rapide"},
        ]
        ordered = network_order(rows)
        self.assertEqual(ordered[0]["url"], "https://a.fr/direct")
        self.assertEqual([row["url"] for row in ordered[1:]], [
            "https://a.fr/profil-1", "https://b.fr/profil", "https://c.fr/profil",
            "https://a.fr/profil-2",
        ])

    def test_network_order_urls_interleaves_detail_hosts(self):
        urls = ["https://a.fr/1", "https://a.fr/2", "https://b.fr/1", "https://c.fr/1"]
        self.assertEqual(network_order_urls(urls), [
            "https://a.fr/1", "https://b.fr/1", "https://c.fr/1", "https://a.fr/2",
        ])

    def test_detail_count_ignores_sources_not_selected_this_passage(self):
        logs = {"https://selected.fr": {"fiches_lues": 0}}
        sources = [{"url": "https://old-source.fr"}, {"url": "https://selected.fr"}]
        count_detail_for_selected_sources(logs, sources, True)
        self.assertEqual(logs["https://selected.fr"]["fiches_lues"], 1)

    def test_directory_discovers_external_official_site_and_sale_page(self):
        source = {"url": "https://annuaire.fr/tribunal", "usage": "decouverte_nationale"}
        html = '''<a href="https://tribunal.fr/">Site internet</a>
                  <a href="https://cabinet.fr/ventes-aux-encheres">Ventes aux enchères immobilières</a>
                  <a href="https://facebook.com/tribunal">Site internet</a>
                  <a href="/interne">Site internet</a>'''
        links = discover_source_links(html, source["url"], source, NOW.isoformat())
        self.assertEqual({row["url"] for row in links},
                         {"https://tribunal.fr/", "https://cabinet.fr/ventes-aux-encheres"})

    def test_coverage_is_not_claimed_without_independent_sample(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "controle.csv"
            path.write_text("date_publication,url_reference,url_detectee,detectee,statut\n", encoding="utf-8")
            self.assertEqual(coverage_audit(path, set())["statut"], "non_mesure")
            path.write_text("date_publication,url_reference,url_detectee,detectee,statut\n"
                            "2026-10-01,https://reference.fr/a,https://moteur.fr/a,oui,\n"
                            "2026-10-02,https://reference.fr/b,,,\n", encoding="utf-8")
            audit = coverage_audit(path, {"https://reference.fr/b"})
            self.assertEqual((audit["echantillon"], audit["detectees"], audit["taux_pct"]), (2, 2, 100.0))
            self.assertFalse(audit["objectif_95"])

    def test_pagination_follows_only_explicit_same_site_next_link(self):
        page = "https://exemple.fr/ventes?page=1"
        html = '''<a href="/ventes?page=2" rel="next">2</a>
                  <a href="/ventes?page=3">3</a>
                  <a href="https://autre.fr/ventes" rel="next">Suivant</a>'''
        self.assertEqual(pagination_pages(html, page), ["https://exemple.fr/ventes?page=2"])

    def test_intervention_is_immediate_for_dead_link_and_delayed_for_transient_problem(self):
        state = {}
        dead = {"nom": "Cabinet", "url": "https://exemple.fr/ancien", "statut": "echec",
                "liens_detectes": 0, "erreur": "HTTP Error 404: Not Found"}
        alerts = source_interventions(state, [dead], NOW.isoformat())
        self.assertEqual(alerts[0]["priorite"], "urgente")
        self.assertEqual(alerts[0]["url"], dead["url"])

        state = {}
        temporary = {**dead, "url": "https://exemple.fr/lent", "erreur": "timed out"}
        self.assertEqual(source_interventions(state, [temporary], NOW.isoformat()), [])
        self.assertEqual(source_interventions(state, [temporary], NOW.isoformat()), [])
        self.assertEqual(len(source_interventions(state, [temporary], NOW.isoformat())), 1)
        healthy = {**temporary, "statut": "ok", "liens_detectes": 2, "erreur": ""}
        self.assertEqual(source_interventions(state, [healthy], NOW.isoformat()), [])
        self.assertEqual(state["sante_sources"][temporary["url"]]["echecs_consecutifs"], 0)

    def test_reference_failures_are_grouped_by_domain(self):
        logs = [{"nom": f"Profil {index}", "url": f"https://profils.fr/{index}", "usage": "veille_rapide",
                 "statut": "echec", "liens_detectes": 0, "erreur": "HTTP Error 404: Not Found"}
                for index in range(3)]
        alerts = source_interventions({}, logs, NOW.isoformat())
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["nombre_sources"], 3)
        self.assertIn("3 adresse(s)", alerts[0]["probleme"])


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
            (root / "liste.html").write_text(LIST, encoding="utf-8")
            (root / "fiche.html").write_text(DETAIL, encoding="utf-8")
            (root / "piece.pdf").write_bytes(b"%PDF-1.4\nfixture")
            mapping = {SOURCE["url"]: str(root / "liste.html"), URL: str(root / "fiche.html"), PDF_URL: str(root / "piece.pdf")}
            (root / "mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
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
