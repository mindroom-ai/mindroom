import SwiftUI

struct AppSettingsView: View {
    @ObservedObject var runner: MindRoomCommandRunner
    @State private var startAtLogin = LoginItemController.shared.isEnabled
    @State private var loginTitle = LoginItemController.shared.menuTitle
    @State private var errorMessage: String?
    @State private var confirmServiceInstall = false

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Settings").font(.largeTitle).fontWeight(.semibold)
            Text("App preferences, updates, and troubleshooting.").foregroundStyle(.secondary)
            AppSectionCard {
                LabeledContent("MindRoom version", value: appVersion)
                    .textSelection(.enabled)
            }
            AppSectionCard {
                Toggle("Open menu bar app at login", isOn: Binding(
                    get: { startAtLogin },
                    set: { _ in toggleLogin() }
                )).disabled(!LoginItemController.shared.canToggle)
                Text("Starts quietly in the menu bar. Local agents have their own background service; computer access starts only when you choose.")
                    .font(.callout).foregroundStyle(.secondary)
                if !LoginItemController.shared.canToggle {
                    Text(loginTitle).font(.callout)
                }
            }
            AppSectionCard {
                Text("Updates").font(.headline)
                Text("App updates include the computer-access helper. The local-agent runtime updates separately.").foregroundStyle(.secondary)
                HStack {
                    Button("Check App Updates…") {
                        do { try AppUpdater.shared.checkForUpdates() }
                        catch { errorMessage = error.localizedDescription }
                    }.disabled(!AppUpdater.shared.canCheckForUpdates)
                    Button("Update Local Runtime") { runner.run(.updateRuntime) }
                        .disabled(runner.isRunningCommand)
                }
                Text("After updating the runtime, apply it to the background service to use the new version. This starts or restarts local agents.")
                    .font(.callout).foregroundStyle(.secondary)
                Button("Apply Runtime to Service…") { confirmServiceInstall = true }
                    .disabled(runner.isRunningCommand || runner.serviceStatus.state == .runtimeMissing)
                    .confirmationDialog("Apply the runtime and restart local agents?", isPresented: $confirmServiceInstall) {
                        Button("Apply and Start Agents") { runner.run(.installService) }
                    } message: {
                        Text("The service will use the currently installed runtime. Active local-agent work may be interrupted.")
                    }
                if !AppUpdater.shared.canCheckForUpdates {
                    Text("App updates are not configured for this build.").font(.callout).foregroundStyle(.secondary)
                }
            }
            AppSectionCard {
                Text("Troubleshooting").font(.headline)
                HStack {
                    Button("Open Logs Folder") { runner.run(.openLogsFolder) }
                    Button("Open Config Folder") { runner.run(.openConfigFolder) }
                }
                Text("The app and command-line tools share ~/.mindroom. Command results appear below.")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }
        .onAppear { refreshLogin() }
        .alert("Unable to Complete Action", isPresented: Binding(
            get: { errorMessage != nil }, set: { if !$0 { errorMessage = nil } }
        )) {
            Button("OK") { errorMessage = nil }
        } message: {
            Text(errorMessage ?? "")
        }
    }

    private func refreshLogin() {
        startAtLogin = LoginItemController.shared.isEnabled
        loginTitle = LoginItemController.shared.menuTitle
    }

    private var appVersion: String {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Development"
        guard let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String else { return version }
        return "\(version) (build \(build))"
    }

    private func toggleLogin() {
        do { try LoginItemController.shared.toggle() }
        catch { errorMessage = error.localizedDescription }
        refreshLogin()
    }
}
