import Foundation
import HomeKit
import os
import UIKit

/// Spike collector (falltemp.1): discovers every HomeKit accessory, subscribes to the
/// characteristics we care about, and appends every event plus a periodic heartbeat to
/// JSONL files in ~/Library/Application Support/HomeTemp/ (Catalyst, unsandboxed).
final class Collector: NSObject, ObservableObject, HMHomeManagerDelegate, HMAccessoryDelegate, HMHomeDelegate {
    struct Row: Identifiable {
        let id: String          // characteristic uniqueIdentifier
        let room: String
        let accessory: String
        let kind: String
        var value: String
        var events: Int
        var lastEvent: Date?
    }

    @Published var status = "Starting…"
    @Published var rows: [String: Row] = [:]
    @Published var totalEvents = 0
    @Published var lastHeartbeat: Date?

    static let heartbeatInterval: TimeInterval = 300

    /// Characteristic types worth recording, with short names for logs.
    static let interesting: [String: String] = [
        HMCharacteristicTypeCurrentTemperature: "currentTemperature",
        HMCharacteristicTypeTargetTemperature: "targetTemperature",
        HMCharacteristicTypeCurrentHeatingCooling: "currentHeatingCooling",
        HMCharacteristicTypeTargetHeatingCooling: "targetHeatingCooling",
        HMCharacteristicTypeHeatingThreshold: "heatingThreshold",
        HMCharacteristicTypeCoolingThreshold: "coolingThreshold",
        HMCharacteristicTypeCurrentRelativeHumidity: "currentRelativeHumidity",
        HMCharacteristicTypeTargetRelativeHumidity: "targetRelativeHumidity",
        HMCharacteristicTypeTemperatureUnits: "temperatureUnits",
        HMCharacteristicTypeBatteryLevel: "batteryLevel",
        HMCharacteristicTypeStatusLowBattery: "statusLowBattery",
        HMCharacteristicTypeCurrentFanState: "currentFanState",
        HMCharacteristicTypeActive: "active",
        // Matter thermostats (e.g. Resideo X2S) surface as a HeaterCooler service, not Thermostat.
        // CurrentHeaterCoolerState: 0 inactive, 1 idle, 2 heating, 3 cooling — the run-time signal.
        HMCharacteristicTypeCurrentHeaterCoolerState: "currentHeaterCoolerState",
        // TargetHeaterCoolerState: 0 auto, 1 heat, 2 cool.
        HMCharacteristicTypeTargetHeaterCoolerState: "targetHeaterCoolerState",
        // Air quality sensors: CO2 is a strong occupancy signal for the office.
        HMCharacteristicTypeCarbonDioxideLevel: "carbonDioxideLevel",
        HMCharacteristicTypeAirQuality: "airQuality",
        HMCharacteristicTypePM2_5Density: "pm25Density",
        HMCharacteristicTypeVolatileOrganicCompoundDensity: "vocDensity",
    ]

    private let manager = HMHomeManager()
    private let log = Logger(subsystem: "ca.fallday.hometemp.collector", category: "collector")
    private let writer = JSONLWriter()
    private var heartbeat: Timer?
    private var activity: NSObjectProtocol?

