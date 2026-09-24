"""共享安装方案接口测试：纯函数与 HTTP 端到端。"""

import pytest

from app import main as main_module
from app.models import ManifestIn
from app.shared import evaluate_shared_resolution, merge_manifest_constraints
from app.solver import DEFAULT_NODE_BUDGET, Solver

PATH = "/api/v1/audits/shared-resolution"


def _make_audit(client, dependencies=None, plugin_name=None, plugin_version=None):
    """提交一条审计，返回 (audit_id, status, resolution)。"""
    manifest: dict = {"dependencies": dependencies or []}
    if plugin_name is not None:
        manifest["plugin_name"] = plugin_name
        manifest["plugin_version"] = plugin_version
    resp = client.post("/api/v1/audits", json=manifest)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["id"], body["status"], body["resolution"]


def _share(client, audit_ids):
    return client.post(PATH, json={"audit_ids": audit_ids})


# ---------- 纯函数 ----------


def test_merge_manifest_constraints_combines_root_pins_and_dependencies():
    manifests = [
        ManifestIn.model_validate(
            {
                "plugin_name": "mega",
                "plugin_version": "1.0.0",
                "dependencies": [{"name": "a", "range": "^1.0.0"}],
            }
        ),
        ManifestIn.model_validate({"dependencies": [{"name": "b", "range": "~2.0.0"}]}),
    ]
    # 根插件固定为 =版本 约束，显式依赖原样并入，清单间保持输入顺序
    assert merge_manifest_constraints(manifests) == [
        ("mega", "=1.0.0"),
        ("a", "^1.0.0"),
        ("b", "~2.0.0"),
    ]


def test_merge_manifest_constraints_without_root_plugin():
    manifests = [
        ManifestIn.model_validate({"dependencies": [{"name": "a", "range": "*"}]}),
        ManifestIn.model_validate({"dependencies": []}),
    ]
    assert merge_manifest_constraints(manifests) == [("a", "*")]


