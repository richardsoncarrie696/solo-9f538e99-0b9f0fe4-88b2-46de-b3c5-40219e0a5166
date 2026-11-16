# 生态监测名称归并供数 API

把野外观测记录中的旧物种名（同义名）沿分类表的指向关系追踪到**接受名**，生成可重读、
可追溯修订的供数结果；并可基于供数结果做**丰富度上下界分析**。技术栈：FastAPI + SQLite，
本地单容器部署。

## 业务规则

### 观测批次
- `POST /batches` 一次录入一批观测，**至多 100 条**，每条含批内唯一编号 `code` 与物种名 `name`。
- 批内编号重复返回 `409`，且整批回滚、不留部分数据。

### 分类表修订（可整版更正）
- `POST /taxonomies` 整版提交分类表：接受名列表 + **至多 40 条**同义名指向关系。
- 同义关系为有向边（`synonym → target`），**允许分叉**（一个名称指向多个目标），也允许链式与菱形合流。
- 每次提交生成一个新的不可变修订；旧修订标记 `superseded`，引用旧修订的有效结果立即置为 `expired`。

### 名称追踪与供数判定
对每条观测的名称，沿所有可达路径追踪：

| 情况 | 处理 | reason_code |
|---|---|---|
| 所有路径无环且汇聚到**同一个**接受名 | 纳入供数 `included`，记录接受名 | — |
| 不同路径通向不同接受名（分叉冲突） | 待核 `pending` | `divergent_accepted` |
| 可达子图中存在环（含自环） | 待核 `pending` | `cycle` |
| 没有可达的接受名（含部分路径断在非接受名） | 待核 `pending` | `no_accepted_name` |

### 结果
- `POST /results` 生成结果，**固定观测批次与分类表修订**，返回结果号 `result_id`。
- `GET /results/{result_id}` 按结果号重读；分类表更正后旧结果标 `expired` 但内容冻结、仍可读取。
- 生成与更正交错时：写操作经 SQLite `BEGIN IMMEDIATE` 串行化；针对旧修订生成的结果直接落为
  `expired`，**旧修订永远不会覆盖当前供数**；任何失败整体回滚，不产生部分结果。
- 同一批次新生成的当前结果会使该批次旧的 `active` 结果过期，保证每批次至多一份当前供数。

### 丰富度分析（精确上下界）

`POST /richness-analyses` 接收**当前供数结果号**，基于该结果所固定的分类表修订，回答：
「旧名的分叉归属会让这批观测的物种数相差多少？」

- **候选接受名**：对每种观测名，取其可达的接受名**终点**。任一观测名遇环或存在非接受名终点，
  整次分析拒绝（`422`），`detail.observations` 逐条列出观测编号、原因码与非接受名终点，
  **不留下部分分析**。
- **同名同选**：名称相同的多条记录只能选同一个候选接受名，见证中一并列出这些观测编号。
- **已纳入记录保持原归属**：来源结果中 `included` 的名称固定为原接受名（`attribution_fixed=true`），
  不参与选择，但其接受名照常计入丰富度；待核记录才真正参与选择。
- **上下界**：在全部合法选择中，求「被使用的不同接受名总数」的精确最小值 `lower_bound`
  与最大值 `upper_bound`，并分别给出 `min_witness` / `max_witness` 完整名称归属见证
  （每种观测名选了哪个接受名、对应哪些观测编号、最终用到的接受名集合）。
  求解器对候选名子集做最小覆盖枚举 + 二分图最大匹配，两者都给出精确界（不是启发式）。
- **规模上限**：观测名至多 **20** 种，候选接受名并集至多 **16** 种，超限 `422`。
- **不改变供数资格**：分析只读取来源结果，不改动任何 `result`/`result_item`。
- **固定与过期**：分析保存来源结果号与修订号；按分析号重读的范围与见证始终不变。
  来源结果过期（分类更正或同批次新结果）时分析自动标 `expired`，但仍可按分析号重读。
  计算过程中完成的分类更正不受阻塞；若来源在计算期间过期（**迟到保存**），
  分析仍完整保存但直接落为 `expired`。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开交互文档
