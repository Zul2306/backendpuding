# Backend FastAPI

Backend menggunakan database MySQL `db_prediksi_suplai` dan model XGBoost di
folder `model_akhir_xgboost_per_mitra`.

## Menjalankan

```powershell
cd E:\TA\b
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Konfigurasi koneksi database dapat diubah dengan menyalin `.env.example`
menjadi `.env`, lalu menyesuaikan `DATABASE_URL`.

Dokumentasi API tersedia di `http://127.0.0.1:8000/docs`.
