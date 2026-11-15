"""Plugin parts for Kiro: one source per plugin, the rendered mirror, and commands turned into skills."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from helpers import ShareHome, set_times
from test_share_plan import MacCopy
from test_share_sync import YES

from agents_router.config import load_config
from agents_router.share import apply, commands, frontmatter, plugin_parts, providers, scope, settings


def tree_hashes(folder: Path) -> dict[str, str]:
    """Every file under `folder` (links not followed) and its sha256, or a link's target."""

    found = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if path.is_symlink():
            found[relative] = "link:" + os.readlink(path)
        elif path.is_file():
            found[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


class Installs:
    """Installing a plugin into a Claude folder of a `ShareHome`."""

    def install(self, folder: Path, plugin_id: str, version: str, *, enabled=True, scope="user", files=None) -> Path:
        """Install a plugin the way Claude Code keeps it: a versioned cache folder, the inventory, enabledPlugins."""

        name, _, market = plugin_id.partition("@")
        cache = folder / "plugins" / "cache" / market / name / version
        for relative, content in (files if files is not None else {"skills/fmt/SKILL.md": "---\nname: fmt\ndescription: Formats.\n---\nGo.\n"}).items():
            path = cache / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        cache.mkdir(parents=True, exist_ok=True)
        inventory_path = folder / "plugins" / "installed_plugins.json"
        inventory = json.loads(inventory_path.read_text()) if inventory_path.exists() else {"version": 2, "plugins": {}}
        entry = {"scope": scope, "installPath": str(cache), "version": version}
        if scope != "user":
            entry["projectPath"] = str(self.root)
        inventory["plugins"][plugin_id] = [entry]
        inventory_path.write_text(json.dumps(inventory))
        settings_path = folder / "settings.json"
        values = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        if enabled is not None:
            values.setdefault("enabledPlugins", {})[plugin_id] = enabled
        settings_path.write_text(json.dumps(values))
        return cache


class PartsBase(Installs, ShareHome):
    def setUp(self):
        super().setUp()
        self.ntb = self.folder(".claude-ntb")
        self.claude = self.folder(".claude")
        self.state = self.agt_home / "state" / "share" / "plugin-parts"
        self.folders = [self.provider(self.ntb), self.provider(self.claude)]

    def provider(self, folder: Path, cli: str = "claude") -> providers.Provider:
        return providers.Provider(cli=cli, folder=folder, label=providers.base_label(cli, folder), installed=True,
                                  locations=providers.locations(cli, folder, self.user))

    def chosen(self, folders=None):
        selection = plugin_parts.select(folders if folders is not None else self.folders)
        return {plugin_id: (source.label, source.version) for plugin_id, source in selection.sources.items()}


class SourceTests(PartsBase):
    """Step 1: one source per plugin."""

    def test_newest_of_two_folders_wins(self):
        self.install(self.ntb, "infra@ntbx", "1.9.0")
        newest = self.install(self.claude, "infra@ntbx", "1.10.0")  # older as text, newer as a version
        self.assertEqual(self.chosen(), {"infra@ntbx": ("claude", "1.10.0")})
        self.assertEqual(plugin_parts.select(self.folders).sources["infra@ntbx"].root, newest)

    def test_equal_versions_take_folder_order(self):
        self.install(self.ntb, "infra@ntbx", "1.6.1")
        self.install(self.claude, "infra@ntbx", "1.6.1")
        self.assertEqual(self.chosen(), {"infra@ntbx": ("claude-ntb", "1.6.1")})
        self.assertEqual(self.chosen(list(reversed(self.folders))), {"infra@ntbx": ("claude", "1.6.1")})

    def test_version_order(self):
        key = plugin_parts.version_key
        self.assertLess(key("2.0.0-beta.2"), key("2.0.0"))
        self.assertLess(key("2.0.0-beta.2"), key("2.0.0-beta.10"))
        self.assertEqual(key("1.2"), key("1.2.0"))
        self.assertLess(key("a1b2c3d"), key("0.0.1"))
        self.assertEqual(key("a1b2c3d"), key("unknown"))  # neither is a version: folder order decides

    def test_turned_off_everywhere_gives_none(self):
        self.install(self.ntb, "infra@ntbx", "1.6.1", enabled=False)
        self.install(self.claude, "infra@ntbx", "1.4.0", enabled=None)  # installed, but not listed as on
        self.assertEqual(self.chosen(), {})

    def test_on_in_one_folder_only_takes_that_one(self):
        self.install(self.ntb, "infra@ntbx", "1.6.1", enabled=False)
        self.install(self.claude, "infra@ntbx", "1.4.0")
        self.assertEqual(self.chosen(), {"infra@ntbx": ("claude", "1.4.0")})

    def test_project_scope_only_gives_none(self):
        self.install(self.ntb, "infra@ntbx", "1.6.1", scope="project")
        self.install(self.claude, "infra@ntbx", "1.6.1", scope="local")
        self.assertEqual(self.chosen(), {})

    def test_agents_router_gives_none(self):
        self.install(self.ntb, "agents-router@agents-router", "0.17.1")
        self.assertEqual(self.chosen(), {})

    def test_unreadable_inventory_and_missing_files_are_not_gone(self):
        self.install(self.ntb, "infra@ntbx", "1.6.1")
        (self.ntb / "plugins" / "installed_plugins.json").write_text("{half")
        cache = self.install(self.claude, "deck@tools", "2.0.0")
        shutil.rmtree(cache)
        selection = plugin_parts.select(self.folders)
        self.assertEqual(selection.sources, {})
        self.assertIn("claude-ntb", selection.unreadable)
        self.assertIn("deck@tools", selection.held)

    def test_only_claude_folders_are_read(self):
        self.install(self.ntb, "infra@ntbx", "1.6.1")
        kiro = providers.Provider(cli="kiro", folder=self.folder(".kiro"), label="kiro", installed=True)
        self.assertEqual(self.chosen([kiro, *self.folders]), {"infra@ntbx": ("claude-ntb", "1.6.1")})


SKILL = """---
name: s
description: Plans things.
---
Run `${CLAUDE_PLUGIN_ROOT}/hooks/x.sh` or `$CLAUDE_PLUGIN_ROOT/hooks/x.sh`.
Read ${CLAUDE_SKILL_DIR}/references/r.md.
Keep ${CLAUDE_SKILL_DIR:+x} and $CLAUDE_PLUGIN_ROOT_EXTRA as written.
Then run ../../scripts/a.sh.
Linux: [`../../scripts/a.sh`](../../scripts/a.sh), see ./references/r.md.
Not ../../scripts/none.sh, https://example.com/../../scripts/a.sh or ../../../outside.md.
"""


class MirrorTests(PartsBase):
    """Step 2: the rendered mirror."""

    def setUp(self):
        super().setUp()
        self.cache = self.install(self.ntb, "mentor@private", "6.1.0", files={
            "skills/s/SKILL.md": SKILL,
            "skills/s/references/r.md": "See $CLAUDE_SKILL_DIR/SKILL.md and ${CLAUDE_PLUGIN_ROOT}/scripts/a.sh.\r\n",
            "skills/s/references/up.md": "Back to [the skill](../SKILL.md).\n",
            "skills/taste-skill/SKILL.md": "---\nname: design-taste-frontend\ndescription: Taste.\n---\nLook.\n",
            "skills/taste-skill/logo.png": bytes(range(256)) + b"${CLAUDE_PLUGIN_ROOT}",
            "hooks/x.sh": "#!/bin/sh\necho ${CLAUDE_PLUGIN_ROOT}\n",
            "scripts/a.sh": "#!/bin/sh\n",
            "docs/notes.md": "Outside skills: ${CLAUDE_PLUGIN_ROOT} stays.\n",
            "README.md": "${CLAUDE_PLUGIN_ROOT}\n",
            ".git/config": "[core]\n",
        })
        self.mirror = plugin_parts.mirror_path(self.state, "mentor@private")

    def build(self, previous=None, **kwargs):
        selection = plugin_parts.select(self.folders)
        return plugin_parts.refresh(selection.sources["mentor@private"], self.state, previous, **kwargs)

    def test_variables_become_mirror_paths_and_layout_is_kept(self):
        before = tree_hashes(self.cache)
        result = self.build()
        self.assertIsNone(result.failed)
        self.assertTrue(result.rebuilt)
        self.assertEqual(result.mirror, self.state / "private" / "mentor")
        self.assert_inside(result.mirror)
        mirror = result.mirror.as_posix()
        text = (result.mirror / "skills" / "s" / "SKILL.md").read_text()
        self.assertIn(f"`{mirror}/hooks/x.sh` or `{mirror}/hooks/x.sh`", text)
        self.assertTrue((result.mirror / "hooks" / "x.sh").is_file())
        self.assertIn(f"Read {mirror}/skills/s/references/r.md.", text)
        self.assertIn("Keep ${CLAUDE_SKILL_DIR:+x} and $CLAUDE_PLUGIN_ROOT_EXTRA as written.", text)
        self.assertTrue(os.path.isfile(result.mirror / "skills" / "s" / "../../scripts/a.sh"))
        # Relative paths that name something in the mirror become absolute, read from the file's own folder.
        self.assertIn(f"Then run {mirror}/scripts/a.sh.", text)
        self.assertIn(f"Linux: [`{mirror}/scripts/a.sh`]({mirror}/scripts/a.sh), see {mirror}/skills/s/references/r.md.",
                      text)
        self.assertTrue((result.mirror / "scripts" / "a.sh").is_file())
        self.assertEqual((result.mirror / "skills" / "s" / "references" / "up.md").read_text(),
                         f"Back to [the skill]({mirror}/skills/s/SKILL.md).\n")
        # A missing path, a URL and a path outside the mirror stay as written.
        self.assertIn("Not ../../scripts/none.sh, https://example.com/../../scripts/a.sh or ../../../outside.md.", text)
        # A reference file takes its skill's folder, and keeps its line endings.
        self.assertEqual((result.mirror / "skills" / "s" / "references" / "r.md").read_bytes(),
                         f"See {mirror}/skills/s/SKILL.md and {mirror}/scripts/a.sh.\r\n".encode())
        # Outside skills/ and in other files, nothing changes: the bytes are the source's.
        for relative in ("skills/taste-skill/logo.png", "hooks/x.sh", "docs/notes.md", "README.md"):
            self.assertEqual((result.mirror / relative).read_bytes(), (self.cache / relative).read_bytes(), relative)
        self.assertFalse((result.mirror / ".git").exists())
        self.assertEqual(tree_hashes(self.cache), before)  # the plugin cache is only read
        self.assertEqual([(part.name, part.folder) for part in result.skills],
                         [("design-taste-frontend", result.mirror / "skills" / "taste-skill"),
                          ("s", result.mirror / "skills" / "s")])
        self.assertEqual(result.record["source"], "claude-ntb")
        self.assertEqual(result.record["version"], "6.1.0")
        self.assertTrue(result.record["digest"])
        self.assertEqual([path.name for path in self.state.joinpath("private").iterdir()], ["mentor"])  # no temp left

    def test_a_relative_path_out_of_the_mirror_stays_even_when_it_exists(self):
        outside = self.state / "private" / "outside.md"  # where ../../../outside.md lands from the build's skills/s
        outside.parent.mkdir(parents=True)
        outside.write_text("Not the plugin's.\n")
        result = self.build()
        self.assertIsNone(result.failed)
        self.assertIn("or ../../../outside.md.", (result.mirror / "skills" / "s" / "SKILL.md").read_text())

    def test_a_path_of_dots_only_stays_as_written(self):
        """`cd ../..` in a shell snippet means the reader's own folder, never the mirror's."""

        text = "Run `cd ../.. && ls ../` here, or ./ alone; cd ../... ends a sentence.\n"
        cache = self.install(self.claude, "dots@tools", "1.0.0", files={
            "skills/d/SKILL.md": "---\nname: d\ndescription: Dots.\n---\n" + text,
            "commands/go.md": "---\ndescription: Go\n---\n" + text,
            "scripts/a.sh": "#!/bin/sh\n",
        })
        result = plugin_parts.refresh(plugin_parts.select(self.folders).sources["dots@tools"], self.state)
        self.assertIsNone(result.failed)
        self.assertTrue(cache.is_dir())
        self.assertIn(text, (result.mirror / "skills" / "d" / "SKILL.md").read_text())
        self.assertIn(text, (result.mirror / ".agt-commands" / "go" / "SKILL.md").read_text())

    def test_a_linked_markdown_file_is_never_written_through(self):
        try:
            os.symlink(self.cache / "docs" / "notes.md", self.cache / "skills" / "s" / "notes.md")
        except OSError:
            self.skipTest("symlinks need extra rights on this system")
        before = tree_hashes(self.cache)
        result = self.build()
        self.assertIsNone(result.failed)
        self.assertTrue((result.mirror / "skills" / "s" / "notes.md").is_symlink())  # copied as a link, not rendered
        self.assertEqual(tree_hashes(self.cache), before)

    def test_steady_state_builds_and_hashes_nothing(self):
        first = self.build()
        with patch.object(plugin_parts, "_digest", wraps=plugin_parts._digest) as digest, \
                patch.object(plugin_parts, "_copy_file", wraps=plugin_parts._copy_file) as copy:
            second = self.build(first.record)
        self.assertFalse(second.rebuilt)
        digest.assert_not_called()
        copy.assert_not_called()
        self.assertEqual([part.name for part in second.skills], ["design-taste-frontend", "s"])
        self.assertEqual(second.record, first.record)

    def test_a_touched_mirror_file_is_hashed_but_not_rebuilt(self):
        first = self.build()
        set_times(first.mirror / "hooks" / "x.sh", 1_000_000)
        second = self.build(first.record, backup=lambda folder: self.fail("nothing was edited"))
        self.assertFalse(second.rebuilt)
        self.assertNotEqual(second.record["mirror_signature"], first.record["mirror_signature"])
        third = self.build(second.record)
        self.assertFalse(third.rebuilt)

    def test_a_source_change_rebuilds_in_place(self):
        first = self.build()
        (self.cache / "scripts" / "a.sh").write_text("#!/bin/sh\necho new\n")
        set_times(self.cache / "scripts" / "a.sh", 2_000_000)
        second = self.build(first.record)
        self.assertTrue(second.rebuilt)
        self.assertFalse(second.edited)
        self.assertEqual(second.mirror, first.mirror)
        self.assertEqual((second.mirror / "scripts" / "a.sh").read_text(), "#!/bin/sh\necho new\n")
        self.assertNotEqual(second.record["digest"], first.record["digest"])

    def test_a_version_bump_with_the_old_folder_deleted_keeps_the_path(self):
        first = self.build()
        files = {"skills/s/SKILL.md": SKILL.replace("Plans things.", "Plans better."), "hooks/x.sh": "#!/bin/sh\n",
                 "scripts/a.sh": "#!/bin/sh\n"}
        self.install(self.ntb, "mentor@private", "6.2.0", files=files)
        shutil.rmtree(self.cache)
        second = self.build(first.record)
        self.assertTrue(second.rebuilt)
        self.assertEqual(second.mirror, first.mirror)
        self.assertEqual(second.record["version"], "6.2.0")
        self.assertIn("Plans better.", (second.mirror / "skills" / "s" / "SKILL.md").read_text())
        self.assertIn(f"{second.mirror.as_posix()}/hooks/x.sh", (second.mirror / "skills" / "s" / "SKILL.md").read_text())

    def test_a_failed_rebuild_leaves_the_old_mirror_whole(self):
        first = self.build()
        old = tree_hashes(first.mirror)
        (self.cache / "scripts" / "a.sh").write_text("#!/bin/sh\necho new\n")
        set_times(self.cache / "scripts" / "a.sh", 2_000_000)
        seen = []
        copy = plugin_parts._copy_file

        def interrupted(source, destination):
            seen.append(tree_hashes(first.mirror))  # what a reader of the mirror sees mid-build
            if len(seen) == 3:
                raise OSError("disk full")
            return copy(source, destination)

        with patch.object(plugin_parts, "_copy_file", interrupted):
            failed = self.build(first.record)
        self.assertIn("disk full", failed.failed)
        self.assertFalse(failed.rebuilt)
        self.assertEqual(failed.record, first.record)
        self.assertTrue(seen and all(state == old for state in seen))
        self.assertEqual(tree_hashes(first.mirror), old)
        self.assertEqual([path.name for path in self.state.joinpath("private").iterdir()], ["mentor"])
        # The next run builds it.
        second = self.build(first.record)
        self.assertTrue(second.rebuilt)
        self.assertEqual((second.mirror / "scripts" / "a.sh").read_text(), "#!/bin/sh\necho new\n")

    def test_an_edited_mirror_goes_to_backup_then_is_restored(self):
        first = self.build()
        skill = first.mirror / "skills" / "s" / "SKILL.md"
        original = skill.read_bytes()
        skill.write_text("edited in Kiro\n")
        kept = []

        def backup(folder):
            kept.append((folder / "skills" / "s" / "SKILL.md").read_text())

        second = self.build(first.record, backup=backup)
        self.assertTrue(second.rebuilt)
        self.assertTrue(second.edited)
        self.assertEqual(kept, ["edited in Kiro\n"])
        self.assertEqual(skill.read_bytes(), original)
        self.assertEqual(second.record["digest"], first.record["digest"])

    def test_a_mirror_without_a_record_goes_to_backup(self):
        self.build()
        kept = []
        result = self.build(None, backup=kept.append)
        self.assertTrue(result.edited)
        self.assertEqual(len(kept), 1)

    def test_a_left_over_build_is_swept(self):
        first = self.build()
        stale = first.mirror.with_name(".mentor.abc.tmp")
        stale.mkdir()
        (self.cache / "scripts" / "a.sh").write_text("changed\n")
        set_times(self.cache / "scripts" / "a.sh", 2_000_000)
        self.build(first.record)
        self.assertFalse(stale.exists())

    def test_links_inside_the_plugin_lead_into_the_mirror_and_links_out_are_left_out(self):
        outside = self.root / "outside.md"
        outside.write_text("Not the plugin's.\n")
        sibling = self.cache.parent / "6.0.0" / "old.md"  # an older version's folder next to this one
        sibling.parent.mkdir(parents=True)
        sibling.write_text("Old.\n")
        links = {"skills/s/abs.sh": self.cache / "scripts" / "a.sh", "skills/s/rel.sh": "../../scripts/a.sh",
                 "skills/s/out.md": outside, "skills/s/sibling.md": "../../../6.0.0/old.md"}
        try:
            for relative, target in links.items():
                os.symlink(target, self.cache / relative)
        except OSError:
            self.skipTest("symlinks need extra rights on this system")
        before = tree_hashes(self.cache)
        result = self.build()
        self.assertIsNone(result.failed)
        mirror = result.mirror.resolve()
        absolute = result.mirror / "skills" / "s" / "abs.sh"
        self.assertTrue(absolute.is_symlink())
        self.assertTrue(absolute.resolve().is_relative_to(mirror), os.readlink(absolute))  # never into the cache
        self.assertEqual(absolute.read_text(), "#!/bin/sh\n")
        self.assertEqual(os.readlink(result.mirror / "skills" / "s" / "rel.sh"), "../../scripts/a.sh")  # as it was
        for name in ("out.md", "sibling.md"):  # out of the plugin: left out, and said so
            self.assertFalse(os.path.lexists(result.mirror / "skills" / "s" / name), name)
            self.assertTrue(any(f"skills/s/{name}" in line for line in result.reports), result.reports)
        self.assertEqual(sorted(entry["name"] for entry in result.record["left_out"] if entry["part"] == "link"),
                         ["skills/s/out.md", "skills/s/sibling.md"])
        absolute.write_text("edited in Kiro\n")  # an edit through the link stays in the mirror
        self.assertEqual(tree_hashes(self.cache), before)

    def test_an_edit_made_during_a_rebuild_is_backed_up(self):
        first = self.build()
        skill = first.mirror / "skills" / "s" / "SKILL.md"
        (self.cache / "scripts" / "a.sh").write_text("#!/bin/sh\necho new\n")
        set_times(self.cache / "scripts" / "a.sh", 2_000_000)
        copy = plugin_parts._copy_file
        kept = []

        def editing(source, destination):
            if not kept and skill.read_text() != "edited during the build\n":
                skill.write_text("edited during the build\n")  # through Kiro, while agt copies the plugin
            return copy(source, destination)

        def backup(folder):
            kept.append((folder / "skills" / "s" / "SKILL.md").read_text())

        with patch.object(plugin_parts, "_copy_file", editing):
            second = self.build(first.record, backup=backup)
        self.assertTrue(second.rebuilt)
        self.assertEqual(kept, ["edited during the build\n"])


COMMAND = """---
description: Export a diagram to .svg and .png
argument-hint: <html-file> [--svg-only|--png-only]
allowed-tools:
  - Read
  - Bash
---

Export the diagram at `$1`, following [the export reference](../skills/x/ref.md).
Run `${CLAUDE_PLUGIN_ROOT}/scripts/export.py`, see ./missing.md and https://example.com/../a.md.
Then run `skills/x/scripts/y.py`, not skills/x/scripts/none.py, https://example.com/skills/x/scripts/y.py,
skills/../../outside.md or input/output.

Full argument string: `$ARGUMENTS`
"""


class CommandTests(PartsBase):
    """Step 3: commands become skills."""

    def setUp(self):
        super().setUp()
        self.install(self.claude, "diagram@studio", "2.6.136", files={
            "skills/x/SKILL.md": "---\nname: x\ndescription: Draws.\n---\nDraw.\n",
            "skills/x/ref.md": "How to export.\n",
            "skills/learn-folder/SKILL.md": "---\nname: learn\ndescription: Learns.\n---\nLearn.\n",
            "scripts/export.py": "print('ok')\n",
            "skills/x/scripts/y.py": "print('y')\n",
            "commands/export-diagram.md": COMMAND,
            "commands/x.md": "---\ndescription: Same as the skill\n---\nDo x.\n",
            "commands/learn.md": "---\ndescription: Learn\n---\nLearn.\n",
            "commands/Bad_Name.md": "---\ndescription: Bad\n---\nBad.\n",
            "commands/plain.md": "# Tidy the notes\n\nTidy them.\n",
            "commands/empty.md": "",
        })

    def build(self):
        selection = plugin_parts.select(self.folders)
        return plugin_parts.refresh(selection.sources["diagram@studio"], self.state)

    def test_a_command_becomes_a_skill(self):
        result = self.build()
        mirror = result.mirror.as_posix()
        folder = result.mirror / ".agt-commands" / "export-diagram"
        document = frontmatter.parse((folder / "SKILL.md").read_text())
        self.assertEqual(document.fields, {"name": "export-diagram", "description": "Export a diagram to .svg and .png"})
        self.assertNotIn("allowed-tools", (folder / "SKILL.md").read_text())
        body = document.body
        self.assertIn("Arguments: <html-file> [--svg-only|--png-only]", body)
        self.assertIn("`$ARGUMENTS` below stands for everything asked", body)
        self.assertIn("`$1` for its first word", body)
        self.assertIn(f"[the export reference]({mirror}/skills/x/ref.md)", body)
        self.assertTrue((result.mirror / "skills" / "x" / "ref.md").is_file())
        self.assertIn(f"`{mirror}/scripts/export.py`", body)
        self.assertTrue((result.mirror / "scripts" / "export.py").is_file())
        self.assertIn("see ./missing.md and https://example.com/../a.md.", body)  # only paths that exist change
        # A bare plugin-root path becomes absolute; a missing one, a URL and one out of the mirror stay.
        self.assertIn(f"Then run `{mirror}/skills/x/scripts/y.py`, not skills/x/scripts/none.py, "
                      "https://example.com/skills/x/scripts/y.py,\nskills/../../outside.md or input/output.", body)
        self.assertTrue((result.mirror / "skills" / "x" / "scripts" / "y.py").is_file())
        self.assertIn("Export the diagram at `$1`", body)
        self.assertIn(("export-diagram", folder), [(part.name, part.folder) for part in result.commands])
        self.assertTrue(all(part.folder.parent.name == plugin_parts.COMMANDS for part in result.commands))

    def test_a_command_without_front_matter_takes_its_first_line(self):
        result = self.build()
        document = frontmatter.parse((result.mirror / ".agt-commands" / "plain" / "SKILL.md").read_text())
        self.assertEqual(document.fields["description"], "Tidy the notes")

    def test_a_skill_covers_its_same_name_command(self):
        result = self.build()
        for name in ("x", "learn"):  # learn: the skill's front matter name, not its folder's
            self.assertFalse((result.mirror / ".agt-commands" / name).exists(), name)
            lines = [line for line in result.reports if line.startswith(f"commands/{name}.md")]
            self.assertEqual(len(lines), 1, result.reports)
            self.assertIn(f"skill {name} covers it", lines[0])

    def test_two_skills_with_one_name_keep_it_from_a_command(self):
        self.install(self.ntb, "dup@studio", "1.0.0", files={
            "skills/a/SKILL.md": "---\nname: dup\ndescription: A.\n---\nA.\n",
            "skills/b/SKILL.md": "---\nname: dup\ndescription: B.\n---\nB.\n",
            "commands/dup.md": "---\ndescription: Dup\n---\nDup.\n",
            "commands/other.md": "---\ndescription: Other\n---\nOther.\n",
        })
        result = plugin_parts.refresh(plugin_parts.select(self.folders).sources["dup@studio"], self.state)
        self.assertIsNone(result.failed)
        self.assertEqual(result.skills, [])  # neither skill is linked
        self.assertEqual([part.name for part in result.commands], ["other"])  # nor does the command take the name
        self.assertFalse((result.mirror / ".agt-commands" / "dup").exists())
        self.assertIn("skills/a, skills/b: left out, they all name the skill dup", result.reports)
        lines = [line for line in result.reports if line.startswith("commands/dup.md")]
        self.assertEqual(len(lines), 1, result.reports)
        self.assertIn("not made a skill", lines[0])
        self.assertIn("skills/a, skills/b", lines[0])

    def test_invalid_and_empty_commands_are_reported(self):
        result = self.build()
        self.assertFalse((result.mirror / ".agt-commands" / "Bad_Name").exists())
        self.assertTrue(any(line.startswith("commands/Bad_Name.md: not made a skill, its name doesn't fit")
                            for line in result.reports), result.reports)
        self.assertFalse((result.mirror / ".agt-commands" / "empty").exists())
        self.assertTrue(any(line.startswith("commands/empty.md") for line in result.reports), result.reports)
        self.assertEqual(sorted(part.name for part in result.commands), ["export-diagram", "plain"])
        self.assertEqual(result.record["reports"], result.reports)

    def test_what_is_left_out_is_one_note_per_plugin_and_reason(self):
        record = self.build().record
        shown = plugin_parts.View(["kiro"], {}, records={"diagram@studio": record})
        self.assertEqual(shown.left_out(), [
            "diagram: 2 commands not made skills, its skills of the same names cover them.",
            "diagram: 1 command not made a skill, Kiro doesn't accept its name.",
            "diagram: 1 command not made a skill, it has no description and no text.",
        ])
        self.assertEqual(shown.left_out(named=True), [
            "diagram: 2 commands not made skills, its skills of the same names cover them (learn, x).",
            "diagram: 1 command not made a skill, Kiro doesn't accept its name (Bad_Name).",
            "diagram: 1 command not made a skill, it has no description and no text (empty).",
        ])
        self.assertEqual(shown.notes(), [])  # notes of their own, never attention entries or issues
        self.assertEqual(plugin_parts.attention(shown), [])
        self.assertEqual(shown.summary()["issues"], 0)
        # Two skills of one name, which a command of that name can't take either; a plugin name two plugins share
        # goes by its id.
        self.install(self.ntb, "diagram@other", "1.0.0", files={
            "skills/a/SKILL.md": "---\nname: dup\ndescription: A.\n---\nA.\n",
            "skills/b/SKILL.md": "---\nname: dup\ndescription: B.\n---\nB.\n",
            "commands/dup.md": "---\ndescription: Dup\n---\nDup.\n",
        })
        other = plugin_parts.refresh(plugin_parts.select(self.folders).sources["diagram@other"], self.state).record
        shown.records["diagram@other"] = other
        self.assertEqual(shown.left_out(named=True)[:2], [
            "diagram@other: 1 skill name left out, two or more of its skills have it (dup).",
            "diagram@other: 1 command not made a skill, its skills that clash over the name keep it (dup).",
        ])
        self.assertTrue(shown.left_out()[2].startswith("diagram@studio: 2 commands not made skills"))

    def test_a_url_stays_as_written_even_its_query(self):
        url = "https://example.com/?next=../x/ref.md"
        self.install(self.ntb, "links@studio", "1.0.0", files={
            "skills/x/SKILL.md": f"---\nname: x\ndescription: Draws.\n---\nSee {url} and ./ref.md.\n",
            "skills/x/ref.md": "How to export.\n",
            "scripts/export.py": "print('ok')\n",
            "commands/go.md": "---\ndescription: Go\n---\nOpen https://example.com/?file=skills/x/ref.md&up=../skills/x/ref.md"
                              " and skills/x/ref.md.\n",
        })
        result = plugin_parts.refresh(plugin_parts.select(self.folders).sources["links@studio"], self.state)
        mirror = result.mirror.as_posix()
        self.assertIn(f"See {url} and {mirror}/skills/x/ref.md.", (result.mirror / "skills" / "x" / "SKILL.md").read_text())
        self.assertIn(f"Open https://example.com/?file=skills/x/ref.md&up=../skills/x/ref.md and {mirror}/skills/x/ref.md.",
                      (result.mirror / ".agt-commands" / "go" / "SKILL.md").read_text())


class ProjectTests(PartsBase):
    def test_every_selected_plugin_gets_a_mirror(self):
        self.install(self.ntb, "infra@ntbx", "1.6.1")
        self.install(self.claude, "infra@ntbx", "1.4.0")
        self.install(self.claude, "deck@tools", "1.0.0")
        outcome = plugin_parts.project(self.folders, self.state)
        self.assertEqual(sorted(outcome.projections), ["deck@tools", "infra@ntbx"])
        infra = outcome.projections["infra@ntbx"]
        self.assertEqual((infra.source.label, infra.record["version"]), ("claude-ntb", "1.6.1"))
        self.assertTrue((self.state / "ntbx" / "infra" / "skills" / "fmt" / "SKILL.md").is_file())
        again = plugin_parts.project(self.folders, self.state,
                                     {plugin_id: item.record for plugin_id, item in outcome.projections.items()})
        self.assertFalse(any(item.rebuilt for item in again.projections.values()))

    def test_an_unreadable_source_folder_holds_the_mirror(self):
        self.install(self.ntb, "infra@ntbx", "1.10.0")
        self.install(self.claude, "infra@ntbx", "1.9.0")
        self.install(self.ntb, "deck@tools", "1.0.0")
        first = plugin_parts.project(self.folders, self.state)
        records = {plugin_id: item.record for plugin_id, item in first.projections.items()}
        (self.ntb / "plugins" / "installed_plugins.json").write_text("{half")  # Claude Code mid-write
        with patch.object(plugin_parts, "_copy_file", wraps=plugin_parts._copy_file) as copy:
            held = plugin_parts.project(self.folders, self.state, records)
        copy.assert_not_called()  # not rebuilt down to 1.9.0 from the other folder
        self.assertEqual(sorted(held.projections), ["deck@tools", "infra@ntbx"])  # deck isn't gone either
        for plugin_id, item in held.projections.items():
            self.assertIn("claude-ntb", item.held)
            self.assertFalse(item.rebuilt)
            self.assertEqual(item.record, records[plugin_id])
        self.assertEqual(held.projections["infra@ntbx"].record["version"], "1.10.0")
        self.assertEqual([part.name for part in held.projections["infra@ntbx"].skills], ["fmt"])

    def test_missing_installed_files_hold_the_mirror(self):
        cache = self.install(self.claude, "deck@tools", "1.0.0")
        first = plugin_parts.project(self.folders, self.state)
        records = {plugin_id: item.record for plugin_id, item in first.projections.items()}
        shutil.rmtree(cache)
        held = plugin_parts.project(self.folders, self.state, records)
        self.assertIn("aren't there", held.projections["deck@tools"].held)
        self.assertEqual(held.projections["deck@tools"].record, records["deck@tools"])

    def test_an_uninstalled_plugin_is_left_out(self):
        self.install(self.claude, "deck@tools", "1.0.0")
        first = plugin_parts.project(self.folders, self.state)
        (self.claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {}}))
        gone = plugin_parts.project(self.folders, self.state, {"deck@tools": first.projections["deck@tools"].record})
        self.assertEqual(gone.projections, {})


