import SwiftUI
import UniformTypeIdentifiers

struct DesktopApplicationsView: View {
    @ObservedObject var store: DesktopControlStore
    var showSetup: () -> Void
    var onSaved: () -> Void = {}
    @State private var search = ""
    @State private var showingAppPicker = false
    @State private var confirmingStop = false

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
                HStack {
                    Button(selectionAction.title) {
                        switch selectionAction {
                        case .setup: showSetup()
                        case .stopAndSave: confirmingStop = true
                        case .save: store.saveAllowedApplications(completion: onSaved)
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(selectionAction != .setup && !store.hasAppSelectionChanges)
                    Button("Discard Changes") { store.discardAppSelectionChanges() }
                        .disabled(!store.hasAppSelectionChanges)
                    Spacer()
                    if store.hasAppSelectionChanges {
                        Text(selectionAction != .setup ? "Unsaved changes" : "Not saved yet")
                            .foregroundStyle(.orange)
                    } else if store.status.config.state == "ready" {
                        Text("Saved").foregroundStyle(.secondary)
                    }
                }
                if selectionAction == .setup {
                    Text("Connect your agent first. These app choices will be kept while you finish setup.")
                        .font(.callout).foregroundStyle(.secondary)
                } else if store.status.canStopBridge {
                    Text("Saving stops observation and control. Start Observe Only again when you are ready.")
                        .font(.callout).foregroundStyle(.secondary)
                } else if store.selectedAppIDs.isEmpty {
                    Text("No apps are selected. Agents will not have access to any apps.")
                        .font(.callout).foregroundStyle(.secondary)
                }
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
        .confirmationDialog("Stop computer access and save apps?", isPresented: $confirmingStop) {
            Button("Stop and Save App Access") { store.saveAllowedApplications(completion: onSaved) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This stops observation and revokes control before saving. Computer access stays stopped until you start it again.")
        }
    }

    private var selectionAction: DesktopAppSelectionAction {
        store.needsPairing ? .setup : store.status.appSelectionAction
    }

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
