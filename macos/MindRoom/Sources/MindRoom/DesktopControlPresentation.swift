import Foundation

enum DesktopControlSection: Int, CaseIterable, Hashable {
    case setup, applications, permissions, session

    var title: String {
        switch self {
        case .setup: "Connect"
        case .applications: "Apps"
        case .permissions: "Permissions"
        case .session: "Start"
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
    case starting, running, faulted, busy, setup, unsavedApps, noApps, permissions

    var message: String {
        switch self {
        case .starting: "Computer access is starting. You can select Stop to cancel."
        case .running: "Computer access is already running. Select Stop to end this session."
        case .faulted: "The previous session needs attention. Select Stop before starting again."
        case .busy: "Another action is in progress. Wait for it to finish before starting."
        case .setup: "Connect your agent and sign in before starting computer access."
        case .unsavedApps: "Save or discard your app selection changes before starting."
        case .noApps: "Choose at least one application and save app access before starting."
        case .permissions: "Allow Accessibility and Screen Recording for this copy of MindRoom before starting."
        }
    }

    var destination: DesktopControlSection? {
        switch self {
        case .setup: .setup
        case .unsavedApps, .noApps: .applications
        case .permissions: .permissions
        default: nil
        }
    }
}

enum DesktopAppSelectionAction {
    case setup, save, stopAndSave

    var title: String {
        switch self {
        case .setup: "Continue Setup"
        case .save: "Save App Access"
        case .stopAndSave: "Stop and Save…"
        }
    }
}

extension DesktopStatus {
    var hasSavedConnection: Bool {
        config.state == "ready" && config.enabled && pairing.sessionState == .ready
    }

    var hasRequiredPermissions: Bool {
        permissions.accessibility.state == "granted" && permissions.screenRecording.state == "granted"
    }

    var connectionTitle: String {
        if bridge.state == "observe_only" || bridge.state == "control" { return "Connected" }
        if helper.state == "starting" { return "Connecting…" }
        if bridge.state == "stopping" { return "Stopping…" }
        if bridge.state == "faulted" { return "Connection needs attention" }
        if helper.state == "stopped" { return "Checking connection…" }
        return hasSavedConnection ? "Connection saved · Access off" : "Not connected"
    }

    func nextSetupSection(hasAppSelectionChanges: Bool, needsPairing: Bool = false) -> DesktopControlSection {
        if canStopBridge { return .session }
        if !hasSavedConnection || needsPairing { return .setup }
        if hasAppSelectionChanges || config.allowedAppIDs?.isEmpty != false { return .applications }
        if !hasRequiredPermissions { return .permissions }
        return .session
    }

    func setupProgress(
        for section: DesktopControlSection, needsPairing: Bool, hasAppSelectionChanges: Bool
    ) -> DesktopSetupProgress {
        if helper.state == "stopped" { return .idle("Checking…") }
        switch section {
        case .setup:
            if needsPairing { return .needsAction("Finish setup") }
            return hasSavedConnection ? .complete("Saved") : .needsAction("Not connected")
        case .applications:
            if hasAppSelectionChanges { return .needsAction("Unsaved changes") }
            let apps = config.allowedAppIDs ?? []
            if apps.isEmpty { return .needsAction("No apps selected") }
            let count = apps.filter { $0 != "primary-screen" }.count
            let appLabel = count == 1 ? "1 app" : "\(count) apps"
            if apps.contains("primary-screen") {
                return .complete(count == 0 ? "Screen" : "\(appLabel) + screen")
            }
            return .complete(appLabel)
        case .permissions:
            if hasRequiredPermissions { return .complete("Allowed") }
            let allowed = [permissions.accessibility, permissions.screenRecording].filter { $0.state == "granted" }.count
            return .needsAction(allowed == 0 ? "Not allowed" : "1 of 2 allowed")
        case .session:
            if bridge.state == "observe_only" || bridge.state == "control" { return .complete("Running") }
            if helper.state == "starting" { return .idle("Starting…") }
            if bridge.state == "stopping" { return .idle("Stopping…") }
            if bridge.state == "faulted" { return .needsAction("Needs attention") }
            let ready = startBlocker(isBusy: false, hasAppSelectionChanges: hasAppSelectionChanges, needsPairing: needsPairing) == nil
            return .idle(ready ? "Ready" : "Off")
        }
    }

    var canStopBridge: Bool {
        bridge.state != "stopped" || helper.state == "starting"
    }

    func startBlocker(isBusy: Bool, hasAppSelectionChanges: Bool, needsPairing: Bool = false) -> DesktopStartBlocker? {
        if helper.state == "starting" { return .starting }
        if bridge.state == "faulted" { return .faulted }
        if bridge.state != "stopped" { return .running }
        if isBusy { return .busy }
        if !hasSavedConnection || needsPairing { return .setup }
        if hasAppSelectionChanges { return .unsavedApps }
        if config.allowedAppIDs?.isEmpty != false { return .noApps }
        if !hasRequiredPermissions { return .permissions }
        return nil
    }

    var appSelectionAction: DesktopAppSelectionAction {
        if !hasSavedConnection { return .setup }
        return canStopBridge ? .stopAndSave : .save
    }
}
