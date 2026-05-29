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
    @Published private(set) var isRunningCommand = false
    @Published private(set) var lastOutput = ""

    private let runtime: MindRoomRuntime
    private let processRunner: MindRoomProcessRunner

    init(
        runtime: MindRoomRuntime = MindRoomRuntime(),
        processRunner: @escaping MindRoomProcessRunner = MindRoomCommandRunner.runProcess
    ) {
        self.runtime = runtime
        self.processRunner = processRunner
    }

    func refreshStatus() {
        runRuntimeAction(.serviceStatus, updateStatusFromOutput: true)
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
        default:
            guard let action = command.runtimeAction else { return }
            runRuntimeAction(action, updateStatusFromOutput: action == .serviceStatus)
        }
    }

    private func runRuntimeAction(_ action: MindRoomRuntimeAction, updateStatusFromOutput: Bool = false) {
        isRunningCommand = true
        let invocation = runtime.command(for: action)
        let processRunner = processRunner
        DispatchQueue.global(qos: .userInitiated).async {
            let result = processRunner(invocation)
            DispatchQueue.main.async {
                self.isRunningCommand = false
                self.lastOutput = result.output
                if updateStatusFromOutput {
                    self.serviceStatus = MindRoomServiceStatus.parse(result.output)
                } else if result.exitCode == 0 {
                    self.refreshStatus()
                }
            }
        }
    }

    nonisolated static func runProcess(_ invocation: MindRoomCommandInvocation) -> CommandResult {
        let process = Process()
        process.executableURL = invocation.executableURL
        process.arguments = invocation.arguments
        process.environment = invocation.environment

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return CommandResult(exitCode: 127, output: error.localizedDescription)
        }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let output = String(data: data, encoding: .utf8) ?? ""
        return CommandResult(exitCode: process.terminationStatus, output: output)
    }
}
