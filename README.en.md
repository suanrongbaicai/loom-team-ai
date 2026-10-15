# Loom

> When you put an AI assistant into a team with real people, permissions, and responsibility, the hard part is not “can it run.” **What is this answer based on? Is it allowed to know this? Can next week pick up last week’s work? How do people approve a dangerous action?**
>
> LOOM is a framework already running in a real team, published with identifying details removed. Three record layers, a permission check on every citation, and proposal-and-vote answer those four questions.

[简体中文](README.md) · English · [License: Apache 2.0](LICENSE)

## How one message moves

![How one message moves](docs/images/architecture.svg)

## The design, in three minutes

- **Context**: the team’s settled conclusions and shared understanding. They can be proposed, voted on, and shared into a group, with the version and the basis frozen.
- **CL**: the course of a matter, passage by passage. Each passage must name the material id, version, and quote actually used. The host checks permission and the original text. The model does not get to decide that by itself.
- **Data**: the original bytes, with name, format, digest, source, and the scope in which they were obtained. If they were not fully obtained, that is recorded as such. A link is not the same as having read the original.
- **A citation is a claim. The host checks it.** The model says which passage it used. The host checks that it was actually read this turn, who can see it now, and that the quote and version match. Only then does the link exist.
- **Governance instead of a back office.** Permission and rule changes do not go through an administrator editing config. They go through a proposal, a vote by a real platform identity, and take effect in the same transaction. The model has no vote and no tool that makes a rule take effect.
- **Platform identity is the real identity.** The sender of a Feishu or WeChat message decides who is speaking. The model does not get to declare that.

## How to use this repository

Use it as a reference and adapt it. It is not an installable package. The seats, the connected platforms, and the approval rules come from the original team; another team changes them. Hand the repository to an AI and have it adapt `theory/` and `framework/` to your team.

| Directory | What is there |
|---|---|
| `theory/` | Why it is designed this way. Written in Chinese, and dense. Start here. |
| `framework/` | The current implementation: records, permissions, tasks, model adapters, Feishu and WeChat, delivery, and search |
| `docs/` | Getting started, status, architecture, and limits. What is actually built is in [docs/status.md](docs/status.md) |
| `examples/` | An offline MockModel / MockTransport demo |

## Videos

- [For teams](docs/videos/loom-for-teams.mp4): the problem Loom is for, and how it helps a team continue the work.
- [For technical readers](docs/videos/loom-technical-breakdown.mp4): the three record layers, where skills sit, and the path of one message from arrival to sending.

Names and data in the videos are synthetic.

## Track record

- Windows offline check, 986 tests: 960 passed, 25 skipped, 1 expected failure, zero failures and zero errors
- An independent Linux review: the core 70 tests all passed
- A file existing, a version being approved, and that version actually being loaded are three separate facts. Uncertain delivery stays unknown and is not resent.
- What the design says should be done, and what the code already does, live in different directories. Neither stands in for the other.

## Current limits

There is no one-click deploy. Real identities, chats, configuration, and runtime data are not shipped with the repository. The full WeChat adapter is a separate prerequisite. What is built and what is not is in [docs/status.md](docs/status.md).

## Who it is for

- People deploying an AI assistant into a team and getting stuck on permission, audit, and continuity
- People building agent products who need citations and permissions
- People studying how humans and AI share responsibility

## Contributing

Issues and changes are welcome. Read [how to take part](CONTRIBUTING.md) first. That guide is in Chinese.

<details>
<summary>What was removed before publication</summary>

- Real names, team brands and places, accounts, group ids, message ids, contact details, and assessments of members
- Chats, production databases, real images and attachments, business reports, receipts, logs, and field evidence
- Deployment configuration, subscription logins, passwords, tokens, keys, and real secret values
- Private handoff notes, redaction maps, install directories, caches, and the original commit history

`runtime.sqlite`, Data, and group message ids in the code are implementation concepts or synthetic test entry points, not production data.
</details>

The project's own material is under the [Apache 2.0](LICENSE) license. Third-party material keeps its own terms. See [NOTICE](NOTICE).