open http://localhost:8000/docs   # Linux 用 xdg-open
```

SQLite 数据保存在命名卷 `eco_data`（容器内 `/data/eco.db`），容器重建不丢数据。

### 本地直接运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可通过环境变量指定库路径：ECO_DB_PATH=/path/to/eco.db
```

## /docs 操作流程（Swagger UI）

1. `POST /batches` 录入观测批次；
2. `POST /taxonomies` 提交分类表（更正时再次调用即可，自动出新修订）；
3. `POST /results` 生成供数结果；
4. `GET /results/{result_id}` 查询明细，或 `GET /results?status=active` 查看当前供数；
5. `POST /richness-analyses` 用当前供数结果号生成丰富度分析；
6. `GET /richness-analyses/{analysis_id}` 按分析号重读上下界与见证。

## curl 示例

```bash
# 1. 录入观测（6 条，覆盖纳入/分叉/环/无接受名/合流/直达）
curl -s -X POST localhost:8000/batches -H 'Content-Type: application/json' -d '{
  "note": "第一批",
  "observations": [
    {"code": "o1", "name": "旧名甲"},
    {"code": "o2", "name": "分叉名"},
    {"code": "o3", "name": "环名x"},
    {"code": "o4", "name": "无人名"},
    {"code": "o5", "name": "菱形名"},
    {"code": "o6", "name": "接受名A"}
  ]}'

# 2. 提交分类表（10 条边，含分叉与环）
curl -s -X POST localhost:8000/taxonomies -H 'Content-Type: application/json' -d '{
  "note": "v1",
  "accepted_names": ["接受名A", "接受名B"],
  "synonym_edges": [
    {"synonym": "旧名甲", "target": "中间名"},
    {"synonym": "中间名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名B"},
    {"synonym": "环名x", "target": "环名y"},
    {"synonym": "环名y", "target": "环名x"},
    {"synonym": "菱形名", "target": "s1"},
    {"synonym": "菱形名", "target": "s2"},
    {"synonym": "s1", "target": "接受名A"},
    {"synonym": "s2", "target": "接受名A"}
  ]}'

# 3. 生成供数结果（revision_id 缺省取最新修订）
curl -s -X POST localhost:8000/results -H 'Content-Type: application/json' \
  -d '{"batch_id": 1}'

# 4. 按结果号重读
curl -s localhost:8000/results/1

# 5. 生成丰富度分析（用当前供数结果号）
curl -s -X POST localhost:8000/richness-analyses -H 'Content-Type: application/json' \
  -d '{"result_id": 1}'

# 6. 按分析号重读（范围与见证不变；来源过期后 status 变 expired）
curl -s localhost:8000/richness-analyses/1
```

丰富度分析响应要点（示例；为便于说明改用一批不含环/断链的观测：
`旧名甲`、`分叉名`×2、`菱形名`、`末端名`，分类表中 `分叉名→{A,B}`、`末端名→{B,C}`）：

```json
{
  "analysis_id": 1,
  "result_id": 2,
  "revision_id": 2,
  "status": "active",
  "observation_name_count": 4,
  "candidate_name_count": 3,
  "lower_bound": 2,
  "upper_bound": 3,
  "min_witness": {
    "accepted_names": ["接受名A", "接受名B"],
    "accepted_name_count": 2,
    "assignments": [
      {"observation_name": "旧名甲", "accepted_name": "接受名A", "observation_codes": ["o1"]},
      {"observation_name": "分叉名", "accepted_name": "接受名A", "observation_codes": ["o2", "o3"]},
      {"observation_name": "菱形名", "accepted_name": "接受名A", "observation_codes": ["o4"]},
      {"observation_name": "末端名", "accepted_name": "接受名B", "observation_codes": ["o5"]}
    ]
  },
  "max_witness": {
    "accepted_names": ["接受名A", "接受名B", "接受名C"],
    "accepted_name_count": 3,
    "assignments": [
      {"observation_name": "旧名甲", "accepted_name": "接受名A", "observation_codes": ["o1"]},
      {"observation_name": "分叉名", "accepted_name": "接受名B", "observation_codes": ["o2", "o3"]},
      {"observation_name": "菱形名", "accepted_name": "接受名A", "observation_codes": ["o4"]},
      {"observation_name": "末端名", "accepted_name": "接受名C", "observation_codes": ["o5"]}
    ]
  },
  "observations": [
    {"observation_code": "o2", "input_name": "分叉名", "source_item_status": "pending",
     "source_accepted_name": null,
     "candidate_accepted_names": ["接受名A", "接受名B"],
     "attribution_fixed": false}
  ]
}
```

