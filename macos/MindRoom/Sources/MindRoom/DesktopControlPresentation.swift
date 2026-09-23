import Foundation

enum DesktopControlSection: Hashable {
    case applications, setup
}

enum DesktopStartBlocker {
    case starting, running, faulted, busy, setup, unsavedApps, noApps

    var message: String {
        switch self {
        case .starting: "Computer access is starting. You can select Stop to cancel."
        case .running: "Computer access is already running. Select Stop to end this session."
        case .faulted: "The previous session needs attention. Select Stop before starting again."
        case .busy: "Another action is in progress. Wait for it to finish before starting."
        case .setup: "Start is unavailable until you complete and save connection setup."
        case .unsavedApps: "Save or discard your app selection changes before starting."
        case .noApps: "Choose at least one application and save app access before starting."
        }
    }

    var destination: DesktopControlSection? {
        switch self {
        case .setup: .setup
        case .unsavedApps, .noApps: .applications
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
    var canStopBridge: Bool {
        bridge.state != "stopped" || helper.state == "starting"
    }

    func startBlocker(isBusy: Bool, hasAppSelectionChanges: Bool) -> DesktopStartBlocker? {
        if helper.state == "starting" { return .starting }
        if bridge.state == "faulted" { return .faulted }
        if bridge.state != "stopped" { return .running }
        if isBusy { return .busy }
        if config.state != "ready" { return .setup }
        if hasAppSelectionChanges { return .unsavedApps }
        if config.allowedAppIDs?.isEmpty != false { return .noApps }
        return nil
    }

    var appSelectionAction: DesktopAppSelectionAction {
        if config.state != "ready" { return .setup }
        return canStopBridge ? .stopAndSave : .save
    }
}
