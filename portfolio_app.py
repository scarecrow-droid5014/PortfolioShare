import base64
import datetime
import hashlib
import json
import re
import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

st.set_page_config(
    page_title="스마트 자산배분 포트폴리오 앱",
    page_icon="⚖️",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# 모바일 친화적 스타일링
st.markdown(
    """
    <style>
    .block-container { padding-top: 1.5rem; padding-bottom: 3rem; max-width: 580px; }
    .summary-card { background-color: #f8f9fa; border-radius: 12px; padding: 16px; border: 1px solid #e9ecef; margin-bottom: 12px; }
    .vix-banner { background-color: #e8f5e9; color: #1b5e20; padding: 10px 14px; border-radius: 8px; font-weight: 600; font-size: 0.9rem; margin-bottom: 12px; }
    .user-badge { background-color: #eef2ff; color: #3730a3; padding: 8px 12px; border-radius: 8px; font-size: 0.85rem; font-weight: 600; margin-bottom: 10px; }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_github_config():
    token = st.secrets.get("GITHUB_TOKEN", "")
    repo = st.secrets.get("GITHUB_REPO", "scarecrow-droid5014/PortfolioShare")
    return token, repo


def sanitize_username(username: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9가-힣_-]", "", username.strip().lower())
    return cleaned[:30]


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.strip().encode("utf-8")).hexdigest()


def make_auto_token(username: str, pin_hash: str) -> str:
    raw = f"{username}:{pin_hash}:portfolio_auto_v1".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def load_user_from_github(username: str):
    token, repo = get_github_config()
    if not token:
        return None, None, "Streamlit Secrets에 GITHUB_TOKEN이 설정되지 않았습니다."

    safe_user = sanitize_username(username)
    if not safe_user:
        return None, None, "올바른 닉네임을 입력해 주세요."

    url = f"https://api.github.com/repos/{repo}/contents/portfolio_data/{safe_user}.json"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    res = requests.get(url, headers=headers, timeout=10)
    if res.status_code == 200:
        body = res.json()
        raw_content = base64.b64decode(body["content"]).decode("utf-8")
        return json.loads(raw_content), body["sha"], None
    elif res.status_code == 404:
        return None, None, None
    else:
        return None, None, f"GitHub 조회 오류 ({res.status_code}): {res.text}"


def save_user_to_github(username: str, pin_hash: str, state_data: dict, sha: str = None):
    token, repo = get_github_config()
    if not token:
        return None, "Streamlit Secrets에 GITHUB_TOKEN이 설정되지 않았습니다."

    safe_user = sanitize_username(username)
    url = f"https://api.github.com/repos/{repo}/contents/portfolio_data/{safe_user}.json"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    if not sha:
        check_res = requests.get(url, headers=headers, timeout=10)
        if check_res.status_code == 200:
            sha = check_res.json().get("sha")

    payload_obj = {
        "username": safe_user,
        "pin_hash": pin_hash,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": state_data,
    }
    encoded_content = base64.b64encode(
        json.dumps(payload_obj, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    body = {
        "message": f"Update portfolio data: {safe_user}",
        "content": encoded_content,
    }
    if sha:
        body["sha"] = sha

    put_res = requests.put(url, headers=headers, json=body, timeout=10)
    if put_res.status_code in (200, 201):
        new_sha = put_res.json().get("content", {}).get("sha")
        return new_sha, None
    else:
        return sha, f"GitHub 저장 실패 ({put_res.status_code}): {put_res.text}"


@st.cache_data(ttl=1800)
def fetch_market_indicators():
    vix_val = 15.0
    fx_val = 1380.0
    try:
        vix_df = yf.download("^VIX", period="5d", interval="1d", progress=False, auto_adjust=False)
        if isinstance(vix_df.columns, pd.MultiIndex):
            vix_df.columns = vix_df.columns.get_level_values(0)
        if not vix_df.empty:
            vix_val = round(float(vix_df["Close"].dropna().iloc[-1]), 2)
    except Exception:
        pass

    try:
        fx_df = yf.download("KRW=X", period="5d", interval="1d", progress=False, auto_adjust=False)
        if isinstance(fx_df.columns, pd.MultiIndex):
            fx_df.columns = fx_df.columns.get_level_values(0)
        if not fx_df.empty:
            fx_val = round(float(fx_df["Close"].dropna().iloc[-1]), 2)
    except Exception:
        pass

    return vix_val, fx_val


def fetch_krx_close(ticker, now):
    from pykrx import stock

    end = now.date()
    if now.hour < 16:
        end -= datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=14)

    # adjusted=True uses pykrx's public Naver Finance route.
    try:
        frame = stock.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker, adjusted=True
        )
        closes = pd.to_numeric(frame.get("종가", pd.Series(dtype=float)), errors="coerce")
        closes = closes[np.isfinite(closes) & (closes > 0)].sort_index()
        if not closes.empty:
            return int(closes.iloc[-1]), pd.Timestamp(closes.index[-1]).date()
    except Exception as exc:
        raise ValueError(f"{ticker}의 네이버 금융 시세 조회에 실패했습니다.") from exc

    raise ValueError(f"{ticker}의 최근 종가를 찾지 못했습니다.")


def calc_dynamic_cash_ratio(vix: float, max_cash_pct: float, use_vix_rule: bool) -> float:
    if not use_vix_rule:
        return float(max_cash_pct)
    raw_pct = 20.0 - (vix * 0.5)
    return float(np.clip(raw_pct, 0.0, max_cash_pct))


DEFAULT_ASSETS = [
    {"name": "미국채10년", "weight": 15.0},
    {"name": "국고채10년", "weight": 15.0},
    {"name": "타이거200", "weight": 20.0},
    {"name": "나스닥100", "weight": 15.0},
    {"name": "금", "weight": 15.0},
    {"name": "S&P500", "weight": 20.0},
]

DEFAULT_STATE = {
    "max_cash_pct": 15.0,
    "use_vix_rule": True,
    "rebalance_day": 20,
    "assets": DEFAULT_ASSETS,
    "latest_values": {a["name"]: 0 for a in DEFAULT_ASSETS},
    "latest_cash": 0,
    "total_principal": 0,
    "history": [],
}

st.title("⚖️ 스마트 자산배분 포트폴리오")

live_vix, live_fx = fetch_market_indicators()
st.caption(f"실시간 VIX: {live_vix:.2f} · 원/달러 환율: {live_fx:,.2f}원")

if "pf_user" not in st.session_state:
    st.session_state.pf_user = ""
if "pf_pin_hash" not in st.session_state:
    st.session_state.pf_pin_hash = ""
if "pf_sha" not in st.session_state:
    st.session_state.pf_sha = None
if "pf_state" not in st.session_state:
    st.session_state.pf_state = json.loads(json.dumps(DEFAULT_STATE))

qp_user = st.query_params.get("u", "")
qp_key = st.query_params.get("k", "")
if not st.session_state.pf_user and qp_user and qp_key:
    safe_u = sanitize_username(qp_user)
    if safe_u:
        existing_data, sha, err = load_user_from_github(safe_u)
        if existing_data and not err:
            stored_hash = existing_data.get("pin_hash", "")
            if make_auto_token(safe_u, stored_hash) == qp_key:
                merged = json.loads(json.dumps(DEFAULT_STATE))
                merged.update(existing_data.get("data", {}))
                st.session_state.pf_user = safe_u
                st.session_state.pf_pin_hash = stored_hash
                st.session_state.pf_sha = sha
                st.session_state.pf_state = merged

if not st.session_state.pf_user:
    with st.expander("🔑 내 포트폴리오 불러오기 (또는 새 사용자 등록)", expanded=True):
        st.caption("닉네임과 비밀번호(PIN)를 입력하면 클라우드에 내 비중 설정과 리밸런싱 히스토리가 영구 저장됩니다.")
        with st.form("pf_login_form"):
            lc1, lc2 = st.columns(2)
            in_user = lc1.text_input("닉네임 (영문/한글/숫자)", placeholder="예: lucas")
            in_pin = lc2.text_input("비밀번호 (PIN)", type="password", placeholder="4자리 이상")
            remember_me = st.checkbox("📱 이 기기에서 자동 로그인 유지", value=True)
            login_btn = st.form_submit_button("📂 불러오기 / 새 포트폴리오 시작", use_container_width=True)

            if login_btn:
                safe_u = sanitize_username(in_user)
                if not safe_u or len(in_pin.strip()) < 4:
                    st.warning("닉네임과 4자리 이상의 비밀번호(PIN)를 입력해 주세요.")
                else:
                    p_hash = hash_pin(in_pin)
                    existing_data, sha, err = load_user_from_github(safe_u)
                    if err:
                        st.error(err)
                    elif existing_data is not None:
                        if existing_data.get("pin_hash") != p_hash:
                            st.error("비밀번호(PIN)가 일치하지 않습니다.")
                        else:
                            merged = json.loads(json.dumps(DEFAULT_STATE))
                            merged.update(existing_data.get("data", {}))
                            st.session_state.pf_user = safe_u
                            st.session_state.pf_pin_hash = p_hash
                            st.session_state.pf_sha = sha
                            st.session_state.pf_state = merged
                            if remember_me:
                                st.query_params["u"] = safe_u
                                st.query_params["k"] = make_auto_token(safe_u, p_hash)
                            st.rerun()
                    else:
                        init_data = json.loads(json.dumps(DEFAULT_STATE))
                        new_sha, save_err = save_user_to_github(safe_u, p_hash, init_data, None)
                        if save_err:
                            st.error(save_err)
                        else:
                            st.session_state.pf_user = safe_u
                            st.session_state.pf_pin_hash = p_hash
                            st.session_state.pf_sha = new_sha
                            st.session_state.pf_state = init_data
                            if remember_me:
                                st.query_params["u"] = safe_u
                                st.query_params["k"] = make_auto_token(safe_u, p_hash)
                            st.rerun()
else:
    uc1, uc2 = st.columns([3, 1])
    uc1.markdown(
        f'<div class="user-badge">👤 접속 계정: <b>{st.session_state.pf_user}</b> (클라우드 자동 동기화 중)</div>',
        unsafe_allow_html=True,
    )
    if uc2.button("로그아웃", use_container_width=True):
        st.session_state.pop("pf_valuation", None)
        st.session_state.pf_user = ""
        st.session_state.pf_pin_hash = ""
        st.session_state.pf_sha = None
        st.session_state.pf_state = json.loads(json.dumps(DEFAULT_STATE))
        st.query_params.clear()
        st.rerun()

state = st.session_state.pf_state
assets = state.get("assets", DEFAULT_ASSETS)
max_cash_pct = float(state.get("max_cash_pct", 15.0))
use_vix_rule = bool(state.get("use_vix_rule", True))
rebalance_day = int(state.get("rebalance_day", 20))

tab_reb, tab_cfg, tab_hist = st.tabs(["⚖️ 리밸런싱 계산기", "⚙️ 포트폴리오 비중 설정", "📈 수익률 및 히스토리"])

with tab_reb:
    st.caption(f"정기 리밸런싱 기준일: 매달 {rebalance_day}일 · VIX 연동 가변 현금비중 적용")

    vc1, vc2, vc3 = st.columns(3)
    input_vix = vc1.number_input("적용 VIX 지수", min_value=5.0, max_value=90.0, value=float(live_vix), step=0.1)
    input_fx = vc2.number_input("적용 환율 (원/$)", min_value=900.0, max_value=2000.0, value=float(live_fx), step=1.0)
    hedge_pct = vc3.number_input("환헷지 비율 (%)", min_value=0, max_value=100, value=100, step=10)

    target_cash_pct = calc_dynamic_cash_ratio(input_vix, max_cash_pct, use_vix_rule)
    st.markdown(
        f"""
        <div class="vix-banner">
            📊 VIX <b>{input_vix:.2f}</b> 기준 → 목표 현금비중: <b>{target_cash_pct:.2f}%</b> (주식·자산 배분 비중: <b>{100.0 - target_cash_pct:.2f}%</b>)
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("1️⃣ 현재 계좌 잔고 입력", expanded=False):
        saved_cash = int(state.get("latest_cash", 0))
        saved_principal = int(state.get("total_principal", 0))

        c_top1, c_top2, c_top3 = st.columns(3)
        cur_cash = c_top1.number_input("현재 예수금 (원)", min_value=0, value=saved_cash, step=10000)
        add_deposit = c_top2.number_input("이번 달 추가 투입금 (원)", min_value=0, value=0, step=50000)
        base_principal = c_top3.number_input("직전 누적 원금 (원)", min_value=0, value=saved_principal, step=100000)

    portfolio_key = (
        st.session_state.pf_user,
        tuple((a["name"], a.get("ticker", ""), a.get("quantity", 0)) for a in assets),
    )
    missing_tickers = [a["name"] for a in assets if not a.get("ticker")]
    if missing_tickers:
        st.info("포트폴리오 비중 설정에서 종목코드와 보유 수량을 저장해 주세요. "
                f"미설정 종목: {', '.join(missing_tickers)}")

    if st.button("🔄 현재 평가금 갱신", use_container_width=True, disabled=bool(missing_tickers) or not assets):
        st.session_state.pop("pf_valuation", None)
        now = pd.Timestamp.now(tz="Asia/Seoul")
        quotes = {}
        failed = []
        with st.spinner("pykrx로 네이버 금융 종가를 조회하는 중입니다."):
            for ticker in dict.fromkeys(a["ticker"] for a in assets if a["quantity"] > 0):
                try:
                    quotes[ticker] = fetch_krx_close(ticker, now)
                except (ImportError, ValueError) as exc:
                    failed.append(str(exc) if isinstance(exc, ValueError) else "pykrx 설치가 필요합니다.")
                    if isinstance(exc, ImportError):
                        break
        if failed:
            st.error(" ".join(failed) + " 종목코드를 확인하거나 다시 갱신해 주세요.")
        else:
            st.session_state.pf_valuation = (portfolio_key, quotes)

    st.divider()
    st.subheader("2️⃣ 리밸런싱 주문 가이드")
    valuation = st.session_state.get("pf_valuation")
    if not valuation or valuation[0] != portfolio_key:
        st.info("현재 평가금 갱신 버튼을 누르면 리밸런싱 주문 가이드가 표시됩니다.")
    else:
        quotes = valuation[1]
        current_vals = {
            a["name"]: quotes[a["ticker"]][0] * a["quantity"] if a["quantity"] > 0 else 0
            for a in assets
        }
        if quotes:
            close_dates = sorted({date.isoformat() for _, date in quotes.values()})
            st.caption(f"네이버 금융 수정종가 기준 거래일 · {', '.join(close_dates)}")

        eval_sum = sum(current_vals.values())
        total_asset = eval_sum + cur_cash + add_deposit
        new_principal = base_principal + add_deposit
        cum_profit = total_asset - new_principal
        total_ret_pct = (cum_profit / new_principal * 100.0) if new_principal > 0 else 0.0

        target_cash_amt = round(total_asset * (target_cash_pct / 100.0))
        investable_amt = total_asset - target_cash_amt
        cash_diff = target_cash_amt - (cur_cash + add_deposit)

        # 상단 지표 카드
        m1, m2, m3 = st.columns(3)
        with m1:
            st.markdown(
                f"""
                <div class="summary-card" style="text-align:center; padding:12px;">
                    <div style="font-size:0.8rem; color:#6c757d; font-weight:600;">총 자산 (투입 포함)</div>
                    <div style="font-size:1.05rem; font-weight:800; color:#111; margin:4px 0; word-break:break-all;">{total_asset:,.0f}원</div>
                    <div style="font-size:0.75rem; color:#0d6efd;">원금 {new_principal:,.0f}원</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with m2:
            st.markdown(
                f"""
                <div class="summary-card" style="text-align:center; padding:12px;">
                    <div style="font-size:0.8rem; color:#6c757d; font-weight:600;">누적 수익금</div>
                    <div style="font-size:1.05rem; font-weight:800; color:{'#198754' if cum_profit >= 0 else '#dc3545'}; margin:4px 0; word-break:break-all;">{cum_profit:+,.0f}원</div>
                    <div style="font-size:0.75rem; color:#6c757d;">{total_ret_pct:+.2f}%</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with m3:
            st.markdown(
                f"""
                <div class="summary-card" style="text-align:center; padding:12px;">
                    <div style="font-size:0.8rem; color:#6c757d; font-weight:600;">목표 예수금</div>
                    <div style="font-size:1.05rem; font-weight:800; color:#111; margin:4px 0; word-break:break-all;">{target_cash_amt:,.0f}원</div>
                    <div style="font-size:0.75rem; color:#d63384;">{cash_diff:+,.0f}원 필요</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        total_weight = sum(float(x["weight"]) for x in assets)
        reb_rows = []
        for item in assets:
            a_name = item["name"]
            w_pct = float(item["weight"])
            norm_ratio = (w_pct / total_weight) if total_weight > 0 else 0.0
            cur_v = current_vals[a_name]
            tgt_v = round(investable_amt * norm_ratio)
            diff_v = tgt_v - cur_v
            cur_share_pct = (cur_v / investable_amt * 100.0) if investable_amt > 0 else 0.0

            if diff_v > 0:
                action_str = f"🟢 +{diff_v:,.0f}원 매수"
            elif diff_v < 0:
                action_str = f"🔴 {diff_v:,.0f}원 매도"
            else:
                action_str = "➖ 유지"

            reb_rows.append(
                {
                    "종목명": a_name,
                    "종목코드": item["ticker"],
                    "보유 수량": item["quantity"],
                    "종가": f"{quotes[item['ticker']][0]:,}원" if item["ticker"] in quotes else "—",
                    "목표비중": f"{w_pct:.1f}%",
                    "현재비중": f"{cur_share_pct:.1f}%",
                    "현재평가금": f"{cur_v:,.0f}원",
                    "목표평가금": f"{tgt_v:,.0f}원",
                    "매매가이드": action_str,
                }
            )

        reb_rows.insert(
            0,
            {
                "종목명": "현금(예수금)",
                "목표비중": f"{target_cash_pct:.2f}%",
                "현재비중": f"{((cur_cash + add_deposit) / total_asset * 100.0) if total_asset > 0 else 0.0:.2f}%",
                "현재평가금": f"{cur_cash + add_deposit:,.0f}원",
                "목표평가금": f"{target_cash_amt:,.0f}원",
                "매매가이드": f"{cash_diff:+,.0f}원",
            },
        )

        # 원상복구된 깔끔한 기본 데이터프레임 출력
        df_reb = pd.DataFrame(reb_rows)
        st.dataframe(df_reb, use_container_width=True, hide_index=True)

        rec_date = st.date_input("기록 날짜", value=datetime.date.today())
        if st.button("💾 오늘 리밸런싱 내역 저장하기", use_container_width=True):
            record_entry = {
                "날짜": rec_date.strftime("%Y-%m-%d"),
                "총자산": int(total_asset),
                "원금": int(new_principal),
                "총수익률(%)": round(total_ret_pct, 2),
                "누적수익금": int(cum_profit),
                "추가투입": int(add_deposit),
                "예수금": int(cur_cash + add_deposit),
                "목표예수금": int(target_cash_amt),
                "평가금": int(eval_sum),
                "VIX": float(input_vix),
                "현금비중(%)": round(target_cash_pct, 2),
                "환율": float(input_fx),
                "환헷지(%)": int(hedge_pct),
                "종목별평가금": current_vals,
            }

            history = state.get("history", [])
            date_key = record_entry["날짜"]
            history = [h for h in history if h.get("날짜") != date_key]
            history.append(record_entry)
            history = sorted(history, key=lambda x: x["날짜"])

            state["latest_values"] = current_vals
            state["latest_cash"] = int(cur_cash + add_deposit)
            state["total_principal"] = int(new_principal)
            state["history"] = history
            st.session_state.pf_state = state

            if st.session_state.pf_user:
                new_sha, err = save_user_to_github(
                    st.session_state.pf_user,
                    st.session_state.pf_pin_hash,
                    state,
                    st.session_state.pf_sha,
                )
                if err:
                    st.error(err)
                else:
                    st.session_state.pf_sha = new_sha
                    st.success(f"{date_key} 포트폴리오 기록이 클라우드에 저장되었습니다!")
                    st.rerun()
            else:
                st.warning("로그인하지 않아 현재 브라우저 세션에만 임시 반영되었습니다.")

with tab_cfg:
    st.subheader("⚙️ 현금 규칙 및 보유 종목 편집")
    with st.form("pf_config_form"):
        cc1, cc2, cc3 = st.columns(3)
        new_max_cash = cc1.number_input("최대 현금비중 (%)", min_value=0.0, max_value=50.0, value=max_cash_pct, step=1.0)
        new_use_vix = cc2.selectbox("VIX 연동 가변 현금", options=[True, False], index=0 if use_vix_rule else 1, format_func=lambda x: "사용 (20-0.5×VIX)" if x else "고정 비중")
        new_reb_day = cc3.number_input("매월 리밸런싱 일자", min_value=1, max_value=31, value=rebalance_day, step=1)

        edited_assets = []
        for i in range(10):
            asset = assets[i] if i < len(assets) else {}
            account = st.session_state.pf_user
            ac1, ac2 = st.columns([2, 1])
            n_val = ac1.text_input(f"종목 {i + 1}", value=asset.get("name", ""),
                                   key=f"cfg_name_{account}_{i}", placeholder="종목명 입력")
            w_val = ac2.number_input(f"비중 (%) #{i + 1}", min_value=0.0, max_value=100.0,
                                     value=float(asset.get("weight", 0)), step=1.0, key=f"cfg_w_{account}_{i}")
            ac3, ac4 = st.columns([2, 1])
            ticker = ac3.text_input(f"종목코드 #{i + 1}", value=asset.get("ticker", ""),
                                    key=f"cfg_ticker_{account}_{i}", placeholder="예: 005930", max_chars=6)
            quantity = ac4.number_input(f"보유 수량 (주) #{i + 1}", min_value=0,
                                        value=int(asset.get("quantity", 0)), step=1,
                                        key=f"cfg_quantity_{account}_{i}")
            if n_val.strip() or w_val or ticker.strip() or quantity:
                edited_assets.append({
                    "name": n_val.strip(),
                    "weight": float(w_val),
                    "ticker": ticker.strip().upper(),
                    "quantity": int(quantity),
                })

        weight_sum = sum(x["weight"] for x in edited_assets)
        if abs(weight_sum - 100.0) < 0.01:
            st.success(f"현재 종목 비중 합계: **{weight_sum:.1f}%** (정상)")
        else:
            st.warning(f"현재 종목 비중 합계: **{weight_sum:.1f}%** (100%로 맞추는 것을 권장합니다)")

        save_pf_cfg = st.form_submit_button("💾 포트폴리오 구성 저장하기", use_container_width=True)
        if save_pf_cfg:
            names = [a["name"] for a in edited_assets]
            if not edited_assets or not weight_sum:
                st.error("최소 1개 종목에 목표 비중을 입력해 주세요.")
            elif not all(names) or len(set(names)) != len(names):
                st.error("입력한 각 행에 서로 다른 종목명을 입력해 주세요.")
            elif any(a["ticker"] and not re.fullmatch(r"[0-9A-Z]{6}", a["ticker"]) for a in edited_assets):
                st.error("종목코드는 6자리로 입력해 주세요. 예: 005930")
            elif any(a["quantity"] > 0 and not a["ticker"] for a in edited_assets):
                st.error("보유 수량이 있는 종목에는 종목코드를 입력해 주세요.")
            else:
                updated_state = {
                    **state,
                    "max_cash_pct": float(new_max_cash),
                    "use_vix_rule": bool(new_use_vix),
                    "rebalance_day": int(new_reb_day),
                    "assets": edited_assets,
                }
                err = None
                if st.session_state.pf_user:
                    new_sha, err = save_user_to_github(
                        st.session_state.pf_user,
                        st.session_state.pf_pin_hash,
                        updated_state,
                        st.session_state.pf_sha,
                    )
                    if not err:
                        st.session_state.pf_sha = new_sha
                if err:
                    st.error(err)
                else:
                    st.session_state.pf_state = updated_state
                    st.session_state.pop("pf_valuation", None)
                    st.rerun()

with tab_hist:
    history = state.get("history", [])
    if not history:
        st.info("아직 저장된 리밸런싱 기록이 없습니다.")
    else:
        hist_df = pd.DataFrame(history)
        latest_h = hist_df.iloc[-1]

        hm1, hm2 = st.columns(2)
        hm1.metric("최신 총 자산", f"{int(latest_h['총자산']):,}원", f"{latest_h['총수익률(%)']:+.2f}%")
        hm2.metric("누적 수익금", f"{int(latest_h['누적수익금']):+,}원", f"원금 {int(latest_h['원금']):,}원")

        st.line_chart(hist_df.set_index("날짜")[["총자산", "원금"]])

        display_cols = ["날짜", "총자산", "원금", "총수익률(%)", "누적수익금", "추가투입", "예수금", "목표예수금", "VIX", "현금비중(%)", "환율"]
        avail_cols = [c for c in display_cols if c in hist_df.columns]
        st.dataframe(hist_df[avail_cols].iloc[::-1], use_container_width=True, hide_index=True)

        if st.button("🗑️ 마지막 기록 1건 삭제", use_container_width=True):
            state["history"] = history[:-1]
            st.session_state.pf_state = state
            if st.session_state.pf_user:
                new_sha, _ = save_user_to_github(
                    st.session_state.pf_user,
                    st.session_state.pf_pin_hash,
                    state,
                    st.session_state.pf_sha,
                )
                st.session_state.pf_sha = new_sha
            st.rerun()
