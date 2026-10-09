import AppKit
import Combine
import Foundation

typealias MindRoomProcessRunner = (MindRoomCommandInvocation, MindRoomCommandProcess) -> CommandResult

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
    @Published private(set) var pairingApproval: LocalAgentPairingApproval?
    @Published private(set) var isPairing = false
    @Published var pairingCancelled = false

    /// Called on the main actor when a user-initiated command finishes.
    var onCommandFinished: ((MindRoomCommand, CommandResult) -> Void)?

    let inference: LocalInferenceController
    private var inferenceChanges: AnyCancellable?
    private var refreshRequested = false
    private var activeProcess: MindRoomCommandProcess?
    private let runtime: MindRoomRuntime
    private let processRunner: MindRoomProcessRunner
    private let showSection: (AppSection) -> Void

    init(
        runtime: MindRoomRuntime = MindRoomRuntime(),
        processRunner: @escaping MindRoomProcessRunner = { invocation, process in process.run(invocation) },
        showSection: ((AppSection) -> Void)? = nil
    ) {
        self.inference = LocalInferenceController(runtime: runtime)
        self.runtime = runtime
        self.processRunner = processRunner
        self.showSection = showSection ?? { AppWindowController.shared.show(section: $0) }
        self.inferenceChanges = inference.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
    }

    var isRunningCommand: Bool {
        runningCommandTitle != nil || inference.busy
    }

    /// `run()` refuses commands that need the runtime matching this app from every entry point until a
    /// status refresh has read the installed version and found it matching. Settings also uses this to
    /// disable Apply Runtime to Service.
    func isBlockedByRuntimeUpdate(_ command: MindRoomCommand) -> Bool {
        command.requiresMatchingRuntime && (!hasRefreshedStatus || localSetup.runtimeUpdateReason != nil)
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
            let result = processRunner(invocation, MindRoomCommandProcess())
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
                Task { await self.inference.refresh() }
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
            guard let action = command.runtimeAction, !isBlockedByRuntimeUpdate(command) else { return }
            runUserCommand(command, action: action)
        }
    }

    private func runUserCommand(_ command: MindRoomCommand, action: MindRoomRuntimeAction) {
        guard !isRunningCommand else { return }
        feedback = nil
        needsReconnectConfirmation = false
        pairingCancelled = false
        isPairing = action == .pairHosted || action == .reconnectHosted
        let pairing = isPairing
        let process = MindRoomCommandProcess(onOutput: pairing ? { output in
            guard let approval = LocalAgentPairingApproval.parse(output) else { return }
            DispatchQueue.main.async {
                guard self.isPairing, !self.pairingCancelled, self.pairingApproval != approval else { return }
                self.pairingApproval = approval
                self.showSection(.chat)
            }
        } : nil)
        activeProcess = process
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
            var result = processRunner(invocation, process)
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
                self.activeProcess = nil
                self.isPairing = false
                self.pairingApproval = nil
                // A cooperative CLI may finish saving, or report a save error, after Cancel was pressed.
                let stoppedWhileWaiting = completedResult.exitCode == MindRoomCommand.pairingCancelledExitCode
                    || completedResult.exitCode == SIGTERM
                self.pairingCancelled = pairing && process.isCancelled && stoppedWhileWaiting
                self.lastOutput = self.pairingCancelled ? "" : completedResult.output
                // An already-connected Mac is a question for the user, not a failed action.
                let alreadyConnected = action == .pairHosted && completedResult.exitCode == MindRoomCommand.alreadyConnectedExitCode
                self.feedback = alreadyConnected || self.pairingCancelled ? nil : CommandFeedback(
                    title: command.title, successMessage: command.successMessage, result: completedResult,
                    needsAttention: action == .checkSetup && completedResult.isSuccess && !completedResult.setupCheckPassed
                )
                if alreadyConnected {
                    self.needsReconnectConfirmation = true
                }
                if pairing { self.showSection(.localAgents) }
                self.onCommandFinished?(command, completedResult)
                self.refreshStatus()
            }
        }
    }

    func cancelPairing() {
        guard isPairing, let process = activeProcess else { return }
        process.cancel()
        pairingCancelled = process.isCancelled
        pairingApproval = nil
    }
}
