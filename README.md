# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`（按当前累计剂量重算的优先级降序、期限升序排列）
- `POST /api/items`（可提交`dose_limit`剂量限值，缺省等于`threshold`调查水平）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/readings`，登记一路探测器读数：`instrument_id`、`measured_at`（ISO 8601）、`raw_dose`、`background`、`source`
- `GET /api/items/{id}/readings`，列出全部历史版本；加`?effective=1`只看各仪器最新有效版本
- `POST /api/items/{id}/readings/{reading_id}/confirm`，确认读数
- `POST /api/items/{id}/readings/{reading_id}/corrections`，对已确认读数追加更正版本，必须填写`reason`
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 读数与累计规则

- 一次监测的多路探测器读数是事件下的独立记录，同一仪器同一测量时间的重复上传会被拒绝（409）。
- 读数只追加不改写：确认后如需修改，只能通过更正接口追加新版本并填写原因，历史版本永久可查。
- 事件累计剂量 = 各仪器最新有效版本的`max(0, raw_dose - background)`之和，每次读数变动后自动重算。
- 扣除本底后的累计总量达到调查水平（`threshold`）时事件自动进入`investigation`；达到剂量限值（`dose_limit`）时自动进入`follow_up`并要求补医学随访。
- 优先级、剩余报告时间（`remaining_hours`）和列表顺序均按当前累计结果实时重算；已关闭事件拒绝再登记或更正读数。
- 所有读数上传、确认、更正和累计重算都会写入SHA-256审计链。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
