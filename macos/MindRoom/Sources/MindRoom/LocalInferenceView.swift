import SwiftUI

struct LocalInferenceView: View {
    @ObservedObject var runner: MindRoomCommandRunner
    @ObservedObject var inference: LocalInferenceController

    init(runner: MindRoomCommandRunner) {
        self.runner = runner
        self.inference = runner.inference
    }
    @State private var selected = LocalInferenceModel.recommended(
        memoryGB: Int(ProcessInfo.processInfo.physicalMemory / 1_073_741_824)
    ).id
    private var memoryGB: Int { Int(ProcessInfo.processInfo.physicalMemory / 1_073_741_824) }

    var body: some View {
        AppSectionCard {
            Label("Run AI on this Mac", systemImage: "cpu").font(.headline)
            Text("Download a model once, then inference runs locally. Downloads need internet; your chat account still connects through Matrix.")
            Picker("Model", selection: $selected) {
                ForEach(LocalInferenceModel.choices.filter { $0.minimumMemoryGB <= memoryGB }) { model in
                    Text("\(model.name) · ~\(model.downloadGB, specifier: "%.1f") GB\(model == LocalInferenceModel.recommended(memoryGB: memoryGB) ? " · Recommended" : "")")
                        .tag(model.id)
                }
            }.disabled(inference.busy)
            Text("\(memoryGB) GB unified memory. Smaller models use less memory; available memory and other apps affect performance.")
                .font(.callout).foregroundStyle(.secondary)
            Text(inference.message).textSelection(.enabled)
            if inference.busy {
                if let progress = inference.progress { ProgressView(value: progress) }
                else { ProgressView().controlSize(.small) }
                if inference.isSettingUp { Button("Cancel Setup") { inference.cancel() } }
            } else {
                HStack {
                    Button("Set Up Local Model") {
                        guard let model = LocalInferenceModel.choices.first(where: { $0.id == selected }) else { return }
                        inference.setup(model: model) { runner.refreshStatus() }
                    }.buttonStyle(.borderedProminent)
                        .disabled(runner.isRunningCommand || !runner.localSetup.runtimeReady || !runner.localSetup.configurationExists)
                    if inference.installed {
                        Button(inference.serverRunning ? "Stop Inference" : "Start Inference") {
                            Task { if inference.serverRunning { await inference.stop() } else { await inference.start() } }
                        }.disabled(runner.isRunningCommand)
                    }
                }
            }
            Text("Sets your default model. Agents with explicitly selected models keep their selection. A configuration backup is saved. The inference service starts at login and keeps running after you quit the app.")
                .font(.callout).foregroundStyle(.secondary)
        }
        .task { await inference.refresh() }
    }
}
