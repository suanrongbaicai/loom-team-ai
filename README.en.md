# Loom

> When you put an AI assistant into a team with real people, permissions, and responsibility, the hard part is not “can it run.” **What is this answer based on? Is it allowed to know this? Can next week pick up last week’s work? How do people approve a dangerous action?**
>
> LOOM is a framework already running in a real team, published with identifying details removed. Three record layers, a permission check on every citation, and proposal-and-vote answer those four questions.

[简体中文](README.md) · English · [License: Apache 2.0](LICENSE)

## How one message moves

![How one message moves](docs/images/architecture.svg)

## The design

- **Context**: the current understanding, with a source. Changing it needs a basis, and the old version stays. A conclusion formed in a private chat can be shared to a group as that conclusion alone, after the person confirms.
- **CL**: the course of a matter, with a citation on each passage. The model names the material, version, and quote it actually used. The host checks permission and the original text before the link exists.
- **Data**: the original bytes, kept with name, format, digest, source, and the scope in which they were obtained. A link is not the same as having read the original.
- **Methods are skills**, with versions. Something taught in conversation starts as a candidate and applies to everyone only after approval.
- **The model proposes a citation. The host checks it**: whether it was actually read this turn, who can see it, and whether the quote and version match.
- **Permission and rule changes go through a proposal.** A person votes with their platform identity. When it passes, it takes effect in the same transaction. The model cannot vote, and it cannot make a rule take effect by itself.
- **Platform identity is the real identity.** The sender of a Feishu or WeChat message decides who is speaking. The model does not get to declare that.

## How to use this repository

Use it as a reference and adapt it to your team. The seats, the connected platforms, and who approves changes come from the original team. Hand the repository to an AI and have it adapt `theory/` and `framework/`.

| Directory | What is there |
|---|---|
| `theory/` | Why it is designed this way |
| `framework/` | The current implementation: records, permissions, tasks, model adapters, Feishu and WeChat, delivery, and search |
| `docs/` | Status, sharing, and limits. What is actually built is in [docs/status.md](docs/status.md) |
| `examples/` | An offline synthetic demo of records and receipts |

## Videos

- [For teams](docs/videos/loom-for-teams.mp4): the problem Loom is for, and how it helps a team continue the work.
- [For technical readers](docs/videos/loom-technical-breakdown.mp4): the three record layers, where skills sit, and the path of one message from arrival to sending.

Names and data in the videos are synthetic.

## What it holds

- A reply is saved before it is sent. If delivery is uncertain, it stays unknown and is not resent under a new id.
- A skill has three separate facts: the file exists, a version was approved, and that version is actually loaded.
- The design lives in `theory/`. The implementation lives in `framework/`. A requirement written down is not the same as code that already does it.

## Who it is for

- People putting an AI assistant into a team and getting stuck on permission, evidence, and picking work up later
- People building agent products who need citations and permissions
- People studying how humans and AI share responsibility

Real identities, chats, configuration, and secrets are not in this repository. The full WeChat adapter is installed separately.

## Contributing

Issues and changes are welcome. Read [how to take part](CONTRIBUTING.md) first. That guide is in Chinese.

The project's own material is under the [Apache 2.0](LICENSE) license. Third-party material keeps its own terms. See [NOTICE](NOTICE).
