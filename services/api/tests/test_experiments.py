"""Experiments: ownership of runs (DB trigger and API), one declared variable, per-viewer visibility of each run."""

import json
import uuid
from typing import Any

import psycopg
import pytest
from runlog.eventlog import EventLogWriter

from conftest import Env, bearer
from test_fork import source

PRESETS = ["general@1", "none@1", "failures_only@1", "read_first_test_theories@1"]


def arm(env: Env, owner: str, game: str, preset: str = "general@1", visibility: str = "private", score: int | None = None,
        **kw: Any) -> str:
    run, _ = source(env, owner, game, visibility, cfg={"preset_ref": preset}, with_checkpoint=False, **kw)
    if score is not None:
        env.admin.execute("insert into public.run_scores values (%s, %s, 1, 1, 1, 1, 0, 0, true)", (run, score))
    return run


def make(env: Env, owner: str, run_ids: list[str], **over: Any) -> Any:
    body = {"title": "arms", "variable": "preset_ref", "run_ids": run_ids, "visibility": "private", **over}
    return env.client.post("/api/v1/experiments", json=body, headers=bearer(owner))


def post_ok(env: Env, owner: str, run_ids: list[str], **over: Any) -> str:
    r = make(env, owner, run_ids, **over)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def get(env: Env, who: str | None, eid: str, route: str = "") -> Any:
    return env.client.get(f"/api/v1/experiments/{eid}{route}", headers=bearer(who) if who else {})


# ---------------------------------------------------------------- creating

def test_an_experiment_groups_the_owners_runs_and_summarises_the_scores(env: Env) -> None:
    owner, game = env.user(), env.game()
    runs = [arm(env, owner, game, p, score=s) for p, s in zip(PRESETS, (10, 20, 30, 40), strict=True)]
    eid = post_ok(env, owner, runs, visibility="public")
    body = get(env, owner, eid).json()
    assert [r["run_id"] for r in body["runs"]] == runs                                  # the experiment's order
    assert (body["title"], body["variable"], body["visibility"], body["owner_id"]) == ("arms", "preset_ref", "public", owner)
    s = body["summary"]
    assert (s["n"], s["mean"], s["min"], s["max"]) == (4, 25.0, 10.0, 40.0) and s["stdev"] == pytest.approx(12.909944, abs=1e-5)
    assert s["warnings"] == ["n_below_5"] and s["runs"] == 4 and s["runs_in_progress"] == 0


def test_repeats_of_one_configuration_are_a_valid_experiment(env: Env) -> None:
    owner, game = env.user(), env.game()
    runs = [arm(env, owner, game, "general@1", score=i) for i in range(5)]
    body = get(env, owner, post_ok(env, owner, runs)).json()
    assert body["summary"]["n"] == 5 and body["summary"]["warnings"] == []


def test_two_arms_get_per_arm_stats_and_a_significant_ab_verdict(env: Env) -> None:
    owner, game = env.user(), env.game()
    a = [arm(env, owner, game, "general@1", score=0) for _ in range(5)]   # one arm, all 0
    b = [arm(env, owner, game, "none@1", score=5) for _ in range(5)]      # the other, all 5
    s = get(env, owner, post_ok(env, owner, a + b)).json()["summary"]
    assert s["median"] == 2.5 and isinstance(s["ci95"], list) and len(s["ci95"]) == 2   # overall gains a CI
    arms = {x["label"]: x for x in s["arms"]}
    assert set(arms) == {"general@1", "none@1"}
    assert (arms["general@1"]["n"], arms["general@1"]["mean"]) == (5, 0.0)
    assert (arms["none@1"]["n"], arms["none@1"]["mean"]) == (5, 5.0)
    assert isinstance(arms["general@1"]["ci95"], list)
    c = s["comparison"]
    assert c is not None
    assert (c["a_label"], c["b_label"], c["diff_means"]) == ("general@1", "none@1", 5.0)   # sorted: general < none
    assert c["exact"] is True and c["significant"] is True and c["underpowered"] is False


