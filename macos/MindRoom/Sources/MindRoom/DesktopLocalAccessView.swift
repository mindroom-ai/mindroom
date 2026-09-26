import AppKit
import SwiftUI

/// Edits the read-only folders and shell request setting, which are saved together.
struct DesktopLocalAccessView: View {
    @ObservedObject var store: DesktopControlStore
    var capability: DesktopAccessCapability
    var showSetup: () -> Void
    var onSaved: (DesktopStatus) -> Void = { _ in }
    @State private var confirmingStop = false
    private static let saveTitle = "Save Folder and Shell Access"

    var body: some View {
        AppSectionCard {
            VStack(alignment: .leading, spacing: 12) {
                if capability == .shell { shellSettings } else { folderSettings }
                Divider()
                DesktopAccessSaveBar(
                    store: store, saveTitle: Self.saveTitle, hasChanges: store.hasLocalAccessChanges,
                    idleNote: "Folders and shell settings save together.",
                    confirmingStop: $confirmingStop, showSetup: showSetup, save: save,
                    discard: store.discardLocalAccessChanges
                )
            }
            .disabled(store.isBusy)
        }
        .desktopAccessStopConfirmation(
            isPresented: $confirmingStop, title: "Stop computer access and save folder and shell access?",
            saveTitle: Self.saveTitle, onConfirm: save
        )
    }

    private func save() { store.saveLocalAccess(completion: onSaved) }

    private var folderSettings: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Read-only folders", systemImage: "folder").font(.headline)
                Spacer()
                Text("\(store.fileRoots.count) selected").foregroundStyle(.secondary)
            }
            Text("Agents can list these folders and read text files inside them. They cannot create, change, or delete files. macOS may still ask before MindRoom reads protected folders such as Desktop, Documents, or Downloads.")
                .font(.callout).foregroundStyle(.secondary)
            if store.fileRoots.isEmpty {
                Text("No folders selected.").foregroundStyle(.secondary)
            }
            ForEach(store.fileRoots, id: \.self) { path in
                HStack {
                    Image(systemName: "folder").foregroundStyle(.secondary).accessibilityHidden(true)
                    Text(path).lineLimit(1).truncationMode(.middle).textSelection(.enabled).help(path)
                    Spacer()
                    Text("Read only").font(.caption).foregroundStyle(.secondary)
                    Button("Remove") { store.removeFileRoot(path) }
                        .accessibilityLabel("Remove \(path)")
                }
            }
            Button("Add Folder…") { chooseFolders() }
        }
    }

    private var shellSettings: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Shell commands", systemImage: "terminal").font(.headline)
            Toggle("Allow shell command requests", isOn: $store.shellEnabled)
            Text("While access is on, allowed agents can ask to run commands on this Mac. Commands run with your macOS account's access, including files outside the read-only folders, the network, and anything your account can change. They are not confined to a folder.")
                .font(.callout).foregroundStyle(.secondary)
            Text("Each command waits for your approval in this window, unless you choose to approve commands for a while. Approvals are never saved, so restarting asks again.")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    private func chooseFolders() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Allow Read-Only Access"
        panel.message = "Choose folders your paired agents may read."
        guard panel.runModal() == .OK else { return }
        for url in panel.urls { store.addFileRoot(at: url) }
    }
}
