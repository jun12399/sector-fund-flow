"""
Streamlit 实时监控面板 —— A 股板块资金流向
2026-07-06

启动方式：
    pip install streamlit plotly pandas
    streamlit run app_streamlit.py

浏览器打开 http://localhost:8501
"""

import time
from datetime import datetime
import streamlit as st

from fund_flow_fetcher import (
    fetch_sector_rank, fetch_sector_intraday, is_trading_time,
    get_last_error, record_snapshot, get_intraday_series,
)
from visualize import build_dashboard, build_mock_data


# --------------------------------------------------------------
st.set_page_config(page_title="A股资金流向监控", layout="wide", page_icon="📈")
st.title("📈 A 股板块资金流向实时监控")
# --------------------------------------------------------------

with st.sidebar:
    st.header("⚙️ 设置")
    sector_type = st.radio("板块类型", ["concept", "industry"],
                           format_func=lambda x: "概念板块" if x == "concept" else "行业板块")
    top_line_n = st.slider("画折线的板块数量", 3, 12, 7)
    top_rank_n = st.slider("榜单显示前几名", 10, 30, 15)
    refresh_sec = st.slider("刷新间隔（秒）", 60, 600, 180, step=30,
                             help="3 分钟 = 180；5 分钟 = 300")
    use_mock = st.checkbox("使用 mock 数据（测试/闭市）", value=not is_trading_time())
    manual_refresh = st.button("🔄 立即刷新")

placeholder = st.empty()
status_bar = st.empty()


def render_once():
    with placeholder.container():
        if use_mock:
            hist_data, df_rank = build_mock_data()
            ts = f"{datetime.now():%Y-%m-%d %H:%M:%S}（mock）"
        else:
            df_rank = fetch_sector_rank(sector_type=sector_type, top_n=top_rank_n)
            if df_rank.empty:
                err = get_last_error() or "东财 API 无响应（可能触发了反爬限流）"
                st.error(f"❌ 数据获取失败：{err}")
                st.info("💡 请切换到 mock 模式查看演示数据，或等待 1-2 分钟后刷新")
                return
            # 记录快照到时间序列累加器（替代不可用的 push2his 分钟K线API）
            record_snapshot(df_rank)

            # 从累加器读取走势数据（优先），空则尝试实时API兜底
            hist_data = {}
            failed = 0
            for _, row in df_rank.head(top_line_n).iterrows():
                name = row["板块名称"]
                df = get_intraday_series(name)
                if df.empty:
                    df = fetch_sector_intraday(row["板块代码"])
                if not df.empty:
                    hist_data[name] = df[["时间", "主力净流入"]]
                else:
                    failed += 1
            ts = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
            if failed > 0:
                st.warning(f"⚠️ {failed}/{top_line_n} 个板块暂无分钟数据（等待下一次刷新积累数据点）")

        fig = build_dashboard(hist_data, df_rank, ts)
        st.plotly_chart(fig, use_container_width=True)

        # 明细榜单
        with st.expander("📋 完整榜单明细"):
            st.dataframe(df_rank, use_container_width=True)

    status_bar.info(
        f"最近刷新：{datetime.now():%H:%M:%S}  |  "
        f"下次自动刷新：{refresh_sec}秒后  |  "
        f"交易时段：{'✅' if is_trading_time() else '❌'}"
    )


# 初次渲染
render_once()

# 自动刷新循环
if not manual_refresh:
    time.sleep(refresh_sec)
    st.rerun()
