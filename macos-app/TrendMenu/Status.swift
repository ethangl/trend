// Codable model mirroring the Python loop's status.json schema, plus a
// StatusReader that polls the file and publishes updates to SwiftUI.

import Combine    // @Published lives here; MemberImportVisibility means we
                  // can't rely on SwiftUI to transitively import it.
import Foundation
import SwiftUI

// MARK: - JSON Model

struct Status: Codable {
    let schemaVersion: Int
    let updatedAt: String
    let status: String          // "starting" | "waiting" | "ticking" | "idle" | "error"
    let account: String
    let ibConnected: Bool
    let nextTickAt: String?
    let lastTick: LastTick?
    let expectedPositions: [String: Int]
    let ibkrPositions: [String: Int]
    let skippedSymbols: [String]
    let cellsActive: Int
    let recentFills: [Fill]
    let recentErrors: [ErrorRecord]
    let paused: Bool?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case updatedAt = "updated_at"
        case status
        case account
        case ibConnected = "ib_connected"
        case nextTickAt = "next_tick_at"
        case lastTick = "last_tick"
        case expectedPositions = "expected_positions"
        case ibkrPositions = "ibkr_positions"
        case skippedSymbols = "skipped_symbols"
        case cellsActive = "cells_active"
        case recentFills = "recent_fills"
        case recentErrors = "recent_errors"
        case paused
    }
}

struct LastTick: Codable {
    let startedAt: String
    let endedAt: String?
    let severity: String
    let ordersPlaced: Int
    let fillsReceived: Int
    let error: String?

    enum CodingKeys: String, CodingKey {
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case severity
        case ordersPlaced = "orders_placed"
        case fillsReceived = "fills_received"
        case error
    }
}

struct Fill: Codable, Identifiable {
    let ts: String
    let symbol: String
    let strategy: String
    let side: String
    let qty: Int
    let price: Double
    let orderId: Int

    // Composite ID so SwiftUI lists don't complain — order_id alone is only
    // unique within one cell.
    var id: String { "\(strategy)/\(symbol)/\(orderId)" }

    enum CodingKeys: String, CodingKey {
        case ts, symbol, strategy, side, qty, price
        case orderId = "order_id"
    }
}

struct ErrorRecord: Codable, Identifiable {
    let ts: String
    let message: String
    var id: String { ts + message }
}

// MARK: - Reader

/// Note on isolation: this Xcode project sets `-default-isolation=MainActor`
/// (the new Swift 6 "Approachable Concurrency" default), which would make
/// every class implicitly `@MainActor`. That conflicts with
/// `ObservableObject`'s nonisolated `objectWillChange` requirement.
/// `nonisolated` opts this class out of the project default. The timer
/// callback hops back to `@MainActor` to mutate `@Published` state safely,
/// and the initial `readOnce()` in `init` is on main because SwiftUI builds
/// `@StateObject` on the main thread.
/// `@unchecked Sendable`: the class has mutable `@Published` state but we
/// only mutate it from the main actor (the Timer hops via `Task { @MainActor }`,
/// and the initial `readOnce()` in `init` runs on main because SwiftUI
/// constructs `@StateObject` on main). The compiler can't prove that, so we
/// vouch for it manually.
final class StatusReader: ObservableObject, @unchecked Sendable {
    @Published var status: Status?
    @Published var lastReadError: String?
    @Published var lastReadAt: Date?

    private let url: URL
    private var timer: Timer?

    init(path: String? = nil, pollInterval: TimeInterval = 2.0) {
        // Default: ~/.trend/status.json. Matches the Python loop's default.
        let p = path ?? NSString("~/.trend/status.json").expandingTildeInPath
        self.url = URL(fileURLWithPath: p)
        readOnce()
        timer = Timer.scheduledTimer(withTimeInterval: pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.readOnce() }
        }
    }

    private func readOnce() {
        do {
            let data = try Data(contentsOf: url)
            let decoded = try JSONDecoder().decode(Status.self, from: data)
            self.status = decoded
            self.lastReadError = nil
            self.lastReadAt = Date()
        } catch {
            // File may not exist yet (loop not running) — leave prior status
            // in place and surface the error message.
            self.lastReadError = "\(error.localizedDescription)"
            self.lastReadAt = Date()
        }
    }

    // Status dot color. Green when everything's OK, orange when paused,
    // yellow during transitions or warnings, red on hard problems.
    var statusColor: Color {
        guard let s = status else { return .gray }
        if s.paused == true { return .orange }
        if !s.ibConnected { return .yellow }
        if let last = s.lastTick, last.severity.lowercased() == "halt" { return .red }
        if !s.recentErrors.isEmpty { return .red }
        switch s.status {
        case "error": return .red
        case "starting", "ticking": return .yellow
        case "paused": return .orange
        case "waiting", "idle": return .green
        default: return .gray
        }
    }
}
