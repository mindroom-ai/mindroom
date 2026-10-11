import SwiftUI

enum AppSection: String, CaseIterable, Identifiable {
    case overview = "Overview"
    case chat = "Chat"
    case dashboard = "Dashboard"
    case localAgents = "Local agents"
    case computerAccess = "Computer access"
    case settings = "Settings"

    var id: Self { self }

    var symbol: String {
        switch self {
        case .overview: return "house"
        case .chat: return "bubble.left.and.bubble.right"
        case .dashboard: return "square.grid.2x2"
        case .localAgents: return "cpu"
        case .computerAccess: return "desktopcomputer"
        case .settings: return "gearshape"
        }
    }
}

@MainActor
final class AppNavigation: ObservableObject {
    @Published var section: AppSection = .overview
}
