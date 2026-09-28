import AppKit
import Foundation

typealias MindRoomProcessRunner = (MindRoomCommandInvocation) -> CommandResult

struct CommandFeedback {
    let title: String
    let successMessage: String?
    let result: CommandResult
    let needsAttention: Bool

    var statusLabel: String { needsAttention ? "needs attention" : result.isSuccess ? "finished" : "failed" }
    var statusSymbol: String { needsAttention || !result.isSuccess ? "exclamationmark.triangle" : "checkmark.circle" }
}

@MainActor
final class MindRoomCommandRunner: ObservableObject {
    static let shared = MindRoomCommandRunner()

    @Published private(set) var serviceStatus = MindRoomServiceStatus(
        state: .unknown,
        message: "MindRoom status is unknown"
    )
    @Published private(set) var runningCommandTitle: String?
    @Published private(set) var lastOutput = ""
    @Published private(set) var feedback: CommandFeedback?
    @Published private(set) var localSetup = LocalAgentsSetupSnapshot()
    @Published private(set) var setupCheck: CommandResult?
    @Published private(set) var hasRefreshedStatus = false
    @Published private(set) var isRefreshingStatus = false
    /// Set when Connect Account finds this Mac already connected, so the view can ask before pairing again.
    @Published var needsReconnectConfirmation = false

    /// Called on the main actor when a user-initiated command finishes.
    var onCommandFinished: ((MindRoomCommand, CommandResult) -> Void)?

    private var refreshRequested = false
    private let runtime: MindRoomRuntime
    private let processRunner: MindRoomProcessRunner
    private let showSection: (AppSection) -> Void

    init(
        runtime: MindRoomRuntime = MindRoomRuntime(),
        processRunner: @escaping MindRoomProcessRunner = MindRoomCommandRunner.runProcess,
        showSection: ((AppSection) -> Void)? = nil
    ) {
        self.runtime = runtime
        self.processRunner = processRunner
        self.showSection = showSection ?? { AppWindowController.shared.show(section: $0) }
    }

    var isRunningCommand: Bool {
        runningCommandTitle != nil
    }

    // Status refreshes run independently of user commands so a background
    // refresh never swallows a menu click.
    func refreshStatus(queueIfBusy: Bool = true) {
        guard !isRefreshingStatus else {
            refreshRequested = refreshRequested || queueIfBusy
            return
        }
        isRefreshingStatus = true
        readStatus()
    }

    private func readStatus() {
        let invocation = runtime.command(for: .serviceStatus)
        let processRunner = processRunner
        let runtime = runtime
        DispatchQueue.global(qos: .utility).async {
            let result = processRunner(invocation)
            let setup = runtime.localSetupSnapshot()
            DispatchQueue.main.async {
                if self.refreshRequested {
                    self.refreshRequested = false
                    self.readStatus()
                    return
                }
                if setup.configurationStamp != self.localSetup.configurationStamp {
                    self.setupCheck = nil
                }
                self.localSetup = setup
                self.serviceStatus = result.isSuccess || result.exitCode == 127
                    ? MindRoomServiceStatus.parse(result.output)
                    : MindRoomServiceStatus(state: .unknown, message: "Could not check the background service. Refresh Status to retry.")
                self.hasRefreshedStatus = true
                self.isRefreshingStatus = false
            }
        }
    }

    func run(_ command: MindRoomCommand) {
        switch command {
        case .openDashboard:
            showSection(.dashboard)
        case .openHostedChat:
            showSection(.chat)
        case .openConfigFolder:
            NSWorkspace.shared.open(runtime.localAgentsConfigURL.deletingLastPathComponent())
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
        feedback = nil
        needsReconnectConfirmation = false
        switch action {
        case .installRuntime, .updateRuntime, .initializeHostedConfig, .initializeSelfHostedConfig, .pairHosted, .reconnectHosted, .checkSetup:
            setupCheck = nil
        default: break
        }
        runningCommandTitle = command.title
        let invocation = runtime.command(for: action)
        let processRunner = processRunner
        let runtime = runtime
        DispatchQueue.global(qos: .userInitiated).async {
            let before = runtime.localSetupSnapshot()
            var result = processRunner(invocation)
            let after = runtime.localSetupSnapshot()
            if action == .checkSetup && before.configurationStamp != after.configurationStamp {
                result = CommandResult(exitCode: 1, output: "Configuration changed while checking. Run Check Setup again.")
            }
            let completedResult = result
            DispatchQueue.main.async {
                if action == .checkSetup {
                    self.localSetup = after
                    self.setupCheck = completedResult
                }
                self.runningCommandTitle = nil
                self.lastOutput = completedResult.output
                // An already-connected Mac is a question for the user, not a failed action.
                let alreadyConnected = action == .pairHosted && completedResult.exitCode == MindRoomCommand.alreadyConnectedExitCode
                self.feedback = alreadyConnected ? nil : CommandFeedback(
                    title: command.title, successMessage: command.successMessage, result: completedResult,
                    needsAttention: action == .checkSetup && completedResult.isSuccess && !completedResult.setupCheckPassed
                )
                if alreadyConnected {
                    self.needsReconnectConfirmation = true
                }
                self.onCommandFinished?(command, completedResult)
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
