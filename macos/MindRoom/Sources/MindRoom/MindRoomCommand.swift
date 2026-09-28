import Foundation

enum MindRoomCommand: Equatable {
    case installRuntime
    case updateRuntime
    case installService
    case startService
    case stopService
    case restartService
    case serviceStatus
    case checkSetup
    case initializeHostedConfig
    case initializeSelfHostedConfig
    case pairHosted
    case reconnectHosted
    case openDashboard
    case openHostedChat
    case openConfigFolder
    case openLogsFolder

    /// Exit code of `mindroom connect` when this Mac is already connected and pairing was not forced.
    /// Matches `_CONNECT_ALREADY_CONNECTED_EXIT_CODE` in src/mindroom/cli/main.py.
    static let alreadyConnectedExitCode: Int32 = 3

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
        case .checkSetup:
            return "Check Setup"
        case .initializeHostedConfig:
            return "Prepare Configuration"
        case .initializeSelfHostedConfig:
            return "Prepare Self-Hosted Configuration"
        case .pairHosted:
            return "Pair Chat Account"
        case .reconnectHosted:
            return "Reconnect Chat Account"
        case .openDashboard:
            return "Open Dashboard"
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
            return "The command-line runtime is installed. The background service is a separate step. Continue to Configure, or use your existing configuration."
        case .updateRuntime:
            return "The runtime update finished. In Settings, use Apply Runtime to Service to start or restart local agents with this version."
        case .installService:
            return "The background service was installed and started. Open Chat or Open Dashboard to check that your agents are ready."
        case .startService:
            return "The service start command finished. Open Chat or Open Dashboard to check that your agents are ready."
        case .stopService:
            return "The MindRoom service was stopped."
        case .restartService:
            return "The MindRoom service was restarted."
        case .initializeHostedConfig:
            return "Configuration is ready. Existing files were kept. Click Connect Account to approve this Mac in MindRoom Chat."
        case .initializeSelfHostedConfig:
            return "Configuration is ready. Edit your configuration and .env for your Matrix server and model provider, then install and start agents."
        case .pairHosted:
            return "The chat account was paired. Configure an AI provider, then install and start agents."
        case .reconnectHosted:
            return "The chat account was paired again. Existing agents keep working; new agents get the new namespace."
        case .checkSetup:
            return "The setup check finished. Review the Check summary before continuing to Start."
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
        case .checkSetup:
            return .checkSetup
        case .initializeHostedConfig:
            return .initializeHostedConfig
        case .initializeSelfHostedConfig:
            return .initializeSelfHostedConfig
        case .pairHosted:
            return .pairHosted
        case .reconnectHosted:
            return .reconnectHosted
        case .openDashboard, .openHostedChat, .openConfigFolder, .openLogsFolder:
            return nil
        }
    }
}
