import SwiftUI

struct DesktopControlView: View {
    @ObservedObject var store = DesktopControlStore.shared

    var body: some View {
        Form {
            statusHeader
            pairingSection
            applicationsSection
            permissionsSection
            browserSection
            bridgeSection
            diagnosticsSection
        }
        .formStyle(.grouped)
        .frame(minWidth: 680, minHeight: 720)
        .task { store.refresh() }
    }

    private var statusHeader: some View {
        Section {
            LabeledContent("Desktop bridge", value: store.desktopStatusLabel)
            if let message = store.errorMessage {
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                if let recovery = store.recovery {
                    Text(recovery).font(.callout)
                }
            }
        }
    }

    private var pairingSection: some View {
        Section("Pairing and local confirmation") {
            Text("Paste the setup data from your direct agent chat, then review the connection details below. The pairing code is kept only until the app closes.")
                .font(.callout)
                .foregroundStyle(.secondary)
            TextEditor(text: $store.setupDescriptor)
                .font(.system(.caption, design: .monospaced))
                .frame(height: 70)
            Button("Import Setup") { store.importSetupDescriptor() }
                .disabled(store.isBusy || store.setupDescriptor.isEmpty)
            TextField("Homeserver", text: $store.homeserver)
            TextField("Matrix user ID", text: $store.matrixUserID)
            SecureField("Password (leave blank for browser sign-in)", text: $store.matrixPassword)
            HStack {
                Button("Sign In") { store.login() }
                    .disabled(store.isBusy || store.accessGatewayRequired)
                Text(store.status.pairing.deviceID.map { "Device \($0)" } ?? "No local Matrix device")
                    .foregroundStyle(.secondary)
            }
            Divider()
            TextField("Controller user ID", text: $store.controllerUserID)
            TextField("Controller device ID", text: $store.controllerDeviceID)
            TextField("Controller fingerprint", text: $store.controllerFingerprint)
            TextField("Allowed requester IDs (comma separated)", text: $store.requesterIDs)
            TextField("Allowed agent names (comma separated)", text: $store.agentNames)
            if store.accessGatewayRequired {
                Text("This setup requires local access-gateway authentication. Use the terminal desktop setup command shown in chat, then return here to start the bridge.")
                    .font(.callout)
                    .foregroundStyle(.orange)
            }
            Toggle(
                "I confirm the controller fingerprint, requester, and agent match my MindRoom chat",
                isOn: Binding(
                    get: { store.identityConfirmed },
                    set: { store.identityConfirmed = $0 }
                )
            )
            TextField("Pairing code", text: $store.pairingCode)
            HStack {
                Button("Save Setup") { store.saveConfiguration() }
                    .disabled(store.isBusy)
                Button("Claim Pairing") { store.pair() }
                    .disabled(store.isBusy || !store.identityConfirmed || store.status.config.state != "ready")
            }
            if !store.verification.isEmpty {
                LabeledContent("Verification", value: store.verification)
                Text(store.confirmationCommand)
                    .font(.system(.body, design: .monospaced))
                    .textSelection(.enabled)
            }
        }
    }

    private var applicationsSection: some View {
        Section("Allowed applications") {
            Text("Only selected bundle identifiers are visible to Desktop tools.")
                .font(.callout)
                .foregroundStyle(.secondary)
            List(store.applications, selection: $store.selectedAppIDs) { application in
                HStack {
                    Text(application.name)
                    Spacer()
                    Text(application.id).font(.caption).foregroundStyle(.secondary)
                    if application.running { Image(systemName: "circle.fill").foregroundStyle(.green) }
                }
                .tag(application.id)
            }
            .frame(height: 140)
        }
    }

    private var permissionsSection: some View {
        Section("macOS permissions") {
            permissionRow(
                title: "Accessibility",
                key: "accessibility",
                status: store.status.permissions.accessibility
            )
            permissionRow(
                title: "Screen Recording",
                key: "screen_recording",
                status: store.status.permissions.screenRecording
            )
        }
    }

    private func permissionRow(
        title: String,
        key: String,
        status: DesktopPermissionStatus
    ) -> some View {
        HStack {
            LabeledContent(title, value: status.state.capitalized)
            if status.state != "granted" {
                Button("Request") { store.requestPermission(key) }
                Button("Open Settings") { store.openPermissionSettings(key) }
            }
        }
    }

    private var browserSection: some View {
        Section("Browser") {
            Toggle("Use browser extension", isOn: $store.browserEnabled)
            TextField("Browser executable path (optional)", text: $store.browserExecutable)
            TextField("Browser profile directory (optional)", text: $store.browserProfile)
            LabeledContent(
                "Readiness",
                value: "\(store.status.browser.runtime), \(store.status.browser.extensionState)"
            )
            Text("Browser control can start or stop its extension session and open a new tab. Existing tabs remain outside control.")
                .font(.callout)
                .foregroundStyle(.secondary)
            HStack {
                Button("Start Browser Session") { store.connectBrowser() }
                    .disabled(store.isBusy)
                Button("Disconnect") { store.disconnectBrowser() }
                    .disabled(store.isBusy)
            }
        }
    }

    private var bridgeSection: some View {
        Section("Observe and control") {
            LabeledContent("Mode", value: store.desktopStatusLabel)
            if let active = store.status.bridge.activeAction {
                LabeledContent("Active action", value: active)
            }
            HStack {
                Button("Start Observe Only") { store.start() }
                    .disabled(store.isBusy || store.status.bridge.state != "stopped")
                Button("Stop") { store.stop() }
                    .disabled(store.status.bridge.state == "stopped")
            }
            Stepper("Control duration: \(store.controlMinutes) minutes", value: $store.controlMinutes, in: 1 ... 60)
            HStack {
                Button("Grant Control Locally") { store.grantControl() }
                    .disabled(store.isBusy || store.status.bridge.state == "stopped")
                Button("Revoke Now") { store.revokeControl() }
                    .disabled(!store.status.authority.controlAvailable)
            }
            if store.status.authority.emergencyStopLatched {
                Button("Reset Emergency Stop") { store.resetEmergencyStop() }
                    .disabled(store.status.bridge.activeAction != nil)
            }
            Text("Move the pointer to the upper-left corner to stop input immediately.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    private var diagnosticsSection: some View {
        Section("Diagnostics") {
            LabeledContent("Helper version", value: store.status.helper.version)
            LabeledContent("Configuration revision", value: String(store.status.config.revision))
            Button("Copy Redacted Diagnostics") { store.copyDiagnostics() }
        }
    }
}
