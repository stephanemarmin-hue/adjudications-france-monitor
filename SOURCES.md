# État des sources — 18 octobre 2026

La base `sources.csv` contient **1 996 adresses conservées** : **41 sources de ventes en veille quotidienne** et **1 955 références nationales** non interrogées chaque jour. Aucune adresse n'est supprimée. Le rapport détaillé du passage est publié dans [rapports/sources.csv](rapports/sources.csv), les nouveautés dans [rapports/dernier.md](rapports/dernier.md) et les blocages nécessitant une action dans [rapports/interventions.md](rapports/interventions.md).

## Configuration quotidienne optimisée

| Secteur | Pages en veille quotidienne |
|---|---:|
| Plateformes nationales (`site_pro`) | 9 |
| Journaux d'annonces (`journal`) | 6 |
| Sites de cabinets (`avocat`) | 25 |
| Barreau | 1 |
| **Total** | **41** |

Chaque liste peut être suivie sur plusieurs pages, dans le budget configuré. Les nouveaux liens sont prioritaires ; les fiches déjà connues sont relues par rotation pour détecter les informations ajoutées. « Accessible » ne signifie pas qu'une annonce a été trouvée.

## Référentiel national

Le référentiel restauré contient 1 053 fiches de juridictions/greffes et 1 690 fiches d'avocats ou de praticiens. Ce sont des références de contact et d'identification, pas 2 743 sources de vente à parcourir. Parmi les entrées avocats, 706 sont prioritaires; les autres publications restent à vérifier. Une entrée de praticien ou un profil de plateforme ne constitue pas automatiquement un site de cabinet publiant des adjudications.

Les 20 pages de vente directement repérées sur des sites de cabinets dans la base ont toutes été reprises dans la configuration active. Les autres références de praticiens restent dans les fichiers de catalogue jusqu'à ce qu'une page de vente publique pertinente soit vérifiée. Les pages de greffe servent à l'identification et au contact ; elles ne sont pas des pages d'annonces immobilières.

- [Référentiel national et méthode](REFERENTIEL_NATIONAL.md)
- [Répertoire des greffes et juridictions](repertoire_greffes.csv)
- [Répertoire des avocats et praticiens](repertoire_avocats.csv)
- [Configuration du moteur](sources.csv)

## Limites de la mesure

Le moteur lit les URL marquées `veille_quotidienne` dans `sources.csv`; il n'explore pas automatiquement toutes les références nationales. Les comptes rendus distinguent liens candidats, pages de listes, audiences et fiches lues. Un lien détecté n'est ni une adjudication vérifiée ni une opportunité d'achat. Une erreur permanente déclenche immédiatement une demande d'intervention ; les erreurs temporaires ne la déclenchent qu'après trois passages consécutifs.
