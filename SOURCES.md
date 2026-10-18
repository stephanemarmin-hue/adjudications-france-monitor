# État des sources — 18 octobre 2026

La base `sources.csv` contient **1 996 adresses actives** : **41 sources directes**, **684 profils en veille rapide** et **1 271 entrées d'annuaire/juridiction en découverte nationale**. Aucune adresse n'est supprimée. Le rapport détaillé du passage est publié dans [rapports/sources.csv](rapports/sources.csv), les nouveautés dans [rapports/dernier.md](rapports/dernier.md) et les blocages nécessitant une action dans [rapports/interventions.md](rapports/interventions.md).

## Configuration par rythmes

| Secteur | Pages en veille quotidienne |
|---|---:|
| Plateformes nationales (`site_pro`) | 9 |
| Journaux d'annonces (`journal`) | 6 |
| Sites de cabinets (`avocat`) | 25 |
| Barreau | 1 |
| **Total** | **41** |

Chaque liste peut être suivie sur plusieurs pages, dans le budget configuré. Les nouveaux liens sont prioritaires ; les fiches déjà connues sont relues par rotation pour détecter les informations ajoutées. « Accessible » ne signifie pas qu'une annonce a été trouvée.

| Rythme | Volume | Fréquence |
|---|---:|---|
| Pages directes de ventes | 41 | À chacun des quatre passages quotidiens |
| Profils d'avocats et annonceurs | 684 | À chaque passage, espacés d'environ 6 heures, dans une capacité de 700 |
| Annuaires, greffes et juridictions | 1 271 | 50 par passage, les plus anciens d'abord ; cycle d'environ une semaine |
| Sites découverts automatiquement | Variable | Contrôle immédiat puis toutes les 24 heures |

## Référentiel national

Le référentiel restauré contient 1 053 fiches de juridictions/greffes et 1 690 fiches d'avocats ou de praticiens. Ce sont des références de contact et d'identification, pas 2 743 sources de vente à parcourir. Parmi les entrées avocats, 706 sont prioritaires; les autres publications restent à vérifier. Une entrée de praticien ou un profil de plateforme ne constitue pas automatiquement un site de cabinet publiant des adjudications.

Les 20 pages de vente directement repérées sur des sites de cabinets restent dans la veille directe. Les autres références de praticiens sont maintenant interrogées pour détecter annonces et liens vers les sites de cabinets. Les pages de greffe servent surtout à l'identification et à la découverte : elles tournent séparément, car ce ne sont généralement pas des calendriers de ventes.

- [Référentiel national et méthode](REFERENTIEL_NATIONAL.md)
- [Répertoire des greffes et juridictions](repertoire_greffes.csv)
- [Répertoire des avocats et praticiens](repertoire_avocats.csv)
- [Configuration du moteur](sources.csv)

## Limites de la mesure

Le moteur utilise les trois valeurs `veille_quotidienne`, `veille_rapide` et `decouverte_nationale`. Les comptes rendus distinguent liens candidats, pages de listes, audiences et fiches lues. Un lien détecté n'est ni une adjudication vérifiée ni une opportunité d'achat. Une erreur permanente sur une page directe déclenche immédiatement une demande d'intervention ; les erreurs temporaires attendent trois passages. Les erreurs des grandes séries de profils ou d'annuaires sont regroupées par domaine pour éviter des centaines d'alertes identiques.

Le nombre d'adresses actives mesure l'effort de balayage, pas le pourcentage réel d'annonces françaises captées. Ce dernier est publié seulement à partir de l'échantillon indépendant `controle_couverture.csv`.
