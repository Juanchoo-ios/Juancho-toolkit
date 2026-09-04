# Bisaya Toolkit iOS — TrollStore port

This source package ports the desktop workflow to an on-device iOS application.
It includes a macOS/GitHub Actions build that attempts to compile UnityPy's
native dependencies for iPhone arm64 before producing the IPA.

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

The automated wheel build uses cibuildwheel's iOS target. If an upstream native
package does not support iOS cross-compilation, the workflow intentionally stops
at that package instead of uploading an IPA that installs but crashes.

## Current validation status

The Python sources, ZIP safety logic and shader integrity are checked outside
iOS, and the same converter engine/shaders passed the desktop conversion test.
Final iPhone compilation and device testing still require the macOS workflow.