def test_an_underpowered_two_arm_comparison_is_flagged_not_significant(env: Env) -> None:
    owner, game = env.user(), env.game()
    a = [arm(env, owner, game, "general@1", score=0) for _ in range(2)]
    b = [arm(env, owner, game, "none@1", score=2) for _ in range(2)]
    c = get(env, owner, post_ok(env, owner, a + b)).json()["summary"]["comparison"]
    assert c is not None
    assert c["diff_means"] == 2.0 and c["significant"] is False and c["underpowered"] is True


def test_runs_still_running_and_unscored_runs_are_flagged_not_counted(env: Env) -> None:
    owner, game = env.user(), env.game()
    done, running = arm(env, owner, game, "general@1", score=7), arm(env, owner, game, "none@1", status="running")
    s = get(env, owner, post_ok(env, owner, [done, running])).json()["summary"]
    assert (s["n"], s["runs"], s["runs_in_progress"]) == (1, 2, 1) and "runs_still_running" in s["warnings"]


def test_run_ids_must_be_the_callers_own_runs(env: Env) -> None:
    owner, other, game = env.user(), env.user(), env.game()
    mine, theirs, public = arm(env, owner, game), arm(env, other, game, "none@1"), arm(env, other, game, "none@1", "public")
    results = [make(env, owner, ids) for ids in ([mine, theirs], [mine, public], [mine, str(uuid.uuid4())])]
    assert [r.status_code for r in results] == [422, 422, 422]
    assert len({json.dumps(r.json(), sort_keys=True) for r in results}) == 1            # missing, private, public: one answer
    assert results[0].json()["code"] == "invalid_runs"
    gone = arm(env, owner, game, "none@1")
    env.admin.execute("update public.runs set deleted_at = now() where id = %s", (gone,))
    assert make(env, owner, [mine, gone]).status_code == 422
    assert env.admin.execute("select count(*) n from public.experiments where owner_id = %s", (owner,)).fetchone()["n"] == 0  # type: ignore[index]


@pytest.mark.parametrize("override,code", [
    ({"run_ids": []}, "validation_failed"), ({"run_ids": ["not-a-uuid"]}, "invalid_runs"),
    ({"variable": "banana"}, "validation_failed"), ({"variable": ""}, "validation_failed"),
    ({"title": ""}, "validation_failed"), ({"title": "x" * 121}, "validation_failed"),
    ({"title": "a\x00b"}, "validation_failed"), ({"variable": "preset_ref\x00"}, "validation_failed"),
    ({"visibility": "everyone"}, "validation_failed"), ({"extra": 1}, "validation_failed"),
    ({"owner_id": "x"}, "validation_failed"),
])
def test_bad_experiment_bodies_are_422(env: Env, override: dict[str, Any], code: str) -> None:
    owner, game = env.user(), env.game()
    a, b = arm(env, owner, game), arm(env, owner, game, "none@1")
    body = {"title": "arms", "variable": "preset_ref", "run_ids": [a, b], "visibility": "private", **override}
    r = env.client.post("/api/v1/experiments", json=body, headers=bearer(owner))
    assert (r.status_code, r.json()["code"]) == (422, code), r.text


def test_more_than_fifty_runs_or_a_duplicate_is_refused(env: Env) -> None:
    owner, game = env.user(), env.game()
    a = arm(env, owner, game)
    assert make(env, owner, [str(uuid.uuid4()) for _ in range(51)]).status_code == 422
    assert make(env, owner, [a, a]).status_code == 422
    assert env.admin.execute("select count(*) n from public.experiments where owner_id = %s", (owner,)).fetchone()["n"] == 0  # type: ignore[index]


