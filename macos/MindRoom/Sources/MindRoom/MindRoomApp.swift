import SwiftUI

@main
struct MindRoomApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @Environment(\.openSettings) private var openSettings

    var body: some Scene {
        // Capture the installed SwiftUI action before AppKit's menu can request settings.
        let settingsAction = openSettings
        StatusMenuController.shared.openSettingsAction = { settingsAction() }
        return Settings {
            DesktopControlView()
        }
    }
}
