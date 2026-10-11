import SwiftUI

struct DesktopErrorDetails: Identifiable {
    let id = UUID()
    let message: String
    let recovery: String?
    let diagnostics: String
}

struct DesktopErrorDetailsView: View {
    let details: DesktopErrorDetails
    @Environment(\.dismiss) private var dismiss
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Computer access details", systemImage: "exclamationmark.triangle")
                .font(.title2.bold())
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text(details.message).textSelection(.enabled)
                    if let recovery = details.recovery {
                        Text("Next step").font(.headline)
                        Text(recovery).textSelection(.enabled)
                    }
                    Divider()
                    Text("Redacted diagnostics").font(.headline)
                    Text(details.diagnostics)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack {
                Button(copied ? "Copied" : "Copy Redacted Diagnostics") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(details.diagnostics, forType: .string)
                    copied = true
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 540, height: 460)
    }
}
