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

    /// Registers or unregisters MindRoom as a login item; macOS may still need the user's approval afterwards.
    func setEnabled(_ enabled: Bool) throws {
        if enabled, !isEnabled { try SMAppService.mainApp.register() }
        if !enabled, SMAppService.mainApp.status != .notRegistered { try SMAppService.mainApp.unregister() }
    }

    func toggle() throws { try setEnabled(!isEnabled) }
}

/// Reads and sets "open at login" for full control; tests replace the system login item.
struct LoginItem {
    var isEnabled: @MainActor () -> Bool
    var setEnabled: @MainActor (Bool) throws -> Void

    @MainActor static let system = LoginItem(
        isEnabled: { LoginItemController.shared.isEnabled },
        setEnabled: { try LoginItemController.shared.setEnabled($0) }
    )
}
