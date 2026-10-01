from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import ceil
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import case, extract, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (BahanBaku, DataHarian, KategoriKeuangan, Mitra,
                        LogStokRusak, ModelMitra, MutasiStok, PrediksiHarian,
                        Produk, Produksi, ProduksiDetail, Resep, ResepBahan,
                        StockOpname, StokBatch,
                        StockOpnameDetail, TransaksiKeuangan)
from app.prediction_service import prediksi_suplai_besok
from app.schemas import (BahanBakuCreate, BahanBakuUpdate, DataHarianBatchCreate,
                         DataHarianCreate,
                         MitraCreate, MitraUpdate, ProduksiCreate, ResepBahanCreate,
                         ResepBahanUpdate, ResepCreate, ResepUpdate, PrediksiBatchConfirm,
                         PrediksiConfirm,
                         StockOpnameCreate, TransaksiKeuanganCreate,
                         PembelianBahanBakuCreate,
                         TransaksiKeuanganUpdate, PrediksiProductionCreate,
                         ProduksiEksekusiCreate, ResepHargaUpdate)

app = FastAPI(title="API Prediksi Suplai Puding", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUDDING_PRICE = Decimal("3000")
# Dipakai hanya bila resep prediksi aktif belum tersedia (PRD 6.2 memakai
# hasil_per_loyang dari resep aktif).
DEFAULT_TRAY_CAPACITY = 33
AUTOMATIC_SALES_NOTE = "[Otomatis] Penjualan puding seluruh mitra"
AUTOMATIC_RETURN_NOTE = "[Otomatis] Return puding seluruh mitra"
AUTOMATIC_MATERIAL_NOTE = "[Otomatis] Pemakaian bahan baku"


def _finance_category(db: Session, name: str, kind: str):
    category = (db.query(KategoriKeuangan).filter(
        KategoriKeuangan.nama_kategori == name,
        KategoriKeuangan.jenis == kind,
        KategoriKeuangan.status == "aktif").first())
    if category is None:
        raise HTTPException(500, f"Kategori keuangan aktif tidak ditemukan: {name}")
    return category


def _finance_category_auto(db: Session, name: str, kind: str):
    """Ambil kategori otomatis, buat bila belum tersedia."""
    category = (db.query(KategoriKeuangan).filter(
        KategoriKeuangan.nama_kategori == name,
        KategoriKeuangan.jenis == kind,
        KategoriKeuangan.status == "aktif").first())
    if category is None:
        category = KategoriKeuangan(nama_kategori=name, jenis=kind,
                                    sumber="otomatis", status="aktif")
        db.add(category)
        db.flush()
    return category


def _pudding_price(db: Session) -> Decimal:
    """Harga resep prediksi aktif untuk penjualan harian mitra."""
    recipe = (db.query(Resep).join(Produk, Produk.id_produk == Resep.id_produk)
              .filter(Resep.status == "aktif", Produk.status == "aktif",
                      Produk.metode_perencanaan == "prediksi")
              .order_by(Resep.id_resep).first())
    if recipe is None or recipe.harga_jual_per_potong <= 0:
        return PUDDING_PRICE
    return Decimal(recipe.harga_jual_per_potong)


def _sync_daily_finance(db: Session, tanggal: date):
    """Simpan total penjualan dan return seluruh mitra untuk satu tanggal."""
    totals = db.query(
        func.coalesce(func.sum(DataHarian.jumlah_terjual), 0),
        func.coalesce(func.sum(DataHarian.jumlah_return), 0),
    ).filter(DataHarian.tanggal == tanggal).one()
    sold, returned = (int(totals[0]), int(totals[1]))
    price = _pudding_price(db)
    entries = [
        ("Penjualan puding", "pemasukan", sold * price,
         AUTOMATIC_SALES_NOTE),
        ("Return puding", "pengeluaran", returned * price,
         AUTOMATIC_RETURN_NOTE),
    ]
    for name, kind, nominal, note in entries:
        category = _finance_category(db, name, kind)
        transaction = (db.query(TransaksiKeuangan).filter(
            TransaksiKeuangan.id_kategori == category.id_kategori,
            TransaksiKeuangan.tanggal_transaksi == tanggal,
            TransaksiKeuangan.referensi_tipe == "data_harian").first())
        if nominal <= 0:
            if transaction is not None:
                db.delete(transaction)
            continue
        if transaction is None:
            transaction = TransaksiKeuangan(
                id_kategori=category.id_kategori,
                tanggal_transaksi=tanggal,
                nominal=nominal,
                catatan=note,
                sumber="otomatis",
                referensi_tipe="data_harian",
            )
            db.add(transaction)
        else:
            transaction.nominal = nominal
            transaction.catatan = note


def _stock_query(db: Session):
    saldo = (
        db.query(
            MutasiStok.id_bahan.label("id_bahan"),
            func.coalesce(func.sum(
                MutasiStok.jumlah_masuk - MutasiStok.jumlah_keluar
            ), 0).label("stok_tersedia"),
        )
        .group_by(MutasiStok.id_bahan)
        .subquery()
    )
    return (
        db.query(BahanBaku, func.coalesce(saldo.c.stok_tersedia, 0))
        .outerjoin(saldo, saldo.c.id_bahan == BahanBaku.id_bahan)
        .filter(BahanBaku.status == "aktif")
    )


def _stock_json(bahan: BahanBaku, saldo):
    stok = float(saldo or 0)
    minimum = float(bahan.stok_minimum or 0)
    status = "Habis" if stok <= 0 else "Menipis" if stok <= minimum else "Aman"
    harga = float(bahan.harga_per_satuan_resep or 0)
    isi = float(bahan.rasio_konversi or 1)
    unit_beli = bahan.satuan_beli or bahan.satuan_resep
    kemasan = unit_beli.lower() in {"pcs", "dus", "sachet", "kaleng", "botol"} and isi > 1
    utuh = int(stok // isi) if kemasan else 0
    sisa = stok - (utuh * isi) if kemasan else 0
    return {
        "id_bahan": bahan.id_bahan,
        "nama_bahan": bahan.nama_bahan,
        "satuan": bahan.satuan_resep,
        "satuan_resep": bahan.satuan_resep,
        "satuan_beli": unit_beli,
        "rasio_konversi": isi,
        "kemasan_utuh": utuh,
        "kemasan_terbuka": 1 if kemasan and sisa > 0 else 0,
        "isi_tersisa": sisa,
        "stok_minimum": minimum,
        "stok_tersedia": stok,
        "harga_per_satuan_resep": harga,
        "nilai_stok": stok * harga,
        "status_stok": status,
    }


@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "message": "API berjalan"}


@app.get("/mitra")
def get_mitra(db: Session = Depends(get_db)):
    rows = (db.query(Mitra).filter(Mitra.status == "aktif")
            .order_by(Mitra.nama_mitra).all())
    return {"jumlah_mitra": len(rows), "data": [
        {"id_mitra": x.id_mitra, "nama_mitra": x.nama_mitra,
         "status": x.status} for x in rows
    ]}


@app.get("/mitra/manage")
def manage_mitra(
    pencarian: str = "",
    status: str = "semua",
    db: Session = Depends(get_db),
):
    query = db.query(Mitra)
    if pencarian.strip():
        query = query.filter(Mitra.nama_mitra.like(f"%{pencarian.strip()}%"))
    if status in ("aktif", "nonaktif"):
        query = query.filter(Mitra.status == status)
    rows = query.order_by(Mitra.nama_mitra).all()
    return {"jumlah_mitra": len(rows), "data": [
        {"id_mitra": row.id_mitra, "nama_mitra": row.nama_mitra,
         "status": row.status, "created_at": row.created_at,
         "updated_at": row.updated_at} for row in rows
    ]}


@app.post("/mitra", status_code=201)
def create_mitra(req: MitraCreate, db: Session = Depends(get_db)):
    row = Mitra(nama_mitra=req.nama_mitra.strip(), status=req.status)
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Nama mitra sudah digunakan")
    return {"status": "berhasil", "message": "Mitra berhasil ditambahkan",
            "data": {"id_mitra": row.id_mitra,
                     "nama_mitra": row.nama_mitra, "status": row.status}}


@app.put("/mitra/{id_mitra}")
def update_mitra(id_mitra: int, req: MitraUpdate,
                 db: Session = Depends(get_db)):
    row = db.query(Mitra).filter(Mitra.id_mitra == id_mitra).first()
    if not row:
        raise HTTPException(404, "Mitra tidak ditemukan")
    row.nama_mitra = req.nama_mitra.strip()
    row.status = req.status
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Nama mitra sudah digunakan")
    return {"status": "berhasil", "message": "Mitra berhasil diperbarui",
            "data": {"id_mitra": row.id_mitra,
                     "nama_mitra": row.nama_mitra, "status": row.status}}


@app.delete("/mitra/{id_mitra}")
def deactivate_mitra(id_mitra: int, db: Session = Depends(get_db)):
    row = db.query(Mitra).filter(Mitra.id_mitra == id_mitra).first()
    if not row:
        raise HTTPException(404, "Mitra tidak ditemukan")
    row.status = "nonaktif"
    db.commit()
    return {"status": "berhasil",
            "message": "Mitra dinonaktifkan agar data historis tetap aman"}


@app.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    today = date.today()
    latest = db.query(func.max(DataHarian.tanggal)).scalar()
    mitra_count = (db.query(func.count(Mitra.id_mitra))
                   .filter(Mitra.status == "aktif").scalar() or 0)
    supply = 0
    if latest:
        supply = (db.query(func.coalesce(func.sum(DataHarian.jumlah_suplai), 0))
                  .filter(DataHarian.tanggal == latest).scalar() or 0)
    stock = [_stock_json(x, s) for x, s in _stock_query(db).all()]

    input_count = (db.query(func.count(func.distinct(DataHarian.id_mitra)))
                   .join(Mitra, Mitra.id_mitra == DataHarian.id_mitra)
                   .filter(DataHarian.tanggal == today,
                           Mitra.status == "aktif").scalar() or 0)
    input_count = int(input_count)
    input_percentage = round(input_count / mitra_count * 100, 1) if mitra_count else 0

    stock_priority = {"Habis": 0, "Menipis": 1, "Aman": 2}
    stock_alerts = sorted(
        [item for item in stock if item["status_stok"] != "Aman"],
        key=lambda item: (stock_priority[item["status_stok"]], item["stok_tersedia"]),
    )[:3]

    latest_prediction_target = (db.query(func.max(PrediksiHarian.tanggal_target))
                                .filter(PrediksiHarian.is_test.is_(False),
                                        PrediksiHarian.status_prediksi != "dibatalkan")
                                .scalar())
    top_predictions = []
    if latest_prediction_target:
        prediction_rows = (
            db.query(PrediksiHarian, Mitra)
            .join(Mitra, Mitra.id_mitra == PrediksiHarian.id_mitra)
            .filter(PrediksiHarian.tanggal_target == latest_prediction_target,
                    PrediksiHarian.is_test.is_(False),
                    PrediksiHarian.status_prediksi != "dibatalkan",
                    Mitra.status == "aktif")
            .order_by(PrediksiHarian.jumlah_disetujui.desc())
            .limit(3)
            .all()
        )
        top_predictions = [
            {"id_mitra": partner.id_mitra,
             "nama_mitra": partner.nama_mitra,
             "hasil_prediksi": prediction.jumlah_disetujui}
            for prediction, partner in prediction_rows
        ]

    finance_rows = (
        db.query(TransaksiKeuangan, KategoriKeuangan)
        .join(KategoriKeuangan)
        .filter(TransaksiKeuangan.tanggal_transaksi == today)
        .all()
    )
    income = sum(float(row.nominal) for row, category in finance_rows
                 if category.jenis == "pemasukan")
    expense = sum(float(row.nominal) for row, category in finance_rows
                  if category.jenis == "pengeluaran")
    return {
        "tanggal_data_terakhir": latest,
        "total_mitra": int(mitra_count),
        "total_suplai": int(supply),
        "total_bahan": len(stock),
        "stok_menipis": sum(x["status_stok"] != "Aman" for x in stock),
        "status_input": {
            "tanggal": today,
            "total_mitra": int(mitra_count),
            "sudah_input": input_count,
            "belum_input": int(mitra_count) - input_count,
            "persentase": input_percentage,
        },
        "peringatan_stok": stock_alerts,
        "prediksi_tertinggi": {
            "tanggal_target": latest_prediction_target,
            "data": top_predictions,
        },
        "keuangan_hari_ini": {
            "pemasukan": income,
            "pengeluaran": expense,
            "laba_bersih": income - expense,
        },
    }


@app.post("/data-harian")
def save_daily(req: DataHarianCreate, db: Session = Depends(get_db)):
    # Suplai dan return dapat diinput untuk tanggal lampau atau hari ini. Data yang sudah tersimpan tidak dapat diubah.
    if req.tanggal > date.today():
        raise HTTPException(
            400, "Tanggal input data harian tidak boleh melebihi hari ini")
    partner = db.query(Mitra).filter(Mitra.id_mitra == req.id_mitra).first()
    if not partner:
        raise HTTPException(404, "Mitra tidak ditemukan")
    supply = 0 if req.mitra_tutup else req.jumlah_suplai
    returned = 0 if req.mitra_tutup else req.jumlah_return
    if returned > supply:
        raise HTTPException(400, "Jumlah return tidak boleh melebihi suplai")
    sold = supply - returned
    row = (db.query(DataHarian).filter(
        DataHarian.id_mitra == req.id_mitra,
        DataHarian.tanggal == req.tanggal).first())
    if row:
        raise HTTPException(
            409, "Data harian tanggal ini sudah diinput dan tidak dapat diubah")
    message = "Data harian berhasil ditambahkan"
    row = DataHarian(id_mitra=req.id_mitra, tanggal=req.tanggal,
                     jumlah_suplai=supply,
                     jumlah_return=returned,
                     jumlah_terjual=sold,
                     mitra_tutup=req.mitra_tutup)
    db.add(row)
    try:
        # Total penjualan dan return tanggal ini harus ikut diperbarui ketika
        # data harian mitra diubah, bukan hanya saat baris pertama dibuat.
        _sync_daily_finance(db, req.tanggal)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return {"status": "berhasil", "message": message, "data": {
        "id_data": row.id_data, "id_mitra": row.id_mitra,
        "nama_mitra": partner.nama_mitra, "tanggal": row.tanggal,
        "jumlah_suplai": row.jumlah_suplai,
        "jumlah_return": row.jumlah_return,
        "jumlah_terjual": row.jumlah_terjual,
        "mitra_tutup": row.mitra_tutup}}


@app.get("/data-harian/draft")
def daily_draft(tanggal: date, db: Session = Depends(get_db)):
    """Seluruh mitra aktif beserta nilai yang sudah tersimpan pada tanggal itu."""
    partners = (db.query(Mitra).filter(Mitra.status == "aktif")
                .order_by(Mitra.nama_mitra).all())
    latest_date = db.query(func.max(DataHarian.tanggal)).filter(
        DataHarian.tanggal < tanggal,
        DataHarian.mitra_tutup.is_(False),
    ).scalar()
    if tanggal == date.today() and latest_date is not None and latest_date < tanggal:
        current = latest_date + timedelta(days=1)
        holiday_dates = []
        while current < tanggal:
            if not db.query(DataHarian.id_data).filter(
                    DataHarian.tanggal == current).first():
                holiday_dates.append(current)
            current += timedelta(days=1)
        if holiday_dates:
            for holiday_date in holiday_dates:
                for partner in partners:
                    db.add(DataHarian(
                        id_mitra=partner.id_mitra,
                        tanggal=holiday_date,
                        jumlah_suplai=0,
                        jumlah_return=0,
                        jumlah_terjual=0,
                        mitra_tutup=True,
                    ))
            db.commit()
    existing = {
        row.id_mitra: row for row in db.query(DataHarian).filter(
            DataHarian.tanggal == tanggal).all()
    }
    predictions = {
        row.id_mitra: row for row in db.query(PrediksiHarian).filter(
            PrediksiHarian.tanggal_target == tanggal,
            PrediksiHarian.is_test.is_(False),
            PrediksiHarian.status_prediksi.in_(("disetujui", "diproduksi")),
        ).all()
    }
    reference_rows = {}
    if latest_date is not None:
        # Acuan dihitung per mitra. Sebelumnya acuan hilang begitu satu mitra
        # disimpan sementara pada tanggal ini sehingga mitra lain tampil 0
        # padahal belum diisi (PRD 4.3).
        reference_rows = {
            row.id_mitra: row for row in db.query(DataHarian).filter(
                DataHarian.tanggal == latest_date).all()
        }
    return {"tanggal": tanggal, "jumlah_mitra": len(partners), "data": [
        {"id_mitra": partner.id_mitra,
         "nama_mitra": partner.nama_mitra,
         "jumlah_suplai": existing[partner.id_mitra].jumlah_suplai
             if partner.id_mitra in existing
             else (predictions[partner.id_mitra].jumlah_disetujui
                 if partner.id_mitra in predictions
                 else (reference_rows[partner.id_mitra].jumlah_suplai
                     if partner.id_mitra in reference_rows else 0)),
         "jumlah_return": existing[partner.id_mitra].jumlah_return
             if partner.id_mitra in existing else 0,
         "mitra_tutup": bool(existing[partner.id_mitra].mitra_tutup)
             if partner.id_mitra in existing else False,
         "sudah_tersimpan": partner.id_mitra in existing,
         "suplai_dari_prediksi": partner.id_mitra in predictions and
             partner.id_mitra not in existing,
         "suplai_terkunci": partner.id_mitra in predictions and
             partner.id_mitra not in existing,
         "suplai_dari_acuan": partner.id_mitra in reference_rows and
             partner.id_mitra not in existing and
             partner.id_mitra not in predictions,
         "tanggal_acuan": latest_date}
        for partner in partners
    ]}


@app.post("/data-harian/batch")
def save_daily_batch(req: DataHarianBatchCreate,
                     db: Session = Depends(get_db)):
    """Validasi seluruh baris lalu simpan dalam satu transaksi database."""
    ids = [item.id_mitra for item in req.items]
    if len(ids) != len(set(ids)):
        raise HTTPException(400, "Mitra yang sama tidak boleh muncul dua kali")

    partners = db.query(Mitra).filter(Mitra.status == "aktif").all()
    partner_ids = {partner.id_mitra for partner in partners}
    submitted_ids = set(ids)
    unknown = submitted_ids - partner_ids
    not_submitted = partner_ids - submitted_ids
    if unknown:
        raise HTTPException(400, f"Mitra aktif tidak ditemukan: {sorted(unknown)}")
    if not_submitted:
        raise HTTPException(
            400,
            "Data belum mencakup seluruh mitra aktif. Muat ulang halaman input.",
        )
    # Suplai dan return dapat diinput untuk tanggal lampau atau hari ini. Bila baris tanggal itu sudah ada, seluruh batch ditolak
    # agar data yang sudah tersimpan tidak dapat ditimpa.
    if req.tanggal > date.today():
        raise HTTPException(
            400, "Tanggal input data harian tidak boleh melebihi hari ini")
    existing_row = db.query(DataHarian.id_data).filter(
        DataHarian.tanggal == req.tanggal).first()
    if existing_row is not None:
        raise HTTPException(
            409, "Data harian tanggal ini sudah diinput dan tidak dapat diubah")

    for item in req.items:
        supply = 0 if item.mitra_tutup else item.jumlah_suplai
        returned = 0 if item.mitra_tutup else item.jumlah_return
        if returned > supply:
            partner = next(x for x in partners if x.id_mitra == item.id_mitra)
            raise HTTPException(
                400,
                f"Return {partner.nama_mitra} tidak boleh melebihi suplai",
            )

    try:
        for item in req.items:
            supply = 0 if item.mitra_tutup else item.jumlah_suplai
            returned = 0 if item.mitra_tutup else item.jumlah_return
            row = db.query(DataHarian).filter(
                DataHarian.id_mitra == item.id_mitra,
                DataHarian.tanggal == req.tanggal,
            ).first()
            if row is None:
                row = DataHarian(id_mitra=item.id_mitra,
                                  tanggal=req.tanggal)
                db.add(row)
            row.jumlah_suplai = supply
            row.jumlah_return = returned
            row.jumlah_terjual = supply - returned
            row.mitra_tutup = item.mitra_tutup
        _sync_daily_finance(db, req.tanggal)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"status": "berhasil",
            "message": f"Data harian {len(req.items)} mitra berhasil disimpan",
            "tanggal": req.tanggal, "jumlah_disimpan": len(req.items)}


