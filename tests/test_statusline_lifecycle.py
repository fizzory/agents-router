"""Consent, lifecycle and read-only reporting for live Claude usage."""

import contextlib
import io
import json
from pathlib import Path
import shlex
from unittest.mock import patch

import helpers  # isolate real profiles and CLIs before importing agt
import test_setup as setup_tests
import test_usage_cli as usage_tests

from agents_router import accounts, cli, setup, setup_screen, statusline, usage_snapshot
from agents_router.config import Account, homes_dir, load_config
from agents_router.fsutil import read_json, write_json


class StatuslineLifecycleTests(setup_tests.SetupBase):
    def setUp(self):
        super().setUp()
        self.settings = self.claude_dir / 'settings.json'
        self.original = b'{\n  "statusLine": {"type":"command", "command":"printf original", "padding": 3}\n}\n'
        self.settings.write_bytes(self.original)
        (self.claude_dir / 'logged_in').touch()

    def choice(self, value):
        config = load_config()
        config.usage.statusline = value
        return config

    def doctor(self, config, *extra):
        report = {'runtime': 'claude', 'binary': 'fake-claude', 'version': '1',
                  'accounts': [{'name': 'work', 'home': str(self.claude_dir),
                                'auth': {'loggedIn': True}, 'enabled': True}]}
        output = io.StringIO()
        with patch.object(cli, 'load_config', return_value=config), \
                patch.object(cli.tools, 'account_report', return_value=report), \
                patch.object(cli, '_share_doctor', return_value=[]), \
                patch.object(setup, 'upstream_installs', return_value=[]), \
                contextlib.redirect_stdout(output):
            code = cli.main(['doctor', '--runtime', 'claude', *extra])
        return code, output.getvalue()

    def wrap(self):
        result = statusline.refresh(None, True, [self.claude_dir])
        self.assertEqual(result.problems, [])

    def test_unset_setup_and_update_never_wrap(self):
        config = self.choice(None)
        setup.run_setup(config, ['claude'], [], role_agents=False)
        setup.run_update(config, ['claude'], package=False)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertEqual(statusline.recorded(), [])

    def test_combined_proposal_lists_each_profile_and_saves_consent_only_on_apply(self):
        args = cli.build_parser().parse_args(['setup', '--host', 'claude', '--share', 'off', '--roles', 'off', '--dry-run'])
        screen = setup_screen.build(args)
        proposal = setup_screen.proposal(args, screen)
        self.assertIn('switches', proposal)
        self.assertIn('statusline', proposal['switches'])
        self.assertTrue(proposal['switches']['statusline']['on'])
        self.assertIn('--statusline on', proposal['command'])
        for profile in proposal['claude_profiles']:
            if profile['gets_plugin']:
                self.assertIn(profile['name'] + '  status line: record usage for agt, then run yours', proposal['plugin']['planned'])
        self.assertIsNone(load_config().usage.statusline)
        self.assertEqual(self.settings.read_bytes(), self.original)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(shlex.split(proposal['command'])[1:]), 0)
        self.assertIs(load_config().usage.statusline, True)
        self.assertEqual(statusline.state(self.claude_dir), 'live')

    def test_setup_decline_is_saved_without_wrapping(self):
        code, output = self.run_cli('--host', 'claude', '--share', 'off', '--roles', 'off', '--yes', '--statusline', 'off')
        self.assertEqual(code, 0, output)
        self.assertIs(load_config().usage.statusline, False)
        self.assertEqual(self.settings.read_bytes(), self.original)

    def test_setup_plan_names_each_selected_profile(self):
        config = self.choice(True)
        other = self.root / 'other-profile'
        other.mkdir()
        config.runtime('claude').accounts.append(Account('other', other, setup='own'))
        code, report = setup.run_setup(config, ['claude'], [], dry_run=True, role_agents=False)
        self.assertEqual(code, 0, report.failed)
        for name in (setup.profile_name(config.runtime('claude').primary_account()), 'other'):
            self.assertIn(name + '  status line: record usage for agt, then run yours', report.done)
        self.assertFalse((other / 'settings.json').exists())

    def test_unset_note_is_once_across_profiles(self):
        config = self.choice(None)
        report = {'runtime': 'claude', 'binary': 'fake-claude', 'version': '1', 'accounts': [
            {'name': name, 'home': str(self.claude_dir), 'auth': {'loggedIn': True}}
            for name in ('work', 'personal')]}
        with patch.object(cli, 'load_config', return_value=config), \
                patch.object(cli.tools, 'account_report', return_value=report), \
                patch.object(cli, '_share_doctor', return_value=[]), \
                patch.object(setup, 'upstream_installs', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(['doctor', '--runtime', 'claude'])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(output.getvalue().count("Live Claude usage isn't chosen yet"), 1)

    def test_update_rewraps_replacement_and_off_restores_unlisted_recorded_files(self):
        self.wrap()
        replacement = b'{"statusLine":{"type":"command","command":"printf replacement"}}\n'
        self.settings.write_bytes(replacement)
        setup.write_statusline_choice(True)
        config = self.choice(True)
        code, report = setup.run_update(config, ['claude'], package=False)
        self.assertEqual(code, 0, report.failed)
        self.assertEqual(statusline.state(self.claude_dir), 'live')
        self.assertEqual(statusline.parse(read_json(self.settings)['statusLine']['command'])[0], 'printf replacement')
        # Uninstall's first command: restore the registry even if no account list includes this folder.
        config.usage.statusline = False
        report = setup.Report()
        setup.refresh_statuslines(config, report, profiles=[])
        self.assertEqual(report.failed, [])
        self.assertEqual(self.settings.read_bytes(), replacement)
        self.assertEqual(statusline.recorded(), [])

    def test_update_uses_consent_changed_during_plugin_updates(self):
        setup.write_statusline_choice(True)
        config = load_config()

        def turn_off(*args, **kwargs):
            setup.write_statusline_choice(False)
            return True

        with patch.object(setup, "update_plugin", side_effect=turn_off):
            code, report = setup.run_update(config, ["claude"], package=False)
        self.assertEqual(code, 0, report.failed)
        self.assertIs(load_config().usage.statusline, False)
        self.assertIs(config.usage.statusline, True)  # the original update snapshot is deliberately stale
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertEqual(statusline.recorded(), [])

    def test_doctor_has_exact_lines_and_json_for_all_seven_states(self):
        config = self.choice(None)
        for value, enabled, wrapped, text, code in [
            ('unset', None, False, "Live Claude usage isn't chosen yet. Turn it on: agt config set usage.statusline true", 0),
            ('off', False, False, 'usage from jobs only (off)', 0),
            ('missing', True, False, "✗ work has no status line tap, so its usage updates only from agt's jobs", 1),
            ('live', True, True, '✓ live usage', 0),
            ('replaced', True, False, "✗ Another tool replaced work's status line, so its usage updates only from agt's jobs", 1),
            ('no_limits', True, True, 'work: Claude Code reports no usage limits for this login. Only Pro and Max logins have them.', 0),
            ('unsupported', True, True, 'work: usage from jobs only (live usage needs macOS or Linux)', 0),
        ]:
            with self.subTest(state=value):
                config.usage.statusline = enabled
                if wrapped:
                    self.wrap()
                elif value == 'replaced':
                    self.settings.write_bytes(self.original)
                if value == 'no_limits':
                    write_json(self.claude_dir / '.claude.json', {'oauthAccount': {'accountUuid': 'login-a'}})
                    login = accounts._folder_login(self.claude_dir)
                    write_json(accounts._live_path(self.claude_dir), {'login': login, 'limits': False})
                before = usage_tests.listing(self.root)
                with patch.object(statusline, '_windows', return_value=value == 'unsupported'):
                    actual, output = self.doctor(config)
                    self.assertEqual(actual, code, output)
                    self.assertIn(text, output)
                    actual_json, output_json = self.doctor(config, '--json')
                self.assertEqual(actual_json, code, output_json)
                self.assertEqual(json.loads(output_json)['accounts'][0]['statusline_usage'], value)
                self.assertEqual(usage_tests.listing(self.root), before, 'doctor wrote while reporting')
                if value == 'unset':
                    self.assertEqual(output.count("Live Claude usage isn't chosen yet"), 1)
                if value == 'replaced':
                    self.assertIn('Fix it   agt setup (keeps the new status line)', output)
                if value == 'missing':
                    self.assertIn('Fix it   agt setup', output)

    def test_no_limits_requires_current_login_and_live_wrapper(self):
        self.wrap()
        config = self.choice(True)
        account = Account('work', self.claude_dir)
        write_json(self.claude_dir / '.claude.json', {'oauthAccount': {'accountUuid': 'login-b'}})
        write_json(accounts._live_path(self.claude_dir), {'login': 'old-login', 'limits': False})
        self.assertEqual(usage_snapshot.statusline_usage(config, account), 'live')
        write_json(accounts._live_path(self.claude_dir), {'login': accounts._folder_login(self.claude_dir), 'limits': False})
        self.assertEqual(usage_snapshot.statusline_usage(config, account), 'no_limits')
        self.settings.write_bytes(self.original)
        self.assertEqual(usage_snapshot.statusline_usage(config, account), 'replaced')

    def test_no_limits_uses_configured_login_before_the_folders_login(self):
        self.wrap()
        config = self.choice(True)
        account = Account('work', self.claude_dir, login='configured-login')
        runtime = config.runtime('claude')
        runtime.accounts, runtime._found = [account], []
        write_json(self.claude_dir / '.claude.json', {'oauthAccount': {'accountUuid': 'folder-login'}})
        path = accounts._live_path(self.claude_dir)
        for login, expected in [(accounts._folder_login(self.claude_dir), 'live'), ('configured-login', 'no_limits')]:
            with self.subTest(live_login=login):
                write_json(path, {'login': login, 'limits': False})
                before = usage_tests.listing(self.root)
                self.assertEqual(usage_snapshot.statusline_usage(config, account), expected)
                report = usage_snapshot.snapshot(config)
                self.assertEqual(report['providers'][0]['accounts'][0]['statusline_usage'], expected)
                code, output = self.doctor(config, '--json')
                self.assertEqual(code, 0, output)
                self.assertEqual(json.loads(output)['accounts'][0]['statusline_usage'], expected)
                self.assertEqual(usage_tests.listing(self.root), before)

    def test_empty_windows_without_limits_are_still_live(self):
        self.wrap()
        config = self.choice(True)
        account = Account('work', self.claude_dir)
        write_json(self.claude_dir / '.claude.json', {'oauthAccount': {'accountUuid': 'login-a'}})
        write_json(accounts._live_path(self.claude_dir), {'login': accounts._folder_login(self.claude_dir), 'windows': {}})
        before = usage_tests.listing(self.root)
        self.assertEqual(usage_snapshot.statusline_usage(config, account), 'live')
        code, output = self.doctor(config, '--json')
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['accounts'][0]['statusline_usage'], 'live')
        self.assertEqual(usage_tests.listing(self.root), before)

    def test_no_limits_never_trusts_a_login_filled_from_old_job_usage(self):
        self.wrap()
        config = self.choice(True)
        account = Account('work', self.claude_dir)
        runtime = config.runtime('claude')
        runtime.accounts, runtime._found = [account], []
        write_json(accounts._live_path(self.claude_dir), {'login': 'old-job-login', 'limits': False})
        write_json(accounts._usage_path('claude'), {'work': {'login': 'old-job-login'}})
        self.assertIsNone(accounts._login(account))
        before = usage_tests.listing(self.root)
        report = usage_snapshot.snapshot(config)
        self.assertEqual(report['providers'][0]['accounts'][0]['statusline_usage'], 'live')
        self.assertEqual(usage_tests.listing(self.root), before)

    def test_returning_to_login_a_does_not_use_login_bs_no_limits_file(self):
        self.wrap()
        config = self.choice(True)
        account = Account('work', self.claude_dir)
        login_path = self.claude_dir / '.claude.json'
        write_json(login_path, {'oauthAccount': {'accountUuid': 'login-a'}})
        login_a = accounts._folder_login(self.claude_dir)
        write_json(accounts._live_path(self.claude_dir), {'login': login_a, 'limits': True})
        write_json(login_path, {'oauthAccount': {'accountUuid': 'login-b'}})
        login_b = accounts._folder_login(self.claude_dir)
        self.assertNotEqual(login_a, login_b)
        write_json(accounts._live_path(self.claude_dir), {'login': login_b, 'limits': False})
        self.assertEqual(usage_snapshot.statusline_usage(config, account), 'no_limits')
        write_json(login_path, {'oauthAccount': {'accountUuid': 'login-a'}})
        self.assertEqual(accounts._folder_login(self.claude_dir), login_a)
        before = usage_tests.listing(self.root)
        self.assertEqual(usage_snapshot.statusline_usage(config, account), 'live')
        code, output = self.doctor(config, '--json')
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['accounts'][0]['statusline_usage'], 'live')
        self.assertEqual(usage_tests.listing(self.root), before)

    def test_doctor_replaced_command_recommends_setup_and_keeps_the_new_command(self):
        self.wrap()
        replacement = {'statusLine': {'type': 'command', 'command': 'printf replacement'}}
        write_json(self.settings, replacement)
        config = self.choice(True)
        before = usage_tests.listing(self.root)
        code, output = self.doctor(config)
        self.assertEqual(code, 1, output)
        self.assertIn("✗ Another tool replaced work's status line, so its usage updates only from agt's jobs", output)
        self.assertIn('Fix it   agt setup (keeps the new status line)', output)
        self.assertEqual(usage_tests.listing(self.root), before)
        code, output = self.doctor(config, '--fix')
        self.assertEqual(code, 0, output)
        self.assertEqual(statusline.parse(read_json(self.settings)['statusLine']['command'])[0], 'printf replacement')

    def test_doctor_edited_agt_tap_recommends_manual_edit_and_never_rewrites_it(self):
        self.wrap()
        data = read_json(self.settings)
        data['statusLine']['command'] += '; printf edited'
        write_json(self.settings, data)
        self.assertTrue(statusline.is_ours(data['statusLine']['command']))
        self.assertIsNone(statusline.parse(data['statusLine']['command']))
        config = self.choice(True)
        expected = "work's status line holds an edited agt tap that agt can't read, so agt leaves it alone"
        fix = f'edit "statusLine" in {self.settings} yourself'
        before = usage_tests.listing(self.root)
        for extra in [(), ('--json',)]:
            code, output = self.doctor(config, *extra)
            self.assertEqual(code, 1, output)
            if '--json' in extra:
                report = json.loads(output)
                self.assertEqual(report['accounts'][0]['statusline_usage'], 'replaced')
                self.assertEqual(report['problems'], [{'problem': expected, 'fix': fix}])
            else:
                self.assertIn('✗ ' + expected, output)
                self.assertIn('Fix it   ' + fix, output)
            self.assertNotIn('keeps the new status line', output)
            self.assertEqual(usage_tests.listing(self.root), before)
        code, output = self.doctor(config, '--fix')
        self.assertEqual(code, 1, output)
        self.assertIn('Fix it   ' + fix, output)
        self.assertNotIn('Fix it   agt setup', output)
        self.assertEqual(read_json(self.settings), data)

    def test_usage_json_adds_only_claude_field_and_never_writes(self):
        config = self.choice(None)
        config.runtime('claude')._found = []
        before = usage_tests.listing(self.root)
        with patch('agents_router.config.load_config', return_value=config), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['usage', '--json']), 0)
        report = json.loads(output.getvalue())
        for provider in report['providers']:
            for account in provider['accounts']:
                if provider['runtime'] == 'claude':
                    self.assertIn('statusline_usage', account)
                    self.assertEqual(account['statusline_usage'], 'unset')
                else:
                    self.assertNotIn('statusline_usage', account)
        self.assertEqual(usage_tests.listing(self.root), before)

    def test_doctor_fix_reapplies_saved_choice(self):
        config = self.choice(True)
        code, output = self.doctor(config, '--fix')
        self.assertEqual(code, 0, output)
        self.assertEqual(statusline.state(self.claude_dir), 'live')

    def test_generated_headless_homes_have_no_tap_state_or_doctor_problem(self):
        config = self.choice(True)
        self.claude_dir = homes_dir() / 'headless'
        self.claude_dir.mkdir(parents=True)
        self.assertIsNone(usage_snapshot.statusline_usage(config, Account('work', self.claude_dir)))
        code, output = self.doctor(config, '--json')
        self.assertEqual(code, 0, output)
        self.assertNotIn('statusline_usage', json.loads(output)['accounts'][0])

    def test_windows_setup_omits_item_and_unset_doctor_is_a_successful_note(self):
        args = cli.build_parser().parse_args(['setup', '--host', 'claude', '--share', 'off', '--roles', 'off', '--dry-run'])
        with patch.object(statusline, '_windows', return_value=True):
            proposal = setup_screen.proposal(args, setup_screen.build(args))
            self.assertNotIn('statusline', proposal.get('switches', {}))
            self.assertNotIn('--statusline', proposal['command'])
            self.assertFalse(any('status line:' in line for line in proposal['plugin']['planned']))
            code, output = self.doctor(self.choice(None))
        self.assertEqual(code, 0, output)
        self.assertIn('usage from jobs only (live usage needs macOS or Linux)', output)
