#!/usr/bin/env bash
# Build the Lambda layers. Run from WSL, not Windows: these artefacts go into a
# Linux Lambda runtime.
#
#   layers/shared/python/   the `shared` package plus pydantic  (deployed)
#   layers/ffmpeg/bin/      static ffmpeg + ffprobe             (opt-in)
#
# Both directories are gitignored: the ffmpeg binaries are ~80MB and the
# shared layer is generated. `cdk synth` succeeds without the shared layer but
# emits a warning, so run this before `cdk deploy`.
#
# The ffmpeg layer is no longer deployed -- the PWA mixes background music in
# the browser. Build it only when working on the retained mix_audio handler.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAYERS="$REPO_ROOT/layers"

# ffmpeg static build. John Van Sickle's builds are the long-standing static
# Linux distribution and are GPL-licensed; the binaries ship in the layer and
# are not redistributed further.
FFMPEG_URL="${FFMPEG_URL:-https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz}"

build_shared() {
  local target="$LAYERS/shared/python"
  echo "==> building shared layer -> $target"
  rm -rf "$target"
  mkdir -p "$target"
  # --no-compile keeps the layer small; Lambda compiles on first import.
  python3 -m pip install --quiet --no-compile --target "$target" "$REPO_ROOT/backend"
  # The API and functions ship their own code; the layer only carries `shared`
  # and its dependencies.
  rm -rf "$target"/api "$target"/functions
  # The rm -rf above also removed the *tracked* .gitkeep that holds this
  # directory open in git; without it every build leaves a deletion sitting in
  # git status, waiting to ride along with an unrelated commit.
  touch "$target/.gitkeep"
  echo "    $(du -sh "$target" | cut -f1)"
}

build_ffmpeg() {
  local target="$LAYERS/ffmpeg/bin"
  echo "==> building ffmpeg layer -> $target"
  if [[ -x "$target/ffmpeg" && -x "$target/ffprobe" ]]; then
    echo "    already present, skipping download (rm -rf $target to force)"
    return
  fi
  mkdir -p "$target"
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  echo "    downloading $FFMPEG_URL"
  curl -fsSL "$FFMPEG_URL" -o "$tmp/ffmpeg.tar.xz"
  tar -xJf "$tmp/ffmpeg.tar.xz" -C "$tmp" --strip-components=1
  install -m 0755 "$tmp/ffmpeg" "$target/ffmpeg"
  install -m 0755 "$tmp/ffprobe" "$target/ffprobe"
  echo "    $("$target/ffmpeg" -version | head -1)"
}

# Defaults to `shared` only: the browser mixes background music, so the
# deployed pipeline has no ffmpeg step and no reason to pull an 80MB binary.
# `$0 ffmpeg` still builds it for the retained, undeployed mix_audio handler.
case "${1:-shared}" in
  shared) build_shared ;;
  ffmpeg) build_ffmpeg ;;
  all) build_shared; build_ffmpeg ;;
  *) echo "usage: $0 [shared|ffmpeg|all]" >&2; exit 2 ;;
esac

echo "==> done"