拒绝示例（环 + 非接受名终点，整次拒绝，`detail.observations` 列出观测编号）：

```json
{"detail": {"message": "存在环或非接受名终点，无法进行丰富度分析",
            "observations": [{"observation_code": "o3", "input_name": "环名x",
                              "reason_codes": ["cycle"], "non_accepted_terminals": []},
                             {"observation_code": "o4", "input_name": "无人名",
                              "reason_codes": ["no_accepted_name"],
                              "non_accepted_terminals": ["无人名"]}]}}
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/batches` | 录入观测批次（≤100 条） |
| GET | `/batches` | 批次列表 |
| GET | `/batches/{id}` | 批次明细 |
| POST | `/taxonomies` | 整版提交分类表，生成新修订（边 ≤40） |
| GET | `/taxonomies` | 修订列表 |
| GET | `/taxonomies/{id}` | 修订完整内容 |
| POST | `/results` | 生成供数结果 |
| GET | `/results` | 结果列表（可按 `batch_id`、`status` 过滤） |
| GET | `/results/{id}` | 按结果号重读（含每条观测的追踪结论与原因） |
| POST | `/richness-analyses` | 由当前供数结果生成丰富度分析（精确上下界 + 两端见证） |
| GET | `/richness-analyses` | 分析列表（可按 `result_id`、`status` 过滤） |
| GET | `/richness-analyses/{id}` | 按分析号重读（范围与见证不变，过期后 `status=expired`） |

丰富度分析错误码：`404` 来源结果/分析不存在；`409` 来源结果已过期（不再是当前供数）；
`422` 遇环或非接受名终点（整次拒绝并列出观测编号），或观测名 >20 / 候选名并集 >16。

## 测试

```bash
pip install pytest httpx
python -m pytest tests/ -q
```

- `tests/test_resolver.py`：解析算法（链、分叉合流、分叉冲突、环、断链等 11 个用例）；
- `tests/test_api.py`：端到端业务场景（更正过期、旧修订不得激活、上限校验、404、结果冻结等）；
- `tests/test_richness.py`：丰富度分析（上下界与见证、同名同选、固定归属、拒绝并列出编号、
  上限、过期/迟到保存、来源结果不变，另有 600 组随机用例与暴力枚举比对求解器精确性）；
- `tests/test_concurrency.py`：并发测试（4 线程 100 次生成/更正交错，校验无部分结果、
  至多一份当前供数、active 必属最新修订、结果与串行重算一致；以及分析在并发更正下的完整性）。

## 目录结构

```
app/
  database.py    # SQLite 连接、建表、IMMEDIATE 写事务
  models.py      # Pydantic v2 请求/响应模型与上限校验
  resolver.py    # 名称追踪：可达性 + Kahn 环检测 + 汇聚判定；终点分析供丰富度分析复用
  richness.py    # 丰富度分析：候选终点、拒绝判定、最小覆盖/最大匹配求精确上下界与见证
  services.py    # 事务化业务服务（批次/修订/结果/丰富度分析）
  main.py        # FastAPI 路由
tests/           # pytest 测试
Dockerfile, docker-compose.yml, requirements.txt
```
