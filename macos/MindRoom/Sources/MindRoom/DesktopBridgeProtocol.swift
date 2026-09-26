import CoreFoundation
import Foundation

let desktopBridgeProtocolVersion = 1
let desktopBridgeMaximumRequestBytes = 65_536
let desktopBridgeMaximumPendingRequests = 8
let desktopBridgeMaximumOutputBytes = 262_144

func isDesktopBridgeProtocolVersion(_ value: Any?) -> Bool {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return false }
    let floatingPointEncodings = Set(["f", "d", "D"])
    guard !floatingPointEncodings.contains(String(cString: number.objCType)) else { return false }
    return number.intValue == desktopBridgeProtocolVersion
}

enum DesktopBridgeDecodeDisposition: Equatable {
    case accepted
    case ignoredExpiredResponse
    case protocolFailure
}

struct DesktopBridgeErrorPayload: Codable, Error, Equatable {
    let code: String
    let message: String
    let recovery: String?
    let retryable: Bool
}

struct DesktopPermissionStatus: Codable, Equatable {
    let state: String
    let canRequest: Bool
    let recovery: String?

    enum CodingKeys: String, CodingKey {
        case state, recovery
        case canRequest = "can_request"
    }
}

struct DesktopConfigStatus: Codable, Equatable {
    let state: String
    let revision: Int
    let enabled: Bool
    let controllerUserID: String?
    let controllerDeviceID: String?
    let allowedRequesterIDs: [String]?
    let allowedAgentNames: [String]?
    let allowedAppIDs: [String]?
    var fileRoots: [String] = []
    var shellEnabled = false

    enum CodingKeys: String, CodingKey {
        case state, revision, enabled
        case controllerUserID = "controller_user_id"
        case controllerDeviceID = "controller_device_id"
        case allowedRequesterIDs = "allowed_requester_ids"
        case allowedAgentNames = "allowed_agent_names"
        case allowedAppIDs = "allowed_app_ids"
        case fileRoots = "file_roots"
        case shellEnabled = "shell_enabled"
    }
}

extension DesktopConfigStatus {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        state = try container.decode(String.self, forKey: .state)
        revision = try container.decode(Int.self, forKey: .revision)
        enabled = try container.decode(Bool.self, forKey: .enabled)
        controllerUserID = try container.decodeIfPresent(String.self, forKey: .controllerUserID)
        controllerDeviceID = try container.decodeIfPresent(String.self, forKey: .controllerDeviceID)
        allowedRequesterIDs = try container.decodeIfPresent([String].self, forKey: .allowedRequesterIDs)
        allowedAgentNames = try container.decodeIfPresent([String].self, forKey: .allowedAgentNames)
        allowedAppIDs = try container.decodeIfPresent([String].self, forKey: .allowedAppIDs)
        // Absent folder and shell fields mean those capabilities are off.
        fileRoots = try container.decodeIfPresent([String].self, forKey: .fileRoots) ?? []
        shellEnabled = try container.decodeIfPresent(Bool.self, forKey: .shellEnabled) ?? false
    }
}

enum DesktopSessionState: String, Codable {
    case missing, ready, invalid
}

struct DesktopPairingStatus: Codable, Equatable {
    let state: String
    let sessionState: DesktopSessionState
    let homeserver: String?
    let userID: String?
    let deviceID: String?
    let controllerFingerprint: String?

    enum CodingKeys: String, CodingKey {
        case state, homeserver
        case sessionState = "session_state"
        case userID = "user_id"
        case deviceID = "device_id"
        case controllerFingerprint = "controller_fingerprint"
    }
}

struct DesktopHelperStatus: Codable, Equatable {
    let state: String
    let version: String
}

struct DesktopRuntimeStatus: Codable, Equatable {
    let state: String
    let activeAction: String?
    let lastError: DesktopBridgeErrorPayload?

    enum CodingKeys: String, CodingKey {
        case state
        case activeAction = "active_action"
        case lastError = "last_error"
    }
}

