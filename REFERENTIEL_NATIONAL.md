# Référentiel national des greffes et avocats

## Comptage et périmètre

La base de référence collectée le **30 septembre 2026** contient :

- **1 053 fiches de juridictions et contacts** dans repertoire_greffes.csv, dont 157 tribunaux judiciaires. Le reste comprend notamment des tribunaux de proximité, prud’hommes, tribunaux de commerce et activités économiques, juridictions administratives, greffes des associations et services spécialisés.
- **1 690 fiches d’avocats ou praticiens** dans repertoire_avocats.csv. La base marque **706 références prioritaires** (annonceurs ou ventes référencés) et 984 profils de praticiens dont la publication reste à vérifier.
- **3 709 occurrences d’URL** après séparation des champs contenant plusieurs adresses, soit **1 975 URL canoniques distinctes**. Le dédoublonnage met l’hôte en minuscules, retire le fragment et uniformise la barre oblique finale. Les répertoires d’origine conservent la provenance par fiche.

Le fichier catalogue_urls.csv rassemble ces URL, une adresse canonique par ligne, avec le domaine, le nombre et le type de références, les identifiants des fiches associées, la priorité et l’indicateur d’activation actuelle. Le générateur build_catalogue_urls.py le reconstruit à chaque exécution du workflow et après les modifications des deux répertoires.

## Ce qui est réellement surveillé

sources.csv est la base opérationnelle du moteur. Elle contient **1 996 lignes** : **41 sources actives en veille quotidienne** — 9 plateformes, 6 journaux, 25 pages de cabinets et 1 page de barreau — et **1 955 références** conservées sans interrogation quotidienne. **20 URL du catalogue national correspondent à des pages de vente de cabinets actuellement actives**. Les autres URL restent recensées et rattachées aux fiches sources, à qualifier comme pages de veille avant leur éventuelle activation.

Les 1 053 fiches de juridictions restent un répertoire de contacts et d’orientation. Elles ne sont pas toutes des pages de veille d’annonces : leurs URL officielles sont principalement des fiches d’annuaire, pas des calendriers de ventes. Les 1 690 fiches d’avocats ne correspondent pas chacune à un site indépendant. Plusieurs renvoient à des profils sur les plateformes Licitor, Info-Enchères, Enchères Publiques ou Immo Enchère. Une fiche de praticien ou une mention d’annonceur ne prouve pas qu’une vente soit en cours.

## Fraîcheur et limites

La date de collecte du référentiel est le 30 septembre 2026. Les références sont conservées avec leur statut et leurs URL ; la collecte du moteur confirme l’accessibilité des 41 pages surveillées au moment de chaque passage. Une adresse dans le catalogue ne garantit ni son fonctionnement actuel, ni la publication d’une vente. Les résultats figurent dans rapports/dernier.json et rapports/sources.csv ; les blocages persistants et leurs liens directs figurent dans rapports/interventions.md.
