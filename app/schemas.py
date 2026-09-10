from datetime import date
from decimal import Decimal
from typing import Literal, Optional
from pydantic import BaseModel, Field


class DataHarianCreate(BaseModel):
    id_mitra: int
    tanggal: date
    jumlah_suplai: int = Field(ge=0)
    jumlah_return: int = Field(ge=0)
    mitra_tutup: bool = False


class DataHarianBatchItem(BaseModel):
    id_mitra: int
    jumlah_suplai: int = Field(ge=0)
    jumlah_return: int = Field(ge=0)
    mitra_tutup: bool = False


class DataHarianBatchCreate(BaseModel):
    tanggal: date
    items: list[DataHarianBatchItem] = Field(min_length=1)


class DataHarianResponse(BaseModel):
    id_data: int
    id_mitra: int
    tanggal: date
    jumlah_suplai: int
    jumlah_return: int
    jumlah_terjual: int
    mitra_tutup: bool

    class Config:
        from_attributes = True


class PrediksiConfirm(BaseModel):
    id_mitra: int
    jumlah_disetujui: int = Field(ge=0)


class PrediksiBatchConfirmItem(BaseModel):
    id_mitra: int
    jumlah_disetujui: int = Field(ge=0)


class PrediksiBatchConfirm(BaseModel):
    items: list[PrediksiBatchConfirmItem] = Field(min_length=1)


class StockOpnameItemCreate(BaseModel):
    id_bahan: int
    stok_aktual: Decimal = Field(ge=0)
    catatan: Optional[str] = None


class StockOpnameCreate(BaseModel):
    tanggal_opname: date
    catatan: Optional[str] = None
    items: list[StockOpnameItemCreate] = Field(min_length=1)


class TransaksiKeuanganCreate(BaseModel):
    id_kategori: int
    id_mitra: Optional[int] = None
    tanggal_transaksi: date
    nominal: Decimal = Field(gt=0)
    deskripsi: Optional[str] = None


class TransaksiKeuanganUpdate(TransaksiKeuanganCreate):
    pass


class MitraCreate(BaseModel):
    nama_mitra: str = Field(min_length=2, max_length=100)
    status: Literal["aktif", "nonaktif"] = "aktif"


class MitraUpdate(BaseModel):
    nama_mitra: str = Field(min_length=2, max_length=100)
    status: Literal["aktif", "nonaktif"]


class BahanBakuCreate(BaseModel):
    nama_bahan: str = Field(min_length=2, max_length=100)
    satuan: str = Field(min_length=1, max_length=20)
    satuan_pembelian: Optional[str] = Field(default=None, max_length=30)
    isi_per_pembelian: Decimal = Field(default=1, gt=0)
    label_pembelian: Optional[str] = Field(default=None, max_length=50)
    stok_minimum: Decimal = Field(ge=0)
    harga_per_satuan: Decimal = Field(ge=0)
    status: Literal["aktif", "nonaktif"] = "aktif"


class BahanBakuUpdate(BahanBakuCreate):
    pass


class PembelianBahanBakuCreate(BaseModel):
    id_bahan: int
    tanggal_pembelian: date
    jumlah_pembelian: Decimal = Field(gt=0)
    harga_total: Decimal = Field(gt=0)
    catatan: Optional[str] = None


class ProduksiCreate(BaseModel):
    id_resep: int
    tanggal_produksi: date
    jumlah_loyang: int = Field(ge=1)
    catatan: Optional[str] = None
    sumber_produksi: Literal["prediksi", "manual"] = "manual"
    jumlah_diminta: Optional[int] = Field(default=None, ge=1)
    id_prediksi: Optional[int] = None


class ResepCreate(BaseModel):
    id_produk: int
    nama_resep: str = Field(min_length=2, max_length=100)
    hasil_per_loyang: int = Field(ge=1)
    satuan_hasil: str = Field(min_length=1, max_length=20)
    status: Literal["aktif", "nonaktif"] = "aktif"


class ResepUpdate(ResepCreate):
    pass


class ResepBahanCreate(BaseModel):
    id_bahan: int
    kebutuhan_per_loyang: Decimal = Field(gt=0)
    status: Literal["aktif", "nonaktif"] = "aktif"


class ResepBahanUpdate(ResepBahanCreate):
    pass
