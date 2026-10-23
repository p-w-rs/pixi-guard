# Security

pixi-guard is a guardrail. It keeps Claude's habits on pixi; it is not a
sandbox, and a command made to hide what it runs can get past it. The known
gaps are in [docs/dev/LIMITS.md](docs/dev/LIMITS.md).

## Reporting a problem

Report it privately through GitHub's vulnerability reporting:
<https://github.com/p-w-rs/pixi-guard/security/advisories/new> (the
repository's **Security** tab, **Report a vulnerability**). Please don't
describe it in a public issue. If the **Report a vulnerability** button is
missing, open an issue that only asks for a private contact, with no
details.

Include what Claude ran (the command or the tool's input), what you expected
and what happened, and the versions of pixi-guard, Claude Code, your OS and
the shell Claude Code uses.

## In scope

- A common habit that slips through: an ordinary way Claude runs Python,
  pip or another Python tool outside pixi that the hook lets through without
  asking.
- An ordinary command that makes the hook fail or time out. The hook lets a
  command through when it fails, so that is a slip too.
- The hook running a program from the command it checks, or writing outside
  its own folders.

## Out of scope

- The forms listed in [docs/dev/LIMITS.md](docs/dev/LIMITS.md), and commands
  built to hide what they run.
- What Claude Code, checkpoint or the gateway do with a command after the
  hook has answered.

## Supported versions

Only the latest release gets fixes. To update:
`claude plugin marketplace update pixi-guard`, then
`claude plugin update pixi-guard@pixi-guard`.
