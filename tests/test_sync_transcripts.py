import importlib.util
import os
import plistlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


def load_module():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "sync_transcripts.py",
    )
    spec = importlib.util.spec_from_file_location("sync_transcripts", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync = load_module()

SAMPLE = textwrap.dedent(
    """\
    {"type":"user","isMeta":true,"message":{"role":"user","content":"ignore me"}}
    {"type":"user","message":{"role":"user","content":"Where are the transcripts?"},"timestamp":"2026-11-08T10:00:00Z"}
    {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"In your brain repo."},{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}
    {"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"secret tool output"}]}}
    """
)


class RepoNameTest(unittest.TestCase):
    def test_repo_is_named_after_the_github_login(self):
        self.assertEqual(sync.brain_repo_name("alexlipa91"), "alexlipa91-my-brain")
        self.assertEqual(sync.github_email(123, "alexlipa91"), "123+alexlipa91@users.noreply.github.com")

    def test_rejects_a_login_that_cannot_be_a_repo_name(self):
        self.assertIsNone(sync.brain_repo_name(""))
        self.assertIsNone(sync.brain_repo_name("../etc"))
        self.assertIsNone(sync.brain_repo_name("-leading"))


class MarkdownTest(unittest.TestCase):
    def test_render_keeps_conversation_text_and_drops_tool_output(self):
        rendered = sync.render_markdown(SAMPLE, "session-1", "-Users-ada-src-app")
        self.assertIn("# session-1", rendered)
        self.assertIn("Where are the transcripts?", rendered)
        self.assertIn("In your brain repo.", rendered)
        self.assertIn("[tool] Bash", rendered)
        self.assertNotIn("ignore me", rendered)
        self.assertNotIn("secret tool output", rendered)

    def test_empty_transcript_is_not_written(self):
        self.assertEqual(sync.render_markdown("{}\n", "session-1", "app"), "")

    def test_paths_stay_inside_the_sessions_tree(self):
        inside = sync.repo_relative(
            "/Users/ada/.claude/projects",
            "/Users/ada/.claude/projects/-Users-ada-src-app/session-1.jsonl",
        )
        outside = sync.repo_relative("/Users/ada/.claude/projects", "/tmp/session-1.jsonl")
        self.assertEqual(inside, "sessions/-Users-ada-src-app/session-1.md")
        self.assertIsNone(outside)


class PublishTest(unittest.TestCase):
    def test_commit_adds_the_session_and_a_second_run_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "projects", "-Users-ada-src-app")
            os.makedirs(projects)
            transcript = os.path.join(projects, "session-1.jsonl")
            with open(transcript, "w", encoding="utf-8") as handle:
                handle.write(SAMPLE)
            checkout = os.path.join(tmp, "checkout")
            os.makedirs(checkout)
            subprocess.run(["git", "init", "-b", "main", checkout], check=True, capture_output=True)
            user = sync.GitHubUser("ada", "Ada", "1+ada@users.noreply.github.com")
            written = sync.export_transcripts(os.path.join(tmp, "projects"), checkout)
            self.assertEqual(written, 1)
            self.assertTrue(sync.commit_transcripts(checkout, user, os.environ.copy(), push=False))
            self.assertFalse(sync.commit_transcripts(checkout, user, os.environ.copy(), push=False))
            saved = os.path.join(checkout, "sessions", "-Users-ada-src-app", "session-1.md")
            with open(saved, encoding="utf-8") as handle:
                self.assertIn("Where are the transcripts?", handle.read())

    def test_timer_command_syncs_without_deleting(self):
        config = sync.Config(60, "/projects", "/plugin", "/data", "/usr/bin", "/Users/ada")
        plist = sync.launchd_plist(config)
        self.assertEqual(
            plistlib.loads(plistlib.dumps(plist))["ProgramArguments"][-3:],
            ["--all", "--config", "/data/config.json"],
        )
        self.assertNotIn("clean", " ".join(plist["ProgramArguments"]))
        existing = "0 4 * * * /usr/bin/backup\n# claude-transcripts-sync\n0 * * * * old\n"
        rendered = sync.render_crontab(existing, "0 */1 * * *", "/usr/bin/python3 sync --all")
        self.assertIn("0 4 * * * /usr/bin/backup", rendered)
        self.assertNotIn("old", rendered)


if __name__ == "__main__":
    unittest.main()
