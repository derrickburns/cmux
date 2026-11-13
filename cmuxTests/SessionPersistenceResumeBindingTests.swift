import CMUXAgentLaunch
import Foundation
import CmuxCore
import Testing

#if canImport(cmux_DEV)
@testable import cmux_DEV
#elseif canImport(cmux)
@testable import cmux
#endif

@Suite struct SessionPersistenceResumeBindingTests {
    @Test func structuredLaunchCaptureRoundTripsAdditively() throws {
        let binding = SurfaceResumeBindingSnapshot(
            name: "Codex",
            kind: "codex",
            command: "codex resume legacy-display-command",
            cwd: "/tmp/项目 with 'quotes'",
            checkpointId: "a22293b7-bcef-4707-8439-2f538c8517a4",
            source: "agent-hook",
            environment: ["CODEX_HOME": "/tmp/配置"],
            launchCommand: AgentLaunchCommandSnapshot(
                launcher: "codex",
                executablePath: "/opt/company bin/codex",
                arguments: [
                    "/opt/company bin/codex",
                    "--model",
                    "gpt-5.6-sol",
                    "日本語",
                ],
                workingDirectory: "/tmp/项目 with 'quotes'",
                environment: ["CODEX_HOME": "/tmp/配置"],
                capturedAt: 123,
                source: "process"
            ),
            autoResume: true
        )

        let decoded = try JSONDecoder().decode(
            SurfaceResumeBindingSnapshot.self,
            from: JSONEncoder().encode(binding)
        )

        #expect(decoded == binding)
        #expect(decoded.launchCommand?.arguments == binding.launchCommand?.arguments)
        #expect(decoded.command.contains("codex resume legacy-display-command"))
    }

