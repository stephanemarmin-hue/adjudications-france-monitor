"""Critères prudents de sélection, sans donnée inventée."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
MONTHS = dict(zip(
    "janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre".split(),
    range(1, 13),
))
DATE_PATTERN = r"\b(?:\d{1,2}(?:er)?\s+(?:" + "|".join(MONTHS) + r")\s+\d{4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b"
MONEY_PATTERN = r"([\d][\d\s\u00a0\u202f.,]*?)\s*(?:€|euros?\b)"


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).casefold()
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).replace("’", "'").split())


def french_date(text: str) -> date | None:
    match = re.search(DATE_PATTERN, normalize(text))
    if not match:
        return None
    value = match.group()
    try:
        if re.search(r"[-/.]", value):
            d, m, y = map(int, re.split(r"[-/.]", value))
            return date(y + 2000 if y < 100 else y, m, d)
        d, month, y = value.split()
        d = d.removesuffix("er")
        return date(int(y), MONTHS[month], int(d))
    except (ValueError, KeyError):
        return None


def amount(value: str | int | float | Decimal | None) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    value = re.sub(r"[^\d.,+-]", "", str(value))
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    elif value.count(".") > 1 or re.fullmatch(r"\d{1,3}(?:\.\d{3})+", value):
        value = value.replace(".", "")
    try:
        result = Decimal(value)
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def labelled_amount(text: str, label: str) -> str | None:
    match = re.search(label + r"\s*:?\s*" + MONEY_PATTERN, normalize(text))
    result = amount(match.group(1)) if match else None
    return str(result) if result is not None else None


def labelled_date(text: str, label: str) -> str | None:
    match = re.search(label + r"\s*:?\s*(?:[a-z]+\s+)?(" + DATE_PATTERN + ")", normalize(text))
    result = french_date(match.group(1)) if match else None
    return result.isoformat() if result else None


def asserted_matches(text: str, pattern: str, *, future=False) -> list[str]:
    """Écarter les mentions niées ou hypothétiques dans leur propre proposition."""
    matches = []
    for match in re.finditer(pattern, text):
        before = re.split(r"[.;:!?\n,]", text[:match.start()])[-1][-80:]
        after = text[match.end():match.end() + 45]
        if re.search(r"\b(?:pas|non|aucun(?:e|s|es)?|sans|ni|plus|absence de)\b[^.;:!?\n,]{0,45}$", before):
            continue
        if after.lstrip().startswith("?"):
            continue
        if future and (re.search(r"\b(?:sera(?:ient)?|seront|serait|etait|etaient|anciennement|auparavant|pourrait|devrait|deviendra|si|hypothese|sous reserve)\b[^.;:!?\n,]{0,35}$", before)
                       or re.match(r"\s+(?:apres|a partir|a compter|a l'issue|sous reserve)\b", after)):
            continue
        matches.append(match.group())
    return matches


def occupancy(text: str, *, labels=()) -> tuple[str, list[str]]:
    """Les libellés courts sont fournis uniquement depuis la description du bien."""
    original = normalize(text)
    # Les retours de ligne d'un PDF peuvent couper une phrase ou sa négation.
    text = original
    subject = r"(?:biens?|lieux|logements?|appartements?|maisons?|pavillons?|locaux|immeubles?)"
    patterns = {
        "occupe": rf"\b(?:{subject} (?:est |sont )?occupes?|occupes? par|occupation\s*:?\s*(?:bien )?occupe|occupe sans droit|actuellement occupe)\b",
        "loue": rf"\b(?:{subject} (?:est |sont )?loues?|loue depuis|loue suivant|donne en location|occupation\s*:?\s*(?:bien )?loue|bail en cours|occupe par (?:un |le |des |les )?locataires?)\b",
        "libre": rf"\b(?:libres? de toute occupation|{subject} (?:est|sont) (?:libres?|vacants?)|occupation\s*:?\s*(?:libre|vacant))\b",
    }
    evidence = {k: asserted_matches(text, p, future=True) for k, p in patterns.items()}
    for label, context in labels:
        kind = {"occupe": "occupe", "loue": "loue", "libre": "libre", "vacant": "libre"}.get(normalize(label))
        if kind and asserted_matches(normalize(context), rf"\b{re.escape(normalize(label))}\b", future=True):
            evidence[kind].append(normalize(label))
    if evidence["libre"] and (evidence["occupe"] or evidence["loue"]):
        return "contradictoire", sum(evidence.values(), [])
    for kind in ["loue", "occupe", "libre"]:
        if evidence[kind]:
            return kind, list(dict.fromkeys(evidence[kind]))
    if re.search(r"(?:pas|non)\s+libre|libre\s+(?:a partir|apres|a l'issue)|sera(?:ient)?\s+libre", original):
        return "a_verifier", ["libération future, conditionnelle ou niée"]
    return "inconnue", []


def works(text: str) -> tuple[str, list[str]]:
    pattern = (r"en cours de construction|travaux (?:a prevoir|necessaires|requis|obligatoires)|"
               r"(?:a|doit etre) renover|etat (?:general )?degrade|inhabitable|"
               r"materiaux de finition a renover|renovation (?:complete|necessaire)|"
               r"(?:tres )?mauvais etat|etat (?:general )?(?:tres )?mauvais|"
               r"presence de moisissures|fissures significatives|equipements (?:sanitaires )?deposes")
    normalized = normalize(text)
    evidence = asserted_matches(normalized, pattern, future=True)
    return ("necessaires", evidence) if evidence else ("a_verifier", [])


def classify(record: dict, now: datetime, valuation: dict | None = None) -> dict:
    today = now.astimezone(PARIS).date()
    facts = record.get("faits", {})
    result = {**record, "motifs_rejet": [], "a_verifier": [], "indices_exclusion": []}
    rejected, missing = result["motifs_rejet"], result["a_verifier"]
    sale = None
    if facts.get("date_vente"):
        try:
            sale = date.fromisoformat(facts["date_vente"])
        except ValueError:
            pass
    age = (today - sale).days if sale else None
    result["jours_apres_audience"] = age
    result["jours_avant_enchere"] = (sale - today).days if sale else None

    starting_price = amount(facts.get("mise_a_prix_eur"))
    if starting_price is None:
        missing.append("prix de départ")
    elif starting_price > Decimal("60000"):
        rejected.append("prix de départ supérieur à 60 000 €")
    if facts.get("hors_metropole"):
        rejected.append("hors France métropolitaine")
    if facts.get("retiree"):
        rejected.append("vente retirée, reportée ou non requise")
    if facts.get("vente_amiable"):
        rejected.append("vente amiable, hors scénario d'adjudication")
    if facts.get("occupation") in {"occupe", "loue", "contradictoire"}:
        rejected.append("occupation incompatible ou contradictoire")
    elif facts.get("occupation") != "libre":
        missing.append("preuve d'un bien libre")

    # Une pièce rattachée au dossier peut écarter un bien occupé, mais elle ne
    # valide jamais à elle seule que le bien est libre. Les mentions de travaux,
    # de prix adjugé ou de surenchère restent informatives et non bloquantes.
    attached_urls = set(record.get("documents", [])) | {d.get("url") for d in record.get("documents_extraits", [])}
    for proof in record.get("preuves_documentaires", []):
        if proof.get("url") not in attached_urls or proof.get("methode") not in {"texte", "ocr"}:
            continue
        try:
            checked = datetime.fromisoformat(proof.get("controle_acces_utc", ""))
            recent = checked.tzinfo is not None and 0 <= (today - checked.astimezone(PARIS).date()).days <= 7
        except (ValueError, TypeError):
            recent = False
        if not recent:
            continue
        field, value = proof.get("champ"), proof.get("valeur")
        negative = field == "occupation" and value in {"occupe", "loue", "contradictoire"}
        if negative:
            rejected.append(f"indice documentaire incompatible : {field}={value}, p. {proof.get('page', '?')} ({proof['methode']}) — identité et actualité à contrôler")
            result["indices_exclusion"].append({key: proof.get(key) for key in ["champ", "valeur", "url", "page", "methode", "controle_acces_utc"]})
    if record.get("erreur_detail"):
        rejected.append("fiche inaccessible lors de ce passage")

    # Une validation manuelle récente peut confirmer l'occupation, sans exiger
    # les anciens champs financiers, d'état du bien ou de délai de surenchère.
    # Elle ne remplace jamais la mise à prix publiée, qui reste obligatoire.
    if valuation:
        validation_ok = False
        try:
            checked = date.fromisoformat(valuation.get("date_verification", ""))
            validation_ok = 0 <= (today - checked).days <= 7
        except (ValueError, TypeError):
            pass
        if (validation_ok and valuation.get("source_occupation")
                and valuation.get("occupation") == "libre"
                and facts.get("occupation") not in {"occupe", "loue", "contradictoire"}):
            missing = [item for item in missing if item != "preuve d'un bien libre"]

    result["motifs_rejet"] = list(dict.fromkeys(rejected))
    result["a_verifier"] = list(dict.fromkeys(missing))
    result["statut"] = "rejete" if rejected else ("a_verifier" if result["a_verifier"] else "retenu_sur_donnees_validees")
    result["niveau_preuve"] = ("occupation libre et prix de départ ≤ 60 000 € confirmés"
                                if result["statut"] == "retenu_sur_donnees_validees"
                                else "C — publication à contrôler")
    return result
