import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class SetupScriptTests(unittest.TestCase):
    def test_runtime_uses_restored_onnx_ocr_dependencies(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        setup_script = (ROOT / "setup.ps1").read_text(encoding="utf-8")

        self.assertIn("numpy>=1.24,<2.4", requirements)
        self.assertIn("onnxruntime-directml==1.24.4", requirements)
        self.assertIn("pyclipper==1.4.0", requirements)
        self.assertIn("PySide6_Essentials", requirements)
        self.assertNotIn("winrt-Windows.Media.Ocr", requirements)
        self.assertNotIn("Install-Paddle", setup_script)
        self.assertNotIn("SkipPaddle", setup_script)
        self.assertIn('$ocrDir = Join-Path $Root "runtime\\ocr"', setup_script)
        self.assertIn(
            '$ocrRequired = @("manifest.json", "det.onnx", "rec.onnx", "characters.txt")',
            setup_script,
        )
        self.assertIn("Get-FileHash -LiteralPath (Join-Path $ocrDir $name)", setup_script)

    def test_install_and_check_modes_verify_dependency_consistency(self):
        setup_script = (ROOT / "setup.ps1").read_text(encoding="utf-8")

        self.assertIn("function Test-PythonDependencies", setup_script)
        self.assertEqual(
            setup_script.count("Test-PythonDependencies -VenvPy $venvPy"),
            2,
        )
        self.assertIn("& $VenvPy -m pip check", setup_script)

    def test_pip_install_retries_without_a_broken_cache(self):
        setup_script = (ROOT / "setup.ps1").read_text(encoding="utf-8")

        self.assertIn("function Install-PipWithRetry", setup_script)
        self.assertIn("-m pip install --no-cache-dir @Arguments", setup_script)
        self.assertIn('Arguments @("-r", (Join-Path $Root "requirements.txt"))', setup_script)

    def test_default_install_builds_launcher_when_it_is_missing(self):
        setup_script = (ROOT / "setup.ps1").read_text(encoding="utf-8")

        self.assertIn('$launcherExe = Join-Path $Root "翻译.exe"', setup_script)
        self.assertIn(
            "if ($BuildLauncher -or -not (Test-Path -LiteralPath $launcherExe))",
            setup_script,
        )
        self.assertIn("Invoke-BuildLauncher -VenvPy $venvPy", setup_script)
        self.assertIn('-Arguments @("pyinstaller")', setup_script)
        self.assertIn("翻译.exe 生成结束后仍未找到", setup_script)

    def test_runtime_assets_are_release_only(self):
        setup_script = (ROOT / "setup.ps1").read_text(encoding="utf-8")
        self.assertNotIn("DownloadRuntime", setup_script)
        self.assertFalse((ROOT / "scripts" / "download_runtime.ps1").exists())

    def test_ci_does_not_install_paddle_or_require_release_assets(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("paddlepaddle", workflow.lower())
        self.assertIn(
            "python scripts/smoke_import.py --skip-runtime-assets",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
