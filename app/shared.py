"""共享安装方案求解（只读、纯内存）。

把多份**已保存可行审计**的原始清单合并为同一组约束，用现有求解器求一套
可同时安装的版本组合：

* 每份清单的显式依赖原样并入根约束；
* 清单声明的根插件转换为 ``=版本`` 的固定约束并入——其注册表中声明的
  传递依赖随求解展开，自然参与约束传播；
* 同名包在解中只能出现一个版本：不同清单对同一包给出不兼容的固定版本
  （或范围交集为空）时，求解器给出 ``range_conflict`` / ``selection_conflict``
  冲突链。

搜索预算耗尽时返回 ``inconclusive``，**不宣称无解**。
整个过程只读：不写审计记录，也不修改注册表。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

Registry = Dict[str, Dict[str, Dict[str, str]]]


def merge_manifest_constraints(manifests: List[Any]) -> List[Tuple[str, str]]:
    """把多份清单的显式依赖与根插件固定版本合并为一组根约束。

    每份清单先贡献其根插件的 ``=版本`` 固定约束（若声明了根插件），
    再按声明顺序贡献显式依赖；清单之间保持输入顺序。重复约束由求解器
    在加约束阶段按（范围 + 来源链）去重。
    """
    merged: List[Tuple[str, str]] = []
    for manifest in manifests:
        if manifest.plugin_name is not None and manifest.plugin_version is not None:
            merged.append((manifest.plugin_name, f"={manifest.plugin_version}"))
        for dep in manifest.dependencies:
            merged.append((dep.name, dep.range))
    return merged


def evaluate_shared_resolution(
    audits: List[Dict[str, Any]],
    registry: Registry,
    solver_factory: Callable[[Registry], Any],
) -> Dict[str, Any]:
    """对去重后的可行审计列表求共享安装方案。

    :param audits: 已保存审计行（调用方负责保证存在且 ``status == feasible``），
        每项需含 ``id`` 与 ``manifest_json``；顺序即输出顺序。
    :param registry: 当前注册表（``{包: {版本: {依赖: 范围}}}``）。
    :param solver_factory: 给定注册表返回求解器的工厂（需提供
        ``solve(root_dependencies, root_name, root_version)``），
        便于测试注入低预算求解器。
    :return: 可直接序列化为 :class:`~app.models.SharedResolutionResponse`
        的响应体（不含请求侧的 ID 计数字段）。
    """
    # 延迟导入避免与 models/solver 的潜在循环依赖
    from .models import ManifestIn

    manifests = [ManifestIn.model_validate(row["manifest_json"]) for row in audits]
    merged = merge_manifest_constraints(manifests)

    solver = solver_factory(registry)
    result = solver.solve(
        root_dependencies=merged,
        root_name=None,
        root_version=None,
    )

    return {
        "status": result.status,
        "resolution": result.resolution,
        "sources": result.sources,
        "conflicts": result.conflicts,
        "node_count": result.node_count,
        "explanation": result.explanation,
        "manifests": [
            {
                "audit_id": row["id"],
                "plugin_name": manifest.plugin_name,
                "plugin_version": manifest.plugin_version,
                "dependencies": [
                    {"name": dep.name, "range": dep.range}
                    for dep in manifest.dependencies
                ],
            }
            for row, manifest in zip(audits, manifests)
        ],
        "merged_constraints": [
            {"name": name, "range": range_text} for name, range_text in merged
        ],
    }
