import io
import ctypes
import json
import logging
import os
import queue
import struct
import tempfile
import unittest
import urllib.error
import winreg
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

import translate_popup as app
import screen_context as screen


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(app.DEFAULT_CONFIG)
        self.logger = logging.getLogger("translation_test")

    def test_explicit_key_takes_priority(self):
        self.cfg["api_key"] = " test-official-key "
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-env-key"}, clear=True):
            self.assertEqual(app.resolve_api(self.cfg),
                             ("test-official-key", "https://api.deepseek.com"))

    def test_official_environment_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": " test-env-key "}, clear=True):
            self.assertEqual(app.resolve_api(self.cfg),
                             ("test-env-key", "https://api.deepseek.com"))

    def test_custom_environment_key(self):
        self.cfg["api_key_env"] = "TRANSLATOR_API_KEY"
        with patch.dict(os.environ, {"TRANSLATOR_API_KEY": "test-env-key"}, clear=True):
            self.assertEqual(app.resolve_api(self.cfg)[0], "test-env-key")

    def test_missing_key_does_not_read_other_files_or_send_requests(self):
        with patch.dict(os.environ, {}, clear=True), patch("builtins.open") as read, \
                patch.object(app.urllib.request, "urlopen") as send:
            with self.assertRaisesRegex(app.TranslationError, "缺少 API key"):
                app.translate("你好", self.cfg, self.logger)
        read.assert_not_called()
        send.assert_not_called()

    def test_401_is_not_retried_and_does_not_expose_response(self):
        error = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions", 401, "Unauthorized", {},
            io.BytesIO(b'Authentication failed: test-official-key'))
        with patch.object(app.urllib.request, "urlopen", side_effect=error) as send:
            with self.assertRaises(app.TranslationError) as caught:
                app.call_model(("test-official-key", "https://api.deepseek.com"),
                               self.cfg, [], self.logger)
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("test-official-key", str(caught.exception))
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(send.call_count, 1)

    def test_other_http_errors_redact_key(self):
        error = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions", 403, "Forbidden", {},
            io.BytesIO(b'Forbidden: test-official-key'))
        with patch.object(app.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(app.TranslationError) as caught:
                app._post_chat("none", ("test-official-key", "https://api.deepseek.com"),
                               self.cfg, [], 2000)
        self.assertNotIn("test-official-key", str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_official_request_and_independent_back_translation(self):
        self.cfg["api_key"] = "test-official-key"
        responses = [
            io.BytesIO(json.dumps({"choices": [{"message": {"content": text}}]}).encode())
            for text in ("Hello", "你好")
        ]
        with patch.object(app.urllib.request, "urlopen", side_effect=responses) as send:
            result = app.translate("你好", self.cfg, self.logger)
        self.assertEqual(result["english"], "Hello")
        self.assertEqual(result["chinese"], "你好")
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            request = call.args[0]
            self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-official-key")
            self.assertEqual(json.loads(request.data)["model"], "deepseek-flash")
            self.assertEqual(json.loads(request.data)["reasoning_effort"], "none")
        back_payload = json.loads(send.call_args_list[1].args[0].data)
        self.assertEqual(back_payload["messages"][1]["content"], "Hello")
        self.assertNotIn("你好", json.dumps(back_payload, ensure_ascii=False))

    def test_next_submission_reloads_configuration(self):
        popup = app.PopupApp.__new__(app.PopupApp)
        popup.busy = False
        popup.input = Mock()
        popup.input.get.return_value = "你好"
        popup.cfg = self.cfg
        popup.out_en = Mock()
        popup.out_zh = Mock()
        popup.set_text = Mock()
        popup._apply_state = Mock()
        popup._animate_progress = Mock()
        popup.context_hwnd = 123
        popup._hwnd = Mock(return_value=456)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch.object(app, "CONFIG_PATH", str(path)), \
                    patch.object(app.threading, "Thread") as thread:
                for key in ("test-first-key", "test-replacement-key"):
                    path.write_text(json.dumps({"api_key": key}), encoding="utf-8")
                    popup.busy = False
                    popup.start_translate()
                    self.assertEqual(app.resolve_api(popup.cfg)[0], key)
                self.assertEqual(thread.return_value.start.call_count, 2)

    def test_thinking_opt_in_changes_requests_without_replacing_saved_effort(self):
        for enabled, expected in ((False, "none"), (True, "high"), (False, "none")):
            with self.subTest(enabled=enabled):
                cfg = dict(self.cfg, thinking_enabled=enabled, reasoning_effort="high")
                reply = io.BytesIO(b'{"choices":[{"message":{"content":"hello"}}]}')
                with patch.object(app.urllib.request, "urlopen", return_value=reply) as send:
                    app.call_model(("test-key", "https://api.deepseek.com"), cfg, [], self.logger)
                self.assertEqual(json.loads(send.call_args.args[0].data)["reasoning_effort"], expected)
                self.assertEqual(cfg["reasoning_effort"], "high")

    def test_legacy_effort_without_opt_in_defaults_to_no_thinking(self):
        cfg = {"api_key": "test-key", "reasoning_effort": "low"}
        reply = io.BytesIO(b'{"choices":[{"message":{"content":"hello"}}]}')
        with patch.object(app.urllib.request, "urlopen", return_value=reply) as send:
            app.call_model(("test-key", "https://api.deepseek.com"), cfg, [], self.logger)
        self.assertEqual(json.loads(send.call_args.args[0].data)["reasoning_effort"], "none")
        self.assertNotIn("thinking_enabled", cfg)

    def test_missing_screen_option_never_sends_image(self):
        cfg = {"api_key": "test-key"}
        reply = io.BytesIO(b'{"choices":[{"message":{"content":"hello"}}]}')
        with patch.object(app.urllib.request, "urlopen", return_value=reply) as send:
            app.translate("Hello", cfg, self.logger, screen_image="data:image/png;base64,c2NyZWVu")
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"], "Hello")
        self.assertEqual(payload["reasoning_effort"], "none")

    def test_loading_defaults_never_rewrites_existing_config(self):
        original = '{ "reasoning_effort": "low", "custom_setting": [1, 2] }'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(original, encoding="utf-8")
            with patch.object(app, "CONFIG_PATH", str(path)):
                cfg = app.load_config()
            self.assertFalse(cfg["thinking_enabled"])
            self.assertFalse(cfg["screen_context"])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_failed_save_keeps_original_config_intact(self):
        original = '{ "reasoning_effort": "high", "custom_setting": 42 }'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(original, encoding="utf-8")
            with patch.object(app, "CONFIG_PATH", str(path)), \
                    patch.object(app.os, "replace", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    app.save_config({"thinking_enabled": True})
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])


class ScreenContextTests(unittest.TestCase):
    image = "data:image/png;base64,c2NyZWVu"

    def setUp(self):
        self.cfg = dict(app.DEFAULT_CONFIG, api_key="test-official-key", screen_context=True,
                        thinking_enabled=True)
        self.logger = Mock()

    def request_payloads(self, source, responses, **cfg):
        replies = [io.BytesIO(json.dumps({"choices": [{"message": {"content": text}}]}).encode())
                   for text in responses]
        with patch.object(app.urllib.request, "urlopen", side_effect=replies) as send:
            result = app.translate(source, dict(self.cfg, **cfg), self.logger,
                                   screen_image=self.image)
        return result, [json.loads(call.args[0].data) for call in send.call_args_list]

    def test_image_is_sent_with_original_but_not_independent_back_translation(self):
        result, payloads = self.request_payloads("这个字段不能为空", ["This field cannot be NULL.", "该字段不能为NULL。"])
        parts = payloads[0]["messages"][1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "这个字段不能为空"})
        self.assertEqual(parts[1]["image_url"]["url"], self.image)
        self.assertTrue(result["screen_context_used"])
        self.assertEqual(payloads[1]["messages"][1]["content"], "This field cannot be NULL.")
        self.assertNotIn("data:image", json.dumps(payloads[1]))
        self.assertNotIn("这个字段不能为空", json.dumps(payloads[1], ensure_ascii=False))
        self.assertEqual(payloads[0]["reasoning_effort"], "low")

    def test_english_input_uses_image_in_its_only_request(self):
        result, payloads = self.request_payloads("Open the table", ["打开数据表"])
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["messages"][1]["content"][1]["image_url"]["url"], self.image)
        self.assertEqual(result["chinese"], "打开数据表")

    def test_disabled_context_does_not_transmit_even_an_explicit_image(self):
        result, payloads = self.request_payloads("Open the table", ["打开表格"], screen_context=False)
        self.assertFalse(result["screen_context_used"])
        self.assertEqual(payloads[0]["messages"][1]["content"], "Open the table")
        self.assertNotIn("data:image", json.dumps(payloads))

    def test_echoed_image_and_key_are_removed_from_errors(self):
        body = json.dumps({"error": self.image, "key": "test-official-key"}).encode()
        error = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 400,
                                       "Bad request", {}, io.BytesIO(body))
        with patch.object(app.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(app.TranslationError) as caught:
                app._post_chat("low", ("test-official-key", "https://api.deepseek.com"),
                               self.cfg, [], 100)
        self.assertNotIn(self.image, str(caught.exception))
        self.assertNotIn("c2NyZWVu", str(caught.exception))
        self.assertNotIn("test-official-key", str(caught.exception))

    def popup(self):
        popup = app.PopupApp.__new__(app.PopupApp)
        popup.events = queue.Queue()
        popup.logger = self.logger
        return popup

    def test_capture_failure_still_translates_and_reports_text_only(self):
        popup = self.popup()
        with patch.object(app, "capture_screen_context", side_effect=screen.ScreenCaptureError("unavailable")), \
                patch.object(app, "translate", return_value={"screen_context_used": False}) as translate:
            popup._worker("Hello", self.cfg, 123, 456)
        self.assertIsNone(translate.call_args.kwargs["screen_image"])
        events = list(popup.events.queue)
        self.assertEqual(events[-1][0], "done")
        self.assertTrue(events[-1][1]["screen_capture_failed"])

    def test_disabled_context_never_captures(self):
        popup = self.popup()
        with patch.object(app, "capture_screen_context") as capture, \
                patch.object(app, "translate", return_value={"screen_context_used": False}):
            popup._worker("Hello", dict(self.cfg, screen_context=False), 123, 456)
        capture.assert_not_called()

    def test_each_submission_captures_a_new_frame(self):
        popup = self.popup()
        frames = [screen.ScreenCapture(self.image + suffix, 800, 600, 100) for suffix in ("A", "B")]
        with patch.object(app, "capture_screen_context", side_effect=frames) as capture, \
                patch.object(app, "translate", side_effect=[{}, {}]) as translate:
            popup._worker("Hello", self.cfg, 123, 456)
            popup._worker("Hello", self.cfg, 123, 456)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual([call.kwargs["screen_image"] for call in translate.call_args_list],
                         [frame.data_url for frame in frames])

    def test_toggle_persists_and_preserves_other_settings(self):
        popup = self.popup()
        popup.busy = False
        popup._redraw = Mock()
        popup.set_status = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(dict(self.cfg, max_tokens=1234)), encoding="utf-8")
            with patch.object(app, "CONFIG_PATH", str(path)):
                popup.toggle_screen_context()
                self.assertFalse(app.load_config()["screen_context"])
                self.assertEqual(app.load_config()["max_tokens"], 1234)
                popup.toggle_screen_context()
                self.assertTrue(app.load_config()["screen_context"])

    def test_toggles_only_save_the_selected_option(self):
        popup = self.popup()
        popup.busy = False
        popup._redraw = Mock()
        popup.set_status = Mock()
        original = {"api_key": "test-key", "reasoning_effort": "high",
                    "screen_context": False, "custom_setting": {"preserve": True}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            with patch.object(app, "CONFIG_PATH", str(path)):
                popup.toggle_thinking()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                                 dict(original, thinking_enabled=True))
                self.assertEqual(app.effective_reasoning_effort(popup.cfg), "high")
                popup.toggle_screen_context()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                                 dict(original, thinking_enabled=True, screen_context=True))
                popup.toggle_thinking()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                                 dict(original, thinking_enabled=False, screen_context=True))
                self.assertEqual(app.effective_reasoning_effort(popup.cfg), "none")

    def test_png_preserves_row_order_channels_and_ignores_padding(self):
        # Red pixel then blue pixel on separate top-down, padded BGR rows.
        png = screen._encode_png(b"\x00\x00\xff\x00\xff\x00\x00\x00", 1, 2, 4)
        self.assertEqual(struct.unpack(">II", png[16:24]), (1, 2))
        offset, compressed = 8, b""
        while offset < len(png):
            size = struct.unpack(">I", png[offset:offset + 4])[0]
            tag = png[offset + 4:offset + 8]
            data = png[offset + 8:offset + 8 + size]
            if tag == b"IDAT":
                compressed += data
            offset += 12 + size
        self.assertEqual(zlib.decompress(compressed), b"\x00\xff\x00\x00\x00\x00\x00\xff")

    def test_failed_gdi_capture_releases_all_resources(self):
        user, gdi = Mock(), Mock()
        user.GetDC.return_value = 11
        gdi.CreateCompatibleDC.return_value = 12
        def create_bitmap(_dc, _header, _usage, bits, _section, _offset):
            ctypes.cast(bits, ctypes.POINTER(ctypes.c_void_p))[0] = 1234
            return 13
        gdi.CreateDIBSection.side_effect = create_bitmap
        gdi.SelectObject.return_value = 14
        gdi.StretchBlt.return_value = 0
        with self.assertRaises(screen.ScreenCaptureError):
            screen._capture_png(user, gdi, (-1920, 0, 0, 1080), 1920)
        gdi.SelectObject.assert_any_call(12, 14)
        gdi.DeleteObject.assert_called_once_with(13)
        gdi.DeleteDC.assert_called_once_with(12)
        user.ReleaseDC.assert_called_once_with(None, 11)

    def test_exclusion_is_restored_even_when_capture_fails(self):
        user = Mock()
        dwm = Mock()
        dwm.DwmFlush.return_value = 0
        with patch.object(screen.ctypes, "WinDLL", return_value=dwm), \
                patch.object(screen.sys, "getwindowsversion", return_value=Mock(build=26100)):
            with self.assertRaises(screen.ScreenCaptureError):
                with screen._exclude_window(user, 123):
                    raise screen.ScreenCaptureError("capture failed")
        self.assertEqual([call.args for call in user.SetWindowDisplayAffinity.call_args_list],
                         [(123, 0x11), (123, 0)])


