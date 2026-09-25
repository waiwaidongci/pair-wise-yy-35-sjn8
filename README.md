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
- `GET /api/items`（按当前累计剂量重算的优先级降序排列，支持`?status=`过滤）
- `POST /api/items`（可传`dose_limit`剂量限值，默认50）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/readings`：登记一路探测器读数，必填`instrument_id`、`measured_at`（ISO 8601，统一转UTC）、`raw_dose`、`background`、`source`；同一仪器同一时间重复上传返回409
- `GET /api/items/{id}/readings`：默认返回全部历史版本，`?effective=1`只看当前有效版本
- `POST /api/items/{id}/readings/{reading_id}/confirm`：确认读数（radiation_officer）
- `POST /api/items/{id}/readings/correct`：对已确认读数追加更正版本，必须填写`reason`，更正版确认前累计仍按原版本计算
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`（支持`?item_id=`过滤）

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。读数上传：dosimetrist/radiation_officer；确认：radiation_officer；更正：radiation_officer/health_physicist。

## 读数与累计规则

- 待确认（pending）读数不计入累计；确认后不能改写，只能追加更正版本并填写原因。
- 累计剂量 = 各路仪器最新已确认版本的`max(0, raw_dose - background)`之和；无已确认读数时回退到登记总量`quantity`。
- 扣除本底后的累计值超过调查水平`threshold`时`escalation_required=true`（须进入调查）；达到剂量限值`dose_limit`时`follow_up_required=true`（须补医学随访）。
- 优先级、剩余报告时间`remaining_hours`和列表顺序都按当前累计结果实时重算；历史版本与SHA-256审计链始终可查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
