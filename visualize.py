"""
Plotly 可视化：复刻抖音资金流向复盘图（左折线 + 右榜单）
2026-07-06

用法：
    python visualize.py                # 用真实接口
    python visualize.py --mock         # 用 mock 数据（沙箱/离线预览）
"""

import argparse
import random
from datetime import datetime, timedelta
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

try:
    from fund_flow_fetcher import fetch_sector_rank, fetch_sector_intraday
except ImportError:
    fetch_sector_rank = None
    fetch_sector_intraday = None


# ===================================================================
# Mock 数据（离线预览时用）——模拟 7 个热门板块的分钟级资金流向
# ===================================================================
def build_mock_data():
    """构造 mock 数据，模拟真实盘中形态"""
    sectors = [
        "人形机器人", "固态电池", "算力租赁", "半导体设备",
        "AI芯片", "创新药", "光伏设备",
    ]
    times = []
    t = datetime(2026, 7, 4, 9, 30)
    end_am = datetime(2026, 7, 4, 11, 30)
    end_pm = datetime(2026, 7, 4, 15, 0)
    while t <= end_am:
        times.append(t.strftime("%H:%M"))
        t += timedelta(minutes=5)
    t = datetime(2026, 7, 4, 13, 0)
    while t <= end_pm:
        times.append(t.strftime("%H:%M"))
        t += timedelta(minutes=5)

    random.seed(42)
    hist = {}
    final_val = {}
    for i, s in enumerate(sectors):
        # 模拟：主力资金一路走高或下探，带随机波动
        trend = random.choice([1, 1, 1, -1])
        base = random.uniform(0.5, 2.0)
        vals = []
        cur = 0
        for k in range(len(times)):
            cur += trend * base * random.uniform(0.05, 0.3) + random.gauss(0, 0.15)
            vals.append(round(cur, 2))
        hist[s] = pd.DataFrame({"时间": times, "主力净流入": vals})
        final_val[s] = vals[-1]

    # 排行榜按最终值排序
    df_rank = pd.DataFrame([
        {"板块名称": s, "主力净流入": final_val[s],
         "涨跌幅%": round(random.uniform(-2, 8), 2)}
        for s in sectors
    ]).sort_values("主力净流入", ascending=False).reset_index(drop=True)
    return hist, df_rank


