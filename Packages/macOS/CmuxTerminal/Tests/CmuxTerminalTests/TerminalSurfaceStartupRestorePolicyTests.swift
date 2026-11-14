import AppKit
import CmuxTerminalCore
import Foundation
import GhosttyKit
import Testing
@testable import CmuxTerminal

@MainActor
@Suite
struct TerminalSurfaceStartupRestorePolicyTests {
    @Test("Public cancellation prevents staged restore input at a later prompt")
    func publicCancellationConsumesStagedCommandBeforeLaterPrompt() {
        let nativeView = FakeTerminalSurfaceNativeView(
            frame: NSRect(x: 0, y: 0, width: 800, height: 600)
        )
        let paneHost = FakeTerminalSurfacePaneHost(
            surfaceView: nativeView,
            attachesThroughSurfaceModel: true
        )
        let surface = makeSurface(
            policy: .pacedSessionRestore.requiringStartupRestoreAdmission(),
            scheduler: RecordingRestoreSpawnScheduler(),
            nativeView: nativeView,
            paneHost: paneHost
        )
        defer { surface.closeHeadlessStartupWindowIfNeeded() }
        let generation = surface.terminalLifecycleId
        surface.startupInputGate.stage("original restore\n", generation: generation)

        surface.cancelStartupRestoreAdmission()

        let inputAtLaterPrompt = surface.startupInputGate.takeForPrompt(generation: generation)
        #expect(inputAtLaterPrompt == nil)
        #expect(!surface.admitStartupRestoreRuntime(initialInput: "late restore\n"))
        #expect(surface.suppressConfiguredInitialInput)
    }

    @Test("Restore admission composes with relaunch spawn pacing")
    func admissionPreservesRestorePacing() {
        let nativeView = FakeTerminalSurfaceNativeView(
            frame: NSRect(x: 0, y: 0, width: 800, height: 600)
        )
        let paneHost = FakeTerminalSurfacePaneHost(
            surfaceView: nativeView,
            attachesThroughSurfaceModel: true
        )
        let scheduler = RecordingRestoreSpawnScheduler()
        let surface = makeSurface(
            policy: .pacedSessionRestore.requiringStartupRestoreAdmission(),
            scheduler: scheduler,
            nativeView: nativeView,
            paneHost: paneHost
        )
        surface.agentCommandShimInstallCompleted = true
        defer { surface.closeHeadlessStartupWindowIfNeeded() }

        surface.scheduleHeadlessRuntimeStartIfNeeded(reason: "before-topology-admission")
        surface.createSurface(for: nativeView, source: .inputDemand)

        #expect(surface.debugRuntimeSurfaceCreateAttemptCountForTesting() == 0)
        #expect(scheduler.scheduledSurfaceIds.isEmpty)

        surface.admitStartupRestoreRuntime()

        #expect(surface.debugRuntimeSurfaceCreateAttemptCountForTesting() == 0)
        #expect(scheduler.scheduledSurfaceIds == [surface.id])

        surface.admitStartupRestoreRuntime()
        #expect(scheduler.scheduledSurfaceIds == [surface.id])

        scheduler.runScheduledOperation()
        #expect(surface.debugRuntimeSurfaceCreateAttemptCountForTesting() == 1)
    }

    @Test("Explicit input cancels a deferred agent resume before admission")
    func explicitInputCancelsDeferredAgentResumeBeforeAdmission() {
        let nativeView = FakeTerminalSurfaceNativeView(
            frame: NSRect(x: 0, y: 0, width: 800, height: 600)
        )
        let paneHost = FakeTerminalSurfacePaneHost(
            surfaceView: nativeView,
            attachesThroughSurfaceModel: true
        )
        let scheduler = RecordingRestoreSpawnScheduler()
        let surface = TerminalSurface(
            tabId: UUID(),
            context: GHOSTTY_SURFACE_CONTEXT_SPLIT,
            configTemplate: nil,
            initialInput: "resume codex session\n",
            runtimeSpawnPolicy: .pacedSessionRestore
                .requiringDeferredAgentResumeAdmission(),
            dependencies: makeDependencies(
                scheduler: scheduler,
                nativeView: nativeView,
                paneHost: paneHost
            )
        )
        surface.agentCommandShimInstallCompleted = true
        defer { surface.closeHeadlessStartupWindowIfNeeded() }

        var cancellationCount = 0
        surface.onStartupRestoreAdmissionCancelled = { cancellationCount += 1 }
        surface.didReceiveExplicitInput()

        #expect(cancellationCount == 1)
        #expect(surface.suppressConfiguredInitialInput)
        #expect(!surface.admitStartupRestoreRuntime(initialInput: "late resume\n"))
        // Explicit input is an immediate runtime demand and must bypass the
        // paced restore queue after cancelling the deferred agent command.
        #expect(scheduler.scheduledSurfaceIds.isEmpty)
        #expect(surface.debugRuntimeSurfaceCreateAttemptCountForTesting() == 1)
    }

