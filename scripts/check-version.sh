#!/usr/bin/env bash
# check-version.sh — cohérence du numéro de version (exécuté par la CI).
#
#   ./scripts/check-version.sh                  # VERSION semver + section CHANGELOG
#   BASE_REF=origin/main ./scripts/check-version.sh   # + VERSION strictement > base
#   BRANCH=release/v0.2.2 ./scripts/check-version.sh  # + branche release/vX.Y.Z == VERSION
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail() { echo "::error::$*" >&2; exit 1; }

VERSION="$(tr -d '[:space:]' < VERSION)"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "VERSION « $VERSION » n'est pas au format X.Y.Z"
grep -qE "^## \[$VERSION\] — [0-9]{4}-[0-9]{2}-[0-9]{2}$" CHANGELOG.md \
    || fail "CHANGELOG.md : section « ## [$VERSION] — AAAA-MM-JJ » absente"
echo "✓ VERSION $VERSION et section CHANGELOG présentes"

if [ -n "${BRANCH:-}" ] && [[ "$BRANCH" == release/* ]]; then
    [ "$BRANCH" = "release/v$VERSION" ] || fail "branche $BRANCH ≠ release/v$VERSION (VERSION)"
    echo "✓ branche $BRANCH conforme"
fi

if [ -n "${BASE_REF:-}" ]; then
    BASE_VERSION="$(git show "$BASE_REF:VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
    if [ -z "$BASE_VERSION" ]; then
        echo "✓ pas de VERSION sur $BASE_REF (première version versionnée)"
    else
        newest="$(printf '%s\n%s\n' "$BASE_VERSION" "$VERSION" | sort -V | tail -1)"
        { [ "$newest" = "$VERSION" ] && [ "$VERSION" != "$BASE_VERSION" ]; } \
            || fail "VERSION $VERSION doit être supérieure à celle de $BASE_REF ($BASE_VERSION) : make bump"
        echo "✓ $BASE_VERSION → $VERSION"
    fi
fi