FMT = "---\nname: fmt\ndescription: Formats.\n---\nRun ${CLAUDE_PLUGIN_ROOT}/scripts/fmt.sh.\n"
INFRA = {"skills/fmt/SKILL.md": FMT, "scripts/fmt.sh": "#!/bin/sh\n",
         "commands/tidy.md": "---\ndescription: Tidy up\n---\nTidy $ARGUMENTS.\n"}


class LinkTests(PartsBase):
    """Step 4: links in Kiro, the state file, and the clash, edit, update and removal rules."""

    def setUp(self):
        super().setUp()
        self.kiro = self.folder(".kiro")
        self.codex = self.folder(".codex")
        self.included = [*self.folders, self.provider(self.codex, "codex"), self.provider(self.kiro, "kiro")]
        self.cache = self.install(self.ntb, "infra@ntbx", "1.6.1", files=INFRA)
        self.mirror = self.state / "ntbx" / "infra"

    def run_parts(self, included=None):
        included = included if included is not None else self.included
        with apply.lock([provider.folder for provider in included]):
            return plugin_parts.run(included, apply.new_run_id())

    def link(self, name):
        return self.kiro / "skills" / name

    def saved(self):
        return json.loads((self.agt_home / "state" / "share" / "plugin-parts.json").read_text())

    def caches(self):
        return {str(folder): tree_hashes(folder / "plugins" / "cache") for folder in (self.ntb, self.claude)
                if (folder / "plugins" / "cache").is_dir()}

    def changes(self, result):
        return sorted((change.action, change.label, change.name, change.ok) for change in result.changes)

    def test_the_link_is_made_and_resolves(self):
        before = self.caches()
        result = self.run_parts()
        self.assertIsNone(result.problem)
        self.assertEqual(self.changes(result), [("link", "kiro", "fmt", True), ("link", "kiro", "tidy", True),
                                                ("rebuild", "", "infra@ntbx", True)])
        for name, folder in (("fmt", self.mirror / "skills" / "fmt"), ("tidy", self.mirror / ".agt-commands" / "tidy")):
            link = self.link(name)
            self.assertTrue(link.is_symlink(), name)
            self.assertEqual(os.readlink(link), str(folder))  # absolute, and no version in it
            self.assertNotIn("1.6.1", os.readlink(link))
            self.assertTrue((link / "SKILL.md").is_file())
        self.assertIn(f"Run {self.mirror.as_posix()}/scripts/fmt.sh.", (self.link("fmt") / "SKILL.md").read_text())
        self.assertTrue((self.link("fmt") / ".." / ".." / "scripts" / "fmt.sh").resolve().is_file())
        # Only Kiro gets them.
        for folder in (self.ntb / "skills", self.claude / "skills", self.codex / "skills", self.user / ".agents" / "skills"):
            self.assertFalse(folder.exists() and any(folder.iterdir()), folder)
        record = self.saved()["plugins"]["infra@ntbx"]
        self.assertEqual((record["source"], record["version"], record["links"]), ("claude-ntb", "1.6.1",
                                                                                  {"kiro": ["fmt", "tidy"]}))
        # A second run changes nothing, not even the state file.
        state_file = self.agt_home / "state" / "share" / "plugin-parts.json"
        written = state_file.stat().st_mtime_ns
        again = self.run_parts()
        self.assertEqual(again.changes, [])
        self.assertEqual(state_file.stat().st_mtime_ns, written)
        self.assertEqual(self.caches(), before)

    def test_a_user_skill_that_appears_later_makes_the_link_step_aside(self):
        self.run_parts()
        mine = self.skill(self.claude, "fmt", body="My own steps.")
        result = self.run_parts()
        self.assertEqual(self.changes(result), [("remove", "kiro", "fmt", True)])
        change = result.changes[0]
        self.assertIn("your own skill fmt in claude comes first", change.message)
        self.assertTrue(change.backup.is_symlink())  # the link itself went to the backups
        self.assertEqual(change.backup.parent, self.agt_home / "state" / "share" / "backups" / result.run / "plugin-parts"
                         / "links" / "kiro")
        self.assertFalse(os.path.lexists(self.link("fmt")))
        self.assertTrue(self.link("tidy").is_symlink())  # the other part stays
        self.assertIn("fmt (infra@ntbx): not linked, your own skill fmt in claude comes first", result.reports)
        self.assertEqual(self.saved()["plugins"]["infra@ntbx"]["links"], {"kiro": ["tidy"]})
        # Ordinary sharing then links your skill into Kiro; the next runs leave it there.
        os.symlink(mine, self.link("fmt"), target_is_directory=True)
        for _ in range(2):
            again = self.run_parts()
            self.assertEqual(again.changes, [])
            self.assertEqual(os.readlink(self.link("fmt")), str(mine))

    def test_a_user_skill_in_kiro_and_a_two_plugin_clash_are_reported_and_not_linked(self):
        mine = self.skill(self.kiro, "fmt", body="Kiro's own.")
        before = tree_hashes(mine)
        self.install(self.claude, "deck@tools", "1.0.0", files={"skills/review/SKILL.md": "---\nname: review\ndescription: A.\n---\nA.\n"})
        self.install(self.claude, "lint@tools", "1.0.0", files={"skills/review/SKILL.md": "---\nname: review\ndescription: B.\n---\nB.\n"})
        result = self.run_parts()
        self.assertFalse(self.link("fmt").is_symlink())
        self.assertEqual(tree_hashes(mine), before)
        self.assertFalse(os.path.lexists(self.link("review")))
        self.assertTrue(self.link("tidy").is_symlink())
        self.assertIn("fmt (infra@ntbx): not linked, your own skill fmt in kiro comes first", result.reports)
        self.assertIn("review: not linked, the plugins deck@tools, lint@tools all have a skill review", result.reports)
        self.assertEqual(self.saved()["reports"], result.reports)
        self.assertFalse(any(change.action == "link" and change.name in ("fmt", "review") for change in result.changes))

    def test_a_second_plugin_with_a_linked_name_takes_the_link_away(self):
        self.run_parts()
        self.install(self.claude, "deck@tools", "1.0.0", files={"skills/fmt/SKILL.md": FMT})
        result = self.run_parts()
        self.assertIn(("remove", "kiro", "fmt", True), self.changes(result))
        self.assertFalse(os.path.lexists(self.link("fmt")))
        self.assertIn("fmt: not linked, the plugins deck@tools, infra@ntbx all have a skill fmt", result.reports)

    def test_a_version_bump_with_the_old_folder_deleted_keeps_the_link(self):
        self.run_parts()
        before = os.readlink(self.link("fmt"))
        self.install(self.ntb, "infra@ntbx", "1.7.0", files={**INFRA, "skills/fmt/SKILL.md": FMT.replace("Formats.", "Formats better.")})
        shutil.rmtree(self.cache)  # Claude Code deletes the old version's folder
        result = self.run_parts()
        self.assertEqual(self.changes(result), [("rebuild", "", "infra@ntbx", True)])  # no link removed or remade
        self.assertEqual(os.readlink(self.link("fmt")), before)
        self.assertIn("Formats better.", (self.link("fmt") / "SKILL.md").read_text())
        self.assertEqual(self.saved()["plugins"]["infra@ntbx"]["version"], "1.7.0")
        self.assertEqual(self.saved()["plugins"]["infra@ntbx"]["links"], {"kiro": ["fmt", "tidy"]})

    def test_a_skill_that_moves_in_its_plugin_is_linked_again(self):
        self.run_parts()
        self.install(self.ntb, "infra@ntbx", "1.7.0", files={**{key: value for key, value in INFRA.items()
                                                                if not key.startswith("skills/")},
                                                             "skills/format/SKILL.md": FMT})
        result = self.run_parts()
        self.assertIn(("relink", "kiro", "fmt", True), self.changes(result))
        self.assertEqual(os.readlink(self.link("fmt")), str(self.mirror / "skills" / "format"))
        relinked = next(change for change in result.changes if change.action == "relink")
        self.assertEqual(os.readlink(relinked.backup), str(self.mirror / "skills" / "fmt"))  # the old link, kept

    def test_an_edit_through_kiro_is_backed_up_then_restored(self):
        self.run_parts()
        before = self.caches()
        original = (self.link("fmt") / "SKILL.md").read_bytes()
        (self.link("fmt") / "SKILL.md").write_text("edited in Kiro\n")
        result = self.run_parts()
        self.assertEqual(self.changes(result), [("backup", "", "infra@ntbx", True), ("rebuild", "", "infra@ntbx", True)])
        backup = next(change.backup for change in result.changes if change.action == "backup")
        self.assertEqual(backup, self.agt_home / "state" / "share" / "backups" / result.run / "plugin-parts" / "mirrors"
                         / "ntbx" / "infra")
        self.assertEqual((backup / "skills" / "fmt" / "SKILL.md").read_text(), "edited in Kiro\n")
        self.assertEqual((self.link("fmt") / "SKILL.md").read_bytes(), original)
        self.assertEqual(self.caches(), before)  # the plugin cache is never written
        manifest = json.loads((backup.parents[2] / "manifest.json").read_text())
        self.assertEqual([entry["backup"] for entry in manifest["changes"]], [str(backup)])

    def test_an_uninstall_moves_the_links_and_the_mirror_to_the_backups(self):
        self.run_parts()
        (self.ntb / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {}}))
        result = self.run_parts()
        self.assertEqual(self.changes(result), [("remove", "", "infra@ntbx", True), ("remove", "kiro", "fmt", True),
                                                ("remove", "kiro", "tidy", True)])
        backups = self.agt_home / "state" / "share" / "backups" / result.run / "plugin-parts"
        self.assertTrue((backups / "links" / "kiro" / "fmt").is_symlink())
        self.assertTrue((backups / "mirrors" / "ntbx" / "infra" / "skills" / "fmt" / "SKILL.md").is_file())
        self.assertFalse(os.path.lexists(self.link("fmt")) or os.path.lexists(self.link("tidy")))
        self.assertFalse(self.mirror.exists())
        self.assertEqual(self.saved()["plugins"], {})
        self.assertTrue(self.cache.is_dir())  # the plugin's own files stay

    def test_a_plugin_turned_off_goes_the_same_way(self):
        self.run_parts()
        self.install(self.ntb, "infra@ntbx", "1.6.1", files=INFRA, enabled=False)
        result = self.run_parts()
        self.assertIn(("remove", "kiro", "fmt", True), self.changes(result))
        self.assertFalse(self.mirror.exists())

    def test_held_plugins_keep_their_links(self):
        self.run_parts()
        (self.ntb / "plugins" / "installed_plugins.json").write_text("{half")  # Claude Code mid-write
        result = self.run_parts()
        self.assertEqual(result.changes, [])
        self.assertTrue(self.link("fmt").is_symlink())
        self.assertTrue(any(line.startswith("infra@ntbx: kept as it was") for line in result.reports), result.reports)

    def test_a_kiro_folder_that_is_a_git_repo_keeps_the_links_out_of_git_status(self):
        if not shutil.which("git"):
            self.skipTest("git isn't installed")
        subprocess.run(["git", "init", "-q", str(self.kiro)], check=True)
        result = self.run_parts()
        exclude_file = self.kiro / ".git" / "info" / "exclude"
        self.assertIn("/skills/fmt\n", exclude_file.read_text())
        self.assertTrue(result.excluded)
        (self.ntb / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {}}))
        self.run_parts()
        self.assertNotIn("/skills/fmt", exclude_file.read_text())

    def test_a_damaged_state_file_changes_nothing(self):
        state_file = self.agt_home / "state" / "share" / "plugin-parts.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("{half")
        result = self.run_parts()
        self.assertIn("is damaged", result.problem)
        self.assertEqual(result.changes, [])
        self.assertFalse(os.path.lexists(self.link("fmt")))
        self.assertFalse(self.mirror.exists())

    def test_nothing_in_a_repo_scope_or_without_kiro(self):
        repo = self.root / "repo"
        repo.mkdir()
        with scope.use(scope.Scope(repo)):
            self.assertEqual(self.run_parts().changes, [])
        self.assertEqual(self.run_parts(self.folders).changes, [])
        self.assertFalse(self.state.exists())

    def uninstall(self):
        (self.ntb / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {}}))

    def test_kept_copies_in_a_kiro_folder_that_stops_sharing_never_dangle(self):
        other = self.folder(".kiro-b")
        second = providers.Provider(cli="kiro", folder=other, label="kiro-b", installed=True,
                                    locations=providers.locations("kiro", other, self.user))
        self.run_parts([*self.included, second])
        self.assertTrue((other / "skills" / "fmt").is_symlink())
        with apply.lock([provider.folder for provider in [*self.included, second]]):
            result = plugin_parts.detach([*self.included, second], apply.new_run_id(), {"kiro"})
        self.assertEqual(result.failed, [])
        kept = self.link("fmt")
        self.assertFalse(kept.is_symlink())  # a folder of your own now, not a link into agt's mirror
        self.assertEqual((kept / "SKILL.md").read_text(), (self.mirror / "skills" / "fmt" / "SKILL.md").read_text())
        self.assertEqual(self.saved()["plugins"]["infra@ntbx"]["links"], {"kiro-b": ["fmt", "tidy"]})
        # The plugin goes: kiro-b's links and the mirror go to the backups, and the copies kept in kiro stay whole.
        self.uninstall()
        self.run_parts([*self.folders, second])
        self.assertFalse(self.mirror.exists())
        self.assertFalse(os.path.lexists(other / "skills" / "fmt"))
        self.assertTrue((kept / "SKILL.md").is_file())
        self.assertTrue((self.link("tidy") / "SKILL.md").is_file())

    def test_an_uninstall_keeps_the_mirror_while_a_link_to_it_stays(self):
        self.run_parts()
        self.uninstall()
        move = plugin_parts._Backups.move

        def failing(backups, path, *parts):
            if parts[0] == "links":
                raise PermissionError("the Kiro folder is read-only")
            return move(backups, path, *parts)

        with patch.object(plugin_parts._Backups, "move", failing):
            result = self.run_parts()
        self.assertIn(("remove", "kiro", "fmt", False), self.changes(result))
        self.assertTrue(self.mirror.is_dir())
        self.assertTrue((self.link("fmt") / "SKILL.md").is_file())  # the link still leads somewhere
        self.assertEqual(self.saved()["plugins"]["infra@ntbx"]["links"], {"kiro": ["fmt", "tidy"]})
        # The next run finishes it.
        self.run_parts()
        self.assertFalse(os.path.lexists(self.link("fmt")))
        self.assertFalse(self.mirror.exists())
        self.assertEqual(self.saved()["plugins"], {})

    def test_a_kiro_skills_folder_that_disappears_is_not_made_again(self):
        self.run_parts()
        shutil.rmtree(self.kiro / "skills")
        result = self.run_parts()
        self.assertFalse((self.kiro / "skills").exists())
        self.assertTrue(any("kiro's skills folder is missing" in line for line in result.reports), result.reports)
        self.assertEqual(self.saved()["plugins"]["infra@ntbx"]["links"], {"kiro": ["fmt", "tidy"]})
        self.assertEqual(self.saved()["stopped"], {})
        # Back, empty: the links come back, and none counts as one you deleted.
        (self.kiro / "skills").mkdir()
        self.run_parts()
        self.assertTrue(self.link("fmt").is_symlink() and self.link("tidy").is_symlink())
        self.assertEqual(self.saved()["stopped"], {})

    def test_a_kiro_skills_folder_that_is_a_claude_skills_folder_is_left_alone(self):
        claude_skills = self.claude / "skills"
        claude_skills.mkdir()
        os.symlink(claude_skills, self.kiro / "skills", target_is_directory=True)
        result = self.run_parts()
        self.assertEqual(list(claude_skills.iterdir()), [])  # nothing lands in Claude
        [issue] = [issue for issue in result.issues if issue["kind"] == "part-failed"]
        self.assertIn(str(claude_skills), issue["message"])
        [entry] = plugin_parts.attention(plugin_parts.view(self.included))
        self.assertEqual((entry["kind"], entry["id"]), ("part-failed", "part-failed:plugin-parts:kiro"))

    def test_a_kiro_skills_folder_made_its_own_again_gets_its_links_back(self):
        self.run_parts()
        claude_skills = self.claude / "skills"
        claude_skills.mkdir()
        shutil.rmtree(self.kiro / "skills")
        os.symlink(claude_skills, self.kiro / "skills", target_is_directory=True)
        self.run_parts()
        self.assertEqual(list(claude_skills.iterdir()), [])
        (self.kiro / "skills").unlink()
        (self.kiro / "skills").mkdir()  # fixed, as the attention entry says: a folder of its own, empty
        self.run_parts()
        self.assertTrue(self.link("fmt").is_symlink() and self.link("tidy").is_symlink())
        self.assertEqual(self.saved()["stopped"], {})

    def test_kept_copies_never_land_in_a_claude_folder_that_is_kiros(self):
        self.run_parts()
        os.symlink(self.kiro / "skills", self.ntb / "skills", target_is_directory=True)  # after agt linked there
        with apply.lock([provider.folder for provider in self.included]):
            result = plugin_parts.detach(self.included, apply.new_run_id(), {"kiro"})
        self.assertEqual(self.changes(result), [("remove", "kiro", "fmt", True), ("remove", "kiro", "tidy", True)])
        self.assertEqual(list((self.kiro / "skills").iterdir()), [])

    def test_a_claude_skills_folder_that_is_kiros_is_left_alone(self):
        kiro_skills = self.kiro / "skills"
        kiro_skills.mkdir()
        os.symlink(kiro_skills, self.ntb / "skills", target_is_directory=True)
        result = self.run_parts()
        self.assertEqual(list(kiro_skills.iterdir()), [])
        self.assertEqual([issue["kind"] for issue in result.issues], ["part-failed"])


