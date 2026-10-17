# État des sources — 15 octobre 2026

La liste exécutée par le moteur est `sources.csv`. Elle contient **1 996 sources actives**, dont toutes les **1 975 URL distinctes** du catalogue national et 21 sources complémentaires. Le rapport détaillé par URL est publié dans [rapports/sources.csv](rapports/sources.csv) et les nouveautés ou modifications du jour dans [rapports/dernier.md](rapports/dernier.md).

## Couverture au dernier passage

| Secteur | Pages actives | Accessibles | Fiches détaillées lues |
|---|---:|---:|---:|
| Plateformes nationales (`site_pro`) | 9 | 8 | 101 |
| Journaux d'annonces (`journal`) | 6 | 5 | 0 |
| Sites de cabinets (`avocat`) | 23 | 19 | 19 |
| **Total** | **38** | **32** | **120** |

Les 6 pages restantes étaient soit en erreur d'accès (3), soit accessibles sans lien d'annonce reconnu (3). « Accessible » ne signifie pas qu'une annonce a été trouvée. Le moteur a aussi laissé 108 fiches candidates en attente de lecture au passage du 15 octobre; ses limites de lecture les font tourner lors des passages suivants. Les pages détaillées peuvent contenir des ventes passées, des audiences regroupant plusieurs lots ou des dossiers qui exigent une vérification humaine.

## Référentiel national

Le référentiel restauré contient 1 053 fiches de juridictions/greffes et 1 690 fiches d'avocats ou de praticiens. Ce sont des références de contact et d'identification, pas 2 743 sources de vente à parcourir. Parmi les entrées avocats, 706 sont prioritaires; les autres publications restent à vérifier. Une entrée de praticien ou un profil de plateforme ne constitue pas automatiquement un site de cabinet publiant des adjudications.

Les 20 pages de vente directement repérées sur des sites de cabinets dans la base ont toutes été reprises dans la configuration active; 18 ont été ajoutées et 2 étaient déjà configurées. Les autres références de praticiens restent dans les fichiers de catalogue jusqu'à ce qu'une page de vente publique pertinente soit vérifiée. Les pages de greffe servent à l'identification et au contact; elles ne sont pas des pages d'annonces immobilières.

- [Référentiel national et méthode](REFERENTIEL_NATIONAL.md)
- [Répertoire des greffes et juridictions](repertoire_greffes.csv)
- [Répertoire des avocats et praticiens](repertoire_avocats.csv)
- [Configuration du moteur](sources.csv)

## Limites de la mesure

Le moteur lit les URL marquées actives dans `sources.csv`; il n'explore pas automatiquement tous les liens du répertoire national. Les comptes rendus distinguent liens candidats, audiences et fiches lues. Un lien détecté n'est ni une adjudication vérifiée ni une opportunité d'achat. Les refus 403, délais d'attente, pages sans lien reconnu et fiches en attente sont visibles dans les rapports.
