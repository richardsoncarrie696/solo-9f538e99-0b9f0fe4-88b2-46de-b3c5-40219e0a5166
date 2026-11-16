"""FastAPI 应用：观测录入、分类表整版更正、供数结果生成与查询。

启动后访问 /docs 可直接在 Swagger UI 中录入、生成并查询结果。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlite3 import Connection

from . import services
from .database import connect, init_db
from .models import (
    BatchCreate,
    BatchOut,
    BatchSummaryOut,
    EdgeOut,
    NameChoiceOut,
    ObservationOut,
    ResultCreate,
    ResultDetailOut,
    ResultItemOut,
    ResultSummaryOut,
    RichnessAnalysisCreate,
    RichnessAnalysisOut,
    RichnessAnalysisSummaryOut,
    RichnessObservationOut,
    RichnessWitnessOut,
    TaxonomyCreate,
    TaxonomyOut,
    TaxonomySummaryOut,
)
from .richness import AnalysisLimitError, UnresolvableNameError
from .services import ConflictError, NotFoundError


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="生态监测名称归并供数 API",
    description=(
        "把野外观测的旧物种名沿分类表同义名关系追踪到接受名。"
        "观测至多 100 条/批；分类表整版更正生成新修订（同义关系至多 40 条，允许分叉）；"
        "结果固定批次与修订，可按结果号重读，更正后旧结果自动过期。"
        "另提供丰富度分析：从当前供数结果固定的修订出发，求观测名候选接受名归属"
        "在不同合法选择下「不同接受名总数」的精确上下界及完整见证。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_conn() -> Connection:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _row_to_batch_out(conn: Connection, batch_id: int):
    row = services.get_batch(conn, batch_id)
    obs_rows = services.list_observations(conn, batch_id)
    return BatchOut(
        id=row["id"],
        note=row["note"],
        created_at=row["created_at"],
        observation_count=len(obs_rows),
        observations=[
            ObservationOut(line_no=r["line_no"], code=r["code"], name=r["name"])
            for r in obs_rows
        ],
    )


@app.get("/health", tags=["系统"], summary="健康检查")
def health():
    return {"status": "ok"}


# ---------- 观测批次 ----------

@app.post(
    "/batches",
    response_model=BatchOut,
    status_code=201,
    tags=["观测批次"],
    summary="录入一批观测（至多 100 条，编号在批内唯一）",
)
def create_batch(payload: BatchCreate, conn: Connection = Depends(get_conn)):
    try:
        batch_id = services.create_batch(conn, payload)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _row_to_batch_out(conn, batch_id)


@app.get(
    "/batches",
    response_model=list[BatchSummaryOut],
    tags=["观测批次"],
    summary="列出观测批次",
)
def list_batches(conn: Connection = Depends(get_conn)):
    return [dict(r) for r in services.list_batches(conn)]


@app.get(
    "/batches/{batch_id}",
    response_model=BatchOut,
    tags=["观测批次"],
    summary="读取一个观测批次（含全部观测）",
)
def get_batch(batch_id: int, conn: Connection = Depends(get_conn)):
    if services.get_batch(conn, batch_id) is None:
        raise HTTPException(status_code=404, detail=f"观测批次不存在：batch_id={batch_id}")
    return _row_to_batch_out(conn, batch_id)


# ---------- 分类表修订 ----------

@app.post(
    "/taxonomies",
    response_model=TaxonomyOut,
    status_code=201,
    tags=["分类表"],
    summary="整版提交分类表（自动生成新修订，旧修订与旧结果过期）",
)
def create_taxonomy(payload: TaxonomyCreate, conn: Connection = Depends(get_conn)):
    try:
        revision_id = services.create_taxonomy_revision(conn, payload)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _row_to_taxonomy_out(conn, revision_id)


def _row_to_taxonomy_out(conn: Connection, revision_id: int) -> TaxonomyOut:
    row = services.get_revision(conn, revision_id)
    accepted = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM accepted_name WHERE revision_id = ? ORDER BY rowid",
            (revision_id,),
        )
    ]
    edges = [
        EdgeOut(synonym=r["synonym"], target=r["target"])
        for r in conn.execute(
            "SELECT synonym, target FROM synonym_edge WHERE revision_id = ? ORDER BY rowid",
            (revision_id,),
        )
    ]
    return TaxonomyOut(
        revision_id=row["revision_id"],
        note=row["note"],
        created_at=row["created_at"],
        superseded_at=row["superseded_at"],
        accepted_count=len(accepted),
        edge_count=len(edges),
        accepted_names=accepted,
        synonym_edges=edges,
    )


@app.get(
    "/taxonomies",
    response_model=list[TaxonomySummaryOut],
    tags=["分类表"],
    summary="列出分类表修订",
)
def list_taxonomies(conn: Connection = Depends(get_conn)):
    return [dict(r) for r in services.list_revisions(conn)]


@app.get(
    "/taxonomies/{revision_id}",
    response_model=TaxonomyOut,
    tags=["分类表"],
    summary="读取一个分类表修订的完整内容",
)
def get_taxonomy(revision_id: int, conn: Connection = Depends(get_conn)):
    if services.get_revision(conn, revision_id) is None:
        raise HTTPException(
            status_code=404, detail=f"分类表修订不存在：revision_id={revision_id}"
        )
    return _row_to_taxonomy_out(conn, revision_id)


# ---------- 供数结果 ----------

@app.post(
    "/results",
    response_model=ResultDetailOut,
    status_code=201,
    tags=["供数结果"],
    summary="生成供数结果（固定批次与修订，按结果号可重读）",
)
def generate_result(payload: ResultCreate, conn: Connection = Depends(get_conn)):
    try:
        result_id = services.generate_result(conn, payload)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _row_to_result_detail(conn, result_id)


def _row_to_result_detail(conn: Connection, result_id: int) -> ResultDetailOut:
    row = services.get_result(conn, result_id)
    items = services.list_result_items(conn, result_id)
    return ResultDetailOut(
        result_id=row["result_id"],
        batch_id=row["batch_id"],
        revision_id=row["revision_id"],
        status=row["status"],
        created_at=row["created_at"],
        included_count=row["included_count"],
        pending_count=row["pending_count"],
        items=[
            ResultItemOut(
                line_no=r["line_no"],
                observation_code=r["observation_code"],
                input_name=r["input_name"],
                item_status=r["item_status"],
                accepted_name=r["accepted_name"],
                reason_code=r["reason_code"],
                reason=r["reason"],
            )
            for r in items
        ],
    )


@app.get(
    "/results",
    response_model=list[ResultSummaryOut],
    tags=["供数结果"],
    summary="列出供数结果（可按批次、状态过滤）",
)
def list_results(
    conn: Connection = Depends(get_conn),
    batch_id: int | None = Query(None, description="按观测批次过滤"),
    status: str | None = Query(None, pattern="^(active|expired)$", description="active/expired"),
):
    return [dict(r) for r in services.list_results(conn, batch_id, status)]


@app.get(
    "/results/{result_id}",
    response_model=ResultDetailOut,
    tags=["供数结果"],
    summary="按结果号重读供数结果（过期结果仍可读取，status 标记为 expired）",
)
def get_result(result_id: int, conn: Connection = Depends(get_conn)):
    if services.get_result(conn, result_id) is None:
        raise HTTPException(status_code=404, detail=f"供数结果不存在：result_id={result_id}")
    return _row_to_result_detail(conn, result_id)


# ---------- 丰富度分析 ----------

def _row_to_richness_analysis(conn: Connection, analysis_id: int) -> RichnessAnalysisOut:
    row = services.get_richness_analysis(conn, analysis_id)
    name_rows = services.richness_analysis_names(conn, analysis_id)
    candidates_by_name = {
        r["observation_name"]: json.loads(r["candidate_names_json"]) for r in name_rows
    }
    fixed_by_name = {r["observation_name"]: bool(r["fixed_attribution"]) for r in name_rows}
    observations = [
        RichnessObservationOut(
            observation_code=r["observation_code"],
            input_name=r["input_name"],
            source_item_status=r["source_item_status"],
            source_accepted_name=r["source_accepted_name"],
            candidate_accepted_names=candidates_by_name[r["input_name"]],
            attribution_fixed=fixed_by_name[r["input_name"]],
        )
        for r in services.richness_analysis_observations(conn, analysis_id)
    ]

    def _witness(bound: str) -> RichnessWitnessOut:
        w = services.richness_analysis_witness(conn, analysis_id, bound)
        return RichnessWitnessOut(
            accepted_names=json.loads(w["accepted_names_json"]),
            accepted_name_count=w["accepted_name_count"],
            assignments=[
                NameChoiceOut(**assignment)
                for assignment in json.loads(w["assignments_json"])
            ],
        )

    return RichnessAnalysisOut(
        analysis_id=row["analysis_id"],
        result_id=row["result_id"],
        revision_id=row["revision_id"],
        status=row["status"],
        created_at=row["created_at"],
        observation_name_count=row["observation_name_count"],
        candidate_name_count=row["candidate_name_count"],
        lower_bound=row["lower_bound"],
        upper_bound=row["upper_bound"],
        observations=observations,
        min_witness=_witness("min"),
        max_witness=_witness("max"),
    )


@app.post(
    "/richness-analyses",
    response_model=RichnessAnalysisOut,
    status_code=201,
    tags=["丰富度分析"],
    summary="基于当前供数结果生成丰富度分析（精确上下界 + 两端完整归属见证）",
)
def create_richness_analysis(
    payload: RichnessAnalysisCreate, conn: Connection = Depends(get_conn)
):
    try:
        analysis_id = services.create_richness_analysis(conn, payload.result_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except UnresolvableNameError as exc:
        # 遇环或非接受名终点：整次拒绝，并列出关联观测编号。
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "observations": exc.failures,
            },
        )
    except AnalysisLimitError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"丰富度分析规模超限：观测名 {exc.observation_name_count} 种"
                    f"（上限 20），候选接受名并集 {exc.candidate_name_count} 种（上限 16）"
                ),
                "observation_name_count": exc.observation_name_count,
                "candidate_name_count": exc.candidate_name_count,
                "max_observation_names": 20,
                "max_candidate_names": 16,
            },
        )
    return _row_to_richness_analysis(conn, analysis_id)


@app.get(
    "/richness-analyses",
    response_model=list[RichnessAnalysisSummaryOut],
    tags=["丰富度分析"],
    summary="列出丰富度分析（可按来源结果、状态过滤）",
)
def list_richness_analyses(
    conn: Connection = Depends(get_conn),
    result_id: int | None = Query(None, description="按来源供数结果过滤"),
    status: str | None = Query(None, pattern="^(active|expired)$", description="active/expired"),
):
    return [dict(r) for r in services.list_richness_analyses(conn, result_id, status)]


@app.get(
    "/richness-analyses/{analysis_id}",
    response_model=RichnessAnalysisOut,
    tags=["丰富度分析"],
    summary="按分析号重读（范围与见证不变；来源结果过期则分析标 expired）",
)
def get_richness_analysis(analysis_id: int, conn: Connection = Depends(get_conn)):
    if services.get_richness_analysis(conn, analysis_id) is None:
        raise HTTPException(
            status_code=404, detail=f"丰富度分析不存在：analysis_id={analysis_id}"
        )
    return _row_to_richness_analysis(conn, analysis_id)
