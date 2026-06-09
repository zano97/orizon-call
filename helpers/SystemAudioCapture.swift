// SystemAudioCapture.swift
//
// Captures macOS system audio via ScreenCaptureKit and writes raw
// interleaved Float32 stereo PCM @ 48 kHz to stdout. Status messages
// go to stderr. Exits cleanly on SIGINT / SIGTERM or when stdout closes.
//
// Build:
//   swiftc -O -o system_audio_capture SystemAudioCapture.swift \
//          -framework ScreenCaptureKit -framework CoreMedia \
//          -framework AVFoundation -framework CoreGraphics
//
// Requires: macOS 13.0+ and "Screen Recording" permission.

import Foundation
import ScreenCaptureKit
import CoreMedia
import AVFoundation

let TARGET_SAMPLE_RATE: Double = 48_000
let TARGET_CHANNELS: AVAudioChannelCount = 2

// MARK: - stderr helpers

func logErr(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8) ?? Data())
}

// Write a structured status line to stderr that Python can parse.
// Format: "STATUS <key> <value>"
func status(_ key: String, _ value: String) {
    logErr("STATUS \(key) \(value)")
}

// MARK: - stdout pipe

let stdoutHandle = FileHandle.standardOutput

func writePCM(_ data: Data) -> Bool {
    do {
        try stdoutHandle.write(contentsOf: data)
        return true
    } catch {
        // stdout closed (Python parent went away). Signal shutdown.
        return false
    }
}

// MARK: - Capture delegate

final class AudioCaptureOutput: NSObject, SCStreamOutput, SCStreamDelegate {

    private let shutdown: () -> Void
    private var stdoutAlive = true
    private var formatLogged = false

    // Diagnostics
    private var audioBuffersReceived: UInt64 = 0
    private var bytesEmitted: UInt64 = 0
    private var maxPeak: Float = 0.0
    private var dropReasons: [String: UInt64] = [:]
    private var lastReportAt = Date()
    private let reportInterval: TimeInterval = 5.0

    private func drop(_ reason: String) {
        dropReasons[reason, default: 0] &+= 1
    }

    init(shutdown: @escaping () -> Void) {
        self.shutdown = shutdown
        super.init()
    }

    func stream(_ stream: SCStream,
                didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        // We register the delegate for both .audio and .screen. Discard video.
        guard type == .audio else { return }
        guard stdoutAlive else { return }
        guard sampleBuffer.isValid else { drop("invalid_buffer"); emitHeartbeatIfDue(); return }
        guard sampleBuffer.dataReadiness == .ready else { drop("not_ready"); emitHeartbeatIfDue(); return }

        audioBuffersReceived &+= 1

        guard let data = extractInterleavedAudio(from: sampleBuffer) else {
            drop("extract_failed"); emitHeartbeatIfDue(); return
        }

        // Track signal peak from the bytes we're actually emitting.
        data.withUnsafeBytes { raw in
            let count = data.count / MemoryLayout<Float>.size
            let buf = raw.bindMemory(to: Float.self)
            var localMax: Float = 0
            for i in 0..<count {
                let v = abs(buf[i])
                if v > localMax { localMax = v }
            }
            if localMax > maxPeak { maxPeak = localMax }
        }

        if !writePCM(data) {
            stdoutAlive = false
            shutdown()
            return
        }
        bytesEmitted &+= UInt64(data.count)
        emitHeartbeatIfDue()
    }

    private func emitHeartbeatIfDue() {
        let now = Date()
        if now.timeIntervalSince(lastReportAt) < reportInterval { return }
        lastReportAt = now
        var dropSummary = ""
        if !dropReasons.isEmpty {
            let parts = dropReasons.map { "\($0.key)=\($0.value)" }.sorted()
            dropSummary = " drops=" + parts.joined(separator: ",")
        }
        status("heartbeat",
               "buffers=\(audioBuffersReceived) bytes=\(bytesEmitted) peak=\(String(format: "%.4f", maxPeak))\(dropSummary)")
        maxPeak = 0.0
        dropReasons.removeAll(keepingCapacity: true)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        status("error", "stream_stopped: \(error.localizedDescription)")
        shutdown()
    }

    // MARK: Buffer extraction

    // CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer error codes
    // we want to report once on first occurrence (avoids spamming stderr).
    private var loggedMakeStatuses = Set<OSStatus>()

