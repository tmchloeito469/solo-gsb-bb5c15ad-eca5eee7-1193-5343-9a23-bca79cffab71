"""共享安装方案求解（只读、纯内存）。

把多条已保存可行审计的原始清单合并为**同一组根约束**，调用现有求解器
求出一套可同时安装的版本组合：

* 每条清单的显式依赖（包名 + SemVer 范围）原样并入；
* 每条清单的根插件以 ``=版本`` 精确约束并入——求解器选中该版本后会自动
  展开其注册表依赖，因此根插件的**传递依赖同样参与求解**；
* 同名包在解中只会被锁定一个版本；两条清单对同一包提出不兼容约束
  （例如不同的固定版本）时，合并约束直接冲突，整体 ``infeasible``；
* 搜索预算耗尽时求解器返回 ``inconclusive``，原样透传，**不宣称无解**。

整个过程只在内存中进行：不写审计记录，也不修改注册表。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Tuple

if TYPE_CHECKING:  # 避免与 models/solver 的潜在循环依赖
    from .models import ManifestIn
    from .solver import SolveResult

Registry = Dict[str, Dict[str, Dict[str, str]]]


def merge_shared_constraints(manifests: List["ManifestIn"]) -> List[Tuple[str, str]]:
    """把各清单的显式依赖与根插件固定版本合并为一组根约束。

    保持输入顺序：先按清单顺序、再按清单内声明顺序；根插件表示为
    ``(plugin_name, "=<plugin_version>")`` 精确约束，排在该清单显式依赖之后。
    完全相同的约束由求解器内部按签名去重。
    """
    merged: List[Tuple[str, str]] = []
    for manifest in manifests:
        for dep in manifest.dependencies:
            merged.append((dep.name, dep.range))
        if manifest.plugin_name is not None:
            merged.append((manifest.plugin_name, f"={manifest.plugin_version}"))
    return merged


def solve_shared_resolution(
    manifests: List["ManifestIn"],
    registry: Registry,
    solver_factory: Callable[[Registry], Any],
) -> "SolveResult":
    """对合并后的约束运行一次求解，返回求解器的 ``SolveResult``。

    :param manifests: 已校验的 :class:`~app.models.ManifestIn` 列表
        （来自各审计保存时的原始清单，顺序即约束并入顺序）。
    :param registry: 当前注册表（``{包: {版本: {依赖: 范围}}}``）。
    :param solver_factory: 给定注册表返回求解器的工厂（需提供
        ``solve(root_dependencies, root_name, root_version)``），
        便于测试注入低预算求解器。
    """
    solver = solver_factory(registry)
    return solver.solve(
        root_dependencies=merge_shared_constraints(manifests),
        root_name=None,
        root_version=None,
    )
