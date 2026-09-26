import SwiftUI
import UniformTypeIdentifiers

struct DesktopApplicationsView: View {
    @ObservedObject var store: DesktopControlStore
    var showSetup: () -> Void
    var onSaved: (DesktopStatus) -> Void = { _ in }
    @State private var search = ""
    @State private var showingAppPicker = false
    @State private var confirmingStop = false
    private static let saveTitle = "Save App Access"

    var body: some View {
        AppSectionCard {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Label("Allowed applications", systemImage: "app.badge.checkmark")
                        .font(.headline)
                    Spacer()
                    Text("\(store.selectedAppIDs.subtracting(["primary-screen"]).count) selected")
                        .foregroundStyle(.secondary)
                }
                Text("Check the apps your paired agents may use, then save app access. This does not grant control or change macOS permissions.")
                    .font(.callout).foregroundStyle(.secondary)
                DesktopAccessSaveBar(
                    store: store, saveTitle: Self.saveTitle, hasChanges: store.hasAppSelectionChanges,
                    idleNote: store.selectedAppIDs.isEmpty ? "No apps are selected. Agents will not have access to any apps." : nil,
                    confirmingStop: $confirmingStop, showSetup: showSetup, save: save,
                    discard: store.discardAppSelectionChanges
                )
                Divider()
                HStack {
                    TextField("Search apps by name or bundle ID", text: $search)
                        .textFieldStyle(.roundedBorder)
                    Button("Add App…") { showingAppPicker = true }
                    Button("Refresh List") { store.refreshApplications() }
                }
                List {
                    if filteredApplications.isEmpty {
                        Text("No matching apps. Use Add App to choose an application from another folder.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(filteredApplications) { app in
                        Toggle(isOn: selection(for: app.id)) {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(app.name)
                                    Text(app.id).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if app.running {
                                    Text("Running").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                        .toggleStyle(.checkbox)
                        .padding(.vertical, 3)
                    }
                }
                .frame(height: 250)
                Text("\(filteredApplications.count) of \(applications.count) apps shown. Apps in standard folders and running apps are included; use Add App for other locations.")
                    .font(.caption).foregroundStyle(.secondary)
                DisclosureGroup("Advanced screen access") {
                    VStack(alignment: .leading, spacing: 6) {
                        Toggle("Allow primary screen", isOn: selection(for: "primary-screen"))
                            .toggleStyle(.checkbox)
                        Text("Allows capture of the entire primary screen and coordinate input, including content outside the selected apps.")
                            .font(.callout).foregroundStyle(.secondary)
                    }.padding(.top, 6)
                }

            }
            .disabled(store.isBusy)
        }
        .fileImporter(isPresented: $showingAppPicker, allowedContentTypes: [.applicationBundle]) { result in
            if case let .success(url) = result {
                if let id = store.addApplication(at: url) { search = id }
            }
        }
        .desktopAccessStopConfirmation(
            isPresented: $confirmingStop, title: "Stop computer access and save apps?",
            saveTitle: Self.saveTitle, onConfirm: save
        )
    }

    private func save() { store.saveAllowedApplications(completion: onSaved) }

    private var applications: [InstalledDesktopApplication] {
        var result = store.applications.filter { $0.id != "primary-screen" }
        let knownIDs = Set(result.map(\.id))
        let savedAndSelected = Set(store.status.config.allowedAppIDs ?? []).union(store.selectedAppIDs)
        for id in savedAndSelected.subtracting(knownIDs).subtracting(["primary-screen"]) {
            result.append(InstalledDesktopApplication(id: id, name: id, running: false))
        }
        return result.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }

    private var filteredApplications: [InstalledDesktopApplication] {
        applications.filter { $0.matches(search: search) }
    }

    private func selection(for id: String) -> Binding<Bool> {
        Binding(
            get: { store.selectedAppIDs.contains(id) },
            set: { allowed in
                if allowed { store.selectedAppIDs.insert(id) }
                else { store.selectedAppIDs.remove(id) }
            }
        )
    }
}
