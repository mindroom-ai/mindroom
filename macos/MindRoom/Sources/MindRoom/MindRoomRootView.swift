import SwiftUI

struct MindRoomRootView: View {
    @ObservedObject var navigation: AppNavigation
    @ObservedObject var runner: MindRoomCommandRunner
    @ObservedObject var desktop: DesktopControlStore
    @StateObject private var webTabs: EmbeddedWebTabs
    @FocusState private var focusedSection: AppSection?
    @State private var errorDetails: DesktopErrorDetails?

    init(navigation: AppNavigation, runner: MindRoomCommandRunner, desktop: DesktopControlStore,
         webTabs: EmbeddedWebTabs? = nil) {
        self.navigation = navigation
        self.runner = runner
        self.desktop = desktop
        _webTabs = StateObject(wrappedValue: webTabs ?? EmbeddedWebTabs(
            desktop: desktop, preferences: ChatWebsitePreferences()
        ))
    }

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            ScrollViewReader { proxy in
                VStack(spacing: 0) {
                    if navigation.section == .chat {
                        chatContent
                    } else if navigation.section == .dashboard {
                        dashboardContent
                    } else {
                        ScrollView {
                            VStack(alignment: .leading, spacing: 22) {
                                switch navigation.section {
                                case .overview:
                                    overview
                                case .chat, .dashboard:
                                    EmptyView()
                                case .localAgents:
                                    LocalAgentsView(runner: runner)
                                case .computerAccess:
                                    DesktopControlView(store: desktop) {
                                        withAnimation { proxy.scrollTo(AppSection.computerAccess, anchor: .top) }
                                    }.id(AppSection.computerAccess)
                                case .settings:
                                    AppSettingsView(runner: runner, chatPreferences: webTabs.preferences)
                                }
                                if navigation.section != .computerAccess {
                                    CommandFeedbackView(runner: runner).id("command-feedback")
                                }
                            }
                            .padding(28)
                            .frame(maxWidth: .infinity, alignment: .topLeading)
                        }
                    }
                    if desktop.status.shellApprovalState.isPending {
                        Divider()
                        HStack(spacing: 10) {
                            Image(systemName: "terminal")
                            Text("Shell command waiting for approval")
                            Spacer()
                            Button("Review Command") {
                                DesktopApprovalWindowController.shared.showPending()
                            }.buttonStyle(.borderedProminent)
                        }.padding(14)
                    }
                    if navigation.section == .computerAccess {
                        if let message = desktop.errorMessage {
                            Divider()
                            HStack(spacing: 10) {
                                Image(systemName: "exclamationmark.triangle")
                                Text(message).lineLimit(2)
                                Spacer()
                                Button("View Details") {
                                    errorDetails = DesktopErrorDetails(
                                        message: message, recovery: desktop.recovery,
                                        diagnostics: desktop.diagnosticsText
                                    )
                                }
                            }.padding(14)
                        }
                    } else if navigation.section != .chat && navigation.section != .dashboard {
                        commandActivity {
                            withAnimation { proxy.scrollTo("command-feedback", anchor: .top) }
                        }
                    }
                }
            }
            .background(Color(nsColor: .windowBackgroundColor))
        }
        .disclosureGroupStyle(AppDisclosureGroupStyle())
        .sheet(item: $errorDetails) { details in
            DesktopErrorDetailsView(details: details)
        }
        .onChange(of: runner.serviceStatus.state) { _, state in
            if state != .running { webTabs.serviceStopped() }
            else if navigation.section == .dashboard { webTabs.openDashboard() }
        }
    }

    private var chatContent: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Chat").font(.headline)
                Spacer()
                Button("Reload") { webTabs.openChat(force: true) }
                Button("Open in Browser") { NSWorkspace.shared.open(webTabs.preferences.url) }
            }
            .padding(.horizontal, 16).padding(.vertical, 9)
            Divider()
            ZStack {
                EmbeddedWebView(webView: webTabs.chat)
                    .allowsHitTesting(webTabs.chatError == nil && !webTabs.chatLoading)
                    .accessibilityHidden(webTabs.chatError != nil || webTabs.chatLoading)
                if let error = webTabs.chatError {
                    webMessage(error, retry: { webTabs.openChat(force: true) })
                } else if webTabs.chatLoading {
                    webLoading("Loading Chat…")
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear { webTabs.openChat() }
    }

    @ViewBuilder
    private var dashboardContent: some View {
        if runner.serviceStatus.state != .running {
            VStack(spacing: 14) {
                Text("Local dashboard unavailable").font(.title2)
                Text("Start local agents to open the dashboard.").foregroundStyle(.secondary)
                Button("Open Local Agents") { navigation.section = .localAgents }
                Button("Refresh Status") { runner.refreshStatus() }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            VStack(spacing: 0) {
                HStack {
                    Text("Dashboard").font(.headline)
                    Spacer()
                    Button("Reload") { webTabs.openDashboard(force: true) }
                }
                .padding(.horizontal, 16).padding(.vertical, 9)
                Divider()
                ZStack {
                    EmbeddedWebView(webView: webTabs.dashboard)
                        .allowsHitTesting(webTabs.dashboardError == nil && !webTabs.dashboardLoading)
                        .accessibilityHidden(webTabs.dashboardError != nil || webTabs.dashboardLoading)
                    if let error = webTabs.dashboardError {
                        webMessage(error, retry: { webTabs.openDashboard(force: true) })
                    } else if webTabs.dashboardLoading {
                        webLoading("Opening Dashboard…")
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .onAppear { webTabs.openDashboard() }
        }
    }

    private func webMessage(_ message: String, retry: @escaping () -> Void) -> some View {
        VStack(spacing: 14) {
            Text(message).multilineTextAlignment(.center)
            HStack {
                Button("Retry", action: retry)
            }
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private func webLoading(_ title: String) -> some View {
        ProgressView(title)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(nsColor: .windowBackgroundColor))
    }

    @ViewBuilder
    private func commandActivity(showDetails: @escaping () -> Void) -> some View {
        if let title = runner.runningCommandTitle {
            Divider()
            HStack(spacing: 10) {
                ProgressView().controlSize(.small)
                Text("\(title)…")
                Spacer()
            }.padding(14)
        } else if let feedback = runner.feedback {
            Divider()
            HStack(spacing: 10) {
                Image(systemName: feedback.result.isSuccess ? "checkmark.circle" : "exclamationmark.triangle")
                Text(feedback.result.isSuccess ? "Action finished" : "Action failed")
                Spacer()
                Button("View Result", action: showDetails)
            }.padding(14)
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                MindRoomLogo().frame(width: 36, height: 36)
                Text("MindRoom").font(.headline)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 18)
            ForEach(AppSection.allCases.filter { $0 != .settings }) { section in
                navigationButton(section)
            }
            Spacer(minLength: 24)
            navigationButton(.settings)
            Text("This Mac").font(.caption).foregroundStyle(.secondary).padding(10)
        }
        .padding(10)
        .frame(width: 195)
        .frame(maxHeight: .infinity)
        .background(.regularMaterial)
        .onChange(of: navigation.section) { _, section in
            // Keep keyboard focus with the selected section when setup redirects navigation.
            if focusedSection != nil { focusedSection = section }
        }
    }

    private func navigationButton(_ section: AppSection) -> some View {
        Button {
            navigation.section = section
        } label: {
            Label(section.rawValue, systemImage: section.symbol)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 10).padding(.vertical, 9)
                .background(navigation.section == section ? Color.accentColor.opacity(0.16) : .clear)
                .clipShape(RoundedRectangle(cornerRadius: 7))
                .overlay {
                    RoundedRectangle(cornerRadius: 7).strokeBorder(
                        focusedSection == section && navigation.section != section ? Color.accentColor : .clear,
                        lineWidth: 1
                    )
                }
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .focused($focusedSection, equals: section)
        .focusEffectDisabled()
        .accessibilityAddTraits(navigation.section == section ? .isSelected : [])
    }

    private var overview: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(spacing: 14) {
                MindRoomLogo().frame(width: 64, height: 64)
                VStack(alignment: .leading, spacing: 5) {
                    Text("MindRoom on this Mac").font(.largeTitle).fontWeight(.semibold)
                    Text("Choose what runs here and what can access this Mac.")
                        .foregroundStyle(.secondary)
                }
            }
            AppSectionCard {
                sectionHeading("Local agents", status: runner.serviceStatus.state.shortTitle)
                Text("Run your MindRoom agents on this Mac. Chat with them from your browser.")
                    .foregroundStyle(.secondary)
                HStack {
                    if !runner.serviceStatus.state.needsSetup {
                        Button("Open Chat") { runner.run(.openHostedChat) }.buttonStyle(.borderedProminent)
                    }
                    Button(runner.serviceStatus.state.needsSetup ? "Set Up Local Agents" : "Manage Local Agents") {
                        navigation.section = .localAgents
                    }
                }
            }
            AppSectionCard {
                sectionHeading("Computer access", status: desktop.connectionStatusLabel)
                Text("Let agents running elsewhere use selected apps, read selected folders, or run commands you approve on this Mac.")
                    .foregroundStyle(.secondary)
                Button(desktop.status.hasSavedConnection && !desktop.needsPairing ? "Manage Access" : "Set Up Computer Access") {
                    navigation.section = .computerAccess
                }
            }
            Text("Use either role, or both. Closing this window keeps background work running.")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    private func sectionHeading(_ title: String, status: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).font(.headline)
            Spacer()
            Text(status).font(.callout).foregroundStyle(.secondary)
        }
    }
}

struct AppSectionCard<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) { content }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(nsColor: .controlBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(.quaternary))
    }
}

struct CommandFeedbackView: View {
    @ObservedObject var runner: MindRoomCommandRunner

    var body: some View {
        if let title = runner.runningCommandTitle {
            HStack(spacing: 10) {
                ProgressView().controlSize(.small)
                Text("\(title)…")
            }
            .accessibilityElement(children: .combine)
        } else if let feedback = runner.feedback {
            AppSectionCard {
                Label(
                    "\(feedback.title) \(feedback.result.isSuccess ? "finished" : "failed")",
                    systemImage: feedback.result.isSuccess ? "checkmark.circle" : "exclamationmark.triangle"
                ).font(.headline)
                Text(feedback.result.isSuccess
                     ? feedback.successMessage ?? feedback.result.condensedOutput
                     : feedback.result.condensedOutput)
                    .textSelection(.enabled)
                if !feedback.result.isSuccess && feedback.result.output.contains("No such option") {
                    Text("Update the local runtime in Settings, then try again.").foregroundStyle(.secondary)
                }
                if !runner.lastOutput.isEmpty {
                    DisclosureGroup("Command output") {
                        Text(runner.lastOutput).font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Button("Copy Output") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(runner.lastOutput, forType: .string)
                    }
                }
            }
        }
    }
}
