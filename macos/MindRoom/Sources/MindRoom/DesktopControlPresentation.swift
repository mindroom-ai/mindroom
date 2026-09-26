import Foundation

enum DesktopControlSection: Int, CaseIterable, Hashable {
    case setup, access, permissions, session

    var title: String {
        switch self {
        case .setup: "Connect"
        case .access: "Access"
        case .permissions: "Permissions"
        case .session: "Start"
        }
    }
}

enum DesktopAccessCapability: CaseIterable, Hashable {
    case applications, folders, shell

    var title: String {
        switch self {
        case .applications: "Applications"
        case .folders: "Read-only folders"
        case .shell: "Shell commands"
        }
    }

    var symbol: String {
        switch self {
        case .applications: "app.badge.checkmark"
        case .folders: "folder"
        case .shell: "terminal"
        }
    }
}

enum DesktopSetupProgress: Equatable {
    case complete(String), needsAction(String), idle(String)

    var detail: String {
        switch self {
        case let .complete(detail), let .needsAction(detail), let .idle(detail): detail
        }
    }

    var symbol: String {
        switch self {
        case .complete: "checkmark.circle.fill"
        case .needsAction: "exclamationmark.circle"
        case .idle: "circle"
        }
    }
}

enum DesktopStartBlocker {
    case starting, running, faulted, busy, setup, unsavedAccess, noAccess, permissions

    var message: String {
        switch self {
        case .starting: "Computer access is starting. You can select Stop to cancel."
        case .running: "Computer access is already running. Select Stop to end this session."
        case .faulted: "The previous session needs attention. Select Stop before starting again."
        case .busy: "Another action is in progress. Wait for it to finish before starting."
        case .setup: "Connect your agent and sign in before starting computer access."
        case .unsavedAccess: "Save or discard your access changes before starting."
        case .noAccess: "Choose at least one app, read-only folder, or shell access and save it before starting."
        case .permissions: "Selected apps need Accessibility and Screen Recording for this copy of MindRoom before starting."
        }
    }

    var destination: DesktopControlSection? {
        switch self {
        case .setup: .setup
        case .unsavedAccess, .noAccess: .access
        case .permissions: .permissions
        default: nil
        }
    }
}

enum DesktopAccessSaveAction {
    case setup, save, stopAndSave

    func title(saving: String) -> String {
        switch self {
        case .setup: "Continue Setup"
        case .save: saving
        case .stopAndSave: "Stop and Save…"
        }
    }
}

enum DesktopShellApprovalState: Equatable {
    case off, askEachTime, pending(DesktopShellRequest), autoApprove(seconds: Int), untilRevoked

    var isPending: Bool {
        if case .pending = self { true } else { false }
    }

    var label: String? {
        switch self {
        case .off: nil
        case .askEachTime: "Shell asks for each command"
        case .pending: "Shell command waiting for approval"
        case let .autoApprove(seconds): "Shell auto-approval · \(desktopDurationLabel(seconds)) left"
        case .untilRevoked: "Shell auto-approval until you stop it"
        }
    }
}

extension DesktopShellAutoApproval {
    var title: String {
        switch self {
        case let .minutes(minutes): "\(minutes) Minutes"
        case .untilStopped: "Until I Stop"
        }
    }
}

extension DesktopShellStatus {
    /// Revoking would end auto-approval or stop a command that was already approved.
    var hasRevocableWork: Bool {
        autoApproveUntilRevoked || autoApproveRemainingSeconds > 0 || activeRequestID != nil
            || handles.contains { $0.state == "running" }
    }
}

extension DesktopShellRequest {
    var displayCommand: String { desktopSafePreview(command) }
    var displayCwd: String { desktopSafePreview(cwd) }
    var displayRequesterID: String { desktopSafePreview(requesterID) }
    var displayAgentName: String { desktopSafePreview(agentName) }
    var hasEscapedCharacters: Bool {
        [command, cwd, requesterID, agentName].contains(where: desktopPreviewEscapes)
    }
}

