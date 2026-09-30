# 社区回收点服务日历 HTTP API

为社区回收点维护**每周开放时段**、**指定日期全天停开**与**当日补开时段**，
按当地日期范围查询，逐日返回**实际开放区间的 UTC 起止、当地日期和来源**；
并支持以当前已发布日历为准的**到访预约**（幂等创建、凭证取消、变更保护）
与已占用时段的**候补排队**（凭密钥查询/撤回、释放时按申请先后自动转正）。

技术栈：Python 3.11 · FastAPI · PostgreSQL 16 · psycopg 3 · Uvicorn。

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 启动两个容器：`db`（PostgreSQL 16 Alpine）与 `api`（本服务）。
- 数据库由 Compose 健康检查把关；API 启动时会自动等待数据库并幂等执行
  [`app/schema.sql`](app/schema.sql) 完成建表初始化，无需手工迁移。
- 数据持久化在命名卷 `pgdata` 中。加 `-d` 可后台运行；`docker compose down -v`
  可连数据卷一起清除。

### 地址与端口

| 服务 | 容器内地址 | 宿主机默认地址 |
| --- | --- | --- |
| API | `http://0.0.0.0:8000` | <http://localhost:8000> |
| Swagger UI（交互式文档） | — | <http://localhost:8000/docs> |
| ReDoc | — | <http://localhost:8000/redoc> |
| OpenAPI 描述 | — | <http://localhost:8000/openapi.json> |
| PostgreSQL | `db:5432` | `localhost:5432` |
| 健康检查 | — | `GET http://localhost:8000/health` |

---

## 2. 配置（环境变量）

均为可选；`docker compose up` 直接用默认值即可。也可复制
[`.env.example`](.env.example) 为 `.env` 后修改，Compose 会自动加载。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_DB` | `recycle` | 数据库名 |
| `POSTGRES_USER` | `recycle` | 数据库用户 |
| `POSTGRES_PASSWORD` | `recycle` | 数据库密码 |
| `POSTGRES_PORT_PUBLISH` | `5432` | 映射到宿主机的数据库端口 |
| `APP_PORT_PUBLISH` | `8000` | 映射到宿主机的 API 端口 |
| `MAX_QUERY_DAYS` | `370` | 日历查询允许的最大跨天数量（两端包含） |
| `RESERVATION_MAX_DAYS_AHEAD` | `30` | 预约起点最多可提前的天数 |
| `RESERVATION_MAX_DURATION_MINUTES` | `120` | 预约最长时长（分钟） |
| `DATABASE_URL` | （无，由上面拼装） | 完整 libpq 连接串，**非容器/本地运行时**可直接设置，例如 `postgresql://recycle:recycle@localhost:5432/recycle` |

容器内 API 固定监听 `0.0.0.0:8000`；容器内数据库主机固定为 `db:5432`。

### 不使用 Docker 的本地运行方式

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://recycle:recycle@localhost:5432/recycle
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 3. 领域规则与语义

- **时区**：每个回收点持有一个 IANA 时区名（如 `Asia/Shanghai`、
  `America/New_York`），所有时刻以该时区的挂钟时间解释，输出统一为 UTC。
- **每周开放时段 `weekly`**：
  - `weekday` 为 ISO 星期：`1`=周一 … `7`=周日；
  - 同一规则内 `end_time <= start_time` 即**跨午夜**，最长延伸到次日；
  - **跨午夜时段按开始日匹配周规则**（周五 20:00–次日 02:00 属于周五场次）。
- **全天停开 `closed_dates`**：
  - 扣除该当地日上的**全部常规片段**，包括前一晚（`D-1`）延续到当日的片段；
  - 同时取消**由当日开场、延伸到次日凌晨的尾巴**，使跨午夜场次整体消失，
    不会在次日留下悬空片段；
  - 然后在当日叠加补开时段。
