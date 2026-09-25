import os
import io
import zipfile
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def get_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    return session


def get_bhavcopy(session, date):
    date_text = date.strftime("%d%m%Y")

    url = (
        "https://nsearchives.nseindia.com/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{date_text}_F_0000.csv.zip"
    )

    response = session.get(url, timeout=30)

    if response.status_code != 200:
        raise RuntimeError(
            f"Bhavcopy not available for {date}: HTTP {response.status_code}"
        )

    if not response.content[:2] == b"PK":
        raise RuntimeError(
            f"Bhavcopy for {date} is not a valid ZIP file"
        )

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        csv_files = [x for x in z.namelist() if x.lower().endswith(".csv")]

        if not csv_files:
            raise RuntimeError(f"No CSV found in Bhavcopy for {date}")

        with z.open(csv_files[0]) as f:
            df = pd.read_csv(f)

    return df


def get_fo_universe(session):
    url = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"

    response = session.get(url, timeout=30)

    if response.status_code != 200:
        raise RuntimeError(
            f"F&O universe unavailable: HTTP {response.status_code}"
        )

    df = pd.read_csv(io.BytesIO(response.content))

    symbol_col = None

    for col in df.columns:
        if str(col).strip().upper() in ["SYMBOL", "SYMBOLS"]:
            symbol_col = col
            break

    if symbol_col is None:
        raise RuntimeError(
            f"Could not find SYMBOL column in F&O universe. Columns: {list(df.columns)}"
        )

    symbols = (
        df[symbol_col]
        .astype(str)
        .str.strip()
        .str.upper()
        .dropna()
        .unique()
        .tolist()
    )

    return set(symbols)


def normalize_columns(df):
    df = df.copy()

    rename_map = {}

    for col in df.columns:
        c = str(col).strip().upper()

        if c in ["TCKR", "TCKR.SYMBOL", "SYMBOL", "SYMBOL_"]:
            rename_map[col] = "SYMBOL"
        elif c in ["H_PRIC", "HIGH", "HIGH_PRICE"]:
            rename_map[col] = "HIGH"
        elif c in ["L_PRIC", "LOW", "LOW_PRICE"]:
            rename_map[col] = "LOW"
        elif c in ["CLOSE_PRIC", "CLOSE", "CLOSE_PRICE"]:
            rename_map[col] = "CLOSE"

    df = df.rename(columns=rename_map)

    required = ["SYMBOL", "HIGH", "LOW", "CLOSE"]

    missing = [x for x in required if x not in df.columns]

    if missing:
        raise RuntimeError(
            f"Required columns missing: {missing}. Actual columns: {list(df.columns)}"
        )

    return df


def prepare_day(df):
    df = normalize_columns(df)

    df["SYMBOL"] = (
        df["SYMBOL"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    for col in ["HIGH", "LOW", "CLOSE"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["SYMBOL", "HIGH", "LOW", "CLOSE"])

    return df


def calculate_camarilla(df):
    df = df.copy()

    rng = df["HIGH"] - df["LOW"]

    df["H4"] = df["CLOSE"] + (rng * 1.1 / 2)
    df["L4"] = df["CLOSE"] - (rng * 1.1 / 2)

    return df


def send_telegram(message):
    url = (
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram failed: HTTP {response.status_code} - {response.text}"
        )


def main():
    session = get_session()

    today_date = datetime.now(IST).date()

    print(f"Checking today's Bhavcopy: {today_date}")

    # IMPORTANT:
    # We intentionally require today's Bhavcopy.
    # If NSE has not published it yet, this script fails.
    # The GitHub workflow will then retry after 10 minutes.
    today_df = get_bhavcopy(session, today_date)

    print("Today's Bhavcopy received successfully.")

    # Find previous trading day
    previous_date = today_date - timedelta(days=1)

    while True:
        try:
            print(f"Checking previous trading day: {previous_date}")
            previous_df = get_bhavcopy(session, previous_date)
            break
        except Exception as e:
            print(f"{previous_date} unavailable: {e}")
            previous_date -= timedelta(days=1)

    today_df = prepare_day(today_df)
    previous_df = prepare_day(previous_df)

    fo_symbols = get_fo_universe(session)

    print(f"F&O universe: {len(fo_symbols)} symbols")

    today_df = today_df[today_df["SYMBOL"].isin(fo_symbols)]
    previous_df = previous_df[previous_df["SYMBOL"].isin(fo_symbols)]

    today_df = calculate_camarilla(today_df)
    previous_df = calculate_camarilla(previous_df)

    previous_levels = previous_df[
        ["SYMBOL", "H4", "L4"]
    ].rename(
        columns={
            "H4": "PREV_H4",
            "L4": "PREV_L4",
        }
    )

    merged = today_df.merge(
        previous_levels,
        on="SYMBOL",
        how="inner"
    )

    inside = merged[
        (merged["H4"] <= merged["PREV_H4"]) &
        (merged["L4"] >= merged["PREV_L4"])
    ].copy()

    inside = inside.sort_values("SYMBOL")

    print(f"Inside Camarilla stocks: {len(inside)}")

    date_text = today_date.strftime("%d-%m-%Y")

    message = (
        "INSIDE CAMARILLA - RETRY\n"
        f"{date_text}\n\n"
    )

    if inside.empty:
        message += "No stocks found."
    else:
        message += f"Total: {len(inside)}\n\n"
        message += "\n".join(inside["SYMBOL"].tolist())

    send_telegram(message)

    print("Telegram message sent successfully.")


if __name__ == "__main__":
    main()
