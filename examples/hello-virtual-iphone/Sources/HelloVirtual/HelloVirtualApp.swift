import Foundation
import SwiftUI

@main
struct HelloVirtualApp: App {
    init() {
        // One line at launch, so the app's log is easy to find with
        // ios.virtual.syslog {"bundle_id":"dev.tomacco.examples.HelloVirtual"}.
        NSLog("HelloVirtual launched")
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
