"""Evaluasi sementara, read-only; hapus include_router untuk melepas fitur."""
from datetime import date, timedelta
import hashlib
import math
import os
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DataHarian, Mitra, ModelMitra
from app.prediction_service import bentuk_fitur_prediksi

router = APIRouter(prefix="/evaluasi-sementara", tags=["Evaluasi sementara"])


def require_enabled():
    if os.getenv("ENABLE_TEMP_EVALUATION", "false").lower() not in ("true", "1"):
        raise HTTPException(404, "Evaluasi sementara tidak diaktifkan pada server.")


class EvaluationRequest(BaseModel):
    tanggal_mulai: date
    tanggal_selesai: date
    tanggal_akhir_data_latih: date
    id_mitra: int | None = Field(default=None, gt=0)
    target_suplai_adalah_terjual: bool = False


def metrics(rows):
    if not rows:
        return {"jumlah": 0, "mae": None, "rmse": None, "bias": None,
                "mae_baseline": None}
    errors = [row["prediksi"] - row["aktual"] for row in rows]
    return {"jumlah": len(rows),
            "mae": sum(abs(x) for x in errors) / len(errors),
            "rmse": math.sqrt(sum(x*x for x in errors) / len(errors)),
            "bias": sum(errors) / len(errors),
            "mae_baseline": sum(abs(row["baseline"] - row["aktual"])
                                for row in rows) / len(rows)}


def valid_daily(row):
    return (row.jumlah_suplai is not None and row.jumlah_return is not None
            and 0 <= row.jumlah_return <= row.jumlah_suplai)


@router.get("/status", dependencies=[Depends(require_enabled)])
def evaluation_status():
    return {"enabled": True, "contract_version": 1}


