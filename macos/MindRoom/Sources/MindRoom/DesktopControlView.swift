import SwiftUI

struct DesktopControlView: View {
  @ObservedObject var store = DesktopControlStore.shared
  var scrollToSection: (DesktopControlSection) -> Void
  @State private var isSetupExpanded = false
  @State private var isBrowserExpanded = false
  @State private var isDiagnosticsExpanded = false
  @State private var isGrantConfirmationPresented = false
  @State private var isReplaceSessionConfirmationPresented = false
  @State private var didChooseInitialSetupState = false

  var body: some View {
    VStack(alignment: .leading, spacing: 20) {
      header
      sessionCard
      DesktopApplicationsView(store: store) { showSection(.setup) }
        .id(DesktopControlSection.applications)
      setupCard.id(DesktopControlSection.setup)
      permissionsCard
      browserCard
      diagnosticsCard
    }
    .task { store.refresh() }
    .onChange(of: store.status, initial: true) { _, status in
      guard !didChooseInitialSetupState, status.helper.state != "stopped" else { return }
      didChooseInitialSetupState = true
      isSetupExpanded = status.config.state == "missing"
    }
    .confirmationDialog(
      "Grant control for \(store.controlMinutes) minutes?",
      isPresented: $isGrantConfirmationPresented,
      titleVisibility: .visible
    ) {
      Button("Grant Control for \(store.controlMinutes) Minutes") {
        store.grantControl()
      }
      Button("Cancel", role: .cancel) {}
    } message: {
      Text(savedControlSummary)
    }
    .confirmationDialog(
      "Replace the saved Matrix session?",
      isPresented: $isReplaceSessionConfirmationPresented,
      titleVisibility: .visible
    ) {
      Button("Sign In and Replace Session", role: .destructive) { store.login(replace: true) }
      Button("Cancel", role: .cancel) {}
    } message: {
      Text(
        "Sign in to \(store.homeserver)\(store.matrixUserID.isEmpty ? "" : " as \(store.matrixUserID)"). This creates a new Matrix device and replaces the saved login on this Mac. You will need to pair the new device again. Cancel to keep using the saved session."
      )
    }
  }

  private func showSection(_ section: DesktopControlSection) {
    if section == .setup { isSetupExpanded = true }
    scrollToSection(section)
  }

  private var startBlocker: DesktopStartBlocker? {
    store.status.startBlocker(isBusy: store.isBusy, hasAppSelectionChanges: store.hasAppSelectionChanges)
  }

  private var header: some View {
    VStack(alignment: .leading, spacing: 6) {
      Text("Computer access")
        .font(.largeTitle.bold())
      Text(
        "Let agents you pair elsewhere observe or control selected apps on this Mac. This session runs independently from Local agents."
      )
      .foregroundStyle(.secondary)
    }
  }

  private var sessionCard: some View {
    AppSectionCard {
      VStack(alignment: .leading, spacing: 14) {
        HStack(alignment: .firstTextBaseline) {
          Label("Session", systemImage: "display")
            .font(.headline)
          Spacer()
          Text(store.desktopStatusLabel)
            .font(.headline)
        }

        if let activeAction = store.status.bridge.activeAction {
          LabeledContent("Active action", value: activeAction)
        }

        if let message = store.errorMessage {
          Label(message, systemImage: "exclamationmark.triangle.fill")
            .foregroundStyle(.red)
          if let recovery = store.recovery {
            Text(recovery)
              .font(.callout)
              .foregroundStyle(.secondary)
          }
        }

        HStack {
          Button("Start Observe Only") { store.start() }
            .disabled(startBlocker != nil)
            .help(startBlocker?.message ?? "Start observation of your saved allowed apps.")
          Button("Stop") { store.stop() }
            .disabled(!store.status.canStopBridge)
            .help(store.status.canStopBridge ? "Stop observation and control." : "Computer access is already stopped.")
        }

        if let blocker = startBlocker {
          Text(blocker.message)
            .font(.callout)
          if let destination = blocker.destination {
            Button(destination == .setup ? "Complete Setup" : "Review App Selection") {
              showSection(destination)
            }
          }
        }
        if !store.status.canStopBridge {
          Text("Stop is unavailable because computer access is already stopped.")
            .font(.callout).foregroundStyle(.secondary)
        }

        Divider()

        Stepper(
          "Control duration: \(store.controlMinutes) minutes",
          value: $store.controlMinutes,
          in: 1...60
        )
        HStack {
          Button("Grant Control…") {
            isGrantConfirmationPresented = true
          }
          .disabled(store.isBusy || store.status.bridge.state == "stopped")
          Button("Revoke Now", role: .destructive) { store.revokeControl() }
            .disabled(!store.status.authority.controlAvailable)
        }
        Text(
          "Control expires automatically. Revoke immediately at any time, or move the pointer to the upper-left corner to stop input."
        )
        .font(.callout)
        .foregroundStyle(.secondary)

        if store.status.authority.emergencyStopLatched {
          Button("Reset Emergency Stop") { store.resetEmergencyStop() }
            .disabled(store.status.bridge.activeAction != nil)
        }
      }
    }
  }

