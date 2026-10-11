import SwiftUI

/// Save, discard, and saved-state controls shared by the Access editors.
/// A running bridge asks through `desktopAccessStopConfirmation`, which each editor attaches outside its busy-disabled content.
struct DesktopAccessSaveBar: View {
    @ObservedObject var store: DesktopControlStore
    var saveTitle: String
    var hasChanges: Bool
    var idleNote: String?
    @Binding var confirmingStop: Bool
    var showSetup: () -> Void
    var save: () -> Void
    var discard: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button(action.title(saving: saveTitle)) {
                    switch action {
                    case .setup: showSetup()
                    case .stopAndSave: confirmingStop = true
                    case .save: save()
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(action != .setup && !hasChanges)
                Button("Discard Changes", action: discard).disabled(!hasChanges)
                Spacer()
                if hasChanges {
                    Text(action != .setup ? "Unsaved changes" : "Not saved yet").foregroundStyle(.orange)
                } else if store.status.config.state == "ready" {
                    Text("Saved").foregroundStyle(.secondary)
                }
            }
            if let hint {
                Text(hint).font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    private var action: DesktopAccessSaveAction {
        store.status.accessSaveAction(needsPairing: store.needsPairing)
    }

    private var hint: String? {
        switch action {
        case .setup: "Connect your agent first. These choices will be kept while you finish setup."
        case .stopAndSave: "Saving stops computer access. Start it again when you are ready."
        case .save: idleNote
        }
    }
}

extension View {
    /// Keep this outside `.disabled(store.isBusy)`: an urgent Stop or Revoke must not freeze an open confirmation.
    func desktopAccessStopConfirmation(
        isPresented: Binding<Bool>, title: String, saveTitle: String, onConfirm: @escaping () -> Void
    ) -> some View {
        confirmationDialog(title, isPresented: isPresented) {
            Button("Stop and \(saveTitle)", action: onConfirm)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This stops computer access, including observation, control, and shell commands, before saving. It stays stopped until you start it again.")
        }
    }
}