    @Test func v06420CommandOnlyBindingStillProducesRestoreVerb() throws {
        let json = """
        {
          "name": "Legacy custom agent",
          "kind": "custom-agent",
          "command": "legacy-agent --resume 'old checkpoint'",
          "cwd": "/tmp/legacy",
          "checkpointId": "old checkpoint",
          "source": "agent-hook",
          "environment": {"LEGACY_VALUE": "preserved"},
          "autoResume": true,
          "approvalPolicy": "auto",
          "approvalRecordId": "legacy-approval",
          "updatedAt": 1
        }
        """
        let binding = try JSONDecoder().decode(
            SurfaceResumeBindingSnapshot.self,
            from: Data(json.utf8)
        )

        #expect(binding.launchCommand == nil)
        #expect(binding.permissionMode == nil)
        #expect(binding.launchFlavor == .local)
        #expect(binding.environment == ["LEGACY_VALUE": "preserved"])
        #expect(
            binding.restoreStartupInput()
                == " \(AgentRestoreLaunch.cliStartupExecutableToken) restore --surface\n"
        )
    }

    @Test func localRestoreUsesOneShortCLICommandRegardlessOfBindingSize() throws {
        let sessionId = "a22293b7-bcef-4707-8439-2f538c8517a4"


        let binding = SurfaceResumeBindingSnapshot(
            kind: "codex",
            command: "codex resume \(sessionId) " + String(repeating: "--config model_provider=subrouter ", count: 80),
            checkpointId: sessionId,
            source: "agent-hook",
            autoResume: true
        )

        let startupInput = try #require(binding.restoreStartupInput())

        #expect(
            startupInput
                == " \(AgentRestoreLaunch.cliStartupExecutableToken) restore codex \(sessionId)\n"
        )
    }

    /// Regression for the first nushell dogfood round: the compatibility
    /// inline startup input stays raw POSIX (local callers apply the nushell
    /// `^/bin/sh -c "…"` envelope only at their typed boundary,
    /// `restoreStartupInput`), and the local restore verb stays bare words,
    /// which parse identically in POSIX shells and nushell.
    @Test func nushellTypingEnvelopeAppliesOnlyAtTheTypedBoundary() throws {
        let binding = SurfaceResumeBindingSnapshot(
            kind: "claude",
            command: "'claude' '--resume' 'session-nu-envelope'",
            checkpointId: "session-nu-envelope",
            source: "agent-hook",
            autoResume: true
        )

        let raw = try #require(binding.inlineStartupInput)
        #expect(!raw.contains("^/bin/sh"), "inline input must stay raw POSIX: \(raw)")

        let restore = try #require(binding.restoreStartupInput())
        #expect(restore.hasPrefix(" \(AgentRestoreLaunch.cliStartupExecutableToken) restore"), "\(restore)")
        #expect(!restore.contains("^/bin/sh"), "the bare-word restore verb needs no dialect envelope: \(restore)")
        #expect(
            !restore.contains("&&") && !restore.contains("||") && !restore.contains("'"),
            "restore input must stay nushell-parseable bare words: \(restore)"
        )
    }

    @Test(arguments: ["codex", "claude"])
    func agentHookRestoreBindingCarriesProviderAndSessionBoundAuthorization(kind: String) throws {
        let sessionId = "a22293b7-bcef-4707-8439-2f538c8517a4"
        let resumeArgument = kind == "codex" ? "resume" : "--resume"
        let binding = SurfaceResumeBindingSnapshot(
            kind: kind,
            command: "'/opt/company/bin/\(kind)' '\(resumeArgument)' '\(sessionId)'",
            checkpointId: sessionId,
            source: "agent-hook",
            autoResume: true
        )

        let startupInput = try #require(binding.inlineStartupInput)
        #expect(
            startupInput.contains("/usr/bin/env 'CMUX_AGENT_RESTORE_LAUNCH=\(kind):\(sessionId)'"),
            "\(startupInput)"
        )
        #expect(startupInput.contains("CMUX_\(kind.uppercased())_WRAPPER_SHIM"), "\(startupInput)")
        #expect(startupInput.contains("CMUX_CUSTOM_\(kind.uppercased())_PATH="), "\(startupInput)")
    }

    @Test func routedCodexBindingRoutesSubrouterChildThroughManagedWrapper() throws {
        let sessionId = "a22293b7-bcef-4707-8439-2f538c8517a4"
        let binding = SurfaceResumeBindingSnapshot(
            kind: "codex",
            command: "'sr' 'codex' 'resume' '\(sessionId)'",
            checkpointId: sessionId,
            source: "agent-hook",
            environment: ["SUBROUTER_CODEX_BIN": "/opt/company/bin/codex"],
            launchCommand: AgentLaunchCommandSnapshot(
                launcher: "codex",
                executablePath: "/opt/company/bin/codex",
                arguments: ["/opt/company/bin/codex", "-c", "model_provider=subrouter"],
                workingDirectory: "/tmp/project",
                environment: [
                    "CMUX_AGENT_LAUNCH_SUBROUTER_CODEX_RESUME_COMMAND": "sr codex resume",
                    "SUBROUTER_CODEX_BIN": "/opt/company/bin/codex",
                    "SUBROUTER_CODEX_RESUME_COMMAND": "sr codex resume",
                ],
                capturedAt: 1,
                source: "environment"
            ),
            autoResume: true
        )

        let startupInput = try #require(binding.inlineStartupInput)

        #expect(startupInput.contains("CMUX_AGENT_RESTORE_LAUNCH=codex:\(sessionId)"), "\(startupInput)")
        #expect(startupInput.contains("SUBROUTER_CODEX_BIN="), "\(startupInput)")
        #expect(startupInput.contains("CMUX_CODEX_WRAPPER_SHIM"), "\(startupInput)")
        #expect(startupInput.contains("CMUX_CUSTOM_CODEX_PATH=/opt/company/bin/codex"), "\(startupInput)")
        #expect(startupInput.contains("/bin/sh -c"), "\(startupInput)")
    }

    @Test func repeatedRoutedCodexBindingKeepsTheCapturedRealCodexExecutable() throws {
        let sessionId = "a22293b7-bcef-4707-8439-2f538c8517a4"
        let binding = SurfaceResumeBindingSnapshot(
            kind: "codex",
            command: "'sr' 'codex' 'resume' '\(sessionId)'",
            checkpointId: sessionId,
            source: "agent-hook",
            launchCommand: AgentLaunchCommandSnapshot(
                launcher: "codex",
                executablePath: "/opt/custom/codex",
                arguments: ["/opt/custom/codex", "-c", "model_provider=subrouter"],
                workingDirectory: "/tmp/project",
                environment: [
                    "CMUX_AGENT_LAUNCH_SUBROUTER_CODEX_RESUME_COMMAND": "sr codex resume",
                    "CMUX_CUSTOM_CODEX_PATH": "/opt/custom/codex",
                    "SUBROUTER_CODEX_BIN": "${CMUX_CODEX_WRAPPER_SHIM:-codex}",
                    "SUBROUTER_CODEX_RESUME_COMMAND": "sr codex resume",
                ],
                capturedAt: 1,
                source: "environment"
            ),
            autoResume: true
        )

        let startupInput = try #require(binding.inlineStartupInput)

        #expect(startupInput.contains("CMUX_CUSTOM_CODEX_PATH=/opt/custom/codex"), "\(startupInput)")
        #expect(
            !startupInput.contains("${CMUX_CODEX_WRAPPER_SHIM:-codex}"),
            "\(startupInput)"
        )
    }

    @Test func restoreBindingAuthorizationRejectsUnownedOrUnboundCommands() throws {
        let sessionId = "a22293b7-bcef-4707-8439-2f538c8517a4"
        let nonHook = SurfaceResumeBindingSnapshot(
            kind: "codex",
            command: "codex resume '\(sessionId)'",
            checkpointId: sessionId,
            source: "cli",
            autoResume: true
        )
        let unsupported = SurfaceResumeBindingSnapshot(
            kind: "gemini",
            command: "gemini --resume '\(sessionId)'",
            checkpointId: sessionId,
            source: "agent-hook",
            autoResume: true
        )
        let invalidSession = SurfaceResumeBindingSnapshot(
            kind: "claude",
            command: "claude --resume not-a-session-id",
            checkpointId: "not-a-session-id",
            source: "agent-hook",
            autoResume: true
        )

        #expect(try #require(nonHook.inlineStartupInput).contains("CMUX_AGENT_RESTORE_LAUNCH") == false)
        #expect(try #require(unsupported.inlineStartupInput).contains("CMUX_AGENT_RESTORE_LAUNCH") == false)
        #expect(try #require(invalidSession.inlineStartupInput).contains("CMUX_AGENT_RESTORE_LAUNCH") == false)
    }

    @Test func agentHookSurfaceResumeRoutesCustomExecutableThroughWrapper() throws {
        let sessionId = "a22293b7-bcef-4707-8439-2f538c8517a4"
        let binding = SurfaceResumeBindingSnapshot(
            kind: "codex",
            command: "'/opt/company/bin/codex' 'resume' '\(sessionId)'",
            checkpointId: sessionId,
            source: "agent-hook",
            autoResume: true
        )

        let startupInput = try #require(binding.startupInput)

        #expect(startupInput.contains("CMUX_CODEX_WRAPPER_SHIM"), "\(startupInput)")
        #expect(startupInput.contains("CMUX_CUSTOM_CODEX_PATH="), "\(startupInput)")
        #expect(startupInput.contains("/opt/company/bin/codex"), "\(startupInput)")
    }

    @Test func decodingAgentHookBindingRewritesPersistedPATHManagedAgentExecutable() throws {
        let executablePath = Self.homeManagedExecutablePath(
            executableName: "claude",
            ".nvm",
            "versions",
            "node",
            "cmux-missing-\(UUID().uuidString)",
            "bin"
        )
        let json = """
        {
          "kind": "claude",
          "command": "{ cd -- '/tmp/project' 2>/dev/null || [ ! -d '/tmp/project' ]; } && '\(executablePath)' '--resume' 'session-moved-cli' '--chrome'",
          "cwd": "/tmp/project",
          "checkpointId": "session-moved-cli",
          "source": "agent-hook",
          "autoResume": true,
          "updatedAt": 123
        }
        """
        let binding = try JSONDecoder().decode(SurfaceResumeBindingSnapshot.self, from: Data(json.utf8))
        let startupInput = try #require(binding.startupInput)

        #expect(binding.command.contains(executablePath), "\(binding.command)")
        #expect(startupInput.contains("/bin/sh -c"), "\(startupInput)")
        #expect(startupInput.contains("CMUX_CLAUDE_WRAPPER_SHIM"), "\(startupInput)")
        #expect(startupInput.contains("--resume"), "\(startupInput)")
        #expect(!startupInput.contains(executablePath), "\(startupInput)")
    }

    @Test func legacyAgentHookBindingWithoutKindRewritesPersistedPATHManagedAgentExecutable() throws {
        let executablePath = Self.homeManagedExecutablePath(
            executableName: "codex",
            ".nvm",
            "versions",
            "node",
            "cmux-missing-\(UUID().uuidString)",
            "bin"
        )
        let json = """
        {
          "command": "'\(executablePath)' 'resume' 'session-legacy-cli'",
          "checkpointId": "session-legacy-cli",
          "source": "agent-hook",
          "autoResume": true,
          "updatedAt": 123
        }
        """
        let binding = try JSONDecoder().decode(SurfaceResumeBindingSnapshot.self, from: Data(json.utf8))
        let startupInput = try #require(binding.startupInput)

        #expect(binding.kind == nil)
        #expect(binding.command.contains(executablePath), "\(binding.command)")
        #expect(
            startupInput.contains("CMUX_CODEX_WRAPPER_SHIM")
                && startupInput.contains("resume")
                && startupInput.contains("session-legacy-cli"),
            "\(startupInput)"
        )
        #expect(!startupInput.contains(executablePath), "\(startupInput)")
    }

    @Test func agentHookBindingRewritesSupportedLocalManagedExecutablePaths() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("cmux-surface-resume-stale-managed-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let executablePaths = [
            Self.localManagedExecutablePath(root: root, executableName: "codex", ".fnm", "current", "bin"),
            "/tmp/cmux-cli-shims/\(UUID().uuidString)/codex",
            Self.localManagedExecutablePath(
                root: root,
                executableName: "codex",
                "Library",
                "Application Support",
                "fnm",
                "node-versions",
                "v24.2.0",
                "installation",
                "bin"
            ),
            Self.localManagedExecutablePath(
                root: root,
                executableName: "codex",
                ".local",
                "share",
                "fnm",
                "node-versions",
                "v24.2.0",
                "installation",
                "bin"
            ),
            Self.localManagedExecutablePath(root: root, executableName: "codex", ".local", "share", "mise", "shims"),
        ]

        for executablePath in executablePaths {
            let binding = SurfaceResumeBindingSnapshot(
                kind: "codex",
                command: "'\(executablePath)' 'resume' 'session-managed-cli'",
                checkpointId: "session-managed-cli",
                source: "agent-hook",
                autoResume: true
            )

            let startupInput = try #require(binding.startupInput)
            #expect(
                startupInput.contains("CMUX_CODEX_WRAPPER_SHIM")
                    && startupInput.contains("resume")
                    && startupInput.contains("session-managed-cli"),
                "\(startupInput)"
            )
            #expect(!startupInput.contains(executablePath), "\(startupInput)")
        }
    }

    @Test func agentHookBindingWithDirectEnvironmentAssignmentRewritesMovedExecutable() throws {
        let staleExecutablePath = Self.homeManagedExecutablePath(
            executableName: "codex",
            ".nvm",
            "versions",
            "node",
            "cmux-missing-\(UUID().uuidString)",
            "bin"
        )
        let binding = SurfaceResumeBindingSnapshot(
            kind: "codex",
            command: "CMUX_TRACE=1 '\(staleExecutablePath)' 'resume' 'session-env-cli'",
            checkpointId: "session-env-cli",
            source: "agent-hook",
            autoResume: true
        )

        let startupInput = try #require(binding.startupInput)

        #expect(
            startupInput.contains("CMUX_TRACE=1")
                && startupInput.contains("CMUX_CODEX_WRAPPER_SHIM")
                && startupInput.contains("session-env-cli"),
            "\(startupInput)"
        )
        #expect(!startupInput.contains(staleExecutablePath), "\(startupInput)")
    }

    @Test func agentHookBindingWithQuotedEnvAssignmentRewritesMovedExecutable() throws {
        let staleExecutablePath = Self.homeManagedExecutablePath(
            executableName: "codex",
            ".nvm",
            "versions",
            "node",
            "cmux-missing-\(UUID().uuidString)",
            "bin"
        )
        let binding = SurfaceResumeBindingSnapshot(
            kind: "codex",
            command: "env 'CMUX_TRACE=1' '\(staleExecutablePath)' 'resume' 'session-quoted-env-cli'",
            checkpointId: "session-quoted-env-cli",
            source: "agent-hook",
            autoResume: true
        )
        let startupInput = try #require(binding.startupInput)

        #expect(
            startupInput.contains("env CMUX_TRACE=1")
                && startupInput.contains("CMUX_CODEX_WRAPPER_SHIM")
                && startupInput.contains("session-quoted-env-cli"),
            "\(startupInput)"
        )
        #expect(!startupInput.contains(staleExecutablePath), "\(startupInput)")
    }

    @Test func agentHookClaudeBindingWithDirectEnvironmentAssignmentPreservesAssignmentSyntax() throws {
        let staleExecutablePath = Self.homeManagedExecutablePath(
            executableName: "claude",
            ".nvm",
            "versions",
            "node",
            "cmux-missing-\(UUID().uuidString)",
            "bin"
        )
        let binding = SurfaceResumeBindingSnapshot(
            kind: "claude",
            command: "CMUX_TRACE='bar baz' '\(staleExecutablePath)' '--resume' 'session-env-cli'",
            checkpointId: "session-env-cli",
            source: "agent-hook",
            autoResume: true
        )
        let startupInput = try #require(binding.startupInput)

        #expect(startupInput.contains("/bin/sh -c"), "\(startupInput)")
        #expect(startupInput.contains("CMUX_CLAUDE_WRAPPER_SHIM"), "\(startupInput)")
        #expect(startupInput.contains("CMUX_TRACE="), "\(startupInput)")
        #expect(startupInput.contains("bar baz"), "\(startupInput)")
        #expect(!startupInput.contains(staleExecutablePath), "\(startupInput)")
    }

    @Test func agentHookClaudeBindingWithShellOperatorKeepsOriginalCommandShape() throws {
        let staleExecutablePath = Self.homeManagedExecutablePath(
            executableName: "claude",
            ".nvm",
            "versions",
            "node",
            "cmux-missing-\(UUID().uuidString)",
            "bin"
        )
        let redirection = "1>/tmp/cmux-claude-resume.log"
        let binding = SurfaceResumeBindingSnapshot(
            kind: "claude",
            command: "'\(staleExecutablePath)' '--resume' 'session-operator-cli' \(redirection) && echo done",
            checkpointId: "session-operator-cli",
            source: "agent-hook",
            autoResume: true
        )
        let startupInput = try #require(binding.startupInput)

        #expect(binding.command.contains("&& echo done"), "\(binding.command)")
        #expect(binding.command.contains(staleExecutablePath), "\(binding.command)")
        #expect(startupInput.contains("/bin/sh -c"), "\(startupInput)")
        #expect(startupInput.contains("CMUX_CLAUDE_WRAPPER_SHIM"), "\(startupInput)")
        #expect(startupInput.contains("session-operator-cli \(redirection) && echo done"), "\(startupInput)")
        #expect(!startupInput.contains(staleExecutablePath), "\(startupInput)")
    }

    @Test func agentHookBindingPreservesRemoteManagedExecutablePath() throws {
        let remoteExecutablePath = "/home/me/.nvm/versions/node/v24.2.0/bin/codex"
        let binding = SurfaceResumeBindingSnapshot(
            kind: "codex",
            command: "'\(remoteExecutablePath)' 'resume' 'session-remote-cli'",
            checkpointId: "session-remote-cli",
            source: "agent-hook",
            autoResume: true
        )

        let startupInput = try #require(binding.startupInput)
        #expect(startupInput.contains("'\(remoteExecutablePath)' 'resume' 'session-remote-cli'"), "\(startupInput)")
    }

    @Test func remoteStartupInputPreservesLocalLookingManagedExecutablePaths() throws {
        let executablePaths = [
            Self.homeManagedExecutablePath(
                executableName: "codex",
                ".nvm",
                "versions",
                "node",
                "cmux-missing-\(UUID().uuidString)",
                "bin"
            ),
            "/tmp/cmux-cli-shims/\(UUID().uuidString)/codex",
        ]

        for executablePath in executablePaths {
            let binding = SurfaceResumeBindingSnapshot(
                kind: "codex",
                command: "'\(executablePath)' 'resume' 'session-remote-local-looking-cli'",
                checkpointId: "session-remote-local-looking-cli",
                source: "agent-hook",
                autoResume: true
            )

            let startupInput = try #require(binding.inlineStartupInput(
                repairPortableAgentExecutable: false
            ))
            #expect(
                startupInput.contains("'\(executablePath)' 'resume' 'session-remote-local-looking-cli'"),
                "\(startupInput)"
            )
        }
    }

    @Test @MainActor func remoteWorkspaceLocalTerminalResumeBindingUsesShortLocalRestoreVerb() throws {
        let fileManager = FileManager.default
        let root = fileManager.temporaryDirectory
            .appendingPathComponent("cmux-local-resume-binding-\(UUID().uuidString)", isDirectory: true)
        let localDirectoryURL = root.appendingPathComponent("local repo", isDirectory: true)
        try fileManager.createDirectory(at: localDirectoryURL, withIntermediateDirectories: true)
        defer { try? fileManager.removeItem(at: root) }

        let suiteName = "cmux-session-resume-binding-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        defaults.set(true, forKey: AgentSessionAutoResumeSettings.autoResumeAgentSessionsKey)

        let remoteWorkspace = Workspace(agentSessionAutoResumeDefaults: defaults)
        remoteWorkspace.setCustomTitle("Remote Workspace With Local Resume Binding")
        remoteWorkspace.configureRemoteConnection(
            WorkspaceRemoteConfiguration(
                destination: "dev@example.com",
                port: 2222,
                identityFile: nil,
                sshOptions: [
                    "StrictHostKeyChecking=accept-new",
                ],
                localProxyPort: nil,
                relayPort: nil,
                relayID: nil,
                relayToken: nil,
                localSocketPath: nil,
                terminalStartupCommand: "ssh -p 2222 dev@example.com",
                preserveAfterTerminalExit: false
            ),
            autoConnect: false
        )
        let paneId = try #require(remoteWorkspace.bonsplitController.allPaneIds.first)
        let localDirectory = localDirectoryURL.path
        let localPanel = try #require(remoteWorkspace.newTerminalSurface(
            inPane: paneId,
            focus: true,
            workingDirectory: localDirectory,
            suppressWorkspaceRemoteStartupCommand: true
        ))
        remoteWorkspace.setPanelCustomTitle(panelId: localPanel.id, title: "Local Resume Shell")
        let staleExecutablePath = Self.homeManagedExecutablePath(
            executableName: "codex",
            ".nvm",
            "versions",
            "node",
            "cmux-missing-\(UUID().uuidString)",
            "bin"
        )
        let oversizedArgument = String(
            repeating: "x",
            count: 901
        )
        let quotedDirectory = "'\(localDirectory)'"
        #expect(remoteWorkspace.setSurfaceResumeBinding(
            SurfaceResumeBindingSnapshot(
                name: "Codex",
                kind: "codex",
                command: "{ cd -- \(quotedDirectory) 2>/dev/null || [ ! -d \(quotedDirectory) ]; } && "
                    + "'\(staleExecutablePath)' 'resume' 'session-local-resume' '\(oversizedArgument)'",
                cwd: localDirectory,
                checkpointId: "session-local-resume",
                source: "agent-hook",
                autoResume: true,
                updatedAt: 10
            ),
            panelId: localPanel.id
        ))

        let snapshot = remoteWorkspace.sessionSnapshot(includeScrollback: false)
        let persistedLocalPanel = try #require(snapshot.panels.first {
            $0.customTitle == "Local Resume Shell"
        })
        #expect(persistedLocalPanel.terminal?.isRemoteTerminal == false)
        #expect(persistedLocalPanel.terminal?.resumeBinding?.command.contains(staleExecutablePath) == true)

        let restoredWorkspace = Workspace(agentSessionAutoResumeDefaults: defaults)
        restoredWorkspace.restoreSessionSnapshot(snapshot)
        let restoredLocalPanel = try #require(
            restoredWorkspace.sessionSnapshot(includeScrollback: false)
                .panels.first { $0.customTitle == "Local Resume Shell" }
        )
        let restoredPanel = try #require(restoredWorkspace.terminalPanel(for: restoredLocalPanel.id))
        #expect(restoredPanel.surface.debugInitialCommand() == nil)
        let restoredBinding = try #require(restoredLocalPanel.terminal?.resumeBinding)
        let restoredInput = try #require(restoredBinding.restoreStartupInput())
        #expect(restoredPanel.requestedWorkingDirectory == localDirectory)
        #expect(
            restoredInput
                == " \(AgentRestoreLaunch.cliStartupExecutableToken) restore codex session-local-resume\n"
        )
        #expect(!restoredInput.contains(staleExecutablePath))
        #expect(!restoredInput.contains(oversizedArgument))
    }

    @Test func agentHookSurfaceResumeStartupInputPreservesExistingPATHManagedAgentExecutable() throws {
        let fileManager = FileManager.default
        let root = fileManager.temporaryDirectory
            .appendingPathComponent("cmux-surface-resume-existing-agent-\(UUID().uuidString)", isDirectory: true)
        let executable = root
            .appendingPathComponent(".nvm", isDirectory: true)
            .appendingPathComponent("versions", isDirectory: true)
            .appendingPathComponent("node", isDirectory: true)
            .appendingPathComponent("v24.2.0", isDirectory: true)
            .appendingPathComponent("bin", isDirectory: true)
            .appendingPathComponent("codex", isDirectory: false)
        try fileManager.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
        try "#!/bin/sh\n".write(to: executable, atomically: true, encoding: .utf8)
        try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        defer { try? fileManager.removeItem(at: root) }

        let binding = SurfaceResumeBindingSnapshot(
            kind: "codex",
            command: "'\(executable.path)' 'resume' 'session-existing-cli'",
            checkpointId: "session-existing-cli",
            source: "agent-hook",
            autoResume: true
        )

        let startupInput = try #require(binding.startupInput)
        #expect(startupInput.contains("'\(executable.path)'"), "\(startupInput)")
    }

    @Test func agentHookSurfaceResumeStartupInputFallsBackWhenRecordedAgentExecutableMoved() throws {
        let fileManager = FileManager.default
        let root = fileManager.temporaryDirectory
            .appendingPathComponent("cmux-surface-resume-moved-agent-\(UUID().uuidString)", isDirectory: true)
        let bin = root.appendingPathComponent("bin", isDirectory: true)
        let cwd = root.appendingPathComponent("repo", isDirectory: true)
        let movedExecutable = root
            .appendingPathComponent(".nvm", isDirectory: true)
            .appendingPathComponent("versions", isDirectory: true)
            .appendingPathComponent("node", isDirectory: true)
            .appendingPathComponent("v24.2.0", isDirectory: true)
            .appendingPathComponent("bin", isDirectory: true)
            .appendingPathComponent("codex", isDirectory: false)
        let outputURL = root.appendingPathComponent("codex-output.txt", isDirectory: false)
        try fileManager.createDirectory(at: bin, withIntermediateDirectories: true)
        try fileManager.createDirectory(at: cwd, withIntermediateDirectories: true)
        defer { try? fileManager.removeItem(at: root) }

        let fakeCodex = bin.appendingPathComponent("codex", isDirectory: false)
        try """
        #!/bin/sh
        printf '%s|%s\\n' "$PWD" "$*" > "$CMUX_FAKE_CODEX_OUTPUT"
        """.write(to: fakeCodex, atomically: true, encoding: .utf8)
        try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: fakeCodex.path)

        let quotedCwd = "'\(cwd.path)'"
        let binding = SurfaceResumeBindingSnapshot(
            kind: "codex",
            command: "{ cd -- \(quotedCwd) 2>/dev/null || [ ! -d \(quotedCwd) ]; } && "
                + "'\(movedExecutable.path)' 'resume' 'session-moved-cli' '--yolo'",
            cwd: cwd.path,
            checkpointId: "session-moved-cli",
            source: "agent-hook",
            autoResume: true
        )
        let startupInput = try #require(binding.startupInput)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-fc", startupInput]
        process.environment = [
            "PATH": "\(bin.path):/usr/bin:/bin",
            "CMUX_FAKE_CODEX_OUTPUT": outputURL.path,
        ]
        let stderr = Pipe()
        process.standardError = stderr

        try runWithBoundedWait(process, shellDescription: "zsh -fc")

        let errorText = String(
            data: stderr.fileHandleForReading.readDataToEndOfFile(),
            encoding: .utf8
        ) ?? ""
        #expect(process.terminationStatus == 0, "\(errorText)")

        let output = try String(contentsOf: outputURL, encoding: .utf8)
        #expect(output == "\(cwd.path)|resume session-moved-cli -c check_for_update_on_startup=false --yolo\n")
        #expect(!startupInput.contains(movedExecutable.path), "\(startupInput)")
    }

    private struct ResumeShellTimeout: Error, CustomStringConvertible {
        let shellDescription: String
        let timeout: TimeInterval

        var description: String {
            "Resume shell (\(shellDescription)) did not exit within \(Int(timeout))s; treating as hung."
        }
    }

    private func runWithBoundedWait(
        _ process: Process,
        shellDescription: String,
        timeout: TimeInterval = 30
    ) throws {
        let exited = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in exited.signal() }
        try process.run()
        if exited.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            _ = exited.wait(timeout: .now() + 2)
            throw ResumeShellTimeout(shellDescription: shellDescription, timeout: timeout)
        }
    }

    private static func homeManagedExecutablePath(executableName: String, _ components: String...) -> String {
        localManagedExecutablePath(root: FileManager.default.homeDirectoryForCurrentUser, executableName: executableName, components)
    }

    private static func localManagedExecutablePath(
        root: URL,
        executableName: String,
        _ components: String...
    ) -> String {
        localManagedExecutablePath(root: root, executableName: executableName, components)
    }

    private static func localManagedExecutablePath(
        root: URL,
        executableName: String,
        _ components: [String]
    ) -> String {
        var directory = root
        for component in components {
            directory.appendPathComponent(component, isDirectory: true)
        }
        return directory.appendingPathComponent(executableName, isDirectory: false).path
    }
}


