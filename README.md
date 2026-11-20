# 苇谱展陈偏好配对册

用户均值中心化相似邻居协同过滤与留一评分RMSE。为苇谱研究协作组的12位分析与业务人员提供从样本核对、独立worker计算到批准快照交付的完整工作流。

## 真实业务场景

匿名试展显式评分→邻居推荐与冷启动说明→策展备选；仅使用训练评分，相似度共同评分阈值。团队原先将 `exhibit-neighbors-202611-01.csv` 复制到多份人工表格，复核时再逐项对照。三个痛点是口径不可追溯、重复计算易错、业务说明与结果脱节。上游提供合成业务观察，系统承担研究和复核，下游业务人员读取批准快照。全部演示数据是固定随机种子生成的模拟数据。

## 业务模块

1. 业务总览：批次、样本、任务与批准交付账册。
2. 试展评分册：创建/修改来源说明、查看样本、用真实业务字段追加记录；列表分页。
3. 分析配置：冻结来源、参数和全部样本，进入Redis队列。
4. 完整结果：真实算法摘要、专属诊断、全部CSV导出和可恢复失败态。
5. 策展备选：分析师草案→负责人批准→阅读者下载批准快照。
6. 误记复核：从已完成研究出发，对指定访客和待查评分找“最少几条一星以内改动会换掉首推”，给出可证下界、方案与独立核对。
7. 审计：登录、来源修改、任务worker完成、业务说明批准、误记复核发起/续查/完成等事件。