def test_the_runs_must_differ_only_in_the_declared_variable(env: Env) -> None:
    owner, game = env.user(), env.game()
    plain, other_preset = arm(env, owner, game, "general@1"), arm(env, owner, game, "none@1")
    other_model, _ = source(env, owner, game, cfg={"preset_ref": "none@1", "model_id": "claude-opus-5-5"}, with_checkpoint=False)
    assert make(env, owner, [plain, other_preset]).status_code == 201
    r = make(env, owner, [plain, other_model])                                           # preset AND model differ
    assert (r.status_code, r.json()["code"]) == (422, "variable_mismatch") and "model_id" in r.json()["title"]
    assert make(env, owner, [plain, other_model], variable="model_id").json()["code"] == "variable_mismatch"   # preset still differs
    assert make(env, owner, [plain, other_preset], variable="model_id").json()["code"] == "variable_mismatch"
    caps_a, _ = source(env, owner, game, cfg={"preset_ref": "general@1"}, caps={"max_steps": 99, "max_tokens": 1000, "max_wall_s": 60},
                       with_checkpoint=False)
    assert make(env, owner, [plain, caps_a]).json()["code"] == "variable_mismatch"      # caps are a field too
    assert make(env, owner, [plain, caps_a], variable="caps").status_code == 201


def test_defaults_are_not_mistaken_for_differences(env: Env) -> None:
    """A config that spells a default out equals one that omits it."""
    owner, game = env.user(), env.game()
    explicit, _ = source(env, owner, game, cfg={"preset_ref": "general@1", "inject_path": "global.md"}, with_checkpoint=False)
    omitted = arm(env, owner, game, "none@1")
    assert make(env, owner, [explicit, omitted]).status_code == 201


def test_the_account_must_be_live_and_experiments_are_capped(env: Env) -> None:
    owner, game = env.user(), env.game()
    a = arm(env, owner, game)
    for i in range(100):
        env.admin.execute("insert into public.experiments (owner_id, title, variable) values (%s, %s, 'preset_ref')", (owner, f"e{i}"))
    r = make(env, owner, [a])
    assert (r.status_code, r.json()["code"]) == (409, "limit_reached")
    assert make(env, env.user(), [a]).status_code == 422                                  # another user can't use my run


# ---------------------------------------------------------------- the database enforces ownership for every writer

def test_the_trigger_refuses_foreign_duplicate_and_deleted_runs_even_for_the_service_role(env: Env) -> None:
    owner, other, game = env.user(), env.user(), env.game()
    mine, theirs, gone = arm(env, owner, game), arm(env, other, game), arm(env, owner, game, "none@1")
    env.admin.execute("update public.runs set deleted_at = now() where id = %s", (gone,))

    def insert(run_ids: list[str]) -> None:
        env.admin.execute("insert into public.experiments (owner_id, title, variable, run_ids) values (%s, 't', 'preset_ref', %s::uuid[])",
                          (owner, run_ids))

    for ids, err in (([mine, theirs], "experiment_run_not_owned"), ([gone], "experiment_run_not_owned"),
                     ([str(uuid.uuid4())], "experiment_run_not_owned"), ([mine, mine], "experiment_duplicate_runs")):
        with pytest.raises(psycopg.errors.RaiseException, match=err):
            insert(ids)
    insert([mine])
    eid = env.admin.execute("select id from public.experiments where owner_id = %s", (owner,)).fetchone()["id"]  # type: ignore[index]
    with pytest.raises(psycopg.errors.RaiseException, match="experiment_run_not_owned"):
        env.admin.execute("update public.experiments set run_ids = run_ids || %s::uuid[] where id = %s", ([theirs], eid))
    with pytest.raises(psycopg.errors.RaiseException, match="experiment_owner_immutable"):
        env.admin.execute("update public.experiments set owner_id = %s where id = %s", (other, eid))
    env.admin.execute("update public.runs set deleted_at = now() where id = %s", (mine,))     # a later delete is allowed
    env.admin.execute("update public.experiments set title = 'renamed' where id = %s", (eid,))   # and does not block edits


