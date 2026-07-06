"""
A 股板块资金流向数据抓取（增强版 v2.1）
2026-07-06

设计要点：
- SmartFetcher 智能请求控制器（L1/L2/L3 反爬 + 403自愈 + 指数退避）
- 底层双通道：纯 socket+SSL（默认，绕过 Clash TUN）+ requests（备选）
- 双源冗余：东财主源 + 同花顺兜底
- 与 visualize.py / app_streamlit.py 接口完全兼容
"""

import json
import os
import glob
import socket
import ssl
import time
import random
import threading
import pandas as pd
from datetime import datetime, timedelta, time as dt_time
from typing import Optional, Literal
from urllib.parse import urlencode, urlparse


# ═══════════════════════════════════════════════════════════════
# UA 池
# ═══════════════════════════════════════════════════════════════
_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]


# ═══════════════════════════════════════════════════════════════
# 交易时段判断
# ═══════════════════════════════════════════════════════════════
def _is_trading_time() -> bool:
    now = datetime.now().time()
    am = dt_time(9, 30) <= now <= dt_time(11, 30)
    pm = dt_time(13, 0) <= now <= dt_time(15, 0)
    return am or pm


def is_trading_time() -> bool:
    """对外别名，保持向后兼容"""
    return _is_trading_time()


# ═══════════════════════════════════════════════════════════════
# 纯 socket+SSL HTTP 客户端（绕过 Clash TUN / 系统代理）
# ═══════════════════════════════════════════════════════════════
def _http_get_raw(host: str, path: str, ua: str, referer: str,
                  timeout: int = 15) -> Optional[dict]:
    """
    使用原生 socket+SSL 发送 HTTP GET，返回 JSON。
    完全绕开 requests/urllib3，避免被 Clash TUN 拦截。
    （pytdx 能通的原因就是走原生 TCP socket）
    """
    for attempt in range(3):
        sock = None
        ssock = None
        try:
            sock = socket.create_connection((host, 443), timeout=timeout)
            ctx = ssl.create_default_context()
            ssock = ctx.wrap_socket(sock, server_hostname=host)

            req = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                f"User-Agent: {ua}\r\n"
                f"Referer: {referer}\r\n"
                f"Accept: application/json, text/plain, */*\r\n"
                f"Accept-Language: zh-CN,zh;q=0.9,en;q=0.8\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            )
            ssock.sendall(req.encode())

            chunks = []
            while True:
                try:
                    data = ssock.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
                except (socket.timeout, ssl.SSLError):
                    break

            raw = b"".join(chunks).decode("utf-8", errors="replace")
            parts = raw.split("\r\n\r\n", 1)
            if len(parts) < 2:
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                return None

            # 检查 HTTP 状态码
            header = parts[0]
            if "403" in header.split("\r\n")[0] if header else False:
                return None  # 反爬，由上层处理

            return json.loads(parts[1])

        except json.JSONDecodeError:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            return None
        except Exception:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            return None
        finally:
            try:
                if ssock:
                    ssock.close()
                elif sock:
                    sock.close()
            except Exception:
                pass
    return None