# ===================================================================
# 复刻抖音风格的组合图
# ===================================================================
def build_dashboard(hist_data: dict, df_rank: pd.DataFrame, title_ts: str) -> go.Figure:
    """
    hist_data: {板块名: DataFrame[时间, 主力净流入]}
    df_rank: 板块排行 DataFrame
    """
    fig = make_subplots(
        rows=1, cols=2,
        column_widths=[0.72, 0.28],
        specs=[[{"type": "scatter"}, {"type": "table"}]],
        subplot_titles=(f"日内板块主力资金流向（{title_ts}）", "板块资金榜"),
        horizontal_spacing=0.04,
    )

    # 左：多板块折线叠加 — 高区分度配色 + 不同线型 + 不同标记
    palette = [
        "#E74C3C", "#2980B9", "#27AE60", "#E67E22", "#8E44AD",
        "#16A085", "#D35400", "#2C3E50", "#C0392B", "#2471A3",
        "#1E8449", "#B9770E",
    ]
    dash_styles = ["solid", "dot", "dash", "dashdot", "longdash",
                   "solid", "dot", "dash", "dashdot", "longdash",
                   "solid", "dot"]
    marker_symbols = ["circle", "diamond", "square", "triangle-up", "cross",
                      "x", "triangle-down", "star", "hexagon", "pentagon",
                      "hourglass", "bowtie"]

    for i, (name, df) in enumerate(hist_data.items()):
        final = df["主力净流入"].iloc[-1]
        c = palette[i % len(palette)]
        label = f"{name}  {final:+.2f}亿"
        fig.add_trace(
            go.Scatter(
                x=df["时间"],
                y=df["主力净流入"],
                mode="lines+markers",
                name=label,
                line=dict(color=c, width=2.8, dash=dash_styles[i % len(dash_styles)]),
                marker=dict(
                    size=5, color=c,
                    symbol=marker_symbols[i % len(marker_symbols)],
                    line=dict(width=1, color="white"),
                ),
                hovertemplate="<b>%{fullData.name}</b><br>%{x}  净流入: %{y:.2f}亿<extra></extra>",
            ),
            row=1, col=1,
        )

    fig.update_xaxes(
        title_text="时间", row=1, col=1,
        showgrid=True, gridcolor="#e8e8e8", tickangle=-45, nticks=15,
    )
    fig.update_yaxes(
        title_text="主力净流入（亿元）", row=1, col=1,
        showgrid=True, gridcolor="#e8e8e8",
        zeroline=True, zerolinecolor="#555", zerolinewidth=1.5,
    )

    # 右：榜单表格
    top = df_rank.head(12).copy()
    top.insert(0, "排名", range(1, len(top) + 1))
    top["主力净流入"] = top["主力净流入"].apply(lambda x: f"{x:+.2f}亿")
    # 涨跌幅可能缺失（降级数据），兼容处理
    if "涨跌幅%" in top.columns:
        top["涨跌幅%"] = top["涨跌幅%"].apply(lambda x: f"{x:+.2f}%")
    else:
        top["涨跌幅%"] = "-"

    # 颜色：红涨绿跌（A 股习惯）
    def cell_color(val):
        try:
            v = float(val.replace("亿", "").replace("%", "").replace("+", ""))
            if v > 0: return "#fff0f0"
            if v < 0: return "#f0fff0"
        except: pass
        return "white"

    inflow_colors = [cell_color(v) for v in top["主力净流入"]]
    change_colors = [cell_color(v) for v in top["涨跌幅%"]]

    fig.add_trace(
        go.Table(
            header=dict(
                values=["#", "板块", "净流入", "涨跌"],
                fill_color="#34495e",
                font=dict(color="white", size=13),
                align="center",
                height=32,
            ),
            cells=dict(
                values=[top["排名"], top["板块名称"], top["主力净流入"], top["涨跌幅%"]],
                fill_color=[
                    ["white"] * len(top),
                    ["white"] * len(top),
                    inflow_colors,
                    change_colors,
                ],
                align=["center", "left", "right", "right"],
                font=dict(size=12),
                height=28,
            ),
        ),
        row=1, col=2,
    )

    fig.update_layout(
        height=600,
        margin=dict(l=50, r=30, t=90, b=50),
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.02,
            xanchor="center", x=0.5,
            font=dict(size=12),
            bgcolor="rgba(255,255,255,0.95)",
            bordercolor="#ddd",
            borderwidth=1,
        ),
        plot_bgcolor="white",
        paper_bgcolor="white",
        title=dict(
            text=f"<b>A 股板块资金流向复盘</b> · 数据源：东方财富 · 更新 {title_ts}",
            x=0.5, xanchor="center",
            font=dict(size=17),
        ),
    )
    return fig


# ===================================================================
# 主入口
# ===================================================================
def main(use_mock: bool = False):
    if use_mock or fetch_sector_rank is None:
        print("使用 mock 数据...")
        hist_data, df_rank = build_mock_data()
        ts = "2026-07-04 15:00（模拟数据）"
    else:
        print("拉取实时数据中...")
        df_rank = fetch_sector_rank(sector_type="concept", top_n=15)
        if df_rank.empty:
            print("接口无响应，回落到 mock")
            hist_data, df_rank = build_mock_data()
            ts = datetime.now().strftime("%Y-%m-%d %H:%M（模拟）")
        else:
            hist_data = {}
            for _, row in df_rank.head(7).iterrows():  # 前 7 名画折线
                df = fetch_sector_intraday(row["板块代码"])
                if not df.empty:
                    hist_data[row["板块名称"]] = df[["时间", "主力净流入"]]
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    fig = build_dashboard(hist_data, df_rank, ts)
    out_html = "fund_flow_dashboard.html"
    fig.write_html(out_html, include_plotlyjs="cdn")
    print(f"✅ 已生成 {out_html}")
    return fig


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="使用 mock 数据（离线预览）")
    args = parser.parse_args()
    main(use_mock=args.mock)
