// Writes a single command JSON file at ~/.trend/command.json. The Python
// loop polls and acts on it during its idle sleep. Fire-and-forget: the
// user sees results via the next status.json update (StatusReader is
// already polling at 2s).

import Combine
import Foundation

final class CommandSender: ObservableObject, @unchecked Sendable {
    @Published var lastSent: String?
    @Published var lastSentAt: Date?
    @Published var lastError: String?

    private let url: URL

    init(path: String? = nil) {
        let p = path ?? NSString("~/.trend/command.json").expandingTildeInPath
        self.url = URL(fileURLWithPath: p)
    }

    func pause()    { send("pause") }
    func resume()   { send("resume") }
    func flatten()  { send("flatten") }
    func restart()  { send("restart") }

    private func send(_ command: String, args: [String: Any] = [:]) {
        let payload: [String: Any] = [
            "id": UUID().uuidString,
            "command": command,
            "args": args,
            "issued_at": ISO8601DateFormatter().string(from: Date()),
        ]
        do {
            try FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: payload,
                                                  options: [.prettyPrinted])
            // Atomic write: temp then rename so the Python reader never
            // sees a half-written file.
            let tmp = url.appendingPathExtension("tmp")
            try data.write(to: tmp, options: .atomic)
            // Replace any existing command file. If one is pending the new
            // one wins — acceptable for our user-initiated buttons.
            if FileManager.default.fileExists(atPath: url.path) {
                try? FileManager.default.removeItem(at: url)
            }
            try FileManager.default.moveItem(at: tmp, to: url)
            lastSent = command
            lastSentAt = Date()
            lastError = nil
        } catch {
            lastError = "send failed: \(error.localizedDescription)"
        }
    }
}
