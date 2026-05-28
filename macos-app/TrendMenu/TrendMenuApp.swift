// Entry point for the menubar app. Owns the StatusReader and the
// ProcessSupervisor and renders the MenuBarExtra.

import AppKit
import SwiftUI

/// We use an AppDelegate purely to guarantee the Python child gets a SIGTERM
/// when the app quits — SwiftUI doesn't always run `deinit` on @StateObjects
/// during normal app termination.
final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var supervisor: ProcessSupervisor?

    func applicationWillTerminate(_ notification: Notification) {
        supervisor?.stop()
        // Give the child ~1s to receive SIGTERM and clean up before we die.
        Thread.sleep(forTimeInterval: 1.0)
    }
}

@main
struct TrendMenuApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var reader = StatusReader()
    @StateObject private var supervisor = ProcessSupervisor()
    @StateObject private var sender = CommandSender()

    var body: some Scene {
        MenuBarExtra {
            MenuView(reader: reader, supervisor: supervisor, sender: sender)
                .onAppear {
                    // Late-binding so the AppDelegate can stop the child on quit.
                    appDelegate.supervisor = supervisor
                }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "circle.fill")
                    .foregroundStyle(dotColor)
                Text("trend")
            }
        }
        .menuBarExtraStyle(.window)
    }

    /// Dot color: combines the reader's status with the supervisor state so
    /// a stopped loop shows gray regardless of what the (stale) status.json says.
    private var dotColor: Color {
        if !supervisor.isRunning { return .gray }
        return reader.statusColor
    }
}
