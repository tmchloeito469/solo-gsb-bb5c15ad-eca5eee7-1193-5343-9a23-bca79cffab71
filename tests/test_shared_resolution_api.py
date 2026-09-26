"""共享安装方案接口测试：纯函数与 HTTP 端到端。"""

from app import main as main_module
from app.models import ManifestIn
from app.shared import merge_shared_constraints, solve_shared_resolution
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


def _resolve(client, audit_ids):
    return client.post(PATH, json={"audit_ids": audit_ids})


# ---------- 纯函数 ----------


def _manifest(raw):
    return ManifestIn.model_validate(raw)


def test_merge_shared_constraints_combines_explicit_and_root_pins_in_order():
    manifests = [
        _manifest(
            {
                "plugin_name": "megaplugin",
                "plugin_version": "1.0.0",
                "dependencies": [{"name": "authkit", "range": "^1.0.0"}],
            }
        ),
        _manifest({"dependencies": [{"name": "core-lib", "range": "~1.4.0"}]}),
        _manifest({"dependencies": []}),
    ]
    assert merge_shared_constraints(manifests) == [
        ("authkit", "^1.0.0"),
        ("megaplugin", "=1.0.0"),
        ("core-lib", "~1.4.0"),
    ]


def test_solve_shared_resolution_unifies_common_packages():
    # 两份清单各约束 core-lib：合并后只能选一个同时满足两侧的版本
    manifests = [
        _manifest({"dependencies": [{"name": "core-lib", "range": "~1.4.0"}]}),
        _manifest({"dependencies": [{"name": "cachekit", "range": "^1.0.0"}]}),
    ]
    registry = {
        "core-lib": {"1.0.0": {}, "1.4.0": {}, "1.5.0": {}, "2.1.0": {}},
        "cachekit": {
            "1.3.0": {"core-lib": "^1.4.0"},
            "1.1.0": {"core-lib": ">=1.0.0 <2.0.0"},
        },
    }
    result = solve_shared_resolution(
        manifests, registry, solver_factory=lambda reg: Solver(reg)
    )
    assert result.status == "feasible"
    # ~1.4.0 与 cachekit@1.3.0 的 ^1.4.0 交集只剩 1.4.0；同名包仅一个版本
    assert result.resolution == {"core-lib": "1.4.0", "cachekit": "1.3.0"}


