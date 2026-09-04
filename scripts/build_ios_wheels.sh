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

echo "Bisaya Toolkit native wheel builder v1.2.4"

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

    if [ "$NAME" = "pillow" ]; then
        # Pillow 12.3.0 publishes this official CPython 3.13 device wheel. Do
        # not fall back to a source build: that would require cross-building
        # Pillow's complete zlib/image dependency stack for iPhoneOS.
        PILLOW_WHEEL="pillow-12.3.0-cp313-cp313-ios_13_0_arm64_iphoneos.whl"
        PILLOW_URL="https://files.pythonhosted.org/packages/9d/ac/31fb64e1e7efb5a4b50cd3d92049ba89ac6e4d8d3bb6a74e15048ca3353e/$PILLOW_WHEEL"
        PILLOW_SHA256="21900ce7ba264168cd50defae43cd75d25c833ad4ad6e73ffc5596d12e25ac89"
        echo "Downloading pinned official Pillow iPhoneOS wheel"
        curl --fail --location --retry 5 --retry-all-errors \
            --output "$WHEELS/$PILLOW_WHEEL" "$PILLOW_URL"
        ACTUAL_SHA256=$(shasum -a 256 "$WHEELS/$PILLOW_WHEEL" | awk '{print $1}')
        if [ "$ACTUAL_SHA256" != "$PILLOW_SHA256" ]; then
            echo "Pillow wheel checksum verification failed."
            exit 1
        fi
        echo "Verified official Pillow iPhoneOS wheel"
        continue
    fi

    # Prefer an official CPython 3.13 iPhoneOS wheel when the package publishes
    # one. This avoids unnecessary native compilation.
    if python -m pip download \
        --no-deps \
        --only-binary=:all: \
        --platform ios_13_0_arm64_iphoneos \
        --python-version 313 \
        --implementation cp \
        --abi cp313 \
        --dest "$WHEELS" \
        "$SPEC"; then
        echo "Downloaded official iPhoneOS wheel for $SPEC"
        continue
    fi

    echo "No official iPhoneOS wheel for $SPEC; building from source"
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
        # cibuildwheel uses an iOS-specific architecture identifier. Generic
        # "arm64" is valid on desktop platforms but rejected for iOS.
        CIBW_BUILD="cp313-*" \
        CIBW_ARCHS="arm64_iphoneos" \
        CIBW_BUILD_VERBOSITY=1 \
        python -m cibuildwheel --platform ios --output-dir "$WHEELS"
    )
done

echo "iOS native wheels are ready in $WHEELS"