// Startup ownership follows the binding's exact generation, not scan timing.
@MainActor
@Suite(.serialized)
struct ProcessDetectedStartupOwnershipTests {
    @Test(arguments: ["tmux", "ssh"])
    func workspaceKeepsQueuedRestoreUntilMatchingChild(kind: String) throws {
        let workspace = Workspace()
        defer { workspace.teardownAllPanels() }
        var snapshot = workspace.sessionSnapshot(includeScrollback: false)
        let original = binding(kind: kind)
        snapshot.panels[0].terminal?.resumeBinding = original
        workspace.restoreSessionSnapshot(snapshot)
        let panelID = try #require(workspace.focusedPanelId)
        #expect(!workspace.restoredAgentLifecycle.ownsInFlightRestoredCommand(panelId: panelID))
        #expect(workspace.restoredAgentLifecycle.startupInput(panelId: panelID) == nil)
        for _ in 0..<8 {
            let saved = workspace.sessionSnapshot(
                includeScrollback: false,
                surfaceResumeBindingIndex: .empty
            )
            #expect(saved.panels.first?.terminal?.resumeBinding == original)
        }
        let detected = SurfaceResumeBindingIndex(bindingsByPanel: [
            .init(workspaceId: workspace.id, panelId: panelID): original
        ])
        _ = workspace.sessionSnapshot(includeScrollback: false, surfaceResumeBindingIndex: detected)
        #expect(workspace.surfaceResumeBinding(panelId: panelID)?.restoredProcessDetectionObservation == nil)
        _ = workspace.sessionSnapshot(includeScrollback: false, surfaceResumeBindingIndex: .empty)
        let exited = workspace.sessionSnapshot(includeScrollback: false, surfaceResumeBindingIndex: .empty)
        #expect(exited.panels.first?.terminal?.resumeBinding == nil)
    }

