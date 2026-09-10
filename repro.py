#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Reproduce a pack keep-file race using synthetic repositories only."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


class Reproducer:
	def __init__(self, root: Path, git: str, expectation: str):
		self.root = root
		self.git = str(Path(git).resolve())
		self.expectation = expectation
		self.env = {key: value for key, value in os.environ.items()
					if not key.upper().startswith("GIT_")}
		self.env.update({
			"GIT_CONFIG_NOSYSTEM": "1",
			"GIT_CONFIG_GLOBAL": os.devnull,
			"GIT_ATTR_NOSYSTEM": "1",
			"GIT_TERMINAL_PROMPT": "0",
			"GIT_NO_LAZY_FETCH": "1",
			"GIT_AUTHOR_NAME": "Reproduction",
			"GIT_AUTHOR_EMAIL": "repro@example.invalid",
			"GIT_COMMITTER_NAME": "Reproduction",
			"GIT_COMMITTER_EMAIL": "repro@example.invalid",
			"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
			"GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
		})
		self.version = self.run(None, ["--version"]).stdout.decode().strip()
		self.exec_path = self.run(None, ["--exec-path"]).stdout.decode().strip()
		self.shim = root / "shim"
		self.shim.mkdir()
		shutil.copyfile(Path(__file__).with_name("race-git.sh"), self.shim / "git")
		(self.shim / "git").chmod(0o755)

	def run(self, repo: Path | None, args: list[str], data: bytes | None = None,
			check: bool = True, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
		command = [self.git]
		if repo is not None:
			command.append("--git-dir=" + str(repo))
		result = subprocess.run(
			command + args, input=data, capture_output=True, cwd=self.root,
			env={**self.env, **(extra_env or {})}, timeout=60,
		)
		if check and result.returncode != 0:
			raise RuntimeError(
				f"Command failed ({result.returncode}): {command + args}\n"
				+ result.stdout.decode(errors="replace")
				+ result.stderr.decode(errors="replace")
			)
		return result

	def create_source(self) -> tuple[Path, dict[str, str], str]:
		source = self.root / "source.git"
		self.run(None, ["init", "--bare", "--initial-branch=main", str(source)])
		objects = {}
		for name, content in [("parent", b"first content\n"), ("tip", b"second content\n")]:
			blob = self.run(source, ["hash-object", "-w", "--stdin"], content).stdout.strip()
			tree = self.run(source, ["mktree"], b"100644 blob " + blob + b"\tfile\n").stdout.strip()
			args = ["commit-tree", tree.decode()]
			if name == "tip":
				args.extend(["-p", objects["parent_commit"]])
			commit = self.run(source, args, (name + "\n").encode()).stdout.decode().strip()
			objects.update({
				name + "_blob": blob.decode(),
				name + "_tree": tree.decode(),
				name + "_commit": commit,
			})
		self.run(source, ["update-ref", "refs/heads/main", objects["tip_commit"]])
		self.run(source, ["update-ref", "refs/tags/parent", objects["parent_commit"]])
		prefix = str(source / "objects" / "pack" / "pack")
		pack = self.run(source, ["pack-objects", "--revs", prefix],
						(objects["parent_commit"] + "\n").encode()).stdout.decode().strip()
		self.run(source, ["pack-objects", "--revs", prefix],
				 (objects["tip_commit"] + "\n^" + objects["parent_commit"] + "\n").encode())
		self.run(source, ["prune-packed"])
		self.run(source, ["fsck", "--full"])
		return source, objects, pack

	def test_case(self, source: Path, objects: dict[str, str], pack: str,
				  iteration: int, variant: str, mode: str) -> dict:
		name = f"{iteration:02d}-{variant}-{mode}"
		case = self.root / name
		case.mkdir()
		repo = case / "repo.git"
		shutil.copytree(source, repo)
		old_pack = repo / "objects" / "pack" / ("pack-" + pack + ".pack")
		before = self.run(repo, ["fsck", "--full"])
		prefix = []
		if mode != "control":
			prefix.append("--exec-path=" + str(self.shim))
		if mode == "workaround":
			prefix.extend(["-c", "repack.packKeptObjects=true"])
		if variant == "follow":
			prefix.extend(["-c", "repack.midxMustContainCruft=false"])
		command = prefix + ["repack", "-d", "-l", "--geometric=2"]
		if variant != "geometric":
			command.append("--write-midx")
		extra_env = {
			"REAL_GIT": Path(self.git).as_posix(),
			"REAL_EXEC_PATH": Path(self.exec_path).as_posix(),
			"RACE_REPO": repo.as_posix(),
			"RACE_PACK": (source / "objects" / "pack" / old_pack.name).as_posix(),
			"RACE_KEEP": old_pack.with_suffix(".keep").as_posix(),
			"RACE_EVENTS": (case / "events.log").as_posix(),
			"RACE_RECEPTION_LOG": (case / "index-pack.log").as_posix(),
		}
		repack = self.run(repo, command, check=False, extra_env=extra_env)
		after = self.run(repo, ["fsck", "--full"], check=False)
		presence = {
			name: self.run(repo, ["-c", "core.multiPackIndex=false", "cat-file", "-e", oid],
						   check=False).returncode == 0
			for name, oid in objects.items()
		}
		missing = [name for name, present in presence.items() if not present]
		events_path = case / "events.log"
		events = events_path.read_text() if events_path.exists() else ""
		injected = "duplicate index-pack completed; keep file exists" in events
		expected_loss = mode == "race" and self.expectation == "vulnerable"
		errors = []
		if repack.returncode != 0:
			errors.append(f"Unexpected repack exit status: {repack.returncode}")
		if injected != (mode != "control"):
			errors.append("The timing injection did not run exactly where expected")
		if variant == "follow" and mode != "control" and "--stdin-packs=follow" not in events:
			errors.append("Git did not use the requested follow-mode path")
		if expected_loss:
			if after.returncode == 0 or not {"parent_commit", "parent_tree"}.issubset(missing):
				errors.append("Expected actual loss of the parent commit and tree")
			if not all(presence[key] for key in ["tip_commit", "tip_tree", "tip_blob"]):
				errors.append("Unexpected loss of tip objects")
			if old_pack.exists():
				errors.append("The selected original pack was not deleted")
		elif after.returncode != 0 or missing:
			errors.append("Expected a healthy repository with every input object preserved")
		for filename, data in [
			("repack.stdout.log", repack.stdout),
			("repack.stderr.log", repack.stderr),
			("fsck.stdout.log", after.stdout),
			("fsck.stderr.log", after.stderr),
		]:
			(case / filename).write_bytes(data)
		return {
			"case": name,
			"command": ["git"] + command,
			"fsck_before": before.returncode,
			"repack_exit": repack.returncode,
			"fsck_after": after.returncode,
			"missing_objects": missing,
			"original_pack_deleted": not old_pack.exists(),
			"timing_injected": injected,
			"expectation_met": not errors,
			"errors": errors,
			"repack_stderr": repack.stderr.decode(errors="replace"),
			"fsck_output": (after.stdout + after.stderr).decode(errors="replace"),
			"subprocess_events": events,
		}

	def execute(self, repeat: int) -> int:
		source, objects, pack = self.create_source()
		results = []
		for iteration in range(1, repeat + 1):
			for variant in ["geometric", "midx", "follow"]:
				for mode in ["control", "race", "workaround"]:
					result = self.test_case(source, objects, pack, iteration, variant, mode)
					results.append(result)
					print(
						f"{result['case']}: fsck {result['fsck_before']} -> {result['fsck_after']}; "
						f"repack exit {result['repack_exit']}; missing {result['missing_objects']}; "
						f"{'PASS' if result['expectation_met'] else 'FAIL'}",
						flush=True,
					)
		report = {
			"git_version": self.version,
			"expectation": self.expectation,
			"repetitions": repeat,
			"synthetic_objects": objects,
			"results": results,
		}
		(self.root / "results.json").write_text(json.dumps(report, indent=2) + "\n")
		summary = [
			"# Git repack keep-file race",
			"",
			f"Git: `{self.version}`. Expected implementation: **{self.expectation}**.",
			"",
			"PASS means the observed outcome matches the expectation, not that vulnerable Git is safe.",
			"",
			"| Case | fsck before | Repack exit | fsck after | Missing objects | Expected outcome |",
			"| --- | --- | --- | --- | --- | --- |",
		]
		for result in results:
			summary.append(
				f"| {result['case']} | {result['fsck_before']} | {result['repack_exit']} | "
				f"{result['fsck_after']} | {', '.join(result['missing_objects']) or 'none'} | "
				f"{'PASS' if result['expectation_met'] else 'FAIL'} |"
			)
		summary_text = "\n".join(summary) + "\n"
		(self.root / "summary.md").write_text(summary_text)
		if os.environ.get("GITHUB_STEP_SUMMARY"):
			with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
				output.write(summary_text)
		return 0 if all(result["expectation_met"] for result in results) else 1


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--output", type=Path, default=Path("artifacts"),
						help="New directory for disposable repositories and results; must not exist")
	parser.add_argument("--git", default="git", help="Git executable to test")
	parser.add_argument("--repeat", type=int, default=3)
	parser.add_argument("--expect", choices=["vulnerable", "fixed"], default="vulnerable")
	args = parser.parse_args()
	if args.repeat < 1:
		parser.error("--repeat must be positive")
	git = shutil.which(args.git)
	if git is None:
		parser.error(f"Git executable not found: {args.git}")
	root = args.output.resolve()
	if root.exists():
		parser.error(f"Output directory already exists; choose a new path: {root}")
	root.mkdir(parents=True)
	return Reproducer(root, git, args.expect).execute(args.repeat)


if __name__ == "__main__":
	sys.exit(main())