@app.get("/data-harian/{id_mitra}")
def daily_by_partner(id_mitra: int, db: Session = Depends(get_db)):
    partner = db.query(Mitra).filter(Mitra.id_mitra == id_mitra).first()
    if not partner:
        raise HTTPException(404, "Mitra tidak ditemukan")
    rows = (db.query(DataHarian).filter(DataHarian.id_mitra == id_mitra)
            .order_by(DataHarian.tanggal.desc()).limit(30).all())
    return {"status": "berhasil", "nama_mitra": partner.nama_mitra,
            "jumlah_data": len(rows), "data": [
        {"id_data": x.id_data, "tanggal": x.tanggal,
         "jumlah_suplai": x.jumlah_suplai,
         "jumlah_return": x.jumlah_return,
         "jumlah_terjual": x.jumlah_terjual,
         "mitra_tutup": bool(x.mitra_tutup)} for x in rows]}


@app.get("/riwayat-harian")
def daily_history(
    tahun: int = Query(default_factory=lambda: date.today().year),
    bulan: int = Query(default_factory=lambda: date.today().month, ge=1, le=12),
    db: Session = Depends(get_db),
):
    rows = (db.query(
        DataHarian.tanggal,
        func.sum(DataHarian.jumlah_suplai).label("supply"),
        func.sum(DataHarian.jumlah_return).label("returned"),
    ).filter(extract("year", DataHarian.tanggal) == tahun,
             extract("month", DataHarian.tanggal) == bulan)
      .group_by(DataHarian.tanggal).order_by(DataHarian.tanggal.desc()).all())
    return {
        "tahun": tahun, "bulan": bulan, "total_hari": len(rows),
        "rata_rata_suplai": round(sum(int(x.supply or 0) for x in rows) /
                                    len(rows), 1) if rows else 0,
        "rata_rata_return": round(sum(int(x.returned or 0) for x in rows) /
                                    len(rows), 1) if rows else 0,
        "data": [{"tanggal": x.tanggal,
                  "jumlah_suplai": int(x.supply or 0),
                  "jumlah_return": int(x.returned or 0)} for x in rows],
    }


