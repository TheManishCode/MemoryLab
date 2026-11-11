"""The proxy in front of the REAL daemon, driven by the real `docker` CLI exactly as the orchestrator drives it, plus the attacks a compromised
orchestrator would try. Creates only objects named memorylab-* (a throwaway internal network, containers) and removes them; the daemon may host
other projects, which are never touched. Needs the images: infra/docker/build.sh."""

import asyncio
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator

import pytest
from orchestrator.config import OrchestratorConfig
from orchestrator.dockercmd import build_run_command

from dockerproxy.policy import Policy
from dockerproxy.server import Proxy

IMAGE = "memorylab-worker:dev"
SOCKET = "/var/run/docker.sock"
LABEL = "memorylab.owner=memorylab"


def docker(*args: str, env: dict[str, str] | None = None, input: bytes | None = None, timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout, check=False, input=input, env={**os.environ, **(env or {})})


def have_docker() -> bool:
    try:
        return docker("image", "inspect", IMAGE).returncode == 0 and os.path.exists(SOCKET)
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [pytest.mark.docker, pytest.mark.skipif(not have_docker(), reason="needs Docker and the worker image: infra/docker/build.sh")]


class Running:
    def __init__(self) -> None:
        self.net = "memorylab-test-dp-" + uuid.uuid4().hex[:8]
        self.created: list[str] = []
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.port = 0
        self.server: asyncio.Server | None = None

    def start(self) -> None:
        r = docker("network", "create", "--internal", "--label", LABEL, self.net)
        assert r.returncode == 0, r.stderr
        self.thread.start()
        proxy = Proxy(Policy(images=frozenset({IMAGE}), network=self.net), SOCKET)
        self.server = asyncio.run_coroutine_threadsafe(proxy.start("127.0.0.1", 0), self.loop).result(10)
        self.port = self.server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        if self.server:
            self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        for name in self.created:
            docker("rm", "-f", name)
        docker("network", "rm", self.net)

    @property
    def env(self) -> dict[str, str]:
        return {"DOCKER_HOST": f"tcp://127.0.0.1:{self.port}"}

    def via(self, *args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return docker(*args, env=self.env, input=input)


@pytest.fixture(scope="module")
def stack() -> Iterator[Running]:
    s = Running()
    s.start()
    try:
        yield s
    finally:
        s.stop()


def exists(name: str) -> bool:
    return docker("container", "inspect", name).returncode == 0


def refused(r: subprocess.CompletedProcess[bytes]) -> bool:
    return r.returncode != 0 and b"memorylab docker proxy" in r.stderr


def test_the_orchestrators_exact_run_command_works_through_the_proxy(stack: Running) -> None:
    run_id = str(uuid.uuid4())
    cfg = OrchestratorConfig(image=IMAGE, network=stack.net, proxy_url="http://x:1", sandbox_ingest_url="http://x", ingest_url="http://x",
                             games_dir="/x", token_secret=b"s" * 32)
    argv = build_run_command(cfg, run_id)
    stack.created.append(f"memorylab-run-{run_id}")
    r = docker(*argv[1:], env=stack.env, input=b"not a job\n")
    assert b"memorylab docker proxy" not in r.stderr, r.stderr                 # create, attach (stdin), start, wait and the auto-remove all passed
    assert r.returncode == 2                                                   # the worker itself ran and rejected the junk job spec
    assert not exists(f"memorylab-run-{run_id}")                              # --rm removed it


def test_the_orchestrators_other_calls_work_through_the_proxy(stack: Running) -> None:
    name = f"memorylab-run-{uuid.uuid4()}"
    stack.created.append(name)
    assert docker("create", "--name", name, "--label", LABEL, "--label", f"memorylab.run={name[14:]}", "--network", stack.net, IMAGE).returncode == 0
    assert stack.via("container", "inspect", name).returncode == 0
    assert stack.via("network", "inspect", stack.net).returncode == 0
    killed = stack.via("kill", name)                                           # not running: the daemon refuses, not the proxy
    assert killed.returncode != 0 and b"memorylab docker proxy" not in killed.stderr
    assert stack.via("rm", "-f", name).returncode == 0
    assert not exists(name)


@pytest.mark.parametrize("flags", [
    ["--privileged"],
    ["-v", "/:/host"],
    ["-v", "/var/run/docker.sock:/var/run/docker.sock"],
    ["--mount", "type=bind,source=/,target=/host"],
    ["--network", "host"],
    ["--pid", "host"],
    ["--ipc", "host"],
    ["--uts", "host"],
    ["--userns", "host"],
    ["--cap-add", "SYS_ADMIN"],
    ["--device", "/dev/null:/dev/x"],
    ["--security-opt", "seccomp=unconfined"],
    ["--user", "root"],
    ["--entrypoint", "/bin/sh"],
    ["-p", "8080:80"],
    ["--add-host", "x:10.0.0.1"],
    ["--sysctl", "net.ipv4.ip_forward=1"],
    ["--runtime", "nvidia"],
    ["--memory", "512g"],
    ["--env", "ANTHROPIC_API_KEY=sk-ant-x"],
])
def test_a_compromised_orchestrator_cannot_start_an_escape(stack: Running, flags: list[str]) -> None:
    run_id = str(uuid.uuid4())
    name = f"memorylab-run-{run_id}"
    stack.created.append(name)
    base = ["run", "-d", "--name", name, "--label", LABEL, "--label", f"memorylab.run={run_id}", "--network", stack.net, "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "256m", "--memory-swap", "256m",
            "--user", "10001:10001"]
    # the flags under test replace the matching safe one where there is one, so the only thing wrong with the request is the flag itself
    argv = [a for a in base]
    if flags[0] == "--network":
        i = argv.index("--network")
        argv[i:i + 2] = []
    if flags[0] == "--user":
        i = argv.index("--user")
        argv[i:i + 2] = []
    if flags[0] == "--security-opt":
        i = argv.index("--security-opt")
        argv[i:i + 2] = []
    if flags[0] == "--memory":
        for opt in ("--memory", "--memory-swap"):
            i = argv.index(opt)
            argv[i:i + 2] = []
    r = stack.via(*argv, *flags, IMAGE)
    assert refused(r), (flags, r.stderr)
    assert not exists(name)                                                    # nothing was created


def test_a_foreign_image_and_a_foreign_name_are_refused(stack: Running) -> None:
    run_id = str(uuid.uuid4())
    common = ["--label", LABEL, "--label", f"memorylab.run={run_id}", "--network", stack.net, "--read-only", "--cap-drop", "ALL",
              "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "256m", "--memory-swap", "256m", "--user", "10001:10001"]
    r = stack.via("create", "--name", f"memorylab-run-{run_id}", *common, "python:3.14-slim", "true")
    assert refused(r) and not exists(f"memorylab-run-{run_id}")
    r = stack.via("create", "--name", "postgres-evil", *common, IMAGE)
    assert refused(r) and not exists("postgres-evil")
    r = stack.via("create", *common, IMAGE)                                    # no name at all
    assert refused(r)
    r = stack.via("create", "--name", f"memorylab-run-{run_id}", "--network", stack.net, IMAGE)    # no hardening flags, no labels
    assert refused(r) and not exists(f"memorylab-run-{run_id}")


def test_everything_else_the_docker_api_offers_is_refused(stack: Running) -> None:
    foreign = "memorylab-test-foreign-" + uuid.uuid4().hex[:8]
    stack.created.append(foreign)
    assert docker("run", "-d", "--name", foreign, "--label", LABEL, "--network", stack.net, "--read-only", "--cap-drop", "ALL", "--entrypoint", "sleep",
                  IMAGE, "300").returncode == 0                                    # labelled like ours, but not named memorylab-run-*
    for argv in (["ps"], ["ps", "-a"], ["images"], ["pull", "alpine"], ["exec", foreign, "id"], ["kill", foreign], ["rm", "-f", foreign],
                 ["stop", foreign], ["restart", foreign], ["logs", foreign], ["cp", f"{foreign}:/etc/passwd", "-"], ["volume", "ls"],
                 ["network", "ls"], ["network", "inspect", "bridge"], ["network", "create", "x-evil"], ["info"], ["system", "prune", "-f"],
                 ["rename", foreign, "x"], ["update", "--memory", "1g", foreign], ["build", "-q", "-"]):
        r = stack.via(*argv, input=b"FROM scratch\n" if argv[0] == "build" else None)
        assert r.returncode != 0 and b"memorylab docker proxy" in r.stderr + r.stdout, (argv, r.stderr)
    time.sleep(0.3)
    state = docker("inspect", "-f", "{{.State.Running}}", foreign)
    assert state.stdout.strip() == b"true"                                     # the foreign container was never touched
    assert docker("network", "inspect", "x-evil").returncode != 0              # and nothing was created


def raw_create(stack: Running, name: str, doc: dict[str, object]) -> tuple[int, str]:
    """POST a create straight to the proxy (not through the CLI), so the body is exactly what the test writes."""
    import http.client
    import json as _json
    conn = http.client.HTTPConnection("127.0.0.1", stack.port, timeout=30)
    conn.request("POST", f"/v{daemon_api_version()}/containers/create?name={name}", _json.dumps(doc), {"Content-Type": "application/json"})
    r = conn.getresponse()
    return r.status, r.read().decode()


def daemon_api_version() -> str:
    """The daemon's own API version (1.56 on a current Docker, 1.48 on a hosted runner), which is also what the CLI negotiates down to."""
    import subprocess
    out = subprocess.run(["docker", "version", "--format", "{{.Server.APIVersion}}"], capture_output=True, text=True, check=True)
    return min(out.stdout.strip(), "1.56", key=lambda v: tuple(int(x) for x in v.split(".")))


def good_doc(stack: Running, run_id: str) -> dict[str, object]:
    import copy

    from payloads import GOOD
    doc = copy.deepcopy(GOOD)
    doc["Labels"] = {"memorylab.owner": "memorylab", "memorylab.run": run_id}
    doc["HostConfig"]["NetworkMode"] = stack.net  # type: ignore[index]
    return doc


def test_second_spelling_payloads_are_refused_and_nothing_is_created_but_the_good_one_works(stack: Running) -> None:
    run_id = str(uuid.uuid4())
    name = f"memorylab-run-{run_id}"
    stack.created.append(name)
    extras: list[dict[str, object]] = [
        {"image": "memorylab-egress:dev"}, {"user": "0"}, {"cmd": ["sh", "-c", "id"]}, {"hostconfig": {"Binds": ["/tmp/x:/host:ro"]}},
        {"hostConfig": {"Privileged": True, "CapAdd": ["SYS_ADMIN"]}}, {"hostConfig": {"Privileged": True, "Binds": ["/:/host"]}}]
    for extra in extras:
        status, _ = raw_create(stack, name, {**good_doc(stack, run_id), **extra})
        assert status == 403, extra
        assert not exists(name), extra                                         # the daemon never saw it
    status, body = raw_create(stack, name, good_doc(stack, run_id))
    assert status == 201, body
    info = docker("inspect", "-f", "{{.Config.Image}} {{.Config.User}} {{.HostConfig.Privileged}} {{len .HostConfig.Binds}}", name).stdout.decode().strip()
    assert info == f"{IMAGE} 10001:10001 false 0"