def test_users_cannot_write_experiments_directly_so_only_the_functions_remain(env: Env) -> None:
    from conftest import API_DB_URL
    owner, other, game = env.user(), env.user(), env.game()
    theirs = arm(env, other, game)
    with psycopg.connect(API_DB_URL) as c:
        c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": owner, "role": "authenticated"}),))
        c.execute("set local role authenticated")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("insert into public.experiments (owner_id, title, variable, run_ids) values (%s, 't', 'v', %s::uuid[])",
                      (owner, [theirs]))
    with psycopg.connect(API_DB_URL) as c:
        c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": owner, "role": "authenticated"}),))
        c.execute("set local role authenticated")
        with pytest.raises(psycopg.errors.RaiseException, match="experiment_run_not_owned"):
            c.execute("select api_private.create_experiment('t', 'preset_ref', %s::uuid[], 'private')", ([theirs],))


# ---------------------------------------------------------------- visibility

@pytest.mark.parametrize("visibility", ["private", "unlisted", "public"])
@pytest.mark.parametrize("who", ["anon", "owner", "other", "admin"])
def test_experiments_follow_their_visibility_with_the_same_404(env: Env, who: str, visibility: str) -> None:
    owner, other, admin, game = env.user(), env.user(), env.user("admin"), env.game()
    eid = post_ok(env, owner, [arm(env, owner, game, "general@1", "public"), arm(env, owner, game, "none@1", "public")],
                  visibility=visibility)
    ids = {"owner": owner, "other": other, "admin": admin}
    viewer = None if who == "anon" else ids[who]
    r = get(env, viewer, eid)
    if visibility != "private" or who in ("owner", "admin"):
        assert r.status_code == 200
        assert get(env, viewer, eid, "/compare").status_code == 200
    else:
        missing = get(env, viewer, str(uuid.uuid4()))
        assert (r.status_code, r.json()) == (missing.status_code, missing.json())
        assert get(env, viewer, eid, "/compare").status_code == 404


