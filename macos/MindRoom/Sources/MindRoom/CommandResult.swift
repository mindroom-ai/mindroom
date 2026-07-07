import Foundation

struct CommandResult: Equatable {
    let exitCode: Int32
    let output: String

    var isSuccess: Bool {
        exitCode == 0
    }

    /// Output condensed for display in an alert: no blank lines, capped length.
    var condensedOutput: String {
        let lines = output
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        let joined = lines.joined(separator: "\n")
        if joined.count <= 800 {
            return joined
        }
        return String(joined.prefix(797)) + "..."
    }
}
