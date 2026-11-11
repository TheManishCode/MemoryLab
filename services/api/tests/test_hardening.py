"""Malformed input is a 422/404 (never a 500), write routes are rate limited, the client IP is not spoofable."""

import hashlib
import uuid
from typing import Any

import pytest

from conftest import Env, bearer
from test_authz_matrix import seed
from test_runs_create import body

NUL = "a\x00b"


def runs_of(env: Env, uid: str) -> int:
    return int(env.admin.execute("select count(*) n from public.runs where owner_id = %s", (uid,)).fetchone()["n"])  # type: ignore[index]


# ---------------------------------------------------------------- caps and NUL bytes (round 1, fix 3)

@pytest.mark.parametrize("wall", [0.5, 1.5, 60.0, 0, 86_401, "60", True, None])
@pytest.mark.parametrize("route", ["/api/v1/runs", "/api/v1/runs/validate"])
def test_max_wall_s_must_be_an_integer_in_range_before_anything_is_used(env: Env, route: str, wall: Any) -> None:
    uid, game = env.user(), env.game()
    b = body(env, game)
    b["caps"]["max_wall_s"] = wall
    if route.endswith("validate"):
        for k in ("api_key", "game", "visibility"):
            del b[k]
    r = env.client.post(route, json=b, headers=bearer(uid))
    assert r.status_code == 422, r.text
    assert env.keys.calls == [] and runs_of(env, uid) == 0 and env.orch.submitted == []


@pytest.mark.parametrize("caps", [{"max_steps": 100_001}, {"max_tokens": 1_000_000_001}, {"max_steps": 1.5}])
def test_the_other_caps_have_the_same_bounds_as_the_database(env: Env, caps: dict[str, Any]) -> None:
    uid, game = env.user(), env.game()
    b = body(env, game)
    b["caps"].update(caps)
    assert env.client.post("/api/v1/runs", json=b, headers=bearer(uid)).status_code == 422
    assert env.keys.calls == [] and runs_of(env, uid) == 0


def test_the_largest_allowed_caps_are_accepted(env: Env) -> None:
    uid, game = env.user(), env.game()
    b = body(env, game, caps={"max_steps": 100_000, "max_tokens": 1_000_000_000, "max_wall_s": 86_400})
    assert env.client.post("/api/v1/runs", json=b, headers=bearer(uid)).status_code == 202


@pytest.mark.parametrize("cursor", [
    "2026-99-99T00:00|{id}",                             # the review's 500 (DatetimeFieldOverflow)
    "2026-99-99T00:00:00+00:00|{id}",
    "2026-11-08T00:00:00|{id}",                          # no timezone
    "2026-11-08T00:00:00+99:99|{id}",
    "2026-11-08T00:00:00+00:00|not-a-uuid",
    "x" * 300,
    "2026-11-08T00:00:00+00:00|{id}" + "0" * 300,
    "2026-11-08T00:00:00+00:00|" + "a" * 10_000,
    "garbage",
    "2026-11-08T00:00:00\x00|{id}",
])
def test_a_bad_cursor_is_a_422(env: Env, cursor: str) -> None:
    uid = env.user()
    r = env.client.get("/api/v1/runs", params={"before": cursor.format(id=uuid.uuid4())}, headers=bearer(uid))
    assert r.status_code == 422 and r.json()["code"] == "validation_failed"


def test_a_cursor_with_a_z_suffix_still_works(env: Env) -> None:
    uid = env.user()
    env.run(uid)
    r = env.client.get("/api/v1/runs", params={"before": f"2100-01-01T00:00:00Z|{uuid.uuid4()}"}, headers=bearer(uid))
    assert r.status_code == 200 and len(r.json()["runs"]) == 1


def _create_with(game: str, mutate: Any) -> Any:
    def build(env: Env) -> dict[str, Any]:
        b = body(env, game)
        mutate(b)
        return b
    return build


CREATE_FIELDS = {
    "game": lambda b: b.update(game=NUL),
    "preset_text": lambda b: b.update(preset_text=NUL),
    "api_key": lambda b: b.update(api_key="sk-ant-" + NUL + "-0123456789"),
    "config_value": lambda b: b["config"].update(model_id=NUL),
    "config_key": lambda b: b["config"].update({NUL: 1}),
    "config_nested": lambda b: b["config"].update(extra_thing={"k": [NUL]}),
}


@pytest.mark.parametrize("field", list(CREATE_FIELDS))
def test_a_nul_byte_in_any_create_field_is_a_422_before_quota_and_key_validation(env: Env, field: str) -> None:
    uid, game = env.user(), env.game()
    b = body(env, game)
    CREATE_FIELDS[field](b)
    r = env.client.post("/api/v1/runs", json=b, headers=bearer(uid))
    assert r.status_code == 422, r.text
    assert env.keys.calls == [] and runs_of(env, uid) == 0 and env.orch.submitted == []
    assert env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid)).status_code == 202   # no quota used