def test_evaluate_shared_resolution_feasible_single_version_per_package():
    audits = [
        {
            "id": "a1",
            "manifest_json": {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
        },
        {
            "id": "a2",
            "manifest_json": {"dependencies": [{"name": "cachekit", "range": "^1.0.0"}]},
        },
    ]
    registry = {
        "core-lib": {"1.5.0": {}, "1.0.0": {}},
        "cachekit": {"1.3.0": {"core-lib": "^1.4.0"}},
    }
    body = evaluate_shared_resolution(
        audits, registry, solver_factory=lambda reg: Solver(reg)
    )
    assert body["status"] == "feasible"
    # 同名包（core-lib 被两条清单共同约束）只选中一个版本
    assert body["resolution"] == {"cachekit": "1.3.0", "core-lib": "1.5.0"}
    assert body["conflicts"] == []
    assert body["sources"] is not None
    assert [m["audit_id"] for m in body["manifests"]] == ["a1", "a2"]
    assert body["merged_constraints"] == [
        {"name": "core-lib", "range": "^1.0.0"},
        {"name": "cachekit", "range": "^1.0.0"},
    ]


def test_evaluate_shared_resolution_infeasible_returns_conflict_chains():
    audits = [
        {
            "id": "a1",
            "manifest_json": {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
        },
        {
            "id": "a2",
            "manifest_json": {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]},
        },
    ]
    registry = {"core-lib": {"1.5.0": {}, "2.1.0": {}}}
    body = evaluate_shared_resolution(
        audits, registry, solver_factory=lambda reg: Solver(reg)
    )
    assert body["status"] == "infeasible"
    assert body["resolution"] is None
    assert body["sources"] is None
    assert body["conflicts"], "无解时必须给出冲突链"
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "range_conflict"
    assert conflict["package"] == "core-lib"
    ranges = {c["version_range"] for c in conflict["minimal_constraints"]}
    assert ranges == {"^1.0.0", "^2.0.0"}


def test_evaluate_shared_resolution_root_plugin_transitives_participate():
    # 根插件 mega@1.0.0 的传递依赖 sub ^1.0.0 与另一清单的 sub ^2.0.0 冲突
    audits = [
        {
            "id": "a1",
            "manifest_json": {
                "plugin_name": "mega",
                "plugin_version": "1.0.0",
                "dependencies": [],
            },
        },
        {
            "id": "a2",
            "manifest_json": {"dependencies": [{"name": "sub", "range": "^2.0.0"}]},
        },
    ]
    registry = {
        "mega": {"1.0.0": {"sub": "^1.0.0"}},
        "sub": {"1.0.0": {}, "2.0.0": {}},
    }
    body = evaluate_shared_resolution(
        audits, registry, solver_factory=lambda reg: Solver(reg)
    )
    assert body["status"] == "infeasible"
    assert body["merged_constraints"][0] == {"name": "mega", "range": "=1.0.0"}


def test_evaluate_shared_resolution_inconclusive_never_claims_infeasible():
    # 需要实际搜索（选择候选版本）的场景，预算为 0 时耗尽
    audits = [
        {
            "id": "a1",
            "manifest_json": {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
        },
        {
            "id": "a2",
            "manifest_json": {"dependencies": [{"name": "cachekit", "range": "^1.0.0"}]},
        },
    ]
    registry = {
        "core-lib": {"1.5.0": {}, "1.0.0": {}},
        "cachekit": {"1.3.0": {"core-lib": "^1.4.0"}},
    }

    class _ZeroBudgetSolver(Solver):
        def __init__(self, registry, node_budget=DEFAULT_NODE_BUDGET):
            super().__init__(registry, node_budget=0)

    body = evaluate_shared_resolution(
        audits, registry, solver_factory=lambda reg: _ZeroBudgetSolver(reg)
    )
    # 预算耗尽：inconclusive，不得宣称无解，也不给冲突链
    assert body["status"] == "inconclusive"
    assert body["resolution"] is None
    assert body["conflicts"] == []


# ---------- HTTP 接口 ----------


def test_shared_resolution_compatible_audits_feasible(client):
    a_id, _, a_res = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    b_id, _, b_res = _make_audit(client, [{"name": "cachekit", "range": "^1.0.0"}])
    assert a_res == {"core-lib": "1.5.0"}
    assert b_res == {"cachekit": "1.3.0", "core-lib": "1.5.0"}

    resp = _share(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["audit_ids"] == [a_id, b_id]
    assert body["requested_audit_count"] == 2
    assert body["duplicate_audit_ids"] == []
    assert body["status"] == "feasible"
    # 共享解同时满足两条清单，core-lib 只出现一个版本
    assert body["resolution"] == {"cachekit": "1.3.0", "core-lib": "1.5.0"}
    assert body["conflicts"] == []
    assert body["node_count"] >= 1
    assert body["explanation"]
    # 每个被选中版本都带来源链
    packages = {s["package"] for s in body["sources"]}
    assert packages == {"cachekit", "core-lib"}
    # 参与清单与合并约束按输入顺序给出
    assert [m["audit_id"] for m in body["manifests"]] == [a_id, b_id]
    assert body["manifests"][0]["dependencies"] == [
        {"name": "core-lib", "range": "^1.0.0"}
    ]
    assert body["merged_constraints"] == [
        {"name": "core-lib", "range": "^1.0.0"},
        {"name": "cachekit", "range": "^1.0.0"},
    ]


def test_shared_resolution_conflicting_ranges_infeasible(client):
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    b_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^2.0.0"}])

    resp = _share(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "infeasible"
    assert body["resolution"] is None
    assert body["sources"] is None
    assert body["conflicts"], "无解时必须返回冲突链"
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "range_conflict"
    assert conflict["package"] == "core-lib"
    ranges = {c["version_range"] for c in conflict["minimal_constraints"]}
    assert ranges == {"^1.0.0", "^2.0.0"}


def test_shared_resolution_root_plugin_transitives_join_solving(client):
    # megaplugin@1.0.0 的传递依赖（authkit/httpkit/...）必须参与共享求解
    a_id, status, _ = _make_audit(
        client, [], plugin_name="megaplugin", plugin_version="1.0.0"
    )
    assert status == "feasible"
    b_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.4.0"}])

    resp = _share(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "feasible"
    resolution = body["resolution"]
    # 根插件固定版本入选，传递依赖一并解出
    assert resolution["megaplugin"] == "1.0.0"
    assert resolution["authkit"] == "1.2.0"
    assert resolution["httpkit"] == "2.2.0"
    assert resolution["core-lib"] == "1.5.0"
    # 根插件以 =版本 固定约束并入
    assert {"name": "megaplugin", "range": "=1.0.0"} in body["merged_constraints"]
    assert body["manifests"][0]["plugin_name"] == "megaplugin"
    assert body["manifests"][0]["plugin_version"] == "1.0.0"


def test_shared_resolution_root_transitive_conflict_infeasible(client):
    # megaplugin@1.0.0 -> httpkit ~2.2.0 -> loggerkit >=0.5.0 <0.9.0，
    # 与另一清单的 loggerkit ^0.9.0 冲突
    a_id, _, _ = _make_audit(
        client, [], plugin_name="megaplugin", plugin_version="1.0.0"
    )
    b_id, _, _ = _make_audit(client, [{"name": "loggerkit", "range": "^0.9.0"}])

    resp = _share(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "infeasible"
    assert body["resolution"] is None
    assert body["conflicts"]
    assert any(c["package"] == "loggerkit" for c in body["conflicts"])


def test_shared_resolution_same_package_root_versions_conflict(client):
    # 两个清单把同一包固定为不同的根插件版本：同名包只能选一个版本
    client.put(
        "/api/v1/registry/packages/plug",
        json={
            "name": "plug",
            "versions": [
                {"version": "1.0.0", "dependencies": {}},
                {"version": "2.0.0", "dependencies": {}},
            ],
        },
    )
    a_id, _, _ = _make_audit(client, [], plugin_name="plug", plugin_version="1.0.0")
    b_id, _, _ = _make_audit(client, [], plugin_name="plug", plugin_version="2.0.0")

    resp = _share(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "infeasible"
    conflict = body["conflicts"][0]
    assert conflict["package"] == "plug"
    ranges = {c["version_range"] for c in conflict["minimal_constraints"]}
    assert ranges == {"=1.0.0", "=2.0.0"}


def test_shared_resolution_duplicate_ids_deduped_in_first_seen_order(client):
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    b_id, _, _ = _make_audit(client, [{"name": "cachekit", "range": "^1.0.0"}])

    resp = _share(client, [b_id, a_id, b_id, a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested_audit_count"] == 5
    assert body["duplicate_audit_ids"] == sorted({a_id, b_id})
    # 参与顺序按首次出现：b 在前 a 在后
    assert body["audit_ids"] == [b_id, a_id]
    assert [m["audit_id"] for m in body["manifests"]] == [b_id, a_id]
    assert body["status"] == "feasible"
    assert body["resolution"] == {"cachekit": "1.3.0", "core-lib": "1.5.0"}


def test_shared_resolution_too_few_distinct_ids_after_dedup_422(client):
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    resp = _share(client, [a_id, a_id, a_id])
    assert resp.status_code == 422
    assert "不足 2 条" in resp.json()["detail"]


def test_shared_resolution_unknown_audit_404(client):
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    missing = "00000000-0000-0000-0000-000000000000"
    resp = _share(client, [a_id, missing])
    assert resp.status_code == 404
    assert missing in resp.json()["detail"]

    # 全部不存在同样 404
    other = "11111111-1111-1111-1111-111111111111"
    resp = _share(client, [missing, other])
    assert resp.status_code == 404
    assert missing in resp.json()["detail"]
    assert other in resp.json()["detail"]


def test_shared_resolution_non_feasible_audit_422(client):
    # cachekit ^1 + loggerkit ^0.9 在种子注册表上无解
    bad_id, bad_status, _ = _make_audit(
        client,
        [
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "loggerkit", "range": "^0.9.0"},
        ],
    )
    assert bad_status == "infeasible"
    good_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])

    resp = _share(client, [good_id, bad_id])
    assert resp.status_code == 422
    assert bad_id in resp.json()["detail"]


def test_shared_resolution_audit_ids_count_limits_422(client):
    # 少于 2 个
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    resp = _share(client, [a_id])
    assert resp.status_code == 422

    # 超过 20 个（即使重复，原始数量也受限）
    resp = _share(client, [a_id] * 21)
    assert resp.status_code == 422

    # 缺字段 / 空列表
    resp = client.post(PATH, json={})
    assert resp.status_code == 422
    resp = client.post(PATH, json={"audit_ids": []})
    assert resp.status_code == 422


def test_shared_resolution_blank_audit_id_422(client):
    resp = _share(client, ["  ", "x"])
    assert resp.status_code == 422


def test_shared_resolution_inconclusive_not_reported_as_infeasible(
    client, monkeypatch
):
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    b_id, _, _ = _make_audit(client, [{"name": "cachekit", "range": "^1.0.0"}])

    # 共享求解器预算归零 -> inconclusive
    class _ZeroSolver(Solver):
        def __init__(self, registry, node_budget=DEFAULT_NODE_BUDGET):
            super().__init__(registry, node_budget=0)

    monkeypatch.setattr(main_module, "Solver", _ZeroSolver)
    resp = _share(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "inconclusive"
    assert body["resolution"] is None
    assert body["conflicts"] == []
    assert "预算" in body["explanation"]


def test_shared_resolution_is_read_only(client):
    packages_before = client.get("/api/v1/registry/packages").json()

    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    b_id, _, _ = _make_audit(client, [{"name": "cachekit", "range": "^1.0.0"}])
    audits_before = client.get("/api/v1/audits").json()["total"]

    for _ in range(2):
        resp = _share(client, [a_id, b_id])
        assert resp.status_code == 200

    # 不新增审计记录、不改注册表；重复调用结果一致
    assert client.get("/api/v1/audits").json()["total"] == audits_before
    assert client.get("/api/v1/registry/packages").json() == packages_before
    again = _share(client, [a_id, b_id]).json()
    assert again["status"] == "feasible"
    assert again["resolution"] == {"cachekit": "1.3.0", "core-lib": "1.5.0"}


def test_shared_resolution_uses_saved_manifests_not_resolutions(client):
    # 共享求解基于各审计保存时的原始清单：两条各自可行的审计合并后
    # 因约束冲突而无解，而不是简单取各自 resolution 的并集
    a_id, _, a_res = _make_audit(client, [{"name": "loggerkit", "range": "^0.9.0"}])
    b_id, _, b_res = _make_audit(client, [{"name": "cachekit", "range": "^1.0.0"}])
    assert a_res["core-lib"] == "2.1.0"
    assert b_res["core-lib"] == "1.5.0"

    resp = _share(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # loggerkit 0.9.0 要 core-lib ^2.0.0，cachekit 1.x 要 core-lib <2.0.0 -> 无解
    assert body["status"] == "infeasible"
    assert body["resolution"] is None


def test_shared_resolution_listed_in_index(client):
    endpoints = client.get("/").json()["endpoints"]
    assert "POST /api/v1/audits/shared-resolution" in endpoints


def test_shared_resolution_openapi_doc_present(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/audits/shared-resolution"]["post"]
    assert operation["tags"] == ["audits"]
    assert "共享" in operation["summary"]
    assert "SharedResolutionResponse" in schema["components"]["schemas"]
    status_prop = schema["components"]["schemas"]["SharedResolutionResponse"][
        "properties"
    ]["status"]
    assert set(status_prop["enum"]) == {"feasible", "infeasible", "inconclusive"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
