# 纸样厚度计数核对 API

登记一批纸样的总片数，分次提交互可重叠的厚度区间计数（整数微米，区间
`[lo, hi)`，含起点不含终点）。给定合格区间，返回**同时满足全部已接收计数**
时合格片数可达到的最小值和最大值。计算按端点做整数规划，不逐片展开。

补报与已有计数不相容时**整份拒收**（HTTP 409），已有结果保持不变、仍可查询。

## 快速开始

```bash
docker compose up --build
```

打开 <http://localhost:8000/docs> 录入与查询。SQLite 数据保存在命名卷
`inspection-data`（容器内 `/data/inspection.db`）中，重启容器不丢失。

本地不用 Docker 时：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/inspections` | 登记总片数（`total_pieces`，可选 `note`） |
| `GET` | `/inspections` | 列出全部登记 |
| `GET` | `/inspections/{id}` | 查看登记及已接收的分次补报 |
| `PATCH` | `/inspections/{id}` | 更正总片数/备注；新总片数与已有计数冲突则拒收 |
| `POST` | `/inspections/{id}/reports` | 提交一批区间计数 `{"segments":[{"lo","hi","count"}]}` |
| `GET` | `/inspections/{id}/reports` | 查看补报历史 |
| `GET` | `/inspections/{id}/bounds?qualified_lo=&qualified_hi=` | 查询合格片数上下界 |
| `GET` | `/health` | 健康检查 |

### 示例

```bash
curl -X POST localhost:8000/inspections \
  -H 'content-type: application/json' -d '{"total_pieces":100}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":70}]}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":50,"hi":70,"count":60}]}'

curl 'localhost:8000/inspections/1/bounds?qualified_lo=50&qualified_hi=60'
# {"inspection_id":1,"total_pieces":100,"qualified_lo":50,"qualified_hi":60,
#  "min_count":30,"max_count":60}

# 冲突补报：整份 409 拒收，上面的结果不受影响
curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":71}]}'
# {"detail":"补报与已有计数（或本报告内部）不相容，整份拒收；已有结果保持不变"}
```

## 计算方法

把所有区间端点（含查询端点与厚度零点）排序，将厚度轴切成原子区间。落在同一
原子区间内的整数厚度在所有计数约束下等价，因此每个原子只需一个非负整数变量
（片数）。设前缀和为 `S_k`：

* 区间 `[lo, hi)` 计数为 `c` ⇔ `S_j - S_i = c`；
* 总片数 `N` ⇔ `S_last - S_0 = N`；
* 原子片数非负 ⇔ `S_k <= S_{k+1}`。

目标“合格区间片数”为 `S_hi - S_lo`。其最大值等于差分约束图上 `lo → hi` 的
最短路，最小值等于 `hi → lo` 最短路取负。约束矩阵为连续一矩阵（全幺模），
最短路给出的整数解即原整数规划的精确最优解。负环 ⇔ 计数互相矛盾。

复杂度只取决于端点数量与图规模，与总片数无关；实现使用 SPFA 求最短路，
负环触发时用 Bellman-Ford 复核，避免误判。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

* `tests/test_solver.py`：重叠区间、相邻不重叠区间、零计数、冲突检测、
  大数量不展开，以及 40 组与逐片枚举对拍。
* `tests/test_api.py`：登记、补报、跨批次重叠、跨批次冲突整份拒收、
  单份报告内部冲突拒收、拒收后原结果可查、`PATCH` 冲突拒收、参数校验、`/docs`。
* `tests/test_concurrency.py`：并发冲突补报只有一个成功；并发相容补报全部接收。