    @Test(arguments: [DockScope.workspace, .global])
    func dockKeepsTheSameQueuedRestoreAndRetiresSuccessfulDetach(scope: DockScope) throws {
        let source = Workspace()
        defer { source.teardownAllPanels() }
        var panel = try #require(source.sessionSnapshot(includeScrollback: false).panels.first)
        let original = binding()
        panel.terminal?.resumeBinding = original
        let dock = DockSplitStore(workspaceId: UUID(), scope: scope, baseDirectoryProvider: { "/tmp" })
        defer { dock.closeAllPanels() }
        let mapping = dock.restoreSessionSnapshot(SessionSplitContainerSnapshot(
            focusedPanelId: panel.id,
            layout: .pane(SessionPaneLayoutSnapshot(panelIds: [panel.id], selectedPanelId: panel.id)),
            panels: [panel]
        ))
        let panelID = try #require(mapping[panel.id])
        #expect(!dock.restoredAgentLifecycle.ownsInFlightRestoredCommand(panelId: panelID))
        #expect(dock.restoredAgentLifecycle.startupInput(panelId: panelID) == nil)
        for pass in 0..<8 {
            let saved = dock.sessionSnapshot(
                includeScrollback: false,
                surfaceResumeBindingIndex: pass.isMultiple(of: 2) ? .empty : nil
            )
            #expect(saved.panels.first?.terminal?.resumeBinding == original)
        }
        let detected = SurfaceResumeBindingIndex(bindingsByPanel: [
            .init(workspaceId: dock.workspaceId, panelId: panelID): original
        ])
        _ = dock.sessionSnapshot(includeScrollback: false, surfaceResumeBindingIndex: detected)
        let exited = dock.sessionSnapshot(includeScrollback: false, surfaceResumeBindingIndex: .empty)
        #expect(exited.panels.first?.terminal?.resumeBinding == nil)
    }

