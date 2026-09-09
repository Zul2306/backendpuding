import os
import joblib
import numpy as np
import pandas as pd


def load_model_bundle(path_model):
    if not os.path.exists(path_model):
        raise FileNotFoundError(f"File model tidak ditemukan: {path_model}")

    bundle = joblib.load(path_model)

    if isinstance(bundle, dict):
        model = bundle["model"]
        fitur_model = bundle["fitur_model"]
        jenis_dataset = bundle.get("jenis_dataset", None)
    else:
        model = bundle
        fitur_model = None
        jenis_dataset = None

    return model, fitur_model, jenis_dataset


def tandai_periode(tanggal, daftar_periode):
    for mulai, selesai in daftar_periode:
        if pd.to_datetime(mulai) <= tanggal <= pd.to_datetime(selesai):
            return 1
    return 0


def hitung_zero_streak(series):
    hasil = []
    streak = 0

    for nilai in series:
        if nilai == 0:
            streak += 1
        else:
            streak = 0

        hasil.append(streak)

    return hasil


def hitung_days_since_last_supply(data_mitra):
    hasil = []
    tanggal_terakhir_suplai = None

    for _, row in data_mitra.iterrows():
        tanggal = row["tanggal"]
        suplai = row["suplai"]

        if suplai > 0:
            hasil.append(0)
            tanggal_terakhir_suplai = tanggal
        else:
            if tanggal_terakhir_suplai is None:
                hasil.append(999)
            else:
                hasil.append((tanggal - tanggal_terakhir_suplai).days)

    return hasil