def test_a_private_run_inside_a_public_experiment_does_not_leak(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, viewer, admin, game = env.user(), env.user(), env.user("admin"), env.game()
    public = arm(env, owner, game, "general@1", "public", score=50)
    private = arm(env, owner, game, "none@1", "private", score=987654)
    unlisted = arm(env, owner, game, "failures_only@1", "unlisted", score=765432)
    for run in (public, private, unlisted):
        w = EventLogWriter(env.store, run)
        w.append([("run.state", 0, {"state": "completed"})])
        w.append([("obs", 3, {"score": 123})])
    eid = post_ok(env, owner, [public, private, unlisted], visibility="public")
    read: list[str] = []
    import api.app as app_mod
    real_reader = app_mod.EventLogReader

    class Spy(real_reader):  # type: ignore[valid-type, misc]
        def __init__(self, store: Any, run_id: str) -> None:
            read.append(run_id)
            super().__init__(store, run_id)

    monkeypatch.setattr(app_mod, "EventLogReader", Spy)
    for who in (None, viewer):
        body = get(env, who, eid).json()
        assert [r["run_id"] for r in body["runs"]] == [public]                          # only what that viewer may list
        assert (body["summary"]["n"], body["summary"]["mean"], body["summary"]["runs"]) == (1, 50.0, 1)
        text = json.dumps(body)
        assert private not in text and unlisted not in text and "987654" not in text and "765432" not in text
        cmp_body = get(env, who, eid, "/compare").json()
        assert [s["run_id"] for s in cmp_body["series"]] == [public]
        assert private not in json.dumps(cmp_body)
    assert private not in read and unlisted not in read                                 # their logs were never even opened
    for who in (owner, admin):
        assert {r["run_id"] for r in get(env, who, eid).json()["runs"]} == {public, private, unlisted}
    assert get(env, owner, eid).json()["summary"]["n"] == 3


def test_a_run_made_private_after_the_experiment_was_made_disappears_for_others(env: Env) -> None:
    owner, viewer, game = env.user(), env.user(), env.game()
    a, b = arm(env, owner, game, "general@1", "public"), arm(env, owner, game, "none@1", "public")
    eid = post_ok(env, owner, [a, b], visibility="public")
    assert len(get(env, viewer, eid).json()["runs"]) == 2
    env.client.patch(f"/api/v1/runs/{b}", json={"visibility": "private"}, headers=bearer(owner))
    assert [r["run_id"] for r in get(env, viewer, eid).json()["runs"]] == [a]
    env.admin.execute("update public.runs set deleted_at = now() where id = %s", (a,))
    assert get(env, viewer, eid).json()["runs"] == [] and [r["run_id"] for r in get(env, owner, eid).json()["runs"]] == [b]


def test_anon_cannot_list_unlisted_or_private_experiments_through_the_data_api(env: Env) -> None:
    owner, game = env.user(), env.game()
    runs = [arm(env, owner, game, "general@1"), arm(env, owner, game, "none@1")]
    ids = {v: post_ok(env, owner, runs, visibility=v, title=v) for v in ("private", "unlisted", "public")}
    env.admin.execute("select 1")
    with psycopg.connect(__import__("conftest").API_DB_URL) as c:
        for claims, role in (({"role": "anon"}, "anon"), ({"sub": env.user(), "role": "authenticated"}, "authenticated")):
            c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
            c.execute(f"set local role {role}")
            listed = {str(r[0]) for r in c.execute("select id from public.experiments").fetchall()}
            assert ids["public"] in listed and ids["unlisted"] not in listed and ids["private"] not in listed
            c.execute("reset role")
    assert get(env, None, ids["unlisted"]).status_code == 200                           # by link it is served


def test_my_experiments_list_is_mine_only_newest_first(env: Env) -> None:
    owner, other, game = env.user(), env.user(), env.game()
    runs = [arm(env, owner, game, "general@1"), arm(env, owner, game, "none@1")]
    first, second = post_ok(env, owner, runs, title="first"), post_ok(env, owner, runs, title="second")
    post_ok(env, other, [arm(env, other, game)], title="theirs")
    rows = env.client.get("/api/v1/experiments", headers=bearer(owner)).json()
    assert [r["id"] for r in rows] == [second, first] and rows[0]["n_runs"] == 2
    assert env.client.get("/api/v1/experiments").status_code == 401


# ---------------------------------------------------------------- compare

def test_compare_aligns_series_on_one_step_axis(env: Env) -> None:
    owner, game = env.user(), env.game()
    a, b = arm(env, owner, game, "general@1"), arm(env, owner, game, "none@1")
    for run, points in ((a, {0: 1, 2: 3, 4: 5}), (b, {1: 10, 3: 30})):
        w = EventLogWriter(env.store, run)
        for step, score in points.items():
            w.append([("obs", step, {"score": score}), ("model.call", step + 10, {"tokens_total": score * 100})])
    eid = post_ok(env, owner, [a, b])
    body = get(env, owner, eid, "/compare").json()
    assert body["metric"] == "score" and body["steps"] == [0, 1, 2, 3, 4]
    assert {s["run_id"]: s["values"] for s in body["series"]} == {a: [1, 1, 3, 3, 5], b: [None, 10, 10, 30, 30]}
    tokens = get(env, owner, eid, "/compare?metric=tokens").json()                  # model.call steps are 10 later
    assert tokens["steps"] == [10, 11, 12, 13, 14]
    assert {s["run_id"]: s["values"] for s in tokens["series"]} == {a: [100, 100, 300, 300, 500], b: [None, 1000, 1000, 3000, 3000]}


def test_compare_with_runs_that_have_no_events_is_empty_not_an_error(env: Env) -> None:
    owner, game = env.user(), env.game()
    eid = post_ok(env, owner, [arm(env, owner, game), arm(env, owner, game, "none@1")])
    body = get(env, owner, eid, "/compare").json()
    assert body["steps"] == [] and [s["values"] for s in body["series"]] == [[], []]


@pytest.mark.parametrize("metric", ["", "nope", "SCORE", "score;drop", "a\x00b"])
def test_an_unknown_metric_is_a_422(env: Env, metric: str) -> None:
    owner, game = env.user(), env.game()
    eid = post_ok(env, owner, [arm(env, owner, game), arm(env, owner, game, "none@1")])
    assert env.client.get(f"/api/v1/experiments/{eid}/compare", params={"metric": metric}, headers=bearer(owner)).status_code == 422


def test_compare_is_rate_limited_and_the_axis_is_bounded(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import api.app as app_mod
    monkeypatch.setattr(app_mod, "MAX_COMPARE_STEPS", 4)
    owner, game = env.user(), env.game()
    a, b = arm(env, owner, game), arm(env, owner, game, "none@1")
    w = EventLogWriter(env.store, a)
    for step in range(10):
        w.append([("obs", step, {"score": step})])
    eid = post_ok(env, owner, [a, b])
    body = get(env, owner, eid, "/compare").json()
    assert len(body["steps"]) <= 4 and all(len(s["values"]) == len(body["steps"]) for s in body["series"])
    codes = [get(env, owner, eid, "/compare").status_code for _ in range(11)]
    assert codes[:9] == [200] * 9 and codes[9:] == [429, 429]


# ---------------------------------------------------------------- patch

def patch(env: Env, who: str, eid: str, payload: dict[str, Any]) -> Any:
    return env.client.patch(f"/api/v1/experiments/{eid}", json=payload, headers=bearer(who))


def test_the_owner_edits_title_visibility_and_the_run_set(env: Env) -> None:
    owner, game = env.user(), env.game()
    a, b, c = (arm(env, owner, game, p) for p in PRESETS[:3])
    eid = post_ok(env, owner, [a, b])
    r = patch(env, owner, eid, {"title": "renamed", "visibility": "unlisted", "add_run_ids": [c, a]})   # a is already in
    assert r.status_code == 200
    body = r.json()
    assert (body["title"], body["visibility"]) == ("renamed", "unlisted") and [x["run_id"] for x in body["runs"]] == [a, b, c]
    body = patch(env, owner, eid, {"remove_run_ids": [a, str(uuid.uuid4())]}).json()               # unknown ids are ignored
    assert [x["run_id"] for x in body["runs"]] == [b, c]
    assert patch(env, owner, eid, {}).status_code == 422


def test_only_the_owner_can_patch_and_a_stranger_gets_the_missing_answer(env: Env) -> None:
    owner, other, admin, game = env.user(), env.user(), env.user("admin"), env.game()
    eid = post_ok(env, owner, [arm(env, owner, game), arm(env, owner, game, "none@1")], visibility="public")
    missing = patch(env, other, str(uuid.uuid4()), {"title": "x"})
    for who in (other, admin):
        r = patch(env, who, eid, {"title": "pwned"})
        assert (r.status_code, r.json()) == (missing.status_code, missing.json()) == (404, missing.json())
    assert get(env, owner, eid).json()["title"] == "arms"


def test_a_patch_that_adds_foreign_runs_or_breaks_the_variable_changes_nothing(env: Env) -> None:
    owner, other, game = env.user(), env.user(), env.game()
    a, b = arm(env, owner, game, "general@1"), arm(env, owner, game, "none@1")
    foreign = arm(env, other, game, "failures_only@1")
    differs, _ = source(env, owner, game, cfg={"preset_ref": "failures_only@1", "model_id": "claude-opus-5-5"}, with_checkpoint=False)
    eid = post_ok(env, owner, [a, b], title="original")
    for payload, code in (({"title": "changed", "add_run_ids": [foreign]}, "invalid_runs"),
                          ({"title": "changed", "add_run_ids": [differs]}, "variable_mismatch"),
                          ({"title": "changed", "add_run_ids": [str(uuid.uuid4())]}, "invalid_runs")):
        r = patch(env, owner, eid, payload)
        assert (r.status_code, r.json()["code"]) == (422, code)
    body = get(env, owner, eid).json()
    assert (body["title"], [x["run_id"] for x in body["runs"]]) == ("original", [a, b])         # rolled back


@pytest.mark.parametrize("payload", [{"title": "a\x00b"}, {"title": ""}, {"title": "x" * 121}, {"visibility": "all"},
                                     {"add_run_ids": ["nope"]}, {"add_run_ids": ["a\x00b"]}, {"x": 1},
                                     {"add_run_ids": [str(uuid.uuid4())] * 51}])
def test_bad_patch_bodies_are_422(env: Env, payload: dict[str, Any]) -> None:
    owner, game = env.user(), env.game()
    eid = post_ok(env, owner, [arm(env, owner, game), arm(env, owner, game, "none@1")])
    assert patch(env, owner, eid, payload).status_code == 422


def test_experiment_writes_are_limited_per_user(make_env: Any) -> None:
    env: Env = make_env(write_limit_per_min=3)
    owner, game = env.user(), env.game()
    runs = [arm(env, owner, game), arm(env, owner, game, "none@1")]
    codes = [make(env, owner, runs).status_code for _ in range(5)]
    assert codes[:3] == [201] * 3 and codes[3:] == [429, 429]


def test_compare_reads_at_most_the_event_cap_per_run(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import api.app as app_mod
    monkeypatch.setattr(app_mod, "MAX_COMPARE_EVENTS", 3)
    owner, game = env.user(), env.game()
    a, b = arm(env, owner, game), arm(env, owner, game, "none@1")
    w = EventLogWriter(env.store, a)
    for step in range(10):
        w.append([("obs", step, {"score": step})])
    body = get(env, owner, post_ok(env, owner, [a, b]), "/compare").json()
    assert body["steps"] == [0, 1, 2]                                                    # the first three events only


# ---------------------------------------------------------------- the functions themselves (defence under the API)

def as_user(uid: str) -> psycopg.Connection[Any]:
    from conftest import API_DB_URL
    c = psycopg.connect(API_DB_URL)
    c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": uid, "role": "authenticated"}),))
    c.execute("set local role authenticated")
    return c


def test_update_experiment_is_owner_only_inside_the_database(env: Env) -> None:
    owner, other, game = env.user(), env.user(), env.game()
    eid = post_ok(env, owner, [arm(env, owner, game), arm(env, owner, game, "none@1")], title="mine", visibility="public")
    for target in (eid, str(uuid.uuid4())):
        with as_user(other) as c, pytest.raises(psycopg.errors.RaiseException, match="experiment_not_found"):
            c.execute("select api_private.update_experiment(%s, 'pwned', 'private', null, null)", (target,))
    row = env.admin.execute("select title, visibility from public.experiments where id = %s", (eid,)).fetchone()
    assert row == {"title": "mine", "visibility": "public"}


def test_experiment_functions_refuse_an_erased_or_missing_account(env: Env) -> None:
    owner, ghost, game = env.user(), env.user(), env.game()
    a = arm(env, owner, game)
    eid = post_ok(env, owner, [a])
    env.admin.execute("update public.profiles set erased_at = now() where id = %s", (ghost,))
    unknown = str(uuid.uuid4())
    for who in (ghost, unknown):
        for sql, args in (("select api_private.create_experiment('t', 'preset_ref', %s::uuid[], 'private')", ([a],)),
                          ("select api_private.update_experiment(%s, 't', null, null, null)", (eid,))):
            with as_user(who) as c, pytest.raises(psycopg.errors.RaiseException, match="account_erased"):
                c.execute(sql, args)


def test_the_experiment_runs_reader_checks_the_experiments_own_visibility(env: Env) -> None:
    owner, other, game = env.user(), env.user(), env.game()
    private = post_ok(env, owner, [arm(env, owner, game, "general@1", "public"), arm(env, owner, game, "none@1", "public")])
    public = post_ok(env, owner, [arm(env, owner, game, "general@1", "public"), arm(env, owner, game, "none@1", "public")],
                     visibility="public")
    with as_user(other) as c:
        assert c.execute("select count(*) from api_private.experiment_runs(%s)", (private,)).fetchone()[0] == 0
        assert c.execute("select count(*) from api_private.experiment_runs(%s)", (public,)).fetchone()[0] == 2
    with as_user(owner) as c:
        assert c.execute("select count(*) from api_private.experiment_runs(%s)", (private,)).fetchone()[0] == 2
