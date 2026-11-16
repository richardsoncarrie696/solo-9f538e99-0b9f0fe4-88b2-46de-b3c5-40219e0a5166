"""业务服务：所有写操作均在单个 IMMEDIATE 事务内完成，失败整体回滚。"""
from __future__ import annotations

import json
import sqlite3

from .database import immediate_tx
from .models import BatchCreate, ResultCreate, TaxonomyCreate
from .resolver import analyze_endpoints, resolve_name
from .richness import RichnessComputation, compute_richness


class ConflictError(Exception):
    """409 冲突：唯一编号重复、来源结果不是当前供数等。"""


class NotFoundError(Exception):
    """404：引用的批次、修订、结果或分析不存在。"""


def _expire_analyses_of_expired_results(conn: sqlite3.Connection) -> None:
    """来源结果过期的分析一并过期。"""
    conn.execute(
        """
        UPDATE richness_analysis
           SET status = 'expired'
         WHERE status = 'active'
           AND result_id IN (SELECT result_id FROM result WHERE status = 'expired')
        """
    )


# ---------- 观测批次 ----------

def create_batch(conn: sqlite3.Connection, payload: BatchCreate) -> int:
    seen: set[str] = set()
    for obs in payload.observations:
        if obs.code in seen:
            raise ConflictError(f"批次内观测编号重复：{obs.code}")
        seen.add(obs.code)

    with immediate_tx(conn):
        cur = conn.execute(
            "INSERT INTO observation_batch(note) VALUES (?)", (payload.note,)
        )
        batch_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO observation(batch_id, code, name, line_no) VALUES (?, ?, ?, ?)",
            [
                (batch_id, obs.code, obs.name, idx)
                for idx, obs in enumerate(payload.observations, start=1)
            ],
        )
    return batch_id


def get_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM observation_batch WHERE id = ?", (batch_id,)
    ).fetchone()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT b.id, b.note, b.created_at, COUNT(o.code) AS observation_count
        FROM observation_batch b
        LEFT JOIN observation o ON o.batch_id = b.id
        GROUP BY b.id
        ORDER BY b.id
        """
    ).fetchall()


def list_observations(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT line_no, code, name FROM observation WHERE batch_id = ? ORDER BY line_no",
        (batch_id,),
    ).fetchall()


# ---------- 分类表（整版更正 -> 新修订） ----------

def create_taxonomy_revision(
    conn: sqlite3.Connection, payload: TaxonomyCreate
) -> int:
    seen_names: set[str] = set()
    for name in payload.accepted_names:
        if name in seen_names:
            raise ConflictError(f"接受名重复：{name}")
        seen_names.add(name)

    seen_edges: set[tuple[str, str]] = set()
    for edge in payload.synonym_edges:
        key = (edge.synonym, edge.target)
        if key in seen_edges:
            raise ConflictError(f"同义名指向关系重复：{edge.synonym} -> {edge.target}")
        seen_edges.add(key)

    with immediate_tx(conn):
        cur = conn.execute(
            "INSERT INTO taxonomy_revision(note) VALUES (?)", (payload.note,)
        )
        revision_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO accepted_name(revision_id, name) VALUES (?, ?)",
            [(revision_id, name) for name in payload.accepted_names],
        )
        conn.executemany(
            "INSERT INTO synonym_edge(revision_id, synonym, target) VALUES (?, ?, ?)",
            [(revision_id, e.synonym, e.target) for e in payload.synonym_edges],
        )
        # 整版更正：旧修订标记 superseded，引用旧修订的有效结果立即过期
        conn.execute(
            """
            UPDATE taxonomy_revision
               SET superseded_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE revision_id != ? AND superseded_at IS NULL
            """,
            (revision_id,),
        )
        conn.execute(
            """
            UPDATE result
               SET status = 'expired'
             WHERE status = 'active' AND revision_id != ?
            """,
            (revision_id,),
        )
        _expire_analyses_of_expired_results(conn)
    return revision_id


def get_revision(conn: sqlite3.Connection, revision_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM taxonomy_revision WHERE revision_id = ?", (revision_id,)
    ).fetchone()


def latest_revision_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT MAX(revision_id) AS rid FROM taxonomy_revision"
    ).fetchone()
    return row["rid"]


def list_revisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.revision_id, r.note, r.created_at, r.superseded_at,
               (SELECT COUNT(*) FROM accepted_name a WHERE a.revision_id = r.revision_id) AS accepted_count,
               (SELECT COUNT(*) FROM synonym_edge e WHERE e.revision_id = r.revision_id) AS edge_count
          FROM taxonomy_revision r
         ORDER BY r.revision_id
        """
    ).fetchall()


