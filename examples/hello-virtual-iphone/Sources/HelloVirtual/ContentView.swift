import Foundation
import SwiftUI

/// A counter with large controls: the virtual iPhone is driven with taps at
/// screen coordinates, and big targets are easy to hit. Plain shapes and no
/// animations keep frames cheap under emulation.
struct ContentView: View {
    @State private var count = 0

    var body: some View {
        VStack(spacing: 32) {
            Text("Hello, virtual iPhone")
                .font(.title)
            Text("\(count)")
                .font(.system(size: 72, weight: .bold, design: .rounded))
                .monospacedDigit()
            Button("Add one") {
                count += 1
                NSLog("HelloVirtual count %d", count)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            Button("Reset", role: .destructive) {
                count = 0
            }
            .controlSize(.large)
        }
        .padding()
        .transaction { $0.animation = nil }
    }
}
