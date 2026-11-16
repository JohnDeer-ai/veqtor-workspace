// SPDX-License-Identifier: Apache-2.0
// Headless driver for the SAME engine and resources used by the native app.
// Only accepts an empty, disposable directory and never uses the real profile.
import Foundation

@main struct VerifyMain {
    static func main() {
        guard CommandLine.arguments.count == 3 else {
            fputs("Usage: verify-installer APP_RESOURCES EMPTY_TEST_HOME\n", stderr); exit(2)
        }
        let resources = URL(fileURLWithPath: CommandLine.arguments[1])
        let home = URL(fileURLWithPath: CommandLine.arguments[2]).standardizedFileURL
        guard home.path != FileManager.default.homeDirectoryForCurrentUser.path,
              !FileManager.default.fileExists(atPath: home.appendingPathComponent(".codex").path) else {
            fputs("Use a new disposable test profile.\n", stderr); exit(2)
        }
        do {
            let result = try InstallerEngine(resources: resources, home: home).install(author: "Installer Test") { print($0) }
            print("VERIFIED_RUNTIME=" + result.runtime.path)
        } catch { fputs(error.localizedDescription + "\n", stderr); exit(1) }
    }
}
