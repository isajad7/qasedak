import ipaddress
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DOCKER_BRIDGE_HOST = "172.17.0.1"
DOCKER_BRIDGE_CIDR = "172.17.0.0/16"
POSTGRES_PORT = "5432"
POSTGRES_LISTEN_ADDRESSES = "127.0.0.1,172.17.0.1"
POSTGRES_HBA_BRIDGE_RULE = f"host all all {DOCKER_BRIDGE_CIDR} scram-sha-256"


@dataclass(frozen=True)
class BridgeCheck:
    name: str
    ok: bool
    message: str
    remediation: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostgresBridgeFiles:
    config_file: Path
    hba_file: Path


@dataclass(frozen=True)
class PostgresBridgeConfigureResult:
    files: PostgresBridgeFiles
    config_backup: Path | None
    hba_backup: Path | None
    config_changed: bool
    hba_changed: bool
    restarted: bool
    checks: tuple[BridgeCheck, ...] = ()


class PostgresBridgeConfigurationError(RuntimeError):
    pass


class CommandRunner:
    def run(self, argv, *, timeout=30):
        return subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)


def ss_listens_on_bridge(output, *, host=DOCKER_BRIDGE_HOST, port=POSTGRES_PORT):
    endpoint_patterns = (
        f"{host}:{port}",
        f"[{host}]:{port}",
    )
    for line in str(output or "").splitlines():
        if any(pattern in line for pattern in endpoint_patterns):
            return True
    return False


