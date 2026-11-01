"""Kills the Rust installer at random publications, in random batch sizes, during
an update, a first installation, a rollback and an uninstallation, and checks that
what is left is always consistent: every file is the old or the new one, and a
rollback, a repeated installation or a repeated uninstallation always ends in the
tree it should. Needs a debug build of tomaccoctl (the kill hook is not in release
builds).

Run: TOMACCOCTL=target/debug/tomaccoctl PYTHONPATH=tools:tests/python:tests:. \
     python3 -m unittest tests.python.test_install_fuzz
More runs or another seed: TOMACCO_FUZZ_RUNS=300 TOMACCO_FUZZ_SEED=7 (default 6 runs
of each scenario, about three minutes)."""
import json
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path

import test_install as base
fake_package, run_native = base.fake_package, base.run_native
RUNTIME = base.PortablePackageTests.RUNTIME
STAGED = ".tomacco-staged-"
RUNS = int(os.environ.get("TOMACCO_FUZZ_RUNS", "6"))
SEED = int(os.environ.get("TOMACCO_FUZZ_SEED", "20260501"))


def snapshot(home, keep_backups=False):
    """Every path under the home with what identifies it: file content and mode,
    link target, or directory."""
    tree = {}
    for path in sorted(home.rglob("*")):
        relative = path.relative_to(home)
        if not keep_backups and "install-backups" in relative.parts:
            continue
        if path.name.startswith(STAGED):
            continue
        if path.is_symlink():
            tree[str(relative)] = ("link", os.readlink(path))
        elif path.is_dir():
            tree[str(relative)] = ("dir",)
        else:
            tree[str(relative)] = ("file", path.read_bytes(), path.stat().st_mode & 0o777)
    return tree


def staged(home):
    return sorted(str(p.relative_to(home)) for p in home.rglob(STAGED + "*"))


def difference(left, right):
    """Files and links that differ or exist on one side only. A directory on one side
    only is left out: an uninstallation may leave the empty directories it needed."""
    return {k: (left.get(k, ("missing",))[:1], right.get(k, ("missing",))[:1]) for k in set(left) | set(right)
            if left.get(k) != right.get(k) and ("dir",) not in (left.get(k), right.get(k))}