struct DesktopAuthorityStatus: Codable, Equatable {
    let controlAvailable: Bool
    let leaseRemainingSeconds: Double
    let leaseExpiresAtMilliseconds: Double?
    let emergencyStopLatched: Bool

    enum CodingKeys: String, CodingKey {
        case controlAvailable = "control_available"
        case leaseRemainingSeconds = "lease_remaining_seconds"
        case leaseExpiresAtMilliseconds = "lease_expires_at_ms"
        case emergencyStopLatched = "emergency_stop_latched"
    }
}

struct DesktopPermissionsStatus: Codable, Equatable {
    let accessibility: DesktopPermissionStatus
    let screenRecording: DesktopPermissionStatus

    enum CodingKeys: String, CodingKey {
        case accessibility
        case screenRecording = "screen_recording"
    }
}

struct DesktopBrowserStatus: Codable, Equatable {
    let configured: Bool
    let executablePath: String?
    let userDataDirectory: String?
    let runtime: String
    let extensionState: String
    let reconnectTokenConfigured: Bool
    let lastError: DesktopBridgeErrorPayload?

    enum CodingKeys: String, CodingKey {
        case configured, runtime
        case executablePath = "executable_path"
        case userDataDirectory = "user_data_dir"
        case extensionState = "extension"
        case reconnectTokenConfigured = "reconnect_token_configured"
        case lastError = "last_error"
    }
}

struct DesktopApplicationStatus: Codable, Equatable, Identifiable {
    let id: String
    let name: String
    let installed: Bool?
    let running: Bool?
}

/// One shell command waiting for local approval; only its request ID is ever sent back.
struct DesktopShellRequest: Codable, Equatable, Identifiable {
    let requestID: String
    let requesterID: String
    let agentName: String
    let command: String
    let cwd: String
    let expiresAtMilliseconds: Double

    var id: String { requestID }

    enum CodingKeys: String, CodingKey {
        case command, cwd
        case requestID = "request_id"
        case requesterID = "requester_id"
        case agentName = "agent_name"
        case expiresAtMilliseconds = "expires_at_ms"
    }
}

struct DesktopShellHandle: Codable, Equatable, Identifiable {
    let handle: String
    let requesterID: String
    let agentName: String
    let commandPreview: String
    let elapsedSeconds: Double
    let state: String

    var id: String { handle }

    enum CodingKeys: String, CodingKey {
        case handle, state
        case requesterID = "requester_id"
        case agentName = "agent_name"
        case commandPreview = "command_preview"
        case elapsedSeconds = "elapsed_seconds"
    }
}

struct DesktopShellStatus: Codable, Equatable {
    var enabled = false
    var pending: DesktopShellRequest?
    var autoApproveRemainingSeconds = 0.0
    var autoApproveUntilRevoked = false
    var activeRequestID: String?
    var handles: [DesktopShellHandle] = []

    enum CodingKeys: String, CodingKey {
        case enabled, pending, handles
        case autoApproveRemainingSeconds = "auto_approve_remaining_seconds"
        case autoApproveUntilRevoked = "auto_approve_until_revoked"
        case activeRequestID = "active_request_id"
    }
}

extension DesktopShellStatus {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        enabled = try container.decodeIfPresent(Bool.self, forKey: .enabled) ?? false
        pending = try container.decodeIfPresent(DesktopShellRequest.self, forKey: .pending)
        autoApproveRemainingSeconds = try container.decodeIfPresent(Double.self, forKey: .autoApproveRemainingSeconds) ?? 0
        autoApproveUntilRevoked = try container.decodeIfPresent(Bool.self, forKey: .autoApproveUntilRevoked) ?? false
        activeRequestID = try container.decodeIfPresent(String.self, forKey: .activeRequestID)
        handles = try container.decodeIfPresent([DesktopShellHandle].self, forKey: .handles) ?? []
    }
}

enum DesktopShellAutoApproval: Hashable, Identifiable {
    case minutes(Int), untilStopped

