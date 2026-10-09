import Foundation

/// One command's output and lifetime, shared by its worker and the Cancel button.
final class MindRoomCommandProcess: @unchecked Sendable {
    private let process = Process()
    private let lock = NSLock()
    private var started = false
    private var cancelled = false
    private let onOutput: ((String) -> Void)?

    init(onOutput: ((String) -> Void)? = nil) { self.onOutput = onOutput }

    var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }

    func cancel() {
        lock.lock()
        defer { lock.unlock() }
        guard !started || process.isRunning else { return }
        cancelled = true
        if started { process.terminate() }
    }

    func run(_ invocation: MindRoomCommandInvocation) -> CommandResult {
        lock.lock()
        guard !cancelled else {
            lock.unlock()
            return CommandResult(exitCode: MindRoomCommand.pairingCancelledExitCode, output: "")
        }
        process.executableURL = invocation.executableURL
        process.arguments = invocation.arguments
        process.environment = invocation.environment
        // Prompts must see EOF when the app runs the CLI without a terminal.
        process.standardInput = FileHandle.nullDevice
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            started = true
            lock.unlock()
        } catch {
            lock.unlock()
            return CommandResult(exitCode: 127, output: error.localizedDescription)
        }
        var data = Data()
        while true {
            let chunk = pipe.fileHandleForReading.availableData
            if chunk.isEmpty { break }
            data.append(chunk)
            onOutput?(String(decoding: data, as: UTF8.self))
        }
        process.waitUntilExit()
        return CommandResult(exitCode: process.terminationStatus, output: String(decoding: data, as: UTF8.self))
    }
}
