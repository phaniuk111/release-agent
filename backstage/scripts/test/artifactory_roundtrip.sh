#!/usr/bin/env bash
# Exercises scripts/artifactory.sh against the stand-in store: the happy path and
# every refusal it promises. No network beyond 127.0.0.1.
#
#   scripts/test/artifactory_roundtrip.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/../artifactory.sh"
work="$(mktemp -d)"
port="${STAND_IN_PORT:-18081}"
trap 'kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; rm -rf "$work"' EXIT

python3 "$here/artifactory_stand_in.py" --root "$work/store" --port "$port" --user ci --token good-token \
  > "$work/server.log" 2>&1 &
pid=$!
for _ in $(seq 50); do curl -s -o /dev/null "http://127.0.0.1:$port/" && break; sleep 0.1; done

export ARTIFACTORY_URL="http://127.0.0.1:$port/artifactory" ARTIFACTORY_REPO=generic-local \
       ARTIFACTORY_PATH=backstage ARTIFACTORY_USER=ci ARTIFACTORY_TOKEN=good-token SOURCE_COMMIT=abc123
pass=0
ok()   { pass=$((pass + 1)); echo "ok   $*"; }
fail() { echo "FAIL $*"; echo '--- server log'; cat "$work/server.log"; exit 1; }
expect_code() {  # code description command...
  local want="$1" what="$2"; shift 2
  set +e; "$@" > "$work/out" 2>&1; local got=$?; set -e
  [ "$got" = "$want" ] && ok "$what" || { cat "$work/out"; fail "$what (exit $got, wanted $want)"; }
}

mkdir -p "$work/in"
head -c 200000 /dev/urandom > "$work/in/skeleton.tar.gz"
head -c 900000 /dev/urandom > "$work/in/bundle.tar.gz"

expect_code 1 "an unknown version is absent"            "$script" exists v1
expect_code 0 "upload"                                   "$script" upload v1 "$work/in"
expect_code 0 "the version now exists"                   "$script" exists v1
grep -qx abc123 "$work/out" && ok "exists prints the source commit" || fail "exists prints the source commit"
expect_code 2 "a second upload of v1 is refused"         "$script" upload v1 "$work/in"
expect_code 0 "download verifies"                        "$script" download v1 "$work/out1"
cmp -s "$work/in/skeleton.tar.gz" "$work/out1/skeleton.tar.gz" && cmp -s "$work/in/bundle.tar.gz" "$work/out1/bundle.tar.gz" \
  && ok "downloaded files are byte-identical" || fail "downloaded files are byte-identical"
grep -q '^SOURCE_COMMIT=abc123$' "$work/out1/manifest.env" && ok "manifest records the commit" || fail "manifest records the commit"

printf 'x' >> "$work/store/generic-local/backstage/v1/bundle.tar.gz"
expect_code 2 "a tampered file is refused"               "$script" download v1 "$work/out2"
[ ! -e "$work/out2/bundle.tar.gz" ] && ok "the tampered file is not left behind" || fail "the tampered file is not left behind"

ARTIFACTORY_TOKEN=wrong expect_code 2 "a 401 is an error, never 'absent'"  "$script" exists v1
ARTIFACTORY_TOKEN=wrong expect_code 2 "a 401 stops an upload"              "$script" upload v9 "$work/in"

"$script" upload v2 "$work/in" >/dev/null
rm "$work/store/generic-local/backstage/v2/manifest.env"
expect_code 2 "a half-finished upload is not a release"  "$script" download v2 "$work/out3"
expect_code 1 "...and reads as absent"                   "$script" exists v2

expect_code 2 "a version with a slash is refused"        "$script" upload "../v3" "$work/in"
expect_code 2 "a version starting with a dot is refused" "$script" exists ".v3"
expect_code 2 "missing files are refused"                "$script" upload v4 "$work/nowhere"

code="$(curl -s -o /dev/null -w '%{http_code}' -u ci:good-token -X PUT -H 'X-Checksum-Sha256: 00' \
        --data-binary @"$work/in/bundle.tar.gz" "$ARTIFACTORY_URL/generic-local/backstage/v5/bundle.tar.gz")"
[ "$code" = 409 ] && ok "the store refuses a body that does not match its checksum" || fail "checksum refusal (HTTP $code)"

unset ARTIFACTORY_USER
expect_code 0 "a Bearer token works without a user"      "$script" exists v1

echo "$pass checks passed"
