import Foundation

extension MindRoomServiceState {
    var primaryAction: MindRoomCommand? {
        switch self {
        case .running: return .stopService
        case .stopped: return .startService
        case .runtimeMissing: return .installRuntime
        case .notInstalled: return .installService
        case .unknown: return nil
        }
    }

    var canOpenDashboard: Bool { self == .running }

    var shortTitle: String {
        switch self {
        case .running: return "Service running"
        case .stopped: return "Stopped"
        case .runtimeMissing, .notInstalled: return "Setup needed"
        case .unknown: return "Checking / unavailable"
        }
    }

    var needsSetup: Bool { self == .runtimeMissing || self == .notInstalled }
}
