#!/bin/sh
set -eu

if [ "$(uname -s)" != "Darwin" ]; then
    echo "iOS wheels require macOS and Xcode."
    exit 1
fi

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WHEELS="$ROOT/wheels"
SOURCES="$ROOT/build/ios-wheel-sources"
mkdir -p "$WHEELS" "$SOURCES"

python -m pip install --upgrade "cibuildwheel>=3.0" build wheel

# Pure-Python dependencies are resolved normally by Briefcase. These packages
# contain native code and must be compiled specifically for iPhone arm64.
PACKAGES="Pillow==12.3.0 lz4 brotli texture2ddecoder==1.0.6 etcpak==0.9.15 astc-encoder-py"

for SPEC in $PACKAGES; do
    NAME=$(printf '%s' "$SPEC" | sed 's/[<>=!~].*$//' | tr '[:upper:]' '[:lower:]' | tr '-' '_')
    if find "$WHEELS" -maxdepth 1 -type f -iname "${NAME}*-iphoneos*.whl" | grep -q .; then
        echo "Using existing iOS wheel for $SPEC"
        continue
    fi
    WORK="$SOURCES/$NAME"
    rm -rf "$WORK"
    mkdir -p "$WORK/download" "$WORK/source"
    python -m pip download --no-deps --no-binary=:all: --dest "$WORK/download" "$SPEC"
    ARCHIVE=$(find "$WORK/download" -type f | head -n 1)
    case "$ARCHIVE" in
        *.tar.gz|*.tgz) tar -xzf "$ARCHIVE" -C "$WORK/source" --strip-components=1 ;;
        *.zip) ditto -x -k "$ARCHIVE" "$WORK/source" ;;
        *) echo "Unsupported source archive: $ARCHIVE"; exit 1 ;;
    esac
    (
        cd "$WORK/source"
        CIBW_BUILD="cp313-*" \
        CIBW_ARCHS="arm64" \
        CIBW_BUILD_VERBOSITY=1 \
        python -m cibuildwheel --platform ios --output-dir "$WHEELS"
    )
done

echo "iOS native wheels are ready in $WHEELS"
