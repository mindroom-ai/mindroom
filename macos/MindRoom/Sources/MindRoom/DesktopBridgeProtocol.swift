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

    enum CodingKeys: String, CodingKey {
        case state, revision, enabled
        case controllerUserID = "controller_user_id"
        case controllerDeviceID = "controller_device_id"
        case allowedRequesterIDs = "allowed_requester_ids"
        case allowedAgentNames = "allowed_agent_names"
        case allowedAppIDs = "allowed_app_ids"
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