    @Test func initialPromptCannotCompleteAnUnissuedRestore() {
        let generation = UUID()
        var pending = binding()
        pending.armRestoredProcessDetectionObservation(terminalLifecycleID: generation)
        pending.observeRestoredProcessDetectionShellState(.promptIdle, terminalLifecycleID: generation)
        #expect(pending.restoredProcessDetectionObservation?.phase == .queued)
        for _ in 0..<10_000 {
            let keepPending = pending.reconcileRestoredProcessDetection(detected: nil, detectionAvailable: true)
            #expect(keepPending)
        }
        #expect(pending.autoResume == true)
    }

    @Test func aDifferentSocketSessionOrDirectoryCannotSettleThePendingRestore() {
        let original = binding()
        var pending = original
        pending.armRestoredProcessDetectionObservation(terminalLifecycleID: UUID())
        var wrongSocket = original
        wrongSocket.launchCommand?.arguments[2] = "other-socket"
        var wrongSession = original
        wrongSession.checkpointId = "other-session"
        var wrongDirectory = original
        wrongDirectory.cwd = "/other-directory"
        for other in [wrongSocket, wrongSession, wrongDirectory] {
            let keepPending = pending.reconcileRestoredProcessDetection(detected: other, detectionAvailable: true)
            #expect(keepPending)
            #expect(pending.restoredProcessDetectionObservation != nil)
        }
        var observed = original
        observed.updatedAt += 1
        observed.launchCommand?.capturedAt = 101
        let keepPendingAfterMatchingDetection = pending.reconcileRestoredProcessDetection(detected: observed, detectionAvailable: true)
        #expect(!keepPendingAfterMatchingDetection)
        #expect(pending.restoredProcessDetectionObservation == nil)
    }

