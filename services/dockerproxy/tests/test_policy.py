import json
from dataclasses import replace
from typing import Any

import pytest

from dockerproxy.policy import check_create, is_ours, route
from payloads import ABSENT, GOOD, NAME, POLICY, mutated


def create(doc: dict[str, Any], name: str = NAME) -> list[str]:
    return check_create(json.dumps(doc).encode(), name, POLICY)


# ---- the exact requests the orchestrator makes ----------------------------------------------------------------------------------
@pytest.mark.parametrize("method,target,kind", [
    ("HEAD", "/_ping", "ping"),
    ("GET", "/_ping", "ping"),
    ("GET", "/v1.56/version", "version"),
    ("POST", f"/v1.56/containers/create?name={NAME}", "create"),
    ("POST", "/v1.56/containers/4a4ac823f358/attach?stderr=1&stdin=1&stdout=1&stream=1", "attach"),
    ("POST", "/v1.56/containers/4a4ac823f358/start", "start"),
    ("POST", "/v1.56/containers/4a4ac823f358/wait?condition=next-exit", "wait"),
    ("POST", f"/v1.56/containers/{NAME}/kill", "kill"),
    ("DELETE", f"/v1.56/containers/{NAME}?force=1", "remove"),
    ("GET", f"/v1.56/containers/{NAME}/json", "inspect"),
    ("GET", "/v1.56/networks/memorylab-sandbox", "network-inspect"),
])
def test_the_calls_the_orchestrator_makes_are_allowed(method: str, target: str, kind: str) -> None:
    v = route(method, target, POLICY)
    assert v.allow and v.route == kind, v
    assert v.upgrade == (kind == "attach") and v.needs_body == (kind == "create")
    assert (v.container is not None) == (kind not in ("ping", "version", "network-inspect"))


@pytest.mark.parametrize("method,target", [
    ("GET", "/containers/json"),                                       # listing other projects' containers
    ("GET", "/images/json"),
    ("POST", "/images/create?fromImage=alpine"),                       # pulling an image
    ("POST", "/build"),
    ("POST", f"/containers/{NAME}/exec"),                              # exec into a sandbox
    ("POST", "/exec/abc/start"),
    ("POST", f"/containers/{NAME}/update"),
    ("POST", f"/containers/{NAME}/restart"),
    ("POST", f"/containers/{NAME}/stop"),
    ("GET", f"/containers/{NAME}/archive?path=/"),                     # reading a container's files
    ("PUT", f"/containers/{NAME}/archive?path=/"),
    ("POST", f"/containers/{NAME}/rename?name=x"),
    ("GET", f"/containers/{NAME}/logs"),
    ("POST", "/containers/prune"),
    ("GET", "/volumes"),
    ("POST", "/volumes/create"),
    ("GET", "/networks"),
    ("POST", "/networks/create"),
    ("POST", "/networks/memorylab-sandbox/connect"),
    ("GET", "/networks/bridge"),                                       # another network
    ("GET", "/networks/host"),
    ("GET", "/info"),
    ("GET", "/events"),
    ("POST", "/swarm/init"),
    ("POST", "/plugins/pull"),
    ("POST", "/session"),
    ("DELETE", "/images/alpine"),
    ("GET", "/system/df"),
    ("POST", "/containers/create?name=evil"),                         # not a sandbox name
    ("POST", f"/containers/create?name={NAME}&platform=x"),
    ("POST", "/containers/create"),
    ("DELETE", f"/containers/{NAME}?force=1&link=1"),
    ("DELETE", f"/containers/{NAME}?v=1&v=1"),                         # repeated parameter
    ("POST", f"/containers/{NAME}/kill?signal=KILL;rm"),
    ("DELETE", f"/containers/{NAME}/../../images/x"),
    ("DELETE", f"/containers/{NAME}%2f..%2fx"),
    ("DELETE", "/containers//x"),
    ("DELETE", "/containers/-bad"),
    ("DELETE", "/containers/a b"),
    ("GET", "http://evil/_ping"),
    ("GET", "_ping"),
    ("POST", f"/containers/{NAME}/kill#x"),
    ("TRACE", "/_ping"),
    ("GET", "/_ping?x=1"),
])
def test_everything_else_is_refused(method: str, target: str) -> None:
    v = route(method, target, POLICY)
    assert not v.allow and v.reason, (method, target)


# ---- a create ------------------------------------------------------------------------------------------------------------------
def test_the_real_sandbox_create_is_accepted() -> None:
    assert create(GOOD) == []


