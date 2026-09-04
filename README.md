# Bisaya Toolkit iOS — TrollStore port

This source package ports the desktop workflow to an on-device iOS application.
It includes a macOS/GitHub Actions build that attempts to compile UnityPy's
native dependencies for iPhone arm64 before producing the IPA.

`ShaderVariants_add.unity3d` is stored as several `.part` files because GitHub's
browser uploader rejects individual files larger than 25 MB. The build runs
`scripts/assemble_shaders.py`, verifies the exact size and SHA-256 checksum, and
reconstructs the original shader automatically before creating the iOS app.

## Included workflow

1. Load and deduplicate the downloadable skin catalogs.
2. Search heroes and skins.
3. Download the selected ZIP to temporary storage.
4. Safely extract it with path, file-count and size checks.
5. Convert all `.unity3d` files using MLBB iOS Auto Converter 1.0.4.
6. Use the three bundled iOS ShaderVariants files.
7. Fully validate the conversion and final ZIP.
8. Move only the final ZIP to `Documents/Bisaya Toolkit/`.
9. Delete the downloaded ZIP, Android extraction and converted working folder.

Filza can open the application container and browse `Documents/Bisaya Toolkit`.
File sharing metadata is enabled so the Documents folder can also appear through
iOS file-management interfaces when supported.

## Build requirements

- A Mac with a current Xcode installation.
- Python 3 and Briefcase.
- iOS-arm64 wheels listed in `wheels/README_REQUIRED.txt`.
- Enough device storage: texture conversion to RGBA32 may temporarily use several
  times the original package size.

Run `sh build_on_macos.sh` on a Mac. It creates an unsigned physical-device IPA
in `release/Bisaya_Toolkit_TrollStore.ipa`; TrollStore signs it during install.
Alternatively, place the project in a GitHub
repository, open **Actions**, select **Build TrollStore IPA**, and choose
**Run workflow**. Download the resulting `Bisaya-Toolkit-TrollStore-IPA`
artifact and install the `.ipa` through TrollStore.

When using GitHub's browser uploader, upload the extracted project contents.
Every included file is below 25 MB in version 1.2.0; do not upload the older
unsplit `ShaderVariants_add.unity3d` file.

Version 1.2.1 fixes the native-wheel target to cibuildwheel's required
`arm64_iphoneos` identifier. This resolves the `Invalid archs option: arm64`
failure from the first GitHub Actions run.

Version 1.2.2 builds Pillow from its complete official Git tag. Pillow's PyPI
source archive omits the iOS dependency helper referenced by its cibuildwheel
configuration, which caused GitHub Actions to stop with exit code 127.

The automated wheel build uses cibuildwheel's iOS target. If an upstream native
package does not support iOS cross-compilation, the workflow intentionally stops
at that package instead of uploading an IPA that installs but crashes.

## Current validation status

The Python sources, ZIP safety logic and shader integrity are checked outside
iOS, and the same converter engine/shaders passed the desktop conversion test.
Final iPhone compilation and device testing still require the macOS workflow.
