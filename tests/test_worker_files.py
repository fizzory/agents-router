"""The agt-parent read tools: a worker that may not run shell can still read the job's folders, and nothing else.

w5 (2026-11-20): a researcher on Codex had only shell and apply_patch, its rules forbade shell, so it asked
the parent for file access, nobody answered, and it wrote nothing.
"""

import io
import json
import os
import threading

from helpers import TempHome

from agents_router import jobs, mcp_server, messages, tools
from agents_router.runtimes.base import JobSpec
from agents_router.tools import ToolError


class WorkerFileToolTests(TempHome):
    def setUp(self):
        super().setUp()
        self.extra = self.root / "extra"
        self.extra.mkdir()
        self.job = jobs.new_job_id()
        folder = jobs.job_dir(self.job)
        folder.mkdir(parents=True)
        spec = JobSpec(job_id=self.job, runtime="codex", prompt="task", cwd=str(self.project), access="read",
                       add_dirs=[str(self.extra)])
        jobs.write_json(folder / "spec.json", spec.to_dict())
        (self.project / "docs").mkdir()
        (self.project / "docs" / "brief.md").write_text("# Brief\nwrite twelve fragments\nframes: 23\n", encoding="utf-8")
        (self.project / "notes.txt").write_text("".join(f"line {n}\n" for n in range(1, 11)), encoding="utf-8")
        (self.extra / "contract.md").write_text("the review contract\n", encoding="utf-8")
        (self.root / "secret.txt").write_text("outside the job\n", encoding="utf-8")

    def call(self, name, **arguments):
        return tools.call(name, arguments, worker_job=self.job)

    def refused(self, name, needle, **arguments):
        with self.assertRaises(ToolError) as caught:
            self.call(name, **arguments)
        self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    # read_file ---------------------------------------------------------------------------

    def test_read_file_returns_numbered_lines_from_the_job_folder(self):
        result = self.call("read_file", path="docs/brief.md")
        self.assertEqual(result["path"], "docs/brief.md")
        self.assertEqual(result["content"], "1\t# Brief\n2\twrite twelve fragments\n3\tframes: 23")
        self.assertEqual((result["start_line"], result["end_line"]), (1, 3))
        self.assertNotIn("more", result)

    def test_read_file_pages_through_a_long_file(self):
        first = self.call("read_file", path="notes.txt", limit=4)
        self.assertEqual(first["content"].splitlines()[-1], "4\tline 4")
        self.assertTrue(first["more"])
        self.assertIn("offset=5", first["note"])
        rest = self.call("read_file", path="notes.txt", offset=5)
        self.assertEqual((rest["start_line"], rest["end_line"]), (5, 10))
        self.assertNotIn("more", rest)

    def test_read_file_reaches_an_added_folder_by_absolute_path(self):
        result = self.call("read_file", path=str(self.extra / "contract.md"))
        self.assertEqual(result["content"], "1\tthe review contract")
        self.assertEqual(result["path"], str(self.extra / "contract.md"))

    def test_paths_outside_the_job_folders_are_refused(self):
        self.refused("read_file", "outside the folders this job may read", path="../secret.txt")
        self.refused("read_file", "outside the folders this job may read", path=str(self.root / "secret.txt"))
        self.refused("read_file", "outside the folders this job may read", path="/etc/hosts")
        self.refused("list_files", "outside the folders this job may read", path="..")
        self.refused("search_files", "outside the folders this job may read", pattern="outside", path="..")

    def test_a_symlink_that_leaves_the_job_folders_is_refused_and_hidden(self):
        os.symlink(self.root / "secret.txt", self.project / "link.txt")
        os.symlink(self.root, self.project / "up")
        self.refused("read_file", "outside the folders this job may read", path="link.txt")
        self.refused("read_file", "outside the folders this job may read", path="up/secret.txt")
        entries = self.call("list_files")["entries"]
        self.assertNotIn("link.txt", entries)
        self.assertNotIn("up/", entries)
        found = self.call("search_files", pattern="outside the job")
        self.assertEqual(found["matches"], [])

    def test_a_symlink_inside_the_job_folders_is_read(self):
        os.symlink(self.project / "notes.txt", self.project / "alias.txt")
        self.assertEqual(self.call("read_file", path="alias.txt", limit=1)["content"], "1\tline 1")

    def test_folders_binaries_and_missing_files_are_refused_with_a_reason(self):
        (self.project / "blob.bin").write_bytes(b"\x00\x01\x02")
        self.refused("read_file", "is a folder; use list_files", path="docs")
        self.refused("read_file", "is a binary file", path="blob.bin")
        self.refused("read_file", "no such file or folder: nope.md", path="nope.md")

    def test_a_very_long_line_is_cut(self):
        (self.project / "min.js").write_text("x" * 50_000 + "\n", encoding="utf-8")
        content = self.call("read_file", path="min.js")["content"]
        self.assertLess(len(content), 2100)
        self.assertTrue(content.endswith("…[line cut]"))

    def test_the_line_after_a_cut_line_is_read_whole(self):
        (self.project / "min.js").write_text("x" * 200_000 + "\nsecond line\n", encoding="utf-8")
        lines = self.call("read_file", path="min.js")["content"].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("…[line cut]"))
        self.assertEqual(lines[1], "2\tsecond line")
        self.assertEqual(self.call("search_files", pattern="second", path="min.js")["matches"][0]["line"], 2)

    def test_a_fifo_is_refused_and_never_blocks_the_server(self):
        """Opening a FIFO waits for a writer that never comes: the agt-parent server would hang."""

        fifo = self.project / "pipe"
        os.mkfifo(fifo)
        self.addCleanup(self.unblock, fifo)
        results = {}

        def calls():
            try:
                self.call("read_file", path="pipe")
            except ToolError as exc:
                results["read"] = str(exc)
            results["search"] = self.call("search_files", pattern="frames")
            try:
                self.call("search_files", pattern="frames", path="pipe")
            except ToolError as exc:
                results["target"] = str(exc)

        thread = threading.Thread(target=calls, daemon=True)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "a tool call is stuck opening the FIFO")
        self.assertEqual(results["read"], "pipe is not a regular file")
        self.assertEqual([item["path"] for item in results["search"]["matches"]], ["docs/brief.md"])
        self.assertEqual(results["target"], "pipe is not a regular file")

    @staticmethod
    def unblock(fifo):
        """Let a reader stuck on the FIFO see end of file, so a failing run can still exit."""

        for _ in range(3):
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                return

    # list_files ----------------------------------------------------------------------------

    def test_list_files_shows_a_folder_and_matches_a_pattern_at_any_depth(self):
        (self.project / ".git").mkdir()
        (self.project / ".git" / "HEAD.md").write_text("ref\n", encoding="utf-8")
        self.assertEqual(self.call("list_files")["entries"], [".git/", "docs/", "notes.txt"])
        self.assertEqual(self.call("list_files", pattern="*.md")["entries"], ["docs/brief.md"])
        self.assertEqual(self.call("list_files", pattern="**/*.md")["entries"], ["docs/brief.md"])
        self.assertEqual(self.call("list_files", path="docs")["entries"], ["brief.md"])

    # search_files --------------------------------------------------------------------------

    def test_search_files_returns_paths_and_line_numbers(self):
        result = self.call("search_files", pattern=r"frames: \d+")
        self.assertEqual(result["matches"], [{"path": "docs/brief.md", "line": 3, "text": "frames: 23"}])
        self.assertEqual(self.call("search_files", pattern="BRIEF", ignore_case=True)["matches"][0]["line"], 1)
        self.assertEqual(self.call("search_files", pattern="line", glob="*.md")["matches"], [])
        self.assertEqual(self.call("search_files", pattern="review", path=str(self.extra))["matches"],
                         [{"path": str(self.extra / "contract.md"), "line": 1, "text": "the review contract"}])

    def test_search_files_skips_heavy_folders_unless_asked_and_caps_results(self):
        (self.project / "node_modules" / "pkg").mkdir(parents=True)
        (self.project / "node_modules" / "pkg" / "index.js").write_text("frames: 99\n", encoding="utf-8")
        self.assertEqual([item["path"] for item in self.call("search_files", pattern="frames")["matches"]],
                         ["docs/brief.md"])
        inside = self.call("search_files", pattern="frames", path="node_modules")["matches"]
        self.assertEqual([item["path"] for item in inside], ["node_modules/pkg/index.js"])
        capped = self.call("search_files", pattern="line", max_results=3)
        self.assertEqual(len(capped["matches"]), 3)
        self.assertEqual(capped["truncated"], "more than 3 matches")

    def test_search_names_the_files_too_big_to_search(self):
        (self.project / "dump.log").write_text("frames: 7\n" * 250_000, encoding="utf-8")
        result = self.call("search_files", pattern="frames")
        self.assertEqual([item["path"] for item in result["matches"]], ["docs/brief.md"])
        self.assertEqual(result["skipped"], "not searched, larger than 2 MB (read them in parts with read_file): dump.log")
        self.refused("search_files", "dump.log is larger than 2 MB, too big to search; read it in parts with read_file",
                     pattern="frames", path="dump.log")

    def test_a_bad_regular_expression_is_refused(self):
        self.refused("search_files", "not a valid regular expression", pattern="(")

    # how the job counts them -------------------------------------------------------------

    def test_reading_counts_as_work_and_messaging_does_not(self):
        """`blocked` looks for a worker that did nothing but wait: reading the brief is doing the task."""

        self.call("read_file", path="docs/brief.md")
        self.call("search_files", pattern="frames")
        self.call("list_files")
        self.assertEqual(messages.parent_calls(self.job), 0)
        self.call("check_parent_messages")
        self.assertEqual(messages.parent_calls(self.job), 1)

    def test_the_worker_server_offers_and_runs_the_read_tools(self):
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "read_file", "arguments": {"path": "docs/brief.md", "limit": 1}}}]
        stdout = io.BytesIO()
        mcp_server.serve(io.BytesIO(b"".join((json.dumps(item) + "\n").encode() for item in lines)), stdout,
                         worker_job=self.job)
        replies = {item["id"]: item for item in map(json.loads, stdout.getvalue().decode().splitlines())}
        names = [tool["name"] for tool in replies[1]["result"]["tools"]]
        self.assertTrue({"read_file", "list_files", "search_files"} <= set(names), names)
        self.assertFalse(replies[2]["result"].get("isError"), replies[2])
        self.assertIn("1\\t# Brief", replies[2]["result"]["content"][0]["text"])


if __name__ == "__main__":
    import unittest

    unittest.main()