    @Test("Cancelling deferred admission drops the resume command")
    func cancellationDropsResumeCommand() {
        let nativeView = FakeTerminalSurfaceNativeView(
            frame: NSRect(x: 0, y: 0, width: 800, height: 600)
        )
        let paneHost = FakeTerminalSurfacePaneHost(
            surfaceView: nativeView,
            attachesThroughSurfaceModel: true
        )
        let scheduler = RecordingRestoreSpawnScheduler()
        let surface = TerminalSurface(
            tabId: UUID(),
            context: GHOSTTY_SURFACE_CONTEXT_SPLIT,
            configTemplate: nil,
            initialCommand: "resume-with-payload",
            runtimeSpawnPolicy: .pacedSessionRestore
                .requiringDeferredAgentResumeAdmission(),
            dependencies: makeDependencies(
                scheduler: scheduler,
                nativeView: nativeView,
                paneHost: paneHost
            )
        )
        defer { surface.closeHeadlessStartupWindowIfNeeded() }

        surface.cancelStartupRestoreAdmission()

        #expect(surface.startupRestoreAdmissionCommandOverride == nil)
        #expect(surface.hasStartupRestoreAdmissionCommandOverride)
        #expect(surface.suppressConfiguredInitialInput)
    }

    @Test("Accepted runtime failure consumes an already staged restore once")
    func acceptedRuntimeFailureConsumesStagedInputOnce() {
        let nativeView = FakeTerminalSurfaceNativeView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
        let paneHost = FakeTerminalSurfacePaneHost(surfaceView: nativeView, attachesThroughSurfaceModel: true)
        let surface = makeSurface(
            policy: .pacedSessionRestore.requiringStartupRestoreAdmission(),
            scheduler: RecordingRestoreSpawnScheduler(),
            nativeView: nativeView,
            paneHost: paneHost
        )
        defer { surface.closeHeadlessStartupWindowIfNeeded() }
        let generation = surface.terminalLifecycleId
        surface.nextRuntimeInitialInput = "original restore\n"
        surface.startupInputGate.stage("original restore\n", generation: generation)
        var generations: [UUID] = []
        surface.onStartupRestoreAbandoned = { current in
            generations.append(current)
            return current == generation
        }

        surface.reportStartupRestoreRuntimeFailure()
        surface.reportStartupRestoreRuntimeFailure()

        #expect(generations == [generation])
        #expect(surface.nextRuntimeInitialInput == nil)
        #expect(surface.startupInputGate.takeForPrompt(generation: generation) == nil)
        #expect(surface.suppressConfiguredInitialInput)
        #expect(surface.hasStartupRestoreAdmissionCommandOverride)
    }

    @Test("Public cancellation notifies the selected generation and consumes queued input")
    func publicCancellationNotifiesGenerationAndConsumesQueuedInput() {
        let nativeView = FakeTerminalSurfaceNativeView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
        let paneHost = FakeTerminalSurfacePaneHost(surfaceView: nativeView, attachesThroughSurfaceModel: true)
        let surface = makeSurface(
            policy: .pacedSessionRestore.requiringStartupRestoreAdmission(),
            scheduler: RecordingRestoreSpawnScheduler(),
            nativeView: nativeView,
            paneHost: paneHost
        )
        defer { surface.closeHeadlessStartupWindowIfNeeded() }
        let generation = surface.terminalLifecycleId
        surface.startupInputGate.stage("original restore\n", generation: generation)
        var generations: [UUID] = []
        surface.onStartupRestoreAbandoned = { current in
            generations.append(current)
            return true
        }

        surface.cancelStartupRestoreAdmission()
        surface.cancelStartupRestoreAdmission()

        #expect(generations == [generation])
        #expect(surface.startupInputGate.takeForPrompt(generation: generation) == nil)
        #expect(!surface.admitStartupRestoreRuntime(initialInput: "late restore\n"))
        #expect(surface.suppressConfiguredInitialInput)
    }

    @Test("A rejected stale owner cannot cancel the current generation's input")
    func rejectedStaleOwnerPreservesCurrentInput() {
        let nativeView = FakeTerminalSurfaceNativeView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
        let paneHost = FakeTerminalSurfacePaneHost(surfaceView: nativeView, attachesThroughSurfaceModel: true)
        let surface = makeSurface(
            policy: .pacedSessionRestore.requiringStartupRestoreAdmission(),
            scheduler: RecordingRestoreSpawnScheduler(),
            nativeView: nativeView,
            paneHost: paneHost
        )
        defer { surface.closeHeadlessStartupWindowIfNeeded() }
        let oldGeneration = surface.terminalLifecycleId
        surface.advanceTerminalLifecycleForRuntimeReplacement()
        let currentGeneration = surface.terminalLifecycleId
        #expect(currentGeneration != oldGeneration)
        surface.nextRuntimeInitialInput = "new restore\n"
        surface.startupInputGate.stage("new restore\n", generation: currentGeneration)
        surface.onStartupRestoreAbandoned = { $0 == oldGeneration }

        surface.reportStartupRestoreRuntimeFailure()

        #expect(surface.nextRuntimeInitialInput == "new restore\n")
        #expect(surface.startupInputGate.takeForPrompt(generation: currentGeneration) == "new restore\n")
        #expect(!surface.suppressConfiguredInitialInput)
    }

