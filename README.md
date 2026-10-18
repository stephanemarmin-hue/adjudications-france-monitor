# Adjudications France — moteur de veille

Surveillance des pages publiques déclarées dans `sources.csv`, lecture bornée des fiches, filtres prudents et rapport actualisé sur GitHub.

## Exécution automatique

Le workflow **Veille adjudications France** s'exécute une fois par jour à **18 h, heure de Marrakech**, avec le fuseau `Africa/Casablanca` et ses changements d'heure. GitHub peut décaler légèrement une exécution programmée. Un passage peut aussi être lancé depuis **Actions → Veille adjudications France → Run workflow**.

Les changements du code ou des sources ne lancent pas une seconde collecte automatiquement : le passage quotidien reste unique, sauf lancement manuel depuis Actions.

- [Dernier rapport lisible](rapports/dernier.md)
- [Résultats structurés](rapports/dernier.json)
- [Tableau CSV](rapports/dernier.csv)
- [Journal des pages sources](rapports/sources.csv)
- [Exécutions et archives](https://github.com/stephanemarmin-hue/adjudications-france-monitor/actions)

Chaque exécution conserve le rapport, le catalogue de liens et l'état dans un artefact pendant 30 jours. Un cache permet de suivre les nouveautés et les modifications entre passages. Si le cache disparaît, la collecte et les filtres fonctionnent toujours ; le compteur de nouveautés repart de zéro.

Une tâche ChatGPT de bilan quotidien peut lire `rapports/dernier.json` et `rapports/dernier.md`. Le workflow ne constitue pas à lui seul une notification ChatGPT.

## Ce que le moteur vérifie

1. Il parcourt les **1 996 sources actives** de `sources.csv`, dont les **1 975 adresses distinctes** du référentiel national restauré. Certaines URL sont des annuaires, profils ou pages sans annonces ; le rapport distingue les pages accessibles et réellement productives.
2. Il détecte des liens d'audiences et de fiches. Les cartes Avoventes incluent date, prix publié et échéance lorsqu'ils sont affichés.
3. Il ouvre une nouvelle audience Licitor **dès que son lien est détecté**, puis suit les audiences déjà connues par rotation, dans la limite de **40 pages d'audience** par passage.
4. Il lit jusqu'à **120 fiches**, avec priorité aux nouveaux liens, puis reprend les fiches déjà connues selon leur dernière tentative. La date de l'enchère ne retarde jamais la détection. Les requêtes sont limitées à quatre traitements concurrents, avec au moins 0,8 seconde entre départs de requêtes d'un même site.
5. Il extrait les mentions explicites de date, mise à prix, prix adjugé, occupation et travaux. Pour la sélection automatique, le bien doit être libre et sa mise à prix publiée doit être inférieure ou égale à **60 000 €**.
6. Il publie un rapport même si des sources échouent. Une couverture partielle est signalée par un avertissement visible et le code de collecte 2. Une erreur d'extracteur ou l'absence de toute source exploitable entraîne un échec du workflow (code 1).
7. Il annexe à chaque bien **tous les liens de pièces détectés**, sans limite de nombre. Il consulte progressivement jusqu'à **12 PDF** par passage, traite jusqu'à **80 pages** dont **6 par OCR**, avec **8 pages au plus par document et par passage**. La lecture reprend les pages restantes aux passages suivants. Le contenu du PDF est identifié par son empreinte : une modification remet sa lecture à zéro. Les pièces des dossiers nouveaux ou enrichis sont prioritaires, puis la file tourne selon la dernière tentative.

Les indices PDF comportent **URL, numéro de page, méthode texte/OCR et extrait ciblé**. Ils sont conservés séparément des faits de l'annonce : plusieurs biens, dates ou prix peuvent figurer dans une pièce. Le rapport affiche les contradictions, pages restantes et erreurs dans ses données structurées. Une extraction ne confirme ni l'attribution d'une mention au bien, ni son actualité. Une occupation incompatible récente peut exclure un dossier ; les mentions de travaux ne le bloquent pas.

Les indices des passages précédents restent disponibles dans `historique_documents` et dans les fiches concernées, avec leur date réelle de contrôle et le marqueur `consulte_ce_passage`. Ils ne sont pas comptés comme des documents relus lors du passage actuel. Le rapport distingue pages techniquement traitées, pages effectivement lues et pages OCR réussies.

## Délais et refus d'accès

Une réponse **429** arrête les requêtes suivantes du même site et respecte `Retry-After`, en secondes ou en date HTTP. Sans délai indiqué, le moteur diffère ce site d'une heure. Les refus **403** sont différés au moins six heures. Les erreurs serveur **5xx** ont un délai croissant, ou le délai indiqué par le serveur. Les autres sites continuent à être consultés.

Ces délais sont conservés dans l'état et restaurés au passage suivant. Le rapport distingue erreurs reçues et accès différés, avec l'heure du prochain essai. Le moteur ne contourne pas les refus. Les données antérieures ne sont pas présentées comme une nouvelle lecture réussie.

## Critères de sélection

- Entrée dans le suivi dès la première détection de la publication, sans attendre la date de l'enchère.
- Mise à prix publiée obligatoire et inférieure ou égale à **60 000 €**.
- Bien libre avec preuve ; un bien occupé, loué ou contradictoire est rejeté.
- Une vente retirée, reportée, non requise ou amiable est rejetée.
- La date de l'enchère est affichée lorsqu'elle est publiée, mais son absence ou son éloignement ne bloque pas la sélection.
- Les travaux, le prix adjugé et la date de surenchère restent informatifs et ne bloquent pas la sélection.

Une occupation inconnue ou une mise à prix absente produit le statut `a_verifier`. Une mise à prix supérieure à 60 000 € produit le statut `rejete`. Le statut `retenu_sur_donnees_validees` signifie uniquement que l'occupation libre et le plafond de mise à prix sont confirmés. Il n'autorise aucune enchère automatiquement.

## Rapport quotidien et historique

Le rapport quotidien affiche uniquement les dossiers **détectés ou enrichis pendant le passage du jour**. Il mentionne la date de l'enchère, la mise à prix, les nouveaux éléments et tous les liens de pièces rattachés au dossier. Un jour sans changement produit donc un rapport vide, sans répéter les anciens résultats. Le catalogue et l'état conservent l'historique daté de chaque enrichissement pour les recherches ultérieures.

Le fichier `estimations.csv` peut confirmer manuellement une occupation libre lorsque la date de vérification et la source d'occupation ont moins de sept jours. La mise à prix doit toujours provenir de la publication du dossier.

## Installation et test local

Python **3.10 ou plus**, sans bibliothèque Python externe. Pour lire les pièces : **Poppler** (`pdfinfo`, `pdftotext`, `pdftoppm`) et **Tesseract**, avec les langues française et anglaise. Le workflow installe ces outils sur Ubuntu. Un outil absent, un PDF protégé, un document dépassant 10 Mo ou 300 pages reste signalé pour contrôle manuel.

```sh
python -m unittest -v test_veille test_ameliore
python veille.py
```

Le collecteur initial reste disponible avec `python monitor.py`. Le workflow utilise `veille.py`, qui ajoute la lecture des fiches, la qualification et les rapports.

Options utiles :

```sh
python veille.py --max-hearings 40 --max-details 120 --max-documents 12 --max-pdf-pages 80 --max-ocr-pages 6
python veille.py --now 2026-10-07T12:00:00+02:00 --fixtures correspondances.json
```

Le second mode lit un JSON `URL → chemin HTML ou PDF local` pour vérifier les extracteurs sans réseau. Ajouter `--max-documents 0` pour désactiver la lecture des pièces. Les tests couvrent les dates françaises, la détection immédiate, l'occupation libre ou incompatible, le plafond de mise à prix, l'annexion complète des pièces, la pagination, les erreurs, l'historique d'enrichissement et l'affichage quotidien limité aux nouveautés.

## Référentiel national restauré

Les fichiers [repertoire_greffes.csv](repertoire_greffes.csv) et [repertoire_avocats.csv](repertoire_avocats.csv) réintègrent la liste nationale collectée le 30 septembre 2026. Ils contiennent 1 053 fiches de juridictions/contacts et 1 690 fiches d’avocats, dont 706 marquées prioritaires. Les deux catalogues portent 3 536 références URL (1 975 adresses distinctes). Ils conservent aussi les types de contact et les liens de provenance.

Le répertoire des greffes comprend des fiches d’annuaire officiel, dont 157 tribunaux judiciaires, mais aussi des juridictions et contacts sans rapport direct avec les adjudications. Les fiches d’annuaire ne sont pas activées comme pages d’annonces. Parmi les références d’avocats, certaines sont des profils sur des plateformes, non des sites indépendants de cabinets. Vingt pages de vente de cabinets reprises dans la base sont incluses dans les sources actives; trois autres pages d’avocats déjà configurées restent également suivies. Les adresses restaurées sont à contrôler au fil des collectes; leur présence dans le catalogue ne garantit pas qu’elles soient encore accessibles ou à jour.

## Limites de couverture

Le référentiel national conserve 1 053 fiches de juridictions et 1 690 fiches d’avocats, dont 706 références prioritaires. Ces fiches sont des pistes et contacts documentés ; elles ne signifient pas que 1 690 cabinets disposent d’une page de ventes distincte, ni que tous les greffes publient des annonces. Les 1 996 URL actives sont parcourues dans les budgets du moteur ; cette liste ne constitue pas un balayage exhaustif de tout le web ou des annonces. Le rapport affiche les lectures réellement effectuées et les erreurs d’accès.

La déduplication porte sur les URL. Deux publications du même bien sur deux sites peuvent rester distinctes ; elles ne doivent pas être comptées comme deux opportunités indépendantes sans contrôle. `Registre_Adjudications.xlsx` et la base nationale restent les outils de consolidation : ce programme ne les modifie pas.

Le dépôt est public. Il ne doit contenir ni credentials, ni coordonnées privées, ni document confidentiel : uniquement le code, les sources publiques et les résultats publics de la collecte.

## Contrôle de l'occupation

Les libellés courts `LIBRE`, `Occupé` et `Loué` sont lus dans les blocs de description du lot reconnus sur Licitor, Avoventes et Vench ; les menus et liens ne fournissent pas ces preuves. Les avis indiquant « les lieux sont occupés » ou « loués et occupés » sont détectés. Les négations et les mentions anciennes, futures ou conditionnelles restent à vérifier.

Une pièce PDF rattachée au dossier et contrôlée depuis au plus sept jours peut écarter un candidat si elle indique une occupation incompatible. Le motif conserve la page et la méthode texte/OCR, et précise que l'identité du lot et l'actualité du document restent à contrôler. Une mention de travaux est conservée comme information, sans exclusion. Après un changement des règles d'extraction, les pages déjà traitées avec l'ancienne version sont relues progressivement dans les mêmes budgets.
