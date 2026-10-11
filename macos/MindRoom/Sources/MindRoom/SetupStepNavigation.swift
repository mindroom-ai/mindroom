import SwiftUI

enum SetupStepProgress: Equatable {
    case complete(String), needsAction(String), idle(String)

    var detail: String {
        switch self {
        case let .complete(detail), let .needsAction(detail), let .idle(detail): detail
        }
    }

    var symbol: String {
        switch self {
        case .complete: "checkmark.circle.fill"
        case .needsAction: "exclamationmark.circle"
        case .idle: "circle"
        }
    }

    var color: Color {
        switch self {
        case .complete: .green
        case .needsAction: .orange
        case .idle: .secondary
        }
    }
}

struct SetupStepNavigation<Step: Hashable>: View {
    let steps: [Step]
    let selection: Step
    let title: (Step) -> String
    let progress: (Step) -> SetupStepProgress
    let select: (Step) -> Void

    var body: some View {
        HStack(spacing: 8) {
            ForEach(steps, id: \.self) { step in
                let status = progress(step)
                Button { select(step) } label: {
                    VStack(spacing: 4) {
                        HStack(spacing: 5) {
                            Image(systemName: status.symbol)
                                .foregroundStyle(status.color).accessibilityHidden(true)
                            Text(title(step))
                        }
                        Text(status.detail).font(.caption).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity).padding(.vertical, 7)
                    .background(selection == step ? Color.accentColor.opacity(0.18) : .clear)
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(title(step)): \(status.detail)")
                .accessibilityAddTraits(selection == step ? .isSelected : [])
            }
        }
    }
}