- **当日补开 `makeup`**：仅限当日（`end_time` 必须严格晚于 `start_time`）。
- **逐日裁切**：查询范围两端包含；结果按当地日逐天给出，区间被裁到当天的
  当地午夜之间（因此跨午夜场次会在午夜处切成两段，但来源相同）。
- **区间合并与来源保留**：同一日内相互重叠（或首尾相接）的开放区间合并为一个，
  `sources` 保留构成该并集的**全部来源**。
- **夏令时**：
  - 挂钟时间在春令时“缺失”（gap）时，边界**顺延到第一个有效时刻**；
  - 在令时“重复”（fall-back）时，**起点取较早实例、终点取较晚实例**；
  - 所有区间运算在 UTC 下进行，正确计入回退重复的一小时。
- **版本化与原子发布**：
  - 规则整套提交，请求体带 `expected_version`（乐观并发控制）；
  - 服务端对回收点行加 `FOR UPDATE` 行锁，校验当前版本号后在**单个事务**里
    插入新版本并推进版本号，原子提交或整体回滚；
  - 历史版本永不就地修改，全部保留可查；
  - 每次日历查询在 `REPEATABLE READ` 事务里读取**同一个已发布版本**的完整规则；
    也可用 `?version=N` 显式固定到某个历史已发布版本。
  - 新建回收点版本号为 `0`，表示“尚无已发布规则”。
- **到访预约**：
  - 预约以**当前已发布**的服务日历为准：请求给出 UTC 起止与客户幂等键，
    起点须在 `RESERVATION_MAX_DAYS_AHEAD`（默认 30）天内且不早于当前时刻，
    终点严格晚于起点且总时长不超过 `RESERVATION_MAX_DURATION_MINUTES`
    （默认 120 分钟）；
  - 预约必须**完整落在一段连续开放区间内**——连续区间允许跨当地午夜
    （跨午夜场次不在午夜处切开，首尾相接的场次合并后视为同一段）；
  - 同一回收点的未取消预约之间不允许 UTC 时间重叠，**首尾相接可以并存**；
  - 幂等键按回收点隔离：同一点同一键以**首次成功请求**为准，同内容重试
    原样返回首次结果（HTTP 200），不同内容报 `idempotency_conflict`；
  - 取消需同时给出预约号与创建时签发的取消凭证；重复取消幂等返回
    已取消状态；已取消预约不再占用时段、也不再阻止规则变更；
  - **凭凭证改期**：对未取消且尚未开始的预约，可凭同一取消凭证把预约改到
    新的带时区起止时间。请求须给出客户本次改期幂等键与期望修订号
    （`revision`，创建时为 `1`，每次成功改期 +1）；新窗口按创建预约的同一套
    规则校验（未来窗口/最长时长/当前已发布日历的连续开放区间/同点未取消预约
    不重叠，判重时忽略自身）。成功后**预约号与取消凭证保持不变**，更新起止、
    所依据规则版本与修订号并返回当前预约；任何校验失败整体回滚，**不释放原
    时段**。同一预约同一改期键同内容重试返回该次成功结果，异内容返回
    `idempotency_conflict`；期望修订号过期返回 `revision_conflict`。
    预约不存在、凭证错误沿用取消接口的 404/403 语义；已取消或已开始的预约
    不可改，返回 `reservation_not_reschedulable`。
  - **原创建幂等键不受改期影响**：创建键的同内容重试始终以**首次创建**的
    起止与凭证为准（响应中的窗口/规则版本/修订号 `1` 即首次创建快照，状态
    字段反映当前状态），异内容仍报 `idempotency_conflict`。
  - **变更保护**：发布新规则或修改时区时，若任一尚未结束的未取消预约
    在新日历下不再完整开放，整个变更被拒绝并返回冲突预约号列表；
    预约创建、改期、规则发布与时区修改在同一回收点行锁下串行，并发下同样
    满足日历覆盖与无重叠。
