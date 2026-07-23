import Foundation

enum MindRoomServiceState: Equatable {
    case running
    case stopped
    case notInstalled
    case runtimeMissing
    case unknown
}

struct MindRoomServiceStatus: Equatable {
    let state: MindRoomServiceState
    let message: String

    static func parse(_ output: String) -> MindRoomServiceStatus {
        let normalized = output.trimmingCharacters(in: .whitespacesAndNewlines)
        let lowercased = normalized.lowercased()

        if lowercased.contains("no such file") || lowercased.contains("command not found") {
            return MindRoomServiceStatus(state: .runtimeMissing, message: "MindRoom runtime is not installed")
        }
        if lowercased.contains("service: running") {
            return MindRoomServiceStatus(state: .running, message: "MindRoom is running")
        }
        if lowercased.contains("installed but not running") {
            return MindRoomServiceStatus(state: .stopped, message: "MindRoom is installed but stopped")
        }
        if lowercased.contains("not installed") {
            return MindRoomServiceStatus(state: .notInstalled, message: "MindRoom service is not installed")
        }
        if normalized.isEmpty {
            return MindRoomServiceStatus(state: .unknown, message: "MindRoom status is unknown")
        }
        return MindRoomServiceStatus(state: .unknown, message: sanitizedUnknownMessage(normalized))
    }

    private static func sanitizedUnknownMessage(_ output: String) -> String {
        let collapsed = output
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        if collapsed.count <= 200 {
            return collapsed
        }
        return String(collapsed.prefix(197)) + "..."
    }
}
