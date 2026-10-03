"""Pins the security-relevant parts of the hub's deploy files.

Changing any of these is a root change on the server too (deploy/README.md):
this test makes sure it is a deliberate one.
"""

from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def compose() -> dict:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))


def hub() -> dict:
    return compose()["services"]["hub"]


def test_only_the_hub_service_and_no_build():
    data = compose()
    assert list(data["services"]) == ["hub"]
    assert "build" not in hub()
    assert hub()["pull_policy"] == "never"
    assert hub()["image"] == "dashboard-x:${APP_VERSION:-current}"


def test_container_is_locked_down():
    service = hub()
    assert service["user"] == "10001:10001"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["mem_limit"] == "192m"
    assert service["pids_limit"] == 64
    for key in ["privileged", "cap_add", "devices", "network_mode", "pid", "ipc", "userns_mode"]:
        assert key not in service, key


def test_mounts_are_exactly_data_and_read_only_config():
    assert hub()["volumes"] == [
        "/home/deploy/Bots_web_dashboard/shared/data:/data",
        "/home/deploy/Bots_web_dashboard:/config:ro",
    ]
    assert hub()["env_file"] == ["/home/deploy/Bots_web_dashboard/shared/.env"]


def test_published_only_on_localhost():
    assert hub()["ports"] == ["127.0.0.1:8790:8000"]


def test_networks_and_trusted_proxy():
    data = compose()
    assert hub()["networks"] == ["edge", "hub_net"]
    assert data["networks"]["hub_net"] == {"external": True}
    gateway = data["networks"]["edge"]["ipam"]["config"][0]["gateway"]
    # X-Forwarded-For is trusted from the host side of `edge` and nowhere else.
    assert hub()["environment"]["FORWARDED_ALLOW_IPS"] == gateway
    assert "*" not in gateway


def test_dockerfile_takes_only_files_and_runs_unprivileged():
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim@sha256:" in text
    copies = [line.split()[1] for line in text.splitlines() if line.startswith("COPY")]
    assert copies == ["requirements.txt", "shared/hub", "shared/templates", "shared/static"]
    assert "USER 10001:10001" in text
    assert "--forwarded-allow-ips" not in text  # comes from the compose file only


def test_deploy_script_never_reads_the_client_command_or_sudo():
    text = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "SSH_ORIGINAL_COMMAND" not in code
    assert "sudo" not in code
    assert "prune" not in code
    assert "PIN_DIR=/opt/dashboard-x-deploy" in code
    assert '-f "$PIN_DIR/Dockerfile"' in code
    assert "fetch.fsckObjects=true" in code