def _prediction_recipe(db: Session):
    return (db.query(Resep).join(Produk, Produk.id_produk == Resep.id_produk)
            .filter(Resep.status == "aktif",
                    Produk.metode_perencanaan == "prediksi",
                    Produk.status == "aktif").first())


def _tray_capacity(recipe) -> int:
    """Kapasitas loyang mengikuti resep prediksi aktif (PRD 6.2)."""
    if recipe is not None and recipe.hasil_per_loyang:
        return int(recipe.hasil_per_loyang)
    return DEFAULT_TRAY_CAPACITY


def _recipe_materials(db: Session, id_resep: int, trays: int):
    """Rincian kebutuhan bahan resep untuk sejumlah loyang."""
    stock = {x.id_bahan: (x, float(s or 0)) for x, s in _stock_query(db).all()}
    details = db.query(ResepBahan).filter(
        ResepBahan.id_resep == id_resep,
        ResepBahan.status == "aktif").all()
    materials = []
    for detail in details:
        material, available = stock.get(detail.id_bahan, (None, 0))
        needed = float(detail.kebutuhan_per_loyang) * trays
        price = float(material.harga_per_satuan_resep or 0) if material else 0
        materials.append({"id_bahan": detail.id_bahan,
            "nama_bahan": material.nama_bahan if material else "Bahan tidak aktif",
            "kebutuhan": needed,
            "stok_tersedia": available,
            "satuan": material.satuan_resep if material else "-",
            "harga_per_satuan_resep": price,
            "nilai": needed * price,
            "cukup": available >= needed,
            "kekurangan": max(needed - available, 0)})
    return materials


def _prediction_recipe_plan(db: Session, predicted: int):
    recipe = _prediction_recipe(db)
    if not recipe:
        return None
    trays = ceil(predicted / recipe.hasil_per_loyang) if predicted > 0 else 0
    materials = _recipe_materials(db, recipe.id_resep, trays)
    return {"id_resep": recipe.id_resep, "nama_resep": recipe.nama_resep,
            "harga_jual_per_potong": recipe.harga_jual_per_potong,
            "hasil_per_loyang": recipe.hasil_per_loyang,
            "jumlah_loyang": trays,
            "hasil_produksi": trays * recipe.hasil_per_loyang,
            "sisa_produksi": trays * recipe.hasil_per_loyang - predicted,
            "stok_cukup": all(x["cukup"] for x in materials),
            "bahan": materials}


def _approved_prediction_total(db: Session):
    """Tanggal target terbaru beserta total jumlah_disetujui-nya (PRD 6.1).

    Dipakai bersama oleh preview produksi dan eksekusi produksi supaya angka
    kebutuhan produksi selalu mengacu pada target yang sama.
    """
    target = (db.query(func.max(PrediksiHarian.tanggal_target))
              .filter(PrediksiHarian.is_test.is_(False),
                      PrediksiHarian.status_prediksi.in_(["disetujui", "diproduksi"]))
              .scalar())
    if target is None:
        return None, 0
    total = (db.query(func.coalesce(func.sum(PrediksiHarian.jumlah_disetujui), 0))
             .filter(PrediksiHarian.tanggal_target == target,
                     PrediksiHarian.is_test.is_(False),
                     PrediksiHarian.status_prediksi.in_(["disetujui", "diproduksi"]))
             .scalar() or 0)
    return target, int(total)


def _prediction_total_plan(db: Session):
    # Cari target terbaru dari prediksi yang disetujui atau sudah diproduksi
    target, total = _approved_prediction_total(db)
    if target is None:
        raise HTTPException(404, "Belum ada prediksi yang disetujui")
    # Cek apakah semua prediksi untuk target ini sudah diproduksi
    sudah_diproduksi = not bool(
        db.query(PrediksiHarian).filter(
            PrediksiHarian.tanggal_target == target,
            PrediksiHarian.is_test.is_(False),
            PrediksiHarian.status_prediksi == "disetujui"
        ).first()
    )
    plan = _prediction_recipe_plan(db, total)
    if plan is None:
        raise HTTPException(404, "Resep prediksi aktif belum tersedia")
    plan["tanggal_target"] = target
    plan["jumlah_prediksi"] = int(total)
    plan["stok_cukup"] = all(item["cukup"] for item in plan["bahan"])
    plan["sudah_diproduksi"] = sudah_diproduksi
    return plan


@app.get("/prediksi/produksi/preview")
def preview_prediction_production(
    stok_manual_dipakai: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
):
    plan = _prediction_total_plan(db)
    batch_plan = _batch_production_plan(
        db, plan["jumlah_prediksi"], stok_manual_dipakai)
    plan.update(batch_plan)
    # Bahan baku dihitung hanya untuk loyang yang benar-benar diproduksi,
    # yaitu kekurangan setelah memakai stok puding yang masih layak.
    trays = int(plan["jumlah_loyang"])
    plan["bahan"] = _recipe_materials(db, plan["id_resep"], trays)
    plan["stok_cukup"] = all(item["cukup"] for item in plan["bahan"])
    plan["estimasi_nilai_bahan"] = sum(item["nilai"] for item in plan["bahan"])
    return {"data": plan}


def _batch_production_plan(db: Session, prediksi_kebutuhan: int,
                           stok_manual_dipakai: int | None = None):
    now = datetime.now()
    batches = (db.query(StokBatch)
               .filter(StokBatch.jumlah_sisa > 0,
                       StokBatch.tanggal_kadaluarsa > now)
               .order_by(StokBatch.created_at.asc())
               .all())
    total_stok_sistem = sum(batch.jumlah_sisa for batch in batches)
    stok_manual = (total_stok_sistem if stok_manual_dipakai is None
                   else min(stok_manual_dipakai, total_stok_sistem))
    stok_rusak = max(0, total_stok_sistem - stok_manual)
    stok_layak = stok_manual
    kekurangan = max(0, prediksi_kebutuhan - stok_layak)
    kapasitas_per_loyang = _tray_capacity(_prediction_recipe(db))
    jumlah_loyang = ceil(kekurangan / kapasitas_per_loyang)
    sisa_stok_baru = (jumlah_loyang * kapasitas_per_loyang) - kekurangan
    return {
        "prediksi_kebutuhan": prediksi_kebutuhan,
        "total_stok_sistem": total_stok_sistem,
        "stok_manual_dipakai": stok_manual,
        "stok_rusak": stok_rusak,
        "stok_layak": stok_layak,
        "kekurangan": kekurangan,
        "kapasitas_per_loyang": kapasitas_per_loyang,
        "jumlah_loyang": jumlah_loyang,
        "sisa_stok_baru": sisa_stok_baru,
        "tanggal_kadaluarsa_batch_baru": (
            now + timedelta(days=7) if jumlah_loyang > 0 else None),
    }


def _calculate_prediction(id_mitra: int, db: Session):
    partner = db.query(Mitra).filter(Mitra.id_mitra == id_mitra).first()
    if not partner:
        raise HTTPException(404, "Mitra tidak ditemukan")
    model = (db.query(ModelMitra).filter(ModelMitra.id_mitra == id_mitra,
                                        ModelMitra.status_model == "aktif").first())
    if not model:
        raise HTTPException(404, "Model aktif tidak ditemukan")
    history = (db.query(DataHarian).filter(DataHarian.id_mitra == id_mitra)
               .order_by(DataHarian.tanggal).all())
    if not history:
        raise HTTPException(400, "Data historis belum tersedia")
    frame = pd.DataFrame([{"tanggal": x.tanggal,
                           "nama_mitra": partner.nama_mitra,
                           "suplai": x.jumlah_suplai,
                           "return": x.jumlah_return,
                           "terjual": x.jumlah_terjual} for x in history])
    model_path = Path(model.path_model)
    if not model_path.is_absolute():
        model_path = Path(__file__).resolve().parents[1] / model_path
    result = prediksi_suplai_besok(frame, str(model_path))
    return partner, model, result


@app.post("/prediksi/preview/{id_mitra}")
@app.post("/prediksi/{id_mitra}")
def preview_prediction(id_mitra: int, db: Session = Depends(get_db)):
    partner, model, result = _calculate_prediction(id_mitra, db)
    rounded = result["hasil_prediksi_bulat"]
    return {"status": "berhasil",
            "message": "Preview berhasil. Data belum disimpan.",
            "data": {"id_mitra": id_mitra, "id_model": model.id_model,
                     "nama_mitra": partner.nama_mitra,
                     "tanggal_data_terakhir": result["tanggal_terakhir"],
                     "tanggal_prediksi": date.today(),
                     "tanggal_target": result["tanggal_target"],
                     "hasil_prediksi": round(result["hasil_prediksi"], 4),
                     "hasil_prediksi_bulat": rounded,
                     "jumlah_disetujui": rounded,
                     "tersimpan": False,
                     "rencana_resep": _prediction_recipe_plan(db, rounded)}}


