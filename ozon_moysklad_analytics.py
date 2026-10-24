import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
from collections import defaultdict

st.set_page_config(page_title="Ozon + МойСклад — Юнит-экономика", layout="wide", page_icon="📊")

st.title("📊 Юнит-экономика: Ozon + МойСклад")
st.caption("Начисления с Озона + себестоимость из МС.")

def get_secret(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return default

with st.sidebar:
    st.header("🔑 Ключи")
    try:
        _ = st.secrets["OZON_CLIENT_ID"]
        st.success("Ключи из Secrets")
    except Exception:
        st.info("Введи ключи вручную")

    ozon_client_id = get_secret("OZON_CLIENT_ID") or st.text_input("Ozon Client-Id", type="password")
    ozon_api_key = get_secret("OZON_API_KEY") or st.text_input("Ozon Api-Key", type="password")
    ms_token = get_secret("MS_TOKEN") or st.text_input("МойСклад Bearer Token", type="password")

    st.header("📅 Период")
    col1, col2 = st.columns(2)
    with col1:
        date_from = st.date_input("С", datetime.now() - timedelta(days=7))
    with col2:
        date_to = st.date_input("По", datetime.now())

    run = st.button("🚀 Собрать аналитику", type="primary", use_container_width=True)

def ozon_headers(cid, key):
    return {"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"}

def ms_headers(token):
    return {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}

def ozon_post_with_retry(url, headers, payload, max_retries=6):
    for attempt in range(max_retries):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=90)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return r
        except Exception:
            time.sleep(1)
    return r

@st.cache_data(ttl=600)
def fetch_accrual_types(cid, key):
    url = "https://api-seller.ozon.ru/v1/finance/accrual/types"
    r = ozon_post_with_retry(url, ozon_headers(cid, key), {})
    if r.status_code != 200:
        return {}
    types = {}
    for t in r.json().get("accrual_types", []):
        types[t.get("id")] = (t.get("name") or t.get("description") or "").lower()
    return types

@st.cache_data(ttl=300)
def fetch_accruals_by_period(cid, key, d_from, d_to):
    url = "https://api-seller.ozon.ru/v1/finance/accrual/by-day"
    all_accruals = []
    current = d_from
    while current <= d_to:
        last_id = ""
        while True:
            payload = {"date": current.strftime("%Y-%m-%d"), "last_id": last_id}
            r = ozon_post_with_retry(url, ozon_headers(cid, key), payload)
            if r.status_code != 200:
                break
            data = r.json()
            accruals = data.get("accruals", [])
            all_accruals.extend(accruals)
            last_id = data.get("last_id") or ""
            if not last_id or not accruals:
                break
            time.sleep(0.3)
        current += timedelta(days=1)
        time.sleep(0.2)
    return all_accruals

def categorize_type(type_id, type_map):
    name = type_map.get(type_id, "")
    if any(x in name for x in ["логист", "delivery", "магистр", "последн"]):
        return "logistics"
    if any(x in name for x in ["эквайр", "acquiring"]):
        return "acquiring"
    if any(x in name for x in ["возврат", "return", "обратн"]):
        return "returns"
    if any(x in name for x in ["реклам", "promo", "клик", "заказ", "звёзд", "бренд"]):
        return "ads"
    return "other_expenses"

def aggregate_by_sku(accruals, type_map):
    sku_data = defaultdict(lambda: defaultdict(float))
    for acc in accruals:
        posting = acc.get("posting") or {}
        for prod in posting.get("products") or []:
            sku = str(prod.get("sku") or "")
            if not sku or sku == "0":
                continue

            qty = float(prod.get("quantity") or 1)
            sku_data[sku]["qty"] += qty

            comm = prod.get("commission")
            if isinstance(comm, dict):
                sale_amt = comm.get("sale_amount") or {}
                if isinstance(sale_amt, dict):
                    sku_data[sku]["revenue"] += float(sale_amt.get("amount") or 0)
                bonus = comm.get("bonus") or {}
                if isinstance(bonus, dict):
                    sku_data[sku]["discount_points"] += float(bonus.get("amount") or 0)
                sale_comm = comm.get("sale_commission") or comm.get("commission") or {}
                if isinstance(sale_comm, dict):
                    sku_data[sku]["ozon_reward"] += float(sale_comm.get("amount") or 0)
                coinv = comm.get("coinvestment") or {}
                if isinstance(coinv, dict):
                    sku_data[sku]["partner_programs"] += float(coinv.get("amount") or 0)

            delivery = prod.get("delivery") or {}
            for svc in delivery.get("services") or []:
                type_id = svc.get("type_id")
                amount = float((svc.get("accrued") or {}).get("amount") or 0)
                cat = categorize_type(type_id, type_map)
                sku_data[sku][cat] += amount

        item_fees = acc.get("item_fees") or {}
        for fee_group in item_fees.get("fees") or []:
            sku = str(fee_group.get("sku") or "")
            if not sku:
                continue
            for f in fee_group.get("fees") or []:
                type_id = f.get("type_id")
                amount = float((f.get("accrued") or {}).get("amount") or 0)
                cat = categorize_type(type_id, type_map)
                sku_data[sku][cat] += amount

    rows = [{"sku": sku, **dict(data)} for sku, data in sku_data.items()]
    return pd.DataFrame(rows)

