# GarminSyncData: Strava 增量同步到本地并导入 Obsidian

这个仓库提供一个最小可用方案：

1. 定时从 Strava API 拉取**增量活动数据**。
2. 保存原始 JSON 到本地（便于回溯）。
3. 生成 Obsidian 可读的 Markdown 日记文件。

## 目录结构

- `sync_strava.py`：主同步脚本。
- `config.example.json`：配置示例（复制后改名为 `config.json`）。
- `requirements.txt`：Python 依赖。
- `data/`：本地数据目录（首次运行自动创建）。
  - `state.db`：同步游标（`last_activity_epoch`）。
  - `activities/`：按活动 ID 存储原始 JSON。
- `obsidian/`：输出 Markdown。

## 1) 创建 Strava API 应用并拿到配置

在 Strava 开发者后台创建应用，拿到：

- `client_id`
- `client_secret`
- `refresh_token`

## 2) 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3) 配置 `config.json`

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
  "strava": {
    "client_id": "your_client_id",
    "client_secret": "your_client_secret",
    "refresh_token": "your_refresh_token"
  },
  "paths": {
    "data_dir": "data",
    "obsidian_dir": "obsidian"
  }
}
```

## 4) 手动运行

```bash
python sync_strava.py
```

首次会全量拉取（按分页），之后会基于最新 `start_date` 做增量。

## 5) 配置定时任务（cron）

示例：每 30 分钟同步一次。

```cron
*/30 * * * * cd /path/to/GarminSyncData && /path/to/GarminSyncData/.venv/bin/python sync_strava.py >> sync.log 2>&1
```

## 6) Obsidian 使用建议

- 把仓库内 `obsidian/` 目录软链接到你的 vault，或把 `paths.obsidian_dir` 指向 vault 某个子目录。
- 每条活动会产出一个 Markdown 文件，文件名类似：`2026-05-09-123456789.md`。


## 7) 如何测试（建议按顺序）

### A. 配置检查（不访问网络）

1. 先复制模板并填写真实配置：

```bash
cp config.example.json config.json
```

2. 语法检查：

```bash
python -m py_compile sync_strava.py
```

### B. 首次联调（真实访问 Strava）

```bash
python sync_strava.py
```

预期结果：

- 终端输出类似 `sync done, new activities: N, cursor: old -> new`。
- 生成 `data/state.db`。
- 在 `data/activities/` 下看到 `*.json`。
- 在 `obsidian/` 下看到 `YYYY-MM-DD-<id>.md`。

### C. 验证“增量同步”是否生效

连续执行两次：

```bash
python sync_strava.py
python sync_strava.py
```

预期结果：

- 如果两次之间没有新活动，第二次 `new activities` 应该接近 0。
- `cursor` 不应回退。

### D. 定时任务自测（不等 30 分钟）

先手动执行 2~3 次确认稳定，再加 cron：

```cron
*/30 * * * * cd /path/to/GarminSyncData && /path/to/GarminSyncData/.venv/bin/python sync_strava.py >> sync.log 2>&1
```

并查看日志：

```bash
tail -f sync.log
```

### E. 常见问题排查

- `config file not found`：未创建 `config.json`。
- `invalid config json`：`config.json` 格式错误（多逗号/引号不匹配）。
- `token refresh failed`：`client_id/client_secret/refresh_token` 不正确。
- `activities fetch failed: 401`：token 无效或应用权限不足。
- `activities fetch failed: 429`：触发速率限制，稍后重试。


## 8) Lap 导出到 CSV

- 每个新增活动会额外请求活动详情接口，并提取 lap 数据写入 `data/laps.csv`。
- 同时保存详情原始 JSON 到 `data/laps/<activity_id>_detail.json`。
- lap 优先级：
  1. 使用 `laps`（间歇等精细 lap）。
  2. 若无 `laps`，使用 `splits_standard`。
  3. 若非间歇且仍无 lap，则按 1 公里自动切分默认 lap。
- `data/laps.csv` 会做去重（`activity_id + lap_id + lap_index`）。