    @Test func completedCommandKeepsAnExactManualRecovery() throws {
        let generation = UUID()
        let original = binding()
        var pending = original
        pending.armRestoredProcessDetectionObservation(terminalLifecycleID: generation)
        pending.observeRestoredProcessDetectionShellState(.commandRunning, terminalLifecycleID: generation)
        pending.observeRestoredProcessDetectionShellState(.promptIdle, terminalLifecycleID: generation)
        let keepManualRecovery = pending.reconcileRestoredProcessDetection(detected: nil, detectionAvailable: true)
        #expect(keepManualRecovery)
        #expect(pending.command == original.command)
        #expect(pending.launchCommand == original.launchCommand)
        #expect(pending.autoResume == false)
        #expect(pending.approvalPolicy == .manual)
        #expect(pending.approvalRecordId == nil)
        let decoded = try JSONDecoder().decode(
            SurfaceResumeBindingSnapshot.self,
            from: JSONEncoder().encode(pending)
        )
        var reloaded = decoded
        #expect(reloaded.restoredProcessDetectionObservation == nil)
        let keepReloadedManualRecovery = reloaded.reconcileRestoredProcessDetection(detected: nil, detectionAvailable: true)
        #expect(keepReloadedManualRecovery)
        #expect(reloaded.autoResume == false)
    }

    @Test func ephemeralOwnershipDoesNotChangePersistedEqualityOrJSON() throws {
        let original = binding()
        var pending = original
        pending.armRestoredProcessDetectionObservation(terminalLifecycleID: UUID())
        let encoder = JSONEncoder()
        encoder.outputFormatting = .sortedKeys
        #expect(pending == original)
        #expect(try encoder.encode(pending) == encoder.encode(original))
    }

    @Test func failureAndClearOnlyRetireTheMountedOriginalGeneration() throws {
        let workspace = Workspace()
        defer { workspace.teardownAllPanels() }
        var snapshot = workspace.sessionSnapshot(includeScrollback: false)
        let original = binding()
        snapshot.panels[0].terminal?.resumeBinding = original
        workspace.restoreSessionSnapshot(snapshot)
        let panelID = try #require(workspace.focusedPanelId)
        let terminal = try #require(workspace.panels[panelID] as? TerminalPanel)
        #expect(terminal.surface.onStartupRestoreAbandoned?(UUID()) == false)
        #expect(terminal.surface.onStartupRestoreAbandoned?(terminal.surface.terminalLifecycleId) == true)
        let manual = try #require(workspace.surfaceResumeBinding(panelId: panelID))
        #expect(manual.command == original.command)
        #expect(manual.launchCommand == original.launchCommand)
        #expect(manual.autoResume == false)
        #expect(manual.approvalPolicy == .manual)
        #expect(workspace.clearSurfaceResumeBinding(panelId: panelID))
        #expect(terminal.surface.onStartupRestoreAbandoned?(terminal.surface.terminalLifecycleId) == false)
    }

    @Test func explicitInputRetiresAutomaticOwnershipWithoutChangingTheSavedInvocation() throws {
        let workspace = Workspace()
        defer { workspace.teardownAllPanels() }
        var snapshot = workspace.sessionSnapshot(includeScrollback: false)
        let original = binding()
        snapshot.panels[0].terminal?.resumeBinding = original
        workspace.restoreSessionSnapshot(snapshot)
        let panelID = try #require(workspace.focusedPanelId)
        let terminal = try #require(workspace.panels[panelID] as? TerminalPanel)
        terminal.surface.onExplicitInput?()
        let manual = try #require(workspace.surfaceResumeBinding(panelId: panelID))
        #expect(manual.command == original.command)
        #expect(manual.launchCommand == original.launchCommand)
        #expect(manual.autoResume == false)
        #expect(manual.approvalPolicy == .manual)
    }

    @Test func publicCancellationRetiresAnOrdinaryWorkspaceRestore() throws {
        let workspace = Workspace()
        defer { workspace.teardownAllPanels() }
        let terminal = try stagedTerminal(in: workspace)
        let original = try #require(workspace.surfaceResumeBinding(panelId: terminal.id))
        terminal.surface.cancelStartupRestoreAdmission()
        let manual = try #require(workspace.surfaceResumeBinding(panelId: terminal.id))
        #expect(manual.command == original.command)
        #expect(manual.launchCommand == original.launchCommand)
        #expect(manual.autoResume == false)
        #expect(manual.approvalPolicy == .manual)
        workspace.updatePanelShellActivityState(panelId: terminal.id, state: .promptIdle)
        for _ in 0..<8 {
            _ = workspace.sessionSnapshot(includeScrollback: false, surfaceResumeBindingIndex: .empty)
        }
        #expect(workspace.surfaceResumeBinding(panelId: terminal.id)?.autoResume == false)
    }

    @Test(arguments: [DockScope.workspace, .global])
    func publicCancellationFollowsTheTransferredGenerationIntoEitherDock(scope: DockScope) throws {
        let source = Workspace()
        defer { source.teardownAllPanels() }
        let terminal = try stagedTerminal(in: source)
        let original = try #require(source.surfaceResumeBinding(panelId: terminal.id))
        let previousOwner = terminal.surface.onStartupRestoreAbandoned
        let transfer = try #require(source.detachSurface(panelId: terminal.id))
        let dock = DockSplitStore(workspaceId: UUID(), scope: scope, baseDirectoryProvider: { "/tmp" })
        defer { dock.closeAllPanels() }
        let pane = try #require(dock.bonsplitController.allPaneIds.first)
        let panelID = try #require(dock.attachDetachedSurface(transfer, inPane: pane, focus: false))
        #expect(previousOwner?(terminal.surface.terminalLifecycleId) == false)
        #expect(dock.surfaceResumeBindingsByPanelId[panelID]?.autoResume == true)
        terminal.surface.cancelStartupRestoreAdmission()
        let manual = try #require(dock.surfaceResumeBindingsByPanelId[panelID])
        #expect(manual.command == original.command)
        #expect(manual.launchCommand == original.launchCommand)
        #expect(manual.autoResume == false)
        #expect(manual.approvalPolicy == .manual)
        dock.updatePanelShellActivityState(panelId: panelID, state: .promptIdle)
        for _ in 0..<8 {
            _ = dock.sessionSnapshot(includeScrollback: false, surfaceResumeBindingIndex: .empty)
        }
        #expect(dock.surfaceResumeBindingsByPanelId[panelID]?.autoResume == false)
    }