@router.post("/jalankan", dependencies=[Depends(require_enabled)])
def run_evaluation(req: EvaluationRequest, db: Session = Depends(get_db)):
    # Also guard direct calls; the endpoint never calls add/flush/commit.
    require_enabled()
    if not 0 <= (req.tanggal_selesai - req.tanggal_mulai).days < 31:
        raise HTTPException(422, "Pilih rentang berurutan maksimal 31 hari.")
    if req.tanggal_selesai >= date.today():
        raise HTTPException(422, "Pilih tanggal sebelum hari ini agar laporan harian sudah selesai.")
    if req.tanggal_akhir_data_latih >= req.tanggal_mulai:
        raise HTTPException(422, "Tanggal terakhir data latih harus sebelum periode evaluasi.")
    partners_query = db.query(Mitra).order_by(Mitra.nama_mitra)
    if req.id_mitra is not None:
        partners_query = partners_query.filter(Mitra.id_mitra == req.id_mitra)
    partners = partners_query.all()
    if not partners:
        raise HTTPException(404, "Mitra tidak ditemukan.")
    ids = [p.id_mitra for p in partners]
    histories = {id_: [] for id_ in ids}
    for row in db.query(DataHarian).filter(
            DataHarian.id_mitra.in_(ids), DataHarian.tanggal <= req.tanggal_selesai
    ).order_by(DataHarian.tanggal, DataHarian.id_data).all():
        histories[row.id_mitra].append(row)
    models = {id_: [] for id_ in ids}
    for model in db.query(ModelMitra).filter(
            ModelMitra.id_mitra.in_(ids), ModelMitra.status_model == "aktif").all():
        models[model.id_mitra].append(model)
    days = [req.tanggal_mulai + timedelta(days=i) for i in
            range((req.tanggal_selesai - req.tanggal_mulai).days + 1)]
    results, summaries = [], []
    for partner in partners:
        rows, warnings = [], []
        meta = {"id_mitra": partner.id_mitra, "nama_mitra": partner.nama_mitra}
        problem = None
        history = histories[partner.id_mitra]
        daily = {r.tanggal: r for r in history}
        if len(daily) != len(history):
            problem = "Ada laporan duplikat untuk tanggal yang sama."
        active = models[partner.id_mitra]
        model = features = bundle = None
        if len(active) != 1:
            problem = "Diperlukan tepat satu model aktif untuk mitra ini."
        elif problem is None:
            record = active[0]
            meta["id_model"] = record.id_model
            path = Path(record.path_model)
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[1] / path
            try:
                meta["model_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                bundle = joblib.load(path)
                model = bundle["model"] if isinstance(bundle, dict) else bundle
                features = bundle.get("fitur_model") if isinstance(bundle, dict) else None
                features = list(features) if features is not None else ["suplai", "return", "terjual"]
                metadata = bundle if isinstance(bundle, dict) else {}
                target = str(metadata.get("target_column", "Tidak tercatat"))
                meta["target_model"] = target
                verified_target = target in ("target_terjual_besok", "terjual_besok") or (
                    target == "target_suplai_besok" and req.target_suplai_adalah_terjual)
                meta["target_dikonfirmasi_pengguna"] = req.target_suplai_adalah_terjual
                if not verified_target:
                    warnings.append("Target model tercatat sebagai " + target +
                                    ". Pastikan target pelatihannya terjual hari berikutnya; skor belum terverifikasi.")
                for key in ("tanggal_train_akhir", "tanggal_test_akhir"):
                    value = metadata.get(key)
                    meta[key] = str(value) if value is not None else None
                    if value is not None:
                        # Bundle records feature dates; next-day labels may extend by one day.
                        if pd.Timestamp(value).date() + timedelta(days=1) >= req.tanggal_mulai:
                            problem = "Periode model (" + key + ") berpotensi memakai data evaluasi. Gunakan model dari periode sebelumnya."
                if metadata.get("tanggal_train_akhir") is None:
                    warnings.append("Periode latih tidak tercatat di file model; batas latih mengikuti keterangan pengguna.")
            except Exception:
                problem = "File model atau metadata gagal dibaca."
        engineered = None
        if problem is None:
            try:
                # Features are causal (shift/rolling backwards, cumulative counts).
                # Build once; a prediction sees ONLY the feature row for day - 1.
                feature_history = [r for r in history if r.tanggal < req.tanggal_selesai
                                   and valid_daily(r)]
                if feature_history:
                    frame = pd.DataFrame([{"tanggal": r.tanggal,
                        "nama_mitra": partner.nama_mitra, "suplai": r.jumlah_suplai,
                        "return": r.jumlah_return,
                        "terjual": r.jumlah_suplai - r.jumlah_return} for r in feature_history])
                    engineered = bentuk_fitur_prediksi(frame, partner.nama_mitra)
                    if not set(features).issubset(engineered.columns):
                        raise ValueError("Fitur model tidak didukung")
                    engineered.index = pd.to_datetime(engineered["tanggal"]).dt.date
            except Exception:
                problem = "Perhitungan fitur gagal atau fitur model tidak sesuai."
        for day in days:
            row = {"id_mitra": partner.id_mitra, "nama_mitra": partner.nama_mitra,
                   "tanggal": day.isoformat(), "batas_riwayat": (day-timedelta(days=1)).isoformat(),
                   "status": "dilewati", "alasan": problem}
            reason = problem
            actual = daily.get(day)
            previous = daily.get(day - timedelta(days=1))
            past = [r for r in history if r.tanggal < day]
            if reason is None:
                if actual is None:
                    reason = "Laporan aktual belum diinput."
                elif previous is None:
                    reason = "Laporan sehari sebelum target belum diinput."
                elif not valid_daily(actual) or any(not valid_daily(r) for r in past):
                    reason = "Ada suplai/return kosong atau tidak valid."
            if reason is None:
                try:
                    inputs = engineered.loc[[day-timedelta(days=1)], features].replace(
                        [np.inf, -np.inf], np.nan).fillna(0)
                    prediction = float(model.predict(inputs)[0])
                    if not math.isfinite(prediction):
                        raise ValueError("Prediksi tidak valid")
                    prediction = max(0.0, prediction)
                    sold = actual.jumlah_suplai - actual.jumlah_return
                    baseline = previous.jumlah_suplai - previous.jumlah_return
                    row.update(status="dinilai", alasan=None, prediksi=prediction,
                               aktual=sold, baseline=baseline,
                               selisih=prediction-sold, error_absolut=abs(prediction-sold))
                    rows.append(row)
                except Exception:
                    reason = "Perhitungan model gagal atau fitur tidak sesuai."
            if reason is not None:
                row["alasan"] = reason
            results.append(row)
        summaries.append({**meta, "peringatan": warnings, "metrik": metrics(rows),
                          "dilewati": len(days)-len(rows)})
    return {"tanggal_mulai": req.tanggal_mulai, "tanggal_selesai": req.tanggal_selesai,
            "tanggal_akhir_data_latih": req.tanggal_akhir_data_latih,
            "target_suplai_adalah_terjual": req.target_suplai_adalah_terjual,
            "ada_peringatan_model": any(item["peringatan"] for item in summaries),
            "catatan": "Batas latih berasal dari keterangan pengguna. Verifikasi juga data tuning/validasi model. Skor memakai prediksi mentah nonnegatif, tanpa pembulatan.",
            "metrik": metrics([r for r in results if r["status"] == "dinilai"]),
            "dilewati": sum(r["status"] != "dinilai" for r in results),
            "mitra": summaries, "data": results}
