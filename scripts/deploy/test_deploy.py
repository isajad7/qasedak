"""Exercise deployment artifact boundaries without contacting a server."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tarfile
import unittest

from write_compose_image import write_image_tag
from image_config_id import image_config_id


ROOT = Path(__file__).resolve().parents[2]


class DeploymentArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qasedak-deploy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.artifact = self.directory / "qasedak-image"
        self.artifact.mkdir()
        self.image = self.artifact / "image.tar.gz"
        self.image.write_bytes(b"runner-built-image-fixture")
        self.checksum = hashlib.sha256(self.image.read_bytes()).hexdigest()
        (self.artifact / "image.tar.gz.sha256").write_text(f"{self.checksum}  image.tar.gz\n")
        self.image_id = "sha256:" + "1" * 64
        (self.artifact / "image.id").write_text(self.image_id + "\n")
        self.revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        self.calls = self.directory / "calls"
        self.remote_script = self.directory / "remote-script"
        binaries = self.directory / "bin"
        binaries.mkdir()
        for command, body in {
            "ssh": '''#!/usr/bin/env bash
printf 'ssh %s\\n' "$*" >> "$DEPLOY_TEST_CALLS"
case "${!#}" in
    'mktemp -d '*) printf '/tmp/qasedak-dev.ABCDEFGH\\n' ;;
    'bash -s -- '*) cat > "$DEPLOY_TEST_REMOTE_SCRIPT" ;;
esac
''',
            "scp": '''#!/usr/bin/env bash
printf 'scp %s\\n' "$*" >> "$DEPLOY_TEST_CALLS"
''',
        }.items():
            path = binaries / command
            path.write_text(body)
            path.chmod(0o700)
        self.environment = {
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(self.directory),
            "GITHUB_SHA": self.revision,
            "DEV_SSH_HOST": "deploy.example.invalid",
            "DEV_SSH_USER": "deploy",
            "DEV_SSH_PRIVATE_KEY": "test-private-key",
            "DEV_SSH_KNOWN_HOSTS": "test-known-hosts",
            "DEPLOY_TEST_CALLS": str(self.calls),
            "DEPLOY_TEST_REMOTE_SCRIPT": str(self.remote_script),
        }

    def sender(self):
        return subprocess.run(
            ["bash", str(ROOT / "scripts/deploy/github_dev_deploy.sh")],
            cwd=ROOT, env=self.environment, capture_output=True, text=True,
        )

    def test_corrupt_image_never_contacts_ssh(self):
        self.image.write_bytes(b"corrupted-in-transit")
        result = self.sender()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Image artifact checksum mismatch", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_missing_image_never_contacts_ssh(self):
        self.image.unlink()
        result = self.sender()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("image artifact is missing", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_sender_transfers_both_artifacts_and_binds_image_identity(self):
        result = self.sender()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text()
        self.assertIn(f"{self.directory}/qasedak-deploy-ssh/release.tar.gz", calls)
        self.assertIn(str(self.image), calls)
        self.assertIn(f"'{self.checksum}' '{self.image_id}'", calls)
        self.assertIn("StrictHostKeyChecking=yes", calls)
        self.assertEqual(self.remote_script.read_bytes(), (ROOT / "scripts/deploy/compose_dev_deploy.sh").read_bytes())
        self.assertFalse((self.directory / "qasedak-deploy-ssh").exists())

    def test_server_rejects_corrupt_image_before_loading_config_or_running_docker(self):
        stage = subprocess.check_output(["mktemp", "-d", "/tmp/qasedak-dev.XXXXXXXX"], text=True).strip()
        self.addCleanup(shutil.rmtree, stage)
        release = Path(stage) / "release.tar.gz"
        release.write_bytes(b"source-fixture")
        (Path(stage) / "image.tar.gz").write_bytes(b"corrupted-in-transit")
        checksum = hashlib.sha256(release.read_bytes()).hexdigest()
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/deploy/compose_dev_deploy.sh"), self.revision,
             checksum, str(release), self.checksum, self.image_id],
            env=self.environment, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Image checksum mismatch", result.stderr)

    def test_verified_image_preserves_compose_profiles_and_file_permissions(self):
        environment = self.directory / ".env"
        environment.write_text("# Runtime settings\nCOMPOSE_PROFILES=consumers\nQASEDAK_IMAGE_TAG=old\nOTHER=value\n")
        environment.chmod(0o640)
        image = "qasedak-development:" + self.revision
        write_image_tag(environment, image)
        write_image_tag(environment, image)
        self.assertEqual(environment.read_text(),
                         "# Runtime settings\nCOMPOSE_PROFILES=consumers\nOTHER=value\nQASEDAK_IMAGE_TAG=" + image + "\n")
        self.assertEqual(environment.stat().st_mode & 0o777, 0o640)

    def test_image_setting_refuses_symlink_or_invalid_revision(self):
        environment = self.directory / ".env"
        environment.symlink_to(self.artifact / "image.id")
        with self.assertRaises(ValueError):
            write_image_tag(environment, "qasedak-development:" + self.revision)
        self.assertEqual((self.artifact / "image.id").read_text(), self.image_id + "\n")
        environment.unlink()
        with self.assertRaises(ValueError):
            write_image_tag(environment, "qasedak-development:latest")
        self.assertFalse(environment.exists())


class PortableImageIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qasedak-image-id-test-")
        self.addCleanup(temporary.cleanup)
        self.archive = Path(temporary.name) / "image.tar.gz"
        self.tag = "qasedak-development:" + "1" * 40

    def export(self, *, architecture="amd64", tag=None):
        config = json.dumps({"os": "linux", "architecture": architecture, "rootfs": {"diff_ids": []}}).encode()
        config_path = "blobs/sha256/" + hashlib.sha256(config).hexdigest()
        manifest = json.dumps([{"RepoTags": [tag or self.tag], "Config": config_path}]).encode()
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, data in [("manifest.json", manifest), (config_path, config), ("index.json", b'{"manifests":[]}')]:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return "sha256:" + hashlib.sha256(config).hexdigest()

    def test_uses_exported_configuration_identity_instead_of_manifest_or_index(self):
        expected = self.export()
        self.assertEqual(image_config_id(self.archive, self.tag), expected)

    def test_rejects_wrong_release_tag(self):
        self.export(tag="qasedak-development:other")
        with self.assertRaises(ValueError):
            image_config_id(self.archive, self.tag)

    def test_rejects_wrong_target_platform(self):
        self.export(architecture="arm64")
        with self.assertRaises(ValueError):
            image_config_id(self.archive, self.tag)


if __name__ == "__main__":
    unittest.main()
