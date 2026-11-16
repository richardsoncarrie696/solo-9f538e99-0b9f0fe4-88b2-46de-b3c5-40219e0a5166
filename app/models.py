"""请求与响应的 Pydantic 模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _non_empty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("字符串不能为空")
    return value


# ---------- 请求模型 ----------

class ObservationIn(BaseModel):
    code: str = Field(..., min_length=1, description="观测唯一编号")
    name: str = Field(..., min_length=1, description="野外记录的物种名（可能是旧名/同义名）")

    @field_validator("code", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _non_empty(v)


class BatchCreate(BaseModel):
    observations: list[ObservationIn] = Field(
        ..., min_length=1, max_length=100, description="一批观测，至多 100 条"
    )
    note: str | None = Field(None, description="备注")


class EdgeIn(BaseModel):
    synonym: str = Field(..., min_length=1, description="同义名（旧名）")
    target: str = Field(..., min_length=1, description="指向的名称（接受名或另一同义名）")

    @field_validator("synonym", "target")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _non_empty(v)


class TaxonomyCreate(BaseModel):
    accepted_names: list[str] = Field(..., min_length=1, description="接受名列表")
    synonym_edges: list[EdgeIn] = Field(
        default_factory=list, max_length=40, description="同义名指向关系，至多 40 条，允许分叉"
    )
    note: str | None = None

    @field_validator("accepted_names")
    @classmethod
    def _strip_names(cls, values: list[str]) -> list[str]:
        cleaned = [_non_empty(v) for v in values]
        return cleaned


class ResultCreate(BaseModel):
    batch_id: int = Field(..., description="观测批次 ID")
    revision_id: int | None = Field(None, description="分类表修订 ID，缺省使用当前最新修订")


class RichnessAnalysisCreate(BaseModel):
    result_id: int = Field(..., description="当前供数结果号（分析固定使用该结果所属的分类表修订）")


# ---------- 响应模型 ----------

class ObservationOut(BaseModel):
    line_no: int
    code: str
    name: str


class BatchOut(BaseModel):
    id: int
    note: str | None
    created_at: str
    observation_count: int
    observations: list[ObservationOut]


class BatchSummaryOut(BaseModel):
    id: int
    note: str | None
    created_at: str
    observation_count: int


class EdgeOut(BaseModel):
    synonym: str
    target: str


class TaxonomyOut(BaseModel):
    revision_id: int
    note: str | None
    created_at: str
    superseded_at: str | None
    accepted_count: int
    edge_count: int
    accepted_names: list[str]
    synonym_edges: list[EdgeOut]


class TaxonomySummaryOut(BaseModel):
    revision_id: int
    note: str | None
    created_at: str
    superseded_at: str | None
    accepted_count: int
    edge_count: int


class ResultItemOut(BaseModel):
    line_no: int
    observation_code: str
    input_name: str
    item_status: str
    accepted_name: str | None
    reason_code: str | None
    reason: str | None


class ResultSummaryOut(BaseModel):
    result_id: int
    batch_id: int
    revision_id: int
    status: str
    created_at: str
    included_count: int
    pending_count: int


class ResultDetailOut(ResultSummaryOut):
    items: list[ResultItemOut]


# ---------- 丰富度分析 ----------

class NameChoiceOut(BaseModel):
    """一种观测名的一个候选接受名归属及代表观测。"""

    observation_name: str = Field(..., description="观测名（去重后的一种名称）")
    accepted_name: str = Field(..., description="该观测名在本次归属中选定的候选接受名")
    observation_codes: list[str] = Field(
        ..., description="同名的全部观测编号（同名记录必须同选，故一并列出）"
    )


class RichnessWitnessOut(BaseModel):
    """一种合法候选归属的完整见证。"""

    accepted_names: list[str] = Field(..., description="该方案下全部被使用的不同接受名")
    accepted_name_count: int = Field(..., description="不同接受名总数（去重后）")
    assignments: list[NameChoiceOut] = Field(
        ..., description="每个观测名 -> 候选接受名的完整归属"
    )


class RichnessObservationOut(BaseModel):
    observation_code: str
    input_name: str
    source_item_status: str = Field(..., description="来源供数结果中的原状态")
    source_accepted_name: str | None = Field(
        None, description="来源供数结果中已纳入记录的接受名（保留原归属）"
    )
    candidate_accepted_names: list[str] = Field(
        ..., description="该观测名可达的接受名终点（已纳入记录为保留原归属）"
    )
    attribution_fixed: bool = Field(
        ...,
        description=(
            "该观测名在来源结果中已有纳入记录，归属固定为原接受名、不参与选择；"
            "无论是否固定，其接受名都计入丰富度"
        ),
    )


class RichnessAnalysisOut(BaseModel):
    analysis_id: int
    result_id: int
    revision_id: int
    status: str
    created_at: str
    observation_name_count: int
    candidate_name_count: int
    lower_bound: int
    upper_bound: int
    observations: list[RichnessObservationOut]
    min_witness: RichnessWitnessOut
    max_witness: RichnessWitnessOut


class RichnessAnalysisSummaryOut(BaseModel):
    analysis_id: int
    result_id: int
    revision_id: int
    status: str
    created_at: str
    observation_name_count: int
    candidate_name_count: int
    lower_bound: int
    upper_bound: int