    override init() {
        super.init()
        // Keep App Nap from throttling us while the window is hidden; still allow system sleep.
        activity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiatedAllowingIdleSystemSleep, .latencyCritical],
            reason: "Recording HomeKit sensor events")
        manager.delegate = self
        status = "Waiting for HomeKit… (log: \(writer.directory.path))"
        record(["type": "launch"])
        // Log app/scene lifecycle so the spike report can tell whether events keep flowing
        // while the window is minimized, hidden (Cmd-H) or closed (Cmd-W).
        let lifecycle: [(Notification.Name, String)] = [
            (UIApplication.didEnterBackgroundNotification, "didEnterBackground"),
            (UIApplication.willEnterForegroundNotification, "willEnterForeground"),
            (UIApplication.didBecomeActiveNotification, "didBecomeActive"),
            (UIApplication.willResignActiveNotification, "willResignActive"),
            (UIApplication.willTerminateNotification, "willTerminate"),
            (UIScene.didDisconnectNotification, "sceneDidDisconnect"),
            (UIScene.didActivateNotification, "sceneDidActivate"),
        ]
        for (name, label) in lifecycle {
            NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                self?.record(["type": "lifecycle", "event": label])
            }
        }
    }

    // MARK: HMHomeManagerDelegate

    func homeManager(_ manager: HMHomeManager, didUpdate status: HMHomeManagerAuthorizationStatus) {
        record(["type": "authorization", "status": Int(status.rawValue)])
    }

    func homeManagerDidUpdateHomes(_ manager: HMHomeManager) {
        record(["type": "homes", "count": manager.homes.count])
        for home in manager.homes {
            home.delegate = self
            dumpInventory(home)
            for accessory in home.accessories { attach(accessory, in: home) }
        }
        status = "\(manager.homes.count) home(s), \(rows.count) characteristics. Log: \(writer.directory.path)"
        startHeartbeat()
    }

    // MARK: HMHomeDelegate (topology changes)

    func home(_ home: HMHome, didAdd accessory: HMAccessory) {
        record(["type": "accessoryAdded", "accessory": accessory.name, "uuid": accessory.uniqueIdentifier.uuidString])
        dumpInventory(home)
        attach(accessory, in: home)
    }

    func home(_ home: HMHome, didRemove accessory: HMAccessory) {
        record(["type": "accessoryRemoved", "accessory": accessory.name, "uuid": accessory.uniqueIdentifier.uuidString])
    }

    func home(_ home: HMHome, didUpdate room: HMRoom, for accessory: HMAccessory) {
        record(["type": "accessoryRoomChanged", "accessory": accessory.name, "room": room.name,
                "uuid": accessory.uniqueIdentifier.uuidString])
    }

    // MARK: HMAccessoryDelegate

    func accessory(_ accessory: HMAccessory, service: HMService, didUpdateValueFor characteristic: HMCharacteristic) {
        recordValue(characteristic, of: accessory, source: "event")
    }

    func accessoryDidUpdateReachability(_ accessory: HMAccessory) {
        record(["type": "reachability", "accessory": accessory.name,
                "uuid": accessory.uniqueIdentifier.uuidString, "reachable": accessory.isReachable])
    }

    // MARK: Internals

    private func attach(_ accessory: HMAccessory, in home: HMHome) {
        accessory.delegate = self
        for service in accessory.services {
            for c in service.characteristics where Self.interesting[c.characteristicType] != nil {
                let id = c.uniqueIdentifier.uuidString
                rows[id] = rows[id] ?? Row(id: id, room: accessory.room?.name ?? "—", accessory: accessory.name,
                                           kind: Self.interesting[c.characteristicType]!, value: "…", events: 0)
                c.readValue { [weak self] error in
                    DispatchQueue.main.async {
                        if let error { self?.recordError("read", c, accessory, error) }
                        else { self?.recordValue(c, of: accessory, source: "initial") }
                    }
                }
                if c.properties.contains(HMCharacteristicPropertySupportsEventNotification) {
                    c.enableNotification(true) { [weak self] error in
                        DispatchQueue.main.async {
                            if let error { self?.recordError("enableNotification", c, accessory, error) }
                        }
                    }
                }
            }
        }
    }

    private func startHeartbeat() {
        guard heartbeat == nil else { return }
        heartbeat = Timer.scheduledTimer(withTimeInterval: Self.heartbeatInterval, repeats: true) { [weak self] _ in
            self?.beat()
        }
    }

    /// Snapshot of last-known (cached) values; lets the server tell "no change" from "collector down".
    private func beat() {
        lastHeartbeat = Date()
        for home in manager.homes {
            for accessory in home.accessories {
                for service in accessory.services {
                    for c in service.characteristics where Self.interesting[c.characteristicType] != nil {
                        recordValue(c, of: accessory, source: "heartbeat")
                    }
                }
            }
        }
    }

    private func recordValue(_ c: HMCharacteristic, of accessory: HMAccessory, source: String) {
        let kind = Self.interesting[c.characteristicType] ?? c.characteristicType
        var entry = base(c, accessory)
        entry["type"] = "value"
        entry["source"] = source
        entry["reachable"] = accessory.isReachable
        entry["value"] = jsonValue(c.value)
        record(entry)

        let id = c.uniqueIdentifier.uuidString
        var row = rows[id] ?? Row(id: id, room: accessory.room?.name ?? "—", accessory: accessory.name,
                                  kind: kind, value: "", events: 0)
        row.value = c.value.map { "\($0)" } ?? "nil"
        if source == "event" {
            row.events += 1
            row.lastEvent = Date()
            totalEvents += 1
        }
        rows[id] = row
    }

    private func recordError(_ op: String, _ c: HMCharacteristic, _ accessory: HMAccessory, _ error: Error) {
        var entry = base(c, accessory)
        entry["type"] = "error"
        entry["op"] = op
        entry["error"] = error.localizedDescription
        record(entry)
        log.error("\(op) failed for \(accessory.name): \(error.localizedDescription)")
    }

    private func base(_ c: HMCharacteristic, _ accessory: HMAccessory) -> [String: Any] {
        [
            "accessory": accessory.name,
            "accessoryUUID": accessory.uniqueIdentifier.uuidString,
            "room": accessory.room?.name ?? "",
            "service": c.service?.serviceType ?? "",
            "serviceName": c.service?.name ?? "",
            "characteristic": Self.interesting[c.characteristicType] ?? c.characteristicType,
            "characteristicUUID": c.uniqueIdentifier.uuidString,
            "unit": c.metadata?.units ?? "",
        ]
    }

    private func dumpInventory(_ home: HMHome) {
        let accessories: [[String: Any]] = home.accessories.map { a in
            [
                "name": a.name,
                "uuid": a.uniqueIdentifier.uuidString,
                "room": a.room?.name ?? "",
                "manufacturer": a.manufacturer ?? "",
                "model": a.model ?? "",
                "firmware": a.firmwareVersion ?? "",
                "bridged": a.isBridged,
                "reachable": a.isReachable,
                "category": a.category.categoryType,
                "services": a.services.map { s in
                    [
                        "name": s.name,
                        "type": s.serviceType,
                        "characteristics": s.characteristics.map { c in
                            [
                                "type": Self.interesting[c.characteristicType] ?? c.characteristicType,
                                "description": c.localizedDescription,
                                "notifies": c.properties.contains(HMCharacteristicPropertySupportsEventNotification),
                                "unit": c.metadata?.units ?? "",
                            ] as [String: Any]
                        },
                    ] as [String: Any]
                },
            ]
        }
        record(["type": "inventory", "home": home.name, "rooms": home.rooms.map(\.name),
                "zones": home.zones.map { ["name": $0.name, "rooms": $0.rooms.map(\.name)] },
                "accessories": accessories])
    }

    private func jsonValue(_ v: Any?) -> Any {
        switch v {
        // HomeKit sometimes hands back enum states as CFBoolean (e.g. CurrentHeaterCoolerState
        // arrived as `false` instead of 0); normalize so every state is numeric.
        case let n as NSNumber where CFGetTypeID(n) == CFBooleanGetTypeID(): return n.boolValue ? 1 : 0
        case let n as NSNumber: return n
        case let s as String: return s
        case nil: return NSNull()
        default: return "\(v!)"
        }
    }

    private func record(_ entry: [String: Any]) {
        var e = entry
        e["ts"] = ISO8601DateFormatter.fractional.string(from: Date())
        writer.append(e)
    }
}

final class JSONLWriter {
    let directory: URL

    init() {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        directory = base.appendingPathComponent("HomeTemp", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    func append(_ entry: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(entry),
              var data = try? JSONSerialization.data(withJSONObject: entry, options: [.sortedKeys]) else { return }
        data.append(0x0A)
        let day = ISO8601DateFormatter.day.string(from: Date())
        let url = directory.appendingPathComponent("events-\(day).jsonl")
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: url)
        }
    }
}

extension ISO8601DateFormatter {
    static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static let day: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withFullDate]
        f.timeZone = .current
        return f
    }()
}
