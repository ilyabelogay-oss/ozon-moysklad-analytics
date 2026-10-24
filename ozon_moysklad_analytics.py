import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import time

st.set_page_config(page_title="Ozon + МойСклад — Прибыльность", layout="wide", page_icon="📊")

st.title("📊 Аналитика прибыльности: Ozon + МойСклад")
st.caption("Выручка и все расходы с Озона (включая баллы за скидки) + себестоимость из МоегоСклада. Матчинг: артикул Озона = код товара в МС.")

# Берём ключи из secrets, если есть, иначе из полей
def get_secret(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return default

with st.sidebar:
    st.header("🔑 Ключи")
    has_secrets = False
    try:
        _ = st.secrets["OZON_CLIENT_ID"]
        has_secrets = True
        st.success("Ключи загружены из Secrets")
    except Exception:
        st.info("Secrets не найдены — введи вручную")

    ozon_client_id = get_secret("OZON_CLIENT_ID") or st.text_input("Ozon Client-Id", type="password")
    ozon_api_key = get_secret("OZON_API_KEY") or st.text_input("Ozon Api-Key", type="password")
    ms_token = get_secret("MS_TOKEN") or st.text_input("МойСклад Bearer Token", type="password")

    st.header("📅 Период")
    col1, col2 = st.columns(2)
    with col1:
        date_from = st.date_input("С", datetime.now() - timedelta(days=30))
    with col2:
        date_to = st.date_input("По", datetime.now())

    run = st.button("🚀 Собрать аналитику", type="primary")

def ozon_headers(cid, key):
    return {"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"}

def ms_headers(token):
    return {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}

def ozon_post_with_retry(url, headers, payload, max_retries=5):
    for attempt in range(max_retries):
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code == 429:
            wait = 1.5 * (attempt + 1)
            time.sleep(wait)
            continue
        return r
    return r

@st.cache_data(ttl=300)
def fetch_ozon_analytics(cid, key, d_from, d_to):
    url = "https://api-seller.ozon.ru/v1/analytics/data"
    payload = {
        "date_from": d_from.strftime("%Y-%m-%d"),
        "date_to": d_to.strftime("%Y-%m-%d"),
        "metrics": ["revenue", "ordered_units"],
        "dimension": ["sku"],
        "limit": 1000,
        "offset": 0
    }
    try:
        r = ozon_post_with_retry(url, ozon_headers(cid, key), payload)
        if r.status_code != 200:
            st.error(f"Озон analytics ошибка {r.status_code}: {r.text}")
            return pd.DataFrame()
        data = r.json().get("result", {}).get("data", [])
        rows = []
        for item in data:
            dims = item.get("dimensions", [])
            mets = item.get("metrics", [])
            if dims:
                rows.append({
                    "sku": str(dims[0].get("id")),
                    "name": dims[0].get("name"),
                    "revenue": mets[0] if mets else 0,
                    "ordered_units": mets[1] if len(mets) > 1 else 0
                })
        return pd.DataFrame(rows)
    except Exception as e:
        st.error(f"Озон analytics упал: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=600)
def fetch_ozon_sku_to_offer(cid, key, skus):
    """Тянем offer_id по списку SKU"""
    if not skus:
        return pd.DataFrame()
    url = "https://api-seller.ozon.ru/v3/product/info/list"
    offer_map = {}
    for i in range(0, len(skus), 100):
        chunk = skus[i:i+100]
        payload = {"sku": [int(s) for s in chunk if str(s).isdigit()]}
        try:
            r = ozon_post_with_retry(url, ozon_headers(cid, key), payload)
            if r.status_code == 200:
                for item in r.json().get("items", []):
                    sku = str(item.get("sku") or item.get("sources", [{}])[0].get("sku", ""))
                    offer = item.get("offer_id")
                    if sku and offer:
                        offer_map[sku] = offer
        except Exception:
            pass
        time.sleep(0.5)
    return pd.DataFrame([{"sku": k, "offer_id": v} for k, v in offer_map.items()])

@st.cache_data(ttl=600)
def fetch_ozon_realization(cid, key, year, month):
    """Отчёт реализации — там баллы за скидки"""
    url = "https://api-seller.ozon.ru/v2/finance/realization"
    payload = {"month": month, "year": year}
    try:
        r = ozon_post_with_retry(url, ozon_headers(cid, key), payload)
        if r.status_code != 200:
            st.warning(f"Realization не отдался: {r.status_code} {r.text}")
            return {}
        return r.json().get("result", {})
    except Exception as e:
        st.warning(f"Realization не отдался: {e}")
        return {}

@st.cache_data(ttl=300)
def fetch_ozon_transactions_old(cid, key, d_from, d_to):
    """Старый метод (может быть уже мёртвый)"""
    url = "https://api-seller.ozon.ru/v3/finance/transaction/list"
    all_ops = []
    page = 1
    while page < 20:  # защита
        payload = {
            "filter": {
                "date": {
                    "from": d_from.strftime("%Y-%m-%dT00:00:00.000Z"),
                    "to": d_to.strftime("%Y-%m-%dT23:59:59.999Z")
                },
                "transaction_type": "all"
            },
            "page": page,
            "page_size": 1000
        }
        try:
            r = ozon_post_with_retry(url, ozon_headers(cid, key), payload)
            if r.status_code != 200:
                break
            ops = r.json().get("result", {}).get("operations", [])
            if not ops:
                break
            all_ops.extend(ops)
            if len(ops) < 1000:
                break
            page += 1
            time.sleep(0.5)
        except Exception:
            break
    return pd.DataFrame(all_ops) if all_ops else pd.DataFrame()

@st.cache_data(ttl=600)
def fetch_ms_costs(token):
    """Только себестоимость + код из МоегоСклада"""
    url = "https://api.moysklad.ru/api/remap/1.2/report/stock/all"
    try:
        r = requests.get(url, headers=ms_headers(token), timeout=60)
        r.raise_for_status()
        rows = r.json().get("rows", [])
        data = []
        for row in rows:
            code = row.get("code") or row.get("article")
            price = row.get("price")  # в копейках
            if code:
                data.append({
                    "ms_code": str(code).strip(),
                    "ms_name": row.get("name"),
                    "cost_price": (price or 0) / 100.0,
                    "stock": row.get("stock", 0)
                })
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"МойСклад: {e}")
        return pd.DataFrame()

if run:
    if not all([ozon_client_id, ozon_api_key, ms_token]):
        st.error("Ключи все вбей, иначе нехуй считать.")
    else:
        progress = st.progress(0)
        status = st.empty()

        status.info("Тянем продажи с Озона...")
        progress.progress(10)
        ozon_sales = fetch_ozon_analytics(ozon_client_id, ozon_api_key, date_from, date_to)

        if ozon_sales.empty:
            st.warning("Озон не вернул продаж. Смотри ошибку выше и проверь ключи/период.")
            st.stop()

        status.info("Достаём артикулы (offer_id)...")
        progress.progress(25)
        if not ozon_sales.empty:
            sku_offer = fetch_ozon_sku_to_offer(ozon_client_id, ozon_api_key, ozon_sales["sku"].unique().tolist())
            if not sku_offer.empty:
                ozon_sales = ozon_sales.merge(sku_offer, on="sku", how="left")
            else:
                ozon_sales["offer_id"] = None
        else:
            ozon_sales["offer_id"] = None

        status.info("Тянем расходы и баллы с Озона (realization + транзакции)...")
        progress.progress(45)
        # Realization за месяц (берём месяц date_to)
        real = fetch_ozon_realization(ozon_client_id, ozon_api_key, date_to.year, date_to.month)
        transactions = fetch_ozon_transactions_old(ozon_client_id, ozon_api_key, date_from, date_to)

        status.info("Себестоимость из МоегоСклада...")
        progress.progress(70)
        ms_costs = fetch_ms_costs(ms_token)

        status.info("Склеиваем и считаем...")
        progress.progress(90)

        # Сводка
        total_revenue = ozon_sales["revenue"].sum() if not ozon_sales.empty else 0
        total_units = ozon_sales["ordered_units"].sum() if not ozon_sales.empty else 0

        # Баллы из realization (может не работать)
        bonus_points = 0
        if isinstance(real, dict) and real:
            rows = real.get("rows") or real.get("items") or []
            for row in rows:
                try:
                    bonus_points += float(row.get("bonus") or row.get("points_for_discounts") or 0)
                except (TypeError, ValueError):
                    pass

        # Расходы из транзакций (amount отрицательные)
        total_expenses = 0
        if not transactions.empty and "amount" in transactions.columns:
            total_expenses = transactions["amount"].sum()  # обычно отрицательное

        # Матчинг и прибыль по товарам
        if not ozon_sales.empty and not ms_costs.empty:
            merged = ozon_sales.merge(
                ms_costs,
                left_on="offer_id",
                right_on="ms_code",
                how="left"
            )
            merged["cost_total"] = merged["cost_price"].fillna(0) * merged["ordered_units"].fillna(0)
            merged["gross_profit"] = merged["revenue"].fillna(0) - merged["cost_total"]
            # Безопасный расчёт маржи без краша на NaN
            revenue_safe = merged["revenue"].fillna(0)
            merged["margin_pct"] = (merged["gross_profit"] / revenue_safe.replace(0, pd.NA) * 100).round(1)
            merged["margin_pct"] = merged["margin_pct"].fillna(0)
        else:
            merged = ozon_sales.copy()
            merged["cost_price"] = 0
            merged["cost_total"] = 0
            merged["gross_profit"] = merged.get("revenue", 0)
            merged["margin_pct"] = None

        progress.progress(100)
        status.success("Готово. Смотри цифры, не обосрись.")

        # === Метрики ===
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Выручка Ozon", f"{total_revenue:,.0f} ₽")
        c2.metric("Заказано, шт", f"{total_units:,.0f}")
        c3.metric("Себестоимость (МС)", f"{merged['cost_total'].sum():,.0f} ₽")
        c4.metric("Баллы за скидки", f"{bonus_points:,.0f}")
        c5.metric("Грубая прибыль", f"{merged['gross_profit'].sum():,.0f} ₽")

        st.subheader("Товары (матчинг артикул Озона = код МС)")
        display_cols = ["offer_id", "name", "sku", "revenue", "ordered_units", "cost_price", "cost_total", "gross_profit", "margin_pct", "ms_name", "stock"]
        display_cols = [c for c in display_cols if c in merged.columns]
        st.dataframe(
            merged[display_cols].sort_values("revenue", ascending=False),
            use_container_width=True,
            height=500
        )

        if not transactions.empty:
            st.subheader("Финансовые операции Ozon (старый метод, может быть пусто)")
            st.dataframe(transactions.head(200), use_container_width=True)

        if real:
            with st.expander("Сырой realization (там баллы)"):
                st.json(real)

        st.download_button(
            "📥 Скачать CSV",
            merged.to_csv(index=False).encode("utf-8-sig"),
            "ozon_ms_profit.csv",
            "text/csv"
        )

st.markdown("---")
st.caption("""
**Что считается:**
- Выручка и штуки — analytics/data
- Артикул — offer_id из product/info/list
- Себестоимость — report/stock/all из МоегоСклада (price/100)
- Баллы за скидки — из realization (поле bonus)
- Расходы — из транзакций (amount), если метод ещё жив

Прибыль = выручка − себестоимость. Комиссии/логистика пока в общей сумме расходов, не раскиданы по SKU (это отдельный геморрой).
""")
