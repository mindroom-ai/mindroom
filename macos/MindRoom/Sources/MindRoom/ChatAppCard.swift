import SwiftUI

/// Overview's offer of the chat app: one click installs it, and afterwards opens it.
struct ChatAppCard: View {
    @ObservedObject var chatApp = ChatAppStore.shared

    var body: some View {
        AppSectionCard {
            HStack(alignment: .firstTextBaseline) {
                Text("MindRoom Chat").font(.headline)
                Spacer()
                Text(chatApp.installedURL == nil ? "Not installed" : "Installed")
                    .font(.callout).foregroundStyle(.secondary)
            }
            Text("The chat app for this Mac, with voice messages and Quick Capture. It installs next to MindRoom and updates itself.")
                .foregroundStyle(.secondary)
            if let progress = chatApp.progress {
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text(progress)
                }
                .accessibilityElement(children: .combine)
            } else {
                Button(chatApp.installedURL == nil ? "Install MindRoom Chat" : "Open MindRoom Chat") {
                    chatApp.openOrInstall()
                }
            }
            if let error = chatApp.error {
                Text(error).foregroundStyle(.red).textSelection(.enabled)
            }
        }
        .onAppear { chatApp.refresh() }
    }
}
