// swift-tools-version: 6.0

import PackageDescription

// A SwiftUI starter for Tomacco's virtual iPhone, in xtool's project format:
// one library product, the app. No package dependencies, no entitlements and
// no resources, so the bundle stays small and within the virtual iPhone's rules.
// The app icon is named by iconPath in xtool.yml, not here.
let package = Package(
    name: "HelloVirtual",
    platforms: [
        .iOS(.v17),
    ],
    products: [
        .library(
            name: "HelloVirtual",
            targets: ["HelloVirtual"]
        ),
    ],
    targets: [
        .target(
            name: "HelloVirtual"
        ),
    ]
)
