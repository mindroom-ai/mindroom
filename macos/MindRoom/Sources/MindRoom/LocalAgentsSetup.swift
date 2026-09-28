import Foundation

enum LocalAgentsSetupStep: Int, CaseIterable {
    case install, configure, check, start

    var title: String {
        switch self {
        case .install: "Install"
        case .configure: "Configure"
        case .check: "Check"
        case .start: "Start"
        }
    }
}

struct LocalAgentsSetupSnapshot: Equatable {
    var runtimePath: String?
    var configurationExists = false
    var configurationDisplayPath: String?
    var configurationStamp: LocalAgentsConfigurationStamp?

    var runtimeInstalled: Bool { runtimePath != nil }

    func nextStep(service: MindRoomServiceState, check: CommandResult?) -> LocalAgentsSetupStep {
        if service == .pairing { return .configure }
        if service == .running || service == .stopped { return .start }
        if !runtimeInstalled { return .install }
        if !configurationExists { return .configure }
        return check?.setupCheckPassed == true ? .start : .check
    }

    func canStart(service: MindRoomServiceState) -> Bool {
        // Installed services keep the configuration path saved by the CLI.
        runtimeInstalled && (service == .stopped || (service == .notInstalled && configurationExists))
    }

    func progress(for step: LocalAgentsSetupStep, service: MindRoomServiceState,
                  check: CommandResult?) -> SetupStepProgress {
        switch step {
        case .install:
            return runtimeInstalled ? .complete("Installed") : .needsAction("Needed")
        case .configure:
            return configurationExists ? .complete("Files found") : .needsAction("Needed")
        case .check:
            guard let check else { return .idle("Not checked") }
            return check.setupCheckPassed ? .complete("Passed last check") : .needsAction("Needs attention")
        case .start:
            switch service {
            case .running: return .complete("Running")
            case .pairing: return .needsAction("Waiting for chat account")
            case .stopped: return .idle("Stopped")
            case .notInstalled: return .idle("Not installed")
            case .runtimeMissing: return .idle("Finish setup")
            case .unknown: return .idle("Unavailable")
            }
        }
    }
}

struct LocalAgentsConfigurationStamp: Equatable {
    let configurationURL: URL
    let storagePath: String?
    let modificationDates: [Date?]
}

extension CommandResult {
    /// Doctor exits zero for warnings too. Only its explicit clean summary
    /// completes setup; unfamiliar output from another runtime stays reviewable.
    var setupCheckPassed: Bool {
        isSuccess && setupCheckSummary?.range(of: #"^\d+ passed, 0 failed, 0 warnings?$"#,
                                             options: .regularExpression) != nil
    }

    var setupCheckSummary: String? {
        output.components(separatedBy: .newlines).reversed().map {
            $0.trimmingCharacters(in: .whitespacesAndNewlines)
        }.first {
            $0.range(of: #"^\d+ passed, \d+ failed, \d+ warnings?$"#, options: .regularExpression) != nil
        }
    }
}
