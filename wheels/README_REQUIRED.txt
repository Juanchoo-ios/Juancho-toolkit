REQUIRED BEFORE BUILDING

Place CPython iOS-arm64 compatible wheels here for every native dependency.
At minimum this includes Pillow, texture2ddecoder, etcpak, astc-encoder-py,
lz4, brotli and any native transitive dependency required by UnityPy 1.25.3.

Desktop Windows/macOS/Linux wheels will not run on iPhone. Wheel platform tags
must target arm64_iphoneos and the chosen CPython/iOS deployment version.