## 快速启动

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
# 可重复执行，不覆盖人工修改
docker compose exec -T backend python -m scripts.seed_demo
```

访问 [前端](http://localhost:18249)；[API文档](http://localhost:19249/docs)。数据库仅演示用途映射 `17749`。首次准备依赖和初始化数据库需要等待健康状态：`docker compose ps`，五个服务均需healthy。

| 组件 | 技术与职责 |
|---|---|
| frontend | 原生 HTML / CSS / JS ES Modules，Nginx代理 `/api/` |
| backend | Python3.11 / FastAPI / SQLAlchemy / JWT |
| worker | 独立Python进程，Redis BLPOP队列，持久化结果/CSV/心跳 |
| db | MySQL8，命名卷持久化 |
| redis | Redis7，AOF命名卷 |

恰好五服务，均配置healthcheck；后端/worker依赖数据库与Redis健康，前端依赖后端健康；worker与后端共享 `artifacts` 命名卷。重置演示环境：`docker compose down -v --remove-orphans`，会删除本项目卷。

## 账号与权限

所有演示账号密码：`Research2026!`。

| 邮箱 | 角色 | 权限 |
|---|---|---|
| admin@exhibit-neighbors.demo | admin | 全部录入/执行、批准业务说明 |
| analyst@exhibit-neighbors.demo | analyst | 录入来源/样本、执行、提交说明，无批准权 |
| viewer@exhibit-neighbors.demo | viewer | 只读、导出结果及已批准快照 |

时间在数据库/API中以UTC存储，总览与审计页面明确转换为Asia/Shanghai北京时间。

JWT有效期8小时；PBKDF2盐化哈希保存密码；角色每请求从数据库读取。访客写操作403，匿名访问业务数据401，重复样本409，非法业务字段422，重复批准409。每客户端/路径写请求90次/分钟，超限429。生产使用需修改JWT及演示数据库凭据。

## 实体与字段

Exhibition / VisitorRating / RecommendationRun / CuratorSelection 是核心实体；另有User和Audit。数据库实际表：`exhibitions` → `visitor_ratings` → `recommendation_runs` → `curator_selections`。样本以固定字段JSON保存，后端按以下字段严格验证，不接受未知字段替代必填值：匿名访客（`visitor`）、展品代号（`exhibit`）、评分 1–5（`rating`）。

实体贯穿 `backend/src/models`、`schemas`、`services`、`controllers`、`routes`，以及 `frontend/src/api/research.js`、`stores/research.js` 和 `pages`。`Source.key` 与 `(Record.source_id, reference)` 为自然键；来源改名不会修改已冻结任务输入。业务说明在创建时保存结果快照，批准后从该快照导出。

## 误记复核（反事实搜索）

问题：已有研究某访客的首推展品是 X，哪些已记评分若被误记、且每条最多差一星，会换成别的展品，最少要改几条？

- 口径固定：矩阵、访客、参数全部取自一条**已完成**研究的冻结快照；每条改动为 `r-1` 或 `r+1` 并保持 1–5 分（贴边评分自动落在边界，仍是一条改动）；方案大小即改动条数；首推按“预测值降序、展品代码升序”判定，与生产算法一致。
- 权威复算：搜索用纯 Python 镜像快速筛选，但任何被判定为“换推”或领先差距小于 `1e-7` 的方案，都用 `services.algorithm.predict` 重新计算后才采纳；页面的“核对结论”也从原研究快照重算基准首推、逐条改动、改后首推与覆盖计数。
- 不穷举全部组合：先按“单条改动对各展品预测的最大位移”排序待查评分，启发式先尝试低成本方案；穷举按改动条数从 0 递增推进。实际演示数据（240 条评分）核对 0 至 1 条的全部组合（1 + 475 个方案）即可证明 2 条最少或找到 1 条方案。
- 预算与可证下界：搜索预算按“评估方案数”计（100–5,000,000）。预算用尽即为 `paused`，先显示 `lower_bound`（0 至下界−1 条的全部组合已核对，因此不存在更少改动的方案）、已找到的方案和每层已查计数；只有最小层全部核对完才报“最少”，全部无解报“范围内无方案”。不把已找到的方案谎称为最少。
- 中断与续查：任务保存 `order`、`cursor`、`checked`、`nodes` 与方案，分块提交；`checked` 与游标同一事务推进，重试只重算未提交的块，不重复计入已查分支。同一研究 + 访客 + 参数 + 待查评分集合的指纹相同即续用同一任务，返回原任务而不新建，保证恢复到同一评分版本。
- 原研究保留：复核只读取 `run.snapshot`，不改动研究结果、说明或快照；核对失败（例如快照校验和变化使指纹不符）会明确显示“不一致”。

接口：`GET /runs/{id}/counterfactual/options`、`GET|POST /runs/{id}/counterfactual`、`GET /counterfactual/{id}`、`POST /counterfactual/{id}/resume`、`POST /counterfactual/{id}/verify`。worker 使用独立 `counterfactual:queue`。

## 算法与口径

用户均值中心化相似邻居协同过滤与留一评分RMSE，全部计算位于 `backend/src/services/algorithm.py`，前端不拟合模型、不生成模拟结果。输入是冻结业务观察及参数，输出summary、专属诊断、逐行计算明细、seed与输入SHA256。CSV包含全部结果行；页面仅预览前30行。参数：

- `neighbors`：相似邻居上限，默认 `5`。
- `min_common`：共同评分下限，默认 `2`。

访客均值中心化的余弦相似度，仅使用正相似邻居；每位访客留一评分验证，留出评分不进入训练。无邻居时回退展品均值。

失败态：业务字段缺失/非有限数值、参数越界、样本不足、退化特征或结构错误均明确拒绝或将任务标failed，错误消息保存在任务中。可回到来源或配置修正后重新执行，历史失败记录保留。

已知边界：显式1–5评分，不是隐式行为推荐；按展品代码确定性留一，不能当作时间外推验证。相似度只取正值，至少两个共同评分；回退会显示依据。生产建议在完成评估后用全量已知评分，不将训练评分误报验证。

## 演示数据与一天剧本

固定seed=42，3个来源 ` WP-202611-01/02/03`，每来源240条，共720条业务记录；三个账号和首轮种子任务。自然键检查后仅插入缺失数据，连跑seed不重复、不覆盖用户修改。`SEED_DEMO=true` 时启动自动执行；失败记录日志但不阻塞服务。首轮任务同样由独立worker真实计算。

1. 09:00 负责人查看三份来源和720条样本。
2. 09:10 分析师选择 `WP-202611-01 秋季常规组`，核对 `exhibit-neighbors-202611-01.csv`。
3. 09:15 在“试展评分册”核对匿名访客（`visitor`）、展品代号（`exhibit`）、评分 1–5（`rating`），追加编号 `WP-NEW-001` 的样本。
4. 09:20 新建“十一月复核研究”，按默认参数冻结241条输入。
5. 09:21 独立worker消费Redis队列，返回实算结果与输入校验和。
6. 09:25 分析师查看专属诊断及逐行结果，并下载CSV复算。
7. 09:30 提交“十一月业务复核意见”，负责人核对并批准。
8. 09:40 viewer下载批准快照，查看审计中的worker完成与批准事件。
9. 09:50 分析师在“误记复核”选择访客，先以较小预算发起，看到可证下界与已找到方案；预算用尽后点“续查”沿用同一评分版本，证齐后核对结论。

## 目录与本地开发

```text
frontend/src/api, stores, types, components/common, hooks, pages, router, utils, constants
backend/src/routes, controllers, services, models, schemas, middlewares, utils, constants, config
backend/tests/test_counterfactual_engine.py, test_counterfactual_api.py
backend/scripts/seed_demo.py
backend/migrations/001_initial.sql
```

本地后端：Python3.11虚拟环境安装 `backend/requirements.txt`，设置 `DATABASE_URL`、`REDIS_URL`、`ARTIFACT_DIR`，在backend目录运行 `uvicorn src.main:app --reload`；另开终端运行 `python -m src.worker`。原生前端无需编译，推荐以Compose Nginx访问，保证 `/api/` 代理。前端为Google Fonts双字体、oklch色板，离线字体回退serif；1280/768/390响应布局。

| 环境变量 | 默认与说明 |
|---|---|
| COMPOSE_PROJECT_NAME | `exhibit-neighbors`，隔离容器/网络/命名卷 |
| FRONTEND_PORT / BACKEND_PORT / DB_PORT | `18249 / 19249 / 17749` |
| SEED_DEMO | `true` |
| JWT_SECRET | `.env.example`演示值，部署前修改 |
| DATABASE_URL / REDIS_URL | Compose内部连接，定义在compose的env锚点 |
| ARTIFACT_DIR | `/artifacts`，后端worker共享 |

共享枚举：`backend/src/constants/enums.py` 与 `frontend/src/constants/enums.js` 都定义 admin/analyst/viewer、queued/running/completed/failed、draft/approved。后端service检查对应字符串；README为全量值说明。共享前端组件table/status/chart/business跨总览、结果、配置与复核复用；actions hook和format utils负责交互/格式。后端errors与rate_limit是两个中间件。

测试及截图在批根 `test-artifacts/gb-0045/`，不混入源码。独立算法oracle、HTTP完整流程和真实Playwright浏览器流程见该目录 `TESTING.md`。License：MIT。
