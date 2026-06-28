# Spécification d'exécution déterministe

Le moteur local applique les modifications proposées par le modèle sans lui laisser la main sur la mécanique Git ou Forge.

## Contraintes obligatoires

1. Le modèle ne produit **qu'un plan JSON**.
2. Le moteur local applique les changements sous forme d'éditions structurées `search` / `replace`.
3. Les modifications doivent être minimales, ciblées et sûres.
4. Si le correctif est déjà présent, le modèle doit définir `already_fixed=true`.
5. Si le correctif n'est pas sûr à appliquer automatiquement, le modèle doit définir `unable_to_fix=true` et expliquer pourquoi.
6. Les chemins de fichiers doivent correspondre aux fichiers fournis dans le contexte.
7. Le modèle ne doit jamais proposer de commande shell, de diff git brut, ni de création de branche, ni de création de merge request.
8. Les explications doivent rester utiles mais compactes ; la structure JSON prime sur la prose.

## Format attendu pour chaque édition

- `file` : chemin exact du fichier à modifier.
- `search` : extrait exact du code actuel à remplacer.
- `replace` : nouvelle version du bloc.

## Qualité du correctif

- Respecter le style du fichier existant.
- Préserver les imports et la structure déjà présents.
- Ne modifier que le minimum nécessaire.
- Ne pas changer les API publiques si ce n'est pas explicitement demandé.