- **候补（waitlist）**：
  - 目标时段已被预约占用时，访客可提交 UTC 起止与幂等键申请候补；
    窗口须通过与创建预约相同的时长/提前期校验、被当前已发布日历完整
    覆盖，且**与至少一个未取消预约重叠**——空闲窗口不属于候补场景，
    会被拒绝并提示直接走预约流程（`window_available`）；
  - 成功返回**候补号、查询密钥（`query_token`）与排队状态**；幂等键按
    回收点隔离：同点同键同内容重试返回原申请（HTTP 200，状态字段反映
    当前状态），异内容报 `idempotency_conflict`；
  - 凭查询密钥可查询本人候补状态；转正后响应附带**预约号与取消凭证**；
    排队中（`queued`）可凭密钥撤回，重复撤回幂等返回；
  - **转正**：取消或改期释放时段时，在同一事务中按申请先后检查该点
    候补队列——开始时间已过的标记 `expired`；当前规则下不再开放或仍与
    未取消预约冲突的暂留队列（`queued`）；最早可用者自动转为预约，
    **按转正时的当前规则版本**签发预约与取消凭证。每个候补**只能转正
    一次**（`promoted` 为终态，其预约日后再被取消也不会重复转正）；
  - 候补申请、撤回、转正与预约创建/取消/改期/规则发布在同一回收点行锁
    下串行：并发下不产生重叠、不错用旧日历；转正失败的整个事务（含
    释放操作与候补状态变更）一并回滚；
  - 候补不占用时段：新预约照常创建，只在释放事件发生时按队列转正。

---

## 4. HTTP API

所有请求/响应均为 JSON；错误统一为：

```json
{ "error": { "code": "...", "message": "...", "details": { } } }
```

### 4.1 回收点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/points` | 创建回收点（校验 IANA 时区） |
| `GET` | `/points/{id}` | 查看回收点（含当前版本号） |
| `PATCH` | `/points/{id}` | 修改名称/时区 |

`POST /points` 请求体：

```json
{ "name": "阳光社区回收点", "timezone": "Asia/Shanghai" }
```

### 4.2 规则发布与版本

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/points/{id}/rules` | 条件式、原子地发布整套规则（201） |
| `GET` | `/points/{id}/rules` | 读取当前已发布版本 |
| `GET` | `/points/{id}/rules/versions` | 已发布版本列表（新到旧） |
| `GET` | `/points/{id}/rules/versions/{version}` | 读取指定历史版本 |

发布请求体：

```json
{
  "expected_version": 0,
  "rules": {
    "weekly": [
      { "weekday": 1, "start_time": "09:00", "end_time": "17:00" },
      { "weekday": 5, "start_time": "20:00", "end_time": "02:00" }
    ],
    "closed_dates": ["2026-10-01"],
    "makeup": [
      { "day": "2026-10-01", "start_time": "13:00", "end_time": "16:00" }
    ]
  }
}
```

时间均为 `HH:MM`（当地挂钟时间，秒可选）。成功返回新版本号与发布时间。

### 4.3 日历查询

```
GET /points/{id}/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD[&version=N]
```

- `from`、`to` 为**当地日期**，**两端包含**；`to` 不得早于 `from`。
- `version` 省略时使用最新已发布版本。

响应示例：

```json
{
  "point_id": 1,
  "version": 1,
  "timezone": "Asia/Shanghai",
  "date_from": "2026-09-28",
  "date_to": "2026-10-04",
  "days": [
    {
      "local_date": "2026-09-28",
      "intervals": [
        {
          "start": "2026-09-28T01:00:00Z",
          "end": "2026-09-28T09:00:00Z",
          "sources": [
            { "kind": "weekly", "weekday": 1,
              "start_time": "09:00:00", "end_time": "17:00:00" }
          ]
        }
      ]
    }
  ]
}
```

无开放时段的日期仍然出现，`intervals` 为空数组。`sources[].kind` 为
`weekly`（带 `weekday`）或 `makeup`（带 `day`）。

### 4.4 到访预约

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/points/{id}/reservations` | 创建预约（201；同键同内容重试为 200） |
| `POST` | `/points/{id}/reservations/{rid}/reschedule` | 凭取消凭证改期（200；同键同内容重试原样返回） |
| `POST` | `/points/{id}/reservations/{rid}/cancel` | 凭取消凭证取消（200，可重复） |

