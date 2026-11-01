import SwiftUI

@main
struct HomeTempCollectorApp: App {
    @StateObject private var collector = Collector()

    var body: some Scene {
        WindowGroup {
            ContentView(collector: collector, uploader: collector.uploader)
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