@app.post("/prediksi/batch/preview")
def preview_all_predictions(db: Session = Depends(get_db)):
    """Hitung seluruh mitra aktif tanpa membuat record prediksi."""
    partners = (db.query(Mitra).filter(Mitra.status == "aktif")
                .order_by(Mitra.nama_mitra).all())
    previews, failed = [], []
    for partner in partners:
        try:
            _, model, result = _calculate_prediction(partner.id_mitra, db)
            rounded = result["hasil_prediksi_bulat"]
            previews.append({
                "id_mitra": partner.id_mitra,
                "id_model": model.id_model,
                "nama_mitra": partner.nama_mitra,
                "tanggal_data_terakhir": result["tanggal_terakhir"],
                "tanggal_prediksi": date.today(),
                "tanggal_target": result["tanggal_target"],
                "hasil_prediksi": round(result["hasil_prediksi"], 4),
                "hasil_prediksi_bulat": rounded,
                "jumlah_disetujui": rounded,
                "tersimpan": False,
            })
        except HTTPException as exc:
            failed.append({"id_mitra": partner.id_mitra,
                           "nama_mitra": partner.nama_mitra,
                           "pesan": str(exc.detail)})
        except Exception:
            failed.append({"id_mitra": partner.id_mitra,
                           "nama_mitra": partner.nama_mitra,
                           "pesan": "Model gagal diproses"})
    return {"status": "berhasil",
            "message": "Preview seluruh mitra selesai. Data belum disimpan.",
            "jumlah_mitra": len(partners),
            "jumlah_berhasil": len(previews),
            "data": previews, "gagal": failed}


@app.post("/prediksi/konfirmasi")
def confirm_prediction(req: PrediksiConfirm, db: Session = Depends(get_db)):
    partner, model, result = _calculate_prediction(req.id_mitra, db)
    target = result["tanggal_target"]
    row = (db.query(PrediksiHarian).filter(
        PrediksiHarian.id_mitra == req.id_mitra,
        PrediksiHarian.tanggal_target == target,
        PrediksiHarian.is_test.is_(False)).first())
    message = "Prediksi berhasil diperbarui"
    if row:
        if row.status_prediksi == "diproduksi":
            raise HTTPException(409, "Prediksi sudah diproduksi dan tidak dapat diubah")
        row.id_model = model.id_model
        row.tanggal_prediksi = date.today()
        row.hasil_prediksi = result["hasil_prediksi"]
        row.hasil_prediksi_bulat = result["hasil_prediksi_bulat"]
        row.jumlah_disetujui = req.jumlah_disetujui
        row.status_prediksi = "disetujui"
        row.dikonfirmasi_at = func.now()
        row.is_test = False
    else:
        message = "Prediksi berhasil dikonfirmasi"
        row = PrediksiHarian(id_mitra=req.id_mitra, id_model=model.id_model,
                             tanggal_prediksi=date.today(), tanggal_target=target,
                             hasil_prediksi=result["hasil_prediksi"],
                             hasil_prediksi_bulat=result["hasil_prediksi_bulat"],
                             jumlah_disetujui=req.jumlah_disetujui,
                             status_prediksi="disetujui",
                             dikonfirmasi_at=func.now(), is_test=False)
        db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "berhasil", "message": message, "data": {
        "id_prediksi": row.id_prediksi, "id_mitra": req.id_mitra,
        "nama_mitra": partner.nama_mitra,
        "tanggal_data_terakhir": result["tanggal_terakhir"],
        "tanggal_prediksi": row.tanggal_prediksi,
        "tanggal_target": row.tanggal_target,
        "hasil_prediksi": round(row.hasil_prediksi, 4),
        "hasil_prediksi_bulat": row.hasil_prediksi_bulat,
        "jumlah_disetujui": row.jumlah_disetujui,
        "status_prediksi": row.status_prediksi,
        "dikonfirmasi_at": row.dikonfirmasi_at,
        "tersimpan": True,
        "rencana_resep": _prediction_recipe_plan(
            db, row.jumlah_disetujui)}}


@app.post("/prediksi/konfirmasi/batch")
def confirm_all_predictions(req: PrediksiBatchConfirm,
                            db: Session = Depends(get_db)):
    """Simpan hasil yang telah disetujui dalam satu transaksi."""
    ids = [item.id_mitra for item in req.items]
    if len(ids) != len(set(ids)):
        raise HTTPException(400, "Mitra yang sama tidak boleh dikonfirmasi dua kali")

    calculated = []
    for item in req.items:
        partner, model, result = _calculate_prediction(item.id_mitra, db)
        target = result["tanggal_target"]
        existing = (db.query(PrediksiHarian).filter(
            PrediksiHarian.id_mitra == item.id_mitra,
            PrediksiHarian.tanggal_target == target,
            PrediksiHarian.is_test.is_(False)).first())
        if existing and existing.status_prediksi == "diproduksi":
            raise HTTPException(
                409, f"Prediksi {partner.nama_mitra} sudah diproduksi dan tidak dapat diubah")
        calculated.append((item, partner, model, result, existing))

    try:
        for item, partner, model, result, row in calculated:
            if row is None:
                row = PrediksiHarian(
                    id_mitra=item.id_mitra, id_model=model.id_model,
                    tanggal_prediksi=date.today(),
                    tanggal_target=result["tanggal_target"],
                    hasil_prediksi=result["hasil_prediksi"],
                    hasil_prediksi_bulat=result["hasil_prediksi_bulat"],
                    jumlah_disetujui=item.jumlah_disetujui,
                    status_prediksi="disetujui", dikonfirmasi_at=func.now(),
                    is_test=False)
                db.add(row)
            else:
                row.id_model = model.id_model
                row.tanggal_prediksi = date.today()
                row.hasil_prediksi = result["hasil_prediksi"]
                row.hasil_prediksi_bulat = result["hasil_prediksi_bulat"]
                row.jumlah_disetujui = item.jumlah_disetujui
                row.status_prediksi = "disetujui"
                row.dikonfirmasi_at = func.now()
                row.is_test = False
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"status": "berhasil",
            "message": f"{len(calculated)} prediksi berhasil dikonfirmasi",
            "jumlah_disimpan": len(calculated)}


@app.get("/prediksi/{id_mitra}")
def prediction_history(id_mitra: int, db: Session = Depends(get_db)):
    partner = db.query(Mitra).filter(Mitra.id_mitra == id_mitra).first()
    if not partner:
        raise HTTPException(404, "Mitra tidak ditemukan")
    rows = (db.query(PrediksiHarian).filter(PrediksiHarian.id_mitra == id_mitra)
            .order_by(PrediksiHarian.tanggal_target.desc(),
                      PrediksiHarian.id_prediksi.desc()).limit(30).all())
    return {"status": "berhasil", "nama_mitra": partner.nama_mitra,
            "data": [{"id_prediksi": x.id_prediksi,
                      "tanggal_prediksi": x.tanggal_prediksi,
                      "tanggal_target": x.tanggal_target,
                      "hasil_prediksi": x.hasil_prediksi,
                      "hasil_prediksi_bulat": x.hasil_prediksi_bulat,
                      "jumlah_disetujui": x.jumlah_disetujui,
                      "status_prediksi": x.status_prediksi,
                      "dikonfirmasi_at": x.dikonfirmasi_at,
                      "tersimpan": True,
                      "is_test": bool(x.is_test),
                      "rencana_resep": _prediction_recipe_plan(
                          db, x.jumlah_disetujui)}
                     for x in rows]}


@app.delete("/prediksi-uji/{id_prediksi}")
def delete_test_prediction(id_prediksi: int, db: Session = Depends(get_db)):
    row = db.query(PrediksiHarian).filter(
        PrediksiHarian.id_prediksi == id_prediksi).first()
    if not row:
        raise HTTPException(404, "Prediksi tidak ditemukan")
    if not row.is_test:
        raise HTTPException(403, "Hanya prediksi mode uji yang boleh dihapus")
    db.delete(row)
    db.commit()
    return {"status": "berhasil", "message": "Data prediksi uji berhasil dihapus"}


@app.get("/stok")
def get_stock(db: Session = Depends(get_db)):
    rows = [_stock_json(x, s) for x, s in _stock_query(db).all()]
    return {"total_bahan": len(rows),
            "total_stok": sum(x["stok_tersedia"] for x in rows),
            "stok_menipis": sum(x["status_stok"] != "Aman" for x in rows),
            "nilai_stok": sum(x["nilai_stok"] for x in rows), "data": rows}


@app.get("/bahan-baku")
def manage_bahan_baku(pencarian: str = "", status: str = "semua",
                      db: Session = Depends(get_db)):
    query = db.query(BahanBaku)
    if pencarian.strip():
        query = query.filter(BahanBaku.nama_bahan.like(f"%{pencarian.strip()}%"))
    if status in ("aktif", "nonaktif"):
        query = query.filter(BahanBaku.status == status)
    rows = query.order_by(BahanBaku.nama_bahan).all()
    return {"data": [{"id_bahan": x.id_bahan, "nama_bahan": x.nama_bahan,
                      "satuan": x.satuan_resep,
                      "satuan_resep": x.satuan_resep,
                      "satuan_beli": x.satuan_beli,
                      "rasio_konversi": x.rasio_konversi,
                      "harga_beli": x.harga_beli,
                      "stok_minimum": x.stok_minimum,
                      "harga_per_satuan_resep": x.harga_per_satuan_resep,
                      "status": x.status} for x in rows]}


@app.post("/bahan-baku")
def create_bahan_baku(req: BahanBakuCreate, db: Session = Depends(get_db)):
    values = req.model_dump()
    values["harga_per_satuan_resep"] = values["harga_beli"] // values["rasio_konversi"]
    row = BahanBaku(**values)
    row.nama_bahan = row.nama_bahan.strip()
    row.satuan_resep = row.satuan_resep.strip()
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Nama bahan baku sudah digunakan")
    return {"status": "berhasil", "message": "Bahan baku berhasil ditambahkan",
            "data": {"id_bahan": row.id_bahan}}