    /// Read PCM directly from the CMSampleBuffer's AudioBufferList,
    /// interleave the channels if needed, and return Data ready to ship
    /// to stdout. Bypasses AVAudioPCMBuffer + AVAudioConverter, which
    /// were silently losing data on macOS 26 (the buffer copy succeeded
    /// but the converter read from a different memory region than the
    /// one CoreMedia wrote into).
    private func extractInterleavedAudio(from sampleBuffer: CMSampleBuffer) -> Data? {
        guard let fmtDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fmtDesc) else {
            return nil
        }
        let asbd = asbdPtr.pointee
        let frameCount = Int(CMSampleBufferGetNumSamples(sampleBuffer))
        let channelCount = max(1, Int(asbd.mChannelsPerFrame))
        let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        let isNonInterleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0

        guard frameCount > 0 else { return nil }
        guard isFloat, asbd.mBitsPerChannel == 32 else {
            // We only handle Float32 here. Log once if SCK ever gives us
            // something else so we know to add a conversion path.
            if !loggedMakeStatuses.contains(-99) {
                loggedMakeStatuses.insert(-99)
                status("warn",
                       "unsupported_format float=\(isFloat) bits=\(asbd.mBitsPerChannel)")
            }
            return nil
        }
        // Log the resolved source format the first time through.
        if !formatLogged {
            formatLogged = true
            status("decoded_format",
                   "sr=\(Int(asbd.mSampleRate)) ch=\(channelCount) "
                 + "interleaved=\(!isNonInterleaved) frames=\(frameCount)")
        }

        // Allocate an AudioBufferList large enough for `channelCount` buffers
        // (when non-interleaved) or 1 buffer (when interleaved).
        let buffersInList = isNonInterleaved ? channelCount : 1
        let ablByteSize = MemoryLayout<AudioBufferList>.size
            + max(0, buffersInList - 1) * MemoryLayout<AudioBuffer>.size

        // Raw byte storage for the ABL; bind to AudioBufferList for the call.
        let rawABL = UnsafeMutableRawPointer.allocate(byteCount: ablByteSize,
                                                       alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { rawABL.deallocate() }
        let ablPtr = rawABL.bindMemory(to: AudioBufferList.self, capacity: 1)

        var blockBuffer: CMBlockBuffer?
        let st = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: ablPtr,
            bufferListSize: ablByteSize,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer
        )
        guard st == noErr, blockBuffer != nil else {
            if !loggedMakeStatuses.contains(st) {
                loggedMakeStatuses.insert(st)
                status("warn", "ABL_extract OSStatus=\(st)")
            }
            return nil
        }

        let bufList = UnsafeMutableAudioBufferListPointer(ablPtr)

        // Build the interleaved Float32 stereo output we promise to stdout.
        let outChannels = Int(TARGET_CHANNELS)
        var out = [Float](repeating: 0, count: frameCount * outChannels)

        if isNonInterleaved {
            // bufList[ch].mData holds frameCount Float32s for channel `ch`.
            let copyChannels = min(channelCount, bufList.count, outChannels)
            for ch in 0..<copyChannels {
                let ab = bufList[ch]
                guard let mData = ab.mData else { continue }
                // Trust the buffer's own byte size, not the header's frame
                // count — a mismatch must clamp, never read out of bounds.
                let availFrames = min(frameCount,
                                      Int(ab.mDataByteSize) / MemoryLayout<Float>.size)
                let chPtr = mData.bindMemory(to: Float.self, capacity: availFrames)
                for f in 0..<availFrames {
                    out[f * outChannels + ch] = chPtr[f]
                }
            }
            // Mono source → duplicate channel 0 onto channel 1.
            if copyChannels == 1 && outChannels >= 2 {
                for f in 0..<frameCount {
                    out[f * outChannels + 1] = out[f * outChannels + 0]
                }
            }
        } else {
            // Interleaved: bufList[0].mData holds frameCount * channelCount Float32s.
            guard let mData = bufList[0].mData else { return nil }
            let availFrames = min(frameCount,
                                  Int(bufList[0].mDataByteSize)
                                      / (MemoryLayout<Float>.size * channelCount))
            let srcPtr = mData.bindMemory(to: Float.self,
                                          capacity: availFrames * channelCount)
            let copyChannels = min(channelCount, outChannels)
            for f in 0..<availFrames {
                for ch in 0..<copyChannels {
                    out[f * outChannels + ch] = srcPtr[f * channelCount + ch]
                }
            }
            if channelCount == 1 && outChannels >= 2 {
                for f in 0..<availFrames {
                    out[f * outChannels + 1] = out[f * outChannels + 0]
                }
            }
        }

        return out.withUnsafeBufferPointer { Data(buffer: $0) }
    }

}

// MARK: - Stream lifecycle

