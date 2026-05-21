import Cocoa
import Foundation

private struct CommandResult {
    let output: String
    let exitCode: Int32
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let home = NSHomeDirectory()
    private let dashboardURL = URL(string: "http://127.0.0.1:9119")!

    private var hermesPath: String {
        "\(home)/.local/bin/hermes"
    }

    private var projectPath: String {
        "\(home)/Git/hermes/hermes-agent"
    }

    private var hermesHome: String {
        "\(home)/.hermes"
    }

    private var commandPath: String {
        [
            "\(projectPath)/venv/bin",
            "\(home)/.local/bin",
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ].joined(separator: ":")
    }

    private let gatewayStatusItem = NSMenuItem(title: "Gateway: checking...", action: nil, keyEquivalent: "")
    private let dashboardStatusItem = NSMenuItem(title: "Dashboard: checking...", action: nil, keyEquivalent: "")
    private var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        if let button = statusItem.button {
            button.title = "H"
            button.toolTip = "Hermes"
        }

        buildMenu()
        statusItem.menu = menu
        refreshStatus()
        timer = Timer.scheduledTimer(withTimeInterval: 15.0, repeats: true) { [weak self] _ in
            self?.refreshStatus()
        }
    }

    private func buildMenu() {
        let title = NSMenuItem(title: "Hermes", action: nil, keyEquivalent: "")
        title.isEnabled = false
        menu.addItem(title)
        menu.addItem(.separator())

        gatewayStatusItem.isEnabled = false
        dashboardStatusItem.isEnabled = false
        menu.addItem(gatewayStatusItem)
        menu.addItem(dashboardStatusItem)
        menu.addItem(.separator())

        menu.addItem(item("Open Dashboard", #selector(openDashboard)))
        menu.addItem(item("Start Dashboard", #selector(startDashboard)))
        menu.addItem(item("Stop Dashboard", #selector(stopDashboard)))
        menu.addItem(.separator())

        menu.addItem(item("Start Gateway", #selector(startGateway)))
        menu.addItem(item("Restart Gateway", #selector(restartGateway)))
        menu.addItem(item("Stop Gateway", #selector(stopGateway)))
        menu.addItem(.separator())

        menu.addItem(item("Open Config", #selector(openConfig)))
        menu.addItem(item("Open Logs", #selector(openLogs)))
        menu.addItem(item("Open Hermes Folder", #selector(openHermesFolder)))
        menu.addItem(.separator())

        menu.addItem(item("Refresh", #selector(refreshStatusAction)))
        menu.addItem(item("Quit", #selector(quit)))
    }

    private func item(_ title: String, _ action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        return item
    }

    @objc private func refreshStatusAction() {
        refreshStatus()
    }

    private func refreshStatus() {
        DispatchQueue.global(qos: .utility).async {
            let gatewayRunning = self.isGatewayRunning()
            let dashboardRunning = self.isDashboardRunning()
            DispatchQueue.main.async {
                self.gatewayStatusItem.title = gatewayRunning ? "Gateway: running" : "Gateway: stopped"
                self.dashboardStatusItem.title = dashboardRunning ? "Dashboard: running" : "Dashboard: stopped"
                self.statusItem.button?.title = gatewayRunning ? "H" : "H!"
                self.statusItem.button?.toolTip = gatewayRunning ? "Hermes gateway is running" : "Hermes gateway is stopped"
            }
        }
    }

    @objc private func openDashboard() {
        DispatchQueue.global(qos: .userInitiated).async {
            if !self.isDashboardRunning() {
                _ = self.runShell(
                    "cd \(self.quoted(self.projectPath)) && nohup \(self.quoted(self.hermesPath)) dashboard --no-open > \(self.quoted(self.hermesHome + "/logs/dashboard-menubar.log")) 2>&1 &",
                    wait: true
                )
                Thread.sleep(forTimeInterval: 2.0)
            }
            DispatchQueue.main.async {
                NSWorkspace.shared.open(self.dashboardURL)
                self.refreshStatus()
            }
        }
    }

    @objc private func startDashboard() {
        runCommandAsync("Start Dashboard") {
            if self.isDashboardRunning() {
                return CommandResult(output: "Dashboard is already running.", exitCode: 0)
            }
            return self.runShellResult(
                "cd \(self.quoted(self.projectPath)) && nohup \(self.quoted(self.hermesPath)) dashboard --no-open > \(self.quoted(self.hermesHome + "/logs/dashboard-menubar.log")) 2>&1 &",
                wait: true
            )
        }
    }

    @objc private func stopDashboard() {
        runCommandAsync("Stop Dashboard") {
            self.runHermesResult("dashboard --stop")
        }
    }

    @objc private func startGateway() {
        runCommandAsync("Start Gateway") {
            self.runHermesResult("gateway start")
        }
    }

    @objc private func restartGateway() {
        runCommandAsync("Restart Gateway") {
            self.runHermesResult("gateway restart")
        }
    }

    @objc private func stopGateway() {
        runCommandAsync("Stop Gateway") {
            self.runHermesResult("gateway stop")
        }
    }

    @objc private func openConfig() {
        openPath(hermesHome + "/config.yaml")
    }

    @objc private func openLogs() {
        openPath(hermesHome + "/logs")
    }

    @objc private func openHermesFolder() {
        openPath(hermesHome)
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private func runCommandAsync(_ title: String, _ work: @escaping () -> CommandResult) {
        DispatchQueue.global(qos: .userInitiated).async {
            let result = work()
            DispatchQueue.main.async {
                self.refreshStatus()
                if result.exitCode != 0 {
                    let output = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
                    let message = output.isEmpty ? "Command exited with code \(result.exitCode)." : output
                    self.showMessage(title: "\(title) failed", message: self.trim(message, max: 1200))
                }
            }
        }
    }

    private func runHermesResult(_ args: String) -> CommandResult {
        runShellResult("cd \(quoted(projectPath)) && \(quoted(hermesPath)) \(args)", wait: true)
    }

    private func isGatewayRunning() -> Bool {
        let output = runShell("launchctl print gui/$(id -u)/ai.hermes.gateway 2>/dev/null | awk '/state = running|pid =/ { found=1 } END { print found ? \"yes\" : \"no\" }'", wait: true)
        return output.trimmingCharacters(in: .whitespacesAndNewlines) == "yes"
    }

    private func isDashboardRunning() -> Bool {
        let output = runShell("curl -fsS --max-time 0.6 \(dashboardURL.absoluteString) >/dev/null 2>&1 && echo yes || echo no", wait: true)
        return output.trimmingCharacters(in: .whitespacesAndNewlines) == "yes"
    }

    private func runShell(_ command: String, wait: Bool) -> String {
        runShellResult(command, wait: wait).output
    }

    private func runShellResult(_ command: String, wait: Bool) -> CommandResult {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-lc", command]
        process.standardOutput = pipe
        process.standardError = pipe
        process.environment = [
            "HOME": home,
            "HERMES_HOME": hermesHome,
            "PATH": commandPath,
        ]

        do {
            try process.run()
            if wait {
                process.waitUntilExit()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                return CommandResult(
                    output: String(data: data, encoding: .utf8) ?? "",
                    exitCode: process.terminationStatus
                )
            }
            return CommandResult(output: "", exitCode: 0)
        } catch {
            return CommandResult(
                output: "Failed to run command: \(error.localizedDescription)",
                exitCode: 1
            )
        }
    }

    private func quoted(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    private func openPath(_ path: String) {
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    private func trim(_ value: String, max: Int) -> String {
        if value.count <= max {
            return value
        }
        return String(value.suffix(max))
    }

    private func showMessage(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
