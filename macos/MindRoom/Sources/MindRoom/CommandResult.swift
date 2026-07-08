import Foundation

struct CommandResult: Equatable {
    let exitCode: Int32
    let output: String

    var isSuccess: Bool {
        exitCode == 0
    }

    /// Output condensed for display in a failure alert: no blank lines, capped length.
    /// Keeps the tail because CLI errors appear at the end of long output.
    var condensedOutput: String {
        let lines = output.suffix(2000)
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        let joined = lines.joined(separator: "\n")
        if joined.count <= 800 {
            return joined
        }
        return "..." + String(joined.suffix(797))
    }
}