    @Test(arguments: ["workspace", "workspaceDock", "globalDock"], ["tmux", "ssh"])
    func processRestoreCustodyFollowsEveryTransferIntoWorkspace(origin: String, kind: String) throws {
        for phase in ["queued", "manual", "persistedManual"] {
            let source = Workspace()
            defer { source.teardownAllPanels() }
            let destination = Workspace()
            defer { destination.teardownAllPanels() }
            let terminal = try stagedTerminal(in: source, kind: kind)
            if phase != "queued" {
                terminal.surface.cancelStartupRestoreAdmission()
            }
            if phase == "persistedManual" {
                let manual = try #require(source.surfaceResumeBinding(panelId: terminal.id))
                source.surfaceResumeBindingsByPanelId[terminal.id] = try JSONDecoder().decode(
                    SurfaceResumeBindingSnapshot.self,
                    from: JSONEncoder().encode(manual)
                )
            }
            let original = try #require(source.surfaceResumeBinding(panelId: terminal.id))
            let generation = terminal.surface.terminalLifecycleId
            var previousOwner = terminal.surface.onStartupRestoreAbandoned
            var transfer = try #require(source.detachSurface(panelId: terminal.id))
            var dock: DockSplitStore?
            defer { dock?.closeAllPanels() }
            if origin != "workspace" {
                let owner = DockSplitStore(
                    workspaceId: UUID(),
                    scope: origin == "globalDock" ? .global : .workspace,
                    baseDirectoryProvider: { "/tmp" }
                )
                dock = owner
                let pane = try #require(owner.bonsplitController.allPaneIds.first)
                let dockPanelID = try #require(owner.attachDetachedSurface(transfer, inPane: pane, focus: false))
                previousOwner = terminal.surface.onStartupRestoreAbandoned
                transfer = try #require(owner.detachSurface(panelId: dockPanelID))
            }
            let pane = try #require(destination.bonsplitController.allPaneIds.first)
            let panelID = try #require(destination.attachDetachedSurface(transfer, inPane: pane, focus: false))

            #expect(terminal.surface.terminalLifecycleId == generation)
            #expect(previousOwner?(generation) == false)
            let adopted = try #require(destination.surfaceResumeBinding(panelId: panelID))
            #expect(adopted == original)
            #expect(adopted.restoredProcessDetectionObservation == original.restoredProcessDetectionObservation)
            for _ in 0..<8 {
                _ = destination.sessionSnapshot(includeScrollback: false, surfaceResumeBindingIndex: .empty)
            }
            #expect(destination.surfaceResumeBinding(panelId: panelID) == original)
            if phase == "queued" {
                #expect(terminal.surface.onStartupRestoreAbandoned?(UUID()) == false)
                terminal.surface.cancelStartupRestoreAdmission()
            }
            let manual = try #require(destination.surfaceResumeBinding(panelId: panelID))
            #expect(manual.command == original.command)
            #expect(manual.launchCommand == original.launchCommand)
            #expect(manual.autoResume == false)
            #expect(manual.approvalPolicy == .manual)
            #expect(manual.approvalRecordId == nil)
        }
    }

    @Test(arguments: ["workspace", "workspaceDock", "globalDock"], ["tmux", "ssh"])
    func workspaceDoesNotAdoptUnownedAutomaticProcessBindings(origin: String, kind: String) throws {
        for ownership in ["unarmed", "foreignGeneration", "changedCommand"] {
            let source = Workspace()
            defer { source.teardownAllPanels() }
            let destination = Workspace()
            defer { destination.teardownAllPanels() }
            let terminal = try stagedTerminal(in: source, kind: kind)
            var unowned = binding(kind: kind)
            if ownership == "foreignGeneration" {
                unowned.armRestoredProcessDetectionObservation(terminalLifecycleID: UUID())
            } else if ownership == "changedCommand" {
                unowned = try #require(source.surfaceResumeBinding(panelId: terminal.id))
                unowned.command += " --changed-invocation"
            }
            source.surfaceResumeBindingsByPanelId[terminal.id] = unowned
            var transfer = try #require(source.detachSurface(panelId: terminal.id))
            var dock: DockSplitStore?
            defer { dock?.closeAllPanels() }
            if origin != "workspace" {
                let owner = DockSplitStore(
                    workspaceId: UUID(),
                    scope: origin == "globalDock" ? .global : .workspace,
                    baseDirectoryProvider: { "/tmp" }
                )
                dock = owner
                let pane = try #require(owner.bonsplitController.allPaneIds.first)
                let dockPanelID = try #require(owner.attachDetachedSurface(transfer, inPane: pane, focus: false))
                transfer = try #require(owner.detachSurface(panelId: dockPanelID))
            }
            let pane = try #require(destination.bonsplitController.allPaneIds.first)
            let panelID = try #require(destination.attachDetachedSurface(transfer, inPane: pane, focus: false))

            #expect(destination.surfaceResumeBinding(panelId: panelID) == nil)
            #expect(terminal.surface.onStartupRestoreAbandoned?(terminal.surface.terminalLifecycleId) == false)
        }
    }

    @Test(arguments: [DockScope.workspace, .global], ["tmux", "ssh"])
    func unownedDockProcessStillUsesItsManagedAgentFallback(scope: DockScope, kind: String) throws {
        for ownership in ["unarmed", "foreignGeneration", "changedCommand"] {
            let source = Workspace()
            defer { source.teardownAllPanels() }
            let destination = Workspace()
            defer { destination.teardownAllPanels() }
            let terminal = try stagedTerminal(in: source, kind: kind)
            let original = try #require(source.surfaceResumeBinding(panelId: terminal.id))
            let transfer = try #require(source.detachSurface(panelId: terminal.id))
            let dock = DockSplitStore(workspaceId: UUID(), scope: scope, baseDirectoryProvider: { "/tmp" })
            defer { dock.closeAllPanels() }
            let dockPane = try #require(dock.bonsplitController.allPaneIds.first)
            let panelID = try #require(dock.attachDetachedSurface(transfer, inPane: dockPane, focus: false))
            let managed = SurfaceResumeBindingSnapshot(
                kind: "custom-agent",
                command: "custom-agent --resume managed-original",
                cwd: "/tmp",
                checkpointId: "managed-original",
                source: "agent-hook",
                autoResume: false,
                approvalPolicy: .manual,
                updatedAt: 101
            )
            let acceptedManagedBinding = dock.setSurfaceResumeBinding(managed, panelId: panelID)
            #expect(acceptedManagedBinding)
            var unowned = binding(kind: kind)
            if ownership == "foreignGeneration" {
                unowned.armRestoredProcessDetectionObservation(terminalLifecycleID: UUID())
            } else if ownership == "changedCommand" {
                unowned = original
                unowned.command += " --changed-invocation"
            }
            dock.surfaceResumeBindingsByPanelId[panelID] = unowned
            let detached = try #require(dock.detachSurface(panelId: panelID))
            #expect(detached.resolvedManagedAgentResumeBinding == managed)
            let destinationPane = try #require(destination.bonsplitController.allPaneIds.first)
            let adoptedPanelID = try #require(destination.attachDetachedSurface(
                detached,
                inPane: destinationPane,
                focus: false
            ))

