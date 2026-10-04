import AppKit
import SwiftUI

/// Approval controls stay disabled this long after a request appears or replaces another one.
private let desktopShellApprovalArmingDelay = Duration.seconds(1)

/// Local shell approval, auto-approval, and running commands; shown above every Computer access step.
struct DesktopShellApprovalView: View {
    @ObservedObject var store: DesktopControlStore
    @State private var confirmation: DesktopShellConfirmation?
    @State private var armedRequestID: String?

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
            // The whole command is shown, never clipped, so the decision buttons always come after all of it.
            DesktopLeftToRightText(text: request.displayCommand, textStyle: .body)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(Color(nsColor: .textBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.secondary.opacity(0.5)))
            Text("The command is \(request.commandSizeLabel).").font(.callout).foregroundStyle(.secondary)
            if let warning = request.escapeWarning {
                Label(warning, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.orange)
            }
            if request.hasNonASCIICharacters {
                DesktopLeftToRightText(text: request.asciiEscapedFields, textStyle: .callout)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text("Working folder").font(.callout)
                DesktopLeftToRightText(text: request.displayCwd, textStyle: .callout)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
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
                    .disabled(armedRequestID != request.requestID)
                Menu("Approve & Allow…") {
                    ForEach(DesktopShellAutoApproval.choices) { approval in
                        Button(approval.title) { confirmation = DesktopShellConfirmation(request: request, approval: approval) }
                    }
                }
                .fixedSize()
                .disabled(armedRequestID != request.requestID)
                Spacer()
                revokeButton
            }
        }
        // A click aimed at the previous request, or made just as this one appears, must not approve it.
        .task(id: request.requestID) {
            try? await Task.sleep(for: desktopShellApprovalArmingDelay)
            if !Task.isCancelled { armedRequestID = request.requestID }
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
            Text("Each command opens an approval window. If you close it, review the waiting command here or from the menu bar.")
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
                        Text("\(desktopSafePreview(handle.agentName)) · \(desktopSafePreview(handle.requesterID)) · \(desktopDurationLabel(Int(handle.elapsedSeconds))) · \(handle.stateLabel)")
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

/// Selectable monospaced text whose every line is laid out left to right, so a leading right-to-left letter cannot
/// reverse how a line of a command reads. Only the layout direction is set; the text, and so any copy of it, is unchanged.
struct DesktopLeftToRightText: NSViewRepresentable {
    let text: String
    let textStyle: NSFont.TextStyle

    static func attributedText(_ text: String, textStyle: NSFont.TextStyle) -> NSAttributedString {
        let paragraph = NSMutableParagraphStyle()
        paragraph.baseWritingDirection = .leftToRight
        paragraph.alignment = .left
        paragraph.lineBreakMode = .byWordWrapping
        let size = NSFont.preferredFont(forTextStyle: textStyle).pointSize
        return NSAttributedString(string: text, attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: size, weight: .regular),
            .foregroundColor: NSColor.labelColor,
            .paragraphStyle: paragraph,
        ])
    }

    /// The height all lines of the field need at this width, so the whole text is always shown.
    @MainActor
    static func height(of field: NSTextField, width: CGFloat) -> CGFloat {
        let bounds = NSRect(x: 0, y: 0, width: width, height: .greatestFiniteMagnitude)
        return ceil(field.cell?.cellSize(forBounds: bounds).height ?? 0)
    }

    func makeNSView(context: Context) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: "")
        field.isSelectable = true
        field.baseWritingDirection = .leftToRight
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return field
    }

    func updateNSView(_ field: NSTextField, context: Context) {
        field.attributedStringValue = Self.attributedText(text, textStyle: textStyle)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView field: NSTextField, context: Context) -> CGSize? {
        guard let width = proposal.width, width.isFinite else { return nil }
        return CGSize(width: width, height: Self.height(of: field, width: width))
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
