// Menu bar companion for the os3-router service.
// The router itself runs as a LaunchAgent (installed by install.sh); this app shows its
// state and offers the everyday actions. It only talks to the router's local API.
import AppKit
import ServiceManagement
import SwiftUI
import WebKit

@main
struct CodexOS3App: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = RouterModel()

    init() {
        // `CodexOS3 --snapshot out.png`: render the menu to a PNG (docs/tests), then quit
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count {
            let out = args[i + 1]
            Task { @MainActor in
                let m = RouterModel()
                try? await Task.sleep(nanoseconds: 2_500_000_000)
                let r = ImageRenderer(content: MenuContent(model: m).background(Color(nsColor: .windowBackgroundColor)))
                r.scale = 2
                if let img = r.nsImage, let tiff = img.tiffRepresentation,
                   let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) {
                    try? png.write(to: URL(fileURLWithPath: out))
                }
                exit(0)
            }
        }
    }

    var body: some Scene {
        MenuBarExtra {
            MenuContent(model: model)
        } label: {
            Image(systemName: model.symbol)
        }
        .menuBarExtraStyle(.window)
    }
}

// MARK: - State

struct Limits: Decodable { let p_pct: Double?; let p_reset: Double?; let s_pct: Double?; let s_reset: Double? }
struct Agent: Decodable { let status: String?; let running: Bool?; let version: String? }
struct Finding: Decodable { let kind: String; let level: String; let msg: String }
struct Watchdog: Decodable { let findings: [Finding]? }
struct UsageLimit: Decodable { let ts: Double; let resets: String? }
struct Status: Decodable {
    let version: String; let limits: Limits?; let agent: Agent?; let watchdog: Watchdog?
    let running: Int; let model: String; let endpoint: String; let usage_limit: UsageLimit?
    let whats_new: Bool?; let no_sleep: Bool?
}
struct Config: Decodable { let api_key: String; let port: Int; let model: String }

@MainActor
final class RouterModel: ObservableObject {
    @Published var status: Status?
    @Published var error: String?
    @Published var busy: String?
    @Published var launchAtLogin = SMAppService.mainApp.status == .enabled
    private var timer: Timer?
    private var showingWhatsNew = false

    static let home = (ProcessInfo.processInfo.environment["CODEX_OS3_HOME"]
                       ?? NSString(string: "~/.codex-os3").expandingTildeInPath)
    static let serviceLabel = "ai.codexos3.router"

    var port: Int { Self.configuredPort }
    static var configuredPort: Int {
        let url = URL(fileURLWithPath: home + "/config.json")
        if let d = try? Data(contentsOf: url),
           let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let p = j["port"] as? Int { return p }
        return 11435
    }
    var base: String { "http://127.0.0.1:\(port)" }

