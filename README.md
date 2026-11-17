# 实验委托模型拆解册

南麓材料实验室（9人）把委托单上的业务依赖整理为可验证的3NF关系，并保留候选键、闭包、最小覆盖和无损校验证据。

## 业务工作流

原先设计员在Excel里堆叠委托号、客户、检测项、方法与结果，再手工画表结构。痛点：依赖口径难确认、复合键容易漏判、拆表后无法证明无损。上游为委托单和已确认业务规则，本项目承接关系草案登记、函数依赖编辑、3NF合成、验证报告导出，下游为实验数据录入系统的数据库设计人员。

核心实体：关系草案 Relation、函数依赖 Dependency、分解报告 Report、拆表演练 Drill。原有4业务页之外新增「拆表演练」列表与工作台；管理员另有审计页。业务规则不执行为SQL，属性名是纯数据。

## 快速启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 http://localhost:18243 ，后端 http://localhost:19243/docs ，MySQL localhost:17743。

|角色|账号|密码|权限|
|---|---|---|---|
|admin|admin@nanlu.test|Demo2026!|草案/依赖写入、运行、审计|
|operator|operator@nanlu.test|Demo2026!|草案/依赖写入、运行、导出|
|viewer|viewer@nanlu.test|Demo2026!|只读与导出，写API403|

## 一天的工作剧本

09:00 operator登录；09:05打开 `NL-1117-A` 材料检测委托单；09:10查看五属性 `委托号,客户,检测项,方法,结果`；09:15确认 `委托号→客户`、`检测项→方法`、`委托号,检测项→结果`；09:20执行 `shift-20261117` 分解任务；09:25候选键为 `{委托号,检测项}`，拆出委托客户、检测项方法、委托检测结果3个关系；09:30导出 `report-1.json`；09:40 viewer复核3NF/依赖保持/无损均通过；10:00 admin审计。

另有 `NL-1117-B` 的循环依赖 A→B/B→A与独立属性C，候选键AC/BC；`NL-1117-C` 没有确认依赖，完整属性集作为唯一候选键，不凭空猜测业务规则。

### 委托数据拆表演练

10:10 在「拆表演练」选中 `shift-20261117` 报告发起演练。服务按报告结果**实际建表**：原表 `drill_src_<relation_id>` 保留委托号/客户/检测项/方法/结果五个文本列，新表 `drill_t_<drill_id>_<n>` 为报告里每条分解关系一张，另加来源键 `_src_key` 用于幂等回填。报告不通过3NF/无损/依赖保持，或分解属性并集还原不出原属性时，拒绝发起。

演练状态：`pending → migrating → switched`，失败为 `failed` 且原表保持可用。要点：

- **方案冻结**：发起时把报告快照、候选键、依赖和分解关系写入 `drills.plan`；之后改草案、加依赖、改报告都不影响本次。
- **回填可中断续跑**：每批（默认500行，可用 `?max_rows=` 调小）在一个事务内连同游标一起提交，进程停机后重开页面点「继续迁移一批」从游标续跑；新表按 `_src_key` upsert，重试不产生重复行。
- **迁移期可继续增改委托行**：写入原表后同步已落库的新表行，并记入 `drill_changes`（迁移期间已接收的更正）。改动若使冻结依赖不成立（例如同一检测项出现两个方法），返回409并指出冲突行、不写库；确实要整组更正时勾选「同组更正」（`cascade`），同决定项的委托行一起改正。
- **切换**：仅迁移完成后可切换。切换事务内加关系级行锁、对原表 `FOR UPDATE`，按原表对齐新表后逐字段核对（行数与SHA256）；差异则回滚并保留原表。`drill_assignments.relation_id` 是主键，并发切换只有第一个方案能插入，其余返回409且原表不动。
- **切换后**：读写走新表（`/rows?view=current` 自动切换），原表保留只读可查；旧报告、旧导出照常可读。

页面提供原表/新表/当前读写三种视图、迁移前后核对（行数、校验和、缺失/多余行）、物理表字段抽样和改动日志。系统只在本演练的 `drill_*` 表上建表读写，不连接外部生产库。

## 算法说明

Python真算，MySQL保存草案/依赖与结果，Redis传递任务，独立worker执行。

