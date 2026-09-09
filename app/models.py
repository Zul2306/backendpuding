from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Boolean,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class Mitra(Base):
    __tablename__ = "mitra"

    id_mitra = Column(Integer, primary_key=True, index=True)
    nama_mitra = Column(String(100), unique=True, nullable=False)
    status = Column(Enum("aktif", "nonaktif"), default="aktif")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class DataHarian(Base):
    __tablename__ = "data_harian"

    id_data = Column(Integer, primary_key=True, index=True)
    id_mitra = Column(Integer, ForeignKey("mitra.id_mitra"), nullable=False)
    tanggal = Column(Date, nullable=False)
    jumlah_suplai = Column(Integer, default=0)
    jumlah_return = Column(Integer, default=0)
    jumlah_terjual = Column(Integer, default=0)
    mitra_tutup = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ModelMitra(Base):
    __tablename__ = "model_mitra"

    id_model = Column(Integer, primary_key=True, index=True)
    id_mitra = Column(Integer, ForeignKey("mitra.id_mitra"), nullable=False)
    jenis_dataset = Column(String(100), nullable=False)
    nama_file_model = Column(String(255), nullable=False)
    path_model = Column(String(255), nullable=False)
    rasio_split = Column(String(10))
    jumlah_data = Column(Integer)
    jumlah_data_latih = Column(Integer)
    jumlah_data_uji = Column(Integer)
    jumlah_fitur = Column(Integer)
    mae = Column(Float)
    rmse = Column(Float)
    r2 = Column(Float)
    tanggal_training = Column(DateTime, server_default=func.now())
    status_model = Column(Enum("aktif", "nonaktif"), default="aktif")


class PrediksiHarian(Base):
    __tablename__ = "prediksi_harian"

    id_prediksi = Column(Integer, primary_key=True, index=True)
    id_mitra = Column(Integer, ForeignKey("mitra.id_mitra"), nullable=False)
    id_model = Column(Integer, ForeignKey("model_mitra.id_model"), nullable=False)
    tanggal_prediksi = Column(Date, nullable=False)
    tanggal_target = Column(Date, nullable=False)
    hasil_prediksi = Column(Float, nullable=False)
    hasil_prediksi_bulat = Column(Integer, nullable=False)
    jumlah_disetujui = Column(Integer, nullable=False)
    status_prediksi = Column(
        Enum("disetujui", "diproduksi", "dibatalkan"),
        nullable=False, default="disetujui")
    dikonfirmasi_at = Column(DateTime)
    is_test = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class BahanBaku(Base):
    __tablename__ = "bahan_baku"

    id_bahan = Column(Integer, primary_key=True)
    nama_bahan = Column(String(100), unique=True, nullable=False)
    satuan = Column(String(20), nullable=False)
    satuan_pembelian = Column(String(30))
    isi_per_pembelian = Column(Numeric(12, 3), nullable=False, default=1)
    label_pembelian = Column(String(50))
    stok_minimum = Column(Numeric(12, 2), nullable=False, default=0)
    harga_per_satuan = Column(Numeric(15, 2), nullable=False, default=0)
    status = Column(Enum("aktif", "nonaktif"), nullable=False, default="aktif")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class MutasiStok(Base):
    __tablename__ = "mutasi_stok"

    id_mutasi = Column(BigInteger, primary_key=True)
    id_bahan = Column(Integer, ForeignKey("bahan_baku.id_bahan"), nullable=False)
    tanggal_mutasi = Column(DateTime, nullable=False, server_default=func.now())
    jenis_mutasi = Column(String(50), nullable=False)
    jumlah_masuk = Column(Numeric(12, 3), nullable=False, default=0)
    jumlah_keluar = Column(Numeric(12, 3), nullable=False, default=0)
    harga_satuan = Column(Numeric(15, 2))
    referensi_tipe = Column(String(50))
    referensi_id = Column(BigInteger)
    catatan = Column(String(255))
    created_at = Column(DateTime, server_default=func.now())


