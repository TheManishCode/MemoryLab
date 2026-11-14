"""What the orchestrator may ask the Docker daemon, and nothing more.

The orchestrator starts one sandbox per run through the `docker` CLI. A raw Docker socket would let a compromised orchestrator run
`docker run --privileged -v /:/host` and own the host, so the socket is held by this filter instead. It allows exactly the calls
the orchestrator makes (`docker run -i --rm`, `kill`, `rm -f`, `container inspect`, `network inspect`) and refuses everything else.

Pure functions: no I/O, so every rule is a unit test. Default deny: a create request is accepted only if every field is one we set
ourselves (orchestrator/dockercmd.py builds the exact command) or is empty/zero."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

OWNER_LABEL, OWNER_VALUE = "memorylab.owner", "memorylab"
NAME = re.compile(r"memorylab-run-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
USER = re.compile(r"[1-9][0-9]{3,8}(:[1-9][0-9]{3,8})?")
SIGNAL = re.compile(r"(SIG)?[A-Z0-9]{1,10}")
VERSION = re.compile(r"/v1\.(4[4-9]|5[0-9]|60)")     # the API versions the Docker CLI in our orchestrator image speaks (1.56 today)
MAX_BODY = 32 * 1024


@dataclass(frozen=True)
class Policy:
    images: frozenset[str]                                   # exactly these image references may be started
    network: str                                             # the one network a sandbox may be on (internal: no route out)
    runtimes: frozenset[str] = frozenset({"", "runsc"})      # "" = the daemon default
    env_names: frozenset[str] = frozenset()                  # environment variables a sandbox may be given (none by default; never secrets)
    max_memory: int = 4 << 30
    max_pids: int = 1024
    max_tmpfs_mb: int = 256


@dataclass(frozen=True)
class Verdict:
    allow: bool
    reason: str = ""
    route: str = ""                                          # which allowed call this is
    container: str | None = None                             # the container the call acts on, to be checked for ownership
    upgrade: bool = False                                    # a hijacked stream (attach): bytes flow both ways after the response head
    needs_body: bool = False


def deny(reason: str) -> Verdict:
    return Verdict(False, reason)


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == 0 or v is False or v == [] or v == {}


def route(method: str, target: str, policy: Policy) -> Verdict:
    """Decide from the request line alone. A create still needs `check_create` on its body; a call on a container needs `owned`."""
    if not target.startswith("/") or "\\" in target or any(ord(c) < 33 or ord(c) > 126 for c in target) or "%" in target:
        return deny("malformed path")
    parts = urlsplit(target)
    if parts.fragment:
        return deny("malformed path")
    path = parts.path
    versioned = VERSION.match(path)
    if versioned:
        path = path[versioned.end():] or "/"
    if path != "/_ping" and not versioned:
        return deny("an API version in the supported range is required")      # only the unversioned ping is allowed without one
    if "//" in path or ".." in path.split("/"):
        return deny("malformed path")
    query = parse_qsl(parts.query, keep_blank_values=True)
    names = [k for k, _ in query]
    if len(set(names)) != len(names):
        return deny("repeated query parameter")
    q = dict(query)

    def only(*allowed: str) -> bool:
        return set(q) <= set(allowed)

    if path == "/_ping" and method in ("GET", "HEAD") and not q:
        return Verdict(True, route="ping")
    if path == "/version" and method == "GET" and not q:
        return Verdict(True, route="version")
    if path == "/containers/create" and method == "POST":
        name = q.get("name", "")
        if not only("name") or not NAME.fullmatch(name):
            return deny("a sandbox must be named memorylab-run-<uuid>")
        return Verdict(True, route="create", container=name, needs_body=True)
    if (m := re.fullmatch(r"/networks/([A-Za-z0-9_.-]{1,64})", path)) and method == "GET":
        if m.group(1) != policy.network or not only("verbose", "scope"):
            return deny("only the sandbox network may be inspected")
        return Verdict(True, route="network-inspect")
    if m := re.fullmatch(r"/containers/([^/]+)(?:/(json|start|kill|wait|attach|resize))?", path):
        ident, action = m.group(1), m.group(2)
        if not IDENT.fullmatch(ident):
            return deny("malformed container reference")
        table: dict[tuple[str, str | None], tuple[str, tuple[str, ...], bool]] = {
            ("GET", "json"): ("inspect", ("size",), False),
            ("POST", "start"): ("start", (), False),
            ("POST", "kill"): ("kill", ("signal",), False),
            ("POST", "wait"): ("wait", ("condition",), False),
            ("POST", "attach"): ("attach", ("stream", "stdin", "stdout", "stderr", "logs"), True),
            ("POST", "resize"): ("resize", ("h", "w"), False),
            ("DELETE", None): ("remove", ("force", "v"), False),
        }
        entry = table.get((method, action))
        if entry is None:
            return deny(f"{method} on a container is not allowed")
        name, allowed_q, upgrade = entry
        if not only(*allowed_q):
            return deny("unexpected query parameter")
        if name == "kill" and "signal" in q and not SIGNAL.fullmatch(q["signal"]):
            return deny("bad signal")
        if name == "remove" and q.get("force") not in (None, "0", "1", "true", "false"):
            return deny("bad force")
        return Verdict(True, route=name, container=ident, upgrade=upgrade)
    return deny("this Docker API call is not allowed")


# fields `_hostconfig` examines, and plain resource settings that cannot widen what a sandbox may touch
CHECKED = frozenset({
    "Binds", "Mounts", "CapAdd", "Devices", "DeviceCgroupRules", "DeviceRequests", "VolumesFrom", "Links", "GroupAdd", "ExtraHosts", "Dns",
    "DnsOptions", "DnsSearch", "PortBindings", "Sysctls", "StorageOpt", "VolumeDriver", "CgroupParent", "Cgroup", "ContainerIDFile",
    "UsernsMode", "UTSMode", "PidMode", "Isolation", "OomKillDisable", "CpuRealtimePeriod", "CpuRealtimeRuntime", "Privileged",
    "PublishAllPorts", "IpcMode", "CgroupnsMode", "NetworkMode", "ReadonlyRootfs", "CapDrop", "SecurityOpt", "MaskedPaths", "ReadonlyPaths",
    "Runtime", "Memory", "PidsLimit", "OomScoreAdj", "MemorySwap", "ShmSize", "Tmpfs", "LogConfig", "RestartPolicy", "Ulimits"})
RESOURCE_KNOBS = frozenset({
    "AutoRemove", "ConsoleSize", "CpuShares", "NanoCpus", "CpuPeriod", "CpuQuota", "CpusetCpus", "CpusetMems", "MemoryReservation",
    "MemorySwappiness", "BlkioWeight", "BlkioWeightDevice", "BlkioDeviceReadBps", "BlkioDeviceWriteBps", "BlkioDeviceReadIOps",
    "BlkioDeviceWriteIOps", "CpuCount", "CpuPercent", "IOMaximumIOps", "IOMaximumBandwidth", "Init"})


def _hostconfig(hc: Mapping[str, Any], p: Policy) -> list[str]:
    bad: list[str] = []
    # every one of these is a way out of the sandbox: they must be absent/empty/default
    for key in ("Binds", "Mounts", "CapAdd", "Devices", "DeviceCgroupRules", "DeviceRequests", "VolumesFrom", "Links", "GroupAdd", "ExtraHosts",
                "Dns", "DnsOptions", "DnsSearch", "PortBindings", "Sysctls", "StorageOpt", "VolumeDriver", "CgroupParent", "Cgroup",
                "ContainerIDFile", "UsernsMode", "UTSMode", "PidMode", "Isolation", "OomKillDisable", "CpuRealtimePeriod", "CpuRealtimeRuntime"):
        if not _empty(hc.get(key)):
            bad.append(key)
    if hc.get("Privileged") not in (None, False):
        bad.append("Privileged")
    if hc.get("PublishAllPorts") not in (None, False):
        bad.append("PublishAllPorts")
    if hc.get("IpcMode") not in (None, "", "private"):
        bad.append("IpcMode")
    if hc.get("CgroupnsMode") not in (None, "", "private"):
        bad.append("CgroupnsMode")
    # a sandbox is always: this network, read-only root, no capabilities, no new privileges, default masked/readonly paths
    if hc.get("NetworkMode") != p.network:
        bad.append("NetworkMode")
    if hc.get("ReadonlyRootfs") is not True:
        bad.append("ReadonlyRootfs")
    if hc.get("CapDrop") != ["ALL"]:
        bad.append("CapDrop")
    if hc.get("SecurityOpt") != ["no-new-privileges"]:
        bad.append("SecurityOpt")
    if hc.get("MaskedPaths") is not None or hc.get("ReadonlyPaths") is not None:
        bad.append("MaskedPaths/ReadonlyPaths")                        # an empty list would unmask /proc and friends
    if hc.get("Runtime", "") not in p.runtimes:
        bad.append("Runtime")
    memory, pids = hc.get("Memory"), hc.get("PidsLimit")
    if not isinstance(memory, int) or isinstance(memory, bool) or not 0 < memory <= p.max_memory:
        bad.append("Memory")
    if not isinstance(pids, int) or isinstance(pids, bool) or not 0 < pids <= p.max_pids:
        bad.append("PidsLimit")
    oom = hc.get("OomScoreAdj")
    if oom is not None and (not isinstance(oom, int) or oom < 0):
        bad.append("OomScoreAdj")
    swap, shm = hc.get("MemorySwap"), hc.get("ShmSize")
    if swap not in (None, 0, memory):
        bad.append("MemorySwap")                                        # unlimited swap would defeat the memory limit
    if not isinstance(shm, int) or isinstance(shm, bool) or not 0 <= shm <= 64 << 20:
        bad.append("ShmSize")
    for key, value in hc.items():
        if key not in CHECKED and key not in RESOURCE_KNOBS and not _empty(value):
            bad.append(f"unknown HostConfig field {key}")               # default deny
    tmpfs = hc.get("Tmpfs")
    if tmpfs is not None:
        if not isinstance(tmpfs, dict) or set(tmpfs) - {"/tmp"}:
            bad.append("Tmpfs")
        else:
            opts = str(tmpfs.get("/tmp", ""))
            size = re.search(r"(?:^|,)size=(\d+)m(?:,|$)", opts)
            if "noexec" not in opts.split(",") or not size or int(size.group(1)) > p.max_tmpfs_mb or "exec" in opts.split(","):
                bad.append("Tmpfs options")
    log = hc.get("LogConfig") or {}
    if log.get("Type") not in (None, "", "none", "json-file", "local"):
        bad.append("LogConfig")
    if (hc.get("RestartPolicy") or {}).get("Name") not in (None, "", "no"):
        bad.append("RestartPolicy")
    for u in hc.get("Ulimits") or []:
        if not isinstance(u, dict) or u.get("Name") not in ("core", "nofile"):
            bad.append("Ulimits")
    return bad


TOP_KEYS = ("Hostname", "Domainname", "User", "AttachStdin", "AttachStdout", "AttachStderr", "Tty", "OpenStdin", "StdinOnce", "Env", "Cmd",
            "Image", "Volumes", "WorkingDir", "Entrypoint", "OnBuild", "Labels", "StopTimeout", "HostConfig", "NetworkingConfig")
HOSTCONFIG_KEYS = CHECKED | RESOURCE_KNOBS | {"Runtime"}
SCALAR = (int, float, str, bool, type(None))


def _strict(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object whose keys are unambiguous. The daemon's decoder matches field names case-insensitively and a later key overrides an
    earlier one, so `{"Image": a, "image": b}` means b to the daemon and a to a case-sensitive reader: refuse any object in which two keys
    are equal ignoring case (which includes exact duplicates)."""
    seen: set[str] = set()
    for key, _ in pairs:
        if key.casefold() in seen:
            raise ValueError("ambiguous key")
        seen.add(key.casefold())
    return dict(pairs)