class InstallerKillFuzz(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        program = base.native_program()
        if not program or "/debug/" not in program:
            raise unittest.SkipTest("TOMACCOCTL must be a debug tomaccoctl that installs (it has the kill hooks)")
        base.NATIVE["program"] = program
        cls.directory = tempfile.TemporaryDirectory(prefix="ik-")
        root = cls.root = Path(cls.directory.name).resolve()
        cls.v1 = fake_package(root / "a", RUNTIME, "1.0.0")
        cls.v2 = fake_package(root / "b", {n: (c + b" 2", x) for n, (c, x) in RUNTIME.items()}, "1.0.1")
        for path in cls.v2.rglob("*"):
            # Most files differ in 1.0.1, so an update publishes many of them.
            if path.is_file() and path.suffix != ".py" and path.name != "VERSION" and "/skills/" not in str(path):
                path.write_bytes(path.read_bytes() + b"\n1.0.1\n")
        # A home with things that are the user's own, which must always survive.
        cls.empty = root / "empty"
        for relative, text in [(".bashrc", "export A=1\n"), (".local/bin/other", "#!/bin/sh\n"),
                               (".claude/settings.json", "{}\n"), (".config/keep/me", "keep\n")]:
            path = cls.empty / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        cls.base_before = snapshot(cls.empty)
        # The files embed the home's path, so every run happens in the same one.
        cls.home = root / "home"
        cls.pristine = {}
        cls.pristine["empty"] = cls.empty
        cls.reset("empty")
        run_native(cls.home, cls.v1, "--no-window-rule")
        cls.old = snapshot(cls.home)
        cls.pristine["installed"] = root / "installed"
        shutil.copytree(cls.home, cls.pristine["installed"], symlinks=True)
        run_native(cls.home, cls.v2, "--no-window-rule")
        cls.new = snapshot(cls.home)
        assert cls.old != cls.new
        cls.steps_cache = {}

    @classmethod
    def tearDownClass(cls):
        base.NATIVE["program"] = None
        cls.directory.cleanup()

    @classmethod
    def reset(cls, name):
        """The home as one of the pristine copies has it."""
        shutil.rmtree(cls.home, ignore_errors=True)
        shutil.copytree(cls.pristine[name], cls.home, symlinks=True)
        return cls.home

    @classmethod
    def steps(cls, name, package, batch):
        """The number of publications of one run with this batch size, found by
        killing at ever later ones."""
        if (name, batch) in cls.steps_cache:
            return cls.steps_cache[name, batch]
        low, high = 1, 4096
        while low < high:
            middle = (low + high) // 2
            home = cls.reset(name)
            result = run_native(home, package, "--no-window-rule", expect=None,
                                environment={"TOMACCO_INSTALL_TEST_BATCH": str(batch),
                                             "TOMACCO_INSTALL_TEST_KILL_AT": str(middle)})
            if result.returncode == 9:
                low = middle + 1
            else:
                high = middle
        cls.steps_cache[name, batch] = low - 1
        return low - 1

    def killed(self, name, package, rng, *arguments):
        """Runs the installer in a fresh home until it is killed at a random
        publication; returns (home, batch, kill point, whether it was killed). A
        point past the last publication lets the run finish."""
        batch = rng.randint(1, 8)
        point = rng.randint(1, self.steps(name, package, batch) + 2)
        home = self.reset(name)
        result = run_native(home, package, *arguments, expect=None,
                            environment={"TOMACCO_INSTALL_TEST_BATCH": str(batch),
                                         "TOMACCO_INSTALL_TEST_KILL_AT": str(point)})
        self.assertIn(result.returncode, (0, 9), result.stderr)
        return home, batch, point, result.returncode == 9

    def latest_backup(self, home):
        backups = home / ".local/share/tomacco/install-backups"
        return sorted(backups.iterdir())[-1].name

    def assertTree(self, home, expected, context, strict_staged=True):
        actual = snapshot(home)
        self.assertEqual(difference(actual, expected), {}, context)
        if strict_staged:
            self.assertEqual(staged(home), [], context)

    def assertOnlyOldOrNew(self, home, old, new, context):
        for relative, state in snapshot(home).items():
            if relative in old and relative in new and state in (old[relative], new[relative]):
                continue
            self.assertIn(state, (old.get(relative), new.get(relative)), f"{relative}: neither old nor new ({context})")
            # A directory or file only one of them has is the other's leftover in transit.

    def test_an_update_killed_anywhere_leaves_old_or_new_files_and_always_recovers(self):
        rng = random.Random(SEED)
        for run in range(RUNS):
            home, batch, point, died = self.killed("installed", self.v2, rng, "--no-window-rule")
            context = f"update run {run} seed {SEED} batch {batch} kill {point}"
            if not died:
                self.assertTree(home, self.new, context)
                continue
            self.assertOnlyOldOrNew(home, self.old, self.new, context)
            choice = rng.choice(["rollback", "again", "rollback-killed", "uninstall"])
            context += f" then {choice}"
            if choice in ("rollback", "rollback-killed"):
                name = self.latest_backup(home)
                if choice == "rollback-killed":
                    again = run_native(home, None, "--rollback", name, expect=None,
                                       environment={"TOMACCO_INSTALL_TEST_BATCH": str(rng.randint(1, 4)),
                                                    "TOMACCO_INSTALL_TEST_KILL_AT": str(rng.randint(1, 6))})
                    self.assertIn(again.returncode, (0, 9), again.stderr)
                    context += f" (rollback exit {again.returncode})"
                run_native(home, None, "--rollback", name)
                self.assertTree(home, self.old, context, strict_staged=False)
                # Whatever a killed run staged is swept by the next installation.
                run_native(home, self.v2, "--no-window-rule")
                self.assertTree(home, self.new, context + " then installed")
            elif choice == "again":
                run_native(home, self.v2, "--no-window-rule")
                self.assertTree(home, self.new, context)
            else:
                run_native(home, None, "--uninstall")
                self.assertTree(home, self.base_before, context)
                self.assertFalse((home / ".local/lib/tomacco").exists(), context)

    def test_a_first_installation_killed_anywhere_rolls_back_to_the_users_own_files(self):
        rng = random.Random(SEED + 1)
        for run in range(RUNS):
            home, batch, point, died = self.killed("empty", self.v1, rng, "--no-window-rule")
            context = f"fresh run {run} seed {SEED + 1} batch {batch} kill {point}"
            if not died:
                self.assertTree(home, self.old, context)
                continue
            choice = rng.choice(["rollback", "again", "uninstall"])
            context += f" then {choice}"
            if choice == "rollback" and (home / ".local/share/tomacco/install-backups").is_dir():
                run_native(home, None, "--rollback", self.latest_backup(home))
                actual = snapshot(home)
                leftovers = {k: v for k, v in difference(actual, self.base_before).items() if "tomacco" not in k}
                self.assertEqual(leftovers, {}, context)
                # Nothing of Tomacco's own remains but empty journal directories and the like.
                files = [k for k, v in actual.items() if v[0] == "file" and k not in self.base_before]
                self.assertEqual(files, [], context)
            elif choice == "again":
                run_native(home, self.v1, "--no-window-rule")
                self.assertTree(home, self.old, context)
            else:
                run_native(home, None, "--uninstall")
                files = [k for k, v in snapshot(home).items() if v[0] == "file" and k not in self.base_before]
                self.assertEqual(files, [], context)
                self.assertEqual({k for k in self.base_before if k not in snapshot(home)}, set(), context)

    def test_an_uninstallation_killed_anywhere_finishes_when_run_again(self):
        rng = random.Random(SEED + 2)
        for run in range(RUNS):
            home = self.reset("installed")
            batch, point = rng.randint(1, 8), rng.randint(1, 80)
            result = run_native(home, None, "--uninstall", expect=None,
                                environment={"TOMACCO_INSTALL_TEST_BATCH": str(batch),
                                             "TOMACCO_INSTALL_TEST_KILL_AT": str(point)})
            context = f"uninstall run {run} seed {SEED + 2} batch {batch} kill {point} exit {result.returncode}"
            self.assertIn(result.returncode, (0, 9), result.stderr)
            if result.returncode == 9:
                # The program that was killed may be gone with its own files: use the package's.
                run_native(home, self.v1, "--uninstall")
            actual = snapshot(home)
            self.assertEqual(difference(actual, self.base_before), {}, context)
            self.assertEqual(staged(home), [], context)

    def test_the_kill_points_cover_every_publication_of_an_update(self):
        # Guards the harness: an update has many publications, so random points reach all phases.
        self.assertGreater(self.steps("installed", self.v2, 1), 20)
        self.assertGreater(self.steps("empty", self.v1, 1), 20)


if __name__ == "__main__":
    unittest.main()
