import Foundation
import ServiceManagement

@MainActor
final class LoginItemController {
    static let shared = LoginItemController()

    private init() {}

    var canToggle: Bool {
        switch SMAppService.mainApp.status {
        case .enabled, .notRegistered:
            return true
        case .requiresApproval, .notFound:
            return false
        @unknown default:
            return false
        }
    }

    var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    var menuTitle: String {
        switch SMAppService.mainApp.status {
        case .enabled:
            return "Start at Login: On"
        case .notRegistered:
            return "Start at Login: Off"
        case .requiresApproval:
            return "Start at Login: Needs Approval"
        case .notFound:
            return "Start at Login: Unavailable"
        @unknown default:
            return "Start at Login: Unknown"
        }
    }

    func toggle() {
        do {
            if isEnabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            MindRoomCommandRunner.shared.lastOutputForDisplay = error.localizedDescription
        }
    }
}