创建请求体（`start`/`end` 为带时区偏移的 ISO-8601 时间戳，统一按 UTC 解释）：

```json
{
  "idempotency_key": "order-2026-0930-0001",
  "start": "2026-10-01T09:00:00Z",
  "end": "2026-10-01T10:00:00Z"
}
```

成功响应（201）包含**预约号、取消凭证、修订号与所依据的规则版本**：

```json
{
  "id": 7,
  "point_id": 1,
  "status": "active",
  "revision": 1,
  "start": "2026-10-01T09:00:00Z",
  "end": "2026-10-01T10:00:00Z",
  "rules_version": 3,
  "cancel_token": "r9vQ…",
  "created_at": "2026-09-30T08:00:00Z",
  "cancelled_at": null
}
```

- 同一回收点内同一 `idempotency_key` 以首次成功请求为准：同内容重试返回
  首次创建快照（HTTP 200，即使该预约后来被改期或取消——窗口/规则版本/
  `revision: 1` 为首次创建内容，状态字段反映当前状态）；不同内容返回
  `409 idempotency_conflict`。
- 取消请求体为 `{ "cancel_token": "…" }`；凭证缺失或错误返回
  `403 invalid_cancel_token`。成功或重复取消均返回 200 与当前状态
  （`status: "cancelled"`，响应不含取消凭证）。

#### 改期（reschedule）

请求体：

```json
{
  "cancel_token": "r9vQ…",
  "idempotency_key": "move-order-0001-to-afternoon",
  "expected_revision": 1,
  "start": "2026-10-01T11:00:00Z",
  "end": "2026-10-01T11:30:00Z"
}
```

- `cancel_token`：创建时签发的取消凭证（改期与取消共用同一能力凭证，
  成功改期后凭证不变）。
- `expected_revision`：客户端看到的修订号；创建成功为 `1`，之后每次成功
  改期 +1。与服务端不符返回 `409 revision_conflict`（`details` 给出
  `expected_revision` 与 `current_revision`）。
- `idempotency_key`：**本次改期**的幂等键，按预约隔离，与创建键相互独立。
- `start`/`end`：新的带时区偏移起止时间，须通过与创建相同的窗口/时长/
  开放区间/重叠校验（重叠判定忽略该预约自身，故新旧窗口可以交叠；
  首尾相接亦可）。

成功返回 200 与更新后的预约（与创建响应同构，含取消凭证）：

```json
{
  "id": 7,
  "point_id": 1,
  "status": "active",
  "revision": 2,
  "start": "2026-10-01T11:00:00Z",
  "end": "2026-10-01T11:30:00Z",
  "rules_version": 3,
  "cancel_token": "r9vQ…",
  "created_at": "2026-09-30T08:00:00Z",
  "cancelled_at": null
}
```

- 同一预约同一改期键、**同内容**重试返回该次成功结果（HTTP 200，修订号
  不再增加；即使此后又改过期或已取消，也返回该次结果对应的窗口，状态字段
  反映当前状态）；**异内容**重试返回 `409 idempotency_conflict`
  （`details` 给出该键已存的窗口）。
- 仅未取消且尚未开始（当前时间早于预约起点）的预约可改；否则返回
  `409 reservation_not_reschedulable`，`details.reason` 为
  `reservation_cancelled` 或 `reservation_started`。
