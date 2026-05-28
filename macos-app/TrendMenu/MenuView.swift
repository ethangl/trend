// The dropdown view shown when the menubar icon is clicked.
// Phase 3: adds a controls section (Pause/Resume/Flatten) that talks to the
// running Python loop via ~/.trend/command.json. Process Start/Stop/Restart
// from Phase 2 remain in their own section.

import SwiftUI

struct MenuView: View {
    @ObservedObject var reader: StatusReader
    @ObservedObject var supervisor: ProcessSupervisor
    @ObservedObject var sender: CommandSender
    @State private var showFlattenConfirm = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            Divider()
            tickSection
            Divider()
            positionsSection
            if let fills = reader.status?.recentFills, !fills.isEmpty {
                Divider()
                fillsSection(fills)
            }
            Divider()
            controlsSection
            Divider()
            processSection
            Divider()
            footer
        }
        .padding(12)
        .frame(width: 340)
        .font(.system(size: 12, design: .monospaced))
        .confirmationDialog(
            "Flatten all positions?",
            isPresented: $showFlattenConfirm,
            titleVisibility: .visible
        ) {
            Button("Flatten All", role: .destructive) { sender.flatten() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Closes every open IB position with MARKET orders and resets every cell to FLAT.")
        }
    }

    private var header: some View {
        HStack {
            Image(systemName: "circle.fill").foregroundStyle(reader.statusColor)
            if reader.status?.paused == true {
                Text("PAUSED").fontWeight(.bold).foregroundStyle(.orange)
            } else {
                Text(reader.status?.status.capitalized ?? "Unknown")
                    .fontWeight(.semibold)
            }
            Spacer()
            if let acct = reader.status?.account, !acct.isEmpty {
                Text(acct).foregroundStyle(.secondary)
            }
        }
    }

    private var tickSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let next = reader.status?.nextTickAt {
                row("next tick", short(next))
            }
            if let last = reader.status?.lastTick {
                row("last tick", short(last.startedAt))
                row("  severity", last.severity)
                row("  orders / fills", "\(last.ordersPlaced) / \(last.fillsReceived)")
            }
            row("ib connected", (reader.status?.ibConnected ?? false) ? "yes" : "no")
        }
    }

    private var positionsSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Positions").fontWeight(.semibold)
            if let s = reader.status {
                let symbols = Array(Set(s.expectedPositions.keys)
                    .union(s.ibkrPositions.keys)).sorted()
                if symbols.isEmpty {
                    Text("  (all flat)").foregroundStyle(.secondary)
                }
                ForEach(symbols, id: \.self) { sym in
                    let expected = s.expectedPositions[sym] ?? 0
                    let actual = s.ibkrPositions[sym] ?? 0
                    let mismatch = expected != actual
                    HStack {
                        Text(sym).frame(width: 50, alignment: .leading)
                        Text(signed(expected)).frame(width: 50, alignment: .trailing)
                        Text("/").foregroundStyle(.secondary)
                        Text(signed(actual)).frame(width: 50, alignment: .trailing)
                            .foregroundStyle(mismatch ? .red : .primary)
                    }
                }
                Text("  \(s.cellsActive) cells active, \(s.skippedSymbols.count) skipped")
                    .foregroundStyle(.secondary)
                    .padding(.top, 2)
            } else {
                Text("  (no status yet)").foregroundStyle(.secondary)
            }
        }
    }

    private func fillsSection(_ fills: [Fill]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Recent fills").fontWeight(.semibold)
            ForEach(fills.suffix(5)) { f in
                HStack {
                    Text(timeOnly(f.ts)).foregroundStyle(.secondary)
                        .frame(width: 60, alignment: .leading)
                    Text(f.symbol).frame(width: 40, alignment: .leading)
                    Text(f.side).frame(width: 40, alignment: .leading)
                    Text("\(f.qty) @ \(String(format: "%.2f", f.price))")
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
        }
    }

    private var controlsSection: some View {
        let isPaused = reader.status?.paused == true
        return VStack(alignment: .leading, spacing: 4) {
            Text("Controls").fontWeight(.semibold)
            HStack(spacing: 6) {
                if isPaused {
                    Button("Resume") { sender.resume() }
                } else {
                    Button("Pause") { sender.pause() }
                }
                Button("Flatten All") { showFlattenConfirm = true }
                    .foregroundStyle(.red)
                Spacer()
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(!supervisor.isRunning)
            if let last = sender.lastSent, let at = sender.lastSentAt {
                Text("sent: \(last) @ \(timeOnly(ISO8601DateFormatter().string(from: at)))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if let err = sender.lastError {
                Text(err).font(.caption2).foregroundStyle(.red)
            }
        }
    }

    private var processSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Loop process").fontWeight(.semibold)
            HStack {
                Image(systemName: supervisor.isRunning ? "circle.fill" : "circle")
                    .foregroundStyle(supervisor.isRunning ? .green : .secondary)
                if supervisor.isRunning, let pid = supervisor.pid {
                    Text("running  pid \(pid)")
                } else {
                    Text("stopped")
                }
                Spacer()
                if supervisor.restartCount > 0 {
                    Text("restarts: \(supervisor.restartCount)")
                        .foregroundStyle(.secondary)
                }
            }
            if !supervisor.lastMessage.isEmpty {
                Text(supervisor.lastMessage)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            HStack(spacing: 6) {
                if supervisor.isRunning {
                    Button("Stop") { supervisor.stop() }
                    Button("Restart") { supervisor.restart() }
                } else {
                    Button("Start") { supervisor.start() }
                    Button("Start (flatten)") { supervisor.start(flattenAccount: true) }
                }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            Text("log: \(supervisor.logPath)")
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    private var footer: some View {
        HStack {
            if let at = reader.lastReadAt {
                Text("updated \(timeOnly(ISO8601DateFormatter().string(from: at)))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Quit") { NSApplication.shared.terminate(nil) }
                .buttonStyle(.borderless)
                .font(.caption)
        }
    }

    // MARK: helpers

    private func row(_ k: String, _ v: String) -> some View {
        HStack {
            Text(k).foregroundStyle(.secondary)
            Spacer()
            Text(v)
        }
    }

    private func signed(_ n: Int) -> String { n >= 0 ? "+\(n)" : "\(n)" }

    /// Trim ISO timestamp to "MM-dd HH:mm" for compactness.
    private func short(_ iso: String) -> String {
        // Cheap formatting: ISO is "2026-05-28T17:30:00-04:00" → "05-28 17:30"
        let s = iso.dropFirst(5).prefix(11).replacingOccurrences(of: "T", with: " ")
        return String(s)
    }

    private func timeOnly(_ iso: String) -> String {
        let s = iso.dropFirst(11).prefix(5)  // "HH:mm"
        return String(s)
    }
}
