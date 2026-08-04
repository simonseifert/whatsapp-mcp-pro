// speech-transcribe — transcribe an audio file with Apple's on-device
// SpeechAnalyzer, print the text to stdout.
//
//   swiftc -O speech-transcribe.swift -o speech-transcribe
//   ./speech-transcribe voice.wav [locale]
//
// Why this exists: wa-assistant's STT_CMD wants a binary that takes a file and
// prints a transcript. The alternatives all cost something SpeechAnalyzer does
// not — mlx-whisper needs Python, uv and a ~1.5 GB model download; Groq needs a
// key and sends your voice notes to a third party. This ships with macOS 26,
// runs entirely on-device, and measured ~2.12% WER against Whisper Small's
// 3.74% at roughly 3x the speed.
//
// Requires macOS 26 (Tahoe). On older systems, keep using WHISPER_BACKEND=mlx.

import AVFoundation
import Foundation
import Speech

// The model is per-locale and downloaded on demand. First run for a new locale
// pulls it; subsequent runs are offline. Without this, analysis fails on a
// clean machine with an unhelpful error.
func ensureModel(for transcriber: SpeechTranscriber, locale: Locale) async throws {
    let installed = await SpeechTranscriber.installedLocales
    if installed.contains(where: { $0.identifier(.bcp47) == locale.identifier(.bcp47) }) {
        return
    }
    let supported = await SpeechTranscriber.supportedLocales
    guard supported.contains(where: { $0.identifier(.bcp47) == locale.identifier(.bcp47) }) else {
        FileHandle.standardError.write(
            "speech-transcribe: locale \(locale.identifier) not supported. Supported: \(supported.map { $0.identifier }.sorted().joined(separator: ", "))\n"
                .data(using: .utf8)!)
        exit(3)
    }
    if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
        FileHandle.standardError.write("speech-transcribe: downloading model for \(locale.identifier)…\n".data(using: .utf8)!)
        try await request.downloadAndInstall()
    }
}

func transcribe(path: String, locale: Locale) async throws -> String {
    let url = URL(fileURLWithPath: path)
    guard FileManager.default.fileExists(atPath: path) else {
        FileHandle.standardError.write("speech-transcribe: no such file: \(path)\n".data(using: .utf8)!)
        exit(2)
    }
    let file = try AVAudioFile(forReading: url)

    let transcriber = SpeechTranscriber(
        locale: locale,
        transcriptionOptions: [],
        reportingOptions: [],       // final results only; volatile ones would duplicate text
        attributeOptions: []        // no timestamps — callers want a plain transcript
    )

    // Collect before analysing: results is an AsyncSequence that yields while
    // analysis runs, so draining it afterwards would miss everything.
    let collector = Task { () -> String in
        var parts: [String] = []
        for try await result in transcriber.results {
            parts.append(String(result.text.characters))
        }
        return parts.joined()
    }

    try await ensureModel(for: transcriber, locale: locale)

    // Note: the SpeechAnalyzer(inputAudioFile:) convenience init ALREADY begins
    // analysing that file. Constructing with it and then also calling
    // analyzeSequence(from:) feeds the same file twice and traps (SIGTRAP, exit
    // 133, no message). Use the plain module init and drive the file once.
    let analyzer = SpeechAnalyzer(modules: [transcriber])
    _ = try await analyzer.analyzeSequence(from: file)
    try await analyzer.finalizeAndFinishThroughEndOfInput()

    return try await collector.value.trimmingCharacters(in: .whitespacesAndNewlines)
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: speech-transcribe <audio-file> [locale]   e.g. en-US, hr-HR\n".data(using: .utf8)!)
    exit(2)
}
let locale = Locale(identifier: args.count >= 3 ? args[2] : "en-US")

do {
    let text = try await transcribe(path: args[1], locale: locale)
    print(text)
} catch {
    FileHandle.standardError.write("speech-transcribe: \(error)\n".data(using: .utf8)!)
    exit(1)
}
