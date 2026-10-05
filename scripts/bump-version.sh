#!/usr/bin/env bash
# bump-version.sh — incrémente la version et prépare la branche de version.
#
#   ./scripts/bump-version.sh            # patch : 0.2.2 -> 0.2.3 (par défaut)
#   ./scripts/bump-version.sh minor      # 0.2.2 -> 0.3.0
#   ./scripts/bump-version.sh major      # 0.2.2 -> 1.0.0
#   ./scripts/bump-version.sh --branch   # crée aussi la branche release/vX.Y.Z
#
# Met à jour VERSION et ouvre la section « ## [X.Y.Z] — date » du CHANGELOG
# juste sous « ## [Non publié] » (les entrées en attente y sont déplacées).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PART="patch"
BRANCH=0
for arg in "$@"; do
    case "$arg" in
        patch|minor|major) PART="$arg" ;;
        --branch) BRANCH=1 ;;
        *) echo "argument inconnu : $arg" >&2; exit 2 ;;
    esac
done

CURRENT="$(tr -d '[:space:]' < VERSION)"
IFS=. read -r MA MI PA <<< "$CURRENT"
case "$PART" in
    patch) PA=$((PA + 1)) ;;
    minor) MI=$((MI + 1)); PA=0 ;;
    major) MA=$((MA + 1)); MI=0; PA=0 ;;
esac
NEXT="$MA.$MI.$PA"
TODAY="$(date +%Y-%m-%d)"

if [ "$BRANCH" = 1 ]; then
    git switch -c "release/v$NEXT"
fi

echo "$NEXT" > VERSION
python3 - "$NEXT" "$TODAY" <<'PY'
import sys
version, today = sys.argv[1], sys.argv[2]
path = "CHANGELOG.md"
text = open(path, encoding="utf-8").read()
marker = "## [Non publié]\n"
if marker not in text:
    sys.exit("CHANGELOG.md : section « ## [Non publié] » introuvable")
if f"## [{version}]" in text:
    sys.exit(f"CHANGELOG.md : la section {version} existe déjà")
text = text.replace(marker, f"{marker}\n## [{version}] — {today}\n", 1)
open(path, "w", encoding="utf-8").write(text)
PY
echo "✓ $CURRENT → $NEXT (VERSION + CHANGELOG)"
echo "  Complète la section [$NEXT] du CHANGELOG, puis : git commit -am \"chore(release): v$NEXT\""
