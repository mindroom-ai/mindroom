import AppKit
import Combine
import Foundation

@MainActor
final class DesktopControlStore: ObservableObject {
    typealias Request = (String, [String: Any], Duration) async throws -> [String: Any]

    static let shared = DesktopControlStore()

    @Published private(set) var status = DesktopStatus.stopped
    @Published private(set) var isBusy = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var recovery: String?
    @Published private(set) var verification = ""
    @Published private(set) var confirmationCommand = ""
    @Published private(set) var setupImported = false
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
    @Published var fileRoots: [String] = [] { didSet { configurationFieldChanged(\.fileRoots) } }
    @Published var shellEnabled = false { didSet { configurationFieldChanged(\.shellEnabled) } }
    @Published var controlMinutes = 15

    @Published private(set) var applications = InstalledApplicationCatalog.applications()
    private let helper: DesktopBridgeProcess
    private let request: Request
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
    private var pendingSetup: DesktopSetupSnapshot?

    init(helper: DesktopBridgeProcess? = nil, request: Request? = nil) {
        let helper = helper ?? DesktopBridgeProcess()
        self.helper = helper
        self.request = request ?? { action, parameters, timeout in
            try await helper.request(action: action, parameters: parameters, timeout: timeout)
        }
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
        status.accessModeLabel(controlRemainingSeconds: leaseRemainingSeconds)
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

    var savedSessionMatchesSetup: Bool {
        status.pairing.sessionState == .ready
            && status.pairing.homeserver?.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
                == homeserver.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            && (matrixUserID.isEmpty || status.pairing.userID == matrixUserID)
    }

    var needsPairing: Bool { setupImported || !confirmationCommand.isEmpty }

    var connectionStatusLabel: String {
        if status.isBridgeOnline {
            return "Connected · \(desktopStatusLabel)"
        }
        if needsPairing && !status.canStopBridge {
            return confirmationCommand.isEmpty ? "Finish connecting" : "Confirm connection in chat"
        }
        return status.connectionTitle
    }

    func finishChatConfirmation(completion: @escaping () -> Void = {}) {
        guard let pendingSetup, pendingSetup.matches(status), !confirmationCommand.isEmpty else {
            errorMessage = DesktopSetupError.changed.localizedDescription
            recovery = nil
            return
        }
        perform(
            "finish_setup", parameters: [
                "expected_revision": pendingSetup.config.revision,
                "expected_session": pendingSetup.sessionParameters,
            ],
            then: { result in
                guard pendingSetup.matches(
                    try Self.responseStatus(result), revision: pendingSetup.config.revision + 1, enabled: true
                ) else { throw DesktopSetupError.changed }
                return result
            },
            completion: { [weak self] _ in
                self?.confirmationCommand = ""
                self?.pairingCode = ""
                self?.setupImported = false
                self?.pendingSetup = nil
                completion()
            }
        )
    }

    func cancelSetupImport() {
        setupImported = false
        pairingCode = ""
        confirmationCommand = ""
        verification = ""
        pendingSetup = nil
        homeserver = status.pairing.homeserver ?? "https://mindroom.chat"
        matrixUserID = status.pairing.userID ?? ""
        matrixPassword = ""
        controllerUserID = status.config.controllerUserID ?? ""
        controllerDeviceID = status.config.controllerDeviceID ?? ""
        controllerFingerprint = status.pairing.controllerFingerprint ?? ""
        requesterIDs = (status.config.allowedRequesterIDs ?? []).joined(separator: ", ")
        agentNames = (status.config.allowedAgentNames ?? []).joined(separator: ", ")
        accessGatewayRequired = false
        hasInitialSessionEdits = false
        identityConfirmed = false
    }

    func saveAndConnect() {
        guard identityConfirmed else {
            errorMessage = "Confirm the displayed controller, requester, and agent before saving."
            recovery = nil
            return
        }
        if !savedSessionMatchesSetup || pairingCode.isEmpty {
            errorMessage = "Import fresh setup data and sign in with the account shown before connecting."
            recovery = nil
            return
        }
        let config: [String: Any] = [
            "v": 1,
            "revision": status.config.revision,
            "enabled": false,
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
        let code = pairingCode
        let accessGateway = accessGatewayRequired
        let expected = DesktopSetupSnapshot(
            config: DesktopConfigStatus(
                state: "ready", revision: status.config.revision + 1, enabled: false,
                controllerUserID: controllerUserID, controllerDeviceID: controllerDeviceID,
                allowedRequesterIDs: split(requesterIDs), allowedAgentNames: split(agentNames),
                allowedAppIDs: selectedAppIDs.sorted()
            ),
            session: status.pairing, controllerFingerprint: controllerFingerprint
        )
        let pair: ([String: Any]) async throws -> [String: Any] = { [request] result in
            guard expected.matches(try Self.responseStatus(result)) else {
                throw DesktopSetupError.changed
            }
            let claimed = try await request(
                "pair",
                ["code": code, "expected_revision": expected.config.revision, "cloudflare_access": accessGateway],
                .seconds(180)
            )
            guard expected.matches(try Self.responseStatus(claimed)) else {
                throw DesktopSetupError.changed
            }
            guard let verification = claimed["verification"] as? String, !verification.isEmpty,
                  let command = claimed["confirmation_command"] as? String, !command.isEmpty else {
                throw DesktopBridgeProcessError.malformedResponse
            }
            return claimed
        }
        pendingSetup = nil
        perform(
            "configure", parameters: ["expected_revision": status.config.revision, "config": config],
            then: pair
        ) { [weak self] result in
            self?.pendingSetup = expected
            self?.verification = result["verification"] as? String ?? ""
            self?.confirmationCommand = result["confirmation_command"] as? String ?? ""
        }
    }

    func saveBrowserConfiguration() {
        guard status.hasSavedConnection, !needsPairing else {
            errorMessage = "Complete connection setup before saving browser settings."
            recovery = nil
            return
        }
        perform("set_browser_config", parameters: [
            "expected_revision": status.config.revision,
            "browser": [
                "enabled": browserEnabled,
                "executable_path": browserExecutable.nilIfBlank,
                "user_data_dir": browserProfile.nilIfBlank,
            ],
        ])
    }

    /// Compares one access draft with the current status, or with a status a save just returned.
    func hasChanges(for capability: DesktopAccessCapability, comparedTo saved: DesktopStatus? = nil) -> Bool {
        let config = (saved ?? status).config
        switch capability {
        case .applications: return selectedAppIDs != Set(config.allowedAppIDs ?? [])
        case .folders: return fileRoots != config.fileRoots
        case .shell: return shellEnabled != config.shellEnabled
        }
    }

    var hasAppSelectionChanges: Bool { hasChanges(for: .applications) }

    var hasLocalAccessChanges: Bool { hasChanges(for: .folders) || hasChanges(for: .shell) }

    var hasAccessChanges: Bool { hasAccessChanges(comparedTo: status) }

    func hasAccessChanges(comparedTo saved: DesktopStatus) -> Bool {
        DesktopAccessCapability.allCases.contains { hasChanges(for: $0, comparedTo: saved) }
    }

    func saveAllowedApplications(completion: @escaping (DesktopStatus) -> Void = { _ in }) {
        guard status.hasSavedConnection, !needsPairing else {
            errorMessage = "Complete connection setup before saving app access."
            recovery = nil
            return
        }
        perform(
            "set_allowed_apps",
            parameters: ["expected_revision": status.config.revision, "allowed_app_ids": selectedAppIDs.sorted()],
            stopFirst: status.canStopBridge,
            completion: { result in
                if let saved = try? Self.responseStatus(result) { completion(saved) }
            }
        )
    }

    /// Saves only folder and shell settings; approvals and auto-approval are never part of saved config.
    func saveLocalAccess(completion: @escaping (DesktopStatus) -> Void = { _ in }) {
        guard status.hasSavedConnection, !needsPairing else {
            errorMessage = "Complete connection setup before saving folder and shell access."
            recovery = nil
            return
        }
        perform(
            "set_local_access",
            parameters: [
                "expected_revision": status.config.revision,
                "files": ["roots": fileRoots],
                "shell": ["enabled": shellEnabled],
            ],
            stopFirst: status.canStopBridge,
            completion: { [weak self] result in
                guard let saved = try? Self.responseStatus(result) else { return }
                // The helper saves canonical folder paths; adopt them so the saved draft is clean.
                self?.fileRoots = saved.config.fileRoots
                self?.shellEnabled = saved.config.shellEnabled
                completion(saved)
            }
        )
    }

    func discardLocalAccessChanges() {
        fileRoots = status.config.fileRoots
        shellEnabled = status.config.shellEnabled
    }

    func addFileRoot(at url: URL) {
        guard let path = Self.canonicalDirectoryPath(url) else {
            errorMessage = "Choose a folder that exists on this Mac."
            recovery = nil
            return
        }
        guard !fileRoots.contains(path) else {
            errorMessage = "\(path) is already a read-only folder."
            recovery = nil
            return
        }
        fileRoots.append(path)
    }

    func removeFileRoot(_ path: String) {
        fileRoots.removeAll { $0 == path }
    }

    /// Answers only the exact request the person reviewed; a newer request needs its own review.
    func decideShell(_ request: DesktopShellRequest, _ decision: DesktopShellDecision) {
        guard status.shell.pending == request else {
            errorMessage = "That command is no longer waiting for approval. Nothing was approved."
            recovery = "Review the current request, if any, before answering it."
            return
        }
        perform("decide_shell", parameters: decision.parameters(commandID: request.requestID), urgent: true)
    }

    func grantShell(_ approval: DesktopShellAutoApproval) {
        perform("grant_shell", parameters: approval.grantParameters, urgent: true)
    }

    func revokeShell() { perform("revoke_shell", urgent: true) }

    func killShellHandle(_ handle: String) {
        perform("kill_shell_handle", parameters: ["handle": handle], urgent: true)
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
        perform("import_setup", parameters: ["descriptor": descriptor], completion: { [weak self] result in
            self?.homeserver = result["homeserver"] as? String ?? ""
            self?.matrixUserID = result["user_id"] as? String ?? ""
            self?.pairingCode = result["code"] as? String ?? ""
            self?.controllerUserID = result["controller_user_id"] as? String ?? ""
            self?.controllerDeviceID = result["controller_device_id"] as? String ?? ""
            self?.controllerFingerprint = result["controller_ed25519"] as? String ?? ""
            self?.requesterIDs = result["requester_id"] as? String ?? ""
            self?.agentNames = result["agent_name"] as? String ?? ""
            self?.accessGatewayRequired = result["cloudflare_access"] as? Bool ?? false
            self?.setupImported = true
            self?.verification = ""
            self?.confirmationCommand = ""
            self?.pendingSetup = nil
            self?.setupDescriptor = ""
            self?.confirmedIdentity = nil
        })
    }

    func login(replace: Bool = false, usePassword: Bool = false) {
        var parameters: [String: Any] = [
            "homeserver": homeserver,
            "method": "password",
            "user_id": matrixUserID,
            "password": matrixPassword,
            "replace": replace,
            "cloudflare_access": accessGatewayRequired,
        ]
        if !usePassword {
            parameters["method"] = "sso"
            parameters.removeValue(forKey: "password")
            if matrixUserID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                parameters.removeValue(forKey: "user_id")
            }
        }
        perform("login", parameters: parameters, timeout: .seconds(300), completion: { [weak self] _ in
            self?.matrixPassword = ""
        })
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
            "file_root_count": status.config.fileRoots.count,
            "shell_enabled": status.config.shellEnabled,
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
        then continuation: (([String: Any]) async throws -> [String: Any])? = nil,
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
                    _ = try await request("stop", [:], .seconds(120))
                }
                var result = try await request(action, parameters, timeout)
                if let continuation { result = try await continuation(result) }
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
        if !initialEdits.contains(\.fileRoots), fileRoots == previous.config.fileRoots {
            fileRoots = value.config.fileRoots
        }
        if !initialEdits.contains(\.shellEnabled), shellEnabled == previous.config.shellEnabled {
            shellEnabled = value.config.shellEnabled
        }
    }

