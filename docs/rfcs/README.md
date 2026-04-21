# Loom RFCs

Design proposals for substantive changes to loom's public surface. The template-less, lightweight kind — short enough to read, explicit enough to argue with.

## Lifecycle

- **Draft** — actively being written or debated.
- **Accepted** — agreed direction; implementation welcome.
- **Implemented** — landed in a released version; RFC becomes historical.
- **Rejected** — explicitly declined; kept for context.
- **Discussion** — open question, not ready to become an implementation spec.

## Current RFCs

| # | Title | Status | Target | Summary |
|---|---|---|---|---|
| [0001](0001-http-tool-pre-request-hook.md) | `HttpCallTool` pre-request hook | Draft | 0.3 | Optional async hook for URL rewriting, header/credential injection. Unblocks consumers composing on top of `HttpCallTool`. |
| [0002](0002-credentials-and-appliers.md) | Credentials, Appliers, and Policies | Implemented | 0.3–0.4 | Three-layer credential subsystem: typed secrets, transport-agnostic appliers, HITL-gated policies. Phase C adds KeychainStore, SigV4Applier, JwtBearerApplier, and ACL hook. |
| [0003](0003-ssh-tool.md) | `SshCallTool` (asyncssh) | Implemented | 0.3 | Agent tool for running commands over SSH. Depends on RFC 0002 for auth. Ships under `loom[ssh]` extras. |
| [0004](0004-sse-event-shape-convergence.md) | SSE event shape convergence | Discussion | TBD | Open question about whether/how to enrich loom's streaming events so consumers don't need translation layers. Requires cross-project alignment. |

## Sequencing

0001 can land independently and is a standalone improvement.

0002 is the foundation. 0003 depends on 0002 and should not land before it. After 0002 lands, helyx can migrate its credential store and delete ~500 LOC; after 0003, helyx gains SSH access for free.

0004 is a discussion, not an implementation plan. It depends on agreement from other loom consumers (nexus) before any code moves.

## Contributing an RFC

- File naming: `NNNN-slug.md`, four-digit zero-padded.
- Start with: Status, Author, Target release, Dependencies.
- Required sections: Summary, Motivation, Non-goals, Design, Backward compatibility, Risks and tradeoffs, Test plan, Open questions.
- Short > thorough. An RFC nobody reads is worse than no RFC. Aim for a reader to get the shape in five minutes.