def _load_revision_graph(
    conn: sqlite3.Connection, revision_id: int
) -> tuple[set[str], dict[str, list[str]]]:
    accepted = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM accepted_name WHERE revision_id = ?", (revision_id,)
        )
    }
    adjacency: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT synonym, target FROM synonym_edge WHERE revision_id = ?",
        (revision_id,),
    ):
        adjacency.setdefault(row["synonym"], []).append(row["target"])
    return accepted, adjacency


# ---------- 供数结果 ----------

def generate_result(conn: sqlite3.Connection, payload: ResultCreate) -> int:
    if get_batch(conn, payload.batch_id) is None:
        raise NotFoundError(f"观测批次不存在：batch_id={payload.batch_id}")

    revision_id = payload.revision_id
    if revision_id is None:
        revision_id = latest_revision_id(conn)
        if revision_id is None:
            raise NotFoundError("尚无分类表修订，无法生成供数结果")
    elif get_revision(conn, revision_id) is None:
        raise NotFoundError(f"分类表修订不存在：revision_id={revision_id}")

    observations = list_observations(conn, payload.batch_id)

    with immediate_tx(conn):
        # 事务内重新确认修订状态：旧修订不得作为当前供数
        revision = conn.execute(
            "SELECT revision_id, superseded_at FROM taxonomy_revision WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        current = conn.execute(
            "SELECT MAX(revision_id) AS rid FROM taxonomy_revision"
        ).fetchone()["rid"]
        is_current = current == revision_id

        accepted, adjacency = _load_revision_graph(conn, revision_id)

        cur = conn.execute(
            """
            INSERT INTO result(batch_id, revision_id, status, included_count, pending_count)
            VALUES (?, ?, 'active', 0, 0)
            """,
            (payload.batch_id, revision_id),
        )
        result_id = cur.lastrowid

        items = []
        included = pending = 0
        for obs in observations:
            r = resolve_name(obs["name"], adjacency, accepted)
            if r.included:
                included += 1
                items.append(
                    (result_id, obs["line_no"], obs["code"], obs["name"],
                     "included", r.accepted_name, None, None)
                )
            else:
                pending += 1
                items.append(
                    (result_id, obs["line_no"], obs["code"], obs["name"],
                     "pending", None, r.reason_code, r.reason)
                )

        conn.executemany(
            """
            INSERT INTO result_item(result_id, line_no, observation_code, input_name,
                                    item_status, accepted_name, reason_code, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            items,
        )
        conn.execute(
            "UPDATE result SET included_count = ?, pending_count = ? WHERE result_id = ?",
            (included, pending, result_id),
        )

        # 若该修订已被更正（交错场景），结果直接落为 expired；
        # 只有结果本身是当前供数时，才使同批次旧的 active 结果过期，
        # 保证旧修订生成的结果绝不会覆盖当前供数，且每批次至多一份 active。
        if not is_current or revision["superseded_at"] is not None:
            conn.execute(
                "UPDATE result SET status = 'expired' WHERE result_id = ?",
                (result_id,),
            )
        else:
            conn.execute(
                """
                UPDATE result
                   SET status = 'expired'
                 WHERE status = 'active'
                   AND batch_id = ? AND result_id != ?
                """,
                (payload.batch_id, result_id),
            )
        _expire_analyses_of_expired_results(conn)
    return result_id


def get_result(conn: sqlite3.Connection, result_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM result WHERE result_id = ?", (result_id,)
    ).fetchone()


def list_result_items(conn: sqlite3.Connection, result_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM result_item WHERE result_id = ? ORDER BY line_no", (result_id,)
    ).fetchall()


def list_results(
    conn: sqlite3.Connection, batch_id: int | None = None, status: str | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM result WHERE 1=1"
    params: list = []
    if batch_id is not None:
        sql += " AND batch_id = ?"
        params.append(batch_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY result_id"
    return conn.execute(sql, params).fetchall()


# ---------- 丰富度分析 ----------

def _load_result_items(conn: sqlite3.Connection, result_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM result_item WHERE result_id = ? ORDER BY line_no", (result_id,)
        )
    ]


def _insert_analysis(
    conn: sqlite3.Connection,
    result_id: int,
    revision_id: int,
    computation: RichnessComputation,
    status: str = "active",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO richness_analysis(
            result_id, revision_id, status,
            observation_name_count, candidate_name_count, lower_bound, upper_bound
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result_id,
            revision_id,
            status,
            computation.name_count,
            computation.candidate_count,
            computation.lower_bound,
            computation.upper_bound,
        ),
    )
    analysis_id = cur.lastrowid
    conn.executemany(
        """
        INSERT INTO richness_analysis_name(
            analysis_id, observation_name, candidate_names_json, fixed_attribution
        ) VALUES (?, ?, ?, ?)
        """,
        [
            (
                analysis_id,
                scope.observation_name,
                json.dumps(list(scope.candidate_accepted_names), ensure_ascii=False),
                1 if scope.fixed else 0,
            )
            for scope in computation.observation_names
        ],
    )
    conn.executemany(
        """
        INSERT INTO richness_analysis_observation(
            analysis_id, line_no, observation_code, input_name,
            source_item_status, source_accepted_name, attribution_fixed
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                analysis_id,
                obs.line_no,
                obs.observation_code,
                obs.input_name,
                obs.source_item_status,
                obs.source_accepted_name,
                1 if obs.attribution_fixed else 0,
            )
            for obs in computation.observations
        ],
    )
    conn.executemany(
        """
        INSERT INTO richness_analysis_witness(
            analysis_id, bound, accepted_names_json, assignments_json, accepted_name_count
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                analysis_id,
                bound,
                json.dumps(list(witness.accepted_names), ensure_ascii=False),
                json.dumps(
                    [
                        {
                            "observation_name": name,
                            "accepted_name": accepted,
                            "observation_codes": list(codes),
                        }
                        for name, accepted, codes in witness.assignments
                    ],
                    ensure_ascii=False,
                ),
                witness.accepted_name_count,
            )
            for bound, witness in (
                ("min", computation.min_witness),
                ("max", computation.max_witness),
            )
        ],
    )
    return analysis_id


def create_richness_analysis(conn: sqlite3.Connection, result_id: int) -> int:
    """基于供数结果生成丰富度分析。

    - 只读来源结果与固定修订，不改变任何 result / result_item 行；
    - 计算在事务外完成（纯读、修订不可变），计算期间分类更正仍可完成；
    - 计算前来源必须是当前供数；计算期间来源过期（“迟到保存”）时，
      分析仍按已固定范围与见证落库并标为 expired。
    - 任何失败整体回滚，不留下部分分析。
    """
    result = get_result(conn, result_id)
    if result is None:
        raise NotFoundError(f"供数结果不存在：result_id={result_id}")
    if result["status"] != "active":
        raise ConflictError(
            f"供数结果已过期，不能作为丰富度分析来源：result_id={result_id}"
        )

    revision_id = result["revision_id"]
    items = _load_result_items(conn, result_id)
    accepted, adjacency = _load_revision_graph(conn, revision_id)
    computation = compute_richness(items, accepted, adjacency, analyze_endpoints)

    with immediate_tx(conn):
        current = conn.execute(
            "SELECT status FROM result WHERE result_id = ?", (result_id,)
        ).fetchone()
        if current is None:
            raise NotFoundError(f"供数结果不存在：result_id={result_id}")
        latest = conn.execute(
            "SELECT MAX(revision_id) AS rid FROM taxonomy_revision"
        ).fetchone()["rid"]
        still_current = current["status"] == "active" and latest == revision_id
        analysis_id = _insert_analysis(
            conn, result_id, revision_id, computation, "active" if still_current else "expired"
        )
    return analysis_id


def get_richness_analysis(
    conn: sqlite3.Connection, analysis_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM richness_analysis WHERE analysis_id = ?", (analysis_id,)
    ).fetchone()


def list_richness_analyses(
    conn: sqlite3.Connection,
    result_id: int | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM richness_analysis WHERE 1=1"
    params: list = []
    if result_id is not None:
        sql += " AND result_id = ?"
        params.append(result_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY analysis_id"
    return conn.execute(sql, params).fetchall()


def richness_analysis_observations(
    conn: sqlite3.Connection, analysis_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM richness_analysis_observation
         WHERE analysis_id = ? ORDER BY line_no
        """,
        (analysis_id,),
    ).fetchall()


def richness_analysis_names(
    conn: sqlite3.Connection, analysis_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM richness_analysis_name
         WHERE analysis_id = ? ORDER BY rowid
        """,
        (analysis_id,),
    ).fetchall()


def richness_analysis_witness(
    conn: sqlite3.Connection, analysis_id: int, bound: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM richness_analysis_witness
         WHERE analysis_id = ? AND bound = ?
        """,
        (analysis_id, bound),
    ).fetchone()