- 新窗口不合法（终点不晚于起点、超过最长时长、起点在过去或超出可约天数）
  返回 `422 invalid_period`；不落在当前已发布日历的连续开放区间内返回
  `409 outside_open_hours`；与同点**其他**未取消预约重叠返回
  `409 reservation_overlap`。
- 任一失败均在同一事务内回滚：原时段继续被占用，修订号与窗口不变。

### 4.5 候补（waitlist）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/points/{id}/waitlist` | 申请候补（201；同键同内容重试为 200） |
| `GET` | `/points/{id}/waitlist/{wid}?token=…` | 凭查询密钥查看候补状态（转正后含预约号与取消凭证） |
| `POST` | `/points/{id}/waitlist/{wid}/withdraw` | 凭查询密钥撤回排队中的候补（200，可重复） |

申请请求体与创建预约同构（`start`/`end` 为带时区偏移的 ISO-8601 时间戳）：

```json
{
  "idempotency_key": "wait-2026-0930-0001",
  "start": "2026-10-01T09:00:00Z",
  "end": "2026-10-01T10:00:00Z"
}
```

- 仅接受符合预约时长/提前期策略、被当前已发布日历完整覆盖、且与至少
  一个未取消预约重叠的窗口；窗口空闲时返回 `409 window_available`，
  应改用 `POST /points/{id}/reservations` 直接预约。
- 成功响应（201）包含**候补号、查询密钥与排队状态**：

```json
{
  "id": 3,
  "point_id": 1,
  "status": "queued",
  "start": "2026-10-01T09:00:00Z",
  "end": "2026-10-01T10:00:00Z",
  "rules_version": 3,
  "query_token": "k8Qx…",
  "promoted_reservation_id": null,
  "created_at": "2026-09-30T08:00:00Z",
  "decided_at": null
}
```

- 同一回收点内同一 `idempotency_key` 以首次成功申请为准：同内容重试
  返回原申请（HTTP 200，`status` 反映当前状态），不同内容返回
  `409 idempotency_conflict`。
- 候补状态机：`queued`（排队中）→ `promoted`（已转正）/ `withdrawn`
  （已撤回）/ `expired`（释放检查时发现开始时间已过）。
- 查询响应在转正后附带预约号与取消凭证（`reservation` 字段，否则为
  `null`）；缺失或错误的查询密钥返回 `403 invalid_query_token`，候补
  不存在返回 `404 waitlist_entry_not_found`：

```json
{
  "id": 3,
  "point_id": 1,
  "status": "promoted",
  "start": "2026-10-01T09:00:00Z",
  "end": "2026-10-01T10:00:00Z",
  "rules_version": 3,
  "created_at": "2026-09-30T08:00:00Z",
  "decided_at": "2026-09-30T09:12:00Z",
  "reservation": {
    "id": 11,
    "point_id": 1,
    "status": "active",
    "revision": 1,
    "start": "2026-10-01T09:00:00Z",
    "end": "2026-10-01T10:00:00Z",
    "rules_version": 4,
    "cancel_token": "r9vQ…",
    "created_at": "2026-09-30T09:12:00Z",
    "cancelled_at": null
  }
}
```

  其中候补的 `rules_version` 是**申请时**校验依据的版本，转正预约的
  `rules_version` 是**转正时**的当前已发布版本。
- 撤回请求体为 `{ "query_token": "…" }`；仅 `queued` 状态可撤回，重复
  撤回幂等返回 `withdrawn`；已转正/已失效的候补返回
  `409 waitlist_not_withdrawable`（`details.reason` 为
  `already_promoted` / `already_expired`）。
- **转正时机**：预约取消或改期释放时段时，在同一事务内按申请先后扫描
  该点候补队列，最早可用者自动转为预约（新预约不占用客户幂等键空间，
  其取消凭证随查询响应签发）；仍不满足条件的候补暂留队列，等待下一次
  释放检查。

### 4.6 可区分错误码

