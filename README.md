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
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、修正报文、并发和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本。
- 事件修订链 `event_revisions`：事件每次状态变更或台站修正都追加一版，
  记录版本号、来源台站、报文编号、是否实质性变更、变更字段、审校信息与数据快照。

## 台站修正报文

值班编目期间，台站修正报文在初审、发布和修订各阶段陆续到达：

- 报文按**台站编号**归并到事件的 `reports`，波形追加到该台站的 `waveforms`
  （同通道重复补传幂等）；同一 `station + message_id` 重传不占用新版本。
- **仅补波形**属于非实质性修正：事件原状态与审校结论（审核员、震级等）保留，
  已发布稿不撤回。
- **震中（`location`/`latitude`/`longitude`）、发震时刻（`origin_time`）或
  震级（`magnitude`）变化**为实质性修正：已发布事件撤回发布稿，事件进入
  `revision_pending`（待复核）并生成 `pending_review` 修订；审核员复核通过后
  可重新发布，待复核修订随之关闭为 `reviewed`。
- 两站同时修改时，提交事务先到先得占用版本号；后到请求读到版本冲突后会基于
  最新版本**自动重算归并**（审校意见不被覆盖）。调用方显式传 `expected_version`
  时不做重算，直接返回 `409 Conflict`。
- 历史事件没有稳定报文编号：服务启动时通过 `PRAGMA user_version` 自动迁移，
  为既有事件回填修订链起点（version 1）；历史事件仍可查询并继续修订，
  `message_id` 可空（空编号不参与幂等去重）。
- 审计表只追加：SQLite 触发器禁止 `UPDATE`/`DELETE` `audit_log`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/entities/<id>/corrections`：台站修正报文，请求体例如
  `{"station":"STA-1","message_id":"MSG-9","location":"新区","magnitude":4.3,
  "waveforms":[{"channel":"BHZ"}]}`；角色需为 `station`/`analyst`/`admin`。
- `GET /api/entities/<id>/revisions`：事件修订链，可用 `?status=pending_review` 过滤。
- `GET /api/revisions?status=pending_review`：全部待复核修订。
- `GET /api/audit`：读取审计记录，支持 `?entity_id=<id>` 过滤。

事件状态：`candidate → associated → reviewed → published`，
发布后可进入 `revision_pending`（待复核）/ `revised`（已修订）/ `withdrawn`（已撤回）。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
