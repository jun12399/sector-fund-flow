"""
A 股板块资金流向实时监控面板
2026-07-07

启动方式：
    streamlit run app_streamlit.py
浏览器打开 http://localhost:8501

功能：
- 启动即自动采集数据（无需打开页面）
- 闭市后自动显示全天完整走势
- 支持历史任意日期回看
"""

import time
from datetime import datetime, date, timedelta
import streamlit as st

from fund_flow_fetcher import (
    fetch_sector_rank, fetch_sector_intraday, is_trading_time,
    get_last_error, get_intraday_series, get_snapshot_count,
    load_day_data, get_available_dates, stop_collector,
)
from visualize import build_dashboard, build_mock_data


# ── 页面配置 ──
st.set_page_config(page_title="A股板块资金流向", layout="wide", page_icon="📈")
st.title("📈 A 股板块资金流向实时监控")

# ── 侧边栏 ──
with st.sidebar:
    st.header("⚙️ 设置")

    # 模式选择
    view_mode = st.radio(
        "查看模式",
        ["实时监控", "历史回看"],
        format_func=lambda x: f"📡 {x}" if x == "实时监控" else f"📅 {x}",
    )

    sector_type = st.radio(
        "板块类型",
        ["concept", "industry"],
        format_func=lambda x: "概念板块" if x == "concept" else "行业板块",
    )

    # 历史回看模式：日期选择
    hist_date = None
    if view_mode == "历史回看":
        available = get_available_dates()
        if available:
            hist_date = st.selectbox("选择日期", available, index=0)
        else:
            st.warning("暂无历史数据")
            hist_date = None

    top_line_n = st.slider("折线板块数量", 3, 12, 7)
    top_rank_n = st.slider("榜单前几名", 10, 50, 20)

    if view_mode == "实时监控":
        refresh_sec = st.slider("刷新间隔（秒）", 60, 600, 180, step=30,
                                help="3 分钟 = 180；5 分钟 = 300")
        use_mock = st.checkbox("使用模拟数据", value=False)

    manual_refresh = st.button("🔄 立即刷新")

placeholder = st.empty()
status_bar = st.empty()


def _build_fallback_rank(sector_type: str, top_n: int):
    """API 不可用时，从累加器构建临时排行"""
    from fund_flow_fetcher import load_day_data
    today = datetime.now().strftime("%Y-%m-%d")
    all_data = load_day_data(today, sector_type)
    if not all_data:
        return __import__("pandas").DataFrame()
    rows = []
    for name, df in all_data.items():
        if not df.empty:
            rows.append({"板块名称": name, "主力净流入": df["主力净流入"].iloc[-1]})
    if not rows:
        return __import__("pandas").DataFrame()
    df = __import__("pandas").DataFrame(rows)
    df = df.sort_values("主力净流入", ascending=False).head(top_n).reset_index(drop=True)
    return df


# ── 渲染函数 ──
def render_realtime():
    """实时监控模式"""
    with placeholder.container():
        if use_mock:
            hist_data, df_rank = build_mock_data()
            ts = f"{datetime.now():%Y-%m-%d %H:%M:%S}（模拟数据）"
        else:
            df_rank = fetch_sector_rank(sector_type=sector_type, top_n=top_rank_n)
            if df_rank.empty:
                # API 暂时不可用，用累加器最新数据降级展示
                st.warning("⚠️ 实时排行 API 暂时无响应，以下为后台已采集的最新数据")
                df_rank = _build_fallback_rank(sector_type, top_rank_n)
                if df_rank.empty:
                    st.error("❌ 暂无任何数据，请等待后台采集或检查网络")
                    return

            hist_data = {}
            failed = 0
            for _, row in df_rank.head(top_line_n).iterrows():
                name = row["板块名称"]
                df = get_intraday_series(name, sector_type)
                if not df.empty:
                    hist_data[name] = df[["时间", "主力净流入"]]
                else:
                    failed += 1
            ts = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
            if failed > 0:
                st.warning(f"⚠️ {failed}/{top_line_n} 个板块暂无分钟数据（后台采集中）")

        fig = build_dashboard(hist_data, df_rank, ts)
        st.plotly_chart(fig, use_container_width=True, config={
            "locale": "zh-CN",
            "displaylogo": False,
            "toImageButtonOptions": {"filename": "资金流向", "format": "png"},
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        })

        with st.expander("📋 完整榜单明细"):
            st.dataframe(df_rank, use_container_width=True)

    n = get_snapshot_count()
    status_bar.info(
        f"最近刷新：{datetime.now():%H:%M:%S}  |  "
        f"下次刷新：{refresh_sec}秒后  |  "
        f"已采集：{n}轮  |  "
        f"交易时段：{'✅ 是' if is_trading_time() else '❌ 已闭市'}"
    )


def render_history():
    """历史回看模式"""
    with placeholder.container():
        if not hist_date:
            st.info("📅 请先选择一个日期")
            return

        all_data = load_day_data(hist_date, sector_type)
        if not all_data:
            st.warning(f"📅 {hist_date} 暂无数据记录")
            return

        # 按最新值排序取 top N
        latest_vals = {}
        for name, df in all_data.items():
            if not df.empty:
                latest_vals[name] = df["主力净流入"].iloc[-1]

        sorted_names = sorted(latest_vals, key=lambda x: latest_vals[x], reverse=True)[:top_rank_n]

        # 构建排行 DataFrame
        df_rank_data = []
        for i, name in enumerate(sorted_names):
            df_rank_data.append({
                "板块名称": name,
                "主力净流入": latest_vals[name],
                "板块代码": "",
                "涨跌幅%": 0,
            })
        df_rank = __import__("pandas").DataFrame(df_rank_data)

        # 取 top N 折线
        hist_data = {}
        for name in sorted_names[:top_line_n]:
            df = all_data[name]
            if not df.empty:
                hist_data[name] = df[["时间", "主力净流入"]]

        ts = f"{hist_date}（历史回看）"
        fig = build_dashboard(hist_data, df_rank, ts)
        st.plotly_chart(fig, use_container_width=True, config={
            "locale": "zh-CN",
            "displaylogo": False,
            "toImageButtonOptions": {"filename": f"资金流向_{hist_date}", "format": "png"},
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        })

        with st.expander("📋 完整榜单明细"):
            st.dataframe(df_rank, use_container_width=True)

    status_bar.info(
        f"📅 查看日期：{hist_date}  |  "
        f"板块类型：{'概念板块' if sector_type == 'concept' else '行业板块'}  |  "
        f"数据点数：{get_snapshot_count()}轮"
    )


# ── 主逻辑 ──
if view_mode == "实时监控":
    render_realtime()
else:
    render_history()

if not manual_refresh and view_mode == "实时监控":
    time.sleep(refresh_sec)
    st.rerun()