def test_a_nul_byte_in_validate_patch_report_and_admin_bodies_is_a_422(env: Env) -> None:
    owner, other, admin = env.user(), env.user(), env.user("admin")
    run = env.run(owner, "public")
    v = body(env, "x")
    for k in ("api_key", "game", "visibility"):
        del v[k]
    v["preset_text"] = NUL
    assert env.client.post("/api/v1/runs/validate", json=v, headers=bearer(owner)).status_code == 422
    assert env.client.patch(f"/api/v1/runs/{run}", json={"title": NUL}, headers=bearer(owner)).status_code == 422
    assert env.client.post(f"/api/v1/runs/{run}/report", json={"reason": NUL}, headers=bearer(other)).status_code == 422
    assert env.client.post(f"/api/v1/admin/runs/{run}/kill", json={"reason": NUL}, headers=bearer(admin)).status_code == 422
    assert env.client.post("/api/v1/admin/breaker", json={"open": True, "reason": NUL},
                           headers=bearer(admin)).status_code == 422
    for signup in ({"email": f"a{NUL}@example.test", "password": "correct horse battery"},
                   {"email": "a@example.test", "password": "correct horse" + NUL + "battery"}):
        assert env.client.post("/api/v1/auth/signup", json=signup).status_code == 422
    assert env.gateway.signups == [] and env.orch.killed == [] and env.orch.breaker_calls == []
    assert env.admin.execute("select count(*) n from public.reports").fetchone()["n"] == 0        # type: ignore[index]


@pytest.mark.parametrize("route", ["memory/file", "memory/diff"])
def test_a_nul_byte_in_the_memory_path_is_a_404(env: Env, route: str) -> None:
    ids = seed(env, "public")
    params = {"path": NUL} if route == "memory/file" else {"path": NUL, "from": 1, "to": 2}
    r = env.client.get(f"/api/v1/runs/{ids['run']}/{route}", params=params)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_a_nul_byte_in_a_path_id_is_a_404(env: Env) -> None:
    assert env.client.get("/api/v1/runs/a%00b").status_code == 404
    assert env.client.get("/api/v1/runs/a%00b/memory/file?path=x").status_code == 404


# ---------------------------------------------------------------- write limits (round 1, fix 4)

def test_the_eleventh_report_in_an_hour_is_429_and_a_repeat_is_not_a_new_report(env: Env) -> None:
    owner, reporter = env.user(), env.user()
    runs = [env.run(owner, "public") for _ in range(11)]
    codes = [env.client.post(f"/api/v1/runs/{r}/report", json={"reason": "spam"}, headers=bearer(reporter)).status_code
             for r in runs]
    assert codes == [202] * 10 + [429]
    again = env.client.post(f"/api/v1/runs/{runs[0]}/report", json={"reason": "again"}, headers=bearer(reporter))
    assert again.status_code == 202                                           # duplicate while open: same report
    n = env.admin.execute("select count(*) n from public.reports where reporter_id = %s", (reporter,)).fetchone()["n"]  # type: ignore[index]
    assert n == 10


def test_a_duplicate_open_report_adds_no_row(env: Env) -> None:
    owner, reporter = env.user(), env.user()
    run = env.run(owner, "public")
    for reason in ("one", "two", "three"):
        assert env.client.post(f"/api/v1/runs/{run}/report", json={"reason": reason},
                               headers=bearer(reporter)).status_code == 202
    assert env.admin.execute("select count(*) n from public.reports where run_id = %s", (run,)).fetchone()["n"] == 1  # type: ignore[index]
    admin = env.user("admin")
    env.admin.execute("update public.reports set status = 'dismissed', resolved_at = now(), resolved_by = %s, "
                      "resolution_reason = 'ok' where run_id = %s", (admin, run))
    env.client.post(f"/api/v1/runs/{run}/report", json={"reason": "new"}, headers=bearer(reporter))
    assert env.admin.execute("select count(*) n from public.reports where run_id = %s", (run,)).fetchone()["n"] == 2  # type: ignore[index]


def test_an_owner_cannot_report_their_own_run(env: Env) -> None:
    owner = env.user()
    run = env.run(owner, "public")
    r = env.client.post(f"/api/v1/runs/{run}/report", json={"reason": "me"}, headers=bearer(owner))
    assert r.status_code == 422
    assert env.admin.execute("select count(*) n from public.reports").fetchone()["n"] == 0        # type: ignore[index]


