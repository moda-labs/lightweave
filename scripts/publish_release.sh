#!/usr/bin/env bash
set -eu

asset_dir=${1:-dist}
manifest="$asset_dir/lightweave-release.json"
firmware="$asset_dir/lightweave-field-${RELEASE_TAG}.bin"
serial_flash="$asset_dir/lightweave-serial-flash-${RELEASE_TAG}.zip"
release_api="repos/${GITHUB_REPOSITORY}/releases/tags/${RELEASE_TAG}"
verify_dir=$(mktemp -d)
trap 'rm -rf "$verify_dir"' EXIT
expected_assets=$(printf '%s\n' \
  "lightweave-field-${RELEASE_TAG}.bin" \
  "lightweave-serial-flash-${RELEASE_TAG}.zip" \
  lightweave-release.json | LC_ALL=C sort)

verify_asset_set() {
  actual_assets=$(gh api "$release_api" --jq '.assets[].name' | LC_ALL=C sort)
  test "$actual_assets" = "$expected_assets"
}

find_draft_id() {
  gh api --paginate "repos/${GITHUB_REPOSITORY}/releases?per_page=100" \
    --jq ".[] | select(.tag_name == \"${RELEASE_TAG}\" and .draft == true) | .id"
}

if draft=$(gh api "$release_api" --jq .draft 2>"$verify_dir/api-error"); then
  if [ "$draft" = false ]; then
    verify_asset_set
    gh release download "$RELEASE_TAG" \
      --repo "$GITHUB_REPOSITORY" \
      --dir "$verify_dir" \
      --pattern lightweave-release.json \
      --pattern "lightweave-field-${RELEASE_TAG}.bin" \
      --pattern "lightweave-serial-flash-${RELEASE_TAG}.zip"
    cmp "$manifest" "$verify_dir/lightweave-release.json"
    cmp "$firmware" "$verify_dir/lightweave-field-${RELEASE_TAG}.bin"
    cmp "$serial_flash" "$verify_dir/lightweave-serial-flash-${RELEASE_TAG}.zip"
    exit 0
  fi
  test "$draft" = true
else
  grep -q 'HTTP 404' "$verify_dir/api-error" || {
    cat "$verify_dir/api-error" >&2
    exit 1
  }
  draft_id=$(find_draft_id)
  if [ -z "$draft_id" ]; then
    gh release create "$RELEASE_TAG" \
      --draft \
      --repo "$GITHUB_REPOSITORY" \
      --title "Lightweave ${RELEASE_TAG}" \
      --verify-tag \
      --generate-notes
    draft_id=$(find_draft_id)
  fi
  case "$draft_id" in
    ''|*[!0-9]*) echo "expected exactly one numeric draft release ID" >&2; exit 1 ;;
  esac
  release_api="repos/${GITHUB_REPOSITORY}/releases/${draft_id}"
fi

gh release upload "$RELEASE_TAG" "$manifest" "$firmware" "$serial_flash" \
  --clobber \
  --repo "$GITHUB_REPOSITORY"
verify_asset_set
gh release download "$RELEASE_TAG" \
  --repo "$GITHUB_REPOSITORY" \
  --dir "$verify_dir" \
  --pattern lightweave-release.json \
  --pattern "lightweave-field-${RELEASE_TAG}.bin" \
  --pattern "lightweave-serial-flash-${RELEASE_TAG}.zip"
cmp "$manifest" "$verify_dir/lightweave-release.json"
cmp "$firmware" "$verify_dir/lightweave-field-${RELEASE_TAG}.bin"
cmp "$serial_flash" "$verify_dir/lightweave-serial-flash-${RELEASE_TAG}.zip"
gh release edit "$RELEASE_TAG" --draft=false --repo "$GITHUB_REPOSITORY"
