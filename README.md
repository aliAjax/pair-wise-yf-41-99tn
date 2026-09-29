# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：值班演示页面，展示事件、修订链、待复核状态并可提交修正报文。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`、`?station=`过滤（台站过滤匹配事件报告中的台站编号）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/entities/<id>/corrections`：台站提交修正报文（角色`station`或`admin`），字段见下。
- `GET /api/entities/<id>/revisions`：读取事件修订链（每个版本一条快照）。
- `GET /api/audit`：读取审计记录，可用`?entity_id=<id>`按事件过滤。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 值班修正报文

台站修正报文在初审、发布、修订各阶段都会陆续到达。事件状态在原有
`candidate → associated → reviewed → published / revised`之外增加`pending_review`。

`POST /api/entities/<id>/corrections`请求体：

- `station`（必填）：台站编号，必须是已登记台站；事件按台站编号归并，同站报告覆盖（保留已有波形），新站报告追加。
- `waveform`：补波形。仅补波形时**保留审校结论与发布状态**，事件数据产生新版本快照。
- `origin_time` / `location` / `magnitude`：发震时刻、震中或震级。相对当前值有变化即为关键修正，
  对已发布（`published`/`revised`）事件会撤回发布稿（原`communication_id`连同原因记入`withdrawn_releases`），
  事件转入`pending_review`；审校人等既有字段保留，重新初审（`review`）后可再次`publish`。
- `reason`：撤回/修正原因（可选）。

并发语义：修正落地使用乐观锁。未带`expected_version`时，若版本已被其他台站先占用，
后到请求会按新版本重新归并重算（最多5次），保证先落地的审校意见不被覆盖；
显式携带过期`expected_version`则返回409，调用方需重取后再提交。

## 数据升级与审计

- 服务启动自动建表并执行`PRAGMA user_version`迁移：升级前创建的历史事件没有稳定报文编号，
  迁移会为其回填一条修订链快照，之后仍可正常查询和继续修订；迁移幂等，重复启动不会重复回填。
- 每个事件版本在`event_revisions`表留快照（含中文修订说明）。
- `audit_log`为只追加表，SQLite触发器禁止UPDATE/DELETE，审计记录不能改写。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