    init() {
        // first launch: start with the user's session, like the router service does
        if !UserDefaults.standard.bool(forKey: "didRegisterLoginItem") {
            try? SMAppService.mainApp.register()
            UserDefaults.standard.set(true, forKey: "didRegisterLoginItem")
            launchAtLogin = SMAppService.mainApp.status == .enabled
        }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    var symbol: String {
        guard let s = status else { return error == nil ? "circle.dotted" : "xmark.circle" }
        if let ul = s.usage_limit, Date().timeIntervalSince1970 - ul.ts < 3600 { return "exclamationmark.octagon" }
        if (s.watchdog?.findings ?? []).contains(where: { $0.level == "error" }) { return "exclamationmark.triangle" }
        if s.agent?.status != "connected" { return "exclamationmark.triangle" }
        return s.running > 0 ? "bolt.circle.fill" : "checkmark.circle"
    }

    func refresh() {
        Task {
            do {
                let (d, _) = try await URLSession.shared.data(from: URL(string: base + "/api/status")!)
                status = try JSONDecoder().decode(Status.self, from: d)
                error = nil
                if status?.whats_new == true && !showingWhatsNew { showingWhatsNew = true; AppWindow.shared.show() }
            } catch {
                status = nil
                self.error = "Router not running"
            }
        }
    }

    func post(_ path: String, body: [String: Any] = [:]) async throws -> [String: Any] {
        var r = URLRequest(url: URL(string: base + "/api/" + path)!)
        r.httpMethod = "POST"
        r.setValue("1", forHTTPHeaderField: "X-Codex-OS3")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try JSONSerialization.data(withJSONObject: body)
        r.timeoutInterval = 120
        let (d, response) = try await URLSession.shared.data(for: r)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return (try? JSONSerialization.jsonObject(with: d) as? [String: Any]) ?? [:]
    }

    func config() async -> Config? {
        guard let (d, _) = try? await URLSession.shared.data(from: URL(string: base + "/api/config")!) else { return nil }
        return try? JSONDecoder().decode(Config.self, from: d)
    }

    func copySetup() {
        Task {
            guard let c = await config() else { return }
            let text = """
            endpoint: http://localhost:\(c.port)/v1
            model id: \(c.model)
            api key: \(c.api_key)
            context window: 200000
            """
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            flash("Copied OS3 settings")
        }
    }

    func copyKey() {
        Task {
            guard let c = await config() else { return }
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(c.api_key, forType: .string)
            flash("Copied API key")
        }
    }

    func restartAgent() {
        busy = "Restarting rabbit-agent…"
        Task {
            let r = try? await post("agent/restart")
            busy = nil
            flash((r?["message"] as? String) ?? "Restart requested")
            refresh()
        }
    }

    func reload() {
        Task { _ = try? await post("reload"); flash("Router reload requested"); refresh() }
    }

    func openDashboard(_ tab: String = "") {
        NSWorkspace.shared.open(URL(string: "http://localhost:\(port)/" + (tab.isEmpty ? "" : "#" + tab))!)
    }

    /// Start/stop the LaunchAgent through launchd, so the service keeps the proper
    /// context (processes started any other way can lose macOS permissions).
    func service(start: Bool) {
        let uid = getuid()
        let plist = NSString(string: "~/Library/LaunchAgents/\(Self.serviceLabel).plist").expandingTildeInPath
        let args = start ? ["bootstrap", "gui/\(uid)", plist] : ["bootout", "gui/\(uid)/\(Self.serviceLabel)"]
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = args
        try? p.run()
        p.waitUntilExit()
        if start {  // already loaded: make sure it runs
            let k = Process()
            k.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            k.arguments = ["kickstart", "gui/\(uid)/\(Self.serviceLabel)"]
            try? k.run()
            k.waitUntilExit()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.refresh() }
    }

    func toggleLaunchAtLogin() {
        do {
            if launchAtLogin { try SMAppService.mainApp.unregister() } else { try SMAppService.mainApp.register() }
        } catch { flash("Could not change login item: \(error.localizedDescription)") }
        launchAtLogin = SMAppService.mainApp.status == .enabled
    }

    func setPreventIdleSleep(_ enabled: Bool) {
        Task {
            do {
                let result = try await post("config", body: ["no_sleep": enabled])
                guard result["ok"] as? Bool == true else { throw URLError(.badServerResponse) }
                refresh()
            } catch { flash("Could not change sleep setting") }
        }
    }

    @Published var note: String?
    func flash(_ s: String) {
        note = s
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { if self.note == s { self.note = nil } }
    }
}

// MARK: - UI

struct Meter: View {
    let title: String; let pct: Double?; let reset: Double?
    var color: Color { guard let p = pct else { return .secondary }; return p >= 90 ? .red : p >= 70 ? .orange : .accentColor }
    var resetText: String {
        guard let r = reset else { return "" }
        let s = r - Date().timeIntervalSince1970
        if s <= 0 { return "resetting" }
        let h = Int(s) / 3600, m = (Int(s) % 3600) / 60
        return "resets in " + (h > 0 ? "\(h) h " : "") + "\(m) min"
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(title).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text(pct.map { "\(Int($0.rounded()))%" } ?? "–").font(.caption.monospacedDigit().weight(.semibold))
            }
            GeometryReader { g in  // drawn by hand: same look as the web dashboard's meters
                ZStack(alignment: .leading) {
                    Capsule().fill(color.opacity(0.18))
                    Capsule().fill(color).frame(width: g.size.width * min(1, max(0, (pct ?? 0) / 100)))
                }
            }.frame(height: 7)
            Text(resetText).font(.caption2).foregroundStyle(.tertiary)
        }
    }
}

struct MenuContent: View {
    @ObservedObject var model: RouterModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("OS3 Router").font(.headline)
                Spacer()
                if let v = model.status?.version { Text("v\(v)").font(.caption).foregroundStyle(.secondary) }
            }
            if let s = model.status {
                statusLine(s)
                Meter(title: "5-hour window", pct: s.limits?.p_pct, reset: s.limits?.p_reset)
                Meter(title: "Weekly limit", pct: s.limits?.s_pct, reset: s.limits?.s_reset)
                ForEach(Array((s.watchdog?.findings ?? []).prefix(3).enumerated()), id: \.offset) { _, f in
                    Label(f.msg, systemImage: f.level == "error" ? "xmark.octagon" : "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(f.level == "error" ? .red : .orange).lineLimit(3)
                }
            } else {
                Label(model.error ?? "Connecting…", systemImage: "xmark.circle").foregroundStyle(.red)
                Button("Start router service") { model.service(start: true) }
            }
            if let b = model.busy { ProgressView(b).controlSize(.small) }
            if let n = model.note { Text(n).font(.caption).foregroundStyle(.secondary) }
            Divider()
            Button("Open OS3 Router") { AppWindow.shared.show() }.keyboardShortcut("o")
            Divider()
            Group {
                Button("Open web dashboard") { model.openDashboard() }
                Button("Copy OS3 settings") { model.copySetup() }
                Button("Copy API key") { model.copyKey() }
                Button("Restart rabbit-agent") { model.restartAgent() }.disabled(model.status == nil || model.busy != nil)
                Button("Reload router (no downtime)") { model.reload() }.disabled(model.status == nil)
            }.buttonStyle(.plain)
            Divider()
            Toggle("Open at login", isOn: Binding(get: { model.launchAtLogin }, set: { _ in model.toggleLaunchAtLogin() }))
                .toggleStyle(.checkbox)
            Toggle("Prevent idle sleep", isOn: Binding(
                get: { model.status?.no_sleep ?? false }, set: { model.setPreventIdleSleep($0) }))
                .toggleStyle(.checkbox).disabled(model.status == nil)
                .help("Keeps this Mac awake while the router runs; the display can still turn off.")
            HStack {
                Button("Stop router") { model.service(start: false) }.disabled(model.status == nil)
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }
            }
        }
        .padding(14)
        .frame(width: 290)
        .onAppear { model.refresh() }
    }

    @ViewBuilder func statusLine(_ s: Status) -> some View {
        let agentOK = s.agent?.status == "connected" && (s.agent?.running ?? false)
        VStack(alignment: .leading, spacing: 2) {
            Label(agentOK ? "rabbit-agent connected" : "rabbit-agent: \(s.agent?.status ?? "not found")",
                  systemImage: agentOK ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(agentOK ? .green : .red)
            Text(s.running > 0 ? "Working on \(s.running) request(s) · \(s.model)" : "Idle · \(s.model)")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}


// MARK: - App window

/// Opened from Launchpad/Finder (not at login): show the app window.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ n: Notification) {
        let atLogin = ProcessInfo.processInfo.systemUptime < 180
        if !atLogin && !CommandLine.arguments.contains("--snapshot") { AppWindow.shared.show() }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        AppWindow.shared.show()
        return true
    }
}