@available(macOS 13.0, *)
final class Capturer {

    private var stream: SCStream?
    private var output: AudioCaptureOutput?
    private let outputQueue = DispatchQueue(label: "orizon.audio.output")
    // Signal handlers must NOT run on the main queue: the main thread
    // blocks on stopSemaphore.wait() below and never services the main
    // queue, so handlers scheduled there would never fire and every stop
    // would escalate to SIGKILL on the Python side.
    private let signalQueue = DispatchQueue(label: "orizon.signals")
    private let stopSemaphore = DispatchSemaphore(value: 0)
    private var signalSources: [DispatchSourceSignal] = []

    func run() {
        Task {
            do {
                try await start()
            } catch {
                status("error", "start_failed: \(error.localizedDescription)")
                self.stopSemaphore.signal()
            }
        }

        // Install signal handlers so SIGINT/SIGTERM trigger a clean stop.
        for sig in [SIGINT, SIGTERM] {
            let source = DispatchSource.makeSignalSource(signal: sig, queue: signalQueue)
            source.setEventHandler { [weak self] in self?.requestShutdown() }
            source.resume()
            signalSources.append(source)
            signal(sig, SIG_IGN)
        }
        signal(SIGPIPE, SIG_IGN)

        // Block until shutdown requested (signal, stream error, or stdout
        // closed), then stop the capture and wait for it to finish so the
        // process exits cleanly instead of being killed mid-teardown.
        stopSemaphore.wait()
        let stopDone = DispatchSemaphore(value: 0)
        Task {
            await self.stop()
            stopDone.signal()
        }
        _ = stopDone.wait(timeout: .now() + 2.0)
    }

    func requestShutdown() {
        stopSemaphore.signal()
    }

    private func start() async throws {
        status("step", "querying_shareable_content")
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false
        )
        status("step", "got_shareable_content displays=\(content.displays.count) apps=\(content.applications.count)")

        guard let display = content.displays.first else {
            status("error", "no_display")
            requestShutdown()
            return
        }

        // Exclude this process from the audio capture (we don't want to record ourselves).
        let pidToExclude = ProcessInfo.processInfo.processIdentifier
        let appsToExclude = content.applications.filter { $0.processID == pidToExclude }
        status("step", "filter_ready excluded=\(appsToExclude.count)")

        let filter = SCContentFilter(display: display,
                                     excludingApplications: appsToExclude,
                                     exceptingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = Int(TARGET_SAMPLE_RATE)
        config.channelCount = Int(TARGET_CHANNELS)
        // NOTE: on macOS 26 (Tahoe) setting excludesCurrentProcessAudio = true
        // can silently mute the captured stream entirely in some setups.
        // Our helper process doesn't output audio, so excluding it gains
        // nothing — leave it off.
        config.excludesCurrentProcessAudio = false
        // Video config: minimum viable. We don't consume video output, but SCK
        // requires a sane video filter even for audio-only capture.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1) // 1 fps
        config.queueDepth = 6
        config.showsCursor = false
        status("step", "config_ready ex_self=\(config.excludesCurrentProcessAudio)")

        let output = AudioCaptureOutput(shutdown: { [weak self] in self?.requestShutdown() })
        let stream = SCStream(filter: filter, configuration: config, delegate: output)
        status("step", "stream_created")

        // IMPORTANT: register BOTH .screen and .audio outputs. With some
        // macOS versions / SCK configurations, audio buffers are not
        // delivered unless a video output is also registered, even if
        // we never look at the video frames.
        try stream.addStreamOutput(output, type: .screen, sampleHandlerQueue: outputQueue)
        try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: outputQueue)
        status("step", "outputs_added")

        try await stream.startCapture()
        self.stream = stream
        self.output = output
        status("ready", "capturing sr=\(Int(TARGET_SAMPLE_RATE)) ch=\(TARGET_CHANNELS)")
    }

    private func stop() async {
        guard let stream = stream else { return }
        do {
            try await stream.stopCapture()
        } catch {
            // Suppress: harmless on shutdown paths.
        }
        self.stream = nil
        self.output = nil
        status("stopped", "ok")
    }
}

// MARK: - Entry point

// Print as the very first action so we can prove the binary actually
// reached main(). If you don't see this, the binary is being killed by
// the OS before it can run (typically a code-signing / TCC issue after
// recompiling — see README for resolution).
status("started", "pid=\(ProcessInfo.processInfo.processIdentifier)")

if #available(macOS 13.0, *) {
    Capturer().run()
} else {
    status("error", "requires_macos_13")
    exit(2)
}
