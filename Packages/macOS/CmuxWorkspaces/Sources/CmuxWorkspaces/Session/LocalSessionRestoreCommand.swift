/// A validated, canonical local-session attach command.
///
/// Construction accepts only the closed tmux and zellij command shapes emitted
/// by the local-session owners. This value does not execute commands, discover
/// sessions, or grant restore approval.
///
/// ```swift
/// let restorable = LocalSessionRestoreCommand(capturedCommand)
/// let command = restorable?.command
/// ```
public struct LocalSessionRestoreCommand: Sendable {
    /// The canonical command accepted by a local-session restore policy.
    public let command: String

    /// Validates a captured command using the existing local-session policies.
    ///
    /// - Parameter rawCommand: A captured startup command, or `nil` when absent.
    /// - Returns: `nil` unless a local-session policy accepts the command.
    public init?(_ rawCommand: String?) {
        guard let command = LocalTmuxRestoreCommandPolicy().restorableCommand(rawCommand)
            ?? LocalZellijRestoreCommandPolicy().restorableCommand(rawCommand) else {
            return nil
        }
        self.command = command
    }
}