    @Test("Input cancellation keeps the legacy deferred-owner notification")
    func generationCancellationPreservesLegacyDeferredCallback() {
        let nativeView = FakeTerminalSurfaceNativeView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
        let paneHost = FakeTerminalSurfacePaneHost(surfaceView: nativeView, attachesThroughSurfaceModel: true)
        let surface = makeSurface(
            policy: .pacedSessionRestore.requiringDeferredAgentResumeAdmission(),
            scheduler: RecordingRestoreSpawnScheduler(),
            nativeView: nativeView,
            paneHost: paneHost
        )
        surface.agentCommandShimInstallCompleted = true
        defer { surface.closeHeadlessStartupWindowIfNeeded() }
        var selectedGenerations: [UUID] = []
        var legacyCount = 0
        surface.onStartupRestoreAbandoned = { generation in
            selectedGenerations.append(generation)
            return false
        }
        surface.onStartupRestoreAdmissionCancelled = { legacyCount += 1 }

        surface.didReceiveExplicitInput()

        #expect(selectedGenerations == [surface.terminalLifecycleId])
        #expect(legacyCount == 1)
        #expect(surface.suppressConfiguredInitialInput)
    }

    @Test("Runtime pacing and an unavailable app remain pending observations")
    func pacingAndUninitializedRuntimeDoNotAbandonRestore() {
        let nativeView = FakeTerminalSurfaceNativeView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
        let paneHost = FakeTerminalSurfacePaneHost(surfaceView: nativeView, attachesThroughSurfaceModel: true)
        let scheduler = RecordingRestoreSpawnScheduler()
        let surface = makeSurface(
            policy: .pacedSessionRestore.requiringStartupRestoreAdmission(),
            scheduler: scheduler,
            nativeView: nativeView,
            paneHost: paneHost
        )
        surface.agentCommandShimInstallCompleted = true
        defer { surface.closeHeadlessStartupWindowIfNeeded() }
        var abandonments = 0
        surface.onStartupRestoreAbandoned = { _ in
            abandonments += 1
            return true
        }
        surface.nextRuntimeInitialInput = "pending restore\n"

        surface.admitStartupRestoreRuntime()
        #expect(abandonments == 0)
        #expect(scheduler.scheduledSurfaceIds == [surface.id])
        scheduler.runScheduledOperation()

        // FakeTerminalEngine has no runtime app: this is a wait, not a failed native creation.
        #expect(abandonments == 0)
        #expect(surface.nextRuntimeInitialInput == "pending restore\n")
        #expect(!surface.suppressConfiguredInitialInput)
    }


    private func makeSurface(
        policy: TerminalSurfaceRuntimeSpawnPolicy,
        scheduler: RecordingRestoreSpawnScheduler,
        nativeView: FakeTerminalSurfaceNativeView,
        paneHost: FakeTerminalSurfacePaneHost
    ) -> TerminalSurface {
        TerminalSurface(
            tabId: UUID(),
            context: GHOSTTY_SURFACE_CONTEXT_SPLIT,
            configTemplate: nil,
            runtimeSpawnPolicy: policy,
            dependencies: makeDependencies(
                scheduler: scheduler,
                nativeView: nativeView,
                paneHost: paneHost
            )
        )
    }

    private func makeDependencies(
        scheduler: RecordingRestoreSpawnScheduler,
        nativeView: FakeTerminalSurfaceNativeView,
        paneHost: FakeTerminalSurfacePaneHost
    ) -> TerminalSurfaceRuntimeDependencies {
        TerminalSurfaceRuntimeDependencies(
                registry: FakeSurfaceRegistry(),
                engine: FakeTerminalEngine(),
                viewProvider: FakeTerminalSurfaceViewProvider(
                    surfaceView: nativeView,
                    paneHost: paneHost
                ),
                spawnPolicy: FakeSpawnPolicyProvider(),
                byteTee: FakeTerminalByteTee(),
                rendererRealization: FakeRendererRealizationScheduler(),
                hibernationRecorder: FakeHibernationRecorder(),
                runtimeTeardown: TerminalSurfaceRuntimeTeardownCoordinator(),
                restoreSpawnScheduler: scheduler,
                runtimeFilesystem: TerminalSurfaceRuntimeFilesystem(
                    agentCommandShimRootDirectory: URL(
                        fileURLWithPath: "/tmp/cmux-terminal-tests",
                        isDirectory: true
                    ),
                    installAgentCommandShims: { _, _, _ in nil },
                    isExecutableFile: { _ in false }
                ),
                sessionPortBase: 40_000,
                sessionPortRangeSize: 100,
                scrollbackReplayEnvironmentKey: "CMUX_TEST_SCROLLBACK_REPLAY"
        )
    }
}