class OrdinarySharingTests(Installs, MacCopy):
    """Step 5: the agents and skills sharing never shares a projection.

    The `plugin-parts` kind lands with the sync wiring, so this test makes the projection with
    `plugin_parts.run` itself, then runs the ordinary `agt share sync` with `skills` on."""

    def test_sync_leaves_projections_in_kiro(self):
        code, out = self.run_share(*YES)
        self.assertEqual(code, 0, out)
        cache = self.install(self.claude, "taste@studio", "1.0.0", files={
            "skills/taste-skill/SKILL.md": "---\nname: design-taste-frontend\ndescription: Taste.\n---\nLook.\n",
            "commands/tidy.md": "---\ndescription: Tidy up\n---\nTidy.\n",
        })
        saved = settings.load()
        self.assertIn("skills", saved.kinds)
        included = commands._folders(load_config(), None, False, saved)[1]

        def project():
            with apply.lock([provider.folder for provider in included]):
                return plugin_parts.run(included, apply.new_run_id())

        def skill_folders():
            found = {}
            for folder in (self.claude / "skills", self.lab / "skills", self.user / ".codex" / "skills",
                           self.user / ".agents" / "skills"):
                found[str(folder)] = sorted(entry.name for entry in folder.iterdir()) if folder.is_dir() else []
            return found

        result = project()
        kiro = self.user / ".kiro" / "skills"
        self.assertEqual(sorted(change.name for change in result.written if change.action == "link"),
                         ["design-taste-frontend", "tidy"])
        before = skill_folders()
        code, out = self.run_share("sync")
        self.assertEqual(code, 0, out)
        self.assertEqual(skill_folders(), before)
        code, out = self.run_share("sync", "--dry-run", "--json")
        self.assertEqual(code, 0, out)
        proposal = json.loads(out)
        reason = "part of the taste@studio plugin; agt links it into Kiro only"
        for name in ("design-taste-frontend", "tidy"):
            self.assertIn({"kind": "skills", "name": name, "labels": ["kiro"], "reason": reason}, proposal["excluded"])
            self.assertNotIn(name, [item["name"] for item in proposal["items"]])
        # A version bump with the old folder deleted: the link keeps its path, and nothing freezes or counts as deleted.
        self.install(self.claude, "taste@studio", "1.1.0", files={
            "skills/taste-skill/SKILL.md": "---\nname: design-taste-frontend\ndescription: Taste 2.\n---\nLook.\n",
            "commands/tidy.md": "---\ndescription: Tidy up\n---\nTidy.\n",
        })
        shutil.rmtree(cache)
        self.assertEqual([change.action for change in project().changes], ["rebuild"])
        self.assertIn("Taste 2.", (kiro / "design-taste-frontend" / "SKILL.md").read_text())
        code, out = self.run_share("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("✓ everything is in sync, nothing to write", out)
        self.assertEqual(skill_folders(), before)
        code, out = self.run_share("sync", "--dry-run", "--json")
        self.assertEqual(code, 0, out)
        proposal = json.loads(out)
        for key in ("changes", "needs_answer", "notices"):
            self.assertIn(key, proposal)
        self.assertEqual(proposal["changes"], [])
        self.assertEqual(proposal["needs_answer"], [])  # a frozen folder's deletes would wait for an answer here
        self.assertFalse([notice for notice in proposal["notices"] if "delet" in notice or "disappear" in notice],
                         proposal["notices"])


class WiringTests(Installs, MacCopy):
    """Steps 6 and 7: the `plugin-parts` kind, the sync and watcher wiring, picks, status and list."""

    def setUp(self):
        super().setUp()
        code, out = self.run_share(*YES)
        self.assertEqual(code, 0, out)
        self.kiro_skills = self.user / ".kiro" / "skills"
        self.cache = self.install(self.claude, "infra@ntbx", "1.6.1", files=INFRA)
        self.mirror = self.agt_home / "state" / "share" / "plugin-parts" / "ntbx" / "infra"
        self.state_file = self.agt_home / "state" / "share" / "plugin-parts.json"

    def share_json(self, *args, code=0):
        found, out = self.run_share(*args, "--json")
        self.assertEqual(found, code, out)
        return json.loads(out)

    def turn_on(self):
        report = self.share_json("kinds", "add", "plugin-parts")
        self.assertTrue(settings.load().plugin_parts)
        return report

    def links(self):
        return sorted(entry.name for entry in self.kiro_skills.iterdir()
                      if plugin_parts.exclude.plugin_part(entry) is not None) if self.kiro_skills.is_dir() else []

    def test_kinds_add_and_both_remove_answers(self):
        self.assertFalse(settings.load().plugin_parts)  # off by default
        report = self.turn_on()
        self.assertEqual(sorted(o["name"] for o in report["written"] if o["action"] == "link"), ["fmt", "tidy"])
        self.assertEqual(self.links(), ["fmt", "tidy"])
        self.assertIn("plugin_parts = true", (self.user / ".agt" / "settings.toml").read_text())
        self.assertEqual(self.share_json("kinds", "add", "plugin-parts"),
                         {"kind": "plugin-parts", "enabled": True, "unchanged": True})
        # An agent without an answer: nothing changes, exit 3.
        asked = self.share_json("kinds", "remove", "plugin-parts", code=3)
        self.assertEqual(asked["error"], "needs_choice")
        self.assertEqual([choice["value"] for choice in asked["choices"]], ["--keep-copies", "--remove-copies"])
        self.assertEqual(sorted(copy["name"] for copy in asked["copies"]), ["fmt", "tidy"])
        self.assertTrue(settings.load().plugin_parts)
        self.assertEqual(self.links(), ["fmt", "tidy"])
        # --keep-copies: the links stay as your files, and agt stops updating them.
        kept = self.share_json("kinds", "remove", "plugin-parts", "--keep-copies")
        self.assertEqual((kept["plugin-parts"], kept["answer"], kept["removed"]), ("off", "keep", []))
        self.assertFalse(settings.load().plugin_parts)
        self.assertEqual(self.links(), ["fmt", "tidy"])
        self.assertEqual(self.share_json("kinds", "remove", "plugin-parts"),
                         {"kind": "plugin-parts", "enabled": False, "unchanged": True})
        # --remove-copies: the links and the mirrors go to the backups.
        self.turn_on()
        removed = self.share_json("kinds", "remove", "plugin-parts", "--remove-copies")
        self.assertEqual(removed["plugin-parts"], "off")
        self.assertEqual(sorted(o["name"] for o in removed["removed"] if o["labels"]), ["fmt", "tidy"])
        self.assertEqual(self.links(), [])
        self.assertFalse(self.mirror.exists())
        self.assertFalse(self.state_file.exists())
        backups = Path(removed["backups"])
        self.assertTrue((backups / "links" / "kiro" / "fmt").is_symlink())
        self.assertTrue((backups / "mirrors" / "ntbx" / "infra" / "skills" / "fmt" / "SKILL.md").is_file())
        self.assertNotIn("plugin-parts", [change["key"].partition("/")[0] for data in apply.manifests()
                                          for change in data["changes"]])  # `undo` never sees them

    def test_a_session_start_builds_no_mirror_and_leaves_it_to_the_watcher(self):
        from agents_router.share import watch

        self.turn_on()
        self.install(self.claude, "infra@ntbx", "1.7.0", files={**INFRA, "skills/fmt/SKILL.md": FMT + "Go 1.7.\n"})
        shutil.rmtree(self.cache)  # Claude Code deletes the old version
        copied = []
        original = plugin_parts._copy_file

        def counting(source, destination):
            copied.append(source)
            return original(source, destination)

        with patch.object(plugin_parts, "_copy_file", counting):
            outcome, _ = watch.background_sync("session")
            self.assertEqual(copied, [], outcome)
            self.assertEqual(json.loads(self.state_file.read_text())["plugins"]["infra@ntbx"]["version"], "1.6.1")
            self.assertEqual(self.links(), ["fmt", "tidy"])  # kept: the link path has no version
            outcome, _ = watch.background_sync("watcher")  # the session left the signature unsaved
        self.assertTrue(copied, outcome)
        self.assertEqual(json.loads(self.state_file.read_text())["plugins"]["infra@ntbx"]["version"], "1.7.0")
        self.assertIn("Go 1.7", (self.kiro_skills / "fmt" / "SKILL.md").read_text())
        self.assertEqual(watch.background_sync("watcher")[0], "nothing to write")  # agt's own writes moved the signature
        self.assertEqual(watch.background_sync("watcher")[0], "nothing moved")

    def test_the_watch_list_has_the_plugin_inventory_with_only_plugin_parts_on(self):
        from agents_router.share import watch

        saved = settings.load()
        included = commands._folders(load_config(), None, False, saved)[1]
        inventory = {self.claude / "plugins" / "installed_plugins.json", self.claude / "settings.json",
                     self.lab / "plugins" / "installed_plugins.json"}
        self.assertFalse(inventory & set(watch.watched_files(saved, included)))
        saved.plugin_parts = True
        self.assertFalse(saved.plugins)
        self.assertLessEqual(inventory, set(watch.watched_files(saved, included)))

    @unittest.skipIf(shutil.which("git") is None, "needs git")
    def test_a_listed_repo_builds_no_plugin_parts(self):
        self.turn_on()
        repo = self.root / "work" / "app"
        self.skill(repo / ".claude", "release-notes")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        code, out = self.run_share("repo", "add", str(repo))
        self.assertEqual(code, 0, out)
        code, out = self.run_share("sync")
        self.assertEqual(code, 0, out)
        self.assertTrue((repo / ".kiro" / "skills" / "release-notes").exists())
        self.assertEqual(sorted(entry.name for entry in (repo / ".kiro" / "skills").iterdir()), ["release-notes"])
        with scope.use(scope.Scope(repo)):
            self.assertFalse((scope.folder() / "plugin-parts").exists())
            self.assertFalse(scope.folder().joinpath("plugin-parts.json").exists())
            saved = settings.load()
            self.assertEqual(commands._parts_run(saved, commands._folders(load_config(), None, False, saved)[1])[1], None)

    def test_a_deleted_link_stops_it_and_pick_in_brings_it_back(self):
        self.turn_on()
        (self.kiro_skills / "fmt").unlink()
        self.assertEqual(self.run_share("sync")[0], 0)
        self.assertEqual(self.links(), ["tidy"])
        self.assertEqual(json.loads(self.state_file.read_text())["stopped"], {"fmt": ["kiro"]})
        self.assertEqual(self.run_share("sync")[0], 0)
        self.assertEqual(self.links(), ["tidy"])  # it stays out
        cell = {item["id"]: item for item in self.share_json("list")["items"]}["plugin-parts/fmt"]["cells"]["kiro"]
        self.assertEqual((cell["state"], cell["note"], cell["editable"]), ("skipped", "you deleted its link in kiro", True))
        self.share_json("pick", "plugin-parts/fmt", "--in", "kiro")
        self.assertEqual(self.links(), ["fmt", "tidy"])
        self.assertEqual(json.loads(self.state_file.read_text())["stopped"], {})

    def test_pick_in_right_after_deleting_a_link_brings_it_back(self):
        self.turn_on()
        (self.kiro_skills / "fmt").unlink()
        self.share_json("pick", "plugin-parts/fmt", "--in", "kiro")  # no sync in between
        self.assertEqual(self.links(), ["fmt", "tidy"])
        self.assertEqual(json.loads(self.state_file.read_text())["stopped"], {})

    def test_cli_remove_of_kiro_keep_copies_leaves_folders_of_your_own(self):
        self.turn_on()
        self.share_json("cli", "remove", "kiro", "--keep-copies")
        for name in ("fmt", "tidy"):
            kept = self.kiro_skills / name
            self.assertFalse(kept.is_symlink(), name)  # never a link into a mirror agt goes on changing
            self.assertTrue((kept / "SKILL.md").is_file(), name)
        self.assertEqual(self.links(), [])
        self.assertTrue(all(not record.get("links") for record in json.loads(self.state_file.read_text())["plugins"].values()))

    def test_a_missing_kiro_skills_folder_stays_missing_and_its_copies_wait(self):
        from agents_router.share import state

        self.turn_on()
        self.assertTrue((self.kiro_skills / "ask-simply" / "SKILL.md").is_file())  # an ordinary skill in Kiro too
        shutil.rmtree(self.kiro_skills)
        code, out = self.run_share("sync")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.kiro_skills.exists())  # neither pass makes it again
        record = state.load().items["skills/ask-simply"]
        self.assertNotIn("kiro", record.get("stopped") or [])

    def test_pick_not_in_keeps_it_out_and_in_takes_it_back(self):
        self.turn_on()
        report = self.share_json("pick", "plugin-parts/fmt", "--not-in", "kiro")
        self.assertEqual([(o["name"], o["action"]) for o in report["written"]], [("fmt", "remove")])
        self.assertEqual(settings.load().not_in, {"plugin-parts/fmt": ["kiro"]})
        self.assertEqual(self.links(), ["tidy"])
        self.assertEqual(self.run_share("sync")[0], 0)
        self.assertEqual(self.links(), ["tidy"])
        code, out = self.run_share("pick", "plugin-parts/fmt", "--in", "claude")
        self.assertNotEqual(code, 0, out)  # Claude Code loads it from the plugin
        self.share_json("pick", "plugin-parts/fmt", "--in", "kiro")
        self.assertEqual(settings.load().not_in, {})
        self.assertEqual(self.links(), ["fmt", "tidy"])

    def test_cli_remove_of_kiro_counts_and_removes_its_links(self):
        self.turn_on()
        asked = self.share_json("cli", "remove", "kiro", code=3)
        self.assertIn("fmt", [copy["name"] for copy in asked["copies"]])
        self.share_json("cli", "remove", "kiro", "--remove-copies")
        self.assertEqual(self.links(), [])
        self.assertTrue(all(not record.get("links") for record in json.loads(self.state_file.read_text())["plugins"].values()))

    def test_status_and_list_show_plugin_parts_clashes_and_failed_builds(self):
        self.turn_on()
        status = self.share_json("status")
        self.assertEqual(status["plugin_parts"]["skills"], 2)
        self.assertEqual(status["plugin_parts"]["linked"], {"kiro": 2})
        listed = {item["id"]: item for item in self.share_json("list")["items"]}
        self.assertNotIn("skills/fmt", listed)  # the plugin-parts item stands for the link
        cells = listed["plugin-parts/fmt"]["cells"]
        self.assertEqual((cells["kiro"]["state"], cells["kiro"]["editable"]), ("has", True))
        for label in ("claude", "claude-lab", "codex"):
            self.assertEqual((cells[label]["note"], cells[label]["editable"]), ("loads it from the plugin", False))
        # Your skill of the same name wins: the designed outcome, so a status note, not an attention entry.
        self.skill(self.claude, "tidy", body="Mine.")
        code, out = self.run_share("sync")
        self.assertEqual(code, 0, out)
        self.assertFalse(plugin_parts.exclude.plugin_part(self.kiro_skills / "tidy"))
        status = self.share_json("status", code=0)
        self.assertFalse([entry for entry in status["attention"] if entry["kind"].startswith("part-")])
        self.assertEqual([note for note in status["notes"] if "tidy" in note],
                         ["Your own skill tidy in claude comes first, so the infra@ntbx plugin's goes to no Kiro folder; "
                          "rename yours to get both, or `agt share pick plugin-parts/tidy --not-in kiro` leaves the "
                          "plugin's out."])
        self.assertEqual(status["plugin_parts"]["issues"], 1)  # still not linked
        code, text = self.run_share("status")
        self.assertEqual(code, 0, text)
        self.assertIn("Plugin parts", text)
        self.assertIn("Notes", text)
        self.assertIn("agt share pick plugin-parts/tidy --not-in kiro", text)
        self.assertNotIn("Waiting for you", text)
        self.share_json("pick", "plugin-parts/tidy", "--not-in", "kiro")  # keep yours: the note goes
        self.assertFalse([note for note in self.share_json("status", code=0)["notes"] if "tidy" in note])
        # A build that fails is reported with its fix, and the earlier mirror and links stay.
        self.install(self.claude, "infra@ntbx", "1.7.0", files=INFRA)

        def failing(source, destination):
            raise OSError("disk full")

        with patch.object(plugin_parts, "_copy_file", failing):
            code, out = self.run_share("sync")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.links(), ["fmt"])
        failed = [entry for entry in self.share_json("status", code=1)["attention"] if entry["kind"] == "part-failed"]
        self.assertEqual([(entry["item"], entry["fix"]) for entry in failed], [("plugin-parts/infra@ntbx", ["agt share sync"])])
        self.assertEqual(self.run_share("sync")[0], 0)
        self.assertFalse([entry for entry in self.share_json("status", code=0)["attention"]
                          if entry["kind"].startswith("part-")])

    def test_sync_and_status_note_what_the_plugins_leave_out(self):
        self.install(self.claude, "infra@ntbx", "1.6.1", files={
            **INFRA, "commands/fmt.md": "---\ndescription: Format\n---\nFormat.\n",
            "commands/Bad_Name.md": "---\ndescription: Bad\n---\nBad.\n"})
        short = ["infra: 1 command not made a skill, its skill of the same name covers it.",
                 "infra: 1 command not made a skill, Kiro doesn't accept its name."]
        named = ["infra: 1 command not made a skill, its skill of the same name covers it (fmt).",
                 "infra: 1 command not made a skill, Kiro doesn't accept its name (Bad_Name)."]
        report = self.turn_on()  # its sync builds the mirror
        self.assertEqual([note for note in report["notes"] if note.startswith("infra: ")], named)
        self.assertEqual(self.links(), ["fmt", "tidy"])
        # status: notes, never attention entries, so it exits 0; the names under -v and in --json.
        status = self.share_json("status", code=0)
        self.assertEqual([note for note in status["notes"] if note.startswith("infra: ")], named)
        self.assertFalse([entry for entry in status["attention"] if entry["kind"].startswith("part-")])
        self.assertEqual(status["plugin_parts"]["issues"], 0)
        code, text = self.run_share("status")
        self.assertEqual(code, 0, text)
        for note in short:
            self.assertIn(note, text)
        self.assertNotIn("(fmt)", text)
        code, text = self.run_share("status", "-v")
        self.assertEqual(code, 0, text)
        for note in named:
            self.assertIn(note, text)
        # sync says it each run, from what that run left in the state file.
        code, text = self.run_share("sync")
        self.assertEqual(code, 0, text)
        for note in short:
            self.assertIn(note, text)
        self.assertNotIn("(Bad_Name)", text)
        code, text = self.run_share("sync", "-v")
        self.assertEqual(code, 0, text)
        self.assertIn(named[1], text)
        self.assertEqual([note for note in self.share_json("sync")["notes"] if note.startswith("infra: ")], named)
        # A plugin update that drops them drops the notes.
        self.install(self.claude, "infra@ntbx", "1.7.0", files=INFRA)
        self.assertEqual([note for note in self.share_json("sync")["notes"] if note.startswith("infra: ")], [])
        self.assertEqual([note for note in self.share_json("status")["notes"] if note.startswith("infra: ")], [])

    def test_a_kiro_entry_of_yours_is_a_note_and_two_plugins_stay_waiting(self):
        self.turn_on()
        (self.kiro_skills / "fmt").unlink()
        self.skill(self.user / ".kiro", "fmt", body="My own fmt.")  # a real folder, not agt's link
        self.share_json("pick", "plugin-parts/fmt", "--in", "kiro")  # wanted there, so the clash shows
        status = self.share_json("status", code=0)
        self.assertFalse([entry for entry in status["attention"] if entry["kind"].startswith("part-")])
        self.assertTrue([note for note in status["notes"] if note.startswith("Your own skill fmt in ")],
                        status["notes"])
        self.assertIn("My own fmt.", (self.kiro_skills / "fmt" / "SKILL.md").read_text())
        # Two plugins with one skill name: still waiting for your choice, and status exits 1.
        self.install(self.claude, "fmt@other", "1.0.0", files={"skills/tidy/SKILL.md": "---\nname: tidy\ndescription: T.\n---\nT.\n"})
        self.assertEqual(self.run_share("sync")[0], 0)
        status = self.share_json("status", code=1)
        self.assertEqual([entry["kind"] for entry in status["attention"] if entry["kind"].startswith("part-")],
                         ["part-conflict"])

    def test_disable_remove_copies_takes_the_links_and_mirrors_to_the_backups(self):
        self.turn_on()
        self.assertTrue(self.mirror.is_dir())
        report = self.share_json("disable", "--remove-copies")
        self.assertFalse(settings.load().enabled)
        self.assertEqual(self.links(), [])
        self.assertFalse(self.mirror.exists())
        self.assertFalse(self.state_file.exists())
        removed = [o for o in report["written"] if o["kind"] == "plugin-parts"]
        self.assertEqual(sorted(o["name"] for o in removed if o["labels"]), ["fmt", "tidy"])
        [backups] = (self.agt_home / "state" / "share" / "backups").glob("*/plugin-parts")
        self.assertTrue((backups / "links" / "kiro" / "fmt").is_symlink())
        self.assertTrue((backups / "mirrors" / "ntbx" / "infra" / "skills" / "fmt" / "SKILL.md").is_file())
        # Turned on again, the next sync builds and links them again.
        code, out = self.run_share(*YES)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_share("sync")[0], 0)
        self.assertEqual(self.links(), ["fmt", "tidy"])

    def test_disable_keeps_the_links_as_they_are_and_stops_refreshing_them(self):
        from agents_router.share import watch

        self.turn_on()
        report = self.share_json("disable")
        self.assertFalse(report["enabled"])
        self.assertEqual(self.links(), ["fmt", "tidy"])
        self.assertTrue(self.mirror.is_dir())
        before = (self.kiro_skills / "fmt" / "SKILL.md").read_text()
        self.install(self.claude, "infra@ntbx", "1.7.0", files={**INFRA, "skills/fmt/SKILL.md": FMT + "Go 1.7.\n"})
        shutil.rmtree(self.cache)
        watch.background_sync("watcher")
        self.run_share("sync")
        self.assertEqual((self.kiro_skills / "fmt" / "SKILL.md").read_text(), before)  # nothing refreshed it
        self.assertEqual(json.loads(self.state_file.read_text())["plugins"]["infra@ntbx"]["version"], "1.6.1")

    def test_kinds_remove_plugin_parts_writes_a_log_line(self):
        from agents_router import synclog

        def turn_offs():
            return [entry for entry in synclog.entries() if entry.get("turned_off") == "plugin-parts"]

        self.turn_on()
        self.share_json("kinds", "remove", "plugin-parts", "--keep-copies")
        kept = turn_offs()
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0]["type"], kept[0]["answer"], kept[0]["changes"]), ("sync", "keep", []))
        self.assertEqual(sorted(copy["item"] for copy in kept[0]["copies"]), ["plugin-parts/fmt", "plugin-parts/tidy"])
        self.assertIn("turned off plugin-parts, keep the copies", synclog.line(kept[0]))
        self.turn_on()
        self.share_json("kinds", "remove", "plugin-parts", "--remove-copies")
        removed = [entry for entry in turn_offs() if entry["answer"] == "remove"]
        self.assertEqual(len(removed), 1)
        self.assertEqual(sorted(change["item"] for change in removed[0]["changes"] if change["labels"]),
                         ["plugin-parts/fmt", "plugin-parts/tidy"])
        self.assertTrue(all(change["ok"] and change["backup"] for change in removed[0]["changes"]))
        self.assertIn("turned off plugin-parts, remove the copies", synclog.line(removed[0]))

    def test_other_turn_offs_leave_plugin_parts_alone(self):
        self.turn_on()
        survey = self.share_json("kinds", "remove", "agents", "--dry-run")
        self.assertFalse([copy for copy in survey["copies"] if copy["name"] in ("fmt", "tidy")])
        self.share_json("pick", "plugin-parts/fmt", "--not-in", "kiro")
        code, out = self.run_share(*YES)  # enable again, with a plugin-parts pick saved
        self.assertEqual(code, 0, out)
        saved = settings.load()
        self.assertEqual((saved.not_in, saved.plugin_parts), ({"plugin-parts/fmt": ["kiro"]}, True))

    def test_a_kept_link_you_delete_comes_back_when_plugin_parts_are_on_again(self):
        self.turn_on()
        self.share_json("kinds", "remove", "plugin-parts", "--keep-copies")
        (self.kiro_skills / "fmt").unlink()  # your file now: deleting it stops nothing
        self.turn_on()
        self.assertEqual(self.links(), ["fmt", "tidy"])
