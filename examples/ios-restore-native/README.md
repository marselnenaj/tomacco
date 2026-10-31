# Native programs built on Linux

These C and **Embedded Swift** command-line programs execute in the actual iOS
restore-shell guest. They do not use Foundation, UIKit, SwiftUI or an Apple SDK.
`libSystem.tbd` declares only linker symbols; the implementation comes from the
real `libSystem` in iOS. No replacement library is bundled.

Build with Clang, Swift with Embedded support, and an LLVM Mach-O linker that
supports iOS. The `ld64.lld` bundled with some Linux Swift toolchains rejects iOS;
provide a compatible LLVM linker explicitly. The tested versions are Clang
22.1.8, Swift 6.4 and Rust's LLVM/LLD 23.1.

```sh
python3 tools/build_ios_native_example.py \
  --linker /absolute/path/to/ld64.lld \
  --output .tooling/my-ios-programs
python3 tools/prepare_ios_emulator.py \
  --toolchain .tooling/ios-sptm \
  --firmware /absolute/extracted-iPhone16-IPSW \
  --payload .tooling/my-ios-programs/programs \
  --output .tooling/ios-sptm/my-native-guest
```

Run `/tomacco/hello-c` and `/tomacco/hello-swift 100` using `ios.emulator.exec` and
the newly prepared firmware directory. Swift allocates an integer buffer using
the actual guest allocator, verifies its sum, writes Unicode text and returns
an exit code. Arguments outside 0–1000 fail with code 7. Build and preparation
manifests retain input and signed-output hashes. The input programs are preserved.

This shows native command-line execution only, not a SwiftUI
app, IPA installation, a complete Apple SDK or the graphical iPhone environment.
