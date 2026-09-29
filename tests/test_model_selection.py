import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.config import DEFAULTS
from app.paths import available_translation_models, resolve_path
from app.ui.windows import SettingsWindow
from scripts import setup_config


class ModelDiscoveryTests(unittest.TestCase):
    def test_only_valid_top_level_gguf_files_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            (models / "B.GGUF").write_bytes(b"GGUFmodel-b")
            (models / "a.gguf").write_bytes(b"GGUFmodel-a")
            (models / "broken.gguf").write_bytes(b"partial")
            (models / "downloading.gguf.part").write_bytes(b"GGUFpartial")
            nested = models / "nested"
            nested.mkdir()
            (nested / "hidden.gguf").write_bytes(b"GGUFnested")

            found = available_translation_models(models)

        self.assertEqual([path.name for path in found], ["a.gguf", "B.GGUF"])


class ModelSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def test_selected_model_is_saved_and_loads_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            first = models / "first.gguf"
            second = models / "second.gguf"
            first.write_bytes(b"GGUFfirst")
            second.write_bytes(b"GGUFsecond")
            cfg = dict(DEFAULTS)
            cfg["model_path"] = str(first)
            on_saved = Mock()

            with (
                patch(
                    "app.ui.windows.available_translation_models",
                    return_value=[first, second],
                ),
                patch("app.ui.windows.config.save") as save_cfg,
                patch("app.ui.windows.show_toast") as show_toast,
            ):
                window = SettingsWindow(cfg, on_saved=on_saved)
                try:
                    self.assertEqual(
                        resolve_path(window._model_file.currentData()),
                        first.resolve(),
                    )
                    window._model_file.setCurrentIndex(1)
                    window._save()
                finally:
                    window.close()
                    window.deleteLater()
                    self.qapp.processEvents()

            self.assertEqual(resolve_path(cfg["model_path"]), second.resolve())
            save_cfg.assert_called_once_with(cfg)
            on_saved.assert_called_once_with()
            self.assertNotIn("重启", show_toast.call_args.args[0])

    def test_translation_device_is_saved_and_loads_without_restart(self):
        cfg = dict(DEFAULTS)
        with (
            patch("app.ui.windows.available_translation_models", return_value=[]),
            patch("app.ui.windows.config.save") as save_cfg,
            patch("app.ui.windows.show_toast") as show_toast,
            patch("app.hotkeys.find_hotkey_conflicts", return_value=[]),
        ):
            window = SettingsWindow(cfg)
            try:
                window._llama_device.setCurrentIndex(
                    window._llama_device.findData("cpu")
                )
                window._save()
            finally:
                window.close()
                window.deleteLater()
                self.qapp.processEvents()

        self.assertEqual(cfg["llama_device"], "cpu")
        save_cfg.assert_called_once_with(cfg)
        self.assertNotIn("重启", show_toast.call_args.args[0])

    def test_saved_translation_device_is_restored_without_becoming_auto(self):
        for device in ("cpu", "gpu"):
            cfg = dict(DEFAULTS)
            cfg["llama_device"] = device
            with patch(
                "app.ui.windows.available_translation_models",
                return_value=[],
            ):
                window = SettingsWindow(cfg)
                try:
                    self.assertEqual(window._llama_device.currentData(), device)
                finally:
                    window.close()
                    window.deleteLater()
                    self.qapp.processEvents()

class MaxTokensSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def test_presets_and_custom_value_are_both_available(self):
        cfg = dict(DEFAULTS)
        cfg["max_tokens"] = 700

        with (
            patch("app.ui.windows.available_translation_models", return_value=[]),
            patch("app.ui.windows.config.save") as save_cfg,
            patch("app.ui.windows.show_toast"),
            patch("app.hotkeys.find_hotkey_conflicts", return_value=[]),
        ):
            window = SettingsWindow(cfg)
            try:
                presets = [
                    window._max_tokens.itemData(index)
                    for index in range(window._max_tokens.count())
                    if isinstance(window._max_tokens.itemData(index), int)
                ]
                self.assertEqual(
                    presets,
                    [64, 128, 256, 512, 1024, 2048, 4096, 8192],
                )
                self.assertIn("预设", window._max_tokens_presets.text())
                self.assertEqual(window._max_tokens.currentData(), "custom")
                self.assertFalse(window._max_tokens_custom.isHidden())
                self.assertEqual(window._max_tokens_custom.value(), 700)

                window._max_tokens_custom.setValue(768)
                window._save()
            finally:
                window.close()
                window.deleteLater()
                self.qapp.processEvents()

        self.assertEqual(cfg["max_tokens"], 768)
        save_cfg.assert_called_once_with(cfg)

    def test_custom_value_is_constrained_to_valid_range(self):
        cfg = dict(DEFAULTS)
        with (
            patch("app.ui.windows.available_translation_models", return_value=[]),
        ):
            window = SettingsWindow(cfg)
            try:
                window._max_tokens.setCurrentIndex(
                    window._max_tokens.findData("custom")
                )
                window._max_tokens_custom.setValue(1)
                self.assertEqual(window._max_tokens_custom.value(), 64)
                window._max_tokens_custom.setValue(99999)
                self.assertEqual(window._max_tokens_custom.value(), 8192)
            finally:
                window.close()
                window.deleteLater()
                self.qapp.processEvents()


class SetupConfigTests(unittest.TestCase):
    def test_setup_preserves_selected_model_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = "runtime/models/custom.gguf"
            (root / "config.json").write_text(
                json.dumps({"model_path": selected}),
                encoding="utf-8",
            )
            argv = ["setup_config.py", str(root), "1", "8", "99"]
            with patch.object(sys, "argv", argv), patch("builtins.print"):
                result = setup_config.main()
            saved = json.loads((root / "config.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(saved["model_path"], selected)

    def test_setup_removes_legacy_ocr_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps({
                    "ocr_lang": "ch",
                    "hotkey_silent_ocr": "<alt>+s",
                    "source_language": "英语",
                }),
                encoding="utf-8",
            )
            argv = ["setup_config.py", str(root), "0", "8", "0"]
            with patch.object(sys, "argv", argv), patch("builtins.print"):
                result = setup_config.main()
            saved = json.loads((root / "config.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertNotIn("ocr_lang", saved)
        self.assertNotIn("hotkey_silent_ocr", saved)
        self.assertNotIn("source_language", saved)


if __name__ == "__main__":
    unittest.main()