/// The main window: the router's own app page (/app) in a native window. The page is served
/// by the router, so it updates together with the router.
final class AppWindow: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    static let shared = AppWindow()
    private var window: NSWindow?
    private var web: WKWebView?
    private var retry: Timer?

    var url: URL {
        URL(string: "http://127.0.0.1:\(RouterModel.configuredPort)/app?native=1")!
    }

    func show() {
        if window == nil { build() }
        NSApp.setActivationPolicy(.regular)  // Dock icon while the window is open
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func build() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                         backing: .buffered, defer: false)
        w.titlebarAppearsTransparent = true
        w.titleVisibility = .hidden
        w.title = "OS3 Router"
        w.minSize = NSSize(width: 780, height: 560)
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.center()
        w.setFrameAutosaveName("OS3RouterMain")
        let v = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        v.navigationDelegate = self
        v.uiDelegate = self
        v.setValue(false, forKey: "drawsBackground")  // no white flash before the page paints
        let drag = DragStrip()  // the page covers the title bar: this strip moves the window
        let root = NSView()
        for sub in [v, drag] as [NSView] { sub.translatesAutoresizingMaskIntoConstraints = false; root.addSubview(sub) }
        NSLayoutConstraint.activate([
            v.topAnchor.constraint(equalTo: root.topAnchor), v.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            v.leadingAnchor.constraint(equalTo: root.leadingAnchor), v.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            drag.topAnchor.constraint(equalTo: root.topAnchor), drag.heightAnchor.constraint(equalToConstant: 34),
            drag.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 80), drag.trailingAnchor.constraint(equalTo: root.trailingAnchor),
        ])
        w.contentView = root
        window = w
        web = v
        load()
    }

    func load() { web?.load(URLRequest(url: url)) }

    func windowWillClose(_ n: Notification) { NSApp.setActivationPolicy(.accessory) }

    // router not answering (stopped, restarting): a calm page, then try again
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        w.loadHTMLString("""
        <html><body style="font:15px -apple-system;display:grid;place-items:center;height:92vh;margin:0;color:#8a8a92;background:transparent">
        <div style="text-align:center"><div style="font-size:22px;font-weight:700;color:#999">Starting the router…</div>
        <p>If this stays, use the menu bar icon → Start router service.</p></div></body></html>
        """, baseURL: nil)
        retry?.invalidate()
        retry = Timer.scheduledTimer(withTimeInterval: 3, repeats: false) { [weak self] _ in self?.load() }
    }

    // only the router's own app page loads in this window; anything else (web dashboard, GitHub)
    // opens in the normal browser, so no other page can ever pose as the app
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let u = a.request.url else { return decisionHandler(.cancel) }
        let ours = u.host == "127.0.0.1" && u.port == RouterModel.configuredPort && (u.path.hasPrefix("/app") || u.path.hasPrefix("/api/") || u.path.hasPrefix("/guide/"))
        if ours || u.scheme == "about" { return decisionHandler(.allow) }
        if ["http", "https"].contains(u.scheme ?? "") && a.navigationType == .linkActivated { NSWorkspace.shared.open(u) }
        decisionHandler(.cancel)
    }

    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction, windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let u = a.request.url { NSWorkspace.shared.open(u) }
        return nil
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = m
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = m
        a.runModal()
        completionHandler()
    }
}

final class DragStrip: NSView {
    override func mouseDown(with e: NSEvent) {
        if e.clickCount == 2 { window?.performZoom(nil) } else { window?.performDrag(with: e) }
    }
}