def pg_hba_allows_bridge(text, *, cidr=DOCKER_BRIDGE_CIDR):
    wanted_network = ipaddress.ip_network(cidr, strict=False)
    for raw_line in str(text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = re.split(r"\s+", line)
        if len(fields) < 5 or fields[0] != "host":
            continue
        if fields[4] != "scram-sha-256":
            continue
        try:
            network = ipaddress.ip_network(fields[3], strict=False)
        except ValueError:
            continue
        database, user = fields[1], fields[2]
        if database == "all" and user == "all" and network == wanted_network:
            return True
    return False


def remediation_commands():
    return (
        "sudo -u postgres psql -Atqc \"SHOW config_file; SHOW hba_file;\" postgres",
        "sudo cp <postgresql.conf> <postgresql.conf>.bak.$(date +%Y%m%d%H%M%S)",
        "sudo cp <pg_hba.conf> <pg_hba.conf>.bak.$(date +%Y%m%d%H%M%S)",
        "set listen_addresses = '127.0.0.1,172.17.0.1' in postgresql.conf",
        "append: host all all 172.17.0.0/16 scram-sha-256",
        "sudo systemctl restart postgresql",
        "sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail",
    )


def postgres_discovery_command():
    return ["sudo", "-u", "postgres", "psql", "-Atqc", "SHOW config_file; SHOW hba_file;", "postgres"]


def _first_line(text):
    return (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""


def discover_postgres_bridge_files(runner=None):
    runner = runner or CommandRunner()
    command = postgres_discovery_command()
    result = runner.run(command, timeout=15)
    if result.returncode != 0:
        raise PostgresBridgeConfigurationError(
            "Could not query PostgreSQL config paths with sudo -u postgres psql."
        )
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if len(lines) < 2:
        raise PostgresBridgeConfigurationError("PostgreSQL did not return both config_file and hba_file paths.")
    config_file = Path(lines[0])
    hba_file = Path(lines[1])
    if not config_file.is_file():
        raise PostgresBridgeConfigurationError("Could not locate postgresql.conf for Docker bridge configuration.")
    if not hba_file.is_file():
        raise PostgresBridgeConfigurationError("Could not locate pg_hba.conf for Docker bridge configuration.")
    return PostgresBridgeFiles(config_file=config_file, hba_file=hba_file)


def discover_hba_file(runner):
    result = runner.run(["runuser", "-u", "postgres", "--", "psql", "-Atqc", "SHOW hba_file;", "postgres"], timeout=15)
    if result.returncode == 0:
        return _first_line(result.stdout)
    result = runner.run(["sudo", "-u", "postgres", "psql", "-Atqc", "SHOW hba_file;", "postgres"], timeout=15)
    if result.returncode == 0:
        return _first_line(result.stdout)
    return ""


def postgresql_conf_with_bridge_listener(text, *, listen_addresses=POSTGRES_LISTEN_ADDRESSES):
    replacement = f"listen_addresses = '{listen_addresses}'"
    lines = str(text or "").splitlines()
    changed = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*#?\s*listen_addresses\s*=", line):
            if line != replacement:
                lines[index] = replacement
                changed = True
            return "\n".join(lines) + "\n", changed
    lines.append(replacement)
    return "\n".join(lines) + "\n", True


def pg_hba_with_bridge_rule(text, *, rule=POSTGRES_HBA_BRIDGE_RULE):
    existing = str(text or "")
    if pg_hba_allows_bridge(existing):
        return existing, False
    suffix = "" if existing.endswith("\n") or not existing else "\n"
    return existing + suffix + rule + "\n", True


def configure_postgres_bridge(
    *,
    runner=None,
    apply=False,
    restart=False,
    check_after_restart=False,
    docker_image="postgres:16-alpine",
    stamp=None,
):
    runner = runner or CommandRunner()
    files = discover_postgres_bridge_files(runner)

    config_text = files.config_file.read_text(encoding="utf-8")
    hba_text = files.hba_file.read_text(encoding="utf-8")
    next_config_text, config_changed = postgresql_conf_with_bridge_listener(config_text)
    next_hba_text, hba_changed = pg_hba_with_bridge_rule(hba_text)

    config_backup = None
    hba_backup = None
    checks = ()
    restarted = False
    if apply:
        suffix = stamp or datetime.now().strftime("%Y%m%d%H%M%S")
        config_backup = files.config_file.with_name(f"{files.config_file.name}.bak.{suffix}")
        hba_backup = files.hba_file.with_name(f"{files.hba_file.name}.bak.{suffix}")
        shutil.copy2(files.config_file, config_backup)
        shutil.copy2(files.hba_file, hba_backup)
        if config_changed:
            files.config_file.write_text(next_config_text, encoding="utf-8")
        if hba_changed:
            files.hba_file.write_text(next_hba_text, encoding="utf-8")
        if restart:
            restart_result = runner.run(["sudo", "systemctl", "restart", "postgresql"], timeout=60)
            if restart_result.returncode != 0:
                raise PostgresBridgeConfigurationError("PostgreSQL restart failed after bridge configuration.")
            restarted = True
            if check_after_restart:
                checks = tuple(run_checks(runner=runner, hba_file=str(files.hba_file), docker_image=docker_image))

    return PostgresBridgeConfigureResult(
        files=files,
        config_backup=config_backup,
        hba_backup=hba_backup,
        config_changed=config_changed,
        hba_changed=hba_changed,
        restarted=restarted,
        checks=checks,
    )


def run_checks(*, runner=None, hba_file=None, docker_image="postgres:16-alpine"):
    runner = runner or CommandRunner()
    checks = []
    remediation = remediation_commands()

    ss_result = runner.run(["ss", "-ltnp"], timeout=15)
    listens = ss_result.returncode == 0 and ss_listens_on_bridge(ss_result.stdout)
    checks.append(
        BridgeCheck(
            "postgres-listen-bridge",
            listens,
            (
                f"PostgreSQL listens on {DOCKER_BRIDGE_HOST}:{POSTGRES_PORT}"
                if listens
                else f"PostgreSQL is not listening on Docker bridge {DOCKER_BRIDGE_HOST}:{POSTGRES_PORT}"
            ),
            () if listens else remediation,
        )
    )

    hba_path = hba_file or discover_hba_file(runner)
    hba_ok = False
    if hba_path:
        try:
            hba_ok = pg_hba_allows_bridge(Path(hba_path).read_text(encoding="utf-8"))
        except OSError:
            hba_ok = False
    checks.append(
        BridgeCheck(
            "postgres-hba-bridge",
            hba_ok,
            (
                f"pg_hba.conf allows {DOCKER_BRIDGE_CIDR} with scram-sha-256"
                if hba_ok
                else f"pg_hba.conf must include: host all all {DOCKER_BRIDGE_CIDR} scram-sha-256"
            ),
            () if hba_ok else remediation,
        )
    )

    docker_result = runner.run(
        [
            "docker",
            "run",
            "--rm",
            "--add-host",
            "host.docker.internal:host-gateway",
            docker_image,
            "pg_isready",
            "-h",
            DOCKER_BRIDGE_HOST,
            "-p",
            POSTGRES_PORT,
            "-t",
            "3",
        ],
        timeout=60,
    )
    container_ok = docker_result.returncode == 0
    checks.append(
        BridgeCheck(
            "postgres-container-bridge",
            container_ok,
            (
                f"A Docker container can reach PostgreSQL at {DOCKER_BRIDGE_HOST}:{POSTGRES_PORT}"
                if container_ok
                else f"A Docker container cannot reach PostgreSQL at {DOCKER_BRIDGE_HOST}:{POSTGRES_PORT}"
            ),
            () if container_ok else remediation,
        )
    )
    return checks