def bentuk_fitur_prediksi(df, nama_mitra):
    df = df.copy()
    df = df.sort_values(["nama_mitra", "tanggal"]).reset_index(drop=True)

    df["tanggal"] = pd.to_datetime(df["tanggal"])

    # ========================================================
    # FITUR WAKTU
    # ========================================================

    df["tahun"] = df["tanggal"].dt.year
    df["bulan"] = df["tanggal"].dt.month
    df["hari"] = df["tanggal"].dt.day
    df["hari_dalam_minggu"] = df["tanggal"].dt.dayofweek
    df["hari_minggu"] = (df["hari_dalam_minggu"] == 6).astype(int)
    df["is_weekend"] = df["hari_dalam_minggu"].isin([5, 6]).astype(int)
    df["awal_bulan"] = (df["tanggal"].dt.day <= 7).astype(int)
    df["akhir_bulan"] = (df["tanggal"].dt.is_month_end).astype(int)
    df["indeks_waktu"] = df.groupby("nama_mitra").cumcount()

    # ========================================================
    # FITUR KONDISI KHUSUS
    # ========================================================

    periode_lebaran = [
        ("2025-03-01", "2025-04-09"),
        ("2026-02-19", "2026-03-31")
    ]

    periode_libur = [
        ("2024-12-21", "2025-01-07"),
        ("2025-06-21", "2025-08-18"),
        ("2025-12-20", "2026-02-04"),
        ("2026-02-19", "2026-03-31")
    ]

    mitra_akademik = {
        "feb",
        "fisip",
        "farmasi",
        "fkg",
        "fkep",
        "faperta",
        "ukafe",
        "akbid",
        "kop. alamanda",
        "kop alamanda",
        "kop. subandi",
        "kop subandi",
        "kop. subandi 2.",
        "kop. subandi 2",
        "kop subandi 2"
    }

    df["is_lebaran"] = df["tanggal"].apply(
        lambda x: tandai_periode(x, periode_lebaran)
    )

    df["is_libur"] = df["tanggal"].apply(
        lambda x: tandai_periode(x, periode_libur)
    )

    df["nama_mitra_lower"] = df["nama_mitra"].astype(str).str.lower().str.strip()

    df["is_mitra_akademik"] = df["nama_mitra_lower"].isin(mitra_akademik).astype(int)

    df["is_libur_mitra_akademik"] = (
        (df["is_libur"] == 1) &
        (df["is_mitra_akademik"] == 1)
    ).astype(int)

    # ========================================================
    # FITUR RASIO
    # ========================================================

    df["return_rate"] = np.where(
        df["suplai"] > 0,
        df["return"] / df["suplai"],
        0
    )

    df["sell_through"] = np.where(
        df["suplai"] > 0,
        df["terjual"] / df["suplai"],
        0
    )

    # ========================================================
    # FITUR LAG
    # ========================================================

    for lag in [1, 2, 3, 7, 14]:
        df[f"suplai_lag_{lag}"] = (
            df.groupby("nama_mitra")["suplai"].shift(lag)
        )

    df["return_lag_1"] = df.groupby("nama_mitra")["return"].shift(1)
    df["return_lag_7"] = df.groupby("nama_mitra")["return"].shift(7)

    df["terjual_lag_1"] = df.groupby("nama_mitra")["terjual"].shift(1)
    df["terjual_lag_7"] = df.groupby("nama_mitra")["terjual"].shift(7)

    # ========================================================
    # FITUR ROLLING AVERAGE
    # ========================================================

    df["suplai_avg_3"] = (
        df.groupby("nama_mitra")["suplai"]
        .transform(lambda x: x.shift(1).rolling(3, min_periods=1).mean())
    )

    df["suplai_avg_7"] = (
        df.groupby("nama_mitra")["suplai"]
        .transform(lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    )

    df["suplai_avg_14"] = (
        df.groupby("nama_mitra")["suplai"]
        .transform(lambda x: x.shift(1).rolling(14, min_periods=1).mean())
    )

    df["return_avg_7"] = (
        df.groupby("nama_mitra")["return"]
        .transform(lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    )

    df["terjual_avg_7"] = (
        df.groupby("nama_mitra")["terjual"]
        .transform(lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    )

    df["terjual_avg_14"] = (
        df.groupby("nama_mitra")["terjual"]
        .transform(lambda x: x.shift(1).rolling(14, min_periods=1).mean())
    )

    # ========================================================
    # STATISTIK HISTORIS
    # ========================================================

    df["suplai_std_7"] = (
        df.groupby("nama_mitra")["suplai"]
        .transform(lambda x: x.shift(1).rolling(7, min_periods=1).std())
    )

    df["suplai_min_7"] = (
        df.groupby("nama_mitra")["suplai"]
        .transform(lambda x: x.shift(1).rolling(7, min_periods=1).min())
    )

    df["suplai_max_7"] = (
        df.groupby("nama_mitra")["suplai"]
        .transform(lambda x: x.shift(1).rolling(7, min_periods=1).max())
    )

    df["suplai_max_14"] = (
        df.groupby("nama_mitra")["suplai"]
        .transform(lambda x: x.shift(1).rolling(14, min_periods=1).max())
    )

    # ========================================================
    # FITUR TREN DAN PERUBAHAN
    # ========================================================

    df["selisih_suplai_1"] = df["suplai"] - df["suplai_lag_1"]

    df["trend_7"] = df["suplai_avg_7"] - df["suplai_avg_14"]

    df["rasio_suplai_dari_avg_7"] = np.where(
        df["suplai_avg_7"] > 0,
        df["suplai"] / df["suplai_avg_7"],
        0
    )

    df["rasio_suplai_dari_avg_14"] = np.where(
        df["suplai_avg_14"] > 0,
        df["suplai"] / df["suplai_avg_14"],
        0
    )

    # ========================================================
    # FITUR RASIO HISTORIS
    # ========================================================

    df["return_rate_lag_1"] = (
        df.groupby("nama_mitra")["return_rate"].shift(1)
    )

    df["return_rate_avg_7"] = (
        df.groupby("nama_mitra")["return_rate"]
        .transform(lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    )

    df["sell_through_lag_1"] = (
        df.groupby("nama_mitra")["sell_through"].shift(1)
    )

    df["sell_through_avg_7"] = (
        df.groupby("nama_mitra")["sell_through"]
        .transform(lambda x: x.shift(1).rolling(7, min_periods=1).mean())
    )

    # ========================================================
    # FITUR POLA NOL DAN REAKTIVASI
    # ========================================================

    df["zero_lag_1"] = (
        df.groupby("nama_mitra")["suplai"]
        .shift(1)
        .fillna(0)
        .eq(0)
        .astype(int)
    )

    df["zero_rate_7"] = (
        df.groupby("nama_mitra")["suplai"]
        .transform(lambda x: x.shift(1).eq(0).rolling(7, min_periods=1).mean())
    )

    df["zero_rate_14"] = (
        df.groupby("nama_mitra")["suplai"]
        .transform(lambda x: x.shift(1).eq(0).rolling(14, min_periods=1).mean())
    )

    df["zero_streak"] = (
        df.groupby("nama_mitra")["suplai"]
        .transform(hitung_zero_streak)
    )

    hasil_days = []

    for _, data_mitra in df.groupby("nama_mitra"):
        hasil_days.extend(hitung_days_since_last_supply(data_mitra))

    df["days_since_last_supply"] = hasil_days

    df["reaktivasi_mitra"] = (
        (df["suplai"] > 0) &
        (df["zero_lag_1"] == 1)
    ).astype(int)

    df = df.drop(columns=["nama_mitra_lower"], errors="ignore")

    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.fillna(0)

    return df


def prediksi_suplai_besok(df_historis, path_model):
    model, fitur_model, jenis_dataset = load_model_bundle(path_model)

    df_historis = df_historis.copy()
    df_historis["tanggal"] = pd.to_datetime(df_historis["tanggal"])
    df_historis = df_historis.sort_values(["nama_mitra", "tanggal"]).reset_index(drop=True)

    df_fitur = bentuk_fitur_prediksi(df_historis, df_historis["nama_mitra"].iloc[0])

    if fitur_model is None:
        fitur_model = ["suplai", "return", "terjual"]

    for fitur in fitur_model:
        if fitur not in df_fitur.columns:
            df_fitur[fitur] = 0

    data_terakhir = df_fitur.iloc[[-1]].copy()

    X_pred = data_terakhir[fitur_model]
    X_pred = X_pred.replace([np.inf, -np.inf], np.nan).fillna(0)

    hasil_prediksi = float(model.predict(X_pred)[0])

    if hasil_prediksi < 0:
        hasil_prediksi = 0

    hasil_prediksi_bulat = int(round(hasil_prediksi))

    tanggal_terakhir = pd.to_datetime(data_terakhir["tanggal"].iloc[0]).date()
    tanggal_target = tanggal_terakhir + pd.Timedelta(days=1)

    return {
        "tanggal_terakhir": tanggal_terakhir,
        "tanggal_target": tanggal_target.date() if hasattr(tanggal_target, "date") else tanggal_target,
        "hasil_prediksi": hasil_prediksi,
        "hasil_prediksi_bulat": hasil_prediksi_bulat,
        "jumlah_fitur": len(fitur_model),
        "fitur_model": fitur_model
    }
