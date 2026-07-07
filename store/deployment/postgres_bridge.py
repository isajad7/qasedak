import ipaddress
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


DOCKER_BRIDGE_HOST = "172.17.0.1"
DOCKER_BRIDGE_CIDR = "172.17.0.0/16"
POSTGRES_PORT = "5432"


@dataclass(frozen=True)
class BridgeCheck:
    name: str
    ok: bool
    message: str
    remediation: tuple[str, ...] = ()


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


def _first_line(text):
    return (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""


def discover_hba_file(runner):
    result = runner.run(["runuser", "-u", "postgres", "--", "psql", "-Atqc", "SHOW hba_file;", "postgres"], timeout=15)
    if result.returncode == 0:
        return _first_line(result.stdout)
    result = runner.run(["sudo", "-u", "postgres", "psql", "-Atqc", "SHOW hba_file;", "postgres"], timeout=15)
    if result.returncode == 0:
        return _first_line(result.stdout)
    return ""


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
