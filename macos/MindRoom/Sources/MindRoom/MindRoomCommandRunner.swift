import AppKit
import Foundation

typealias MindRoomProcessRunner = (MindRoomCommandInvocation) -> CommandResult

@MainActor
final class MindRoomCommandRunner: ObservableObject {
    static let shared = MindRoomCommandRunner()

    @Published private(set) var serviceStatus = MindRoomServiceStatus(
        state: .unknown,
        message: "MindRoom status is unknown"
    )
    @Published private(set) var runningCommandTitle: String?
    @Published private(set) var lastOutput = ""

    /// Called on the main actor when a user-initiated command finishes.
    var onCommandFinished: ((MindRoomCommand, CommandResult) -> Void)?

    private var isRefreshingStatus = false
    private let runtime: MindRoomRuntime
    private let processRunner: MindRoomProcessRunner

    init(
        runtime: MindRoomRuntime = MindRoomRuntime(),
        processRunner: @escaping MindRoomProcessRunner = MindRoomCommandRunner.runProcess
    ) {
        self.runtime = runtime
        self.processRunner = processRunner
    }

    var isRunningCommand: Bool {
        runningCommandTitle != nil
    }

    // Status refreshes run independently of user commands so a background
    // refresh never swallows a menu click.
    func refreshStatus() {
        guard !isRefreshingStatus else { return }
        isRefreshingStatus = true
        let invocation = runtime.command(for: .serviceStatus)
        let processRunner = processRunner
        DispatchQueue.global(qos: .utility).async {
            let result = processRunner(invocation)
            DispatchQueue.main.async {
                self.isRefreshingStatus = false
                self.serviceStatus = MindRoomServiceStatus.parse(result.output)
            }
        }
    }

    func run(_ command: MindRoomCommand) {
        switch command {
        case .openDashboard:
            NSWorkspace.shared.open(URL(string: "http://localhost:8765")!)
        case .openHostedChat:
            NSWorkspace.shared.open(URL(string: "https://chat.mindroom.chat")!)
        case .openConfigFolder:
            NSWorkspace.shared.open(runtime.configDirectoryURL)
        case .openLogsFolder:
            NSWorkspace.shared.open(runtime.logsDirectoryURL)
        case .serviceStatus:
            refreshStatus()
        default:
            guard let action = command.runtimeAction else { return }
            runUserCommand(command, action: action)
        }
    }

    private func runUserCommand(_ command: MindRoomCommand, action: MindRoomRuntimeAction) {
        guard runningCommandTitle == nil else { return }
        runningCommandTitle = command.title
        let invocation = runtime.command(for: action)
        let processRunner = processRunner
        DispatchQueue.global(qos: .userInitiated).async {
            let result = processRunner(invocation)
            DispatchQueue.main.async {
                self.runningCommandTitle = nil
                self.lastOutput = result.output
                self.onCommandFinished?(command, result)
                self.refreshStatus()
            }
        }
    }

    nonisolated static func runProcess(_ invocation: MindRoomCommandInvocation) -> CommandResult {
        let process = Process()
        process.executableURL = invocation.executableURL
        process.arguments = invocation.arguments
        process.environment = invocation.environment
        // No TTY is attached, so any CLI prompt must see EOF instead of hanging.
        process.standardInput = FileHandle.nullDevice

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        do {
            try process.run()
        } catch {
            return CommandResult(exitCode: 127, output: error.localizedDescription)
        }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let output = String(data: data, encoding: .utf8) ?? ""
        return CommandResult(exitCode: process.terminationStatus, output: output)
    }
}