@pytest.mark.parametrize("method,path,payload", [
    ("PATCH", "/api/v1/runs/{run}", {"title": "t"}),
    ("POST", "/api/v1/runs/{run}/stop", None),
    ("DELETE", "/api/v1/runs/{run}", None),
    ("POST", "/api/v1/runs/{run}/report", {"reason": "x"}),
])
def test_write_routes_are_limited_per_user(make_env: Any, method: str, path: str, payload: Any) -> None:
    env: Env = make_env(write_limit_per_min=3, read_limit_per_min=1000)
    owner, other = env.user(), env.user()
    caller = other if path.endswith("report") else owner
    codes = []
    for _ in range(5):
        run = env.run(owner, "public")
        codes.append(env.client.request(method, path.format(run=run), json=payload, headers=bearer(caller)).status_code)
    assert 429 not in codes[:3] and codes[3:] == [429, 429], codes
    third = env.user()                                                       # another user has their own bucket
    assert env.client.request(method, path.format(run=env.run(owner, "public")), json=payload,
                              headers=bearer(third)).status_code != 429


def test_the_write_limit_applies_before_the_ownership_lookup(make_env: Any) -> None:
    env: Env = make_env(write_limit_per_min=2)
    uid = env.user()
    missing = f"/api/v1/runs/{uuid.uuid4()}"
    codes = [env.client.patch(missing, json={"title": "t"}, headers=bearer(uid)).status_code for _ in range(4)]
    assert codes == [404, 404, 429, 429]


# ---------------------------------------------------------------- client IP (round 1, fix 6)

def iph(ip: str) -> str:
    return hashlib.sha256(b"test-salt" + ip.encode()).hexdigest()[:32]


def submitted_ip(env: Env, xff: list[str] | None) -> str:
    uid, game = env.user(), env.game()
    headers = bearer(uid)
    if xff is not None:
        headers["x-forwarded-for"] = ", ".join(xff)
    assert env.client.post("/api/v1/runs", json=body(env, game), headers=headers).status_code == 202
    return str(env.orch.submitted[-1]["ip_hash"])


def test_x_forwarded_for_is_ignored_when_no_proxy_is_trusted(env: Env) -> None:
    assert submitted_ip(env, ["9.9.9.9"]) == iph("testclient")


@pytest.mark.parametrize("hops,xff,expected", [
    (1, ["1.2.3.4, 5.6.7.8"], "5.6.7.8"),                    # the entry our one proxy appended; the left is the client's claim
    (2, ["1.2.3.4, 5.6.7.8"], "1.2.3.4"),
    (1, ["1.2.3.4", "5.6.7.8"], "5.6.7.8"),                  # several header lines count as one list
    (1, None, "testclient"),                                  # no header: use the socket peer
    (2, ["5.6.7.8"], "testclient"),                           # fewer entries than proxies: not from our gateway
    (1, ["1.2.3.4, evil"], "testclient"),                     # garbage is not an address
    (1, ["::1"], "::1"),
])
def test_the_trusted_proxy_hop_picks_the_client_address(make_env: Any, hops: int, xff: list[str] | None,
                                                       expected: str) -> None:
    env: Env = make_env(trusted_proxy_hops=hops)
    assert submitted_ip(env, xff) == iph(expected)


def test_anonymous_reads_get_one_bucket_per_forwarded_client(make_env: Any) -> None:
    env: Env = make_env(trusted_proxy_hops=1, read_limit_per_min=2)
    a, b = {"x-forwarded-for": "198.51.100.1"}, {"x-forwarded-for": "198.51.100.2"}
    assert [env.client.get("/api/v1/games", headers=a).status_code for _ in range(3)] == [200, 200, 429]
    assert env.client.get("/api/v1/games", headers=b).status_code == 200


def test_spoofing_the_header_does_not_dodge_a_limit_when_no_proxy_is_trusted(make_env: Any) -> None:
    env: Env = make_env(read_limit_per_min=2)
    codes = [env.client.get("/api/v1/games", headers={"x-forwarded-for": f"198.51.100.{i}"}).status_code
             for i in range(4)]
    assert codes == [200, 200, 429, 429]


# ---------------------------------------------------------------- tests added for review round 1b

def test_key_failure_lockout_is_what_refuses_the_sixth_attempt(make_env: Any) -> None:
    env: Env = make_env(run_create_per_user_per_hour=100, run_create_per_ip_per_hour=100)
    uid, game = env.user(), env.game()
    for _ in range(5):
        r = env.client.post("/api/v1/runs", json=body(env, game, api_key="sk-ant-wrong-key-123456"), headers=bearer(uid))
        assert r.status_code == 422
    r = env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid))
    assert r.status_code == 429 and r.json()["title"] == "Run creation is locked for an hour"
    assert len(env.keys.calls) == 5                                          # the good key was never even tried


