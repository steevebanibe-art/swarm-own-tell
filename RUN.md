# Comment lancer les choses

Tout se fait dans un terminal PowerShell ouvert dans le dossier `swarm-own-tell`.
Ne tape jamais `python` tout court (sur ce PC, c'est un faux raccourci du Microsoft Store).
On utilise toujours le Python de l'environnement : `.venv\Scripts\python.exe`.

## 1. Les tests (gratuits, sans internet)

```powershell
$env:PYTHONUTF8 = "1"
.venv\Scripts\python.exe -m pytest
```

Les tests n'appellent jamais l'API : toutes les réponses sont simulées.
Internet est coupé pendant les tests et ta vraie clé est cachée (même si elle est
dans le terminal), donc tu peux les lancer sans risque et sans payer.
Tu dois voir à la fin une ligne verte du genre `121 passed` (état du 4 nov. 2026).
Si un test reste bloqué, il est arrêté tout seul au bout de 30 secondes.

## 2. Le test réel (coûte moins d'un centime)

Un seul appel minuscule à `claude-haiku-5-5` pour vérifier que la clé, le modèle,
le calcul du prix et les en-têtes de limite de débit fonctionnent.
Fait le 3 nov. 2026 : `end_turn`, 0,0000074 $, limites lues 1 000 requêtes,
500 000 jetons d'entrée et 80 000 jetons de sortie par minute.

1. Ta clé est déjà rangée dans les variables d'environnement Windows (niveau utilisateur) :
   un terminal ouvert après le 30 oct. la voit tout seul. Ne la colle jamais dans un fichier,
   dans le chat ou sur une capture d'écran. Si un vieux terminal ne la voit pas :

   ```powershell
   $env:ANTHROPIC_API_KEY = [Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','User')
   ```

2. Lance :

   ```powershell
   $env:PYTHONUTF8 = "1"
   .venv\Scripts\python.exe scripts\smoke_test.py
   ```

Le script affiche : la raison d'arrêt (`stop_reason`), les jetons utilisés, le coût en dollars
et les en-têtes `anthropic-ratelimit-*`. Il n'affiche jamais la clé.
Le coût est noté dans `state\smoke_ledger.jsonl` (plafond de sécurité : 0,05 $).

## Règle importante : un seul programme par registre de dépenses

Un fichier `state\...ledger.jsonl` ne peut servir qu'à un seul programme à la fois.
Si tu lances un deuxième terminal sur le même registre, il s'arrête tout de suite
avec l'erreur `LedgerLocked` (c'est voulu : sinon chacun croirait avoir tout le budget).
Pour lancer plusieurs tailles de flotte en même temps, donne à chaque terminal
son propre registre et sa propre part du plafond (par exemple 4 registres à 5,50 $).

Même règle pour les batchs : un fichier d'état (`state\batches_n10.json` par exemple) par
terminal, sinon le deuxième s'arrête avec l'erreur `BatchStateLocked`. Donne aussi à chaque
terminal son propre fichier de résultats (`runs\n10.jsonl`) et son propre cache
(`state\call_cache_n10.jsonl`), et mets la taille de flotte dans les identifiants des requêtes
batch (par exemple `n10-ep3-r5-a2`), pour qu'aucun terminal ne confonde les batchs d'un autre.

## Pendant une longue série de runs : mettre en pause, surveiller

- **Mettre en pause sans rien casser** : crée un fichier vide nommé `STOP` à l'endroit indiqué
  au lancement (par exemple `state\STOP`) :

  ```powershell
  New-Item -ItemType File state\STOP
  ```

  Aucune nouvelle tâche ne démarre ; celles en cours finissent et sont enregistrées.
  Pour reprendre : supprime le fichier (`Remove-Item state\STOP`) et relance la même commande
  (ce qui est déjà fait est sauté). Les contrôles d'arrêt automatiques créent ce même fichier.
- **Voir où on en est** (aussi depuis le téléphone si le fichier est synchronisé) :
  `state\status.json`, réécrit toutes les 10 minutes : tâches faites, en échec, vitesse,
  heure de fin estimée, dépense, dépense projetée, et `paused` si la file est en pause.
- **Coupure d'internet** : les tâches tombées sur une erreur réseau ne sont pas comptées comme
  ratées, elles retournent dans la file. Si plus de la moitié des tâches des 2 dernières minutes
  échouent ainsi, la file s'arrête 60 secondes, puis essaie une seule tâche ; elle repart quand
  le réseau revient.

## Faire tourner sur Hugging Face (Jobs)

Ton compte `Banibe` (PRO) est déjà connecté sur ce PC. Les crédits de calcul du PRO paient les Jobs
(machine `cpu-basic` : 0,01 $ de l'heure). Le lanceur envoie le code dans un espace PRIVÉ de ton compte
(il refuse de continuer si cet espace est public), et les résultats (`state\`, `runs\`) restent dans
cet espace même après la fin du Job.

```powershell
$env:PYTHONUTF8 = "1"
.venv\Scripts\python.exe scripts\hf_launch.py selftest
```

Ça lance les tests sur Hugging Face, sans aucune clé API (quelques centimes au plus). Puis :

```powershell
.venv\Scripts\python.exe scripts\hf_launch.py status ID_DU_JOB
.venv\Scripts\python.exe scripts\hf_launch.py logs ID_DU_JOB
.venv\Scripts\python.exe scripts\hf_launch.py pull
```

`pull` recopie les résultats dans `hf_work\work`. Pour les vrais runs (à partir du vendredi) :
`hf_launch.py run --timeout 10h --with-anthropic-key -- python ...`. Sans `--timeout`, le lanceur
refuse : par défaut Hugging Face coupe un Job au bout de 30 minutes. Ta clé est envoyée comme
secret chiffré, lue dans ta variable Windows ; elle n'est jamais affichée.
**Règle : jamais la même file de runs en même temps sur le PC et sur Hugging Face.**

## 3. Si l'environnement n'existe plus

```powershell
uv venv --python 3.13 --system-site-packages .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

## Où vont les fichiers

- `runs\` : résultats des expériences (une ligne JSON par appel).
- `state\` : registre des dépenses et état des batchs.

Ces deux dossiers ne sont pas versionnés (voir `.gitignore`).
