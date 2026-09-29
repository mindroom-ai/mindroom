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
    var runtimeVersion: String?
    /// The runtime release this app was built with, or nil when any runtime is accepted.
    var requiredRuntimeVersion: String?
    var configurationExists = false
    var configurationDisplayPath: String?
    var configurationStamp: LocalAgentsConfigurationStamp?

    var runtimeInstalled: Bool { runtimePath != nil }

    /// The app passes CLI options that only its own runtime release is guaranteed to accept,
    /// so a different installed runtime, for example after an app update, must be updated first.
    var runtimeUpdateReason: String? {
        guard runtimeInstalled, let requiredRuntimeVersion, runtimeVersion != requiredRuntimeVersion else { return nil }
        let installed = runtimeVersion.map { "runtime \($0) is installed" } ?? "the installed runtime version is unknown"
        return "This app needs MindRoom runtime \(requiredRuntimeVersion), but \(installed)."
    }

    var runtimeReady: Bool { runtimeInstalled && runtimeUpdateReason == nil }

    func nextStep(service: MindRoomServiceState, check: CommandResult?) -> LocalAgentsSetupStep {
        if runtimeUpdateReason != nil { return .install }
        if service == .pairing { return .configure }
        if service == .running || service == .stopped { return .start }
        if !runtimeInstalled { return .install }
        if !configurationExists { return .configure }
        return check?.setupCheckPassed == true ? .start : .check
    }

    func canStart(service: MindRoomServiceState) -> Bool {
        // Installed services keep the configuration path and runtime version saved by the CLI.
        (service == .stopped && runtimeInstalled) || (service == .notInstalled && configurationExists && runtimeReady)
    }

    func progress(for step: LocalAgentsSetupStep, service: MindRoomServiceState,
                  check: CommandResult?) -> SetupStepProgress {
        switch step {
        case .install:
            if runtimeUpdateReason != nil { return .needsAction("Update needed") }
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
