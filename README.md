# my-brain

Claude Code plugin that pushes session transcripts to a private GitHub repository named after the person who installed it: `https://github.com/<login>-my-brain`.

This repository is the plugin. Transcripts are not committed here. Each installer needs [GitHub CLI](https://cli.github.com/) logged in (`gh auth login`). The plugin creates `<login>-my-brain` as a private repo on first use.

The copy in git is the conversation text. Tool output stays on the machine that ran the session. Sessions already pushed are left in the repo when Claude Code later deletes its local transcripts.

## Install

In a Claude Code session:

```text
/plugin marketplace add alexlipa91/my-brain
/plugin install transcripts@my-brain
```

Choose **Install for you**.

From a shell:

```bash
claude plugin install transcripts --marketplace alexlipa91/my-brain --scope user
```

## What runs

- The next session start creates `<login>-my-brain` if it does not exist, and installs a timer for this user.
- When a session ends, that session is committed and pushed.
- The timer runs hourly and pushes any transcript text that is not in the repo yet. On a Mac this is a LaunchAgent. Elsewhere it is a crontab entry marked `claude-transcripts-sync`.

A log is written to `~/.claude/plugins/data/transcripts/sync.log`.