            #expect(destination.surfaceResumeBinding(panelId: adoptedPanelID) == managed)
            #expect(terminal.surface.onStartupRestoreAbandoned?(terminal.surface.terminalLifecycleId) == false)
        }
    }

    @Test(arguments: [DockScope.workspace, .global], ["missing", "unavailable"])
    func recoveryFreezeDowngradesEvenAnOwnedQueuedDockRestore(scope: DockScope, detection: String) throws {
        let source = Workspace()
        defer { source.teardownAllPanels() }
        let terminal = try stagedTerminal(in: source)
        var original = try #require(source.surfaceResumeBinding(panelId: terminal.id))
        original.approvalPolicy = .auto
        original.approvalRecordId = "previously-approved-owned-restore"
        source.surfaceResumeBindingsByPanelId[terminal.id] = original
        let transfer = try #require(source.detachSurface(panelId: terminal.id))
        let dock = DockSplitStore(workspaceId: UUID(), scope: scope, baseDirectoryProvider: { "/tmp" })
        defer { dock.closeAllPanels() }
        let pane = try #require(dock.bonsplitController.allPaneIds.first)
        let panelID = try #require(dock.attachDetachedSurface(transfer, inPane: pane, focus: false))
        let index: SurfaceResumeBindingIndex? = detection == "unavailable" ? .unavailable : nil
        let generation = terminal.surface.terminalLifecycleId
        #expect(terminal.surface.isAwaitingStartupRestoreAdmission)

        for forceManualRecovery in [false, true] {
            dock.surfaceResumeBindingsByPanelId[panelID] = original
            let snapshot = dock.sessionSnapshot(
                includeScrollback: false,
                surfaceResumeBindingIndex: index,
                downgradeStoredProcessDetectedResumeBindingsWhenDetectionUnavailable: forceManualRecovery,
                currentAgentProcessIdentity: { _ in nil },
                agentProcessPresence: { _ in .absent }
            )
            let saved = try #require(snapshot.panels.first(where: { $0.id == panelID })?.terminal?.resumeBinding)
            #expect(saved.command == original.command)
            #expect(saved.launchCommand == original.launchCommand)
            #expect(saved.autoResume == !forceManualRecovery)
            #expect(terminal.surface.terminalLifecycleId == generation)
            #expect(terminal.surface.isAwaitingStartupRestoreAdmission)
            if forceManualRecovery {
                #expect(saved.approvalPolicy == .manual)
                #expect(saved.approvalRecordId == nil)
                #expect(dock.surfaceResumeBindingsByPanelId[panelID] == saved)
                let later = dock.sessionSnapshot(includeScrollback: false, surfaceResumeBindingIndex: .empty)
                #expect(later.panels.first(where: { $0.id == panelID })?.terminal?.resumeBinding == saved)
            }
        }
    }

    @Test(arguments: [DockScope.workspace, .global])
    func recoveryFreezeStillUsesAnActuallyDetectedDockProcess(scope: DockScope) throws {
        let source = Workspace()
        defer { source.teardownAllPanels() }
        let terminal = try stagedTerminal(in: source)
        let original = try #require(source.surfaceResumeBinding(panelId: terminal.id))
        let transfer = try #require(source.detachSurface(panelId: terminal.id))
        let dock = DockSplitStore(workspaceId: UUID(), scope: scope, baseDirectoryProvider: { "/tmp" })
        defer { dock.closeAllPanels() }
        let pane = try #require(dock.bonsplitController.allPaneIds.first)
        let panelID = try #require(dock.attachDetachedSurface(transfer, inPane: pane, focus: false))
        let detected = SurfaceResumeBindingIndex(bindingsByPanel: [
            .init(workspaceId: dock.workspaceId, panelId: panelID): original
        ])

        let snapshot = dock.sessionSnapshot(
            includeScrollback: false,
            surfaceResumeBindingIndex: detected,
            downgradeStoredProcessDetectedResumeBindingsWhenDetectionUnavailable: true,
            currentAgentProcessIdentity: { _ in nil },
            agentProcessPresence: { _ in .absent }
        )
        let saved = try #require(snapshot.panels.first(where: { $0.id == panelID })?.terminal?.resumeBinding)
        #expect(saved == original)
        #expect(saved.autoResume == true)
        #expect(dock.surfaceResumeBindingsByPanelId[panelID]?.restoredProcessDetectionObservation == nil)
    }

    @Test func workspaceRejectsAnOldEventAfterTheSameSurfaceAdvancesGeneration() throws {
        let workspace = Workspace()
        defer { workspace.teardownAllPanels() }
        let terminal = try stagedTerminal(in: workspace)
        let surface = terminal.surface
        let previousGeneration = surface.terminalLifecycleId
        let original = try #require(workspace.surfaceResumeBinding(panelId: terminal.id))
        #expect(surface.suspendRuntimeSurfaceForAgentHibernation(reason: "restore-generation-regression"))
        #expect(terminal.surface === surface)
        #expect(surface.terminalLifecycleId != previousGeneration)
        #expect(surface.onStartupRestoreAbandoned?(previousGeneration) == false)
        #expect(workspace.surfaceResumeBinding(panelId: terminal.id)?.autoResume == true)
        var replacement = original
        replacement.updatedAt += 1
        replacement.armRestoredProcessDetectionObservation(terminalLifecycleID: surface.terminalLifecycleId)
        workspace.surfaceResumeBindingsByPanelId[terminal.id] = replacement
        #expect(surface.onStartupRestoreAbandoned?(surface.terminalLifecycleId) == true)
        #expect(workspace.surfaceResumeBinding(panelId: terminal.id)?.autoResume == false)
    }

    @Test(arguments: [DockScope.workspace, .global])
    func eitherDockRejectsAnOldEventOnItsReusedCurrentSurface(scope: DockScope) throws {
        let source = Workspace()
        defer { source.teardownAllPanels() }
        let terminal = try stagedTerminal(in: source)
        let original = try #require(source.surfaceResumeBinding(panelId: terminal.id))
        let transfer = try #require(source.detachSurface(panelId: terminal.id))
        let dock = DockSplitStore(workspaceId: UUID(), scope: scope, baseDirectoryProvider: { "/tmp" })
        defer { dock.closeAllPanels() }
        let pane = try #require(dock.bonsplitController.allPaneIds.first)
        let panelID = try #require(dock.attachDetachedSurface(transfer, inPane: pane, focus: false))
        let surface = terminal.surface
        let previousGeneration = surface.terminalLifecycleId
        #expect(surface.suspendRuntimeSurfaceForAgentHibernation(reason: "restore-generation-regression"))
        #expect(terminal.surface === surface)
        #expect(surface.terminalLifecycleId != previousGeneration)
        #expect(surface.onStartupRestoreAbandoned?(previousGeneration) == false)
        #expect(dock.surfaceResumeBindingsByPanelId[panelID]?.autoResume == true)
        var replacement = original
        replacement.updatedAt += 1
        replacement.armRestoredProcessDetectionObservation(terminalLifecycleID: surface.terminalLifecycleId)
        dock.surfaceResumeBindingsByPanelId[panelID] = replacement
        #expect(surface.onStartupRestoreAbandoned?(surface.terminalLifecycleId) == true)
        #expect(dock.surfaceResumeBindingsByPanelId[panelID]?.autoResume == false)
    }

    private func stagedTerminal(in workspace: Workspace, kind: String = "tmux") throws -> TerminalPanel {
        let pane = try #require(workspace.bonsplitController.allPaneIds.first)
        let terminal = try #require(workspace.newTerminalSurface(
            inPane: pane,
            focus: false,
            initialInput: "cmux restore --surface\n",
            runtimeSpawnPolicy: .pacedSessionRestore.requiringStartupRestoreAdmission()
        ))
        var pending = binding(kind: kind)
        pending.armRestoredProcessDetectionObservation(terminalLifecycleID: terminal.surface.terminalLifecycleId)
        workspace.surfaceResumeBindingsByPanelId[terminal.id] = pending
        return terminal
    }

    private func binding(kind: String = "tmux") -> SurfaceResumeBindingSnapshot {
        let argv = kind == "tmux"
            ? ["tmux", "-L", "cmux-owned-restore-regression", "attach", "-t", "cmux-owned-restore-regression"]
            : ["ssh", "cmux-owned-restore-regression.invalid"]
        return SurfaceResumeBindingSnapshot(
            kind: kind,
            command: argv.map(SurfaceResumeBindingSnapshot.shellSingleQuoted).joined(separator: " "),
            cwd: "/tmp",
            checkpointId: "cmux-owned-restore-regression",
            source: "process-detected",
            launchCommand: AgentLaunchCommandSnapshot(
                launcher: nil,
                executablePath: argv[0],
                arguments: argv,
                workingDirectory: "/tmp",
                capturedAt: 100,
                source: "process-detected"
            ),
            autoResume: true,
            updatedAt: 100
        )
    }
}
