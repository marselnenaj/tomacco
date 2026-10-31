// swift-tools-version: 6.4
import PackageDescription
let package = Package(name: "TomaccoCore", products: [
    .library(name: "TomaccoCore", type: .dynamic, targets: ["TomaccoCore"])
], targets: [.target(name: "TomaccoCore"), .testTarget(name: "TomaccoCoreTests", dependencies: ["TomaccoCore"])])