@st.cache_data(ttl=600)
def fetch_sku_to_offer(cid, key, skus):
    if not skus:
        return pd.DataFrame()
    url = "https://api-seller.ozon.ru/v3/product/info/list"
    offer_map = {}
    for i in range(0, len(skus), 100):
        chunk = [s for s in skus[i:i+100] if str(s).isdigit()]
        if not chunk:
            continue
        payload = {"sku": [int(s) for s in chunk]}
        r = ozon_post_with_retry(url, ozon_headers(cid, key), payload)
        if r.status_code == 200:
            for item in r.json().get("items", []):
                sku = str(item.get("sku") or "")
                offer = item.get("offer_id")
                name = item.get("name")
                if sku and offer:
                    offer_map[sku] = {"offer_id": offer, "name": name}
        time.sleep(0.4)
    return pd.DataFrame([{"sku": k, **v} for k, v in offer_map.items()])

@st.cache_data(ttl=600)
def fetch_ms_costs(token):
    url = "https://api.moysklad.ru/api/remap/1.2/report/stock/all"
    try:
        r = requests.get(url, headers=ms_headers(token), timeout=60)
        r.raise_for_status()
        rows = r.json().get("rows", [])
        data = []
        for row in rows:
            code = row.get("code") or row.get("article")
            if code:
                data.append({
                    "ms_code": str(code).strip(),
                    "ms_name": row.get("name"),
                    "cost_price": (row.get("price") or 0) / 100.0,
                    "stock": row.get("stock", 0)
                })
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"МойСклад: {e}")
        return pd.DataFrame()

if run:
    if not all([ozon_client_id, ozon_api_key, ms_token]):
        st.error("Введи все ключи")
        st.stop()

    progress = st.progress(0)
    status = st.empty()

    status.info("Справочник типов...")
    progress.progress(10)
    type_map = fetch_accrual_types(ozon_client_id, ozon_api_key)

    status.info("Начисления Ozon...")
    progress.progress(40)
    accruals = fetch_accruals_by_period(ozon_client_id, ozon_api_key, date_from, date_to)

    status.info("Агрегация по SKU...")
    progress.progress(60)
    sku_df = aggregate_by_sku(accruals, type_map)

    if sku_df.empty:
        st.warning("Не удалось собрать данные")
        st.stop()

    status.info("Артикулы...")
    progress.progress(75)
    offers = fetch_sku_to_offer(ozon_client_id, ozon_api_key, sku_df["sku"].tolist())
    if not offers.empty:
        sku_df = sku_df.merge(offers, on="sku", how="left")

    status.info("Себестоимость МС...")
    progress.progress(85)
    ms = fetch_ms_costs(ms_token)

    if not ms.empty and "offer_id" in sku_df.columns:
        merged = sku_df.merge(ms, left_on="offer_id", right_on="ms_code", how="left")
    else:
        merged = sku_df.copy()
        merged["cost_price"] = 0

    for col in ["revenue", "discount_points", "ozon_reward", "partner_programs", "logistics", "acquiring", "returns", "ads", "other_expenses", "cost_price", "qty"]:
        if col not in merged.columns:
            merged[col] = 0.0
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0)

    # Все суммы с знаками как в Озоне
    merged["sales_total"] = merged["revenue"] + merged["discount_points"] + merged["partner_programs"]
    merged["accruals_total"] = (
        merged["ozon_reward"] + merged["logistics"] + merged["acquiring"] +
        merged["returns"] + merged["ads"] + merged["other_expenses"]
    )
    merged["ozon_net"] = merged["sales_total"] + merged["accruals_total"]
    merged["cost_total"] = merged["cost_price"] * merged["qty"]
    merged["profit"] = merged["ozon_net"] - merged["cost_total"]
    merged["margin_pct"] = (merged["profit"] / merged["revenue"].replace(0, pd.NA) * 100).fillna(0).round(1)

    progress.progress(100)
    status.success("Готово")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Выручка", f"{merged['revenue'].sum():,.0f} ₽")
    c2.metric("Начисления", f"{merged['accruals_total'].sum():,.0f} ₽")
    c3.metric("Себестоимость", f"{merged['cost_total'].sum():,.0f} ₽")
    c4.metric("Прибыль", f"{merged['profit'].sum():,.0f} ₽")

    st.subheader("Позиции")
    display_cols = ["sku", "offer_id", "name", "qty", "revenue", "discount_points", "partner_programs", "ozon_reward", "logistics", "acquiring", "returns", "ads", "other_expenses", "cost_total", "profit", "margin_pct"]
    display_cols = [c for c in display_cols if c in merged.columns]

    col_names = {
        "sku": "SKU",
        "offer_id": "Артикул",
        "name": "Название",
        "qty": "Шт",
        "revenue": "Выручка",
        "discount_points": "Баллы",
        "partner_programs": "Партнёры",
        "ozon_reward": "Вознаграждение",
        "logistics": "Логистика",
        "acquiring": "Эквайринг",
        "returns": "Возвраты",
        "ads": "Реклама",
        "other_expenses": "Прочие",
        "cost_total": "Себестоимость",
        "profit": "Прибыль",
        "margin_pct": "Маржа %"
    }
    display_df = merged[display_cols].rename(columns=col_names).sort_values("Выручка", ascending=False)

    st.dataframe(display_df, use_container_width=True, height=600)

    st.download_button("📥 CSV", display_df.to_csv(index=False).encode("utf-8-sig"), "unit_economics.csv", "text/csv")

st.markdown("---")
st.caption("Прибыль = (выручка + баллы + партнёры) + все начисления (с знаками) − себестоимость × шт.")
