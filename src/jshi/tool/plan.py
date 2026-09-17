"""205 策划口：测里用规则；主流程注入 SkillPlanner。

RulePlanner：need 够长 → echo；过短或空 → PlanFailure。
``normalize_plan`` 把任何 planner 给的多步计划归一成可调度的形状（200 调用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Union

from .contract import AskMode, ToolOrigin, ToolRequest
from .hang import NEED_MIN_CHARS
from .intake import IntakeRecord


@dataclass(frozen=True)
class PlanFailure:
    message: str


@dataclass(frozen=True)
class ToolStep:
    step_id: str
    request: ToolRequest
    depends_on: tuple[str, ...] = ()
    # 只记录模型给的分组，**程序不据此调度**（见《计划落账与调度语义》拍板 3）。
    parallel_group: int = 0


@dataclass(frozen=True)
class ToolPlan:
    plan_id: str
    need: str
    mode: str = "sequential"  # sequential | parallel | mixed
    steps: tuple[ToolStep, ...] = ()


PlanResult = Union[ToolRequest, ToolPlan, PlanFailure]


def normalize_plan(plan: ToolPlan) -> Union[ToolPlan, PlanFailure]:
    """把策划给的多步计划归一成可调度的形状。

    - 步骤编号空 / 重复、依赖引用不存在、依赖成环 → ``PlanFailure``
    - ``mode=sequential`` 且某步没写 ``depends_on`` → 按声明顺序补上「依赖前一步」

    显式 ``depends_on`` 永远优先，不被覆盖。归一化放在这里、由 200 调用，
    所以对任何 planner 都生效（含测试注入的假 planner）。
    """
    steps = plan.steps
    if not steps:
        return PlanFailure("我这边没能定下计划，里面没有步骤。")
    ids = [step.step_id for step in steps]
    if any(not step_id for step_id in ids):
        return PlanFailure("我这边没能定下计划，步骤缺少编号。")
    if len(set(ids)) != len(ids):
        return PlanFailure("我这边没能定下计划，步骤编号重复。")
    known = set(ids)

    normalized: list[ToolStep] = []
    for index, step in enumerate(steps):
        if step.request.ask is AskMode.PROPOSE_ONLY:
            # 「先报价、等人批准」需要有人在计划中途批准，而计划调度没有这个口。
            # 放进去的话这一步永远不会启动，整条计划会静默卡在 running。
            return PlanFailure("我这边没法把「先问过再跑」放进分步计划里。")
        depends = tuple(dep for dep in step.depends_on if dep)
        if plan.mode == "sequential" and index > 0 and not depends:
            # 顺序计划里，漏写依赖的步骤按声明顺序接上前一步。
            depends = (ids[index - 1],)
        for dep in depends:
            if dep not in known:
                return PlanFailure("我这边没能定下计划，步骤依赖不存在。")
        normalized.append(
            ToolStep(
                step_id=step.step_id,
                request=step.request,
                depends_on=depends,
                parallel_group=step.parallel_group,
            )
        )

    if _has_cycle({step.step_id: step.depends_on for step in normalized}):
        return PlanFailure("我这边没能定下计划，步骤之间互相依赖，排不出顺序。")
    return ToolPlan(
        plan_id=plan.plan_id,
        need=plan.need,
        mode=plan.mode,
        steps=tuple(normalized),
    )


def _has_cycle(edges: Mapping[str, tuple[str, ...]]) -> bool:
    """三色 DFS：成环的计划永远等不到就绪，必须当场拒掉。"""
    white, grey, black = 0, 1, 2
    color: dict[str, int] = {node: white for node in edges}

    def visit(node: str) -> bool:
        color[node] = grey
        for dep in edges.get(node, ()):
            state = color.get(dep, black)
            if state == grey:
                return True
            if state == white and visit(dep):
                return True
        color[node] = black
        return False

    return any(color[node] == white and visit(node) for node in list(color))


class ToolPlanner(Protocol):
    def plan(self, intake: IntakeRecord) -> PlanResult: ...


class RulePlanner:
    """规则策划：默认 echo，params 空，ask=execute。"""

    def plan(self, intake: IntakeRecord) -> PlanResult:
        need = (intake.need or "").strip()
        if len(need) < NEED_MIN_CHARS:
            return PlanFailure("我这边没弄清你要什么，说得太短了。")
        origin = ToolOrigin.EXTERNAL_05
        raw_origin = (intake.origin or "").strip()
        if raw_origin:
            try:
                origin = ToolOrigin(raw_origin)
            except ValueError:
                origin = ToolOrigin.EXTERNAL_05
        return ToolRequest(
            subject_id=intake.subject_id,
            activity_id=intake.activity_id,
            origin=origin,
            need=need,
            template="generic",
            command="echo",
            params={},
            ask=AskMode.EXECUTE,
        )