| HTTP | `error.code` | 触发场景 |
| --- | --- | --- |
| 404 | `point_not_found` | 回收点不存在 |
| 404 | `version_not_found` | 指定的已发布版本不存在 |
| 404 | `reservation_not_found` | 预约号不存在（或不属于该回收点） |
| 403 | `invalid_cancel_token` | 取消时未提供凭证或凭证不匹配 |
| 409 | `no_published_version` | 尚无已发布规则（当前版本为 0）就查询日历或创建预约 |
| 409 | `version_conflict` | `expected_version` 与服务端当前版本不符（`details` 给出双方版本号） |
| 409 | `idempotency_conflict` | 同一回收点同一幂等键提交了不同的起止时间（`details` 给出已存预约） |
| 409 | `reservation_overlap` | 与同点未取消预约 UTC 时间重叠（创建时；改期时忽略自身，`details` 给出冲突预约号） |
| 409 | `outside_open_hours` | 预约（或改期后的新窗口）未完整落在当前已发布日历的任一段连续开放区间内 |
| 409 | `revision_conflict` | 改期 `expected_revision` 与预约当前修订号不符（`details` 给出双方修订号） |
| 409 | `reservation_not_reschedulable` | 改期对象已取消或已开始（`details.reason` 区分 `reservation_cancelled` / `reservation_started`） |
| 409 | `reservation_change_conflict` | 发布规则或修改时区将使未取消预约不再完整开放（`details.conflicting_reservation_ids` 给出冲突预约号） |
| 404 | `waitlist_entry_not_found` | 候补号不存在（或不属于该回收点） |
| 403 | `invalid_query_token` | 查询/撤回候补时未提供查询密钥或密钥不匹配 |
| 409 | `window_available` | 候补申请的窗口当前空闲（未与任何未取消预约重叠），应直接走预约流程 |
| 409 | `waitlist_not_withdrawable` | 候补已转正或已失效，不可撤回（`details.reason` 区分 `already_promoted` / `already_expired`） |
| 422 | `invalid_period` | 无效时段：星期越界、时间格式错、常规起终点相等、补开跨日/零长度、重复规则、日期范围倒置/超限；预约终点不晚于起点、超过最长时长、起点在过去或超出可约天数 |
| 422 | `unknown_timezone` | 无法识别的 IANA 时区 |
| 422 | `invalid_request` | 请求体结构未通过校验（字段缺失、类型错误、时间戳缺时区偏移等） |

---

## 5. 调用示例

服务启动后任选其一：

```bash
# 方式一：curl 脚本（创建点 → 发布 → 冲突 → 查询 → 预约全流程 → 错误演示）
./examples/curl_examples.sh

# 方式二：仅依赖 Python 标准库的客户端
python3 examples/python_client.py
```

手动快速体验：

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/points \
  -H 'Content-Type: application/json' \
  -d '{"name":"阳光社区回收点","timezone":"Asia/Shanghai"}'
```

预约快速体验（假设回收点 `1` 已发布覆盖该时段的规则）：

```bash
# 创建预约（起点须在未来 30 天内，时长 ≤ 2 小时）
curl -s -X POST localhost:8000/points/1/reservations \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"visit-0001",
       "start":"2026-10-01T09:00:00Z",
       "end":"2026-10-01T10:00:00Z"}'

# 凭预约号与取消凭证取消（可重复调用）
curl -s -X POST localhost:8000/points/1/reservations/7/cancel \
  -H 'Content-Type: application/json' \
  -d '{"cancel_token":"<创建响应中的 cancel_token>"}'

# 凭同一取消凭证改期（带期望修订号与本次改期幂等键）
curl -s -X POST localhost:8000/points/1/reservations/7/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"cancel_token":"<创建响应中的 cancel_token>",
       "idempotency_key":"move-visit-7-to-11",
       "expected_revision":1,
       "start":"2026-10-01T11:00:00Z",
       "end":"2026-10-01T11:30:00Z"}'