    static let choices: [Self] = [.minutes(5), .minutes(15), .minutes(60), .untilStopped]

    var id: Self { self }

    var grantParameters: [String: Any] {
        switch self {
        case let .minutes(minutes): ["duration_seconds": minutes * 60]
        case .untilStopped: ["until_revoked": true]
        }
    }
}

enum DesktopShellDecision: Equatable {
    case reject, approveOnce, approveAndAllow(DesktopShellAutoApproval)

    func parameters(commandID: String) -> [String: Any] {
        var parameters: [String: Any] = ["command_id": commandID, "approved": self != .reject, "auto_approve_seconds": 0]
        switch self {
        case .approveAndAllow(.minutes(let minutes)): parameters["auto_approve_seconds"] = minutes * 60
        case .approveAndAllow(.untilStopped): parameters["auto_approve_until_revoked"] = true
        case .reject, .approveOnce: break
        }
        return parameters
    }
}

struct DesktopStatus: Codable, Equatable {
    let config: DesktopConfigStatus
    let pairing: DesktopPairingStatus
    let helper: DesktopHelperStatus
    let bridge: DesktopRuntimeStatus
    let authority: DesktopAuthorityStatus
    let permissions: DesktopPermissionsStatus
    let browser: DesktopBrowserStatus
    let apps: [DesktopApplicationStatus]
    let capabilities: [String]
    var shell = DesktopShellStatus()

    enum CodingKeys: String, CodingKey {
        case config, pairing, helper, bridge, authority, permissions, browser, apps, capabilities, shell
    }

    static let stopped = DesktopStatus(
        config: DesktopConfigStatus(
            state: "missing",
            revision: 0,
            enabled: false,
            controllerUserID: nil,
            controllerDeviceID: nil,
            allowedRequesterIDs: nil,
            allowedAgentNames: nil,
            allowedAppIDs: nil
        ),
        pairing: DesktopPairingStatus(
            state: "unpaired",
            sessionState: .missing,
            homeserver: nil,
            userID: nil,
            deviceID: nil,
            controllerFingerprint: nil
        ),
        helper: DesktopHelperStatus(state: "stopped", version: "unknown"),
        bridge: DesktopRuntimeStatus(state: "stopped", activeAction: nil, lastError: nil),
        authority: DesktopAuthorityStatus(
            controlAvailable: false,
            leaseRemainingSeconds: 0,
            leaseExpiresAtMilliseconds: nil,
            emergencyStopLatched: false
        ),
        permissions: DesktopPermissionsStatus(
            accessibility: DesktopPermissionStatus(state: "unknown", canRequest: false, recovery: nil),
            screenRecording: DesktopPermissionStatus(state: "unknown", canRequest: false, recovery: nil)
        ),
        browser: DesktopBrowserStatus(
            configured: false,
            executablePath: nil,
            userDataDirectory: nil,
            runtime: "missing",
            extensionState: "disabled",
            reconnectTokenConfigured: false,
            lastError: nil
        ),
        apps: [],
        capabilities: []
    )
}

extension DesktopStatus {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        config = try container.decode(DesktopConfigStatus.self, forKey: .config)
        pairing = try container.decode(DesktopPairingStatus.self, forKey: .pairing)
        helper = try container.decode(DesktopHelperStatus.self, forKey: .helper)
        bridge = try container.decode(DesktopRuntimeStatus.self, forKey: .bridge)
        authority = try container.decode(DesktopAuthorityStatus.self, forKey: .authority)
        permissions = try container.decode(DesktopPermissionsStatus.self, forKey: .permissions)
        browser = try container.decode(DesktopBrowserStatus.self, forKey: .browser)
        apps = try container.decode([DesktopApplicationStatus].self, forKey: .apps)
        capabilities = try container.decode([String].self, forKey: .capabilities)
        shell = try container.decodeIfPresent(DesktopShellStatus.self, forKey: .shell) ?? DesktopShellStatus()
    }
}
