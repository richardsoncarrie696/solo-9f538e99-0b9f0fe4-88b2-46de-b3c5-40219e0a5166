"""丰富度分析：在固定分类修订下，求观测名候选接受名归属的精确上下界。

分析范围来自一份供数结果（及其固定的分类表修订）：

- 对每种观测名求其可达的接受名终点；任一观测名遇环或存在非接受名终点，
  整次分析拒绝，并列出相关观测编号。
- 每种观测名必须在候选接受名中选一个；同名记录必须同选。
- 来源结果中已纳入的记录保持原归属（固定），其接受名计入丰富度。
- 在全部合法选择中，求「被使用的不同接受名总数」的精确最小值与最大值，
  并给出两端完整的名称归属见证。

约束：观测名至多 20 种，候选接受名并集至多 16 种（超限拒绝）。

精确性论证：

- **下界 = 最小集合覆盖。** 任一合法归属所用接受名集合必须与每种观测名的候选集
  相交，故其大小不小于覆盖数；反过来，在最小覆盖内为每种名称任取一个候选名，
  所得并集若更小则与最小性矛盾，因此恰好等于该覆盖。于是最小覆盖大小即精确下界。
- **上界 = 二分图最大匹配（值为接受名、观测名为资源）。** 一次归属中的每个不同
  接受名都需由一个不同观测名选出（一种观测名只选一个），故使用数不超过最大匹配；
  反过来，按最大匹配认领后，未匹配的观测名任取其候选名：该候选名若未被匹配覆盖，
  就能与它配成更大匹配，矛盾，故不会增加取值集合。于是最大匹配大小即精确上界。

两个界都在候选子集/匹配上求得，均为多项式时间且精确（非启发式）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

MAX_OBSERVATION_NAMES = 20
MAX_CANDIDATE_NAMES = 16


class UnresolvableNameError(Exception):
    """存在环或非接受名终点，整次分析拒绝。"""

    def __init__(self, failures: list[dict]):
        super().__init__("存在环或非接受名终点，无法进行丰富度分析")
        self.failures = failures


class AnalysisLimitError(Exception):
    """观测名种类或候选接受名并集超过上限。"""

    def __init__(
        self,
        observation_name_count: int,
        candidate_name_count: int,
    ):
        super().__init__("丰富度分析规模超过上限")
        self.observation_name_count = observation_name_count
        self.candidate_name_count = candidate_name_count


@dataclass(frozen=True)
class ObservationScope:
    line_no: int
    observation_code: str
    input_name: str
    source_item_status: str
    source_accepted_name: str | None
    attribution_fixed: bool
    candidate_accepted_names: tuple[str, ...]


@dataclass(frozen=True)
class NameScope:
    observation_name: str
    candidate_accepted_names: tuple[str, ...]
    fixed: bool
    observation_codes: tuple[str, ...]


@dataclass(frozen=True)
class Witness:
    accepted_names: tuple[str, ...]
    accepted_name_count: int
    assignments: tuple[tuple[str, str, tuple[str, ...]], ...] = field(default=())

    def as_dict(self) -> dict:
        return {
            "accepted_names": list(self.accepted_names),
            "accepted_name_count": self.accepted_name_count,
            "assignments": [
                {
                    "observation_name": name,
                    "accepted_name": accepted,
                    "observation_codes": list(codes),
                }
                for name, accepted, codes in self.assignments
            ],
        }


@dataclass(frozen=True)
class RichnessComputation:
    observation_names: tuple[NameScope, ...]
    observations: tuple[ObservationScope, ...]
    lower_bound: int
    upper_bound: int
    min_witness: Witness
    max_witness: Witness

    @property
    def name_count(self) -> int:
        return len(self.observation_names)

    @property
    def candidate_count(self) -> int:
        return len(
            {name for scope in self.observation_names for name in scope.candidate_accepted_names}
        )


def _build_scope(
    items: list, accepted: set[str], adjacency: dict[str, list[str]], analyze
) -> RichnessComputation:
    """根据来源结果的逐条明细构造分析范围并求上下界。"""
    names_in_order: list[str] = []
    items_by_name: dict[str, list] = {}
    for item in items:
        name = item["input_name"]
        if name not in items_by_name:
            names_in_order.append(name)
            items_by_name[name] = []
        items_by_name[name].append(item)

    failures: list[dict] = []
    scopes: list[NameScope] = []
    candidates_by_name: dict[str, tuple[str, ...]] = {}
    candidate_union: set[str] = set()

    for name in names_in_order:
        group = items_by_name[name]
        endpoints = analyze(name, adjacency, accepted)
        if endpoints.has_cycle or endpoints.non_accepted_terminals:
            reason_codes = []
            if endpoints.has_cycle:
                reason_codes.append("cycle")
            if endpoints.non_accepted_terminals:
                reason_codes.append("no_accepted_name")
            for item in group:
                failures.append(
                    {
                        "observation_code": item["observation_code"],
                        "input_name": name,
                        "reason_codes": reason_codes,
                        "non_accepted_terminals": list(endpoints.non_accepted_terminals),
                    }
                )
            continue

        included = [item for item in group if item["item_status"] == "included"]
        if included:
            fixed_names = {item["accepted_name"] for item in included}
            if (
                len(fixed_names) != 1
                or None in fixed_names
                or not fixed_names.issubset(set(endpoints.accepted_terminals))
            ):
                # 解析器对同一名称是确定性的；出现这种情况说明来源结果被破坏。
                for item in group:
                    failures.append(
                        {
                            "observation_code": item["observation_code"],
                            "input_name": name,
                            "reason_codes": ["inconsistent_source_result"],
                            "non_accepted_terminals": [],
                        }
                    )
                continue
            # 已纳入记录保持原归属：候选集收缩为来源结果记录的接受名。
            candidates = tuple(sorted(fixed_names))
            fixed = True
        else:
            candidates = tuple(endpoints.accepted_terminals)
            fixed = False
        candidates_by_name[name] = candidates
        candidate_union.update(candidates)
        scopes.append(
            NameScope(
                observation_name=name,
                candidate_accepted_names=candidates,
                fixed=fixed,
                observation_codes=tuple(item["observation_code"] for item in group),
            )
        )

    if failures:
        raise UnresolvableNameError(failures)
    if len(names_in_order) > MAX_OBSERVATION_NAMES:
        raise AnalysisLimitError(len(names_in_order), len(candidate_union))
    if len(candidate_union) > MAX_CANDIDATE_NAMES:
        raise AnalysisLimitError(len(names_in_order), len(candidate_union))

    observations = tuple(
        ObservationScope(
            line_no=item["line_no"],
            observation_code=item["observation_code"],
            input_name=item["input_name"],
            source_item_status=item["item_status"],
            source_accepted_name=item["accepted_name"],
            attribution_fixed=item["item_status"] == "included",
            candidate_accepted_names=candidates_by_name[item["input_name"]],
        )
        for item in items
    )

    min_witness, max_witness = _solve(scopes)
    return RichnessComputation(
        observation_names=tuple(scopes),
        observations=observations,
        lower_bound=min_witness.accepted_name_count,
        upper_bound=max_witness.accepted_name_count,
        min_witness=min_witness,
        max_witness=max_witness,
    )


def compute_richness(
    items: list,
    accepted: set[str],
    adjacency: dict[str, list[str]],
    analyze,
) -> RichnessComputation:
    """纯计算入口；``analyze`` 为 ``resolver.analyze_endpoints``。"""
    return _build_scope(items, accepted, adjacency, analyze)


def _assign_within(
    subset: frozenset[str], scopes: list[NameScope]
) -> dict[str, str]:
    """在给定取值集合内为每种观测名确定性选一个候选名（前提：并集覆盖全部名称）。"""
    assignment: dict[str, str] = {}
    for scope in scopes:
        inside = sorted(set(scope.candidate_accepted_names) & subset)
        assignment[scope.observation_name] = inside[0]
    return assignment


def _witness(
    subset: frozenset[str], scopes: list[NameScope], assignment: dict[str, str]
) -> Witness:
    scope_by_name = {scope.observation_name: scope for scope in scopes}
    return Witness(
        accepted_names=tuple(sorted(subset)),
        accepted_name_count=len(subset),
        assignments=tuple(
            (
                name,
                assignment[name],
                scope_by_name[name].observation_codes,
            )
            for name in sorted(
                assignment,
                key=lambda name: next(
                    idx for idx, s in enumerate(scopes) if s.observation_name == name
                ),
            )
        ),
    )


def _minimum_cover(scopes: list[NameScope], universe: list[str]) -> Witness:
    """最小不同接受名数：选取最少的接受名，使每种观测名都能在其中取值。

    任一合法归属的取值并集必然覆盖全部观测名；反过来，在最小覆盖内为每种名称
    取一个候选名，所得并集必然等于该覆盖（若小于覆盖则与最小性矛盾）。
    因此最小覆盖大小即精确下界。
    """
    if not scopes:
        return Witness(accepted_names=(), accepted_name_count=0, assignments=())
    for size in range(0, len(universe) + 1):
        for pick in combinations(universe, size):
            subset = frozenset(pick)
            if all(
                set(scope.candidate_accepted_names) & subset for scope in scopes
            ):
                assignment = _assign_within(subset, scopes)
                assert set(assignment.values()) == subset
                return _witness(subset, scopes, assignment)
    raise AssertionError("候选名并集必然覆盖全部观测名")  # pragma: no cover


def _maximum_distinct(scopes: list[NameScope], universe: list[str]) -> Witness:
    """最大不同接受名数：值为接受名、观测名为资源的二分图最大匹配。

    一次归属使用的每个不同接受名都需要一个不同观测名认领（一种观测名只选一个），
    故使用数不超过最大匹配；反过来，按最大匹配认领后其余观测名任取候选名，
    不会超出匹配覆盖的取值集合，因此最大匹配数即精确上界。
    """
    if not scopes:
        return Witness(accepted_names=(), accepted_name_count=0, assignments=())

    match_scope: dict[int, str] = {}

    def try_assign(value: str, seen: set[int]) -> bool:
        for idx, scope in enumerate(scopes):
            if idx in seen or value not in scope.candidate_accepted_names:
                continue
            seen.add(idx)
            current = match_scope.get(idx)
            if current is None or try_assign(current, seen):
                match_scope[idx] = value
                return True
        return False

    for value in universe:
        try_assign(value, set())

    matched_values = frozenset(match_scope.values())
    assignment: dict[str, str] = {}
    for idx, scope in enumerate(scopes):
        if idx in match_scope:
            assignment[scope.observation_name] = match_scope[idx]
        else:
            # 未参与匹配的观测名，其任一候选名必定已被匹配值覆盖；
            # 否则该值与它可组成更大的匹配，与最大性矛盾。
            picked = scope.candidate_accepted_names[0]
            assert picked in matched_values
            assignment[scope.observation_name] = picked
    used = frozenset(assignment.values())
    assert used == matched_values
    return _witness(used, scopes, assignment)


def _solve(scopes: list[NameScope]) -> tuple[Witness, Witness]:
    """精确求不同接受名总数的上下界及两端见证。"""
    universe = sorted(
        {name for scope in scopes for name in scope.candidate_accepted_names}
    )
    return _minimum_cover(scopes, universe), _maximum_distinct(scopes, universe)
