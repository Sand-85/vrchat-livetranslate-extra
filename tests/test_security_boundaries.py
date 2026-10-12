"""Security regressions with synthetic tokens and isolated local files only."""
import asyncio
import base64
import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vlt import credentials
from vlt.room.client import RoomClient
from vlt.room.model import RoomConfig


class BoundaryTests(unittest.TestCase):
    def test_malformed_sensitive_configuration_is_never_logged(self):
        secret = 9876543210123456789
        inputs = [{'token': secret}, {'token': [str(secret)]},
                  {'server_url': {'url': f'wss://example.invalid/?token={secret}'}},
                  [{'token': secret}]]
        for raw in inputs:
            with self.subTest(raw_type=type(raw).__name__):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    RoomConfig.from_dict(raw)
                self.assertNotIn(str(secret), output.getvalue())
                self.assertIn('非法', output.getvalue())

    def test_invalid_ports_are_rejected_before_a_thread_starts(self):
        for authority in ('example.invalid:bad', 'example.invalid:65536',
                          'example.invalid:-1', 'example.invalid:0', '[::1]suffix:80'):
            cfg = RoomConfig(server_url=f'wss://{authority}/ws', room_code='ABCD1234')
            with self.subTest(authority=authority), \
                 patch('vlt.room.client.threading.Thread') as thread, \
                 contextlib.redirect_stdout(io.StringIO()):
                client = RoomClient(cfg, lambda _: None)
                client.start()
                thread.assert_not_called()
                self.assertEqual(cfg.unavailable_reason(), 'server_url 格式无效')
        for authority in ('example.invalid:443', '[::1]:12345'):
            self.assertIsNone(RoomConfig(server_url=f'wss://{authority}/ws',
                                         room_code='ABCD1234').unavailable_reason())

    def test_only_loopback_may_use_plaintext(self):
        for host in ('example.invalid', '192.168.1.1', 'localhost.evil.invalid'):
            self.assertIsNotNone(RoomConfig(server_url=f'ws://{host}/ws',
                                           room_code='ABCD1234').unavailable_reason())
        for host in ('127.0.0.1', '127.0.0.2', '[::1]', 'localhost'):
            self.assertIsNone(RoomConfig(server_url=f'ws://{host}/ws',
                                        room_code='ABCD1234').unavailable_reason())

    def test_legacy_query_token_is_migrated_out_of_url(self):
        cfg = RoomConfig(server_url='wss://example.invalid/ws?room=ABCD1234&k=synthetic-token&x=1',
                         room_code='ABCD1234')
        client = RoomClient(cfg, lambda _: None)
        self.assertEqual(client._connect_url(), 'wss://example.invalid/ws?room=ABCD1234&x=1')
        self.assertEqual(client._auth_token(), 'synthetic-token')

    def test_legacy_query_credentials_are_absent_from_startup_logs_and_status(self):
        token = 'synthetic-startup-credential'
        for key in ('k', 'tok', 'token', '%6b', '%74ok', '%74oken'):
            with self.subTest(key=key):
                cfg = RoomConfig(server_url=f'wss://example.invalid/ws?{key}={token}&x=1',
                                 room_code='ABCD1234', nickname='測試')
                status = []
                client = RoomClient(cfg, lambda _: None, status.append)
                captured = io.StringIO()
                with patch('vlt.room.client.threading.Thread'), \
                     patch.object(client._ready, 'wait', return_value=True), \
                     contextlib.redirect_stdout(captured):
                    client.start()
                self.assertNotIn(token, captured.getvalue())
                self.assertNotIn(token, '\n'.join(status))
                self.assertIn('example.invalid/ws', captured.getvalue())
                self.assertIn('x=1', captured.getvalue())
                self.assertEqual(client._auth_token(), token)
                self.assertNotIn(token, client._connect_url())

    def test_invalid_endpoint_does_not_echo_credentials_in_error_state(self):
        token = 'synthetic-invalid-endpoint-credential'
        cfg = RoomConfig(server_url=f'https://example.invalid/ws?token={token}',
                         room_code='ABCD1234')
        status = []
        client = RoomClient(cfg, lambda _: None, status.append)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            client.start()
        self.assertNotIn(token, cfg.unavailable_reason())
        self.assertNotIn(token, client.state().last_error)
        self.assertNotIn(token, captured.getvalue())
        self.assertNotIn(token, '\n'.join(status))
        self.assertIn('ws://', cfg.unavailable_reason())

    def test_invalid_unicode_endpoint_is_rejected_without_startup_exception(self):
        self.assertIsNone(RoomConfig(server_url='wss://example.invalid/路徑?名稱=測試日本語😀',
                                     room_code='ABCD1234').unavailable_reason())
        cfg = RoomConfig(server_url='wss://example.invalid/ws?x=' + chr(0xD800),
                         room_code='ABCD1234')
        client = RoomClient(cfg, lambda _: None)
        with patch('vlt.room.client.threading.Thread') as thread, \
             contextlib.redirect_stdout(io.StringIO()):
            client.start()
        thread.assert_not_called()
        self.assertEqual(cfg.unavailable_reason(), 'server_url 格式无效')
        self.assertEqual(client.state().last_error, cfg.unavailable_reason())

    def test_scanner_blocks_secret_without_printing_it(self):
        spec = importlib.util.spec_from_file_location('scanner', ROOT/'scripts/check_no_secrets.py')
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)
        fake = 'sk-' + 'A'*32
        captured = io.StringIO()
        with patch.object(scanner, 'staged_files', return_value=['synthetic.txt']), \
             patch('builtins.open', mock_open(read_data=fake.encode())), \
             patch.object(sys, 'argv', ['scanner']), contextlib.redirect_stdout(captured):
            self.assertEqual(scanner.main(), 1)
        self.assertNotIn(fake, captured.getvalue())
        self.assertNotIn('A'*8, captured.getvalue())
        self.assertIn('synthetic.txt', captured.getvalue())

    def test_failed_key_replacement_preserves_old_key_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            key = storage/'api_key.txt'
            key.write_text('old-synthetic-key', encoding='utf-8')
            with patch.object(credentials, '_storage_dir_override', lambda: storage), \
                 patch.object(credentials.os, 'replace', side_effect=OSError('replacement failed')):
                with self.assertRaises(OSError):
                    credentials.save_api_key('new-synthetic-key')
            self.assertEqual(key.read_text(encoding='utf-8'), 'old-synthetic-key')
            self.assertEqual(list(storage.iterdir()), [key])

    def test_release_action_is_pinned_and_only_publisher_can_write(self):
        """发布动作必须**钉在 tag 上**、所有 action 都 pin 版本，且写权限只在建 release 那一处。

        ⚠️ 与上游不同（**有意**）：上游 `release.yml` 是 `build` / `release` 两个 job 的
        最小权限模型（顶层 read、只有 release job 拿 write）；本仓库的 `release.yml` 是**定制版**：
        「只发 Windows + 手写 Release 说明」且只有**一个** job，由它自己建 Release →
        顶层就是 `contents: write`。所以这条用例在本仓库守的是我们自己shape 上的等价判据
        （写权限归属唯一一个 job + 发布动作 gate 在 tag 上 + action 全部 pin 版本）。
        要跟着上游拆成两个 job 的话，这条判据要一起改。
        （上游还把发布 action pin 到 **40 位 commit SHA**；本仓库所有 workflow 统一 pin 到
        **tag**（`@v7` / `@v3`），所以这里只判「不是 @main/@master」—— 要跟 SHA 就一起改。）
        """
        workflow = yaml.safe_load((ROOT/'.github/workflows/release.yml').read_text(encoding='utf-8'))
        # 1) 写权限只在**唯一**一个 job 里（本仓库 shape：build 兼发布）
        self.assertEqual(workflow['permissions']['contents'], 'write')
        self.assertEqual(sorted(workflow['jobs']), ['build'])
        # 2) 发布动作 gate 在 tag 上：workflow_dispatch 只出 artifact，不许凭空建 Release
        steps = workflow['jobs']['build']['steps']
        publish = [s for s in steps if 'action-gh-release' in str(s.get('uses', ''))]
        self.assertEqual(len(publish), 1, f"建 Release 的步骤应当恰好 1 个：{publish}")
        self.assertIn("refs/tags/", str(publish[0].get('if', '')),
                      "建 Release 的步骤必须 gate 在 tag 上")
        # 3) 所有 action 都 pin 具体版本（不许 @main / @master 漂移）
        for job in workflow['jobs'].values():
            for step in job['steps']:
                ref = str(step.get('uses', ''))
                if ref and '@' in ref:
                    self.assertNotIn(ref.split('@')[1], ('main', 'master'),
                                     f"action 未 pin 版本：{ref}")


class HeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_uses_header_and_redirects_are_disabled(self):
        client = RoomClient(RoomConfig(server_url='wss://example.invalid/ws', token='synthetic-token'),
                            lambda _: None)
        connection = asyncio.get_running_loop().create_future()
        connection.set_result(None)
        connection.process_redirect = Mock()
        with patch('websockets.connect', return_value=connection) as connect:
            await client._open_ws(client._connect_url())
        self.assertEqual(connect.call_args.kwargs['additional_headers'],
                         {'Authorization': 'Bearer synthetic-token'})
        error = RuntimeError('redirect')
        self.assertIs(connection.process_redirect(error), error)

    async def test_unicode_tokens_roundtrip_through_ascii_header(self):
        token = '合成令牌 テスト'
        client = RoomClient(RoomConfig(server_url='wss://example.invalid/ws', token=token), lambda _: None)
        connection = asyncio.get_running_loop().create_future()
        connection.set_result(None)
        with patch('websockets.connect', return_value=connection) as connect:
            await client._open_ws(client._connect_url())
        header = connect.call_args.kwargs['additional_headers']['Authorization']
        self.assertTrue(header.isascii())
        self.assertEqual(header.split()[0], 'VLT')
        self.assertEqual(base64.urlsafe_b64decode(header.split()[1]).decode('utf-8'), token)


if __name__ == '__main__':
    unittest.main()