    private var currentIdentity: String {
        [controllerUserID, controllerDeviceID, controllerFingerprint, requesterIDs, agentNames]
            .joined(separator: "\u{1F}")
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

    private static func canonicalDirectoryPath(_ url: URL) -> String? {
        // Match the helper's canonical form so a linked or repeated folder is caught before saving.
        guard let resolved = url.withUnsafeFileSystemRepresentation({ $0.flatMap { realpath($0, nil) } }) else {
            return nil
        }
        defer { free(resolved) }
        let path = String(cString: resolved)
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory), isDirectory.boolValue else {
            return nil
        }
        return path
    }

    private static func responseStatus(_ result: [String: Any]) throws -> DesktopStatus {
        guard let status = result["status"] as? [String: Any] else {
            throw DesktopBridgeProcessError.malformedResponse
        }
        return try JSONDecoder().decode(DesktopStatus.self, from: JSONSerialization.data(withJSONObject: status))
    }
}

private enum DesktopSetupError: LocalizedError {
    case changed

    var errorDescription: String? {
        "The saved setup or login changed. Import fresh setup data and connect again."
    }
}

private struct DesktopSetupSnapshot {
    let config: DesktopConfigStatus
    let session: DesktopPairingStatus
    let controllerFingerprint: String?

    var sessionParameters: [String: String] {
        ["homeserver": session.homeserver ?? "", "user_id": session.userID ?? "", "device_id": session.deviceID ?? ""]
    }

    func matches(_ status: DesktopStatus, revision: Int? = nil, enabled: Bool? = nil) -> Bool {
        status.config.state == "ready"
            && status.config.revision == (revision ?? config.revision)
            && status.config.enabled == (enabled ?? config.enabled)
            && status.config.controllerUserID == config.controllerUserID
            && status.config.controllerDeviceID == config.controllerDeviceID
            && status.config.allowedRequesterIDs == config.allowedRequesterIDs
            && status.config.allowedAgentNames == config.allowedAgentNames
            && status.config.allowedAppIDs == config.allowedAppIDs
            && status.pairing.controllerFingerprint == controllerFingerprint
            && status.pairing.sessionState == .ready
            && status.pairing.homeserver == session.homeserver
            && status.pairing.userID == session.userID
            && status.pairing.deviceID == session.deviceID
    }
}

private extension String {
    var nilIfBlank: Any {
        trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? NSNull() : self
    }
}
