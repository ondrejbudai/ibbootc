"""Focused tests for the local payload packaging path."""

import hashlib
import io
import re
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

import ibbootc


def write_definition(directory, files, scripts=None, blueprint_config=None, packages=None):
    image_packages = packages if packages is not None else ["bash", "podman"]
    document = {
        "distros": [{"name": "fedora-44", "defs_path": "fedora-44"}],
        "image_types": {
            "bootc-container": {
                "package_sets": {"os": [{"include": image_packages}]}
            }
        },
        "blueprint_config": blueprint_config or {},
        "extensions": {"files": files, "scripts": scripts or []},
    }
    path = directory / "definition.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


class PayloadTests(unittest.TestCase):
    def test_pin_adds_digest_and_check_detects_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "payload.txt").write_bytes(b"original payload\n")
            definition = directory / "definition.yaml"
            definition.write_text(
                """distros:
  - name: fedora-44
    defs_path: fedora-44
image_types:
  bootc-container:
    package_sets:
      os:
        - include: [bash]
extensions:
  files:
    - source: payload.txt
      destination: /etc/payload.txt
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                ibbootc.pin_missing(definition, ibbootc.read_definition(definition))

            digest = hashlib.sha256(b"original payload\n").hexdigest()
            self.assertIn(f"sha256: {digest}", definition.read_text(encoding="utf-8"))
            ibbootc.validate_files(definition, ibbootc.read_definition(definition))

            (directory / "payload.txt").write_bytes(b"changed payload\n")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                ibbootc.validate_files(definition, ibbootc.read_definition(definition))

    def test_prepare_rechecks_the_bytes_written_to_the_rpm_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "payload.txt").write_bytes(b"pinned bytes")
            definition = write_definition(
                directory,
                [
                    {
                        "source": "payload.txt",
                        "destination": "/etc/payload.txt",
                        "sha256": hashlib.sha256(b"pinned bytes").hexdigest(),
                    }
                ],
            )
            original_validate = ibbootc.validate_files

            def mutate_after_validation(path, document, **kwargs):
                files = original_validate(path, document, **kwargs)
                (directory / "payload.txt").write_bytes(b"changed after validation")
                return files

            with patch.object(ibbootc, "validate_files", side_effect=mutate_after_validation), patch.object(
                ibbootc, "run"
            ) as run_mock:
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    ibbootc.prepare(definition, directory / "work")
            run_mock.assert_not_called()

    def test_duplicate_yaml_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            definition = Path(temporary) / "definition.yaml"
            definition.write_text(
                """distros:
  - name: fedora-44
    defs_path: fedora-44
    defs_path: elsewhere
image_types:
  bootc-container:
    package_sets: {}
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate key"):
                ibbootc.read_definition(definition)

    def test_destination_paths_must_be_canonical_and_not_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "one").write_bytes(b"one")
            (directory / "two").write_bytes(b"two")
            digest = hashlib.sha256(b"one").hexdigest()
            definition = write_definition(
                directory,
                [{"source": "one", "destination": "/etc/../etc/a", "sha256": digest}],
            )
            with self.assertRaisesRegex(ValueError, "unsafe destination"):
                ibbootc.validate_files(definition, ibbootc.read_definition(definition))

            digest_two = hashlib.sha256(b"two").hexdigest()
            definition = write_definition(
                directory,
                [
                    {"source": "one", "destination": "/etc/a", "sha256": digest},
                    {"source": "two", "destination": "/etc/a/child", "sha256": digest_two},
                ],
            )
            with self.assertRaisesRegex(ValueError, "conflicting destinations"):
                ibbootc.validate_files(definition, ibbootc.read_definition(definition))

    def test_payload_tar_release_and_repo_are_stable_across_prepare_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            data = b"immutable data\n"
            (directory / "payload.txt").write_bytes(data)
            definition = write_definition(
                directory,
                [
                    {
                        "source": "payload.txt",
                        "destination": "/etc/payload.txt",
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "mode": "0640",
                    }
                ],
                scripts=["test -f /etc/payload.txt", "printf 'ready\\n' > /etc/ready"],
                blueprint_config={"customizations": {"user": [{"name": "poc"}]}},
            )
            work = directory / "work"
            commands = []

            def fake_run(args, *, root=False):
                commands.append(list(args))
                if args[0] == "rpmbuild":
                    define_index = args.index("--define")
                    topdir = Path(args[define_index + 1].split(" ", 1)[1])
                    spec = Path(args[-1]).read_text(encoding="utf-8")
                    release = re.search(r"^Release: (.+)$", spec, re.MULTILINE).group(1)
                    output = topdir / "RPMS" / "noarch" / f"{ibbootc.PAYLOAD_NAME}-1-{release}.noarch.rpm"
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(f"rpm:{release}".encode())
                elif args[0] == "createrepo_c":
                    repo = Path(args[-1])
                    self.assertNotIn("--update", args)
                    self.assertFalse((repo / "repodata").exists())
                    self.assertEqual(len(list(repo.glob("*.rpm"))), 1)

            with patch.object(ibbootc, "run", side_effect=fake_run), redirect_stdout(io.StringIO()):
                result = ibbootc.prepare(definition, work)
                archive_path = work / "rpmbuild" / "SOURCES" / "payload.tar.gz"
                first_archive = archive_path.read_bytes()
                spec_path = work / "rpmbuild" / "SPECS" / f"{ibbootc.PAYLOAD_NAME}.spec"
                first_spec = spec_path.read_text(encoding="utf-8")
                first_release = re.search(r"^Release: (.+)$", first_spec, re.MULTILINE).group(1)

                stale_rpm = work / "rpmbuild" / "RPMS" / "x86_64" / f"{ibbootc.PAYLOAD_NAME}-0-stale.rpm"
                stale_rpm.parent.mkdir(parents=True, exist_ok=True)
                stale_rpm.write_bytes(b"stale")
                stale_repo_rpm = result[-1] / f"{ibbootc.PAYLOAD_NAME}-0-stale.noarch.rpm"
                stale_repo_rpm.write_bytes(b"stale")
                stale_repodata = result[-1] / "repodata"
                stale_repodata.mkdir()
                (stale_repodata / "repomd.xml").write_text("stale", encoding="utf-8")
                stale_defs = work / "defs" / "stale-distro"
                stale_defs.mkdir()
                (stale_defs / "imagetypes.yaml").write_text("stale", encoding="utf-8")

                second_result = ibbootc.prepare(definition, work)

            self.assertEqual(first_archive, archive_path.read_bytes())
            second_spec = spec_path.read_text(encoding="utf-8")
            second_release = re.search(r"^Release: (.+)$", second_spec, re.MULTILINE).group(1)
            self.assertEqual(first_release, second_release)
            self.assertEqual(len(first_release), 66)
            self.assertEqual(len(list(second_result[-1].glob("*.rpm"))), 1)
            self.assertFalse(stale_rpm.exists())
            self.assertFalse(stale_repodata.exists())
            self.assertFalse(stale_defs.exists())
            self.assertEqual(int.from_bytes(first_archive[4:8], "little"), 0)

            with tarfile.open(archive_path, "r:gz") as archive:
                payload = archive.extractfile("payload/tree/etc/payload.txt")
                self.assertEqual(payload.read(), data)
                self.assertEqual(archive.getmember("payload/tree/etc/payload.txt").mode, 0o640)
                script_member = archive.getmember(f"payload/tree{ibbootc.POSTTRANS_PATH}")
                self.assertEqual(script_member.mtime, 0)
                self.assertIn(b"test -f /etc/payload.txt", archive.extractfile(script_member).read())

            self.assertNotIn("Requires(posttrans):", first_spec)
            self.assertIn(f"%posttrans -p /bin/bash\n{ibbootc.POSTTRANS_PATH}", first_spec)
            self.assertEqual(sum(command[0] == "createrepo_c" for command in commands), 2)

    def test_blueprint_packages_cannot_break_final_payload_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            definition = write_definition(
                directory,
                [],
                blueprint_config={"packages": [{"name": "podman"}]},
            )
            with self.assertRaisesRegex(ValueError, "only final transaction package"):
                ibbootc.prepare(definition, directory / "work")

    def test_payload_cannot_be_in_a_prior_definition_package_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            definition = write_definition(directory, [], packages=[ibbootc.PAYLOAD_NAME])
            with self.assertRaisesRegex(ValueError, "must not be listed"):
                ibbootc.read_definition(definition)

    def test_cli_identifiers_cannot_be_option_like(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            definition = write_definition(directory, [])
            document = yaml.safe_load(definition.read_text(encoding="utf-8"))
            document["image_types"] = {"--force-repo": document["image_types"]["bootc-container"]}
            definition.write_text(yaml.safe_dump(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "safe name"):
                ibbootc.read_definition(definition)

    def test_defs_path_cannot_escape_generated_definitions_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            definition = write_definition(directory, [])
            document = yaml.safe_load(definition.read_text(encoding="utf-8"))
            document["distros"][0]["defs_path"] = ".."
            definition.write_text(yaml.safe_dump(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsafe defs_path"):
                ibbootc.read_definition(definition)


class BootTests(unittest.TestCase):
    def test_boot_uses_kvm_acceleration(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            qcow2_directory = work / "qcow2"
            qcow2_directory.mkdir()
            (qcow2_directory / "test.qcow2").touch()

            with patch.object(ibbootc, "run") as run_mock:
                ibbootc.boot(work)

        command = run_mock.call_args.args[0]
        self.assertEqual(command[command.index("-accel") + 1], "kvm")


if __name__ == "__main__":
    unittest.main()
