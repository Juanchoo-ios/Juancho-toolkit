#!/bin/sh
set -eu

if [ "$(uname -s)" != "Darwin" ]; then
    echo "This build must run on macOS with Xcode installed."
    exit 1
fi

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip briefcase
sh scripts/build_ios_wheels.sh

briefcase create iOS
briefcase build iOS

# Briefcase creates/builds the Xcode project but does not currently emit a
# standalone physical-device IPA. Build the iphoneos target without signing;
# TrollStore signs the finished IPA when it is installed.
PROJECT=$(find build -type d -name '*.xcodeproj' | head -n 1)
if [ -z "$PROJECT" ]; then
    echo "Could not find the generated Xcode project."
    exit 1
fi
SCHEME=$(xcodebuild -list -json -project "$PROJECT" | python -c 'import json,sys; print(json.load(sys.stdin)["project"]["schemes"][0])')
rm -rf build/device build/Payload release
xcodebuild \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -sdk iphoneos \
    -configuration Release \
    -derivedDataPath build/device \
    CODE_SIGNING_ALLOWED=NO \
    CODE_SIGNING_REQUIRED=NO \
    CODE_SIGN_IDENTITY="" \
    build

APP=$(find build/device/Build/Products/Release-iphoneos -maxdepth 1 -type d -name '*.app' | head -n 1)
if [ -z "$APP" ]; then
    echo "The physical-device .app was not produced."
    exit 1
fi
mkdir -p build/Payload release
ditto "$APP" "build/Payload/$(basename "$APP")"
(
    cd build
    zip -qry ../release/Bisaya_Toolkit_TrollStore.ipa Payload
)

unzip -tq release/Bisaya_Toolkit_TrollStore.ipa

echo "Build finished: release/Bisaya_Toolkit_TrollStore.ipa"