def test_solve_shared_resolution_root_plugin_transitive_deps_participate():
    # 根插件固定版本并入约束，其传递依赖随求解自动展开
    manifests = [
        _manifest(
            {
                "plugin_name": "megaplugin",
                "plugin_version": "1.0.0",
                "dependencies": [],
            }
        ),
        _manifest({"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]}),
    ]
    registry = {
        "megaplugin": {"1.0.0": {"authkit": "^1.0.0", "httpkit": "~2.2.0"}},
        "authkit": {
            "1.2.0": {"core-lib": ">=1.4.0 <2.0.0", "cachekit": "^1.0.0"},
            "1.0.0": {"core-lib": "~1.0.0", "cachekit": "^1.0.0"},
        },
        "cachekit": {
            "1.3.0": {"core-lib": "^1.4.0"},
            "1.1.0": {"core-lib": ">=1.0.0 <2.0.0"},
            "1.0.0": {"core-lib": "^1.0.0"},
        },
        "httpkit": {
            "2.2.0": {"loggerkit": ">=0.5.0 <0.9.0"},
        },
        "loggerkit": {
            "0.5.0": {"core-lib": "^1.0.0"},
        },
        "core-lib": {"1.5.0": {}, "1.4.0": {}, "1.0.0": {}},
    }
    result = solve_shared_resolution(
        manifests, registry, solver_factory=lambda reg: Solver(reg)
    )
    assert result.status == "feasible"
    assert result.resolution["megaplugin"] == "1.0.0"
    # 根插件的传递依赖全部参与且只选一个版本
    assert set(result.resolution) == {
        "megaplugin", "authkit", "cachekit", "httpkit", "loggerkit", "core-lib",
    }
    assert result.resolution["core-lib"] == "1.5.0"


def test_solve_shared_resolution_incompatible_pins_are_infeasible():
    # 同一包的两个不兼容固定版本：同名包只能选一个，合并约束无解
    manifests = [
        _manifest({"dependencies": [{"name": "core-lib", "range": "1.0.0"}]}),
        _manifest({"dependencies": [{"name": "core-lib", "range": "2.1.0"}]}),
    ]
    registry = {"core-lib": {"1.0.0": {}, "2.1.0": {}}}
    result = solve_shared_resolution(
        manifests, registry, solver_factory=lambda reg: Solver(reg)
    )
    assert result.status == "infeasible"
    assert result.resolution is None
    assert result.conflicts
    assert result.conflicts[0]["package"] == "core-lib"


def test_solve_shared_resolution_budget_exhausted_is_inconclusive():
    manifests = [
        _manifest({"dependencies": [{"name": "core-lib", "range": "*"}]}),
        _manifest({"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]}),
    ]
    registry = {"core-lib": {"1.0.0": {}, "1.5.0": {}, "2.1.0": {}}}

    class _ZeroBudgetSolver(Solver):
        def __init__(self, registry, node_budget=DEFAULT_NODE_BUDGET):
            super().__init__(registry, node_budget=0)

    result = solve_shared_resolution(
        manifests, registry, solver_factory=lambda reg: _ZeroBudgetSolver(reg)
    )
    # 预算耗尽：只能是 inconclusive，绝不能宣称无解
    assert result.status == "inconclusive"
    assert result.resolution is None
    assert result.conflicts == []


# ---------- HTTP 接口 ----------


def test_shared_resolution_feasible_union(client):
    # A：core-lib ^1.0.0（单独选中 1.5.0）
    a_id, a_status, a_res = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    assert a_status == "feasible"
    # B：httpkit ~2.2.0（单独求解会传递带入 loggerkit 0.5.0 与 core-lib 1.5.0）
    b_id, b_status, b_res = _make_audit(client, [{"name": "httpkit", "range": "~2.2.0"}])
    assert b_status == "feasible"
    assert b_res == {"httpkit": "2.2.0", "loggerkit": "0.5.0", "core-lib": "1.5.0"}

    resp = _resolve(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested_audit_count"] == 2
    assert body["duplicate_audit_ids"] == []
    assert body["audit_ids"] == [a_id, b_id]
    assert body["status"] == "feasible"
    # 一套可同时安装的版本：两个清单的包全部出现，同名包一个版本
    assert body["resolution"] == {
        "core-lib": "1.5.0",
        "httpkit": "2.2.0",
        "loggerkit": "0.5.0",
    }
    assert body["conflicts"] == []
    assert body["node_count"] >= 1
    # sources 列出每个被选中版本的约束来源
    source_packages = {s["package"] for s in body["sources"]}
    assert source_packages == {"core-lib", "httpkit", "loggerkit"}


def test_shared_resolution_single_shared_version_satisfies_both(client):
    # A：~1.4.0 单独锁定 1.4.0；B：cachekit ^1.0.0 单独把 core-lib 带到 1.5.0。
    # 合并后 cachekit@1.3.0 的 ^1.4.0 与 ~1.4.0 交集为 1.4.0，一套解同时满足两侧。
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "~1.4.0"}])
    b_id, _, _ = _make_audit(client, [{"name": "cachekit", "range": "^1.0.0"}])
    resp = _resolve(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "feasible"
    assert body["resolution"]["core-lib"] == "1.4.0"
    assert body["resolution"]["cachekit"] == "1.3.0"


def test_shared_resolution_root_plugin_transitive_deps(client):
    # 根插件 megaplugin@1.0.0 固定版本并入；其传递依赖须参与共享求解
    root_id, root_status, root_res = _make_audit(
        client, [], plugin_name="megaplugin", plugin_version="1.0.0"
    )
    assert root_status == "feasible"
    assert root_res["megaplugin"] == "1.0.0"
    other_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])

    resp = _resolve(client, [root_id, other_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "feasible"
    resolution = body["resolution"]
    assert resolution["megaplugin"] == "1.0.0"
    # 根插件的传递依赖（authkit/httpkit/...）全部进入同一套版本解
    assert set(resolution) == {
        "megaplugin", "authkit", "cachekit", "httpkit", "loggerkit", "core-lib",
    }
    # 根插件固定版本以精确约束出现在 sources 的来源链中
    megaplugin_source = next(s for s in body["sources"] if s["package"] == "megaplugin")
    assert megaplugin_source["version"] == "1.0.0"
    assert megaplugin_source["requested_by"][0]["version_range"] == "=1.0.0"


def test_shared_resolution_incompatible_ranges_infeasible(client):
    # cachekit ^1.0.0 与 loggerkit ^0.9.0 各自可行，合并后在 core-lib 上冲突无解
    a_id, a_status, _ = _make_audit(client, [{"name": "cachekit", "range": "^1.0.0"}])
    assert a_status == "feasible"
    b_id, b_status, _ = _make_audit(
        client, [{"name": "loggerkit", "range": "^0.9.0"}]
    )
    assert b_status == "feasible"

    resp = _resolve(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["audit_ids"] == [a_id, b_id]
    assert body["status"] == "infeasible"
    assert body["resolution"] is None
    assert body["sources"] is None
    assert body["conflicts"]
    conflict = body["conflicts"][0]
    assert conflict["package"] == "core-lib"
    # 冲突链保留逐跳来源
    assert conflict["minimal_constraints"]
    for constraint in conflict["minimal_constraints"]:
        assert constraint["source_chain"]
        assert constraint["source_chain"][0]["package"] == "<manifest>"


def test_shared_resolution_same_package_different_pins_infeasible(client):
    # 同名包两个不兼容固定版本：只能选一个版本，合并后确定无解
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "1.0.0"}])
    b_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "2.1.0"}])
    resp = _resolve(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "infeasible"
    assert body["resolution"] is None
    assert {c["package"] for c in body["conflicts"]} == {"core-lib"}


def test_shared_resolution_same_package_caret_ranges_infeasible(client):
    # ^1.0.0 与 ^2.0.0 要求同一包选不同主版本：同名包只能选一个 -> 无解
    a_id, a_status, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    assert a_status == "feasible"
    b_id, b_status, _ = _make_audit(client, [{"name": "core-lib", "range": "^2.0.0"}])
    assert b_status == "feasible"
    resp = _resolve(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "infeasible"
    assert body["conflicts"][0]["package"] == "core-lib"


def test_shared_resolution_duplicate_ids_deduped_in_input_order(client):
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    b_id, _, _ = _make_audit(client, [{"name": "httpkit", "range": "~2.2.0"}])

    resp = _resolve(client, [b_id, a_id, b_id, a_id, a_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested_audit_count"] == 5
    assert body["duplicate_audit_ids"] == sorted({a_id, b_id})
    # 去重后按首次出现顺序参与
    assert body["audit_ids"] == [b_id, a_id]
    assert body["status"] == "feasible"


def test_shared_resolution_two_identical_ids_422(client):
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    # 原始 2 条但去重后只剩 1 条：无法构成共享方案
    resp = _resolve(client, [a_id, a_id])
    assert resp.status_code == 422
    assert "去重" in resp.json()["detail"]


def test_shared_resolution_unknown_audit_404(client):
    resp = _resolve(
        client,
        ["00000000-0000-0000-0000-000000000000", "11111111-1111-1111-1111-111111111111"],
    )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "00000000" in detail
    assert "11111111" in detail

    # 一个存在一个不存在：仍 404 且不执行求解
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    b_id, _, _ = _make_audit(client, [{"name": "httpkit", "range": "~2.2.0"}])
    resp = _resolve(
        client,
        [a_id, "22222222-2222-2222-2222-222222222222", b_id],
    )
    assert resp.status_code == 404
    assert "22222222" in resp.json()["detail"]


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

    resp = _resolve(client, [bad_id, good_id])
    assert resp.status_code == 422
    assert bad_id in resp.json()["detail"]

    resp = _resolve(client, [good_id, bad_id])
    assert resp.status_code == 422
    assert bad_id in resp.json()["detail"]


def test_shared_resolution_count_limits_422(client):
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])

    # 空列表
    resp = client.post(PATH, json={"audit_ids": []})
    assert resp.status_code == 422

    # 仅 1 条
    resp = _resolve(client, [a_id])
    assert resp.status_code == 422

    # 超过 20 条（即使全部重复，原始数量也受限）
    resp = client.post(PATH, json={"audit_ids": [a_id] * 21})
    assert resp.status_code == 422

    # 缺字段
    resp = client.post(PATH, json={})
    assert resp.status_code == 422

    # 空 ID
    resp = client.post(PATH, json={"audit_ids": [a_id, "  "]})
    assert resp.status_code == 422


def test_shared_resolution_inconclusive_does_not_claim_infeasible(client, monkeypatch):
    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    b_id, _, _ = _make_audit(client, [{"name": "httpkit", "range": "~2.2.0"}])

    class _ZeroSolver(Solver):
        def __init__(self, registry, node_budget=DEFAULT_NODE_BUDGET):
            super().__init__(registry, node_budget=0)

    monkeypatch.setattr(main_module, "Solver", _ZeroSolver)
    resp = _resolve(client, [a_id, b_id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 预算耗尽：必须保留 inconclusive，resolution/conflicts 均不得宣称无解
    assert body["status"] == "inconclusive"
    assert body["resolution"] is None
    assert body["sources"] is None
    assert body["conflicts"] == []
    assert "预算" in body["explanation"]


def test_shared_resolution_is_read_only(client):
    packages_before = client.get("/api/v1/registry/packages").json()
    audits_before = client.get("/api/v1/audits").json()["total"]

    a_id, _, _ = _make_audit(client, [{"name": "core-lib", "range": "^1.0.0"}])
    b_id, _, _ = _make_audit(client, [{"name": "httpkit", "range": "~2.2.0"}])

    first = _resolve(client, [a_id, b_id]).json()
    second = _resolve(client, [a_id, b_id]).json()
    # 纯只读计算：重复调用字节级稳定
    assert first == second

    assert client.get("/api/v1/registry/packages").json() == packages_before
    # 只有两次 POST /audits 落库，共享求解不产生审计记录
    assert client.get("/api/v1/audits").json()["total"] == audits_before + 2


def test_shared_resolution_listed_in_index(client):
    endpoints = client.get("/").json()["endpoints"]
    assert "POST /api/v1/audits/shared-resolution" in endpoints


def test_shared_resolution_openapi_doc_present(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][PATH]["post"]
    assert operation["tags"] == ["audits"]
    assert "共享" in operation["summary"]
    assert "SharedResolutionResponse" in schema["components"]["schemas"]
    assert "SharedResolutionIn" in schema["components"]["schemas"]
    status_prop = schema["components"]["schemas"]["SharedResolutionResponse"][
        "properties"
    ]["status"]
    assert set(status_prop["enum"]) == {"feasible", "infeasible", "inconclusive"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
