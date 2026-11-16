// SPDX-License-Identifier: Apache-2.0
import SwiftUI
import AppKit

@MainActor final class SetupModel: ObservableObject {
    @Published var author = NSFullUserName()
    @Published var busy = false
    @Published var status = ""
    @Published var error: String?
    @Published var installed = false
    @Published var enabled = true
    var engine: InstallerEngine?

    func connect() {
        let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.openai.codex")
        guard app != nil else { error = "Install ChatGPT Desktop first, then open this installer again."; return }
        guard let resources = Bundle.main.resourceURL else { return }
        let home = FileManager.default.homeDirectoryForCurrentUser
        let custom = ProcessInfo.processInfo.environment["CODEX_HOME"]
        let config = custom.map { URL(fileURLWithPath: $0).appendingPathComponent("config.toml") }
        let engine = InstallerEngine(resources: resources, home: home, config: config)
        self.engine = engine
        error = nil; busy = true
        let name = author
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let result = try engine.install(author: name) { message in
                    DispatchQueue.main.async { self.status = message }
                }
                DispatchQueue.main.async {
                    self.enabled = result.enabled; self.installed = true; self.busy = false
                }
            } catch {
                DispatchQueue.main.async { self.error = error.localizedDescription; self.busy = false }
            }
        }
    }
}

final class SetupDelegate: NSObject, NSApplicationDelegate {
    weak var model: SetupModel?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model, model.busy else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "Cancel installation?"
        alert.informativeText = "Workspace may still be downloading. You can run the installer again later."
        alert.addButton(withTitle: "Keep installing")
        alert.addButton(withTitle: "Cancel installation")
        if alert.runModal() == .alertSecondButtonReturn { model.engine?.cancel(); return .terminateNow }
        return .terminateCancel
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

struct ConnectButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.headline).padding(.horizontal, 16).padding(.vertical, 11)
            .foregroundStyle(.black)
            .background(Color(red: 0.73, green: 0.96, blue: 0.30), in: RoundedRectangle(cornerRadius: 8))
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

struct SetupView: View {
    @ObservedObject var model: SetupModel
    private let accent = Color(red: 0.73, green: 0.96, blue: 0.30)
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 12) {
                if let path = Bundle.main.path(forResource: "icon", ofType: "png"), let icon = NSImage(contentsOfFile: path) {
                    Image(nsImage: icon).resizable().frame(width: 44, height: 44).clipShape(RoundedRectangle(cornerRadius: 10))
                }
                Text("Veqtor Workspace").font(.title2.weight(.semibold))
            }
            if model.installed {
                Image(systemName: "checkmark.circle.fill").font(.system(size: 42)).foregroundStyle(accent)
                Text("Workspace is installed.").font(.title.weight(.semibold))
                Text(model.enabled
                     ? "Restart ChatGPT Desktop, then start a new chat. Ask it to use Veqtor Workspace with a saved Word file."
                     : "Your existing connection is disabled. Enable Veqtor Workspace in ChatGPT’s MCP settings when you are ready.")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Button("Open ChatGPT") {
                    if let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.openai.codex") {
                        NSWorkspace.shared.openApplication(at: app, configuration: NSWorkspace.OpenConfiguration())
                    }
                }.buttonStyle(ConnectButtonStyle())
                Button("Done") { NSApplication.shared.terminate(nil) }.buttonStyle(.link)
            } else {
                Text("Connect to ChatGPT.").font(.largeTitle.weight(.semibold))
                Text("Let your assistant read, edit and save documents on your Mac.")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                VStack(alignment: .leading, spacing: 7) {
                    Text("Your name in Word").font(.headline)
                    TextField("Name", text: $model.author).textFieldStyle(.roundedBorder).disabled(model.busy)
                    Text("Used for new tracked changes and comments.").font(.callout).foregroundStyle(.secondary)
                }
                if model.busy {
                    HStack { ProgressView().controlSize(.small); Text(model.status).font(.callout) }
                } else {
                    Button("Connect to ChatGPT") { model.connect() }
                        .buttonStyle(ConnectButtonStyle())
                        .controlSize(.large).disabled(model.author.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                if let error = model.error {
                    ScrollView { Text(error).font(.callout).foregroundStyle(.red).textSelection(.enabled) }.frame(maxHeight: 100)
                }
                Text("Connecting lets ChatGPT use Workspace to read and edit files on this Mac. An internet connection is needed for the first installation.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            HStack {
                Text("Mac M1 or newer · macOS 13+").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Link("Installation guide", destination: URL(string: "https://veqtor.pro/setup")!).font(.caption)
            }
        }
        .padding(30).frame(width: 500).fixedSize(horizontal: false, vertical: true)
        .preferredColorScheme(.dark)
    }
}

@main struct VeqtorSetupApp: App {
    @NSApplicationDelegateAdaptor(SetupDelegate.self) var delegate
    @StateObject var model = SetupModel()
    var body: some Scene {
        WindowGroup("Veqtor Workspace") {
            SetupView(model: model).onAppear { delegate.model = model }
        }.windowResizability(.contentSize)
    }
}
