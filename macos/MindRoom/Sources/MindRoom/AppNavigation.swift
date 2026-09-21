import SwiftUI

enum AppSection: String, CaseIterable, Identifiable {
    case overview = "Overview"
    case localAgents = "Local agents"
    case computerAccess = "Computer access"
    case settings = "Settings"

    var id: Self { self }

    var symbol: String {
        switch self {
        case .overview: return "house"
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
