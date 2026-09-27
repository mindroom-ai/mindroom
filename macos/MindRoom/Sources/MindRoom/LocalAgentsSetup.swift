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
    var configurationStamp: [Date?] = []

    var runtimeInstalled: Bool { runtimePath != nil }

    func nextStep(service: MindRoomServiceState, check: CommandResult?) -> LocalAgentsSetupStep {
        if service == .running || service == .stopped { return .start }
        if !runtimeInstalled { return .install }
        if !configurationExists { return .configure }
        return check?.isSuccess == true ? .start : .check
    }

    func canStart(service: MindRoomServiceState) -> Bool {
        runtimeInstalled && configurationExists && (service == .notInstalled || service == .stopped)
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
            return check.isSuccess ? .complete("Passed last check") : .needsAction("Needs attention")
        case .start:
            switch service {
            case .running: return .complete("Running")
            case .stopped: return .idle("Stopped")
            case .notInstalled: return .idle("Not installed")
            case .runtimeMissing: return .idle("Finish setup")
            case .unknown: return .idle("Unavailable")
            }
        }
    }
}