def test_four_key_failures_then_a_good_key_is_accepted(make_env: Any) -> None:
    env: Env = make_env(run_create_per_user_per_hour=100, run_create_per_ip_per_hour=100)
    uid, game = env.user(), env.game()
    for _ in range(4):
        env.client.post("/api/v1/runs", json=body(env, game, api_key="sk-ant-wrong-key-123456"), headers=bearer(uid))
    assert env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid)).status_code == 202


def test_leaderboard_has_only_verified_public_runs_for_anon_and_for_the_owner(env: Env) -> None:
    a = env.user()
    ok = env.run(a, "public")
    others = {name: env.run(a, vis) for name, vis in (("unlisted", "unlisted"), ("private", "private"),
                                                      ("unverified", "public"))}
    for run, verified in [(ok, True), (others["unlisted"], True), (others["private"], True),
                          (others["unverified"], False)]:
        env.admin.execute("insert into public.run_scores values (%s, 5, 1, 1, 1, 1, 0, 0, %s)", (run, verified))
    for headers in ({}, bearer(a)):                  # the owner's RLS also shows their private and unlisted runs
        ids = {r["id"] for r in env.client.get("/api/v1/leaderboard", headers=headers).json()}
        assert ok in ids and not ids & set(others.values())


def test_a_deleted_memory_file_is_gone_from_the_tree_and_the_default_read_but_old_versions_stay(env: Env) -> None:
    ids = seed(env, "public")
    run = ids["run"]
    env.admin.execute("insert into public.memory_files values (%s, 'a.md', 3, 2, '', 0, true)", (run,))
    assert env.client.get(f"/api/v1/runs/{run}/memory/file", params={"path": "a.md"}).status_code == 404
    assert env.client.get(f"/api/v1/runs/{run}/memory").json() == []
    r = env.client.get(f"/api/v1/runs/{run}/memory/file", params={"path": "a.md", "version": 2})
    assert r.status_code == 200 and r.text == "one\ntwo\n"
    assert env.client.get(f"/api/v1/runs/{run}/memory/file", params={"path": "a.md", "version": 3}).status_code == 404
    before = env.client.get(f"/api/v1/runs/{run}/memory", params={"at_step": 1}).json()
    assert [(f["path"], f["version"]) for f in before] == [("a.md", 2)]


# ---------------------------------------------------------------- login role and required issuer (round 1, fix 7)

def test_the_default_database_role_is_not_a_superuser_and_does_not_bypass_rls(admin_conn: object) -> None:
    # admin_conn gates on the database being reachable (skip when down, fail under MEMORYLAB_REQUIRE_DB),
    # the same contract the rest of the suite uses; this test then connects as the API role itself.
    from api.db import Database
    from api.settings import Settings
    assert Settings.db_url.startswith("postgresql://memorylab_api:")
    db = Database(Settings.db_url, max_size=1)
    try:
        with db.pool.connection() as conn:                     # a pool connection before apply_identity
            row = conn.execute("select rolbypassrls, rolsuper, rolinherit from pg_roles "
                               "where rolname = current_user").fetchone()
            assert row == {"rolbypassrls": False, "rolsuper": False, "rolinherit": False}
    finally:
        db.close()


def test_a_connection_that_skips_apply_identity_can_read_nothing(admin_conn: object) -> None:
    # admin_conn gates on the database being reachable (see the companion test above).
    import psycopg

    from api.db import Database
    from api.settings import Settings
    db = Database(Settings.db_url, max_size=1)
    try:
        with db.pool.connection() as conn:
            for table in ("runs", "run_configs", "profiles", "reports"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(f"select 1 from public.{table}")
                conn.rollback()
    finally:
        db.close()


def test_the_jwt_issuer_is_required_in_production_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.settings import Settings
    monkeypatch.setenv("MEMORYLAB_JWKS_URL", "http://jwks.invalid")
    monkeypatch.delenv("MEMORYLAB_JWT_ISSUER", raising=False)
    with pytest.raises(KeyError, match="MEMORYLAB_JWT_ISSUER"):
        Settings.from_env()
    monkeypatch.setenv("MEMORYLAB_JWT_ISSUER", "https://auth.example/auth/v1")
    monkeypatch.delenv("MEMORYLAB_LOG_PEPPER", raising=False)
    with pytest.raises(KeyError, match="MEMORYLAB_LOG_PEPPER"):
        Settings.from_env()
    monkeypatch.setenv("MEMORYLAB_LOG_PEPPER", "short")
    with pytest.raises(KeyError, match="MEMORYLAB_LOG_PEPPER"):
        Settings.from_env()
    monkeypatch.setenv("MEMORYLAB_LOG_PEPPER", "p" * 24)
    assert Settings.from_env().issuer == "https://auth.example/auth/v1"
