#!/usr/bin/env bash
# The Backstage release artifacts in an Artifactory GENERIC repo: put once, got back verified.
#
#   scripts/artifactory.sh upload   <version> <dir>   # <dir>/skeleton.tar.gz + <dir>/bundle.tar.gz
#   scripts/artifactory.sh download <version> <dir>   # the same two files + manifest.env, verified
#   scripts/artifactory.sh exists   <version>         # exit 0 + the version's commit, 1 = absent
#
# Settings (env):
#   ARTIFACTORY_URL    https://<host>/artifactory
#   ARTIFACTORY_REPO   the generic repository
#   ARTIFACTORY_PATH   folder inside it (default: backstage)
#   ARTIFACTORY_TOKEN  an access/identity token
#   ARTIFACTORY_USER   optional: with it, basic auth user:token; without, a Bearer token
#   SOURCE_COMMIT      recorded in the manifest (default: git rev-parse HEAD)
#
# Layout: <repo>/<path>/<version>/{skeleton.tar.gz,bundle.tar.gz,manifest.env}
#
# A version is written ONCE: upload refuses a version whose manifest exists, so a
# version always names the same bytes. The manifest goes up LAST and download
# starts from it — a half-finished upload is never taken for a release. Every
# file is sent with its SHA-256 (Artifactory refuses a body that does not match)
# and checked again after download. Credentials reach curl on stdin (-K -), never
# on its command line, so they are not in `ps` or a trace.
set -euo pipefail

FILES=(skeleton.tar.gz bundle.tar.gz)

die() { echo "artifactory.sh: $*" >&2; exit 2; }

need() { [ -n "${!1:-}" ] || die "$1 is not set"; }

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

check_version() {
  # A path segment we build URLs from: letters, digits, dot, dash, underscore.
  case "$1" in
    '' | .* | *[!A-Za-z0-9._-]*) die "bad version '$1' (letters, digits, . _ - only; not starting with .)" ;;
  esac
}

base_url() {
  need ARTIFACTORY_URL; need ARTIFACTORY_REPO
  local path="${ARTIFACTORY_PATH:-backstage}"
  path="${path#/}"; path="${path%/}"
  echo "${ARTIFACTORY_URL%/}/${ARTIFACTORY_REPO}/${path}/$1"
}

# curl with the credentials fed on stdin as a config file.
acurl() {
  need ARTIFACTORY_TOKEN
  local esc="${ARTIFACTORY_TOKEN//\\/\\\\}"; esc="${esc//\"/\\\"}"
  {
    if [ -n "${ARTIFACTORY_USER:-}" ]; then
      local u="${ARTIFACTORY_USER//\\/\\\\}"; u="${u//\"/\\\"}"
      printf 'user = "%s:%s"\n' "$u" "$esc"
    else
      printf 'header = "Authorization: Bearer %s"\n' "$esc"
    fi
  } | curl -K - -sS --connect-timeout 20 --max-time 900 --retry 3 --retry-delay 2 "$@"
}

# HTTP status of a HEAD request: 200 present, 404 absent, anything else is an error
# (a 401 must never read as "not there yet" and invite an overwrite).
status_of() { acurl -o /dev/null -w '%{http_code}' -I "$1" || true; }

manifest_value() {  # key file — read, never sourced: the file came over the network
  local key="$1" k v
  while IFS='=' read -r k v; do
    [ "$k" = "$key" ] && { printf '%s' "$v"; return 0; }
  done < "$2"
  return 1
}

cmd_exists() {
  local version="$1" url code tmp
  check_version "$version"
  url="$(base_url "$version")/manifest.env"
  code="$(status_of "$url")"
  case "$code" in
    200) tmp="$(mktemp)"; acurl --fail -o "$tmp" "$url"
         manifest_value SOURCE_COMMIT "$tmp" || true; echo; rm -f "$tmp"; return 0 ;;
    404) return 1 ;;
    *)   die "cannot tell whether $version exists: HTTP $code from $url" ;;
  esac
}

cmd_upload() {
  local version="$1" dir="$2" base commit sums=() f sum code
  check_version "$version"
  base="$(base_url "$version")"
  for f in "${FILES[@]}"; do [ -s "$dir/$f" ] || die "missing $dir/$f"; done
  code="$(status_of "$base/manifest.env")"
  case "$code" in
    404) ;;
    200) die "version $version already exists — versions are never overwritten; pick a new one" ;;
    *)   die "cannot check $base/manifest.env: HTTP $code" ;;
  esac
  commit="${SOURCE_COMMIT:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"

  for f in "${FILES[@]}"; do
    sum="$(sha256 "$dir/$f")"; sums+=("$sum")
    echo "uploading $f ($sum)"
    acurl --fail-with-body -o /dev/null -X PUT \
      -H "X-Checksum-Sha256: $sum" -T "$dir/$f" "$base/$f" \
      || die "upload of $f failed"
  done

  local manifest; manifest="$(mktemp)"
  {
    echo "VERSION=$version"
    echo "SOURCE_COMMIT=$commit"
    echo "BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "SKELETON_SHA256=${sums[0]}"
    echo "BUNDLE_SHA256=${sums[1]}"
  } > "$manifest"
  acurl --fail-with-body -o /dev/null -X PUT \
    -H "X-Checksum-Sha256: $(sha256 "$manifest")" -T "$manifest" "$base/manifest.env" \
    || { rm -f "$manifest"; die "upload of manifest.env failed"; }
  rm -f "$manifest"
  echo "uploaded $version ($commit) to $base"
}

cmd_download() {
  local version="$1" dir="$2" base f want got key
  check_version "$version"
  base="$(base_url "$version")"
  mkdir -p "$dir"
  acurl --fail -o "$dir/manifest.env" "$base/manifest.env" \
    || die "no complete upload of $version at $base (manifest.env missing)"
  [ "$(manifest_value VERSION "$dir/manifest.env")" = "$version" ] \
    || die "manifest.env at $base names another version"
  for f in "${FILES[@]}"; do
    key="SKELETON_SHA256"; [ "$f" = bundle.tar.gz ] && key="BUNDLE_SHA256"
    want="$(manifest_value "$key" "$dir/manifest.env")" || die "manifest.env has no $key"
    acurl --fail -o "$dir/$f" "$base/$f" || die "download of $f failed"
    got="$(sha256 "$dir/$f")"
    if [ "$got" != "$want" ]; then
      rm -f "$dir/$f"
      die "$f does not match its manifest (got $got, expected $want)"
    fi
    echo "verified $f ($got)"
  done
  echo "SOURCE_COMMIT=$(manifest_value SOURCE_COMMIT "$dir/manifest.env")"
}

case "${1:-}" in
  upload)   [ $# -eq 3 ] || die "usage: upload <version> <dir>";   cmd_upload "$2" "$3" ;;
  download) [ $# -eq 3 ] || die "usage: download <version> <dir>"; cmd_download "$2" "$3" ;;
  exists)   [ $# -eq 2 ] || die "usage: exists <version>";         cmd_exists "$2" ;;
  *) die "usage: artifactory.sh upload|download|exists <version> [dir]" ;;
esac