@app.put("/bahan-baku/{id_bahan}")
def update_bahan_baku(id_bahan: int, req: BahanBakuUpdate,
                      db: Session = Depends(get_db)):
    row = db.query(BahanBaku).filter(BahanBaku.id_bahan == id_bahan).first()
    if not row:
        raise HTTPException(404, "Bahan baku tidak ditemukan")
    for key, value in req.model_dump().items():
        setattr(row, key, value.strip() if isinstance(value, str) else value)
    row.harga_per_satuan_resep = row.harga_beli // row.rasio_konversi
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Nama bahan baku sudah digunakan")
    return {"status": "berhasil", "message": "Bahan baku berhasil diperbarui"}


@app.delete("/bahan-baku/{id_bahan}")
def deactivate_bahan_baku(id_bahan: int, db: Session = Depends(get_db)):
    row = db.query(BahanBaku).filter(BahanBaku.id_bahan == id_bahan).first()
    if not row:
        raise HTTPException(404, "Bahan baku tidak ditemukan")
    row.status = "nonaktif"
    # Bahan nonaktif tidak boleh tetap tampil pada resep aktif (PRD 2.2).
    # Tanpa ini, bahan nonaktif tetap dihitung kebutuhan produksinya tetapi
    # stoknya tidak dibaca sehingga produksi selalu gagal karena stok 0.
    affected = (db.query(ResepBahan)
                .filter(ResepBahan.id_bahan == id_bahan,
                        ResepBahan.status == "aktif")
                .update({ResepBahan.status: "nonaktif"},
                        synchronize_session=False))
    db.commit()
    message = "Bahan dinonaktifkan; riwayat stok tetap tersimpan"
    if affected:
        message += f" dan dikeluarkan dari {affected} resep aktif"
    return {"status": "berhasil", "message": message}


@app.post("/pembelian-bahan-baku", status_code=201)
def create_material_purchase(req: PembelianBahanBakuCreate,
                             db: Session = Depends(get_db)):
    material = (db.query(BahanBaku).filter(
        BahanBaku.id_bahan == req.id_bahan,
        BahanBaku.status == "aktif").first())
    if not material:
        raise HTTPException(404, "Bahan baku aktif tidak ditemukan")

    if req.ubah_harga_beli:
        if req.harga_beli_baru is None:
            raise HTTPException(422, "Harga beli baru wajib diisi")
        material.harga_beli = req.harga_beli_baru
        material.harga_per_satuan_resep = (
            req.harga_beli_baru // material.rasio_konversi)

    isi = Decimal(material.rasio_konversi or 1)
    jumlah_masuk = req.jumlah_pembelian * isi
    harga_satuan = req.harga_total / jumlah_masuk
    mutation = MutasiStok(
        id_bahan=req.id_bahan,
        tanggal_mutasi=datetime.combine(req.tanggal_pembelian, time.min),
        jenis_mutasi="pembelian",
        jumlah_masuk=jumlah_masuk,
        jumlah_keluar=0,
        harga_satuan=harga_satuan,
        referensi_tipe="pembelian_bahan_baku",
        catatan=req.catatan,
    )
    db.add(mutation)

    category = (db.query(KategoriKeuangan).filter(
        KategoriKeuangan.nama_kategori == "Pembelian bahan baku",
        KategoriKeuangan.jenis == "pengeluaran",
        KategoriKeuangan.status == "aktif").first())
    if category is None:
        category = KategoriKeuangan(nama_kategori="Pembelian bahan baku",
                                    jenis="pengeluaran", status="aktif")
        db.add(category)
        db.flush()
    finance = TransaksiKeuangan(
        id_kategori=category.id_kategori,
        tanggal_transaksi=req.tanggal_pembelian,
        nominal=req.harga_total,
        catatan=req.catatan or f"Pembelian {material.nama_bahan}",
        sumber="otomatis",
        referensi_tipe="pembelian_bahan_baku",
    )
    db.add(finance)
    db.commit()
    db.refresh(mutation)
    return {"status": "berhasil",
            "message": "Pembelian bahan baku berhasil disimpan",
            "data": {"id_mutasi": mutation.id_mutasi,
                     "id_bahan": req.id_bahan,
                     "jumlah_pembelian": req.jumlah_pembelian,
                     "jumlah_masuk": jumlah_masuk,
                     "harga_total": req.harga_total}}


def _recipe_json(db: Session, recipe: Resep):
    product = db.query(Produk).filter(Produk.id_produk == recipe.id_produk).first()
    rows = (db.query(ResepBahan, BahanBaku)
            .join(BahanBaku, BahanBaku.id_bahan == ResepBahan.id_bahan)
            .filter(ResepBahan.id_resep == recipe.id_resep)
            .order_by(BahanBaku.nama_bahan).all())
    return {"id_resep": recipe.id_resep, "id_produk": recipe.id_produk,
            "nama_produk": product.nama_produk if product else "-",
            "metode_perencanaan": product.metode_perencanaan if product else "-",
            "nama_resep": recipe.nama_resep,
            "harga_jual_per_potong": recipe.harga_jual_per_potong,
            "hasil_per_loyang": recipe.hasil_per_loyang,
            "satuan_hasil": recipe.satuan_hasil, "status": recipe.status,
            "bahan": [{"id_resep_bahan": detail.id_resep_bahan,
                       "id_bahan": material.id_bahan,
                       "nama_bahan": material.nama_bahan,
                       "satuan": material.satuan_resep,
                       "kebutuhan_per_loyang": float(detail.kebutuhan_per_loyang),
                       "status": detail.status}
                      for detail, material in rows]}


@app.get("/resep/manage")
def manage_recipes(pencarian: str = "", status: str = "semua",
                   db: Session = Depends(get_db)):
    query = db.query(Resep)
    if pencarian.strip():
        query = query.filter(Resep.nama_resep.like(f"%{pencarian.strip()}%"))
    if status in ("aktif", "nonaktif"):
        query = query.filter(Resep.status == status)
    rows = query.order_by(Resep.nama_resep).all()
    return {"data": [_recipe_json(db, row) for row in rows]}


@app.get("/produk")
def get_products(metode: str = "semua", db: Session = Depends(get_db)):
    query = db.query(Produk).filter(Produk.status == "aktif")
    if metode in ("prediksi", "manual"):
        query = query.filter(Produk.metode_perencanaan == metode)
    rows = query.order_by(Produk.nama_produk).all()
    return {"data": [{"id_produk": row.id_produk,
                      "nama_produk": row.nama_produk,
                      "metode_perencanaan": row.metode_perencanaan,
                      "disuplai_ke_mitra": bool(row.disuplai_ke_mitra),
                      "harga_jual_per_potong": row.harga_jual_per_potong,
                      "status": row.status} for row in rows]}


@app.put("/resep/{id_resep}/harga")
def update_recipe_price(id_resep: int, req: ResepHargaUpdate,
                        db: Session = Depends(get_db)):
    try:
        recipe = db.query(Resep).filter(Resep.id_resep == id_resep).first()
        if recipe is None:
            raise HTTPException(404, "Resep tidak ditemukan")
        recipe.harga_jual_per_potong = req.harga_jual_per_potong
        db.commit()
        return {"message": "Harga jual berhasil disimpan", "data": {
            "id_resep": recipe.id_resep,
            "harga_jual_per_potong": recipe.harga_jual_per_potong}}
    except Exception:
        db.rollback()
        raise


@app.post("/resep")
def create_recipe(req: ResepCreate, db: Session = Depends(get_db)):
    if not db.query(Produk).filter(Produk.id_produk == req.id_produk).first():
        raise HTTPException(404, "Produk tidak ditemukan")
    row = Resep(**req.model_dump())
    row.nama_resep = row.nama_resep.strip()
    row.satuan_hasil = row.satuan_hasil.strip()
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "berhasil", "message": "Resep berhasil ditambahkan",
            "data": _recipe_json(db, row)}


@app.put("/resep/{id_resep}")
def update_recipe(id_resep: int, req: ResepUpdate,
                  db: Session = Depends(get_db)):
    row = db.query(Resep).filter(Resep.id_resep == id_resep).first()
    if not row:
        raise HTTPException(404, "Resep tidak ditemukan")
    if not db.query(Produk).filter(Produk.id_produk == req.id_produk).first():
        raise HTTPException(404, "Produk tidak ditemukan")
    for key, value in req.model_dump().items():
        setattr(row, key, value.strip() if isinstance(value, str) else value)
    db.commit()
    return {"status": "berhasil", "message": "Resep berhasil diperbarui",
            "data": _recipe_json(db, row)}


@app.delete("/resep/{id_resep}")
def deactivate_recipe(id_resep: int, db: Session = Depends(get_db)):
    row = db.query(Resep).filter(Resep.id_resep == id_resep).first()
    if not row:
        raise HTTPException(404, "Resep tidak ditemukan")
    row.status = "nonaktif"
    db.query(ResepBahan).filter(ResepBahan.id_resep == id_resep).update(
        {ResepBahan.status: "nonaktif"}, synchronize_session=False)
    db.commit()
    return {"status": "berhasil",
            "message": "Resep dan bahan resep dinonaktifkan"}


@app.post("/resep/{id_resep}/bahan")
def create_recipe_material(id_resep: int, req: ResepBahanCreate,
                           db: Session = Depends(get_db)):
    if not db.query(Resep).filter(Resep.id_resep == id_resep).first():
        raise HTTPException(404, "Resep tidak ditemukan")
    if not db.query(BahanBaku).filter(BahanBaku.id_bahan == req.id_bahan).first():
        raise HTTPException(404, "Bahan baku tidak ditemukan")
    row = ResepBahan(id_resep=id_resep, **req.model_dump())
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Bahan tersebut sudah ada pada resep")
    return {"status": "berhasil", "message": "Bahan resep berhasil ditambahkan"}


