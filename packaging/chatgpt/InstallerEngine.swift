// SPDX-License-Identifier: Apache-2.0
import Foundation
import CryptoKit
import Darwin

struct InstallerError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct InstallationResult {
    let runtime: URL
    let enabled: Bool
}

final class InstallerEngine: @unchecked Sendable {
    let resources: URL
    let home: URL
    let config: URL
    private let lock = NSLock()
    private var currentProcess: Process?
    private var cancelled = false

    init(resources: URL, home: URL, config: URL? = nil) {
        self.resources = resources
        self.home = home
        self.config = config ?? home.appendingPathComponent(".codex/config.toml")
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let process = currentProcess
        lock.unlock()
        if let process { stop(process) }
    }

    private func stop(_ process: Process) {
        guard process.isRunning else { return }
        process.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
    }

    private func checkCancellation() throws {
        lock.lock(); let value = cancelled; lock.unlock()
        if value { throw InstallerError(message: "Installation cancelled. You can run the installer again.") }
    }

    private func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func run(_ executable: URL, _ arguments: [String], environment: [String: String],
                     timeout: TimeInterval = 600) throws -> String {
        try checkCancellation()
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = environment
        process.currentDirectoryURL = resources
        // A file avoids pipe deadlocks, including children retaining a pipe after
        // the parent exits. Logs are private and removed after this operation.
        let log = FileManager.default.temporaryDirectory.appendingPathComponent("veqtor-setup-\(UUID().uuidString).log")
        guard FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
            throw InstallerError(message: "Could not create a temporary installation log.")
        }
        let handle = try FileHandle(forWritingTo: log)
        defer { try? handle.close(); try? FileManager.default.removeItem(at: log) }
        process.standardOutput = handle
        process.standardError = handle
        process.standardInput = FileHandle.nullDevice
        lock.lock(); currentProcess = process; lock.unlock()
        defer { lock.lock(); currentProcess = nil; lock.unlock() }
        try process.run()
        let expiry = DispatchWorkItem { self.stop(process) }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: expiry)
        process.waitUntilExit()
        expiry.cancel()
        try checkCancellation()
        let data = try Data(contentsOf: log)
        let output = String(data: data, encoding: .utf8) ?? ""
        guard process.terminationStatus == 0 else {
            let detail = String(output.suffix(1400)).trimmingCharacters(in: .whitespacesAndNewlines)
            throw InstallerError(message: detail.isEmpty ? "Installation did not finish. Please try again." : detail)
        }
        return output
    }

    func install(author input: String, progress: (String) -> Void) throws -> InstallationResult {
        let author = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !author.isEmpty, author.count <= 120, !author.unicodeScalars.contains(where: { $0.value < 32 }) else {
            throw InstallerError(message: "Enter the name you want used for new Word changes and comments.")
        }
        let fm = FileManager.default
        for path in [config.deletingLastPathComponent(), config] where fm.fileExists(atPath: path.path) {
            let attributes = try fm.attributesOfItem(atPath: path.path)
            guard attributes[.type] as? FileAttributeType != .typeSymbolicLink else {
                throw InstallerError(message: "Your ChatGPT settings use a symbolic link. Please use the manual setup.")
            }
        }
        let existing = fm.fileExists(atPath: config.path) ? try Data(contentsOf: config) : Data()
        let expected = sha256(existing)
        guard let metadata = try JSONSerialization.jsonObject(with: Data(contentsOf: resources.appendingPathComponent("release.json"))) as? [String: String],
              let version = metadata["version"], version.range(of: "^[0-9]+\\.[0-9]+\\.[0-9]+$", options: .regularExpression) != nil,
              let payloadHash = metadata["payload_sha256"] else {
            throw InstallerError(message: "The installer is incomplete. Download it again from veqtor.pro.")
        }
        let payload = resources.appendingPathComponent("workspace.mcpb")
        guard sha256(try Data(contentsOf: payload)) == payloadHash else {
            throw InstallerError(message: "The installer is damaged. Download it again from veqtor.pro.")
        }
        let base = home.appendingPathComponent("Library/Application Support/Veqtor Workspace/ChatGPT")
        // Never overwrite an active runtime: existing chats may still use it.
        let runtime = base.appendingPathComponent("releases/\(version)-\(UUID().uuidString.lowercased())")
        try fm.createDirectory(at: runtime, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let environment = ["HOME": home.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                           "UV_CACHE_DIR": base.appendingPathComponent("cache").path,
                           "UV_PYTHON_INSTALL_DIR": base.appendingPathComponent("python").path,
                           "UV_PYTHON_PREFERENCE": "only-managed", "UV_NO_PROGRESS": "1",
                           "UV_NO_CONFIG": "1", "UV_KEYRING_PROVIDER": "disabled",
                           "PYTHONNOUSERSITE": "1"]
        progress("Preparing Workspace…")
        _ = try run(URL(fileURLWithPath: "/usr/bin/ditto"), ["-x", "-k", payload.path, runtime.path], environment: environment, timeout: 30)
        for name in ["veqtor-launch.py", "configure.py", "probe.py"] {
            try fm.copyItem(at: resources.appendingPathComponent(name), to: runtime.appendingPathComponent(name))
        }
        progress("Downloading the required components…")
        _ = try run(resources.deletingLastPathComponent().appendingPathComponent("Helpers/uv"),
                    ["sync", "--frozen", "--no-dev", "--group", "installer", "--no-install-project", "--no-build",
                     "--directory", runtime.path], environment: environment)
        let python = runtime.appendingPathComponent(".venv/bin/python3")
        progress("Checking the connection…")
        _ = try run(python, ["-I", runtime.appendingPathComponent("probe.py").path, "--root", runtime.path,
                            "--author", author, "--version", version], environment: environment, timeout: 60)
        progress("Connecting to ChatGPT…")
        let output = try run(python, ["-I", runtime.appendingPathComponent("configure.py").path,
                                     "--config", config.path, "--runtime", runtime.path,
                                     "--author", author, "--expected-digest", expected], environment: environment, timeout: 30)
        guard let result = try JSONSerialization.jsonObject(with: Data(output.utf8)) as? [String: Any],
              let enabled = result["enabled"] as? Bool else {
            throw InstallerError(message: "The connection could not be confirmed. Check Workspace in ChatGPT’s MCP settings.")
        }
        return InstallationResult(runtime: runtime, enabled: enabled)
    }
}