def _no_constant(_: str) -> Any:
    raise ValueError("not JSON")


def _shape(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Default-deny on the structure: only the exact-case fields `docker run` sends, at every level. Returns the NEW document built from
    those fields alone (what is forwarded; the client's bytes never are) and the reasons to refuse."""
    bad: list[str] = []
    extra = sorted(set(doc) - set(TOP_KEYS))
    if extra:
        bad.append("unknown field " + ", ".join(k[:40] for k in extra))
    hc = doc.get("HostConfig")
    if not isinstance(hc, dict):
        return {}, bad + ["HostConfig"]
    extra = sorted(set(hc) - HOSTCONFIG_KEYS)
    if extra:
        bad.append("unknown HostConfig field " + ", ".join(k[:40] for k in extra))
    for key, value in hc.items():
        if key in RESOURCE_KNOBS and not (isinstance(value, SCALAR) or value == [] or value == {} or
                                          (key == "ConsoleSize" and isinstance(value, list) and len(value) == 2 and all(type(v) is int for v in value))):
            bad.append(f"HostConfig.{key}")
    log, restart = hc.get("LogConfig"), hc.get("RestartPolicy")
    if log is not None and (not isinstance(log, dict) or set(log) - {"Type", "Config"} or log.get("Config") not in (None, {})):
        bad.append("HostConfig.LogConfig")
    if restart is not None and (not isinstance(restart, dict) or set(restart) - {"Name", "MaximumRetryCount"}):
        bad.append("HostConfig.RestartPolicy")
    for u in hc.get("Ulimits") or []:
        if not isinstance(u, dict) or set(u) != {"Name", "Hard", "Soft"}:
            bad.append("HostConfig.Ulimits")
    net = doc.get("NetworkingConfig")
    if net is not None:
        eps = net.get("EndpointsConfig") if isinstance(net, dict) else None
        if not isinstance(net, dict) or set(net) - {"EndpointsConfig"} or not isinstance(eps, dict) or any(v not in ({}, None) for v in eps.values()):
            bad.append("NetworkingConfig")
    labels = doc.get("Labels")
    if isinstance(labels, dict) and set(labels) != {OWNER_LABEL, "memorylab.run"}:
        bad.append("Labels")
    clean = {k: doc[k] for k in TOP_KEYS if k in doc}
    clean["HostConfig"] = {k: hc[k] for k in sorted(hc) if k in HOSTCONFIG_KEYS}
    return clean, bad


def parse_create(body: bytes, name: str, policy: Policy) -> tuple[list[str], bytes]:
    """(reasons to refuse, the canonical body to forward). `name` is the `name` query parameter. Empty reasons = accept."""
    if len(body) > MAX_BODY:
        return ["body too large"], b""
    try:
        doc = json.loads(body, object_pairs_hook=_strict, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return ["body is not JSON, or has duplicate or case-variant keys"], b""
    if not isinstance(doc, dict):
        return ["body is not a container configuration"], b""
    clean, bad = _shape(doc)
    if not clean:
        return sorted(set(bad)), b""
    bad += _semantics(clean, name, policy)
    reasons = sorted(set(bad))
    return reasons, b"" if reasons else json.dumps(clean, separators=(",", ":"), sort_keys=True).encode()


def check_create(body: bytes, name: str, policy: Policy) -> list[str]:
    return parse_create(body, name, policy)[0]


def _semantics(doc: dict[str, Any], name: str, policy: Policy) -> list[str]:
    bad: list[str] = []
    if doc.get("Image") not in policy.images:
        bad.append("Image")
    labels = doc.get("Labels")
    if not isinstance(labels, dict) or labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get("memorylab.run") != name.removeprefix("memorylab-run-"):
        bad.append("Labels")
    if not isinstance(doc.get("User"), str) or not USER.fullmatch(doc["User"]):
        bad.append("User")                                              # never root
    if (not _empty(doc.get("Cmd")) or not _empty(doc.get("Entrypoint")) or not _empty(doc.get("WorkingDir"))
            or not _empty(doc.get("Volumes")) or not _empty(doc.get("OnBuild"))):
        bad.append("Cmd/Entrypoint/WorkingDir/Volumes/OnBuild")         # the image decides what runs (OnBuild: a Config field newer CLIs send as null; must stay empty)
    env = doc.get("Env")
    if env is not None and not isinstance(env, list):
        bad.append("Env")
    for entry in env or []:
        if not isinstance(entry, str) or entry.partition("=")[0] not in policy.env_names:
            bad.append("Env")
    for key in ("Hostname", "Domainname", "Tty"):
        if not _empty(doc.get(key)):
            bad.append(key)
    for key in ("AttachStdin", "AttachStdout", "AttachStderr", "OpenStdin", "StdinOnce"):
        if not isinstance(doc.get(key), bool):
            bad.append(key)
    stop = doc.get("StopTimeout")
    if stop is not None and (type(stop) is not int or not 0 <= stop <= 120):
        bad.append("StopTimeout")
    endpoints = (doc.get("NetworkingConfig") or {}).get("EndpointsConfig") or {}
    if set(endpoints) - {policy.network}:
        bad.append("NetworkingConfig")
    bad += _hostconfig(doc["HostConfig"], policy)
    return bad


def is_ours(doc: dict[str, Any] | None) -> bool | None:
    """Is this daemon container document one of our sandboxes? None = there is no such container. Decided from what the daemon reports
    (label and name), never from what the caller says."""
    if doc is None:
        return None
    labels = (doc.get("Config") or {}).get("Labels") or {}
    return bool(labels.get(OWNER_LABEL) == OWNER_VALUE and str(doc.get("Name", "")).lstrip("/").startswith("memorylab-run-"))