class ResidentTests(unittest.TestCase):
    def setUp(self):
        self.logger = Mock()

    def run_with_codes(self, codes):
        children = [Mock(pid=100 + index, wait=Mock(return_value=code))
                    for index, code in enumerate(codes)]
        with patch.object(app, "acquire_single_instance", return_value=1), \
                patch.object(app, "_worker_environment", return_value={}), \
                patch.object(app.subprocess, "Popen", side_effect=children) as spawn, \
                patch.object(app.time, "sleep") as sleep, \
                patch.object(app.time, "monotonic", return_value=10), \
                patch.object(app.ctypes, "WinDLL"):
            result = app.run_resident(self.logger)
        return result, spawn.call_count, sleep.call_count

    def test_unexpected_exit_restarts_worker(self):
        self.assertEqual(self.run_with_codes([1, 0]), (0, 2, 1))

    def test_normal_exit_stops_supervisor(self):
        self.assertEqual(self.run_with_codes([0]), (0, 1, 0))

    def test_repeated_crashes_back_off_without_abandoning_recovery(self):
        self.assertEqual(self.run_with_codes([1, 1, 1, 1, 0]), (0, 5, 4))

    def test_duplicate_supervisor_does_not_spawn_worker(self):
        with patch.object(app, "acquire_single_instance", return_value=None), \
                patch.object(app.subprocess, "Popen") as spawn:
            self.assertEqual(app.run_resident(self.logger), 0)
        spawn.assert_not_called()

    def test_launcher_with_stale_environment_reads_user_key(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(app, "load_config", return_value=dict(app.DEFAULT_CONFIG)), \
                patch.object(winreg, "OpenKey"), \
                patch.object(winreg, "QueryValueEx", return_value=("test-user-key", winreg.REG_SZ)):
            self.assertEqual(app._worker_environment()["DEEPSEEK_API_KEY"], "test-user-key")

    def test_existing_environment_key_is_preserved(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-env-key"}, clear=True), \
                patch.object(app, "load_config", return_value=dict(app.DEFAULT_CONFIG)), \
                patch.object(winreg, "OpenKey") as registry:
            self.assertEqual(app._worker_environment()["DEEPSEEK_API_KEY"], "test-env-key")
        registry.assert_not_called()


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        self.fire = Mock()
        self.logger = Mock()
        self.manager = app.HotkeyManager('win+shift+f23', 'ctrl+alt+z', self.fire, Mock(), self.logger)
        self.manager.user32 = Mock()
        self.manager.kernel32 = Mock()
        self.manager.user32.CallNextHookEx.return_value = 77
        self.manager.hook_vk = 0x86
        self.manager.hook_mods = app.MOD_WIN | app.MOD_SHIFT
        self.manager.thread_id = 123
        self.pressed = {0x5B, 0x10}
        self.manager.user32.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key in self.pressed else 0

    def send(self, message, vk=0x86):
        key = app.KBDLLHOOKSTRUCT()
        key.vkCode = vk
        return self.manager._hook_cb(0, message, ctypes.addressof(key))

    def test_matching_chord_is_swallowed_and_work_is_queued(self):
        self.assertEqual(self.send(app.WM_KEYDOWN), 1)
        self.manager.user32.PostThreadMessageW.assert_called_once_with(123, app.WM_HOOK_FIRE, 0, 0)
        self.fire.assert_not_called()
        self.assertEqual(self.logger.mock_calls, [])

    def test_unrelated_keys_and_wrong_modifiers_pass_through(self):
        self.assertEqual(self.send(app.WM_KEYDOWN, ord('A')), 77)
        self.pressed.clear()
        self.assertEqual(self.send(app.WM_KEYDOWN), 77)
        self.manager.user32.PostThreadMessageW.assert_not_called()

    def test_repeat_is_suppressed_and_release_resets_chord(self):
        self.assertEqual(self.send(app.WM_KEYDOWN), 1)
        self.assertEqual(self.send(app.WM_KEYDOWN), 1)
        self.assertEqual(self.manager.user32.PostThreadMessageW.call_count, 1)
        self.pressed.clear()
        self.assertEqual(self.send(app.WM_KEYUP), 1)
        self.pressed.update({0x5B, 0x10})
        self.assertEqual(self.send(app.WM_KEYDOWN), 1)
        self.assertEqual(self.manager.user32.PostThreadMessageW.call_count, 2)

    def test_hook_refresh_installs_before_removing_previous(self):
        self.manager.hook_handle = 11
        self.manager.user32.SetWindowsHookExW.return_value = 22
        self.manager.kernel32.GetModuleHandleW.return_value = 1
        self.assertTrue(self.manager._install_hook(0x86))
        self.assertEqual(self.manager.hook_handle, 22)
        calls = self.manager.user32.mock_calls
        self.assertLess(next(i for i,c in enumerate(calls) if c[0]=='SetWindowsHookExW'),
                        next(i for i,c in enumerate(calls) if c[0]=='UnhookWindowsHookEx'))
        self.manager.user32.UnhookWindowsHookEx.assert_called_once_with(11)

    def test_failed_refresh_keeps_previous_hook(self):
        self.manager.hook_handle = 11
        self.manager.kernel32.GetModuleHandleW.return_value = 1
        self.manager.user32.SetWindowsHookExW.return_value = 0
        self.assertFalse(self.manager._install_hook(0x86))
        self.assertEqual(self.manager.hook_handle, 11)
        self.manager.user32.UnhookWindowsHookEx.assert_not_called()

    def test_thread_joins_and_cleans_up_resources(self):
        self.manager.hook_handle = 11
        self.manager._timer_id = 12
        self.manager._registered = {1: 'win+shift+f23'}
        self.manager._run = Mock()
        self.manager.start()
        self.manager.join(timeout=2)
        self.assertFalse(self.manager.is_alive())
        self.manager.user32.UnhookWindowsHookEx.assert_called_once_with(11)
        self.manager.user32.KillTimer.assert_called_once_with(None, 12)
        self.manager.user32.UnregisterHotKey.assert_called_once_with(None, 1)


if __name__ == "__main__":
    unittest.main()
