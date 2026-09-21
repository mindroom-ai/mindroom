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
            return "Install and Start Agents"
        case .startService:
            return "Start Service"
        case .stopService:
            return "Stop Service"
        case .restartService:
            return "Restart Service"
        case .serviceStatus:
            return "Refresh Status"
        case .initializeHostedConfig:
            return "Prepare Configuration"
        case .initializeSelfHostedConfig:
            return "Prepare Self-Hosted Configuration"
        case .localStackSetup:
            return "Run Local Stack Setup"
        case .pairHosted:
            return "Pair Chat Account"
        case .openDashboard:
            return "Configure Agents"
        case .openHostedChat:
            return "Open Chat"
        case .openConfigFolder:
            return "Open Config Folder"
        case .openLogsFolder:
            return "Open Logs Folder"
        }
    }

    var successMessage: String? {
        switch self {
        case .installRuntime:
            return "The MindRoom runtime is installed. Continue setup in Local agents, or pair an agent in Computer access."
        case .updateRuntime:
            return "The runtime update finished. In Settings, use Apply Runtime to Service to start or restart local agents with this version."
        case .installService:
            return "The background service was installed and started. Open Chat or Configure Agents to check that your agents are ready."
        case .startService:
            return "The service start command finished. Open Chat or Configure Agents to check that your agents are ready."
        case .stopService:
            return "The MindRoom service was stopped."
        case .restartService:
            return "The MindRoom service was restarted."
        case .initializeHostedConfig:
            return "Configuration is ready in ~/.mindroom. Existing files were kept. Open MindRoom Chat, sign in, and use Local MindRoom in the chat sidebar to generate a pair code."
        case .initializeSelfHostedConfig:
            return "Configuration is ready in ~/.mindroom. Edit config.yaml and .env for your Matrix server and model provider, then install and start agents."
        case .localStackSetup:
            return "Local stack setup finished."
        case .pairHosted:
            return "The chat account was paired. Configure an AI provider, then install and start agents."
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
