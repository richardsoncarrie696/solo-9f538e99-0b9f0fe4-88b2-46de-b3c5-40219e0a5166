"""丰富度分析：精确上下界、见证、拒绝条件、上限、过期与迟到保存。"""
import itertools
import random

from app import services
from app.richness import NameScope, _solve, compute_richness
from app.resolver import analyze_endpoints


def _batch(client, observations, note=None):
    return client.post("/batches", json={"observations": observations, "note": note})


def _taxonomy(client, accepted, edges=None, note=None):
    return client.post(
        "/taxonomies",
        json={"accepted_names": accepted, "synonym_edges": edges or [], "note": note},
    )


# 全部名称均可解析（无环、无断链），用于成功路径。
CLEAN_OBS = [
    {"code": "o1", "name": "旧名甲"},    # 固定：接受名A
    {"code": "o2", "name": "分叉名"},    # 可选：A 或 B
    {"code": "o3", "name": "分叉名"},    # 同名记录，必须同选
    {"code": "o4", "name": "菱形名"},    # 固定：A
    {"code": "o5", "name": "末端名"},    # 可选：B 或 C
]

CLEAN_EDGES = [
    {"synonym": "旧名甲", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名B"},
    {"synonym": "菱形名", "target": "s1"},
    {"synonym": "菱形名", "target": "s2"},
    {"synonym": "s1", "target": "接受名A"},
    {"synonym": "s2", "target": "接受名A"},
    {"synonym": "末端名", "target": "接受名B"},
    {"synonym": "末端名", "target": "接受名C"},
]

ACCEPTED = ["接受名A", "接受名B", "接受名C"]


def _clean_result(client):
    bid = _batch(client, CLEAN_OBS).json()["id"]
    rev = _taxonomy(client, ACCEPTED, CLEAN_EDGES).json()["revision_id"]
    return client.post("/results", json={"batch_id": bid}).json(), rev


def test_bounds_witness_and_frozen_scope(client):
    result, rev = _clean_result(client)
    assert result["included_count"] == 2   # 旧名甲、菱形名
    assert result["pending_count"] == 3    # 分叉名 x2（分叉冲突）、末端名

    r = client.post("/richness-analyses", json={"result_id": result["result_id"]})
    assert r.status_code == 201, r.text
    body = r.json()

    # 名称种数 4（旧名甲/分叉名/菱形名/末端名），候选并集 3。
    assert body["observation_name_count"] == 4
    assert body["candidate_name_count"] == 3
    # 已纳入的 旧名甲、菱形名 固定为 A；分叉名 {A,B}、末端名 {B,C}。
    # 末端名无法取 A，故 B 必被使用 -> min = {A,B} = 2；
    # 分叉名取 B、末端名取 C 可同时用上三者 -> max = 3。
    assert body["lower_bound"] == 2
    assert body["upper_bound"] == 3

    # 下界见证：所有名称都归属 A（分叉名取 A，末端名取 B 会引入 B -> 需要 B）
    for witness, count in (
        (body["min_witness"], body["lower_bound"]),
        (body["max_witness"], body["upper_bound"]),
    ):
        assignments = {a["observation_name"]: a["accepted_name"] for a in witness["assignments"]}
        assert set(assignments) == {"旧名甲", "分叉名", "菱形名", "末端名"}
        assert set(witness["accepted_names"]) == set(assignments.values())
        assert witness["accepted_name_count"] == count == len(witness["accepted_names"])
        # 同名记录一并列出（分叉名有 o2、o3）
        codes = {
            a["observation_name"]: set(a["observation_codes"]) for a in witness["assignments"]
        }
        assert codes["分叉名"] == {"o2", "o3"}
    assert body["min_witness"]["accepted_names"] == ["接受名A", "接受名B"]
    assert body["max_witness"]["accepted_names"] == ["接受名A", "接受名B", "接受名C"]

    # 已纳入记录的原归属不变；分叉名候选为 {A,B}，末端名 {B,C}
    by_name = {o["input_name"]: o for o in body["observations"]}
    assert by_name["旧名甲"]["source_accepted_name"] == "接受名A"
    assert by_name["旧名甲"]["attribution_fixed"] is True
    assert by_name["分叉名"]["attribution_fixed"] is False
    assert by_name["分叉名"]["candidate_accepted_names"] == ["接受名A", "接受名B"]
    assert by_name["末端名"]["candidate_accepted_names"] == ["接受名B", "接受名C"]

    # 按分析号重读：范围与见证逐字一致
    again = client.get(f"/richness-analyses/{body['analysis_id']}").json()
    assert again == body

    # 分析不改变待核记录的供数资格：来源结果内容与状态不变
    frozen = client.get(f"/results/{result['result_id']}").json()
    assert frozen["included_count"] == 2 and frozen["pending_count"] == 3
    assert frozen["status"] == "active"


def test_reject_cycle_and_non_accepted_lists_observation_codes(client):
    obs = [
        {"code": "ok", "name": "旧名甲"},
        {"code": "c1", "name": "环名x"},
        {"code": "c2", "name": "断链名"},
    ]
    bid = _batch(client, obs).json()["id"]
    _taxonomy(
        client,
        ["接受名A"],
        [
            {"synonym": "旧名甲", "target": "接受名A"},
            {"synonym": "环名x", "target": "环名y"},
            {"synonym": "环名y", "target": "环名x"},
            {"synonym": "断链名", "target": "未知末端"},
        ],
    )
    result = client.post("/results", json={"batch_id": bid}).json()

    r = client.post("/richness-analyses", json={"result_id": result["result_id"]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    codes = {f["observation_code"] for f in detail["observations"]}
    assert codes == {"c1", "c2"}
    reasons = {f["observation_code"]: f["reason_codes"] for f in detail["observations"]}
    assert reasons["c1"] == ["cycle"]
    assert reasons["c2"] == ["no_accepted_name"]

    # 整次拒绝：不留任何部分分析
    assert client.get("/richness-analyses").json() == []


def test_source_result_must_exist(client):
    assert client.post("/richness-analyses", json={"result_id": 999}).status_code == 404


def test_expired_source_result_rejected(client):
    result, _ = _clean_result(client)
    _taxonomy(client, ACCEPTED, CLEAN_EDGES, note="v2")
    assert client.get(f"/results/{result['result_id']}").json()["status"] == "expired"
    r = client.post("/richness-analyses", json={"result_id": result["result_id"]})
    assert r.status_code == 409
    assert client.get("/richness-analyses").json() == []


def test_expired_source_analysis_is_expired_and_frozen(client):
    result, rev = _clean_result(client)
    analysis = client.post(
        "/richness-analyses", json={"result_id": result["result_id"]}
    ).json()
    assert analysis["status"] == "active"

    # 分类更正仍可完成；旧结果与旧分析过期
    _taxonomy(client, ACCEPTED, CLEAN_EDGES, note="v2")
    old_result = client.get(f"/results/{result['result_id']}").json()
    assert old_result["status"] == "expired"
    reloaded = client.get(f"/richness-analyses/{analysis['analysis_id']}").json()
    assert reloaded["status"] == "expired"
    # 范围与见证不变
    assert reloaded["result_id"] == analysis["result_id"]
    assert reloaded["revision_id"] == analysis["revision_id"] == rev
    assert reloaded["lower_bound"] == analysis["lower_bound"]
    assert reloaded["upper_bound"] == analysis["upper_bound"]
    assert reloaded["min_witness"] == analysis["min_witness"]
    assert reloaded["max_witness"] == analysis["max_witness"]

    # 过期分析可按状态过滤
    assert [
        a["analysis_id"]
        for a in client.get(
            "/richness-analyses", params={"status": "expired"}
        ).json()
    ] == [analysis["analysis_id"]]


def test_new_result_for_batch_expires_analysis(client):
    result, _ = _clean_result(client)
    analysis = client.post(
        "/richness-analyses", json={"result_id": result["result_id"]}
    ).json()
    # 同批次重新生成结果 -> 旧结果过期 -> 分析过期
    new_result = client.post("/results", json={"batch_id": result["batch_id"]}).json()
    assert new_result["result_id"] != result["result_id"]
    assert new_result["status"] == "active"
    reloaded = client.get(f"/richness-analyses/{analysis['analysis_id']}").json()
    assert reloaded["status"] == "expired"


def test_late_save_marks_analysis_expired_and_leaves_no_partial(client, conn, monkeypatch):
    """计算期间完成分类更正：迟到保存的分析落为 expired，范围/见证仍完整。"""
    result, _ = _clean_result(client)
    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    real_compute = services.compute_richness
    called = {"done": False}

    def slow_compute(*args, **kwargs):
        computation = real_compute(*args, **kwargs)
        if not called["done"]:
            called["done"] = True
            # 在“计算结束、保存之前”插入一次分类更正（另一连接、另一事务）
            from app.database import connect as _connect
            from app.models import TaxonomyCreate, EdgeIn

            other = _connect(db_file)
            try:
                services.create_taxonomy_revision(
                    other,
                    TaxonomyCreate(
                        accepted_names=ACCEPTED,
                        synonym_edges=[EdgeIn(**e) for e in CLEAN_EDGES],
                        note="late correction",
                    ),
                )
            finally:
                other.close()
        return computation

    monkeypatch.setattr(services, "compute_richness", slow_compute)
    r = client.post("/richness-analyses", json={"result_id": result["result_id"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "expired"
    assert body["lower_bound"] == 2 and body["upper_bound"] == 3
    assert len(body["min_witness"]["assignments"]) == 4
    assert client.get(f"/results/{result['result_id']}").json()["status"] == "expired"


def test_limits_rejected(client):
    # 21 种观测名（并集仅 1 个候选接受名）-> 观测名上限 422
    obs = [{"code": f"c{i}", "name": f"名{i}"} for i in range(21)]
    bid = _batch(client, obs).json()["id"]
    edges = [{"synonym": f"名{i}", "target": "共同接受名"} for i in range(21)]
    _taxonomy(client, ["共同接受名"], edges)
    result = client.post("/results", json={"batch_id": bid}).json()
    r = client.post("/richness-analyses", json={"result_id": result["result_id"]})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["max_observation_names"] == 20
    assert detail["observation_name_count"] == 21

    # 17 种候选接受名并集 -> 422
    bid2 = _batch(client, [{"code": "d0", "name": "分叉0"}]).json()["id"]
    edges2 = [{"synonym": "分叉0", "target": f"A{i}"} for i in range(17)]
    _taxonomy(client, [f"A{i}" for i in range(17)], edges2)
    result2 = client.post("/results", json={"batch_id": bid2}).json()
    r = client.post("/richness-analyses", json={"result_id": result2["result_id"]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["max_candidate_names"] == 16
    assert detail["candidate_name_count"] == 17


def test_analysis_does_not_change_pending_eligibility(client):
    result, _ = _clean_result(client)
    before = client.get(f"/results/{result['result_id']}").json()
    client.post("/richness-analyses", json={"result_id": result["result_id"]})
    after = client.get(f"/results/{result['result_id']}").json()
    assert after == before
    pendings = [i for i in after["items"] if i["item_status"] == "pending"]
    assert len(pendings) == 3
    assert all(i["accepted_name"] is None for i in pendings)


# ---------- 求解器精确性：与暴力枚举逐一比对 ----------


def _brute_force(scopes: list[NameScope]) -> tuple[int, int]:
    choices = [scope.candidate_accepted_names for scope in scopes]
    counts = []
    for combo in itertools.product(*choices):
        counts.append(len(set(combo)))
    return min(counts), max(counts)


def test_solver_matches_brute_force_random():
    rng = random.Random(20261116)
    for _ in range(600):
        universe = [f"A{i}" for i in range(rng.randint(1, 7))]
        n_names = rng.randint(1, 6)
        scopes = []
        for i in range(n_names):
            k = rng.randint(1, min(3, len(universe)))
            cands = tuple(sorted(rng.sample(universe, k)))
            scopes.append(
                NameScope(
                    observation_name=f"名{i}",
                    # 已纳入名称的候选集退化为单值。
                    candidate_accepted_names=(cands[0],) if rng.random() < 0.25 else cands,
                    fixed=len(cands) == 1 and rng.random() < 0.5,
                    observation_codes=(f"o{i}",),
                )
            )
        lo_witness, hi_witness = _solve(scopes)
        expected_lo, expected_hi = _brute_force(scopes)
        assert (lo_witness.accepted_name_count, hi_witness.accepted_name_count) == (
            expected_lo,
            expected_hi,
        ), scopes
        # 见证自洽：每个观测名一个归属，且都落在自己的候选集内
        for witness in (lo_witness, hi_witness):
            choices = {
                s.observation_name: s.candidate_accepted_names for s in scopes
            }
            assert len(witness.assignments) == len(scopes)
            for name, accepted, _codes in witness.assignments:
                assert accepted in choices[name]
            assert set(witness.accepted_names) == {
                accepted for _n, accepted, _c in witness.assignments
            }


def test_solver_handles_fixed_and_shared_names(client):
    # 固定归属（已纳入）候选恒为单值；固定 A 与可选 {B,C}：
    # 两者无法共享取值，且每种观测名只选一个值 -> 上下界都为 2。
    scopes = [
        NameScope("旧", ("A",), True, ("o1",)),
        NameScope("新", ("B", "C"), False, ("o2",)),
    ]
    lo, hi = _solve(scopes)
    assert (lo.accepted_name_count, hi.accepted_name_count) == (2, 2)
    assert lo.assignments[0][1] == "A"
    assert set(hi.accepted_names) == {"A", hi.assignments[1][1]}

    # 两个已纳入名称各固定在不同接受名：上下界均为 2。
    scopes2 = [
        NameScope("旧1", ("A",), True, ("o1",)),
        NameScope("旧2", ("B",), True, ("o2",)),
    ]
    lo2, hi2 = _solve(scopes2)
    assert (lo2.accepted_name_count, hi2.accepted_name_count) == (2, 2)
    assert hi2.accepted_names == ("A", "B")


def test_empty_batch_analysis(client):
    # 批次至少 1 条观测，因此正常路径不会出现空范围；直接验证纯计算入口的退化行为。
    comp = compute_richness([], {"A"}, {}, analyze_endpoints)
    assert comp.name_count == 0
    assert comp.lower_bound == comp.upper_bound == 0
    assert comp.min_witness.accepted_names == ()


def test_same_name_records_share_choice(client):
    obs = [
        {"code": "a1", "name": "分叉名"},
        {"code": "a2", "name": "分叉名"},
        {"code": "a3", "name": "分叉名"},
    ]
    bid = _batch(client, obs).json()["id"]
    _taxonomy(
        client,
        ["A", "B"],
        [
            {"synonym": "分叉名", "target": "A"},
            {"synonym": "分叉名", "target": "B"},
        ],
    )
    result = client.post("/results", json={"batch_id": bid}).json()
    body = client.post(
        "/richness-analyses", json={"result_id": result["result_id"]}
    ).json()
    assert body["observation_name_count"] == 1
    assert body["lower_bound"] == body["upper_bound"] == 1
    for assignment in body["min_witness"]["assignments"]:
        assert assignment["observation_codes"] == ["a1", "a2", "a3"]
