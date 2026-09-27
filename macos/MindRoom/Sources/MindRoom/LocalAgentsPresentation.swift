import Foundation

extension MindRoomServiceState {
    static let dashboardAfterPairingHint = "The dashboard opens after your chat account is connected."

    var primaryAction: MindRoomCommand? {
        switch self {
        case .running, .pairing: return .stopService
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
        case .pairing: return "Waiting for chat account"
        case .stopped: return "Stopped"
        case .runtimeMissing, .notInstalled: return "Setup needed"
        case .unknown: return "Checking / unavailable"
        }
    }

    var needsSetup: Bool { self == .runtimeMissing || self == .notInstalled }

    /// Opens the setup steps when they are needed, including Connect Account while the service waits for pairing.
    var expandsSetup: Bool { needsSetup || self == .pairing }
}
