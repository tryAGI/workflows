#!/usr/bin/env python3
"""Classify a repository's NuGet release against its published packages.

Only release planning is done here. The caller builds, tests, publishes, and tags.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# PKV006 reports a removed target framework, which breaks consumers just as a
# removed public member does. Other package-validation codes remain failures.
DIAGNOSTIC = re.compile(r"^(?:CP\d{4}|PKV006):", re.MULTILINE)
ADDITION = re.compile(r"^CP000[12]:.*but not on \[Baseline\]", re.MULTILINE)
LEVELS = {"none": 0, "patch": 1, "minor": 2, "major": 3}
FEED = "https://api.nuget.org/v3-flatcontainer"


def version_tuple(value: str) -> tuple[int, int, int] | None:
    match = SEMVER.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def version_text(value: tuple[int, int, int]) -> str:
    return ".".join(map(str, value))


def command(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def package_id(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        nuspecs = [name for name in archive.namelist() if name.endswith(".nuspec")]
        if len(nuspecs) != 1:
            raise ValueError(f"Expected one nuspec in {path}, found {len(nuspecs)}")
        root = ET.fromstring(archive.read(nuspecs[0]))
        package = root.find("./{*}metadata/{*}id")
        if package is None:
            package = root.find("./metadata/id")
        if package is None or not package.text:
            raise ValueError(f"Missing PackageId in {path}")
        return package.text


def package_commit(path: Path) -> str | None:
    with zipfile.ZipFile(path) as archive:
        nuspec = next(name for name in archive.namelist() if name.endswith(".nuspec"))
        root = ET.fromstring(archive.read(nuspec))
        repository = root.find("./{*}metadata/{*}repository")
        if repository is None:
            repository = root.find("./metadata/repository")
        return repository.get("commit") if repository is not None else None


def packages_in(directory: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in sorted(directory.rglob("*.nupkg")):
        name = package_id(path)
        key = name.lower()
        if key in result:
            raise ValueError(f"Duplicate PackageId {name}: {result[key]} and {path}")
        result[key] = path
    if not result:
        raise ValueError(f"No NuGet packages found in {directory}")
    return result


def fetch_json(url: str) -> dict:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 or (error.code != 429 and error.code < 500) or attempt == 3:
                raise
        except urllib.error.URLError:
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def published_versions(package: str) -> list[tuple[int, int, int]]:
    url = f"{FEED}/{urllib.parse.quote(package.lower())}/index.json"
    try:
        values = fetch_json(url)["versions"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise
    return sorted(value for item in values if (value := version_tuple(item)) is not None)


def download_baseline(package: str, version: tuple[int, int, int], cache: Path) -> Path:
    value = version_text(version)
    name = f"{package.lower()}.{value}.nupkg"
    path = cache / name
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        url = f"{FEED}/{urllib.parse.quote(package.lower())}/{value}/{name}"
        partial = path.with_suffix(path.suffix + ".part")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as target:
                    while chunk := response.read(1024 * 1024):
                        target.write(chunk)
                partial.replace(path)
                break
            except (urllib.error.URLError, TimeoutError):
                partial.unlink(missing_ok=True)
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
    return path


def compare(apicompat: Path, current: Path, baseline: Path, strict: bool) -> str:
    args = [str(apicompat), "package", str(current), "--baseline-package", str(baseline),
            "--run-api-compat", "--enable-rule-cannot-change-parameter-name"]
    if strict:
        args.append("--enable-strict-mode-for-baseline-validation")
    result = command(*args)
    output = result.stdout + "\n" + result.stderr
    if result.returncode not in (0, 1) or (result.returncode and not DIAGNOSTIC.search(output)):
        raise RuntimeError(f"ApiCompat failed for {current.name}: {output[-2000:]}")
    return output


def stable_tags(repo: Path) -> list[tuple[tuple[int, int, int], str]]:
    result = command("git", "tag", "--merged", "HEAD", "--list", "v*", cwd=repo)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return sorted((version, tag) for tag in result.stdout.splitlines()
                  if (version := version_tuple(tag.removeprefix("v"))) is not None)


def changed_paths(repo: Path, tag: str | None) -> list[str]:
    if tag is None:
        return []
    result = command("git", "diff", "--name-only", f"{tag}..HEAD", cwd=repo)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.splitlines()


def changeset_level(repo: Path, changed: list[str]) -> tuple[str, list[str]]:
    level = "none"
    reasons: list[str] = []
    for relative in changed:
        if not relative.startswith(".release-impact/") or not relative.endswith(".json"):
            continue
        path = repo / relative
        if not path.is_file():
            continue
        data = json.loads(path.read_text())
        bump, reason = data.get("bump"), data.get("reason")
        if bump not in ("patch", "minor", "major") or not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{relative} requires bump (patch/minor/major) and reason")
        if LEVELS[bump] > LEVELS[level]:
            level = bump
        reasons.append(f"{relative}: {reason.strip()}")
    return level, reasons


def package_affecting(changed: list[str]) -> bool:
    return any((path.startswith("src/") and "/tests/" not in path and "/docs/" not in path)
               or path.endswith((".props", ".targets", ".sln", ".slnx"))
               or path in ("global.json", "NuGet.Config", "nuget.config")
               for path in changed)


def next_version(base: tuple[int, int, int], level: str, zero_major_policy: str) -> tuple[int, int, int]:
    major, minor, patch = base
    if level == "major":
        return (0, minor + 1, 0) if major == 0 and zero_major_policy == "minor" else (major + 1, 0, 0)
    if level == "minor":
        return major, minor + 1, 0
    if level == "patch":
        return major, minor, patch + 1
    raise ValueError(f"Cannot bump {level}")


def plan(args: argparse.Namespace) -> dict:
    repo = args.repo.resolve()
    candidates = packages_in(args.packages)
    tags = stable_tags(repo)
    base_tag = tags[-1][1] if tags else None
    changed = changed_paths(repo, base_tag)
    override, reasons = changeset_level(repo, changed)
    if base_tag and not package_affecting(changed) and override == "none":
        return {"level": "none", "reason": "No package-affecting changes since " + base_tag,
                "base_tag": base_tag, "packages": sorted(candidates)}

    details = []
    level = override if override != "none" else "patch"
    versions = [tags[-1][0]] if tags else []
    baselines: dict[str, tuple[tuple[int, int, int], Path]] = {}
    for key, path in candidates.items():
        published = published_versions(key)
        if not published:
            details.append({"id": package_id(path), "level": "minor", "baseline": None})
            level = "minor" if LEVELS[level] < LEVELS["minor"] else level
            continue
        baseline_version = published[-1]
        versions.append(baseline_version)
        baseline = download_baseline(key, baseline_version, args.cache)
        baselines[key] = baseline_version, baseline
        compatible = compare(args.apicompat, path, baseline, strict=False)
        diagnostics = len(DIAGNOSTIC.findall(compatible))
        if diagnostics:
            impact = "major"
            additions = 0
        else:
            strict = compare(args.apicompat, path, baseline, strict=True)
            additions = len(ADDITION.findall(strict))
            impact = "minor" if additions else "patch"
        details.append({"id": package_id(path), "level": impact,
                        "baseline": version_text(baseline_version),
                        "breaking_diagnostics": diagnostics, "api_additions": additions})
        if LEVELS[impact] > LEVELS[level]:
            level = impact

    if not base_tag and len(baselines) == len(candidates):
        commits = {package_commit(path) for _, path in baselines.values()}
        if len(commits) == 1:
            commit = next(iter(commits))
            if commit and command("git", "merge-base", "--is-ancestor", commit, "HEAD", cwd=repo).returncode == 0:
                changed = changed_paths(repo, commit)
                override, reasons = changeset_level(repo, changed)
                if not package_affecting(changed) and override == "none":
                    return {"level": "none", "reason": "No package-affecting changes since published commit " + commit,
                            "base_tag": None, "packages": sorted(candidates)}
                if LEVELS[override] > LEVELS[level]:
                    level = override

    base = max(versions, default=(0, 0, 0))
    target = (0, 1, 0) if not versions else next_version(base, level, args.zero_major_policy)
    if base_tag and tags[-1][0] < max(versions):
        head = command("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        newest = max(versions)
        if not head or any(package_commit(path) != head for version, path in baselines.values()
                           if version == newest):
            raise RuntimeError(f"Published package version is newer than latest stable tag {base_tag} "
                               "and does not belong to this commit")
        return {"level": "recover", "version": version_text(newest), "tag": "v" + version_text(newest),
                "base_tag": base_tag, "reasons": ["Recover a published package family missing its Git tag"],
                "packages": details}
    return {"level": level, "version": version_text(target), "tag": "v" + version_text(target),
            "base_tag": base_tag, "reasons": reasons, "packages": details}


def verify(args: argparse.Namespace) -> dict:
    plan_data = json.loads(args.plan.read_text())
    expected = {item["id"].lower() for item in plan_data["packages"]}
    actual = packages_in(args.packages)
    if set(actual) != expected:
        raise ValueError(f"Stable package IDs {sorted(actual)} differ from candidate IDs {sorted(expected)}")
    version = version_tuple(plan_data["version"])
    if version is None:
        raise ValueError("Invalid planned version")
    for key, path in actual.items():
        with zipfile.ZipFile(path) as archive:
            nuspec = next(name for name in archive.namelist() if name.endswith(".nuspec"))
            root = ET.fromstring(archive.read(nuspec))
            element = root.find("./{*}metadata/{*}version")
            if element is None:
                element = root.find("./metadata/version")
            if element is None or element.text != plan_data["version"]:
                raise ValueError(f"Wrong version in {path}")
    if args.feed:
        for key in actual:
            for attempt in range(24):
                if version in published_versions(key):
                    break
                if attempt == 23:
                    raise TimeoutError(f"{key} {plan_data['version']} absent from NuGet after 12 minutes")
                time.sleep(30)
    return {"verified": sorted(actual), "version": plan_data["version"], "feed": args.feed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    planning = sub.add_parser("plan")
    planning.add_argument("--repo", type=Path, default=Path.cwd())
    planning.add_argument("--packages", type=Path, required=True)
    planning.add_argument("--apicompat", type=Path, required=True)
    planning.add_argument("--cache", type=Path, required=True)
    planning.add_argument("--zero-major-policy", choices=("minor", "major"), default="minor")
    planning.add_argument("--output", type=Path, required=True)
    checking = sub.add_parser("verify")
    checking.add_argument("--plan", type=Path, required=True)
    checking.add_argument("--packages", type=Path, required=True)
    checking.add_argument("--feed", action="store_true")
    options = parser.parse_args()
    try:
        result = plan(options) if options.command == "plan" else verify(options)
        if options.command == "plan":
            options.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"release-impact: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