def test_an_onbuild_field_is_accepted_only_when_empty() -> None:
    # Docker CLIs newer than the captured 29.9 serialise an OnBuild Config field (as null) in the create body.
    # It has no effect on `docker run` (OnBuild triggers only fire when an image is a build base), but the
    # proxy still default-denies it unless it is empty, so a non-empty value can never slip through.
    assert create(mutated(OnBuild=None)) == []                                        # what a newer CLI actually sends
    assert create(mutated(OnBuild=[])) == []
    assert create(mutated(OnBuild=["RUN touch /pwned"])) == ["Cmd/Entrypoint/WorkingDir/Volumes/OnBuild"]


def test_a_runtime_and_an_environment_variable_are_accepted_only_when_configured() -> None:
    doc = json.dumps(mutated(**{"HostConfig.Runtime": "runsc", "Env": ["MEMORYLAB_ALLOW_FAKE_PROVIDER=1"]})).encode()
    assert sorted(check_create(doc, NAME, POLICY)) == ["Env"]                       # by default a sandbox gets no environment at all
    configured = replace(POLICY, env_names=frozenset({"MEMORYLAB_ALLOW_FAKE_PROVIDER"}))
    assert check_create(doc, NAME, configured) == []


@pytest.mark.parametrize("changes,expect", [
    ({"HostConfig.Privileged": True}, "Privileged"),
    ({"HostConfig.Binds": ["/:/host"]}, "Binds"),
    ({"HostConfig.Binds": ["/var/run/docker.sock:/var/run/docker.sock"]}, "Binds"),
    ({"HostConfig.Mounts": [{"Type": "bind", "Source": "/", "Target": "/host"}]}, "Mounts"),
    ({"HostConfig.CapAdd": ["SYS_ADMIN"]}, "CapAdd"),
    ({"HostConfig.CapDrop": []}, "CapDrop"),
    ({"HostConfig.CapDrop": ["NET_RAW"]}, "CapDrop"),
    ({"HostConfig.PidMode": "host"}, "PidMode"),
    ({"HostConfig.NetworkMode": "host"}, "NetworkMode"),
    ({"HostConfig.NetworkMode": "bridge"}, "NetworkMode"),
    ({"HostConfig.NetworkMode": "container:abc"}, "NetworkMode"),
    ({"HostConfig.Devices": [{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda", "CgroupPermissions": "rwm"}]}, "Devices"),
    ({"HostConfig.DeviceRequests": [{"Driver": "nvidia"}]}, "DeviceRequests"),
    ({"HostConfig.SecurityOpt": []}, "SecurityOpt"),
    ({"HostConfig.SecurityOpt": ["seccomp=unconfined"]}, "SecurityOpt"),
    ({"HostConfig.SecurityOpt": ["no-new-privileges", "apparmor=unconfined"]}, "SecurityOpt"),
    ({"HostConfig.SecurityOpt": ABSENT}, "SecurityOpt"),
    ({"HostConfig.ReadonlyRootfs": False}, "ReadonlyRootfs"),
    ({"HostConfig.ReadonlyRootfs": ABSENT}, "ReadonlyRootfs"),
    ({"HostConfig.MaskedPaths": []}, "MaskedPaths/ReadonlyPaths"),
    ({"HostConfig.ReadonlyPaths": []}, "MaskedPaths/ReadonlyPaths"),
    ({"HostConfig.UsernsMode": "host"}, "UsernsMode"),
    ({"HostConfig.UTSMode": "host"}, "UTSMode"),
    ({"HostConfig.IpcMode": "host"}, "IpcMode"),
    ({"HostConfig.CgroupnsMode": "host"}, "CgroupnsMode"),
    ({"HostConfig.PortBindings": {"80/tcp": [{"HostPort": "80"}]}}, "PortBindings"),
    ({"HostConfig.PublishAllPorts": True}, "PublishAllPorts"),
    ({"HostConfig.ExtraHosts": ["x:10.0.0.1"]}, "ExtraHosts"),
    ({"HostConfig.Dns": ["8.8.8.8"]}, "Dns"),
    ({"HostConfig.Sysctls": {"net.ipv4.ip_forward": "1"}}, "Sysctls"),
    ({"HostConfig.Links": ["db:db"]}, "Links"),
    ({"HostConfig.VolumesFrom": ["other"]}, "VolumesFrom"),
    ({"HostConfig.GroupAdd": ["docker"]}, "GroupAdd"),
    ({"HostConfig.CgroupParent": "/"}, "CgroupParent"),
    ({"HostConfig.Runtime": "nvidia"}, "Runtime"),
    ({"HostConfig.Memory": 0}, "Memory"),
    ({"HostConfig.Memory": 1 << 40}, "Memory"),
    ({"HostConfig.Memory": ABSENT}, "Memory"),
    ({"HostConfig.MemorySwap": -1}, "MemorySwap"),
    ({"HostConfig.PidsLimit": 0}, "PidsLimit"),
    ({"HostConfig.PidsLimit": -1}, "PidsLimit"),
    ({"HostConfig.PidsLimit": 1 << 20}, "PidsLimit"),
    ({"HostConfig.OomScoreAdj": -1000}, "OomScoreAdj"),
    ({"HostConfig.ShmSize": 1 << 40}, "ShmSize"),
    ({"HostConfig.Tmpfs": {"/": "rw"}}, "Tmpfs"),
    ({"HostConfig.Tmpfs": {"/tmp": "rw,exec,size=64m"}}, "Tmpfs options"),
    ({"HostConfig.Tmpfs": {"/tmp": "rw,size=64m"}}, "Tmpfs options"),
    ({"HostConfig.Tmpfs": {"/tmp": "rw,noexec,size=9999m"}}, "Tmpfs options"),
    ({"HostConfig.RestartPolicy": {"Name": "always"}}, "RestartPolicy"),
    ({"HostConfig.LogConfig": {"Type": "syslog"}}, "LogConfig"),
    ({"HostConfig.Ulimits": [{"Name": "memlock", "Hard": -1, "Soft": -1}]}, "Ulimits"),
    ({"HostConfig.StorageOpt": {"size": "1g"}}, "StorageOpt"),
    ({"HostConfig.SomethingNew": "x"}, "unknown HostConfig field SomethingNew"),         # a field we have never seen is refused
    ({"Image": "alpine"}, "Image"),
    ({"Image": "memorylab-worker:evil"}, "Image"),
    ({"Image": "memorylab-worker"}, "Image"),
    ({"Image": "sha256:" + "0" * 64}, "Image"),
    ({"Labels": {}}, "Labels"),
    ({"Labels": {"memorylab.owner": "someone-else", "memorylab.run": "898c1ae2-c330-454b-9bf6-274f4c305038"}}, "Labels"),
    ({"Labels": {"memorylab.owner": "memorylab", "memorylab.run": "other"}}, "Labels"),
    ({"Labels": ABSENT}, "Labels"),
    ({"User": ""}, "User"),
    ({"User": "0"}, "User"),
    ({"User": "0:0"}, "User"),
    ({"User": "root"}, "User"),
    ({"User": "10001:0"}, "User"),
    ({"User": ABSENT}, "User"),
    ({"Entrypoint": ["/bin/sh"]}, "Cmd/Entrypoint/WorkingDir/Volumes/OnBuild"),
    ({"Cmd": ["sh", "-c", "id"]}, "Cmd/Entrypoint/WorkingDir/Volumes/OnBuild"),
    ({"WorkingDir": "/"}, "Cmd/Entrypoint/WorkingDir/Volumes/OnBuild"),
    ({"Volumes": {"/data": {}}}, "Cmd/Entrypoint/WorkingDir/Volumes/OnBuild"),
    ({"Env": ["ANTHROPIC_API_KEY=sk-ant-x"]}, "Env"),
    ({"Env": ["LD_PRELOAD=/tmp/x.so"]}, "Env"),
    ({"Env": [7]}, "Env"),
    ({"NetworkingConfig": {"EndpointsConfig": {"bridge": {}}}}, "NetworkingConfig"),
    ({"ExposedPorts": {"22/tcp": {}}}, "unknown field ExposedPorts"),
    ({"Healthcheck": {"Test": ["CMD", "x"]}}, "unknown field Healthcheck"),
])
def test_every_way_out_of_the_sandbox_is_refused(changes: dict[str, Any], expect: str) -> None:
    reasons = create(mutated(**changes))
    assert expect in reasons, reasons


def test_a_create_that_is_not_a_container_configuration_is_refused() -> None:
    for body in (b"", b"not json", b"[]", b'{"Image":"memorylab-worker:dev"}', b"null", b'{"HostConfig":[]}'):
        assert check_create(body, NAME, POLICY), body
    assert check_create(b"{" + b" " * 40_000 + b"}", NAME, POLICY) == ["body too large"]


def test_the_policy_names_the_run_in_the_label_and_the_name_together() -> None:
    assert create(GOOD, "memorylab-run-00000000-0000-0000-0000-000000000000") == ["Labels"]


def test_ownership_comes_from_the_daemons_answer() -> None:
    ours = {"Name": f"/{NAME}", "Config": {"Labels": {"memorylab.owner": "memorylab"}}}
    assert is_ours(ours) is True
    assert is_ours(None) is None
    assert is_ours({"Name": "/memorylab-run-x", "Config": {"Labels": {}}}) is False                 # right name, no label
    assert is_ours({"Name": "/postgres", "Config": {"Labels": {"memorylab.owner": "memorylab"}}}) is False   # label, wrong name
    assert is_ours({"Name": "/postgres", "Config": {"Labels": None}}) is False
    assert is_ours({}) is False
