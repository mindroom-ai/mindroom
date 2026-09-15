import AppKit
import Combine
import Foundation

@MainActor
final class DesktopControlStore: ObservableObject {
    static let shared = DesktopControlStore()

    @Published private(set) var status = DesktopStatus.stopped
    @Published private(set) var isBusy = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var recovery: String?
    @Published private(set) var verification = ""
    @Published private(set) var confirmationCommand = ""
    @Published private(set) var leaseRemainingSeconds = 0

    @Published var homeserver = "https://mindroom.chat"
    @Published var matrixUserID = ""
    @Published var matrixPassword = ""
    @Published var pairingCode = ""
    @Published var setupDescriptor = ""
    @Published private(set) var accessGatewayRequired = false
    @Published var controllerUserID = "" { didSet { invalidateConfirmation() } }
    @Published var controllerDeviceID = "" { didSet { invalidateConfirmation() } }
    @Published var controllerFingerprint = "" { didSet { invalidateConfirmation() } }
    @Published var requesterIDs = "" { didSet { invalidateConfirmation() } }
    @Published var agentNames = "" { didSet { invalidateConfirmation() } }
    @Published var selectedAppIDs = Set<String>()
    @Published var browserEnabled = false
    @Published var browserExecutable = ""
    @Published var browserProfile = ""
    @Published var controlMinutes = 15

    let applications = InstalledApplicationCatalog.applications()
    private let helper: DesktopBridgeProcess
    private var subscriptions = Set<AnyCancellable>()
    private var countdownTimer: Timer?
    @Published private var confirmedIdentity: String?
    private var observedConfigRevision = 0
    private var pendingOperationCount = 0