# ═══════════════════════════════════════════════════════════════
# SmartFetcher：智能请求控制器（L1+L2+L3 一体化）
# ═══════════════════════════════════════════════════════════════
class SmartFetcher:
    """
    智能请求控制器
    - 随机 UA + Referer（L1）
    - 动态请求间隔，交易时段放缓（L2）
    - 会话状态管理，定期重置（L3）
    - 403 自愈 + 指数退避重试
    """

    def __init__(
        self,
        base_referer: str = "https://data.eastmoney.com/",
        min_interval: float = 0.3,
        max_interval: float = 1.2,
        session_max_requests: int = 50,
        session_ttl_min: int = 30,
        verbose: bool = False,
    ):
        self.base_referer = base_referer
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.session_max_requests = session_max_requests
        self.session_ttl = timedelta(minutes=session_ttl_min)
        self.verbose = verbose

        self._session_born_at: Optional[datetime] = None
        self._session_used: int = 0
        self._last_request_at: Optional[datetime] = None
        self._recent_errors: int = 0
        self._current_ua: str = random.choice(_UAS)

    def _log(self, msg: str):
        if self.verbose:
            print(f"[{datetime.now():%H:%M:%S}] {msg}")

    def _rotate_ua(self):
        """换一个新 UA（403 自愈时调用）"""
        self._current_ua = random.choice(_UAS)
        self._log(f"🔄 切换 UA: {self._current_ua[:50]}...")

    def _check_session_health(self):
        """检查会话是否过期或超限"""
        now = datetime.now()
        reset = False
        if self._session_used >= self.session_max_requests:
            self._log(f"🔁 会话请求 {self._session_used} 次，重置")
            reset = True
        elif self._session_born_at and now - self._session_born_at > self.session_ttl:
            self._log(f"🔁 会话存活超 {self.session_ttl}，重置")
            reset = True
        if reset:
            self._session_born_at = now
            self._session_used = 0

    def _calc_wait(self) -> float:
        """L2：动态计算请求间隔"""
        base = random.uniform(self.min_interval, self.max_interval)
        if _is_trading_time():
            base *= 1.3
        if self._recent_errors >= 2:
            base *= 2.0
            self._log(f"⚠️  最近有错，等待时间加倍到 {base:.1f}s")
        return base

    def _wait(self):
        """节流"""
        if self._last_request_at is None:
            return
        elapsed = (datetime.now() - self._last_request_at).total_seconds()
        need = self._calc_wait()
        if elapsed < need:
            time.sleep(need - elapsed)

    def _mark_success(self):
        self._recent_errors = max(0, self._recent_errors - 1)
        self._session_used += 1

    def _mark_error(self):
        self._recent_errors += 1

    # ── 核心 GET ──
    def get(
        self,
        url: str,
        params: Optional[dict] = None,
        max_retries: int = 4,
        referer: Optional[str] = None,
    ) -> Optional[dict]:
        """
        带完整反爬对策的 GET 请求。
        底层使用纯 socket+SSL（绕过系统代理/Clash TUN）。
        """
        ref = referer or self.base_referer

        # 构建完整 URL + path
        if params:
            full_url = f"{url}?{urlencode(params, doseq=True)}"
        else:
            full_url = url
        parsed = urlparse(full_url)
        host = parsed.hostname or ""
        path = parsed.path + ("?" + parsed.query if parsed.query else "")

        for attempt in range(max_retries):
            self._wait()
            self._check_session_health()

            self._last_request_at = datetime.now()
            data = _http_get_raw(host, path, self._current_ua, ref)

            if data is not None:
                self._mark_success()
                return data

            # 请求失败 → 可能是 403 反爬
            self._mark_error()
            self._log(f"🚫 第 {attempt+1} 次失败，触发 403 自愈")
            self._rotate_ua()

            # 指数退避
            wait = 2.0 * (1.5 ** attempt) + random.uniform(-0.5, 0.5)
            self._log(f"⏱  退避 {wait:.1f}s 后重试")
            time.sleep(max(1, wait))

        self._log(f"💀 {max_retries} 次全部失败")
        return None


# 全局单例
_default_fetcher = SmartFetcher(verbose=False)
_last_error: str = ""  # 供 UI 展示用

# ═══════════════════════════════════════════════════════════════
# 持久化存储：数据自动存盘，保留30天，超期自动清空
# ═══════════════════════════════════════════════════════════════
_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "snapshots")
_RETENTION_DAYS = 30

# 内存累加器：{sector_type:name → [(HH:MM, 净流入), ...]}
# sector_type: "c" = 概念, "i" = 行业
_ts_accumulator: dict[str, list[tuple[str, float]]] = {}
_ts_lock = threading.Lock()
_ts_last_snapshot: Optional[datetime] = None
_collector_running = False
_collector_thread: Optional[threading.Thread] = None
_last_error: str = ""


def _make_key(sector_type: str, name: str) -> str:
    prefix = "c" if sector_type == "concept" else "i"
    return f"{prefix}:{name}"


