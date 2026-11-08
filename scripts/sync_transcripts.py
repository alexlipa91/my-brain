#!/usr/bin/env python3
"""Push Claude Code transcripts to the installer's private GitHub repo.

The repo name is ``<github-login>-my-brain``. A SessionEnd hook commits the
session that just ended. A SessionStart hook creates the repo if needed and
installs a timer that commits any new transcript text. Files already in the
repo are never deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

MARKER = "claude-transcripts-sync"
LAUNCHD_LABEL = "com.alexlipa91.claude-transcripts"
DEFAULT_INTERVAL = 60
REPO_DESCRIPTION = "Private Claude Code transcripts written by the my-brain plugin."


@dataclass
class Config:
    interval_minutes: int
    projects_dir: str
    plugin_root: str
    data_dir: str
    path: str
    home: str

    def as_dict(self) -> dict:
        return {
            "interval_minutes": self.interval_minutes,
            "projects_dir": self.projects_dir,
            "plugin_root": self.plugin_root,
            "data_dir": self.data_dir,
            "path": self.path,
            "home": self.home,
        }


@dataclass
class GitHubUser:
    login: str
    name: str
    email: str


def parse_interval(value: object, default: int = DEFAULT_INTERVAL) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(float(str(value)))
    except ValueError:
        return default
    return min(1440, max(15, parsed))


def brain_repo_name(login: str) -> str | None:
    login = login.strip()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login):
        return None
    return f"{login}-my-brain"


def github_email(user_id: object, login: str) -> str:
    if user_id is None or str(user_id).strip() == "":
        return f"{login}@users.noreply.github.com"
    return f"{user_id}+{login}@users.noreply.github.com"


def default_projects_dir() -> str:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(config_dir, "projects")


def config_from_env() -> Config:
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser(
        "~/.claude/plugins/data/transcripts"
    )
    return Config(
        interval_minutes=parse_interval(os.environ.get("CLAUDE_PLUGIN_OPTION_INTERVAL_MINUTES", "")),
        projects_dir=default_projects_dir(),
        plugin_root=plugin_root,
        data_dir=data_dir,
        path=os.environ.get("PATH", ""),
        home=os.environ.get("HOME", os.path.expanduser("~")),
    )


def config_from_file(path: str) -> Config:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return Config(
        interval_minutes=parse_interval(raw.get("interval_minutes", DEFAULT_INTERVAL)),
        projects_dir=str(raw.get("projects_dir") or default_projects_dir()),
        plugin_root=str(raw.get("plugin_root") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        data_dir=str(raw.get("data_dir") or os.path.dirname(path)),
        path=str(raw.get("path", "")),
        home=str(raw.get("home") or os.path.expanduser("~")),
    )


def read_hook_payload() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def log(data_dir: str, message: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "sync.log")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def command_env(config: Config) -> dict[str, str]:
    env = os.environ.copy()
    if config.path:
        env["PATH"] = config.path
    if config.home:
        env["HOME"] = config.home
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def run_command(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, env=env, check=False)


def block_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text", "")).strip():
            parts.append(str(block["text"]).strip())
        elif block.get("type") == "tool_use" and block.get("name"):
            parts.append(f"[tool] {block['name']}")
    return "\n\n".join(parts)


def render_markdown(raw: str, title: str, project: str) -> str:
    turns: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("isMeta"):
            continue
        kind = event.get("type")
        if kind not in ("user", "assistant"):
            continue
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        text = block_text(message.get("content"))
        if not text:
            continue
        label = "User" if kind == "user" else "Assistant"
        turns.append((label, text))
    if not turns:
        return ""
    body = [f"# {title}", "", f"Project: `{project}`", ""]
    for label, text in turns:
        body.extend([f"## {label}", "", text, ""])
    return "\n".join(body).rstrip() + "\n"


def repo_relative(projects_dir: str, transcript_path: str) -> str | None:
    if not transcript_path.endswith(".jsonl"):
        return None
    transcript_real = os.path.realpath(transcript_path)
    projects_real = os.path.realpath(projects_dir)
    try:
        relative = os.path.relpath(transcript_real, projects_real)
    except ValueError:
        return None
    if relative.startswith("..") or os.path.isabs(relative):
        return None
    markdown = relative[: -len(".jsonl")] + ".md"
    return os.path.join("sessions", markdown.replace(os.sep, "/"))


def find_transcripts(projects_dir: str) -> list[str]:
    found: list[str] = []
    if not os.path.isdir(projects_dir):
        return found
    for root, _dirs, files in os.walk(projects_dir):
        for name in files:
            if name.endswith(".jsonl"):
                found.append(os.path.join(root, name))
    return sorted(found)


def write_if_changed(path: str, text: str) -> bool:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def export_transcripts(projects_dir: str, checkout: str, only_path: str | None = None) -> int:
    paths = [only_path] if only_path else find_transcripts(projects_dir)
    written = 0
    for transcript in paths:
        if not transcript or not os.path.isfile(transcript):
            continue
        relative = repo_relative(projects_dir, transcript)
        if relative is None:
            continue
        with open(transcript, encoding="utf-8", errors="replace") as handle:
            rendered = render_markdown(
                handle.read(),
                title=os.path.splitext(os.path.basename(transcript))[0],
                project=relative.split("/")[1] if relative.count("/") >= 2 else "session",
            )
        if not rendered:
            continue
        if write_if_changed(os.path.join(checkout, relative), rendered):
            written += 1
    return written


def current_github_user(env: dict[str, str]) -> GitHubUser | None:
    if shutil.which("gh", path=env.get("PATH") or None) is None:
        return None
    result = run_command(["gh", "api", "user"], env)
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    login = str(payload.get("login", "")).strip()
    repo = brain_repo_name(login)
    if repo is None:
        return None
    name = str(payload.get("name") or login).strip() or login
    return GitHubUser(login=login, name=name, email=github_email(payload.get("id"), login))


def ensure_checkout(config: Config, user: GitHubUser, env: dict[str, str]) -> str | None:
    repo = brain_repo_name(user.login)
    if repo is None:
        return None
    slug = f"{user.login}/{repo}"
    checkout = os.path.join(config.data_dir, "checkout")
    if os.path.isdir(os.path.join(checkout, ".git")):
        pulled = run_command(["git", "-C", checkout, "pull", "--rebase", "--autostash"], env)
        if pulled.returncode != 0:
            detail = (pulled.stderr or pulled.stdout).strip()
            log(config.data_dir, f"git pull failed: {detail}")
            return None
        return checkout

    viewed = run_command(["gh", "repo", "view", slug, "--json", "name"], env)
    if viewed.returncode != 0:
        created = run_command(
            [
                "gh",
                "repo",
                "create",
                repo,
                "--private",
                "--description",
                REPO_DESCRIPTION,
                "--add-readme",
            ],
            env,
        )
        if created.returncode != 0:
            detail = (created.stderr or created.stdout).strip()
            log(config.data_dir, f"could not create {slug}: {detail}")
            return None
        log(config.data_dir, f"created private repo {slug}")

    os.makedirs(config.data_dir, exist_ok=True)
    cloned = run_command(["gh", "repo", "clone", slug, checkout], env)
    if cloned.returncode != 0:
        time.sleep(2)
        cloned = run_command(["gh", "repo", "clone", slug, checkout], env)
    if cloned.returncode != 0:
        detail = (cloned.stderr or cloned.stdout).strip()
        log(config.data_dir, f"clone failed: {detail}")
        return None
    return checkout


def commit_transcripts(checkout: str, user: GitHubUser, env: dict[str, str], push: bool = True) -> bool:
    added = run_command(["git", "-C", checkout, "add", "--", "sessions"], env)
    if added.returncode != 0:
        return False
    staged = run_command(["git", "-C", checkout, "diff", "--cached", "--quiet"], env)
    if staged.returncode == 0:
        return False
    committed = run_command(
        [
            "git",
            "-C",
            checkout,
            "-c",
            f"user.name={user.name}",
            "-c",
            f"user.email={user.email}",
            "commit",
            "-m",
            "Sync Claude Code transcripts",
        ],
        env,
    )
    if committed.returncode != 0:
        return False
    if not push:
        return True
    pushed = run_command(["git", "-C", checkout, "push", "origin", "HEAD"], env)
    if pushed.returncode == 0:
        return True
    run_command(["git", "-C", checkout, "pull", "--rebase", "--autostash"], env)
    retried = run_command(["git", "-C", checkout, "push", "origin", "HEAD"], env)
    return retried.returncode == 0


def sync_transcripts(config: Config, only_path: str | None = None, push: bool = True) -> int:
    env = command_env(config)
    user = current_github_user(env)
    if user is None:
        log(config.data_dir, "gh is not logged in; transcripts were not pushed")
        return 0
    checkout = ensure_checkout(config, user, env)
    if checkout is None:
        return 0
    export_transcripts(config.projects_dir, checkout, only_path)
    if commit_transcripts(checkout, user, env, push=push):
        repo = brain_repo_name(user.login)
        log(config.data_dir, f"pushed transcripts to {user.login}/{repo}")
    return 0


def cron_expression(minutes: int) -> str:
    minutes = parse_interval(minutes)
    if minutes < 60:
        return f"*/{minutes} * * * *"
    if minutes < 1440 and minutes % 60 == 0:
        return f"0 */{minutes // 60} * * *"
    if minutes >= 1440:
        return "0 0 * * *"
    return "0 * * * *"


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def timer_shell_command(config: Config) -> str:
    script = os.path.join(config.plugin_root, "scripts", "sync_transcripts.py")
    config_path = os.path.join(config.data_dir, "config.json")
    return " ".join(
        [
            f"PATH={shell_quote(config.path)}",
            f"HOME={shell_quote(config.home)}",
            shell_quote(sys.executable),
            shell_quote(script),
            "--all",
            "--config",
            shell_quote(config_path),
        ]
    )


def render_crontab(existing: str, schedule: str, command: str, marker: str = MARKER) -> str:
    comment = f"# {marker}"
    kept: list[str] = []
    skip_command = False
    for line in existing.splitlines():
        if line.strip() == comment:
            skip_command = True
            continue
        if skip_command:
            skip_command = False
            continue
        if marker in line:
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    block = [comment, f"{schedule} {command}"]
    body = kept + ([""] if kept else []) + block
    return "\n".join(body) + "\n"


def launchd_plist(config: Config) -> dict:
    script = os.path.join(config.plugin_root, "scripts", "sync_transcripts.py")
    config_path = os.path.join(config.data_dir, "config.json")
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, script, "--all", "--config", config_path],
        "StartInterval": config.interval_minutes * 60,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": config.path, "HOME": config.home},
    }


def write_config(config: Config) -> None:
    os.makedirs(config.data_dir, exist_ok=True)
    path = os.path.join(config.data_dir, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(config.as_dict(), indent=2) + "\n")
    os.chmod(path, 0o600)


def install_timer(config: Config) -> None:
    env = command_env(config)
    user = current_github_user(env)
    if user is None:
        log(config.data_dir, "timer not installed: gh is not logged in")
        return
    if ensure_checkout(config, user, env) is None:
        log(config.data_dir, "timer not installed: brain repo is not available")
        return
    write_config(config)
    changed = install_launchd(config) if sys.platform == "darwin" else install_cron(config)
    if changed:
        log(config.data_dir, f"timer installed, every {config.interval_minutes} minutes")
        spawn_sync(config)
    else:
        log(config.data_dir, "timer already installed")


def install_launchd(config: Config) -> bool:
    agents = os.path.join(config.home, "Library", "LaunchAgents")
    os.makedirs(agents, exist_ok=True)
    path = os.path.join(agents, f"{LAUNCHD_LABEL}.plist")
    payload = plistlib.dumps(launchd_plist(config), sort_keys=False)
    previous = b""
    if os.path.exists(path):
        with open(path, "rb") as handle:
            previous = handle.read()
    if previous == payload:
        return False
    with open(path, "wb") as handle:
        handle.write(payload)
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LAUNCHD_LABEL}"
    run_command(["launchctl", "bootout", target])
    result = run_command(["launchctl", "bootstrap", domain, path])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        log(config.data_dir, f"launchctl bootstrap failed: {detail}")
    return True


def install_cron(config: Config) -> bool:
    if shutil.which("crontab") is None:
        log(config.data_dir, "crontab is not installed; only session-end push will run")
        return False
    listed = run_command(["crontab", "-l"])
    existing = listed.stdout if listed.returncode == 0 else ""
    rendered = render_crontab(existing, cron_expression(config.interval_minutes), timer_shell_command(config))
    if rendered.strip() == existing.strip():
        return False
    result = subprocess.run(["crontab", "-"], input=rendered, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        log(config.data_dir, f"crontab install failed: {detail}")
        return False
    return True


def spawn_sync(config: Config) -> None:
    script = os.path.join(config.plugin_root, "scripts", "sync_transcripts.py")
    config_path = os.path.join(config.data_dir, "config.json")
    log_path = os.path.join(config.data_dir, "sync.log")
    log_handle = open(log_path, "a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, script, "--all", "--config", config_path],
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=log_handle,
        start_new_session=True,
        env=command_env(config),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push Claude Code transcripts to a private GitHub repo")
    parser.add_argument("--all", action="store_true", help="Sync every local transcript")
    parser.add_argument("--install-timer", action="store_true", help="Create the brain repo and install the timer")
    parser.add_argument("--config", help="Config file written for the timer")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    payload = read_hook_payload()
    config = config_from_file(args.config) if args.config else config_from_env()
    if args.install_timer:
        install_timer(config)
        return 0
    only = None if args.all else payload.get("transcript_path")
    if only is None and not args.all and payload.get("hook_event_name"):
        return 0
    return sync_transcripts(config, only_path=None if args.all else (str(only) if only else None))


if __name__ == "__main__":
    sys.exit(main())