```

候补快速体验（目标时段已被占用时）：

```bash
# 申请候补：窗口须被当前日历覆盖且与未取消预约重叠
curl -s -X POST localhost:8000/points/1/waitlist \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"wait-0001",
       "start":"2026-10-01T09:00:00Z",
       "end":"2026-10-01T10:00:00Z"}'
# → 201 {"id":3, "status":"queued", "query_token":"k8Qx…", ...}

# 凭候补号与查询密钥查看状态（转正后含预约号与取消凭证）
curl -s "localhost:8000/points/1/waitlist/3?token=<query_token>"

# 排队中凭密钥撤回（可重复调用）
curl -s -X POST localhost:8000/points/1/waitlist/3/withdraw \
  -H 'Content-Type: application/json' \
  -d '{"query_token":"<query_token>"}'

# 占用该窗口的预约被取消或改期后，最早可用的候补自动转正；
# 再次查询即可看到 status="promoted" 与 reservation.cancel_token
```

---

## 6. 本地开发与测试

```bash
python3 -m venv .venv --without-pip && . .venv/bin/activate
# 若 venv 中没有 pip：python /tmp/get-pip.py  （或用系统包安装 python3-venv）
pip install -r requirements.txt
pip install pytest pgserver
pytest -q
```

测试无需本机安装 Docker/PostgreSQL：通过 [`pgserver`](https://pypi.org/project/pgserver/)
拉起一次性的真实 PostgreSQL 16 实例，对完整 HTTP 链路做集成测试，覆盖：

- 跨午夜规则按开始日匹配、逐日裁切；
- 停开扣除当日全部常规片段（含前夜延续）并取消当日场次次日尾巴，再叠加补开；
- 重叠区间合并且保留全部来源；
- 夏令时春缺失顺延、秋重复起点取早/终点取晚（纽约、哈瓦那午夜跳变）；
- 版本条件提交、冲突 409、历史版本固定查询、并发发布恰好一方成功；
- 预约：窗口策略（30 天/2 小时/未来起点）、幂等键首请求为准与同内容重放、
  异内容冲突、跨午夜连续区间判定、重叠拒绝与首尾相接并存、凭证取消与重复取消、
  取消后时段释放、发布/改时区的预约保护与并发下恰好一方成功；
- 改期：成功改期保留预约号与凭证并推进修订号、同键同内容重放该次结果
  （含再改期/取消后重放）、异内容冲突、过期修订号冲突、已取消/已开始不可改、
  新窗口窗口策略/开放区间/重叠（忽略自身）校验、失败不释放原时段、原创建键
  重试仍按首次创建内容判定、改期与取消/发布并发下结果一致且无 500；
- 候补：占用窗口可申请（返回候补号/查询密钥/排队状态）、空闲窗口拒绝、
  窗口策略与日历覆盖校验、幂等重放与异内容冲突、凭密钥查询/撤回与重复撤回、
  取消与改期释放触发转正、先到先转与仍冲突暂留、开始已过标记失效、规则
  变更后不再开放暂留并在后续释放时按转正时版本签发、只能转正一次、
  并发取消/创建下不产生重叠且无 500；
- 全部可区分错误码。

---

## 7. 项目结构

```
app/
  config.py      环境变量配置
  schema.sql     PostgreSQL 建表脚本（启动时幂等执行）
  db.py          连接池、版本化规则的原子发布与读取、预约/候补存储（行锁 + 事务）
  scheduler.py   纯 Python 日历展开引擎（跨午夜/停开/补开/合并/DST/连续开放区间）
  schemas.py     Pydantic 请求响应模型与时段语义校验
  errors.py      可区分错误类型
  main.py        FastAPI 路由与错误处理
examples/        curl 与 Python 调用示例
tests/           引擎单测 + 真实 PostgreSQL 的 API 集成测试
Dockerfile, docker-compose.yml
```