/// Escapes control, invisible-format, and text-direction characters so remote text cannot hide what runs.
/// Ordinary newlines stay readable; everything escaped appears as `\u{…}`.
func desktopSafePreview(_ text: String) -> String {
    var preview = ""
    for scalar in text.unicodeScalars {
        if isHiddenPreviewScalar(scalar) {
            preview += "\\u{\(String(scalar.value, radix: 16, uppercase: true))}"
        } else {
            preview.unicodeScalars.append(scalar)
        }
    }
    return preview
}

func desktopPreviewEscapes(_ text: String) -> Bool {
    text.unicodeScalars.contains(where: isHiddenPreviewScalar)
}

private func isHiddenPreviewScalar(_ scalar: Unicode.Scalar) -> Bool {
    switch scalar.properties.generalCategory {
    case .control: scalar != "\n"
    case .format, .lineSeparator, .paragraphSeparator, .privateUse, .surrogate, .unassigned: true
    default: false
    }
}

func desktopDurationLabel(_ seconds: Int) -> String {
    "\(seconds / 60)m \(seconds % 60)s"
}

extension DesktopStatus {
    var hasSavedConnection: Bool {
        config.state == "ready" && config.enabled && pairing.sessionState == .ready
    }

    var hasRequiredPermissions: Bool {
        permissions.accessibility.state == "granted" && permissions.screenRecording.state == "granted"
    }

    var isBridgeOnline: Bool {
        bridge.state == "observe_only" || bridge.state == "control"
    }

    var savedAppIDs: [String] { config.allowedAppIDs ?? [] }

    /// Screen and input permissions matter only for saved apps; folders and shell never need them.
    var needsGUIPermissions: Bool { !savedAppIDs.isEmpty }

    var hasSavedLocalAccess: Bool { !config.fileRoots.isEmpty || config.shellEnabled }

    /// A browser alone is not enough: browser control needs the GUI provider that saved apps start.
    var hasSavedAccess: Bool { needsGUIPermissions || hasSavedLocalAccess }

    var startActionTitle: String { hasSavedLocalAccess ? "Start Access" : "Start Observe Only" }

