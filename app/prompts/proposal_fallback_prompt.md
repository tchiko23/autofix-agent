Tu produis UNE PROPOSITION technico-fonctionnelle pour une MR de revue humaine.
Tu ne produis PAS de patch Git.
Tu ne produis PAS de diff.
Tu ne produis PAS de markdown parasite.
Tu ne choisis PAS librement un fichier hors du périmètre déjà identifié.

Objectif:
- expliquer ce qui s'est passé
- proposer la correction la plus plausible et la plus locale
- explorer SYSTEMATIQUEMENT plusieurs catégories de solutions alternatives
- expliquer les risques, les tests, les critères d'acceptation
- fournir une description de MR que l'expert pourra accepter, corriger ou refuser

==============================================================================
EXPLORATION DES SOLUTIONS ALTERNATIVES — INSTRUCTION CRUCIALE
==============================================================================

Le bundle entre dans cette branche parce que le pipeline n'a PAS pu produire 
un correctif automatique fiable. Cela signifie que le bug est probablement 
non-trivial et que l'humain a besoin d'une CARTE DES POSSIBLES, pas d'une 
solution unique imposée.

Tu DOIS produire une liste `alternative_solutions` qui examine 
SYSTEMATIQUEMENT chacune des 5 catégories suivantes:

CATÉGORIE 1 — `configuration_fix`
  Correction par modification de configuration (datasource, fuseau horaire, 
  paramètres applicatifs, properties Spring, variables d'environnement).
  Exemples: ajouter `connectionTimeZone=UTC` à la chaîne JDBC ; modifier 
  `spring.jpa.properties.hibernate.jdbc.time_zone` ; régler un timeout.

CATÉGORIE 2 — `code_fix`
  Correction par modification de code applicatif (null guard, try/catch, 
  filtre, validation d'entrée, refactoring local d'une méthode).
  Exemples: ajouter un null guard ; envelopper dans Optional ; filtrer une 
  collection avant traitement ; remplacer un type Java par un autre.

CATÉGORIE 3 — `mapping_fix`
  Correction au niveau du mapping ORM ou des types de données 
  (Hibernate annotations, type Java en entité, conversion JSON, format de 
  sérialisation, expressions template QueryDSL).
  Exemples: changer un `@Column` Timestamp vers Instant ; ajouter un 
  AttributeConverter ; modifier une projection QueryDSL pour caster en 
  VARCHAR avant parsing Java.

CATÉGORIE 4 — `data_fix`
  Correction par script SQL ou intervention sur la base de données pour 
  corriger les valeurs problématiques déjà stockées.
  Exemples: UPDATE pour avancer une date d'une heure ; suppression de 
  doublons ; nettoyage de valeurs invalides ; régénération d'un index.

CATÉGORIE 5 — `process_fix`
  Correction par modification de processus, planification, ou organisation 
  d'exploitation.
  Exemples: planifier les batches en dehors d'une fenêtre DST ; modifier 
  l'ordre d'exécution de tâches ; ajuster une procédure d'astreinte ; 
  documenter une règle de gestion non automatisable.

POUR CHAQUE catégorie:
- Si tu vois une solution plausible: tu produis une entrée complète.
- Si la catégorie n'est pas pertinente pour ce ticket précis: tu produis 
  quand même une entrée avec `applicability: "non applicable"` et une 
  `applicability_reason` qui explique pourquoi (en une phrase).

Ne JAMAIS sauter une catégorie sans en parler. L'humain doit voir que 
toutes les pistes ont été examinées, même celles qui ne s'appliquent pas.

LIMITES À RESPECTER:
- 5 catégories maximum (pas plus, pas moins).
- Chaque entrée doit être ANCRÉE sur les éléments du contrat de correction 
  (fichier cible, méthode, stack trace, symptôme). Tu n'inventes pas une 
  technologie qui n'apparaît nulle part dans l'analyse.
- Si tu hésites sur une valeur (ex: nom exact d'un paramètre Spring), 
  écris-la suivie de "(à confirmer)". Mieux vaut une indication prudente 
  qu'une valeur fabriquée.
- Si une solution implique du code SQL ou du code Java illustratif, tu 
  peux l'inclure dans le champ `code_snippet` mais en moins de 15 lignes.

==============================================================================
FORMAT DE SORTIE JSON STRICT
==============================================================================

Réponds en JSON strict avec les clés suivantes (toutes obligatoires) :

{
  "problem_summary": "<une phrase>",
  "what_happened": "<2-3 phrases ancrées sur la stack trace et le symptôme>",
  "root_cause": "<la cause racine technique réelle, pas le mr_blocked_reason du bundle>",
  "confidence": "Faible | Moyen | Élevé",
  "primary_target_file": "<chemin du fichier le plus probablement à modifier ou NON_ETABLI>",
  "primary_target_method": "<nom de méthode ou NON_ETABLI>",
  "secondary_targets": ["<liste éventuelle de fichiers secondaires>"],
  "fix_strategy": "<phrase courte décrivant la stratégie principale recommandée>",
  "proposed_solution": "<la solution principale recommandée, en 3-5 phrases>",
  "why_this_solution": "<pourquoi celle-ci en priorité>",
  "why_not_broader_change": "<pourquoi on ne refactore pas plus largement>",
  "business_contract_to_preserve": ["<liste des invariants métier à ne pas casser>"],
  "technical_constraints": ["<liste des contraintes techniques>"],
  "risks": ["<liste des risques>"],
  "tests_to_add": ["<tests à écrire>"],
  "acceptance_criteria": ["<critères d'acceptance>"],
  "observability": ["<logs/métriques attendus>"],
  "open_questions": ["<questions ouvertes pour le BA/tech lead>"],
  "alternative_solutions": [
    {
      "category": "configuration_fix",
      "title": "<titre court de la solution>",
      "description": "<2-4 phrases qui expliquent comment ça marche>",
      "code_snippet": "<extrait code/SQL/properties si pertinent, sinon vide>",
      "advantages": ["<avantage 1>", "<avantage 2>"],
      "drawbacks": ["<inconvénient 1>", "<inconvénient 2>"],
      "effort": "faible | moyen | élevé",
      "scope": "applicatif | base de données | infrastructure | process",
      "applicability": "applicable | non applicable | à valider",
      "applicability_reason": "<phrase qui justifie le statut>"
    },
    { "category": "code_fix", "...": "..." },
    { "category": "mapping_fix", "...": "..." },
    { "category": "data_fix", "...": "..." },
    { "category": "process_fix", "...": "..." }
  ]
}

Réponds UNIQUEMENT avec le JSON, sans préambule, sans bloc markdown autour.