@app.put("/resep-bahan/{id_resep_bahan}")
def update_recipe_material(id_resep_bahan: int, req: ResepBahanUpdate,
                           db: Session = Depends(get_db)):
    row = db.query(ResepBahan).filter(
        ResepBahan.id_resep_bahan == id_resep_bahan).first()
    if not row:
        raise HTTPException(404, "Bahan resep tidak ditemukan")
    if not db.query(BahanBaku).filter(BahanBaku.id_bahan == req.id_bahan).first():
        raise HTTPException(404, "Bahan baku tidak ditemukan")
    for key, value in req.model_dump().items():
        setattr(row, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Bahan tersebut sudah ada pada resep")
    return {"status": "berhasil", "message": "Bahan resep berhasil diperbarui"}


@app.delete("/resep-bahan/{id_resep_bahan}")
def deactivate_recipe_material(id_resep_bahan: int,
                               db: Session = Depends(get_db)):
    row = db.query(ResepBahan).filter(
        ResepBahan.id_resep_bahan == id_resep_bahan).first()
    if not row:
        raise HTTPException(404, "Bahan resep tidak ditemukan")
    row.status = "nonaktif"
    db.commit()
    return {"status": "berhasil",
            "message": "Bahan resep berhasil dinonaktifkan"}


@app.get("/resep/aktif")
def get_active_recipe(db: Session = Depends(get_db)):
    recipe = (db.query(Resep).join(Produk, Produk.id_produk == Resep.id_produk)
              .filter(Resep.status == "aktif",
                      Produk.metode_perencanaan == "prediksi").first())
    if not recipe:
        raise HTTPException(404, "Resep aktif tidak ditemukan")
    stock = {x.id_bahan: (x, float(s or 0)) for x, s in _stock_query(db).all()}
    details = (db.query(ResepBahan, BahanBaku)
               .join(BahanBaku, BahanBaku.id_bahan == ResepBahan.id_bahan)
               .filter(ResepBahan.id_resep == recipe.id_resep,
                       ResepBahan.status == "aktif").all())
    return {"data": {"id_resep": recipe.id_resep,
            "nama_resep": recipe.nama_resep,
            "hasil_per_loyang": recipe.hasil_per_loyang,
            "satuan_hasil": recipe.satuan_hasil,
            "bahan": [{"id_bahan": material.id_bahan,
                       "nama_bahan": material.nama_bahan,
                       "kebutuhan_per_loyang": float(detail.kebutuhan_per_loyang),
                       "satuan": material.satuan_resep,
                       "stok_tersedia": stock.get(material.id_bahan, (None, 0))[1]}
                      for detail, material in details]}}


def _manual_production_plan(db: Session, product_id: int, trays: int):
    product = db.query(Produk).filter(
        Produk.id_produk == product_id, Produk.status == "aktif",
        Produk.metode_perencanaan == "manual").first()
    if not product:
        raise HTTPException(404, "Produk manual tidak ditemukan")
    recipe = db.query(Resep).filter(
        Resep.id_produk == product_id, Resep.status == "aktif").first()
    if not recipe:
        raise HTTPException(404, "Resep aktif produk belum tersedia")
    stock = {x.id_bahan: (x, float(s or 0)) for x, s in _stock_query(db).all()}
    details = db.query(ResepBahan).filter(
        ResepBahan.id_resep == recipe.id_resep,
        ResepBahan.status == "aktif").all()
    materials = []
    for detail in details:
        material, available = stock.get(detail.id_bahan, (None, 0))
        needed = float(detail.kebutuhan_per_loyang) * trays
        materials.append({"id_bahan": detail.id_bahan,
            "nama_bahan": material.nama_bahan if material else "Bahan tidak aktif",
            "kebutuhan": needed, "stok_tersedia": available,
            "satuan": material.satuan_resep if material else "-",
            "cukup": available >= needed,
            "kekurangan": max(needed - available, 0)})
    return {"id_produk": product.id_produk, "nama_produk": product.nama_produk,
            "id_resep": recipe.id_resep, "nama_resep": recipe.nama_resep,
            "jumlah_loyang": trays,
            "hasil_produksi": trays * recipe.hasil_per_loyang,
            "satuan_hasil": recipe.satuan_hasil,
            "stok_cukup": bool(materials) and all(x["cukup"] for x in materials),
            "bahan": materials}


@app.get("/produksi/manual/preview")
def preview_manual_production(id_produk: int, jumlah_loyang: int = Query(ge=1),
                              db: Session = Depends(get_db)):
    return {"data": _manual_production_plan(db, id_produk, jumlah_loyang)}


@app.post("/produksi")
def create_production(req: ProduksiCreate, db: Session = Depends(get_db)):
    recipe = db.query(Resep).filter(Resep.id_resep == req.id_resep,
                                    Resep.status == "aktif").first()
    if not recipe:
        raise HTTPException(404, "Resep aktif tidak ditemukan")
    details = db.query(ResepBahan).filter(
        ResepBahan.id_resep == req.id_resep, ResepBahan.status == "aktif").all()
    if not details:
        raise HTTPException(400, "Bahan resep belum diisi")
    stock = {x.id_bahan: (x, float(s or 0)) for x, s in _stock_query(db).all()}
    shortages = []
    for detail in details:
        needed = float(detail.kebutuhan_per_loyang) * req.jumlah_loyang
        available = stock.get(detail.id_bahan, (None, 0))[1]
        if available < needed:
            material = stock.get(detail.id_bahan, (None, 0))[0]
            shortages.append(f"{material.nama_bahan if material else detail.id_bahan}: kurang {needed - available:g}")
    if shortages:
        raise HTTPException(400, "Stok tidak cukup: " + ", ".join(shortages))
    production = Produksi(id_resep=req.id_resep,
        sumber_produksi=req.sumber_produksi, jumlah_diminta=req.jumlah_diminta,
        id_prediksi=req.id_prediksi,
        tanggal_produksi=req.tanggal_produksi, jumlah_loyang=req.jumlah_loyang,
        hasil_produksi=recipe.hasil_per_loyang * req.jumlah_loyang,
        status="selesai", catatan=req.catatan)
    db.add(production)
    db.flush()
    material_cost = Decimal("0")
    for detail in details:
        material, before = stock[detail.id_bahan]
        used = float(detail.kebutuhan_per_loyang) * req.jumlah_loyang
        material_cost += Decimal(str(used)) * Decimal(
            material.harga_per_satuan_resep or 0)
        db.add(ProduksiDetail(id_produksi=production.id_produksi,
            id_bahan=detail.id_bahan, jumlah_pakai=used,
            stok_sebelum=before, stok_sesudah=before-used))
        db.add(MutasiStok(id_bahan=detail.id_bahan,
            tanggal_mutasi=req.tanggal_produksi, jenis_mutasi="pemakaian_produksi",
            jumlah_masuk=0, jumlah_keluar=used,
            referensi_tipe="produksi", referensi_id=production.id_produksi,
            catatan=f"Produksi {req.jumlah_loyang} loyang {recipe.nama_resep}"))
    if material_cost > 0:
        category = _finance_category(db, "Pemakaian bahan baku", "pengeluaran")
        db.add(TransaksiKeuangan(
            id_kategori=category.id_kategori,
            tanggal_transaksi=req.tanggal_produksi,
            nominal=material_cost,
            catatan=AUTOMATIC_MATERIAL_NOTE,
            sumber="otomatis",
            referensi_tipe="produksi",
            referensi_id=production.id_produksi,
        ))
    db.commit()
    return {"status": "berhasil",
            "message": f"Produksi {req.jumlah_loyang} loyang berhasil dicatat",
            "data": {"id_produksi": production.id_produksi,
                     "hasil_produksi": production.hasil_produksi,
                     "satuan_hasil": recipe.satuan_hasil}}


@app.post("/prediksi/produksi")
def create_prediction_production(req: PrediksiProductionCreate,
                                 db: Session = Depends(get_db)):
    plan = _prediction_total_plan(db)
    if not plan["stok_cukup"]:
        raise HTTPException(400, "Stok bahan baku tidak mencukupi")
    result = create_production(ProduksiCreate(
        id_resep=plan["id_resep"],
        tanggal_produksi=req.tanggal_produksi,
        jumlah_loyang=plan["jumlah_loyang"],
        jumlah_diminta=plan["jumlah_prediksi"],
        sumber_produksi="prediksi",
        catatan="Produksi otomatis dari total prediksi seluruh mitra",
    ), db)
    target = plan["tanggal_target"]
    db.query(PrediksiHarian).filter(
        PrediksiHarian.tanggal_target == target,
        PrediksiHarian.status_prediksi == "disetujui",
        PrediksiHarian.is_test.is_(False),
    ).update({PrediksiHarian.status_prediksi: "diproduksi"},
             synchronize_session=False)
    db.commit()
    return result


@app.post("/api/produksi/eksekusi")
def execute_batch_production(req: ProduksiEksekusiCreate,
                             db: Session = Depends(get_db)):
    """Eksekusi konsumsi stok batch FIFO dan produksi kekurangan."""
    try:
        # PRD 6.1: kebutuhan produksi adalah total jumlah_disetujui pada satu
        # tanggal target. Angka dari klien diselaraskan lebih dahulu supaya
        # prediksi pada target lain tidak salah ditandai sudah diproduksi.
        prediksi_target, total_disetujui = _approved_prediction_total(db)
        if (prediksi_target is not None and
                total_disetujui != req.prediksi_kebutuhan):
            raise HTTPException(
                409,
                "Total prediksi yang disetujui sekarang "
                f"{total_disetujui} produk, bukan {req.prediksi_kebutuhan}. "
                "Muat ulang halaman prediksi.",
            )
        now = datetime.now()
        batches = (db.query(StokBatch)
                   .filter(StokBatch.jumlah_sisa > 0,
                           StokBatch.tanggal_kadaluarsa > now)
                   .order_by(StokBatch.created_at.asc())
                   .with_for_update().all())
        total_stok_sistem = sum(batch.jumlah_sisa for batch in batches)
        stok_manual = (total_stok_sistem
                       if req.stok_manual_dipakai is None
                       else min(req.stok_manual_dipakai, total_stok_sistem))
        stok_rusak = max(0, total_stok_sistem - stok_manual)

        rusak_sisa = stok_rusak
        for batch in batches:
            if rusak_sisa <= 0:
                break
            jumlah = min(batch.jumlah_sisa, rusak_sisa)
            batch.jumlah_sisa -= jumlah
            rusak_sisa -= jumlah
            db.add(LogStokRusak(
                id_batch=batch.id_batch,
                jumlah_rusak=jumlah,
                alasan="Stok manual dinyatakan tidak layak digunakan",
            ))

        stok_layak = stok_manual
        dipakai_dari_batch = min(req.prediksi_kebutuhan, stok_layak)
        pakai_sisa = dipakai_dari_batch
        for batch in batches:
            if pakai_sisa <= 0:
                break
            jumlah = min(batch.jumlah_sisa, pakai_sisa)
            batch.jumlah_sisa -= jumlah
            pakai_sisa -= jumlah

        # Kapasitas loyang wajib mengikuti resep prediksi aktif supaya jumlah
        # loyang, sisa batch, dan hasil produksi konsisten (PRD 6.2).
        recipe = _prediction_recipe(db)
        kapasitas_per_loyang = _tray_capacity(recipe)
        kekurangan = max(0, req.prediksi_kebutuhan - dipakai_dari_batch)
        jumlah_loyang = ceil(kekurangan / kapasitas_per_loyang)
        sisa_stok_baru = (jumlah_loyang * kapasitas_per_loyang) - kekurangan
        batch_baru = None
        if jumlah_loyang > 0:
            batch_baru = StokBatch(
                jumlah_awal=sisa_stok_baru,
                jumlah_sisa=sisa_stok_baru,
                tanggal_kadaluarsa=now + timedelta(days=7),
            )
            db.add(batch_baru)

        # Catat produksi loyang baru: kurangi stok bahan baku sesuai resep dan
        # buat pengeluaran "Pemakaian bahan baku" di keuangan (PRD 7.2).
        materials = (_recipe_materials(db, recipe.id_resep, jumlah_loyang)
                     if recipe and jumlah_loyang > 0 else [])
        if jumlah_loyang > 0:
            if recipe is None:
                raise HTTPException(404, "Resep prediksi aktif belum tersedia")
            if not materials:
                raise HTTPException(400, "Bahan resep belum diisi")
            shortages = [item for item in materials if not item["cukup"]]
            if shortages:
                raise HTTPException(400, "Stok bahan baku tidak cukup: " + ", ".join(
                    f"{item['nama_bahan']} kurang "
                    f"{item['kekurangan']:g} {item['satuan']}"
                    for item in shortages))

        production = None
        finance_row = None
        material_cost = Decimal("0")
        if jumlah_loyang > 0 and recipe is not None:
            production = Produksi(
                id_resep=recipe.id_resep,
                sumber_produksi="prediksi",
                jumlah_diminta=req.prediksi_kebutuhan,
                tanggal_produksi=date.today(),
                jumlah_loyang=jumlah_loyang,
                hasil_produksi=recipe.hasil_per_loyang * jumlah_loyang,
                status="selesai",
                catatan=f"Produksi prediksi {jumlah_loyang} loyang "
                        f"{recipe.nama_resep}",
            )
            db.add(production)
            db.flush()
            for item in materials:
                used = item["kebutuhan"]
                before = item["stok_tersedia"]
                material_cost += Decimal(str(used)) * Decimal(
                    str(item["harga_per_satuan_resep"]))
                db.add(ProduksiDetail(
                    id_produksi=production.id_produksi,
                    id_bahan=item["id_bahan"], jumlah_pakai=used,
                    stok_sebelum=before, stok_sesudah=before - used))
                db.add(MutasiStok(
                    id_bahan=item["id_bahan"], tanggal_mutasi=now,
                    jenis_mutasi="pemakaian_produksi",
                    jumlah_masuk=0, jumlah_keluar=used,
                    harga_satuan=item["harga_per_satuan_resep"],
                    referensi_tipe="produksi",
                    referensi_id=production.id_produksi,
                    catatan=f"Produksi {jumlah_loyang} loyang "
                            f"{recipe.nama_resep}"))
            if material_cost > 0:
                category = _finance_category_auto(
                    db, "Pemakaian bahan baku", "pengeluaran")
                finance_row = TransaksiKeuangan(
                    id_kategori=category.id_kategori,
                    tanggal_transaksi=date.today(),
                    nominal=material_cost,
                    catatan=AUTOMATIC_MATERIAL_NOTE,
                    sumber="otomatis",
                    referensi_tipe="produksi",
                    referensi_id=production.id_produksi,
                )
                db.add(finance_row)

        # Tandai prediksi pada tanggal target yang benar sebagai diproduksi.
        if prediksi_target is not None:
            (db.query(PrediksiHarian)
               .filter(PrediksiHarian.tanggal_target == prediksi_target,
                       PrediksiHarian.status_prediksi == "disetujui",
                       PrediksiHarian.is_test.is_(False))
               .update({PrediksiHarian.status_prediksi: "diproduksi"},
                       synchronize_session=False))

        db.commit()
        if batch_baru is not None:
            db.refresh(batch_baru)
        if production is not None:
            db.refresh(production)
        if finance_row is not None:
            db.refresh(finance_row)
        return {
            "status": "berhasil",
            "message": "Eksekusi produksi berhasil",
            "data": {
                "prediksi_kebutuhan": req.prediksi_kebutuhan,
                "total_stok_sistem": total_stok_sistem,
                "stok_manual_dipakai": stok_manual,
                "stok_rusak": stok_rusak,
                "stok_layak": stok_layak,
                "stok_dipakai": dipakai_dari_batch,
                "kekurangan": kekurangan,
                "kapasitas_per_loyang": kapasitas_per_loyang,
                "jumlah_loyang": jumlah_loyang,
                "sisa_stok_baru": sisa_stok_baru,
                "id_batch_baru": batch_baru.id_batch
                    if batch_baru is not None else None,
                "tanggal_kadaluarsa_batch_baru": batch_baru.tanggal_kadaluarsa
                    if batch_baru is not None else None,
                "id_produksi": production.id_produksi
                    if production is not None else None,
                "hasil_produksi": production.hasil_produksi
                    if production is not None else 0,
                "pemakaian_bahan": [
                    {"id_bahan": item["id_bahan"],
                     "nama_bahan": item["nama_bahan"],
                     "jumlah_pakai": item["kebutuhan"],
                     "satuan": item["satuan"],
                     "harga_per_satuan_resep": item["harga_per_satuan_resep"],
                     "nilai": item["nilai"]}
                    for item in materials],
                "pengeluaran": {
                    "id_transaksi": finance_row.id_transaksi
                        if finance_row is not None else None,
                    "kategori": "Pemakaian bahan baku",
                    "jenis": "pengeluaran",
                    "nominal": float(material_cost),
                    "tanggal": date.today(),
                },
            },
        }
    except Exception:
        db.rollback()
        raise


@app.get("/stock-opname")
def list_opname(db: Session = Depends(get_db)):
    rows = (
        db.query(
            StockOpname,
            func.count(StockOpnameDetail.id_detail).label("jumlah_item"),
        )
        .outerjoin(
            StockOpnameDetail,
            StockOpnameDetail.id_opname == StockOpname.id_opname,
        )
        .group_by(StockOpname.id_opname)
        .order_by(StockOpname.tanggal_opname.desc(), StockOpname.id_opname.desc())
        .limit(50)
        .all()
    )
    return {"data": [
        {"id_opname": row.id_opname,
         "tanggal_opname": row.tanggal_opname,
         "catatan": row.catatan,
         "status": row.status,
         "jumlah_item": int(item_count)}
        for row, item_count in rows
    ]}


def _mutation_json(mutation: MutasiStok, material: BahanBaku):
    # Bila harga belum tercatat pada mutasi, pakai harga_per_satuan_resep bahan.
    price = float(mutation.harga_satuan or material.harga_per_satuan_resep or 0)
    quantity = float(mutation.jumlah_keluar or mutation.jumlah_masuk or 0)
    return {
        "id_mutasi": mutation.id_mutasi,
        "tanggal": mutation.tanggal_mutasi,
        "nama_bahan": material.nama_bahan,
        "jenis": mutation.jenis_mutasi,
        "jumlah_masuk": float(mutation.jumlah_masuk or 0),
        "jumlah_keluar": float(mutation.jumlah_keluar or 0),
        "satuan": material.satuan_resep,
        "harga_satuan": price,
        "harga_per_satuan_resep": price,
        "nilai": float(quantity * price),
        "catatan": mutation.catatan,
    }


@app.get("/mutasi-stok/riwayat")
def stock_mutation_history(
    tanggal_mulai: date | None = None,
    tanggal_selesai: date | None = None,
    db: Session = Depends(get_db),
):
    query = (db.query(MutasiStok, BahanBaku)
             .join(BahanBaku, BahanBaku.id_bahan == MutasiStok.id_bahan))
    if tanggal_mulai is not None:
        query = query.filter(MutasiStok.tanggal_mutasi >= tanggal_mulai)
    if tanggal_selesai is not None:
        query = query.filter(MutasiStok.tanggal_mutasi <
                             datetime.combine(tanggal_selesai, time.max))
    rows = query.order_by(MutasiStok.tanggal_mutasi.desc(),
                          MutasiStok.id_mutasi.desc()).limit(500).all()
    return {"data": [_mutation_json(mutation, material)
                     for mutation, material in rows]}


@app.get("/stock-opname/{id_opname}")
def detail_opname(id_opname: int, db: Session = Depends(get_db)):
    row = db.query(StockOpname).filter(
        StockOpname.id_opname == id_opname).first()
    if not row:
        raise HTTPException(404, "Stock opname tidak ditemukan")
    details = (
        db.query(StockOpnameDetail, BahanBaku)
        .join(BahanBaku, BahanBaku.id_bahan == StockOpnameDetail.id_bahan)
        .filter(StockOpnameDetail.id_opname == id_opname)
        .order_by(BahanBaku.nama_bahan)
        .all()
    )
    return {"data": {
        "id_opname": row.id_opname,
        "tanggal_opname": row.tanggal_opname,
        "catatan": row.catatan,
        "status": row.status,
        "items": [
            {"id_detail": detail.id_detail,
             "id_bahan": detail.id_bahan,
             "nama_bahan": bahan.nama_bahan,
             "satuan": bahan.satuan_resep,
             "stok_sistem": float(detail.stok_sistem),
             "stok_aktual": float(detail.stok_aktual),
             "selisih": float(detail.stok_aktual - detail.stok_sistem),
             "catatan": detail.catatan}
            for detail, bahan in details
        ],
    }}


@app.post("/stock-opname")
def save_opname(req: StockOpnameCreate, db: Session = Depends(get_db)):
    current = {x.id_bahan: (x, float(s or 0))
               for x, s in _stock_query(db).all()}
    if len({x.id_bahan for x in req.items}) != len(req.items):
        raise HTTPException(400, "Bahan baku tidak boleh duplikat")
    if any(x.id_bahan not in current for x in req.items):
        raise HTTPException(404, "Salah satu bahan baku tidak ditemukan")
    opname = StockOpname(tanggal_opname=req.tanggal_opname,
                         catatan=req.catatan, status="selesai")
    db.add(opname)
    db.flush()
    for item in req.items:
        system_stock = current[item.id_bahan][1]
        actual = float(item.stok_aktual)
        db.add(StockOpnameDetail(id_opname=opname.id_opname,
                                 id_bahan=item.id_bahan,
                                 stok_sistem=system_stock,
                                 stok_aktual=actual, catatan=item.catatan))
        difference = actual - system_stock
        if difference:
            db.add(MutasiStok(
                id_bahan=item.id_bahan, jenis_mutasi="penyesuaian_opname",
                jumlah_masuk=max(difference, 0), jumlah_keluar=max(-difference, 0),
                referensi_tipe="stock_opname", referensi_id=opname.id_opname,
                catatan=item.catatan or "Penyesuaian stock opname"))
    db.commit()
    return {"status": "berhasil", "message": "Stock opname berhasil disimpan",
            "id_opname": opname.id_opname}


@app.put("/stock-opname/{id_opname}")
def update_opname(id_opname: int, req: StockOpnameCreate,
                  db: Session = Depends(get_db)):
    opname = db.query(StockOpname).filter(
        StockOpname.id_opname == id_opname).first()
    if not opname:
        raise HTTPException(404, "Stock opname tidak ditemukan")
    if len({item.id_bahan for item in req.items}) != len(req.items):
        raise HTTPException(400, "Bahan baku tidak boleh duplikat")

    # Batalkan penyesuaian lama sebelum menghitung ulang stok sistem.
    (db.query(MutasiStok).filter(
        MutasiStok.referensi_tipe == "stock_opname",
        MutasiStok.referensi_id == id_opname,
    ).delete(synchronize_session=False))
    (db.query(StockOpnameDetail).filter(
        StockOpnameDetail.id_opname == id_opname,
    ).delete(synchronize_session=False))
    db.flush()

    current = {item.id_bahan: float(stock or 0)
               for item, stock in _stock_query(db).all()}
    if any(item.id_bahan not in current for item in req.items):
        db.rollback()
        raise HTTPException(404, "Salah satu bahan baku tidak ditemukan")

    opname.tanggal_opname = req.tanggal_opname
    opname.catatan = req.catatan
    opname.status = "selesai"
    for item in req.items:
        system_stock = current[item.id_bahan]
        actual = float(item.stok_aktual)
        db.add(StockOpnameDetail(
            id_opname=id_opname, id_bahan=item.id_bahan,
            stok_sistem=system_stock, stok_aktual=actual,
            catatan=item.catatan))
        difference = actual - system_stock
        if difference:
            db.add(MutasiStok(
                id_bahan=item.id_bahan, jenis_mutasi="penyesuaian_opname",
                jumlah_masuk=max(difference, 0),
                jumlah_keluar=max(-difference, 0),
                referensi_tipe="stock_opname", referensi_id=id_opname,
                catatan=item.catatan or "Penyesuaian stock opname"))
    db.commit()
    return {"status": "berhasil", "message": "Stock opname berhasil diperbarui",
            "id_opname": id_opname}


@app.delete("/stock-opname/{id_opname}")
def delete_opname(id_opname: int, db: Session = Depends(get_db)):
    opname = db.query(StockOpname).filter(
        StockOpname.id_opname == id_opname).first()
    if not opname:
        raise HTTPException(404, "Stock opname tidak ditemukan")
    try:
        (db.query(MutasiStok).filter(
            MutasiStok.referensi_tipe == "stock_opname",
            MutasiStok.referensi_id == id_opname,
        ).delete(synchronize_session=False))
        # Detail harus dihapus lebih dahulu, kalau tidak foreign key
        # stock_opname_detail akan menghalangi penghapusan header.
        (db.query(StockOpnameDetail).filter(
            StockOpnameDetail.id_opname == id_opname,
        ).delete(synchronize_session=False))
        db.delete(opname)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"status": "berhasil",
            "message": "Stock opname dan penyesuaian stoknya berhasil dihapus"}


