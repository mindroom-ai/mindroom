import SwiftUI

struct DesktopControlView: View {
    @ObservedObject var store = DesktopControlStore.shared
    var scrollToTop: () -> Void
    @State private var section = DesktopControlSection.setup
    @State private var choseInitialSection = false
    @State private var reconnecting = false
    @State private var showDetails = false
    @State private var isGrantConfirmationPresented = false
    @State private var isReplaceSessionConfirmationPresented = false
    @State private var replaceUsingPassword = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 5) {
                Text("Computer access").font(.largeTitle.bold())
                Text("Connect an agent, choose its apps, then turn access on.")
                    .foregroundStyle(.secondary)
            }
            connectionSummary
            stepNavigation
            switch section {
            case .setup: connectionCard
            case .applications:
                DesktopApplicationsView(store: store, showSetup: { show(.setup) }, onSaved: {
                    if !store.selectedAppIDs.isEmpty {
                        show(store.status.hasRequiredPermissions ? .session : .permissions)
                    }
                })
                if store.status.hasSavedConnection, !store.hasAppSelectionChanges, store.status.config.allowedAppIDs?.isEmpty == false {
                    Button("Continue to Permissions") { show(.permissions) }.buttonStyle(.borderedProminent)
                }
            case .permissions:
                permissionsCard
                if store.status.hasRequiredPermissions {
                    Button("Continue to Start") { show(.session) }.buttonStyle(.borderedProminent)
                }
            case .session: sessionCard
            }
        }
        .task { store.refresh() }
        .onChange(of: store.status, initial: true) { _, status in
            guard !choseInitialSection, status.helper.state != "stopped" else { return }
            choseInitialSection = true
            section = nextSection
        }
        .confirmationDialog(
            "Grant control for \(store.controlMinutes) minutes?",
            isPresented: $isGrantConfirmationPresented, titleVisibility: .visible
        ) {
            Button("Grant Control for \(store.controlMinutes) Minutes") { store.grantControl() }
            Button("Cancel", role: .cancel) {}
        } message: { Text(savedControlSummary) }
        .confirmationDialog(
            "Replace the saved Matrix session?",
            isPresented: $isReplaceSessionConfirmationPresented, titleVisibility: .visible
        ) {
            Button("Sign In and Replace Session", role: .destructive) { store.login(replace: true, usePassword: replaceUsingPassword) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This replaces the saved login with a new device on \(store.homeserver). You will need to connect the new device again.")
        }
    }

    private var nextSection: DesktopControlSection {
        store.status.nextSetupSection(hasAppSelectionChanges: store.hasAppSelectionChanges, needsPairing: store.needsPairing)
    }

    private var startBlocker: DesktopStartBlocker? {
        store.status.startBlocker(isBusy: store.isBusy, hasAppSelectionChanges: store.hasAppSelectionChanges, needsPairing: store.needsPairing)
    }

    private func show(_ target: DesktopControlSection) {
        choseInitialSection = true
        section = target
        scrollToTop()
    }

    private var connectionSummary: some View {
        AppSectionCard {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Label(store.connectionStatusLabel, systemImage: store.status.hasSavedConnection && !store.needsPairing ? "checkmark.circle.fill" : "link")
                        .font(.headline)
                    if store.status.hasSavedConnection {
                        Text("Agent: \((store.status.config.allowedAgentNames ?? []).joined(separator: ", "))")
                            .foregroundStyle(.secondary)
                    }
                    Text(nextStepDescription).font(.callout)
                }
                Spacer()
                if store.status.canStopBridge {
                    Button("Stop Access") { store.stop() }
                } else {
                    Button(nextActionTitle) {
                        if nextSection == .session, startBlocker == nil { store.start() }
                        else { show(nextSection) }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(store.isBusy || store.status.helper.state == "stopped")
                }
            }
            if store.isBusy {
                HStack { ProgressView().controlSize(.small); Text("Working… Complete any browser sign-in that opens.").font(.callout) }
            }
            if let error = store.errorMessage ?? store.status.bridge.lastError?.message {
                Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red)
                if let recovery = store.recovery ?? store.status.bridge.lastError?.recovery {
                    Text(recovery).font(.callout).foregroundStyle(.secondary)
                }
            }
        }
    }

    private var nextActionTitle: String {
        switch nextSection {
        case .setup: store.confirmationCommand.isEmpty ? "Connect Agent" : "Confirm in Chat"
        case .applications: "Choose Apps"
        case .permissions: "Allow Permissions"
        case .session: "Start Observe Only"
        }
    }

    private var nextStepDescription: String {
        if store.status.canStopBridge {
            return "\(store.status.config.allowedAppIDs?.count ?? 0) saved apps. Stop access at any time."
        }
        switch nextSection {
        case .setup:
            return store.confirmationCommand.isEmpty
                ? "Next: connect this Mac to an agent from your MindRoom chat."
                : "Next: send the confirmation command in the same agent chat."
        case .applications: return "Next: choose which apps your agent may access and save your selection."
        case .permissions: return "Next: allow macOS permissions for this copy of MindRoom."
        case .session: return "Ready. Start observation when you want your agent to see your selected apps."
        }
    }

    private var stepNavigation: some View {
        HStack(spacing: 8) {
            ForEach(DesktopControlSection.allCases, id: \.self) { step in
                let progress = store.status.setupProgress(
                    for: step, needsPairing: store.needsPairing, hasAppSelectionChanges: store.hasAppSelectionChanges
                )
                Button { show(step) } label: {
                    VStack(spacing: 4) {
                        HStack(spacing: 5) {
                            Image(systemName: progress.symbol)
                                .foregroundStyle(progressColor(progress)).accessibilityHidden(true)
                            Text("\(step.rawValue + 1). \(step.title)")
                        }
                        Text(progress.detail).font(.caption).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity).padding(.vertical, 7)
                    .background(section == step ? Color.accentColor.opacity(0.18) : .clear)
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(step.rawValue + 1). \(step.title): \(progress.detail)")
                .accessibilityAddTraits(section == step ? .isSelected : [])
            }
        }
    }

    private func progressColor(_ progress: DesktopSetupProgress) -> Color {
        switch progress {
        case .complete: .green
        case .needsAction: .orange
        case .idle: .secondary
        }
    }

    private var connectionCard: some View {
        AppSectionCard {
            if !store.confirmationCommand.isEmpty {
                Label("Confirm in your agent chat", systemImage: "bubble.left.and.bubble.right").font(.headline)
                Text("Copy this command and send it in the same chat where you requested setup. Wait for the agent to confirm pairing.")
                Text(store.confirmationCommand).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                HStack {
                    Button("Copy Confirmation") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(store.confirmationCommand, forType: .string)
                    }
                    Button("I’ve Confirmed in Chat") {
                        store.finishChatConfirmation {
                            reconnecting = false
                            show(.applications)
                        }
                    }.buttonStyle(.borderedProminent)
                    Button("Use Fresh Setup Data") { reconnecting = true; store.cancelSetupImport() }
                }
            } else if store.setupImported {
                Label("Review your connection", systemImage: "person.crop.circle.badge.checkmark").font(.headline)
                LabeledContent("Agent", value: store.agentNames)
                LabeledContent("Requester", value: store.requesterIDs)
                LabeledContent("Controller", value: "\(store.controllerUserID) · \(store.controllerDeviceID)")
                Text("Fingerprint: \(store.controllerFingerprint)").font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                if store.savedSessionMatchesSetup {
                    Label("Signed in as \(store.status.pairing.userID ?? "")", systemImage: "checkmark.circle")
                        .foregroundStyle(.secondary)
                } else {
                    signInFields
                }
                Toggle("These identities and the fingerprint match my agent chat", isOn: Binding(
                    get: { store.identityConfirmed }, set: { store.identityConfirmed = $0 }
                ))
                HStack {
                    Button("Save and Connect") { store.saveAndConnect() }
                        .buttonStyle(.borderedProminent)
                        .disabled(!store.identityConfirmed || !store.savedSessionMatchesSetup || store.pairingCode.isEmpty)
                    Button("Use Different Setup Data") { reconnecting = true; store.cancelSetupImport() }
                }
                DisclosureGroup("Connection details", isExpanded: $showDetails) { connectionDetails.padding(.top, 8) }
            } else if store.status.hasSavedConnection && !reconnecting {
                Label("Your connection is saved", systemImage: "checkmark.circle.fill").font(.headline)
                Text("\((store.status.config.allowedAgentNames ?? []).joined(separator: ", ")) on \(store.status.pairing.homeserver ?? "")")
                Text("Signed in as \(store.status.pairing.userID ?? ""). No need to connect again.").foregroundStyle(.secondary)
                HStack {
                    Button("Choose Apps") { show(.applications) }.buttonStyle(.borderedProminent)
                    Button("Reconnect…") { reconnecting = true }
                }
                DisclosureGroup("Connection details", isExpanded: $showDetails) { connectionDetails.padding(.top, 8) }
            } else {
                Label("Connect your agent", systemImage: "link").font(.headline)
                Text("In a direct chat with your agent, send !desktop setup. Copy the setup data from its reply and paste it below.")
                ZStack(alignment: .topLeading) {
                    if store.setupDescriptor.isEmpty {
                        Text("Paste setup data here").foregroundStyle(.secondary).padding(12)
                            .allowsHitTesting(false).accessibilityHidden(true)
                    }
                    TextEditor(text: $store.setupDescriptor)
                        .font(.system(.body, design: .monospaced)).scrollContentBackground(.hidden).padding(6)
                        .accessibilityLabel("Setup data")
                }
                .frame(height: 100)
                .background(Color(nsColor: .textBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.secondary.opacity(0.5)))
                HStack {
                    Button("Import Setup") { store.importSetupDescriptor() }
                        .buttonStyle(.borderedProminent).disabled(store.setupDescriptor.isEmpty)
                    if store.status.hasSavedConnection {
                        Button("Cancel") { reconnecting = false; store.cancelSetupImport(); store.setupDescriptor = "" }
                    }
                }
            }
        }.disabled(store.isBusy || store.status.canStopBridge)
    }

    private var signInFields: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Sign in to \(store.homeserver) as \(store.matrixUserID). Browser sign-in opens your organization’s login page.")
                .font(.callout)
            if store.status.pairing.sessionState == .missing {
                Button("Sign In with Browser") { store.login() }
            } else {
                Text("The saved login cannot be used for this setup. Sign in with the account shown above.").foregroundStyle(.orange)
                Button("Replace Saved Login…") { replaceUsingPassword = false; isReplaceSessionConfirmationPresented = true }
            }
            DisclosureGroup("Use a password instead") {
                SecureField("Password", text: $store.matrixPassword)
                Button("Sign In with Password") {
                    if store.status.pairing.sessionState == .missing { store.login(usePassword: true) }
                    else { replaceUsingPassword = true; isReplaceSessionConfirmationPresented = true }
                }.disabled(store.matrixPassword.isEmpty)
            }
        }
    }

    private var connectionDetails: some View {
        VStack(alignment: .leading, spacing: 10) {
            if store.setupImported {
                labeledTextField("Homeserver", text: $store.homeserver)
                labeledTextField("Matrix user ID", text: $store.matrixUserID)
                labeledTextField("Controller user ID", text: $store.controllerUserID)
                labeledTextField("Controller device ID", text: $store.controllerDeviceID)
                labeledTextField("Controller fingerprint", text: $store.controllerFingerprint)
                labeledTextField("Allowed requester IDs", text: $store.requesterIDs)
                labeledTextField("Allowed agent names", text: $store.agentNames)
            } else {
                LabeledContent("Homeserver", value: store.status.pairing.homeserver ?? "")
                LabeledContent("Matrix account", value: store.status.pairing.userID ?? "")
                LabeledContent("Controller", value: store.status.config.controllerUserID ?? "")
                LabeledContent("Controller device", value: store.status.config.controllerDeviceID ?? "")
                LabeledContent("Fingerprint", value: store.status.pairing.controllerFingerprint ?? "")
                LabeledContent("Requester", value: (store.status.config.allowedRequesterIDs ?? []).joined(separator: ", "))
                LabeledContent("Agent", value: (store.status.config.allowedAgentNames ?? []).joined(separator: ", "))
            }
        }
    }

    private var permissionsCard: some View {
        AppSectionCard {
            HStack {
                Label("Allow macOS permissions", systemImage: "lock.shield").font(.headline)
                Spacer()
                Button("Check Again") { store.refresh() }.disabled(store.isBusy)
            }
            Text("Accessibility lets your agent read app controls. Screen Recording lets it see selected app windows.")
                .font(.callout).foregroundStyle(.secondary)
            permissionRow(title: "Accessibility", key: "accessibility", status: store.status.permissions.accessibility)
            Divider()
            permissionRow(title: "Screen Recording", key: "screen_recording", status: store.status.permissions.screenRecording)
            if !store.status.hasRequiredPermissions {
                Text("After allowing access, quit and reopen this copy of MindRoom, then select Check Again. An enabled entry for an older copy may not apply to this one.")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    private func permissionRow(title: String, key: String, status: DesktopPermissionStatus) -> some View {
        HStack {
            Label(title, systemImage: status.state == "granted" ? "checkmark.circle.fill" : "circle")
            Spacer()
            Text(status.state == "granted" ? "Allowed" : "Not allowed yet").foregroundStyle(.secondary)
            if status.state != "granted" {
                Button("Request") { store.requestPermission(key) }.disabled(store.isBusy || !status.canRequest)
                Button("Open Settings") { store.openPermissionSettings(key) }
            }
        }
    }

    private var sessionCard: some View {
        AppSectionCard {
            Label(store.status.connectionTitle, systemImage: "display").font(.headline)
            if let blocker = startBlocker, !store.status.canStopBridge {
                Text(blocker.message)
                if let destination = blocker.destination { Button("Review \(destination.title)") { show(destination) } }
            } else if !store.status.canStopBridge {
                Text("Start Observe Only above to let your agent read the apps you selected. Control stays off until you grant it below.")
            }
            if let action = store.status.bridge.activeAction { LabeledContent("Active action", value: action) }
            DisclosureGroup("Optional control") {
                VStack(alignment: .leading, spacing: 10) {
                    Stepper("Control duration: \(store.controlMinutes) minutes", value: $store.controlMinutes, in: 1...60)
                    HStack {
                        Button("Grant Control…") { isGrantConfirmationPresented = true }
                            .disabled(store.isBusy || store.status.bridge.state != "observe_only")
                        Button("Revoke Now", role: .destructive) { store.revokeControl() }
                            .disabled(!store.status.authority.controlAvailable)
                    }
                    Text("Control expires automatically. Revoke it at any time, or move the pointer to the upper-left corner to stop input.")
                        .font(.callout).foregroundStyle(.secondary)
                }.padding(.top, 8)
            }
            if store.status.authority.emergencyStopLatched {
                Button("Reset Emergency Stop") { store.resetEmergencyStop() }.disabled(store.status.bridge.activeAction != nil)
            }
            browserOptions
            DisclosureGroup("Diagnostics") {
                LabeledContent("Helper version", value: store.status.helper.version)
                Button("Copy Redacted Diagnostics") { store.copyDiagnostics() }
            }
        }
    }

    private var browserOptions: some View {
        DisclosureGroup("Optional browser extension") {
            VStack(alignment: .leading, spacing: 8) {
                Toggle("Use browser extension", isOn: $store.browserEnabled)
                labeledTextField("Browser executable path (optional)", text: $store.browserExecutable)
                labeledTextField("Browser profile directory (optional)", text: $store.browserProfile)
                Button("Save Browser Settings") { store.saveBrowserConfiguration() }
                    .disabled(store.isBusy || store.status.canStopBridge || !store.status.hasSavedConnection || store.needsPairing)
                LabeledContent("Readiness", value: "\(store.status.browser.runtime), \(store.status.browser.extensionState)")
                HStack {
                    Button("Start Browser Session") { store.connectBrowser() }.disabled(store.isBusy || !store.status.canStopBridge)
                    Button("Disconnect") { store.disconnectBrowser() }.disabled(store.isBusy)
                }
            }.padding(.top, 8).disabled(!store.canEditBrowserConfiguration)
        }
    }

    private func labeledTextField(_ title: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            TextField(title, text: text).textFieldStyle(.roundedBorder)
        }
    }

    private var savedControlSummary: String {
        """
        Controller: \(store.status.config.controllerUserID ?? "") · \(store.status.config.controllerDeviceID ?? "")
        Fingerprint: \(store.status.pairing.controllerFingerprint ?? "")
        Requester: \((store.status.config.allowedRequesterIDs ?? []).joined(separator: ", "))
        Agent: \((store.status.config.allowedAgentNames ?? []).joined(separator: ", "))
        Applications: \((store.status.config.allowedAppIDs ?? []).joined(separator: ", "))
        Duration: \(store.controlMinutes) minutes
        """
    }
}
