import SwiftUI

/// The collector is created at launch, not by the window: when launchd starts the binary directly
/// (LaunchAgent, no LaunchServices), no scene ever connects, so anything owned by a scene never runs.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        _ = Collector.shared
        return true
    }
}

@main
struct HomeTempCollectorApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @ObservedObject private var collector = Collector.shared

    var body: some Scene {
        WindowGroup {
            ContentView(collector: collector, uploader: collector.uploader)
                .onAppear(perform: closeIfBackground)
        }
    }
}

/// Launched by the LaunchAgent with `--background`: close the launch window and keep running windowless.
/// (Multiple-scene support keeps a Catalyst app alive with no windows; verified in falltemp.1.)
/// Open the app again (e.g. `open -a HomeTempCollector`) to get the status window back.
private var closedLaunchWindow = false
private func closeIfBackground() {
    guard !closedLaunchWindow, CommandLine.arguments.contains("--background") else { return }
    closedLaunchWindow = true
    DispatchQueue.main.async {
        for session in UIApplication.shared.openSessions {
            UIApplication.shared.requestSceneSessionDestruction(session, options: nil)
        }
    }
}

struct ContentView: View {
    @ObservedObject var collector: Collector
    @ObservedObject var uploader: Uploader

    private var sortedRows: [Collector.Row] {
        collector.rows.values.sorted { ($0.room, $0.accessory, $0.kind) < ($1.room, $1.accessory, $1.kind) }
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(collector.status).font(.footnote).textSelection(.enabled)
                    LabeledContent("Events", value: "\(collector.totalEvents)")
                    LabeledContent("Last heartbeat",
                                   value: collector.lastHeartbeat?.formatted(date: .omitted, time: .standard) ?? "—")
                    LabeledContent("Last upload",
                                   value: uploader.lastSuccess?.formatted(date: .omitted, time: .standard) ?? "—")
                    if let error = uploader.lastError {
                        Text("Upload problem: \(error)").font(.footnote).foregroundStyle(.red)
                    }
                }
                Section("Characteristics") {
                    ForEach(sortedRows) { row in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack {
                                Text(row.accessory).bold()
                                Text(row.room).foregroundStyle(.secondary)
                                Spacer()
                                Text(row.value).monospacedDigit()
                            }
                            HStack {
                                Text(row.kind)
                                Spacer()
                                Text("\(row.events) events")
                                if let last = row.lastEvent {
                                    Text(last, style: .relative)
                                }
                            }
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("HomeTemp Collector")
        }
    }
}
