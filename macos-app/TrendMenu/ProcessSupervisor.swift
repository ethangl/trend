// Spawns and supervises the Python trading loop as a child process.
// Auto-restarts on unexpected exit (with backoff). Terminates the child
// when the app quits. stdout + stderr are tee'd into a log file.

import Combine
import Foundation
import SwiftUI

final class ProcessSupervisor: ObservableObject, @unchecked Sendable {
    @Published var isRunning: Bool = false
    @Published var pid: Int32? = nil
    @Published var lastExitStatus: Int32? = nil
    @Published var lastMessage: String = ""
    @Published var restartCount: Int = 0

    // Hardcoded paths for v1. Move to UserDefaults later if you want to
    // configure them from the UI.
    let pythonPath: String
    let scriptPath: String
    let extraArgs: [String]
    let logPath: String

    private var process: Process? = nil
    private var stdoutPipe: Pipe? = nil
    private var stderrPipe: Pipe? = nil
    private var logHandle: FileHandle? = nil
    private let writeQueue = DispatchQueue(label: "supervisor.logwrite")
    private var shouldRestart = true
    private let maxConsecutiveRestarts = 5
    private var consecutiveRestarts = 0

    init() {
        let home = NSString("~").expandingTildeInPath
        self.pythonPath = "\(home)/w/trend/.venv/bin/python"
        self.scriptPath = "\(home)/w/trend/scripts/run_live_loop.py"
        // --exit-on-orphan: if Xcode kills us via ⌘R (which bypasses
        // applicationWillTerminate), the child detects the reparenting and
        // exits cleanly instead of holding the IB clientId.
        // MBT/MET are no longer skipped — the loop now rolls futures in-process
        // (execute_rolls in run_live_loop.py), so a held crypto position
        // migrates to the new front month instead of expiring.
        self.extraArgs = ["--exit-on-orphan"]
        self.logPath = "\(home)/w/trend/logs/loop-supervised.log"
    }

    // MARK: - Lifecycle

    /// Launch the Python loop. `flattenAccount=true` adds --flatten-account
    /// for a clean-slate restart (closes any open IB positions).
    func start(flattenAccount: Bool = false) {
        guard process == nil else {
            lastMessage = "already running (pid \(pid ?? -1))"
            return
        }
        shouldRestart = true
        spawn(flattenAccount: flattenAccount)
    }

    /// Stop the child and don't auto-restart it. Used by the Stop button
    /// and on app quit.
    func stop() {
        shouldRestart = false
        process?.terminate()
        lastMessage = "stopping…"
    }

    /// Stop and restart cleanly. `flattenAccount` is forwarded to the new run.
    func restart(flattenAccount: Bool = false) {
        guard let p = process else {
            start(flattenAccount: flattenAccount)
            return
        }
        // Termination handler will respawn because shouldRestart stays true.
        // Stash the desired flag so the next spawn picks it up.
        pendingFlatten = flattenAccount
        p.terminate()
        lastMessage = "restarting…"
    }

    private var pendingFlatten: Bool = false

    // MARK: - Internals

    private func spawn(flattenAccount: Bool) {
        var args = [scriptPath] + extraArgs
        if flattenAccount { args.append("--flatten-account") }

        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = args
        p.currentDirectoryURL = URL(fileURLWithPath: (scriptPath as NSString)
            .deletingLastPathComponent).deletingLastPathComponent()

        // Open log handle once per spawn.
        let logURL = URL(fileURLWithPath: logPath)
        try? FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        let handle = try? FileHandle(forWritingTo: logURL)
        _ = try? handle?.seekToEnd()
        let header = "\n----- spawn \(Date()) args=\(args) -----\n"
        try? handle?.write(contentsOf: Data(header.utf8))
        logHandle = handle

        // Pipes for stdout/stderr. readabilityHandler runs on a background
        // queue per stream, so we serialize writes through writeQueue.
        let outPipe = Pipe()
        let errPipe = Pipe()
        p.standardOutput = outPipe
        p.standardError = errPipe
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty, let self = self else { return }
            self.writeQueue.async { try? self.logHandle?.write(contentsOf: data) }
        }
        errPipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty, let self = self else { return }
            self.writeQueue.async { try? self.logHandle?.write(contentsOf: data) }
        }
        stdoutPipe = outPipe
        stderrPipe = errPipe

        // Termination handler fires on a background queue — hop to main.
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                self?.handleTermination(status: proc.terminationStatus)
            }
        }

        do {
            try p.run()
            process = p
            pid = p.processIdentifier
            isRunning = true
            lastMessage = "running (pid \(p.processIdentifier))"
        } catch {
            isRunning = false
            process = nil
            pid = nil
            lastMessage = "launch failed: \(error.localizedDescription)"
        }
    }

    private func handleTermination(status: Int32) {
        // Close out pipes/handles for this run.
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        try? logHandle?.close()
        logHandle = nil
        process = nil
        pid = nil
        isRunning = false
        lastExitStatus = status

        if !shouldRestart {
            lastMessage = "stopped (exit \(status))"
            consecutiveRestarts = 0
            return
        }

        // Auto-restart with simple backoff for unexpected exits.
        consecutiveRestarts += 1
        restartCount += 1
        if consecutiveRestarts > maxConsecutiveRestarts {
            shouldRestart = false
            lastMessage = "crashed \(consecutiveRestarts) times — giving up. Check the log."
            return
        }
        let delay = min(30.0, 2.0 * Double(consecutiveRestarts))
        lastMessage = "exit \(status) — restarting in \(Int(delay))s (attempt \(consecutiveRestarts)/\(maxConsecutiveRestarts))"
        let flatten = pendingFlatten
        pendingFlatten = false
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self, self.shouldRestart else { return }
            self.spawn(flattenAccount: flatten)
        }
    }

    // Best-effort cleanup. SwiftUI doesn't always run deinit on app quit,
    // so AppDelegate.applicationWillTerminate also calls stop().
    deinit {
        shouldRestart = false
        process?.terminate()
    }
}
