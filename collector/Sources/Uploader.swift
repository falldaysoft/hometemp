import Foundation
import os

/// Uploads the JSONL log to the hometemp server (falltemp.3.3).
///
/// The daily `events-*.jsonl` files are the buffer: a per-file byte cursor in `upload-state.json` records
/// how far the server has acknowledged. Only complete lines are sent, so a half-written line is retried on
/// the next pass. The server is idempotent, so a crash between POST and cursor save just resends a batch.
final class Uploader: ObservableObject {
    @Published var lastSuccess: Date?
    @Published var lastError: String?
    @Published var pendingBytes: Int = 0

    static let interval: TimeInterval = 60
    static let maxBatchLines = 2000

    private let log = Logger(subsystem: "ca.fallday.hometemp.collector", category: "uploader")
    private var directory: URL!
    private var task: Task<Void, Never>?
    private var cursors: [String: UInt64] = [:]

    /// Server URL; override with `defaults write ca.fallday.hometemp.collector serverURL https://…`.
    private var serverURL: URL {
        URL(string: UserDefaults.standard.string(forKey: "serverURL") ?? "https://hometemp.fallday.ca")!
    }

    private var stateURL: URL { directory.appendingPathComponent("upload-state.json") }
    private var tokenURL: URL { directory.appendingPathComponent("token") }

    func start(directory: URL) {
        guard task == nil else { return }
        self.directory = directory
        if let data = try? Data(contentsOf: stateURL),
           let saved = try? JSONDecoder().decode([String: UInt64].self, from: data) {
            cursors = saved
        }
        task = Task { [weak self] in
            while !Task.isCancelled {
                await self?.pass()
                try? await Task.sleep(nanoseconds: UInt64(Self.interval * 1_000_000_000))
            }
        }
    }

    private func pass() async {
        guard let token = try? String(contentsOf: tokenURL, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines), !token.isEmpty else {
            await publish(error: "No upload token at \(tokenURL.path)")
            return
        }
        let files = ((try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.lastPathComponent.hasPrefix("events-") && $0.pathExtension == "jsonl" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }

        var pending = 0
        for file in files {
            let name = file.lastPathComponent
            while true {
                guard let handle = try? FileHandle(forReadingFrom: file) else { break }
                let start = cursors[name] ?? 0
                let size = (try? handle.seekToEnd()) ?? 0
                guard size > start else { try? handle.close(); break }
                try? handle.seek(toOffset: start)
                let chunk = (try? handle.read(upToCount: 8 * 1024 * 1024)) ?? Data()
                try? handle.close()

                // Complete lines only, at most maxBatchLines of them.
                var lines: [Data] = []
                var consumed = 0
                var lineStart = chunk.startIndex
                for i in chunk.indices where chunk[i] == 0x0A {
                    lines.append(chunk[lineStart..<i])
                    consumed = i - chunk.startIndex + 1
                    lineStart = i + 1
                    if lines.count >= Self.maxBatchLines { break }
                }
                if lines.isEmpty { pending += Int(size - start); break }

                var body = Data("{\"records\":[".utf8)
                for (i, line) in lines.enumerated() {
                    if i > 0 { body.append(0x2C) }
                    body.append(line)
                }
                body.append(contentsOf: Array("]}".utf8))

                var req = URLRequest(url: serverURL.appendingPathComponent("api/ingest"))
                req.httpMethod = "POST"
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                req.httpBody = body
                req.timeoutInterval = 60
                do {
                    let (data, response) = try await URLSession.shared.data(for: req)
                    let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                    guard status == 200 else {
                        let msg = String(data: data.prefix(200), encoding: .utf8) ?? ""
                        await publish(error: "Server said \(status): \(msg)")
                        log.error("upload failed \(status): \(msg)")
                        return
                    }
                } catch {
                    await publish(error: error.localizedDescription)
                    return
                }
                cursors[name] = start + UInt64(consumed)
                saveState()
                await publish(success: Date())
            }
        }
        let total = pending
        await MainActor.run { self.pendingBytes = total }
    }

    private func saveState() {
        if let data = try? JSONEncoder().encode(cursors) {
            try? data.write(to: stateURL, options: .atomic)
        }
    }

    @MainActor private func publish(success: Date? = nil, error: String? = nil) {
        if let success { lastSuccess = success; lastError = nil }
        if let error { lastError = error }
    }
}
