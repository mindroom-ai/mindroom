import AppKit
import SwiftUI

/// Edits the read-only folders and shell request setting, which are saved together.
struct DesktopLocalAccessView: View {
    @ObservedObject var store: DesktopControlStore
    var capability: DesktopAccessCapability
    var showSetup: () -> Void
    var onSaved: (DesktopStatus) -> Void = { _ in }
    @State private var confirmingStop = false

    var body: some View {
        AppSectionCard {
            VStack(alignment: .leading, spacing: 12) {
                if capability == .shell { shellSettings } else { folderSettings }
                Divider()
                saveControls
            }
            .disabled(store.isBusy)
        }
        .confirmationDialog("Stop computer access and save folder and shell access?", isPresented: $confirmingStop) {
            Button("Stop and Save Folder and Shell Access") { store.saveLocalAccess(completion: onSaved) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This stops computer access, including observation, control, and shell commands, before saving. It stays stopped until you start it again.")
        }
    }

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

    private var saveControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button(saveAction.title(saving: "Save Folder and Shell Access")) {
                    switch saveAction {
                    case .setup: showSetup()
                    case .stopAndSave: confirmingStop = true
                    case .save: store.saveLocalAccess(completion: onSaved)
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(saveAction != .setup && !store.hasLocalAccessChanges)
                Button("Discard Changes") { store.discardLocalAccessChanges() }
                    .disabled(!store.hasLocalAccessChanges)
                Spacer()
                if store.hasLocalAccessChanges {
                    Text(saveAction != .setup ? "Unsaved changes" : "Not saved yet").foregroundStyle(.orange)
                } else if store.status.config.state == "ready" {
                    Text("Saved").foregroundStyle(.secondary)
                }
            }
            Text(saveHint).font(.callout).foregroundStyle(.secondary)
        }
    }

    private var saveHint: String {
        if saveAction == .setup {
            return "Connect your agent first. These choices will be kept while you finish setup."
        }
        if store.status.canStopBridge {
            return "Folders and shell settings save together. Saving stops computer access; start it again when you are ready."
        }
        return "Folders and shell settings save together."
    }

    private var saveAction: DesktopAccessSaveAction {
        store.needsPairing ? .setup : store.status.accessSaveAction
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