class StockOpname(Base):
    __tablename__ = "stock_opname"

    id_opname = Column(Integer, primary_key=True)
    tanggal_opname = Column(Date, nullable=False)
    catatan = Column(Text)
    status = Column(Enum("draft", "selesai"), nullable=False, default="draft")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class StockOpnameDetail(Base):
    __tablename__ = "stock_opname_detail"

    id_detail = Column(BigInteger, primary_key=True)
    id_opname = Column(Integer, ForeignKey("stock_opname.id_opname"), nullable=False)
    id_bahan = Column(Integer, ForeignKey("bahan_baku.id_bahan"), nullable=False)
    stok_sistem = Column(Numeric(12, 3), nullable=False, default=0)
    stok_aktual = Column(Numeric(12, 3), nullable=False, default=0)
    catatan = Column(String(255))
    created_at = Column(DateTime, server_default=func.now())


class Produk(Base):
    __tablename__ = "produk"

    id_produk = Column(Integer, primary_key=True)
    nama_produk = Column(String(100), unique=True, nullable=False)
    metode_perencanaan = Column(Enum("prediksi", "manual"), nullable=False)
    disuplai_ke_mitra = Column(Boolean, nullable=False, default=False)
    status = Column(Enum("aktif", "nonaktif"), nullable=False, default="aktif")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Resep(Base):
    __tablename__ = "resep"

    id_resep = Column(Integer, primary_key=True)
    id_produk = Column(Integer, ForeignKey("produk.id_produk"), nullable=False)
    nama_resep = Column(String(100), nullable=False)
    hasil_per_loyang = Column(Integer, nullable=False, default=33)
    satuan_hasil = Column(String(20), nullable=False, default="potong")
    status = Column(Enum("aktif", "nonaktif"), nullable=False, default="aktif")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ResepBahan(Base):
    __tablename__ = "resep_bahan"

    id_resep_bahan = Column(Integer, primary_key=True)
    id_resep = Column(Integer, ForeignKey("resep.id_resep"), nullable=False)
    id_bahan = Column(Integer, ForeignKey("bahan_baku.id_bahan"), nullable=False)
    kebutuhan_per_loyang = Column(Numeric(12, 3), nullable=False)
    status = Column(Enum("aktif", "nonaktif"), nullable=False, default="aktif")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Produksi(Base):
    __tablename__ = "produksi"

    id_produksi = Column(Integer, primary_key=True)
    id_resep = Column(Integer, ForeignKey("resep.id_resep"), nullable=False)
    sumber_produksi = Column(Enum("prediksi", "manual"), nullable=False,
                             default="manual")
    jumlah_diminta = Column(Integer)
    id_prediksi = Column(Integer, ForeignKey("prediksi_harian.id_prediksi"))
    tanggal_produksi = Column(Date, nullable=False)
    jumlah_loyang = Column(Integer, nullable=False)
    hasil_produksi = Column(Integer, nullable=False)
    status = Column(Enum("selesai", "dibatalkan"), nullable=False, default="selesai")
    catatan = Column(String(255))
    created_at = Column(DateTime, server_default=func.now())


class ProduksiDetail(Base):
    __tablename__ = "produksi_detail"

    id_detail = Column(BigInteger, primary_key=True)
    id_produksi = Column(Integer, ForeignKey("produksi.id_produksi"), nullable=False)
    id_bahan = Column(Integer, ForeignKey("bahan_baku.id_bahan"), nullable=False)
    jumlah_pakai = Column(Numeric(12, 3), nullable=False)
    stok_sebelum = Column(Numeric(12, 3), nullable=False)
    stok_sesudah = Column(Numeric(12, 3), nullable=False)


class KategoriKeuangan(Base):
    __tablename__ = "kategori_keuangan"

    id_kategori = Column(Integer, primary_key=True)
    nama_kategori = Column(String(100), nullable=False)
    jenis = Column(Enum("pemasukan", "pengeluaran"), nullable=False)
    status = Column(Enum("aktif", "nonaktif"), nullable=False, default="aktif")
    created_at = Column(DateTime, server_default=func.now())


class TransaksiKeuangan(Base):
    __tablename__ = "transaksi_keuangan"

    id_transaksi = Column(BigInteger, primary_key=True)
    id_kategori = Column(Integer, ForeignKey("kategori_keuangan.id_kategori"), nullable=False)
    id_mitra = Column(Integer, ForeignKey("mitra.id_mitra"))
    tanggal_transaksi = Column(Date, nullable=False)
    nominal = Column(Numeric(15, 2), nullable=False)
    deskripsi = Column(String(255))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
