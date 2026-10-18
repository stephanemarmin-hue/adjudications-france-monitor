"""Accès public borné et délais persistants, sans contournement des refus."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from monitor import MAX_BYTES, TIMEOUT_SECONDS


REQUEST_HEADERS = {
    "User-Agent": "AdjudicationsFranceMonitor/0.3 (veille de pages publiques; contact via le dépôt GitHub)",
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.7",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    "Cache-Control": "no-cache",
}


def utc_iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def retry_after(value, now, default):
    """Le délai du serveur peut être un nombre de secondes ou une date HTTP."""
    if value:
        try:
            return max(60, int(value.strip()))
        except (ValueError, AttributeError):
            try:
                day = parsedate_to_datetime(value)
                if day.tzinfo is None:
                    day = day.replace(tzinfo=timezone.utc)
                return max(60, day.timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
    return default


class AccessDeferred(Exception):
    def __init__(self, host, restriction):
        self.until = restriction["attendre_jusqua_utc"]
        self.code = restriction["code_http"]
        super().__init__(f"Accès différé à {host} jusqu'au {self.until} après HTTP {self.code}")


class PdfDocument(Exception):
    """Le contenu est un PDF ; les octets restent dans le cache du passage."""


class Fetcher:
    def __init__(self, fixture_map=None, pause=0.8, access_state=None, clock=None, retries=2, sleeper=None):
        self.fixture_map, self.pause = fixture_map, pause
        self.access_state = access_state if access_state is not None else {}
        self.clock = clock or time.time
        self.retries = max(0, retries)
        self.sleeper = sleeper or time.sleep
        self.locks, self.last_request, self.memo = {}, {}, {}
        self.guard = threading.Lock()
        self.metrics = {"requetes_http": 0, "reponses_429": 0, "acces_differes": 0,
                        "tentatives_reprises": 0, "reprises_reussies": 0}

    def _count(self, name):
        with self.guard:
            self.metrics[name] += 1

    def _resource(self, url):
        if self.fixture_map is not None:
            path = self.fixture_map.get(url)
            if not path:
                raise ValueError("page non fournie dans le jeu de vérification")
            body = Path(path).read_bytes()
            kind = "application/pdf" if body.startswith(b"%PDF-") else "text/html"
            return body, url, kind, "utf-8"
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if urlparse(url).scheme not in {"http", "https"} or not host:
            raise ValueError("URL publique HTTP(S) requise")
        with self.guard:
            lock = self.locks.setdefault(host, threading.Lock())
        with lock:
            if url in self.memo:
                return self.memo[url]
            restriction = self.access_state.get(host)
            if restriction:
                try:
                    until = datetime.fromisoformat(restriction["attendre_jusqua_utc"]).timestamp()
                except (KeyError, ValueError, TypeError):
                    until = 0
                if until > self.clock():
                    self._count("acces_differes")
                    raise AccessDeferred(host, restriction)
            wait = self.pause - (time.monotonic() - self.last_request.get(host, 0))
            if wait > 0:
                self.sleeper(wait)
            self.last_request[host] = time.monotonic()
            retried = False
            for attempt in range(self.retries + 1):
                request = Request(url, headers=REQUEST_HEADERS)
                self._count("requetes_http")
                try:
                    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                        resolved = response.geturl()
                        kind = response.headers.get("Content-Type", "") or ""
                        body = response.read(MAX_BYTES + 1)
                        if len(body) > MAX_BYTES:
                            raise ValueError("page ou document dépassant 10 Mo")
                        encoding = response.headers.get_content_charset() or "utf-8"
                    if retried:
                        self._count("reprises_reussies")
                    break
                except HTTPError as exc:
                    # Une erreur serveur peut être ponctuelle ; 403 et 429 ne sont jamais forcées.
                    if 500 <= exc.code <= 599 and attempt < self.retries:
                        retried = True
                        self._count("tentatives_reprises")
                        self.sleeper(min(4, 2 ** attempt))
                        continue
                    if exc.code == 429 or exc.code == 403 or 500 <= exc.code <= 599:
                        previous = (restriction or {}).get("echecs_consecutifs", 0)
                        fallback = 21600 if exc.code == 403 else (3600 if exc.code == 429 else min(3600, 300 * 2 ** min(previous, 4)))
                        delay = retry_after(exc.headers.get("Retry-After") if exc.headers else None, self.clock(), fallback)
                        # Un refus 403 n'est pas réessayé immédiatement, même sans Retry-After.
                        if exc.code == 403:
                            delay = max(21600, delay)
                        self.access_state[host] = {"attendre_jusqua_utc": utc_iso(self.clock() + delay),
                                                   "code_http": exc.code, "dernier_echec_utc": utc_iso(self.clock()),
                                                   "echecs_consecutifs": previous + 1}
                        if exc.code == 429:
                            self._count("reponses_429")
                    raise
                except (URLError, TimeoutError, ConnectionError, RemoteDisconnected) as exc:
                    if attempt >= self.retries:
                        # Après les reprises, différer tout le domaine plutôt que
                        # d'attendre le même délai sur des centaines de profils.
                        previous = (restriction or {}).get("echecs_consecutifs", 0)
                        self.access_state[host] = {
                            "attendre_jusqua_utc": utc_iso(self.clock() + 900),
                            "code_http": "reseau", "dernier_echec_utc": utc_iso(self.clock()),
                            "echecs_consecutifs": previous + 1, "derniere_erreur": str(exc),
                        }
                        raise
                    retried = True
                    self._count("tentatives_reprises")
                    self.sleeper(min(4, 2 ** attempt))
            self.access_state.pop(host, None)
            self.memo[url] = body, resolved, kind, encoding
            return self.memo[url]

    def get(self, url):
        body, resolved, kind, encoding = self._resource(url)
        if "application/pdf" in kind.lower() or body.startswith(b"%PDF-"):
            raise PdfDocument("PDF accessible — lecture documentaire")
        sample = body.lstrip()[:1024].lower()
        looks_like_html = any(marker in sample for marker in (b"<!doctype html", b"<html", b"<head", b"<body", b"<main"))
        if "html" not in kind.lower() and not looks_like_html:
            raise ValueError("contenu non HTML : " + kind)
        try:
            return body.decode(encoding, errors="replace"), resolved
        except LookupError:
            return body.decode("utf-8", errors="replace"), resolved

    def get_pdf(self, url):
        body, resolved, _, _ = self._resource(url)
        if not body.startswith(b"%PDF-"):
            raise ValueError("le lien ne renvoie pas un PDF lisible")
        return body, resolved


def incident(exc, url, phase):
    result = {"url": url, "phase": phase, "erreur": str(exc)}
    if isinstance(exc, AccessDeferred):
        result.update(differe=True, nouvel_essai_utc=exc.until, code_http=exc.code)
    elif isinstance(exc, HTTPError):
        result["code_http"] = exc.code
    return result
