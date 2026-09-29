#!/usr/bin/env bash
# Downloads the OPA binary (policy engine) into ./bin for this OS/arch.
# If the download is blocked, ASAP falls back to regopy; if neither is present it fails closed.
set -euo pipefail
VERSION="${OPA_VERSION:-v1.4.2}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/bin"
mkdir -p "$DEST"
if [ -x "$DEST/opa" ]; then echo "opa already installed: $("$DEST/opa" version | head -1)"; exit 0; fi
if command -v opa >/dev/null 2>&1; then echo "using opa on PATH: $(command -v opa)"; exit 0; fi

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) arch=amd64 ;;
  arm64|aarch64) arch=arm64 ;;
  *) echo "unsupported arch $arch; install OPA manually (brew install opa)"; exit 0 ;;
esac
case "$os" in
  darwin) asset="opa_darwin_${arch}" ; [ "$arch" = arm64 ] && asset="opa_darwin_arm64_static" ;;
  linux)  asset="opa_linux_${arch}_static" ;;
  *) echo "unsupported OS $os; install OPA manually"; exit 0 ;;
esac
url="https://github.com/open-policy-agent/opa/releases/download/${VERSION}/${asset}"
echo "downloading $url"
if curl -fsSL -o "$DEST/opa" "$url"; then
  chmod +x "$DEST/opa"
  echo "installed: $("$DEST/opa" version | head -1)"
else
  rm -f "$DEST/opa"
  echo "WARNING: could not download OPA. Install it with 'brew install opa' or 'pip install regopy'." >&2
fi
