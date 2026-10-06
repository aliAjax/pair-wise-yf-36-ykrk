# 生物样本库知情同意与撤回

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8302`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8302
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `participant`：参与者；`consent`：同意版本；`sample`：样本；`withdrawal`：撤回申请。
- `dispatch`：外发发放单。出库时登记接收机构（`recipient_org`）、检测用途（`purpose`）和样本（`sample_ids`），
  样本同步转为 `on_loan`，发放单停留在 `pending_receipt` 等待回执。
- `receipt`：接收机构回执。对账成功为 `reconciled`；断网或发放单已被冻结时为 `suspended`，网络恢复后可重放。

发放单状态机：

- `pending_receipt` → 回执对账成功 → `acknowledged`（已出检测结果）。
- 撤回同意批准时级联：未回执的发放单 `freeze` → `frozen`（记录冻结前状态）→ 召回 `recall_complete` → `recalled`，样本回到在库；
  已确认的发放单冻结后只能 `return`（实物退回，样本回库）或 `destroy`（实物销毁，样本销毁）。
- 回执对账与本地冻结在同一 SQLite 写事务层面互斥（`BEGIN IMMEDIATE` + 行版本校验），并发时只有一边成功，
  失败方整笔回滚；回执落败时登记为 `suspended`，不会两边都改。

## 外发与回执接口

- `POST /api/dispatches`：出库创建发放单（admin/biobank），必填 `recipient_org`、`purpose`、`sample_ids`、`issued_at`。
- `POST /api/receipts`：接收机构提交回执（role=`recipient`），必填 `dispatch_id`、`received_at`、`result_status`；
  可带 `received_sample_ids` 与本地发放单对账。
- `GET /api/dispatches?status=frozen`：按状态查看发放单（页面同样支持）。
- `GET /api/network` / `POST /api/network`（admin，`{"online": false}`）：查看/切换回执通道网络状态。
- `POST /api/receipts/retry`（admin/biobank）：网络恢复后重放全部挂起回执；
  发放单已冻结的回执保持 `suspended` 并记录原因，等待退回/销毁处置。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
