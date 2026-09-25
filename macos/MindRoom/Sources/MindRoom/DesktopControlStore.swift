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

    @Published var homeserver = "https://mindroom.chat" {
        didSet { if observedSession == nil { hasInitialSessionEdits = true } }
    }
    @Published var matrixUserID = "" {
        didSet { if observedSession == nil { hasInitialSessionEdits = true } }
    }
    @Published var matrixPassword = ""
    @Published var pairingCode = ""
    @Published var setupDescriptor = ""
    @Published private(set) var accessGatewayRequired = false
    @Published var controllerUserID = "" { didSet { configurationFieldChanged(\.controllerUserID) } }
    @Published var controllerDeviceID = "" { didSet { configurationFieldChanged(\.controllerDeviceID) } }
    @Published var controllerFingerprint = "" { didSet { configurationFieldChanged(\.controllerFingerprint) } }
    @Published var requesterIDs = "" { didSet { configurationFieldChanged(\.requesterIDs) } }
    @Published var agentNames = "" { didSet { configurationFieldChanged(\.agentNames) } }
    @Published var selectedAppIDs = Set<String>() { didSet { configurationFieldChanged(\.selectedAppIDs) } }
    @Published var browserEnabled = false { didSet { configurationFieldChanged(\.browserEnabled) } }
    @Published var browserExecutable = "" { didSet { configurationFieldChanged(\.browserExecutable) } }
    @Published var browserProfile = "" { didSet { configurationFieldChanged(\.browserProfile) } }
    @Published var controlMinutes = 15

    @Published private(set) var applications = InstalledApplicationCatalog.applications()
    private let helper: DesktopBridgeProcess
    private var subscriptions = Set<AnyCancellable>()
    private var countdownTimer: Timer?
    @Published private var confirmedIdentity: String?
    private var observedConfigRevision = 0
    private var observedConfiguration: DesktopStatus?
    private var observedSession: DesktopPairingStatus?
    // A draft can return to its default value before any saved baseline arrives.
    private var initialConfigurationEdits = Set<PartialKeyPath<DesktopControlStore>>()
    private var hasInitialSessionEdits = false
    private var addedApplicationURLs = Set<URL>()
    private var pendingOperationCount = 0

    init(helper: DesktopBridgeProcess? = nil) {
        let helper = helper ?? DesktopBridgeProcess()
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
            recovery = nil
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

    var hasAppSelectionChanges: Bool {
        selectedAppIDs != Set(status.config.allowedAppIDs ?? [])
    }

    func saveAllowedApplications() {
        guard status.config.state == "ready" else {
            errorMessage = "Complete connection setup before saving app access."
            recovery = nil
            return
        }
        perform(
            "set_allowed_apps",
            parameters: ["expected_revision": status.config.revision, "allowed_app_ids": selectedAppIDs.sorted()],
            stopFirst: status.canStopBridge
        )
    }

    func discardAppSelectionChanges() {
        selectedAppIDs = Set(status.config.allowedAppIDs ?? [])
    }

    func refreshApplications() {
        let imported = addedApplicationURLs.compactMap {
            InstalledApplicationCatalog.application(at: $0)
        }
        var found: [String: InstalledDesktopApplication] = [:]
        for application in imported + InstalledApplicationCatalog.applications() { found[application.id] = application }
        applications = found.values.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }

    func addApplication(at url: URL) -> String? {
        guard let application = InstalledApplicationCatalog.application(at: url) else {
            errorMessage = "Choose a macOS application with a bundle identifier."
            recovery = nil
            return nil
        }
        addedApplicationURLs.insert(url)
        refreshApplications()
        selectedAppIDs.insert(application.id)
        return application.id
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
        stopFirst: Bool = false,
        completion: (([String: Any]) -> Void)? = nil
    ) {
        guard urgent || !isBusy else { return }
        pendingOperationCount += 1
        isBusy = true
        errorMessage = nil
        recovery = nil
        Task {
            do {
                if stopFirst {
                    _ = try await helper.request(action: "stop", timeout: .seconds(120))
                }
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
        if value.pairing.sessionState == .ready {
            if observedSession?.homeserver != value.pairing.homeserver
                || observedSession?.userID != value.pairing.userID
                || observedSession?.deviceID != value.pairing.deviceID {
                confirmedIdentity = nil
            }
            // Keep the login identity together so a prepared SSO login cannot switch accounts or servers.
            if !hasInitialSessionEdits,
               homeserver == (observedSession?.homeserver ?? "https://mindroom.chat"),
               matrixUserID == (observedSession?.userID ?? "") {
                homeserver = value.pairing.homeserver ?? homeserver
                matrixUserID = value.pairing.userID ?? ""
            }
            observedSession = value.pairing
            hasInitialSessionEdits = false
        }
        if observedConfigRevision != value.config.revision {
            confirmedIdentity = nil
        }
        observedConfigRevision = value.config.revision
        guard value.config.state == "ready" else { return }

        // Merge persisted changes only into fields that still match the last saved values.
        // Empty fields and app selections can be intentional unsaved edits.
        let previous = observedConfiguration ?? .stopped
        let initialEdits = initialConfigurationEdits
        observedConfiguration = value
        initialConfigurationEdits.removeAll()
        if !initialEdits.contains(\.controllerUserID),
           controllerUserID == (previous.config.controllerUserID ?? "") {
            controllerUserID = value.config.controllerUserID ?? ""
        }
        if !initialEdits.contains(\.controllerDeviceID),
           controllerDeviceID == (previous.config.controllerDeviceID ?? "") {
            controllerDeviceID = value.config.controllerDeviceID ?? ""
        }
        if !initialEdits.contains(\.requesterIDs),
           requesterIDs == (previous.config.allowedRequesterIDs?.joined(separator: ", ") ?? "") {
            requesterIDs = value.config.allowedRequesterIDs?.joined(separator: ", ") ?? ""
        }
        if !initialEdits.contains(\.agentNames),
           agentNames == (previous.config.allowedAgentNames?.joined(separator: ", ") ?? "") {
            agentNames = value.config.allowedAgentNames?.joined(separator: ", ") ?? ""
        }
        if !initialEdits.contains(\.selectedAppIDs), selectedAppIDs == Set(previous.config.allowedAppIDs ?? []) {
            selectedAppIDs = Set(value.config.allowedAppIDs ?? [])
        }
        if !initialEdits.contains(\.controllerFingerprint),
           controllerFingerprint == (previous.pairing.controllerFingerprint ?? "") {
            controllerFingerprint = value.pairing.controllerFingerprint ?? ""
        }
        if !initialEdits.contains(\.browserEnabled), browserEnabled == previous.browser.configured {
            browserEnabled = value.browser.configured
        }
        if !initialEdits.contains(\.browserExecutable), browserExecutable == (previous.browser.executablePath ?? "") {
            browserExecutable = value.browser.executablePath ?? ""
        }
        if !initialEdits.contains(\.browserProfile), browserProfile == (previous.browser.userDataDirectory ?? "") {
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

    private func configurationFieldChanged(_ field: PartialKeyPath<DesktopControlStore>) {
        if observedConfiguration == nil { initialConfigurationEdits.insert(field) }
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
