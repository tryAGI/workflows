import importlib.util
import json
import subprocess
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "release_impact.py"
spec = importlib.util.spec_from_file_location("release_impact", MODULE_PATH)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def package(path: Path, name: str, version: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}.nuspec", f"<package><metadata><id>{name}</id><version>{version}</version></metadata></package>")
        archive.writestr("lib/net10.0/Fixture.dll", b"fixture")


class ReleaseImpactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidates = self.root / "candidates"
        self.candidates.mkdir()
        package(self.candidates / "Fixture.1.0.1-dev.1.nupkg", "Fixture", "1.0.1-dev.1")
        self.args = Namespace(repo=self.root, packages=self.candidates,
                              apicompat=Path("/tmp/apicompat"), cache=self.root / "cache",
                              zero_major_policy="minor")

    def test_package_id_and_set(self):
        self.assertEqual(release.package_id(next(self.candidates.iterdir())), "Fixture")
        self.assertEqual(list(release.packages_in(self.candidates)), ["fixture"])

    def test_breaking_api_requires_major(self):
        with patch.object(release, "stable_tags", return_value=[((1, 0, 0), "v1.0.0")]), \
             patch.object(release, "changed_paths", return_value=["src/libs/Fixture/Api.cs"]), \
             patch.object(release, "published_versions", return_value=[(1, 0, 0)]), \
             patch.object(release, "download_baseline", return_value=Path("baseline.nupkg")), \
             patch.object(release, "compare", return_value="CP0002: old member exists on [Baseline] but not on current"):
            result = release.plan(self.args)
        self.assertEqual((result["level"], result["version"]), ("major", "2.0.0"))

    def test_removed_target_framework_requires_major(self):
        with patch.object(release, "stable_tags", return_value=[((1, 0, 0), "v1.0.0")]), \
             patch.object(release, "changed_paths", return_value=["src/libs/Fixture/Fixture.csproj"]), \
             patch.object(release, "published_versions", return_value=[(1, 0, 0)]), \
             patch.object(release, "download_baseline", return_value=Path("baseline.nupkg")), \
             patch.object(release, "compare", return_value="PKV006: Target framework net6.0 is no longer supported"):
            result = release.plan(self.args)
        self.assertEqual((result["level"], result["version"]), ("major", "2.0.0"))
        self.assertEqual(result["packages"][0]["breaking_diagnostics"], 1)

    def test_apicompat_accepts_removed_framework_diagnostic(self):
        output = "PKV006: Target framework net6.0 is no longer supported in the latest version."
        with patch.object(release, "command", return_value=subprocess.CompletedProcess([], 1, output, "")):
            self.assertIn("PKV006", release.compare(Path("apicompat"), Path("current.nupkg"),
                                                        Path("baseline.nupkg"), strict=False))

    def test_additive_api_requires_minor(self):
        outputs = ["APICompat ran successfully without finding any breaking changes.",
                   "CP0002: new member exists on current but not on [Baseline]"]
        with patch.object(release, "stable_tags", return_value=[((1, 2, 3), "v1.2.3")]), \
             patch.object(release, "changed_paths", return_value=["src/libs/Fixture/Api.cs"]), \
             patch.object(release, "published_versions", return_value=[(1, 2, 3)]), \
             patch.object(release, "download_baseline", return_value=Path("baseline.nupkg")), \
             patch.object(release, "compare", side_effect=outputs):
            result = release.plan(self.args)
        self.assertEqual((result["level"], result["version"]), ("minor", "1.3.0"))
        self.assertEqual(result["packages"][0]["api_additions"], 1)

    def test_compatible_fix_requires_patch(self):
        with patch.object(release, "stable_tags", return_value=[((1, 2, 3), "v1.2.3")]), \
             patch.object(release, "changed_paths", return_value=["src/libs/Fixture/Api.cs"]), \
             patch.object(release, "published_versions", return_value=[(1, 2, 3)]), \
             patch.object(release, "download_baseline", return_value=Path("baseline.nupkg")), \
             patch.object(release, "compare", return_value="APICompat ran successfully"):
            result = release.plan(self.args)
        self.assertEqual((result["level"], result["version"]), ("patch", "1.2.4"))

    def test_docs_only_does_not_release(self):
        with patch.object(release, "stable_tags", return_value=[((1, 0, 0), "v1.0.0")]), \
             patch.object(release, "changed_paths", return_value=["README.md"]):
            self.assertEqual(release.plan(self.args)["level"], "none")

    def test_changeset_can_raise_behavioral_break(self):
        changeset = self.root / ".release-impact" / "wire-format.json"
        changeset.parent.mkdir()
        changeset.write_text(json.dumps({"bump": "major", "reason": "Response format changed"}))
        with patch.object(release, "stable_tags", return_value=[((1, 0, 0), "v1.0.0")]), \
             patch.object(release, "changed_paths", return_value=["src/libs/Fixture/Api.cs", ".release-impact/wire-format.json"]), \
             patch.object(release, "published_versions", return_value=[(1, 0, 0)]), \
             patch.object(release, "download_baseline", return_value=Path("baseline.nupkg")), \
             patch.object(release, "compare", return_value="APICompat ran successfully"):
            result = release.plan(self.args)
        self.assertEqual((result["level"], result["version"]), ("major", "2.0.0"))

    def test_pre_one_breaking_policy(self):
        self.assertEqual(release.next_version((0, 3, 2), "major", "minor"), (0, 4, 0))
        self.assertEqual(release.next_version((0, 3, 2), "major", "major"), (1, 0, 0))

    def test_first_stable_package_starts_at_zero_one(self):
        with patch.object(release, "stable_tags", return_value=[]), \
             patch.object(release, "published_versions", return_value=[]):
            result = release.plan(self.args)
        self.assertEqual((result["level"], result["version"]), ("minor", "0.1.0"))

    def test_recovers_published_release_without_tag_at_same_commit(self):
        with patch.object(release, "stable_tags", return_value=[((1, 0, 0), "v1.0.0")]), \
             patch.object(release, "changed_paths", return_value=["src/libs/Fixture/Api.cs"]), \
             patch.object(release, "published_versions", return_value=[(1, 0, 0), (1, 1, 0)]), \
             patch.object(release, "download_baseline", return_value=Path("baseline.nupkg")), \
             patch.object(release, "compare", return_value="APICompat ran successfully"), \
             patch.object(release, "package_commit", return_value="abc123"), \
             patch.object(release, "command", return_value=subprocess.CompletedProcess([], 0, "abc123\n", "")):
            result = release.plan(self.args)
        self.assertEqual((result["level"], result["version"]), ("recover", "1.1.0"))

    def test_untagged_repo_does_not_republish_unchanged_package(self):
        def fake_command(*args, **_kwargs):
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(release, "stable_tags", return_value=[]), \
             patch.object(release, "published_versions", return_value=[(1, 0, 0)]), \
             patch.object(release, "download_baseline", return_value=Path("baseline.nupkg")), \
             patch.object(release, "compare", return_value="APICompat ran successfully"), \
             patch.object(release, "package_commit", return_value="abc123"), \
             patch.object(release, "command", side_effect=fake_command), \
             patch.object(release, "changed_paths", return_value=[".github/workflows/dotnet.yml"]):
            result = release.plan(self.args)
        self.assertEqual(result["level"], "none")

    def test_verify_checks_package_id_and_version(self):
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps({"version": "1.2.4", "packages": [{"id": "Fixture"}]}))
        stable = self.root / "stable"
        stable.mkdir()
        package(stable / "Fixture.1.2.4.nupkg", "Fixture", "1.2.4")
        self.assertEqual(release.verify(Namespace(plan=plan_path, packages=stable, feed=False))["version"], "1.2.4")
        package(stable / "Fixture.1.2.4.nupkg", "Fixture", "1.2.5")
        with self.assertRaises(ValueError):
            release.verify(Namespace(plan=plan_path, packages=stable, feed=False))


if __name__ == "__main__":
    unittest.main()