    init(helper: DesktopBridgeProcess = DesktopBridgeProcess()) {
        self.helper = helper
        helper.$status
            .receive(on: RunLoop.main)
            .sink { [weak self] value in
                self?.status = value
                self?.hydrateConfiguration(from: value)
                self?.updateCountdown()
            }
            .store(in: &subscriptions)
        helper.onExit = { [weak self] in
            self?.status = .stopped
            self?.updateCountdown()
        }
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.updateCountdown() }
        }
        RunLoop.main.add(timer, forMode: .common)
        countdownTimer = timer
    }

    var desktopStatusLabel: String {
        switch status.bridge.state {
        case "control":
            return "Control · \(leaseRemainingSeconds / 60)m \(leaseRemainingSeconds % 60)s"
        case "observe_only":
            return "Observe only"
        case "faulted":
            return "Attention required"
        default:
            return "Stopped"
        }
    }

    var identityConfirmed: Bool {
        get { confirmedIdentity == currentIdentity }
        set { confirmedIdentity = newValue ? currentIdentity : nil }
    }

    var canEditBrowserConfiguration: Bool {
        status.helper.state != "stopped"
    }

    func refresh() {
        perform("status")
    }

    func saveConfiguration() {
        guard identityConfirmed else {
            errorMessage = "Confirm the displayed controller, requester, and agent before saving."
            return
        }
        let config: [String: Any] = [
            "v": 1,
            "revision": status.config.revision,
            "enabled": true,
            "controller": [
                "user_id": controllerUserID,
                "device_id": controllerDeviceID,
                "ed25519": controllerFingerprint,
            ],
            "allowed_requester_ids": split(requesterIDs),
            "allowed_agent_names": split(agentNames),
            "allowed_app_ids": Array(selectedAppIDs).sorted(),
            "capture": ["max_screenshot_width": 1568, "jpeg_quality": 80],
            "browser": [
                "enabled": browserEnabled,
                "executable_path": browserExecutable.nilIfBlank,
                "user_data_dir": browserProfile.nilIfBlank,
                "timeout_seconds": 90,
            ],
        ]
        perform("configure", parameters: ["expected_revision": status.config.revision, "config": config])
    }

    func importSetupDescriptor() {
        guard
            let firstBrace = setupDescriptor.firstIndex(of: "{"),
            let lastBrace = setupDescriptor.lastIndex(of: "}"),
            firstBrace <= lastBrace,
            let data = String(setupDescriptor[firstBrace ... lastBrace]).data(using: .utf8),
            let descriptor = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            errorMessage = "The setup descriptor is not valid JSON."
            recovery = "Copy the complete setup descriptor from the same MindRoom chat."
            return
        }
        perform("import_setup", parameters: ["descriptor": descriptor]) { [weak self] result in
            self?.homeserver = result["homeserver"] as? String ?? ""
            self?.matrixUserID = result["user_id"] as? String ?? ""
            self?.pairingCode = result["code"] as? String ?? ""
            self?.controllerUserID = result["controller_user_id"] as? String ?? ""
            self?.controllerDeviceID = result["controller_device_id"] as? String ?? ""
            self?.controllerFingerprint = result["controller_ed25519"] as? String ?? ""
            self?.requesterIDs = result["requester_id"] as? String ?? ""
            self?.agentNames = result["agent_name"] as? String ?? ""
            self?.accessGatewayRequired = result["cloudflare_access"] as? Bool ?? false
            self?.setupDescriptor = ""
            self?.confirmedIdentity = nil
        }
    }

    func login(replace: Bool = false) {
        var parameters: [String: Any] = [
            "homeserver": homeserver,
            "method": "password",
            "user_id": matrixUserID,
            "password": matrixPassword,
            "replace": replace,
        ]
        if matrixPassword.isEmpty {
            parameters["method"] = "sso"
            parameters.removeValue(forKey: "password")
            if matrixUserID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                parameters.removeValue(forKey: "user_id")
            }
        }
        perform("login", parameters: parameters) { [weak self] _ in
            self?.matrixPassword = ""
        }
    }

    func pair() {
        guard identityConfirmed, configurationMatchesCurrentIdentity else {
            errorMessage = "Confirm the current identities after saving this configuration."
            recovery = "Review the controller fingerprint, requester, and agent, then confirm again."
            return
        }
        perform("pair", parameters: ["code": pairingCode]) { [weak self] result in
            self?.verification = result["verification"] as? String ?? ""
            self?.confirmationCommand = result["confirmation_command"] as? String ?? ""
        }
    }

    func start() { perform("start") }
    func stop() { perform("stop", timeout: .seconds(120), urgent: true) }
    func grantControl() {
        perform("grant_control", parameters: ["duration_seconds": controlMinutes * 60])
    }
    func revokeControl() { perform("revoke_control", urgent: true) }
    func resetEmergencyStop() { perform("reset_emergency_stop", urgent: true) }
    func requestPermission(_ permission: String) {
        perform("request_permission", parameters: ["permission": permission])
    }
    func connectBrowser() { perform("browser_connect") }
    func disconnectBrowser() { perform("browser_disconnect") }

    func openPermissionSettings(_ permission: String) {
        let anchor = permission == "accessibility" ? "Privacy_Accessibility" : "Privacy_ScreenCapture"
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(anchor)") {
            NSWorkspace.shared.open(url)
        }
    }

    func copyDiagnostics() {
        let diagnostics: [String: Any] = [
            "helper_version": status.helper.version,
            "helper_state": status.helper.state,
            "bridge_state": status.bridge.state,
            "config_state": status.config.state,
            "config_revision": status.config.revision,
            "allowed_app_count": status.config.allowedAppIDs?.count ?? status.apps.count,
            "accessibility": status.permissions.accessibility.state,
            "screen_recording": status.permissions.screenRecording.state,
            "browser_runtime": status.browser.runtime,
            "browser_extension": status.browser.extensionState,
            "control_available": status.authority.controlAvailable,
            "emergency_stop_latched": status.authority.emergencyStopLatched,
            "last_error_code": status.bridge.lastError.map { $0.code as Any } ?? NSNull(),
        ]
        let data = try? JSONSerialization.data(withJSONObject: diagnostics, options: [.prettyPrinted, .sortedKeys])
        let statusText = data.flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(statusText, forType: .string)
    }

    @discardableResult
    func shutdownHelper(completion: @escaping () -> Void) -> Bool {
        helper.shutdown(completion: completion)
    }

    private func perform(
        _ action: String,
        parameters: [String: Any] = [:],
        timeout: Duration = .seconds(35),
        urgent: Bool = false,
        completion: (([String: Any]) -> Void)? = nil
    ) {
        guard urgent || !isBusy else { return }
        pendingOperationCount += 1
        isBusy = true
        errorMessage = nil
        recovery = nil
        Task {
            do {
                let result = try await helper.request(action: action, parameters: parameters, timeout: timeout)
                completion?(result)
            } catch let DesktopBridgeProcessError.helper(error) {
                errorMessage = error.message
                recovery = error.recovery
            } catch {
                errorMessage = error.localizedDescription
            }
            pendingOperationCount = max(0, pendingOperationCount - 1)
            isBusy = pendingOperationCount > 0
        }
    }

    private func updateCountdown() {
        if let expiry = status.authority.leaseExpiresAtMilliseconds {
            leaseRemainingSeconds = max(0, Int(ceil(expiry / 1000 - Date().timeIntervalSince1970)))
        } else {
            leaseRemainingSeconds = max(0, Int(status.authority.leaseRemainingSeconds.rounded(.up)))
        }
    }

    func hydrateConfiguration(from value: DesktopStatus) {
        let shouldHydrateBrowser = value.config.state == "ready" && observedConfigRevision != value.config.revision
        if observedConfigRevision != value.config.revision {
            confirmedIdentity = nil
        }
        observedConfigRevision = value.config.revision
        if controllerUserID.isEmpty { controllerUserID = value.config.controllerUserID ?? "" }
        if controllerDeviceID.isEmpty { controllerDeviceID = value.config.controllerDeviceID ?? "" }
        if requesterIDs.isEmpty { requesterIDs = value.config.allowedRequesterIDs?.joined(separator: ", ") ?? "" }
        if agentNames.isEmpty { agentNames = value.config.allowedAgentNames?.joined(separator: ", ") ?? "" }
        if selectedAppIDs.isEmpty { selectedAppIDs = Set(value.config.allowedAppIDs ?? []) }
        if controllerFingerprint.isEmpty {
            controllerFingerprint = value.pairing.controllerFingerprint ?? ""
        }
        if shouldHydrateBrowser {
            browserEnabled = value.browser.configured
            browserExecutable = value.browser.executablePath ?? ""
            browserProfile = value.browser.userDataDirectory ?? ""
        }
    }

    private var currentIdentity: String {
        [controllerUserID, controllerDeviceID, controllerFingerprint, requesterIDs, agentNames]
            .joined(separator: "\u{1F}")
    }

    private var configurationMatchesCurrentIdentity: Bool {
        status.config.controllerUserID == controllerUserID
            && status.config.controllerDeviceID == controllerDeviceID
            && status.pairing.controllerFingerprint == controllerFingerprint
            && Set(status.config.allowedRequesterIDs ?? []) == Set(split(requesterIDs))
            && Set(status.config.allowedAgentNames ?? []) == Set(split(agentNames))
    }

    private func invalidateConfirmation() {
        if confirmedIdentity != nil, confirmedIdentity != currentIdentity {
            confirmedIdentity = nil
        }
    }

    private func split(_ value: String) -> [String] {
        value.split(whereSeparator: { $0 == "," || $0.isNewline })
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }
}

private extension String {
    var nilIfBlank: Any {
        trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? NSNull() : self
    }
}
