import SwiftUI

struct LocalAgentsView: View {
    @ObservedObject var runner: MindRoomCommandRunner
    @State private var showSetup = false
    @State private var pairCode = ""

    private var state: MindRoomServiceState { runner.serviceStatus.state }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Local agents").font(.largeTitle).fontWeight(.semibold)
            Text("Your agents run on this Mac. A Matrix server connects them to your chat account.")
                .foregroundStyle(.secondary)
            AppSectionCard {
                HStack {
                    Label("Background service", systemImage: "cpu").font(.headline)
                    Spacer()
                    Text(state.shortTitle).foregroundStyle(.secondary)
                }
                Text(runner.serviceStatus.message).textSelection(.enabled)
                if state == .running {
                    Text("The process is running. Check Chat or the dashboard to confirm your agents are ready.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                HStack {
                    if let action = state.primaryAction, !state.needsSetup {
                        Button(action == .stopService ? "Stop Agents" : "Start Agents") { runner.run(action) }
                            .disabled(runner.isRunningCommand)
                    }
                    if state == .running {
                        Button("Restart") { runner.run(.restartService) }.disabled(runner.isRunningCommand)
                    }
                    Button("Refresh Status") { runner.refreshStatus() }
                    Spacer()
                    Button("View Logs") { runner.run(.openLogsFolder) }
                }
            }

            if !state.needsSetup {
                HStack {
                    Button("Open Chat") { runner.run(.openHostedChat) }.buttonStyle(.borderedProminent)
                    Button("Configure Agents…") { runner.run(.openDashboard) }.disabled(!state.canOpenDashboard)
                }
                Text(state.canOpenDashboard
                     ? "Agent and model settings open in the local web dashboard."
                     : "Start the local service to open its configuration dashboard.")
                    .font(.callout).foregroundStyle(.secondary)
            }

            DisclosureGroup("Set up or reconnect local agents", isExpanded: $showSetup) {
                setup.padding(.top, 12)
            }
            .onAppear { if state.needsSetup { showSetup = true } }
            .onChange(of: state) { _, newState in if newState.needsSetup { showSetup = true } }

            Text("The background service starts at login. Closing or quitting the app leaves it running.")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    private var setup: some View {
        VStack(alignment: .leading, spacing: 16) {
            AppSectionCard {
                setupHeading(1, "Install MindRoom", "Install the command-line runtime for local agents. Computer access uses the helper bundled with this app.")
                Button("Install MindRoom") { runner.run(.installRuntime) }
                    .disabled(runner.isRunningCommand)
            }
            AppSectionCard {
                setupHeading(2, "Connect your chat account", "Use hosted MindRoom Chat while your agents run locally. Existing configuration is kept.")
                HStack {
                    Button("Prepare Configuration") { runner.run(.initializeHostedConfig) }
                        .disabled(runner.isRunningCommand || state == .runtimeMissing)
                    Button("Open MindRoom Chat") { runner.run(.openHostedChat) }
                }
                Text("Sign in, open Local MindRoom in the chat sidebar, and generate a pair code.")
                    .font(.callout).foregroundStyle(.secondary)
                HStack {
                    SecureField("Pair code", text: $pairCode).textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 220)
                        .onSubmit(pair)
                    Button("Pair Account", action: pair)
                        .disabled(pairCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                  || runner.isRunningCommand || state == .runtimeMissing)
                }
            }
            AppSectionCard {
                setupHeading(3, "Connect an AI provider", "Add your model credentials to .env in the config folder, or configure a local model in config.yaml before starting agents.")
                Button("Open Config Folder") { runner.run(.openConfigFolder) }
                Text("Already running? Use Configure Agents above to manage providers in the dashboard.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            AppSectionCard {
                setupHeading(4, "Start your agents", "Install the background service after pairing and configuring a provider.")
                Button("Install and Start Agents") { runner.run(.installService) }
                    .disabled(runner.isRunningCommand || state == .runtimeMissing)
            }
            DisclosureGroup("Use your own Matrix server") {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Start and manage your Matrix server separately. Prepare configuration here, then edit config.yaml and .env in the config folder to connect to it.")
                    Button("Prepare Self-Hosted Configuration") { runner.run(.initializeSelfHostedConfig) }
                        .disabled(runner.isRunningCommand || state == .runtimeMissing)
                }.padding(.top, 8)
            }
        }
    }

    private func setupHeading(_ number: Int, _ title: String, _ detail: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("\(number). \(title)").font(.headline)
            Text(detail).foregroundStyle(.secondary)
        }
    }

    private func pair() {
        let code = pairCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !code.isEmpty, !runner.isRunningCommand, state != .runtimeMissing else { return }
        pairCode = ""
        runner.run(.pairHosted(pairCode: code))
    }
}