  private var setupCard: some View {
    AppSectionCard {
      DisclosureGroup(isExpanded: $isSetupExpanded) {
        VStack(alignment: .leading, spacing: 12) {
          Text(
            "Paste the setup data from your direct agent chat, then review the controller, requester, and agent before saving. The pairing code stays only until the app closes."
          )
          .font(.callout)
          .foregroundStyle(.secondary)

          Text("Setup data")
            .font(.subheadline.weight(.medium))
          ZStack(alignment: .topLeading) {
            if store.setupDescriptor.isEmpty {
              Text("Paste setup data here")
                .foregroundStyle(.secondary)
                .padding(.horizontal, 9).padding(.vertical, 12)
                .allowsHitTesting(false)
                .accessibilityHidden(true)
            }
            TextEditor(text: $store.setupDescriptor)
              .font(.system(.body, design: .monospaced))
              .scrollContentBackground(.hidden)
              .padding(6)
              .accessibilityLabel("Setup data")
              .accessibilityHint("Paste the setup data from your direct agent chat, then select Import Setup.")
          }
          .frame(height: 110)
          .background(Color(nsColor: .textBackgroundColor))
          .clipShape(RoundedRectangle(cornerRadius: 6))
          .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.secondary.opacity(0.5)))
          Button("Import Setup") { store.importSetupDescriptor() }
            .disabled(store.isBusy || store.setupDescriptor.isEmpty)

          Divider()

          labeledTextField("Homeserver", text: $store.homeserver)
          labeledTextField("Matrix user ID", text: $store.matrixUserID)
          labeledSecureField(
            "Password",
            prompt: "Leave blank for browser sign-in",
            text: $store.matrixPassword
          )
          HStack {
            if store.status.pairing.sessionState == .missing {
              Button("Sign In") { store.login() }
            } else {
              Button("Replace Session…") { isReplaceSessionConfirmationPresented = true }
            }
          }
          .disabled(store.isBusy || store.accessGatewayRequired || store.status.canStopBridge)
          if store.status.pairing.sessionState == .ready {
            Text("Saved device \(store.status.pairing.deviceID ?? "")")
            Text("\(store.status.pairing.userID ?? "") · \(store.status.pairing.homeserver ?? "")")
              .font(.callout).foregroundStyle(.secondary)
            Text("Use this saved device to finish pairing. You do not need to sign in again.")
              .font(.callout).foregroundStyle(.secondary)
          } else if store.status.pairing.sessionState == .invalid {
            Text("The saved Matrix session cannot be read. Check its file permissions or restore it before continuing, or replace it by signing in again.")
              .font(.callout).foregroundStyle(.orange)
          } else {
            Text("No local Matrix device").foregroundStyle(.secondary)
          }

          Divider()

          labeledTextField("Controller user ID", text: $store.controllerUserID)
          labeledTextField("Controller device ID", text: $store.controllerDeviceID)
          labeledTextField("Controller fingerprint", text: $store.controllerFingerprint)
          labeledTextField(
            "Allowed requester IDs",
            prompt: "Comma separated",
            text: $store.requesterIDs
          )
          labeledTextField(
            "Allowed agent names",
            prompt: "Comma separated",
            text: $store.agentNames
          )

          if store.accessGatewayRequired {
            Text(
              "This setup requires local access-gateway authentication. Use the terminal desktop setup command shown in chat, then return here to start the bridge."
            )
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
          labeledSecureField("Pairing code", prompt: "From your agent chat", text: $store.pairingCode)
          HStack {
            Button("Save Setup") { store.saveConfiguration() }
              .disabled(store.isBusy)
            Button("Claim Pairing") { store.pair() }
              .disabled(
                store.isBusy || !store.identityConfirmed
                  || store.status.config.state != "ready"
              )
          }

          if !store.verification.isEmpty {
            LabeledContent("Verification", value: store.verification)
            Text(store.confirmationCommand)
              .font(.system(.body, design: .monospaced))
              .textSelection(.enabled)
          }
        }
        .padding(.top, 10)
      } label: {
        Label("Setup or reconnect", systemImage: "arrow.triangle.2.circlepath")
          .font(.headline)
      }
    }
  }

  private var permissionsCard: some View {
    AppSectionCard {
      VStack(alignment: .leading, spacing: 12) {
        HStack {
          Label("macOS permissions", systemImage: "lock.shield")
            .font(.headline)
          Spacer()
          Button("Check Again") { store.refresh() }
            .disabled(store.isBusy)
        }
        permissionRow(
          title: "Accessibility",
          key: "accessibility",
          status: store.status.permissions.accessibility
        )
        Divider()
        permissionRow(
          title: "Screen Recording",
          key: "screen_recording",
          status: store.status.permissions.screenRecording
        )
        if store.status.permissions.accessibility.state != "granted"
          || store.status.permissions.screenRecording.state != "granted" {
          Text("These checks apply to the running copy of MindRoom. If MindRoom is already enabled in System Settings, quit and reopen it. If access is still unavailable after replacing the app, remove the old permission entry and add the current copy, or reinstall the signed release.")
            .font(.callout).foregroundStyle(.secondary)
        }
      }
    }
  }

  private func permissionRow(
    title: String,
    key: String,
    status: DesktopPermissionStatus
  ) -> some View {
    VStack(alignment: .leading, spacing: 6) {
      HStack {
        LabeledContent(title, value: status.state == "missing" ? "Not granted to this copy" : status.state.capitalized)
        if status.state != "granted" {
          Button("Request") { store.requestPermission(key) }
            .disabled(store.isBusy || !status.canRequest)
          Button("Open Settings") { store.openPermissionSettings(key) }
        }
      }
      if let recovery = status.recovery, status.state != "granted" {
        Text(recovery).font(.callout).foregroundStyle(.secondary)
      }
    }
  }

  private func labeledTextField(
    _ title: String,
    prompt: String? = nil,
    text: Binding<String>
  ) -> some View {
    VStack(alignment: .leading, spacing: 4) {
      Text(title)
        .font(.caption)
        .foregroundStyle(.secondary)
      TextField(prompt ?? title, text: text)
    }
  }

  private func labeledSecureField(
    _ title: String,
    prompt: String,
    text: Binding<String>
  ) -> some View {
    VStack(alignment: .leading, spacing: 4) {
      Text(title)
        .font(.caption)
        .foregroundStyle(.secondary)
      SecureField(prompt, text: text)
    }
  }

  private var browserCard: some View {
    AppSectionCard {
      DisclosureGroup(isExpanded: $isBrowserExpanded) {
        VStack(alignment: .leading, spacing: 12) {
          if !store.canEditBrowserConfiguration {
            Text("Loading saved browser settings…")
              .font(.callout)
              .foregroundStyle(.secondary)
          }
          Toggle("Use browser extension", isOn: $store.browserEnabled)
          labeledTextField("Browser executable path (optional)", text: $store.browserExecutable)
          labeledTextField("Browser profile directory (optional)", text: $store.browserProfile)
          LabeledContent(
            "Readiness",
            value: "\(store.status.browser.runtime), \(store.status.browser.extensionState)"
          )
          Text(
            "Browser control can start or stop its extension session and open a new tab. Existing tabs remain outside control."
          )
          .font(.callout)
          .foregroundStyle(.secondary)
          HStack {
            Button("Start Browser Session") { store.connectBrowser() }
              .disabled(store.isBusy)
            Button("Disconnect") { store.disconnectBrowser() }
              .disabled(store.isBusy)
          }
        }
        .padding(.top, 10)
        .disabled(!store.canEditBrowserConfiguration)
      } label: {
        Label("Optional browser access", systemImage: "network")
          .font(.headline)
      }
    }
  }

  private var diagnosticsCard: some View {
    AppSectionCard {
      DisclosureGroup(isExpanded: $isDiagnosticsExpanded) {
        VStack(alignment: .leading, spacing: 10) {
          LabeledContent("Helper version", value: store.status.helper.version)
          LabeledContent("Configuration revision", value: String(store.status.config.revision))
          Button("Copy Redacted Diagnostics") { store.copyDiagnostics() }
        }
        .padding(.top, 10)
      } label: {
        Label("Diagnostics", systemImage: "stethoscope")
          .font(.headline)
      }
    }
  }

  private var savedControlSummary: String {
    let status = store.status
    return """
      Controller: \(joined([status.config.controllerUserID, status.config.controllerDeviceID, status.pairing.controllerFingerprint]))
      Requester: \(joined(status.config.allowedRequesterIDs))
      Agent: \(joined(status.config.allowedAgentNames))
      Applications: \(joined(status.config.allowedAppIDs))
      Duration: \(store.controlMinutes) minutes
      """
  }

  private func joined(_ values: [String?]) -> String {
    let present = values.compactMap { $0 }.filter { !$0.isEmpty }
    return present.isEmpty ? "Not saved" : present.joined(separator: " · ")
  }

  private func joined(_ values: [String]?) -> String {
    guard let values, !values.isEmpty else { return "Not saved" }
    return values.joined(separator: ", ")
  }
}