@app.get("/kategori-keuangan")
def finance_categories(db: Session = Depends(get_db)):
    rows = (db.query(KategoriKeuangan).filter(KategoriKeuangan.status == "aktif")
            .order_by(KategoriKeuangan.jenis, KategoriKeuangan.nama_kategori).all())
    return {"data": [{"id_kategori": x.id_kategori,
                      "nama_kategori": x.nama_kategori, "jenis": x.jenis}
                     for x in rows]}


@app.get("/transaksi-keuangan")
def list_finance(
    jenis: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Riwayat transaksi keuangan (masuk dan keluar), terbaru lebih dahulu."""
    query = (db.query(TransaksiKeuangan, KategoriKeuangan)
             .join(KategoriKeuangan))
    if jenis in ("pemasukan", "pengeluaran"):
        query = query.filter(KategoriKeuangan.jenis == jenis)
    rows = query.order_by(TransaksiKeuangan.tanggal_transaksi.desc(),
                          TransaksiKeuangan.id_transaksi.desc()).limit(
                              limit).all()
    masuk = sum(float(t.nominal) for t, k in rows if k.jenis == "pemasukan")
    keluar = sum(float(t.nominal) for t, k in rows if k.jenis == "pengeluaran")
    return {"jumlah": len(rows), "total_masuk": masuk, "total_keluar": keluar,
            "data": [{"id_transaksi": t.id_transaksi,
                      "id_kategori": t.id_kategori,
                      "tanggal_transaksi": t.tanggal_transaksi,
                      "kategori": k.nama_kategori, "jenis": k.jenis,
                      "nominal": float(t.nominal), "catatan": t.catatan,
                      "sumber": t.sumber}
                     for t, k in rows]}


@app.post("/transaksi-keuangan")
def save_finance(req: TransaksiKeuanganCreate, db: Session = Depends(get_db)):
    if not db.query(KategoriKeuangan).filter(
            KategoriKeuangan.id_kategori == req.id_kategori,
            KategoriKeuangan.status == "aktif").first():
        raise HTTPException(404, "Kategori aktif tidak ditemukan")
    row = TransaksiKeuangan(**req.model_dump(), sumber="manual")
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "berhasil", "message": "Transaksi berhasil disimpan",
            "id_transaksi": row.id_transaksi}


@app.put("/transaksi-keuangan/{id_transaksi}")
def update_finance(id_transaksi: int, req: TransaksiKeuanganUpdate,
                   db: Session = Depends(get_db)):
    row = db.query(TransaksiKeuangan).filter(
        TransaksiKeuangan.id_transaksi == id_transaksi).first()
    if not row:
        raise HTTPException(404, "Transaksi tidak ditemukan")
    if not db.query(KategoriKeuangan).filter(
            KategoriKeuangan.id_kategori == req.id_kategori,
            KategoriKeuangan.status == "aktif").first():
        raise HTTPException(404, "Kategori aktif tidak ditemukan")
    row.id_kategori = req.id_kategori
    row.tanggal_transaksi = req.tanggal_transaksi
    row.nominal = req.nominal
    row.catatan = req.catatan
    db.commit()
    return {"status": "berhasil", "message": "Transaksi berhasil diperbarui"}


@app.delete("/transaksi-keuangan/{id_transaksi}")
def delete_finance(id_transaksi: int, db: Session = Depends(get_db)):
    row = db.query(TransaksiKeuangan).filter(
        TransaksiKeuangan.id_transaksi == id_transaksi).first()
    if not row:
        raise HTTPException(404, "Transaksi tidak ditemukan")
    db.delete(row)
    db.commit()
    return {"status": "berhasil", "message": "Transaksi berhasil dihapus"}


@app.get("/keuangan")
def get_finance(tanggal: date = Query(default_factory=date.today),
                tanggal_mulai: date | None = None,
                tanggal_selesai: date | None = None,
                db: Session = Depends(get_db)):
    start = tanggal_mulai or tanggal
    end = tanggal_selesai or tanggal
    if end < start:
        start, end = end, start
    if (end - start).days > 366:
        raise HTTPException(400, "Rentang tanggal maksimal 1 tahun")
    if tanggal_mulai is None and tanggal_selesai is None:
        _sync_daily_finance(db, tanggal)
        db.commit()
    rows = (db.query(TransaksiKeuangan, KategoriKeuangan)
            .join(KategoriKeuangan)
            .filter(TransaksiKeuangan.tanggal_transaksi >= start,
                    TransaksiKeuangan.tanggal_transaksi <= end)
            .order_by(TransaksiKeuangan.id_transaksi.desc()).all())
    income = sum(float(t.nominal) for t, k in rows if k.jenis == "pemasukan")
    expense = sum(float(t.nominal) for t, k in rows if k.jenis == "pengeluaran")
    profit = income - expense
    # Rincian pemasukan & pengeluaran per tanggal sepanjang periode filter.
    # Dipakai grafik: batang untuk mode harian dan garis pemasukan/pengeluaran
    # untuk mode mingguan, bulanan, dan rentang tanggal.
    daily_rows = (db.query(
        TransaksiKeuangan.tanggal_transaksi,
        func.sum(case((KategoriKeuangan.jenis == "pemasukan",
                       TransaksiKeuangan.nominal),
                      else_=0)).label("pemasukan"),
        func.sum(case((KategoriKeuangan.jenis == "pengeluaran",
                       TransaksiKeuangan.nominal),
                      else_=0)).label("pengeluaran"))
        .join(KategoriKeuangan)
        .filter(TransaksiKeuangan.tanggal_transaksi >= start,
                TransaksiKeuangan.tanggal_transaksi <= end)
        .group_by(TransaksiKeuangan.tanggal_transaksi).all())
    daily_map = {x.tanggal_transaksi: x for x in daily_rows}
    trend = []
    current = start
    while current <= end:
        row = daily_map.get(current)
        income_day = float(row.pemasukan or 0) if row else 0
        expense_day = float(row.pengeluaran or 0) if row else 0
        trend.append({"tanggal": current,
                      "pemasukan": income_day,
                      "pengeluaran": expense_day,
                      "laba": income_day - expense_day})
        current += timedelta(days=1)
    return {"tanggal": tanggal,
            "tanggal_mulai": start, "tanggal_selesai": end,
            "total_pemasukan": income,
            "total_pengeluaran": expense, "laba_bersih": profit,
            "margin_laba": round(profit / income * 100, 2) if income else 0,
            "trend": trend,
            "transaksi": [{"id_transaksi": t.id_transaksi,
                           "id_kategori": t.id_kategori,
                           "tanggal_transaksi": t.tanggal_transaksi,
                           "kategori": k.nama_kategori, "jenis": k.jenis,
                           "nominal": float(t.nominal), "catatan": t.catatan,
                           "sumber": t.sumber}
                          for t, k in rows]}
