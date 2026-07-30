import Foundation

enum MindRoomCommand: Equatable {
    case installRuntime
    case updateRuntime
    case installService
    case startService
    case stopService
    case restartService
    case serviceStatus
    case initializeHostedConfig
    case initializeSelfHostedConfig
    case localStackSetup
    case pairHosted(pairCode: String)
    case openDashboard
    case openHostedChat
    case openConfigFolder
    case openLogsFolder

    var title: String {
        switch self {
        case .installRuntime:
            return "Install MindRoom Runtime"
        case .updateRuntime:
            return "Update MindRoom Runtime"
        case .installService:
            return "Install/Ensure Service"
        case .startService:
            return "Start Service"
        case .stopService:
            return "Stop Service"
        case .restartService:
            return "Restart Service"
        case .serviceStatus:
            return "Refresh Status"
        case .initializeHostedConfig:
            return "Initialize Hosted Config"
        case .initializeSelfHostedConfig:
            return "Initialize Self-Hosted Config"
        case .localStackSetup:
            return "Run Local Stack Setup"
        case .pairHosted:
            return "Pair Hosted MindRoom..."
        case .openDashboard:
            return "Open Dashboard"
        case .openHostedChat:
            return "Open chat.mindroom.chat"
        case .openConfigFolder:
            return "Open Config Folder"
        case .openLogsFolder:
            return "Open Logs Folder"
        }
    }

    var successMessage: String? {
        switch self {
        case .installRuntime:
            return "The MindRoom runtime is installed.\n\nNext: Initialize Hosted Config."
        case .updateRuntime:
            return "The MindRoom runtime is up to date."
        case .installService:
            return "The MindRoom background service is installed and running.\n\nNext: Open Dashboard."
        case .startService:
            return "The MindRoom service was started.\n\nNext: Open Dashboard."
        case .stopService:
            return "The MindRoom service was stopped."
        case .restartService:
            return "The MindRoom service was restarted."
        case .initializeHostedConfig:
            return "Config files are ready in ~/.mindroom.\n\nNext: use Open chat.mindroom.chat, sign in to create your hosted account, click the Local MindRoom icon in the sidebar to generate a pair code, then use Pair Hosted MindRoom..."
        case .initializeSelfHostedConfig:
            return "Config files are ready in ~/.mindroom.\n\nEdit config.yaml and .env to point at your Matrix homeserver and model provider, then use Install/Ensure Service."
        case .localStackSetup:
            return "Local stack setup finished."
        case .pairHosted:
            return "Paired with hosted MindRoom.\n\nNext: Install/Ensure Service, then Open Dashboard."
        case .serviceStatus, .openDashboard, .openHostedChat, .openConfigFolder, .openLogsFolder:
            return nil
        }
    }

    var runtimeAction: MindRoomRuntimeAction? {
        switch self {
        case .installRuntime:
            return .installRuntime
        case .updateRuntime:
            return .updateRuntime
        case .installService:
            return .installService
        case .startService:
            return .startService
        case .stopService:
            return .stopService
        case .restartService:
            return .restartService
        case .serviceStatus:
            return .serviceStatus
        case .initializeHostedConfig:
            return .initializeHostedConfig
        case .initializeSelfHostedConfig:
            return .initializeSelfHostedConfig
        case .localStackSetup:
            return .localStackSetup
        case let .pairHosted(pairCode):
            return .pairHosted(pairCode: pairCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased())
        case .openDashboard, .openHostedChat, .openConfigFolder, .openLogsFolder:
            return nil
        }
    }
}
