import Foundation
import ServiceManagement

@MainActor
final class LoginItemController {
    static let shared = LoginItemController()

    private init() {}

    var canToggle: Bool {
        true
    }

    var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    var menuTitle: String {
        isEnabled ? "Start at Login: On" : "Start at Login: Off"
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
