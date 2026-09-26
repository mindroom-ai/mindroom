import SwiftUI

/// Local shell approval, auto-approval, and running commands; shown above every Computer access step.
struct DesktopShellApprovalView: View {
    @ObservedObject var store: DesktopControlStore
    @State private var confirmation: DesktopShellConfirmation?

    var body: some View {
        AppSectionCard {
            switch store.status.shellApprovalState {
            case let .pending(request): pendingRequest(request)
            case .askEachTime: askEachTime
            case let .autoApprove(seconds): autoApproval("Approving shell commands without asking · \(desktopDurationLabel(seconds)) left")
            case .untilRevoked: autoApproval("Approving shell commands without asking until you stop it")
            case .off: EmptyView()
            }
            if store.status.shell.activeRequestID != nil {
                Label("An approved command is running.", systemImage: "hourglass").font(.callout)
            }
            if !store.status.shell.handles.isEmpty { handles }
        }
        .confirmationDialog(
            confirmation?.title ?? "",
            isPresented: Binding(get: { confirmation != nil }, set: { if !$0 { confirmation = nil } }),
            titleVisibility: .visible,
            presenting: confirmation
        ) { confirmation in
            Button(confirmation.confirmTitle) {
                if let request = confirmation.request {
                    store.decideShell(request, .approveAndAllow(confirmation.approval))
                } else {
                    store.grantShell(confirmation.approval)
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: { confirmation in
            Text(store.status.shellAutoApprovalScope(confirmation.approval))
        }
    }

    private func pendingRequest(_ request: DesktopShellRequest) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Shell command waiting for your approval", systemImage: "terminal").font(.headline)
            ScrollView {
                Text(request.displayCommand)
                    .font(.system(.body, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
            }
            .frame(maxHeight: 160)
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.secondary.opacity(0.5)))
            if request.hasEscapedCharacters {
                Label("This request contains control or text-direction characters, shown as \\u{…}.", systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.orange)
            }
            detail("Working folder", request.displayCwd)
            detail("Agent", request.displayAgentName)
            detail("Requester", request.displayRequesterID)
            TimelineView(.periodic(from: .now, by: 1)) { context in
                let remaining = max(0, Int(request.expiresAtMilliseconds / 1000 - context.date.timeIntervalSince1970))
                Text("Expires in \(desktopDurationLabel(remaining)). It runs with your macOS account's access, not only inside the working folder.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            HStack {
                Button("Reject") { store.decideShell(request, .reject) }
                Button("Approve Once") { store.decideShell(request, .approveOnce) }
                    .buttonStyle(.borderedProminent)
                Menu("Approve & Allow…") {
                    ForEach(DesktopShellAutoApproval.choices) { approval in
                        Button(approval.title) { confirmation = DesktopShellConfirmation(request: request, approval: approval) }
                    }
                }
                .fixedSize()
                Spacer()
                revokeButton
            }
        }
    }

    private var askEachTime: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("Shell commands: Ask for each command", systemImage: "terminal").font(.headline)
                Spacer()
                Menu("Allow Without Asking…") {
                    ForEach(DesktopShellAutoApproval.choices) { approval in
                        Button(approval.title) { confirmation = DesktopShellConfirmation(request: nil, approval: approval) }
                    }
                }
                .fixedSize()
                revokeButton
            }
            Text("Each command request waits here for your approval. The menu bar shows when one is waiting.")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    private func autoApproval(_ title: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label(title, systemImage: "exclamationmark.shield").font(.headline)
                Spacer()
                revokeButton
            }
            Text("Commands from all locally allowed agents and requesters run with your macOS account's access. Revoking stops approved commands and returns to asking for each command.")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    private var handles: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            Text("Background commands").font(.subheadline.bold())
            ForEach(store.status.shell.handles) { handle in
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(desktopSafePreview(handle.commandPreview))
                            .font(.system(.callout, design: .monospaced)).textSelection(.enabled).lineLimit(3)
                        Text("\(desktopSafePreview(handle.agentName)) · \(desktopSafePreview(handle.requesterID)) · \(desktopDurationLabel(Int(handle.elapsedSeconds))) · \(handle.state == "running" ? "Running" : "Finished")")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if handle.state == "running" {
                        Button("Kill", role: .destructive) { store.killShellHandle(handle.handle) }
                    }
                }
            }
        }
    }

    // Never disabled: revoking must stay possible while a command runs or another action is busy.
    private var revokeButton: some View {
        Button("Revoke Shell Access", role: .destructive) { store.revokeShell() }
    }

    private func detail(_ title: String, _ value: String) -> some View {
        LabeledContent(title) {
            Text(value).font(.system(.callout, design: .monospaced)).textSelection(.enabled).multilineTextAlignment(.trailing)
        }
    }
}

/// Captures the exact request being answered when the confirmation opens.
private struct DesktopShellConfirmation: Identifiable {
    let request: DesktopShellRequest?
    let approval: DesktopShellAutoApproval

    var id: String { "\(request?.requestID ?? "grant")-\(approval.title)" }

    var title: String {
        let duration = approval == .untilStopped ? "until you stop it" : "for \(approval.title.lowercased())"
        return request == nil
            ? "Allow shell commands without asking \(duration)?"
            : "Approve this command and allow shell commands without asking \(duration)?"
    }

    var confirmTitle: String {
        request == nil ? "Allow \(approval.title)" : "Approve & Allow \(approval.title)"
    }
}