1. 属性闭包：迭代应用所有X→a，直到不再增加属性。
2. 候选键：先取不出现在RHS的必需属性，再按大小枚举其余属性子集；若闭包覆盖全集且不含已知候选键，加入结果。**最多10个属性/64条依赖**，最多1024个子集；超限API422/算法ValueError，避免指数搜索失控。
3. 最小覆盖：RHS拆成单属性，去平凡依赖；逐一删除左侧多余属性、移除冗余依赖。
4. 3NF合成：按最小覆盖左侧分组，删除被包含关系；若无关系含候选键，补入候选键关系。无依赖时保留全属性关系。
5. 验证：精确生成F+在各分解关系上的投影，枚举局部候选键，逐非平凡投影依赖检查3NF；所有原始依赖须可由投影并集推出。tableau追赶法依据FD合并符号，出现全a符号行则无损，报告包含见证行、合并步骤与最终表。

复合、循环、空决定项、无依赖均支持。不在任何RHS中的属性强制进入候选键。输入入队时冻结快照，后续修改不会改变已有报告。结果JSON含SHA256，固定输入应有固定输出。失败态包括引用关系外属性、重复/空属性、属性超过10、依赖超过64、队列错误；页面可恢复并重新执行。

这是小型关系模型设计辅助工具，FD由人工提供，不能从脏数据自动推断业务规则；正式设计流程不自动执行DDL，只有拆表演练在独立 `drill_*` 表上建表。精确投影在10属性内有界，仍可能随依赖复杂度增加耗时。分解任务仍是单worker串行队列，不带崩溃续跑；拆表演练的迁移/切换由请求驱动，可续跑。演练行存储在物理表中，适合演示规模；大批量生产迁移需另行评估。

## 工程结构

|服务|技术|职责|
|---|---|---|
|frontend|原生HTML/CSS/JS + Nginx|4业务页+拆表演练、登录、共享业务表格与报告组件|
|backend|Python3.11/FastAPI/SQLAlchemy/PyJWT|校验、权限、审计、快照、导出与拆表演练|
|worker|Python3.11|闭包、候选键、3NF与chase|
|db|MySQL8.0|JSON属性与依赖、任务报告、演练方案与原表/新表物理表|
|redis|Redis7|持久任务队列|

目录：`frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}`；`backend/src/{routes,controllers,services,models,schemas,middlewares,utils,constants,config}`；`backend/scripts/seed_demo.py`；`backend/migrations/001_initial.sql`。每核心实体贯穿模型/schema/service/route/API/store/page。组件table/badge/frame复用，hooks/session/feedback和utils/forms/html共用；request_id/errors双后端中间件。

枚举：角色admin/operator/viewer、任务queued/running/completed/failed、演练pending/migrating/switched/failed；前后端定义位于 `backend/src/constants/enums.py` 和 `frontend/src/constants/enums.js`，security/reports/drills、frame/badge消费；worker持久化状态。

演练相关表（`src/models/entities.py`）：`drills`（冻结方案、状态、游标、行数、校验和）、`drill_assignments`（每委托表唯一生效方案）、`drill_changes`（迁移期更正日志）、`relation_locks`（写入/迁移/切换互斥）。原表与新表为物理表，命名见 `backend/migrations/001_initial.sql`。

### 本地测试

```sh
cd backend
pip install -r requirements-dev.txt
python -m pytest tests -q
```

测试用内存SQLite与fakeredis覆盖：按报告建表与切换、迁移中改行与冲突拒绝、同组更正、中断续跑不重复、并发切换只生效一个方案、方案冻结与旧报告可读、绕过同步的写入在切换时被对齐、只读角色403。

环境变量见.env.example：COMPOSE_PROJECT_NAME、FRONTEND_PORT/BACKEND_PORT/DB_PORT、DB_PASSWORD、JWT_SECRET、SEED_DEMO。默认密码仅本地演示，JWT8小时。seed按自然键只补缺失关系及初始依赖，不覆盖用户修改。schema由SQLAlchemy metadata.create_all初始化，无在线迁移引擎。

本地开发：Python3.11安装backend/requirements.txt，配置DATABASE_URL/REDIS_URL后从backend运行 `uvicorn src.main:app --reload`、`python -m src.worker`；前端用Nginx同源代理/api。5服务均有健康检查和健康依赖，数据库/Redis/worker产物为独立命名卷。停止清空：`docker compose down -v --remove-orphans`。

UI为酒红档案册，双字体由Google Fonts加载，离线有回退且业务不依赖字体网络。

## License

MIT。真实测试脚本、截图与结果保存在源码外 `../../test-artifacts/gb-4104/`。
