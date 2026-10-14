# Loom

[简体中文](README.md) · English

[![License: Apache 2.0](docs/images/license-apache-2.0.svg)](LICENSE)

A reference framework for people putting AI to work inside a real team. It comes from a codebase already in use, with identifying details removed before publication. There is no one-click deploy. To use it, pull the repository and adapt it to your own team. This repository is actively maintained.

A day later, or in another conversation, what was said, what was changed, and why it was done that way should still be findable. A reply is not done because the model says so. It needs a saved record and an actual delivery receipt.

## Where records go

The three layers are three drawers. They decide how something is stored. Knowledge kinds are labels on the content. They decide what kind of knowledge it is. The two do not replace each other.

| Layer | What it stores |
|---|---|
| Data | Original messages, actual actions, and results |
| CL | The course of one matter, and the material actually used each time |
| Context | The current understanding, with a source |

| Kind | Where it goes |
|---|---|
| Method | A skill, with a version. Something taught in conversation starts as a candidate and applies to everyone only after approval |
| Decision | Context. A conclusion formed in a private chat can be shared to a group as that conclusion alone, after the person confirms |
| Personal preference | Used only when replying to that person |
| Volatile fact | Left at the source, with a date to check again |
| In progress | Left in the course of the matter, not written into Context |

The model decides what something means. The program checks identity, permission, version, and the actual action. A check that passes does not mean the understanding is correct.

## How one message moves

![Loom architecture](docs/images/architecture.svg)

## Videos

- [For teams](docs/videos/loom-for-teams.mp4): the problem Loom is for, and how it helps a team continue the work.
- [For technical readers](docs/videos/loom-technical-breakdown.mp4): the three record layers, where skills sit, and the path of one message from arrival to sending.

Names and data in the videos are synthetic.

## How to use this repository

Use it as a reference, not as an installable package. The number of seats, the connected platforms, and who approves changes all come from the original team. Another team has to change those. There is no separate simple deploy procedure.

Hand the repository to an AI and have it adapt `theory/` and `framework/` to your team. Real identities, chats, configuration, and runtime data are not in this repository.

| Directory | What is there |
|---|---|
| `theory/` | Why it is designed this way |
| `framework/` | The current implementation |
| `docs/` | Status, sharing premises, and limits |

The project's own material is under the [Apache 2.0](LICENSE) license. Third-party material keeps its own terms. See [NOTICE](NOTICE).

## Contributing

Issues and changes are welcome. Read [how to take part](CONTRIBUTING.md) first. That guide is in Chinese.