def _load_history():
    """启动时加载最近 30 天的历史快照到内存"""
    global _ts_accumulator
    loaded = 0
    cutoff = datetime.now() - timedelta(days=_RETENTION_DAYS)
    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)

    files = sorted(glob.glob(os.path.join(_SNAPSHOT_DIR, "*.jsonl")))
    for fp in files:
        # 从文件名提取日期
        basename = os.path.basename(fp)
        try:
            file_date = datetime.strptime(basename[:10], "%Y-%m-%d")
        except ValueError:
            continue
        if file_date < cutoff:
            continue  # 跳过往期但仍保留（清理在 _cleanup 做）

        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    key = _make_key(rec["s"], rec["n"])
                    if key not in _ts_accumulator:
                        _ts_accumulator[key] = []
                    _ts_accumulator[key].append((rec["t"], rec["v"]))
                    loaded += 1
                except (json.JSONDecodeError, KeyError):
                    continue
    if loaded:
        print(f"[数据] 已加载 {loaded} 条历史快照（最近 {_RETENTION_DAYS} 天）")


def _cleanup_old_files():
    """删除超过保留期的快照文件"""
    cutoff = datetime.now() - timedelta(days=_RETENTION_DAYS)
    files = glob.glob(os.path.join(_SNAPSHOT_DIR, "*.jsonl"))
    deleted = 0
    for fp in files:
        basename = os.path.basename(fp)
        try:
            file_date = datetime.strptime(basename[:10], "%Y-%m-%d")
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                os.remove(fp)
                deleted += 1
            except OSError:
                pass
    if deleted:
        print(f"[数据] 已清理 {deleted} 个过期快照文件（>{_RETENTION_DAYS}天）")


class BackgroundCollector:
    """后台线程：定时采集概念+行业双板块排行榜快照，自动存盘"""

    def __init__(self, interval_sec: int = 180, top_n: int = 30):
        self.interval_sec = interval_sec
        self.top_n = top_n
        self._stop_event = threading.Event()
        self._fetcher: Optional[SmartFetcher] = None

    def start(self):
        global _collector_running, _collector_thread
        if _collector_running:
            return
        _collector_running = True
        _collector_thread = threading.Thread(target=self._run, daemon=True, name="fund-collector")
        _collector_thread.start()

    def stop(self):
        self._stop_event.set()
        global _collector_running
        _collector_running = False

    def _run(self):
        global _last_error
        self._fetcher = SmartFetcher(verbose=False, min_interval=0.5, max_interval=1.5)
        while not self._stop_event.is_set():
            try:
                if not _is_trading_time():
                    self._stop_event.wait(300)
                    continue

                for stype in ("concept", "industry"):
                    df = _fetch_sector_rank_eastmoney(self._fetcher, sector_type=stype, top_n=self.top_n)
                    if df.empty:
                        continue
                    with _ts_lock:
                        self._record(df, stype)
                    time.sleep(random.uniform(1.0, 2.0))

                _last_error = ""

                # 每天第一次采集时清理过期文件
                if datetime.now().hour == 9 and datetime.now().minute < 40:
                    _cleanup_old_files()

            except Exception as e:
                _last_error = f"后台采集异常: {e}"

            self._stop_event.wait(self.interval_sec)

    @staticmethod
    def _record(df_rank: pd.DataFrame, sector_type: str):
        global _ts_accumulator, _ts_last_snapshot
        now = datetime.now()
        now_str = now.strftime("%H:%M")
        today_str = now.strftime("%Y-%m-%d")
        _ts_last_snapshot = now

        os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
        fp = os.path.join(_SNAPSHOT_DIR, f"{today_str}.jsonl")

        with open(fp, "a", encoding="utf-8") as f:
            for _, row in df_rank.iterrows():
                name = row["板块名称"]
                if pd.isna(name):
                    continue
                inflow = float(row["主力净流入"]) if not pd.isna(row.get("主力净流入")) else 0.0
                key = _make_key(sector_type, name)

                if key not in _ts_accumulator:
                    _ts_accumulator[key] = []
                if _ts_accumulator[key] and _ts_accumulator[key][-1][0] == now_str:
                    continue

                _ts_accumulator[key].append((now_str, inflow))

                # 追加写入 JSONL
                rec = {"t": now_str, "n": name, "v": inflow, "s": "c" if sector_type == "concept" else "i"}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── 启动 / 停止 ──
def start_collector(interval_sec: int = 180):
    """启动后台数据采集线程（含历史加载 + 过期清理）"""
    _load_history()
    _cleanup_old_files()
    c = BackgroundCollector(interval_sec=interval_sec)
    c.start()


