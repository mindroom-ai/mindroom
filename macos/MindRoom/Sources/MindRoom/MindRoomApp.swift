import SwiftUI

@main
struct MindRoomApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        Settings {
            EmptyView()
        }
        .commands {
            CommandGroup(replacing: .appSettings) {
                Button("Settings…") { AppWindowController.shared.show(section: .settings) }
                    .keyboardShortcut(",", modifiers: .command)
            }
            CommandGroup(after: .newItem) {
                Button("Open MindRoom") { AppWindowController.shared.show() }
                    .keyboardShortcut("0", modifiers: .command)
            }
        }
    }
}
