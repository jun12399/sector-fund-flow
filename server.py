"""
A 股板块资金流向 — FastAPI 数据服务
2026-07-13

启动方式：
    python server.py
    uvicorn server:app --host 0.0.0.0 --port 8501

数据采集管道 (fund_flow_fetcher) 完全不变，
图表渲染由浏览器端 Plotly.js 完成。
"""

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

from fund_flow_fetcher import (
    _cleanup_old_files,
    _load_history,
    BackgroundCollector,
    fetch_sector_rank,
    get_available_dates,
    get_intraday_series,
    get_last_error,
    get_snapshot_count,
    is_trading_time,
    load_day_data,
)

# ── 静态文件目录 ──
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


# ── FastAPI 生命周期：启动/停止后台采集器 ──
_collector: BackgroundCollector | None = None

@asynccontextmanager
async def lifespan(app):
    """管理 BackgroundCollector 的启动与停止"""
    global _collector
    print("[server] 启动后台数据采集器...")
    _load_history()
    _cleanup_old_files()
    _collector = BackgroundCollector(interval_sec=180, top_n=30)
    _collector.start()
    print("[server] 后台采集器已启动 (interval=180s, top_n=30)")
    yield
    print("[server] 停止后台采集器...")
    if _collector:
        _collector.stop()  # 设置 stop_event，真正停止线程
    print("[server] 已停止")


app = FastAPI(title="A股板块资金流向", lifespan=lifespan)


# ── API ──────────────────────────────────────────────────────────


@app.get("/api/dashboard")
def api_dashboard(
    sector: str = Query("concept", pattern="^(concept|industry)$"),
    line_top_n: int = Query(10, ge=1, le=12),
    line_bottom_n: int = Query(10, ge=1, le=12),
    rank_top_n: int = Query(10, ge=5, le=30),
    rank_bottom_n: int = Query(10, ge=5, le=30),
    date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    统一数据接口。
    - 不传 date → 实时模式（拉取 API 排行 + 内存累加器时序）
    - 传 date   → 历史回看（读取磁盘 JSONL）

    line_top_n / line_bottom_n: 折线图取流入前N + 流出后N
    rank_top_n / rank_bottom_n: 榜单取流入前N + 流出后N
    """
    error = ""
    rank: list[dict] = []
    intraday: dict[str, list[list]] = {}

    if date is None:
        # ── 实时模式：拉取全量板块（fetch_sector_rank 自动分页）──
        df_rank = fetch_sector_rank(sector_type=sector, top_n=600)
        if df_rank.empty:
            error = get_last_error() or "API 无响应，请等待后台采集"

            # 降级：从累加器构建简易排行
            from fund_flow_fetcher import _ts_accumulator, _ts_lock
            prefix = "c" if sector == "concept" else "i"
            latest: dict[str, float] = {}
            with _ts_lock:
                for key, pts in _ts_accumulator.items():
                    if key.startswith(f"{prefix}:") and pts:
                        latest[key[2:]] = pts[-1][1]
            sorted_names = sorted(latest, key=latest.get, reverse=True)[:fetch_size]
            for name in sorted_names:
                rank.append({
                    "name": name, "code": "",
                    "inflow": round(latest[name], 2),
                    "change_pct": 0,
                })
        else:
            names_seen = set()
            for _, row in df_rank.iterrows():
                name = str(row["板块名称"])
                if name in names_seen:
                    continue
                names_seen.add(name)
                rank.append({
                    "name": name,
                    "code": str(row.get("板块代码", "")),
                    "inflow": round(float(row.get("主力净流入", 0)), 2),
                    "change_pct": round(float(row.get("涨跌幅%", 0)), 2),
                })

        # 折线时序数据：前 N + 后 N
        line_names = [r["name"] for r in rank[:line_top_n]]
        if line_bottom_n > 0:
            for r in rank[-line_bottom_n:]:
                line_names.append(r["name"])
        line_names = list(dict.fromkeys(line_names))
        for name in line_names:
            df = get_intraday_series(name, sector)
            if not df.empty:
                intraday[name] = [[str(t), round(float(v), 2)]
                                  for t, v in zip(df["时间"], df["主力净流入"])]
    else:
        # ── 历史回看模式 ──
        all_data = load_day_data(date, sector)
        if not all_data:
            error = f"{date} 暂无数据记录"
        else:
            # 按最终值排序
            latest_vals = {}
            for name, df in all_data.items():
                if not df.empty:
                    latest_vals[name] = float(df["主力净流入"].iloc[-1])
            sorted_names = sorted(latest_vals, key=latest_vals.get, reverse=True)

            for name in sorted_names:
                rank.append({
                    "name": name, "code": "",
                    "inflow": round(latest_vals[name], 2),
                    "change_pct": 0,
                })

            # 折线时序：前 N + 后 N
            line_names = [name for name in sorted_names[:line_top_n]]
            if line_bottom_n > 0:
                for name in sorted_names[-line_bottom_n:]:
                    line_names.append(name)
            line_names = list(dict.fromkeys(line_names))
            for name in line_names:
                df = all_data[name]
                if not df.empty:
                    intraday[name] = [[str(t), round(float(v), 2)]
                                      for t, v in zip(df["时间"], df["主力净流入"])]

    return {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_trading": is_trading_time(),
        "snapshot_count": get_snapshot_count(),
        "rank": rank,
        "intraday": intraday,
        "error": error,
        "total_rank": len(rank),
    }


@app.get("/api/dates")
def api_dates():
    """返回所有有数据的日期"""
    return {"dates": get_available_dates()}


# ── 根路径 → 前端页面 ──


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


# ── 启动入口 ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8501, reload=False)
