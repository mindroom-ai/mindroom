import SwiftUI

struct LocalAgentsView: View {
    @ObservedObject var runner: MindRoomCommandRunner
    var scrollToTop: () -> Void = {}
    @State private var step = LocalAgentsSetupStep.install
    @State private var choseInitialStep = false
    @State private var showChatSetup = false
    @State private var pairCode = ""
    @FocusState private var pairCodeFocused: Bool

    private var state: MindRoomServiceState { runner.serviceStatus.state }
    private var setup: LocalAgentsSetupSnapshot { runner.localSetup }
    private var busy: Bool { runner.isRunningCommand || !runner.hasRefreshedStatus }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Local agents").font(.largeTitle.bold())
            Text("Install, configure, and run agents on this Mac. Your chat account connects you to them.")
                .foregroundStyle(.secondary)
            summary
            SetupStepNavigation(
                steps: LocalAgentsSetupStep.allCases, selection: step,
                title: { "\($0.rawValue + 1). \($0.title)" },
                progress: {
                    runner.hasRefreshedStatus
                        ? setup.progress(for: $0, service: state, check: runner.setupCheck)
                        : .idle("Checking…")
                }, select: show
            )
            CommandFeedbackView(runner: runner, compact: true).id("command-feedback")
            switch step {
            case .install: install
            case .configure: configure
            case .check: check
            case .start: start
            }
            Text("The background service starts at login. Quitting this app leaves running agents active.")
                .font(.callout).foregroundStyle(.secondary)
        }
        .task { runner.refreshStatus() }
        .onChange(of: runner.isRefreshingStatus, initial: true) { _, refreshing in
            guard !refreshing, runner.hasRefreshedStatus, !choseInitialStep else { return }
            choseInitialStep = true
            step = setup.nextStep(service: state, check: runner.setupCheck)
            showChatSetup = !setup.configurationExists
        }
        .onChange(of: setup.runtimeInstalled) { wasInstalled, installed in
            if choseInitialStep, !wasInstalled, installed, step == .install { show(.configure) }
        }
        .onChange(of: runner.setupCheck) { _, result in
            if result?.isSuccess == true { show(.start) }
        }
        .onChange(of: runner.runningCommandTitle) { _, _ in scrollToTop() }
    }

    private var summary: some View {
        AppSectionCard {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 6) {
                    Label(summaryTitle, systemImage: state == .running ? "checkmark.circle.fill" : "cpu")
                        .font(.headline)
                    Text(summaryDetail).font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button(runner.isRefreshingStatus ? "Refreshing…" : "Refresh Status") { runner.refreshStatus() }
                    .disabled(runner.isRefreshingStatus)
            }
        }
    }

    private var summaryTitle: String {
        guard runner.hasRefreshedStatus else { return "Checking this Mac…" }
        switch state {
        case .running: return "Local agents service running"
        case .stopped: return "Local agents service stopped"
        case .notInstalled: return "Runtime installed · Background service not installed"
        case .runtimeMissing: return "Install the local-agent runtime"
        case .unknown: return "Service status unavailable"
        }
    }

    private var summaryDetail: String {
        guard runner.hasRefreshedStatus else { return "Looking for the runtime, configuration, and background service." }
        switch state {
        case .running: return "The process is running on this Mac. Open Chat or Dashboard to check your agents."
        case .stopped: return "The service is installed. Choose Start when you want your agents to run."
        case .notInstalled:
            return setup.configurationExists
                ? "You do not need to reinstall MindRoom. Check your setup, then install and start the service in step 4."
                : "The runtime is ready. Prepare your configuration in step 2, then check and start your agents."
        case .runtimeMissing: return "The menu bar app and the local-agent runtime are separate installations. Begin with step 1."
        case .unknown: return runner.serviceStatus.message
        }
    }

    private var install: some View {
        AppSectionCard {
            Label(setup.runtimeInstalled ? "MindRoom runtime installed" : "Install MindRoom runtime",
                  systemImage: setup.runtimeInstalled ? "checkmark.circle.fill" : "arrow.down.circle")
                .font(.headline)
            if let path = setup.runtimePath {
                Text("Already installed on this Mac. Continue with your existing configuration or set up a new one.")
                Text(path).font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled)
                Button("Continue to Configure") { show(.configure) }.buttonStyle(.borderedProminent)
            } else {
                Text("Install the command-line runtime that runs local agents. This does not install or start the background service.")
                Button("Install MindRoom") { runner.run(.installRuntime) }
                    .buttonStyle(.borderedProminent).disabled(busy)
            }
            Text("Computer access uses the helper bundled with this app and does not need this runtime.")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    private var configure: some View {
        VStack(alignment: .leading, spacing: 12) {
            AppSectionCard {
                Label(setup.configurationExists ? "Existing configuration found" : "Prepare your configuration",
                      systemImage: setup.configurationExists ? "checkmark.circle.fill" : "doc.badge.plus")
                    .font(.headline)
                Text(setup.configurationExists
                     ? "Your config.yaml is already present. Existing files are kept. Check Setup will verify configuration and connectivity."
                     : "Prepare configuration, pair your chat account, and choose an AI provider.")
                if !setup.configurationExists {
                    Button("Prepare Configuration") { runner.run(.initializeHostedConfig) }
                        .buttonStyle(.borderedProminent).disabled(busy || !setup.runtimeInstalled)
                }
                if setup.configurationExists {
                    DisclosureGroup("Connect or reconnect your chat account", isExpanded: $showChatSetup) {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Open MindRoom Chat, sign in, then open Local MindRoom in the chat sidebar to generate a pair code. Skip pairing if this configuration is already connected.")
                            Button("Open MindRoom Chat") { runner.run(.openHostedChat) }
                            HStack {
                                SecureField("Pair code", text: $pairCode).textFieldStyle(.roundedBorder)
                                    .focused($pairCodeFocused)
                                    .frame(maxWidth: 240).onSubmit(pair)
                                Button("Pair Account", action: pair)
                                    .disabled(busy || !setup.runtimeInstalled || !setup.configurationExists || pairCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            }
                        }.padding(.top, 8)
                    }
                    Divider()
                    Text("Choose an AI provider").font(.headline)
                    Text("Add your provider credentials to .env, or configure a local model in config.yaml. Existing credentials can be kept; Check Setup verifies them.")
                    Button("Open Config Folder") { runner.run(.openConfigFolder) }
                }
                DisclosureGroup("Use your own Matrix server") {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Manage your Matrix server separately. Prepare configuration, then edit config.yaml and .env to connect it.")
                        Button("Prepare Self-Hosted Configuration") { runner.run(.initializeSelfHostedConfig) }
                            .disabled(busy || !setup.runtimeInstalled)
                    }.padding(.top, 8)
                }
            }
            if !setup.runtimeInstalled { Text("Install the runtime in step 1 to prepare or pair configuration.").foregroundStyle(.secondary) }
            Button("Continue to Check") { show(.check) }
                .buttonStyle(.borderedProminent).disabled(!setup.runtimeInstalled || !setup.configurationExists)
        }
    }

    private var check: some View {
        AppSectionCard {
            Label("Check your setup", systemImage: "checkmark.shield").font(.headline)
            Text("MindRoom Doctor checks your configuration, AI providers, Matrix server, and local storage. It contacts the services you configured; checks can take several minutes.")
            if let result = runner.setupCheck {
                Text(result.isSuccess ? "The last setup check passed." : "The last check found issues. Review the result above, fix your configuration, then check again.")
            } else {
                Text("Not checked in this session. Files found does not mean credentials or connections have been verified.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            HStack {
                Button("Check Setup") { runner.run(.checkSetup) }.buttonStyle(.borderedProminent)
                    .disabled(busy || !setup.runtimeInstalled || !setup.configurationExists)
                Button("Open Config Folder") { runner.run(.openConfigFolder) }
                Spacer()
                Button("Continue to Start") { show(.start) }
            }
            if !setup.runtimeInstalled || !setup.configurationExists {
                Text("Complete Install and Configure before running checks.").foregroundStyle(.secondary)
            }
        }
    }

    private var start: some View {
        AppSectionCard {
            Label("Background service on this Mac", systemImage: "cpu").font(.headline)
            Text(runner.serviceStatus.message)
            if state == .running {
                HStack {
                    Button("Open Chat") { runner.run(.openHostedChat) }.buttonStyle(.borderedProminent)
                    Button("Open Dashboard") { runner.run(.openDashboard) }
                    Spacer()
                    Button("Stop Agents") { runner.run(.stopService) }.disabled(busy)
                    Button("Restart") { runner.run(.restartService) }.disabled(busy)
                }
            } else {
                if runner.setupCheck?.isSuccess != true {
                    Text("A setup check is recommended before starting. You can also start using configuration you already know works.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                HStack {
                    Button(state == .notInstalled ? "Install and Start Agents" : "Start Agents") {
                        runner.run(state == .notInstalled ? .installService : .startService)
                    }.buttonStyle(.borderedProminent).disabled(busy || !setup.canStart(service: state))
                    Button("Check Setup First") { show(.check) }
                }
                if !setup.runtimeInstalled { Text("Install the runtime in step 1 before starting.").foregroundStyle(.secondary) }
                else if !setup.configurationExists { Text("Prepare configuration in step 2 before starting.").foregroundStyle(.secondary) }
                else if state == .unknown { Text("Refresh Status to determine whether the service can be started.").foregroundStyle(.secondary) }
            }
            Divider()
            Button("View Logs") { runner.run(.openLogsFolder) }
        }
    }

    private func show(_ target: LocalAgentsSetupStep) {
        choseInitialStep = true
        step = target
        scrollToTop()
    }

    private func pair() {
        let code = pairCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !code.isEmpty, !busy, setup.runtimeInstalled, setup.configurationExists else { return }
        pairCodeFocused = false
        pairCode = ""
        runner.run(.pairHosted(pairCode: code))
    }
}