    var savedAccessSummary: String? {
        var parts: [String] = []
        if let apps = savedAppsLabel { parts.append(apps) }
        if let folders = savedFoldersLabel { parts.append(folders) }
        if config.shellEnabled { parts.append("Shell") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private var savedAppsLabel: String? {
        guard !savedAppIDs.isEmpty else { return nil }
        let count = savedAppIDs.filter { $0 != "primary-screen" }.count
        let appLabel = count == 1 ? "1 app" : "\(count) apps"
        guard savedAppIDs.contains("primary-screen") else { return appLabel }
        return count == 0 ? "Screen" : "\(appLabel) + screen"
    }

    private var savedFoldersLabel: String? {
        switch config.fileRoots.count {
        case 0: nil
        case 1: "1 folder"
        case let count: "\(count) folders"
        }
    }

    /// Anything the bridge still has to finish; the helper refuses an emergency-stop reset meanwhile.
    var hasBridgeWorkInFlight: Bool {
        bridge.activeAction != nil || shell.pending != nil || shell.activeRequestID != nil
    }

    var shellApprovalState: DesktopShellApprovalState {
        guard isBridgeOnline, shell.enabled else { return .off }
        if let pending = shell.pending { return .pending(pending) }
        if shell.autoApproveUntilRevoked { return .untilRevoked }
        let remaining = Int(shell.autoApproveRemainingSeconds.rounded(.up))
        return remaining > 0 ? .autoApprove(seconds: remaining) : .askEachTime
    }

    /// Shell commands can change anything the account can, so GUI modes are named only for apps.
    func accessModeLabel(controlRemainingSeconds: Int) -> String {
        switch bridge.state {
        case "observe_only", "control":
            let appMode = bridge.state == "control"
                ? "Control · \(desktopDurationLabel(controlRemainingSeconds))" : "Observe only"
            guard hasSavedLocalAccess else { return appMode }
            return needsGUIPermissions ? "Apps \(appMode.lowercased())" : "Access on"
        case "faulted":
            return "Attention required"
        default:
            return "Stopped"
        }
    }

    func shellAutoApprovalScope(_ approval: DesktopShellAutoApproval) -> String {
        let duration = switch approval {
        case let .minutes(minutes): "for \(minutes) minutes"
        case .untilStopped: "until you revoke shell access or stop computer access"
        }
        return """
        Every shell command from all locally allowed agents and requesters runs without asking \(duration).
        Agents: \((config.allowedAgentNames ?? []).joined(separator: ", "))
        Requesters: \((config.allowedRequesterIDs ?? []).joined(separator: ", "))
        Commands run with your macOS account's access, including files outside the read-only folders and the network. \
        This is never saved; restarting MindRoom asks again.
        """
    }

    var connectionTitle: String {
        if isBridgeOnline { return "Connected" }
        if helper.state == "starting" { return "Connecting…" }
        if bridge.state == "stopping" { return "Stopping…" }
        if bridge.state == "faulted" { return "Connection needs attention" }
        if helper.state == "stopped" { return "Checking connection…" }
        return hasSavedConnection ? "Connection saved · Access off" : "Not connected"
    }

    func nextSetupSection(hasAccessChanges: Bool, needsPairing: Bool = false) -> DesktopControlSection {
        if canStopBridge { return .session }
        if !hasSavedConnection || needsPairing { return .setup }
        if hasAccessChanges || !hasSavedAccess { return .access }
        if needsGUIPermissions && !hasRequiredPermissions { return .permissions }
        return .session
    }

    func accessProgress(for capability: DesktopAccessCapability, hasChanges: Bool) -> DesktopSetupProgress {
        if helper.state == "stopped" { return .idle("Checking…") }
        if hasChanges { return .needsAction("Unsaved changes") }
        switch capability {
        case .applications: return savedAppsLabel.map { .complete($0) } ?? .idle("None")
        case .folders: return savedFoldersLabel.map { .complete($0) } ?? .idle("None")
        case .shell: return config.shellEnabled ? .complete("Allowed") : .idle("Off")
        }
    }

    func setupProgress(
        for section: DesktopControlSection, needsPairing: Bool, hasAccessChanges: Bool
    ) -> DesktopSetupProgress {
        if helper.state == "stopped" { return .idle("Checking…") }
        switch section {
        case .setup:
            if needsPairing { return .needsAction("Finish setup") }
            return hasSavedConnection ? .complete("Saved") : .needsAction("Not connected")
        case .access:
            if hasAccessChanges { return .needsAction("Unsaved changes") }
            return savedAccessSummary.map { .complete($0) } ?? .needsAction("Nothing selected")
        case .permissions:
            if !needsGUIPermissions { return .idle("Not needed") }
            if hasRequiredPermissions { return .complete("Allowed") }
            let allowed = [permissions.accessibility, permissions.screenRecording].filter { $0.state == "granted" }.count
            return .needsAction(allowed == 0 ? "Not allowed" : "1 of 2 allowed")
        case .session:
            if isBridgeOnline { return .complete("Running") }
            if helper.state == "starting" { return .idle("Starting…") }
            if bridge.state == "stopping" { return .idle("Stopping…") }
            if bridge.state == "faulted" { return .needsAction("Needs attention") }
            let ready = startBlocker(isBusy: false, hasAccessChanges: hasAccessChanges, needsPairing: needsPairing) == nil
            return .idle(ready ? "Ready" : "Off")
        }
    }

    var canStopBridge: Bool {
        bridge.state != "stopped" || helper.state == "starting"
    }

    func startBlocker(isBusy: Bool, hasAccessChanges: Bool, needsPairing: Bool = false) -> DesktopStartBlocker? {
        if helper.state == "starting" { return .starting }
        if bridge.state == "faulted" { return .faulted }
        if bridge.state != "stopped" { return .running }
        if isBusy { return .busy }
        if !hasSavedConnection || needsPairing { return .setup }
        if hasAccessChanges { return .unsavedAccess }
        if !hasSavedAccess { return .noAccess }
        if needsGUIPermissions && !hasRequiredPermissions { return .permissions }
        return nil
    }

    var accessSaveAction: DesktopAccessSaveAction {
        if !hasSavedConnection { return .setup }
        return canStopBridge ? .stopAndSave : .save
    }
}