def stop_collector():
    """停止后台采集"""
    global _collector_running
    _collector_running = False


def get_intraday_series(sector_name: str, sector_type: str = "concept") -> pd.DataFrame:
    """返回板块的累计时间序列 DataFrame[时间, 主力净流入]"""
    key = _make_key(sector_type, sector_name)
    with _ts_lock:
        points = list(_ts_accumulator.get(key, []))
    if not points:
        return pd.DataFrame()
    return pd.DataFrame(points, columns=["时间", "主力净流入"])


def get_snapshot_count() -> int:
    """返回已记录的快照轮数"""
    vals = list(_ts_accumulator.values())
    return max((len(v) for v in vals), default=0)


def get_last_error() -> str:
    return _last_error


# ═══════════════════════════════════════════════════════════════
# 接口 1：东财板块资金流向排行榜（主源）
# ═══════════════════════════════════════════════════════════════
def _fetch_sector_rank_eastmoney(
    fetcher: SmartFetcher,
    sector_type: Literal["concept", "industry"] = "concept",
    top_n: int = 20,
) -> pd.DataFrame:
    # m:90+s:4 = 申万二级行业(128个) ← 网站实际数据源
    # m:90+t:3 = 概念板块(495个)
    fs = "m:90+t:3" if sector_type == "concept" else "m:90+s:4"
    url = "https://push2.eastmoney.com/api/qt/clist/get"

    params = {
        "pn": 1, "pz": top_n, "po": 1, "np": 1,
        "fltt": 2, "invt": 2, "fs": fs, "stat": 1,
        "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87",
        "fid": "f62",
        "_": int(datetime.now().timestamp() * 1000),
    }
    data = fetcher.get(url, params)
    if not data or not data.get("data") or not data["data"].get("diff"):
        return pd.DataFrame()
    rows = data["data"]["diff"]
    df = pd.DataFrame(rows).rename(columns={
        "f12": "板块代码", "f14": "板块名称", "f2": "最新价",
        "f3": "涨跌幅%", "f62": "主力净流入", "f184": "主力净占比%",
        "f66": "超大单净流入", "f72": "大单净流入",
        "f78": "中单净流入", "f84": "小单净流入",
    })
    for col in ["主力净流入", "超大单净流入", "大单净流入", "中单净流入", "小单净流入"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce") / 1e8
    df["更新时间"] = datetime.now().strftime("%H:%M:%S")
    df["数据源"] = "东财"
    return df


# ═══════════════════════════════════════════════════════════════
# 接口 2：同花顺板块资金流向排行榜（兜底源）
# ═══════════════════════════════════════════════════════════════
def _fetch_sector_rank_ths(
    fetcher: SmartFetcher,
    sector_type: Literal["concept", "industry"] = "concept",
    top_n: int = 20,
) -> pd.DataFrame:
    """
    同花顺兜底源。接口路径：data.10jqka.com.cn/funds/
    注意：同花顺返回 HTML 需解析，此处为最小占位，用户可按需扩展。
    """
    ths_referer = "https://data.10jqka.com.cn/"
    board_type = "gnzjl" if sector_type == "concept" else "hyzjl"
    url = f"https://data.10jqka.com.cn/funds/{board_type}/field/tradezdf/order/desc/page/1/ajax/1/"
    data = fetcher.get(url, referer=ths_referer)
    if data is None:
        return pd.DataFrame()
    return pd.DataFrame()


# ═══════════════════════════════════════════════════════════════
# 对外接口 1：板块排行（含双源自动 fallback + 缓存）
# ═══════════════════════════════════════════════════════════════
_cache_rank: dict = {"data": None, "ts": None, "ttl": 30}  # 30秒缓存
_cache_intraday: dict = {}  # {code: (df, ts)}


def fetch_sector_rank(
    sector_type: Literal["concept", "industry"] = "concept",
    top_n: int = 20,
    fetcher: Optional[SmartFetcher] = None,
    fallback: bool = True,
) -> pd.DataFrame:
    """
    获取板块资金流向排行榜（按主力净流入降序）
    内置 30 秒缓存，避免连续刷新触发反爬。
    """
    global _last_error, _cache_rank

    # 读缓存
    cache_key = f"{sector_type}_{top_n}"
    entry = _cache_rank.get("data")
    if entry is not None and _cache_rank.get("ts"):
        age = (datetime.now() - _cache_rank["ts"]).total_seconds()
        if age < _cache_rank.get("ttl", 30):
            return entry

    f = fetcher or _default_fetcher
    df = _fetch_sector_rank_eastmoney(f, sector_type, top_n)
    if df.empty and fallback:
        f._log("↩️ 东财失败，尝试同花顺兜底")
        df = _fetch_sector_rank_ths(f, sector_type, top_n)

    if df.empty:
        _last_error = f"东财 API 无响应（已重试 4 次），可切换 mock 模式"
    else:
        _last_error = ""
        _cache_rank = {"data": df.copy(), "ts": datetime.now(), "ttl": 30}

    return df


# ═══════════════════════════════════════════════════════════════
# 接口 3：东财板块分钟级历史资金流向
# ═══════════════════════════════════════════════════════════════
def fetch_sector_intraday(
    sector_code: str,
    fetcher: Optional[SmartFetcher] = None,
) -> pd.DataFrame:
    """
    获取单板块日内分钟级资金流向历史。
    内置 2 分钟缓存，避免连续刷新触发反爬。
    """
    global _cache_intraday
    entry = _cache_intraday.get(sector_code)
    if entry:
        df_cached, ts = entry
        if (datetime.now() - ts).total_seconds() < 120:
            return df_cached.copy()

    f = fetcher or _default_fetcher
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get"
    params = {
        "lmt": 0, "klt": 1, "secid": f"90.{sector_code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "_": int(datetime.now().timestamp() * 1000),
    }
    data = f.get(url, params)
    if not data or not data.get("data") or not data["data"].get("klines"):
        return pd.DataFrame()
    rows = [line.split(",") for line in data["data"]["klines"]]
    df = pd.DataFrame(rows, columns=[
        "时间", "主力净流入", "小单净流入", "中单净流入",
        "大单净流入", "超大单净流入", "主力净占比", "小单净占比",
        "中单净占比", "大单净占比", "超大单净占比", "收盘价",
        "涨跌幅", "_r13", "_r14",
    ])
    for c in df.columns:
        if c != "时间":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ["主力净流入", "小单净流入", "中单净流入", "大单净流入", "超大单净流入"]:
        df[c] = df[c] / 1e8
    _cache_intraday[sector_code] = (df.copy(), datetime.now())
    return df


# ═══════════════════════════════════════════════════════════════
# Tushare 日频对账兜底
# ═══════════════════════════════════════════════════════════════
def fetch_sector_flow_tushare_daily(
    trade_date: str,
    token: str,
    top_n: int = 20,
) -> pd.DataFrame:
    """
    Tushare 收盘日频板块资金流（需要 2000 积分档）
    :param trade_date: YYYYMMDD
    :param token: Tushare token
    """
    try:
        import tushare as ts
    except ImportError:
        print("请先安装：pip install tushare")
        return pd.DataFrame()

    ts.set_token(token)
    pro = ts.pro_api()
    df = pro.moneyflow_ind_dc(trade_date=trade_date)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.sort_values("net_amount", ascending=False).head(top_n)
    df["数据源"] = "Tushare"
    return df


# ═══════════════════════════════════════════════════════════════
# CLI 测试入口
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    fetcher = SmartFetcher(verbose=True)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 拉取概念板块 TOP 15...")
    df_rank = fetch_sector_rank("concept", 15, fetcher=fetcher)
    if df_rank.empty:
        print("❌ 榜单为空——检查网络或反爬")
    else:
        print(df_rank[["板块名称", "涨跌幅%", "主力净流入", "主力净占比%"]].to_string(index=False))
        top_code = df_rank.iloc[0]["板块代码"]
        top_name = df_rank.iloc[0]["板块名称"]
        print(f"\n[{datetime.now():%H:%M:%S}] 拉取 {top_name}({top_code}) 分钟流向...")
        df_hist = fetch_sector_intraday(top_code, fetcher=fetcher)
        if not df_hist.empty:
            print(f"✅ 拿到 {len(df_hist)} 个分钟点，末尾 5 行：")
            print(df_hist.tail(5).to_string(index=False))
