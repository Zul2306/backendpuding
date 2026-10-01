import unittest
from datetime import date
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.models import BahanBaku, MutasiStok, KategoriKeuangan, TransaksiKeuangan
from app.main import get_finance, list_finance


class CashFinanceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.day = date(2026, 10, 1)
        self.transaction_id = 0
        categories = [(1, "Pembelian bahan baku", "pengeluaran"),
                      (2, "Pemakaian bahan baku", "pengeluaran"),
                      (3, "Penjualan puding", "pemasukan"),
                      (4, "Return puding", "pengeluaran"),
                      (5, "Listrik", "pengeluaran")]
        for id_, name, kind in categories:
            self.db.add(KategoriKeuangan(id_kategori=id_, nama_kategori=name,
                                        jenis=kind, sumber="otomatis", status="aktif"))
        self.db.add(BahanBaku(id_bahan=1,nama_bahan="Bahan",satuan_resep="gram",
                             rasio_konversi=1,harga_beli=1000,
                             harga_per_satuan_resep=1000,stok_minimum=0,status="aktif"))
        self.db.flush()
        for id_, (kind, masuk, keluar) in enumerate([("pembelian",1000,0),("pemakaian_produksi",0,500)],1):
            self.db.add(MutasiStok(id_mutasi=id_,id_bahan=1,tanggal_mutasi=self.day,jenis_mutasi=kind,
                                  jumlah_masuk=masuk,jumlah_keluar=keluar))
        self.add_transaction(1, 1000000)
        self.add_transaction(2, 500000)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_transaction(self, category, amount, source="otomatis"):
        self.transaction_id += 1
        self.db.add(TransaksiKeuangan(id_transaksi=self.transaction_id,id_kategori=category,tanggal_transaksi=self.day,
                                     nominal=Decimal(amount),sumber=source))

    def report(self):
        return get_finance(tanggal=self.day,tanggal_mulai=self.day,
                           tanggal_selesai=self.day,db=self.db)

    def test_purchase_and_usage_do_not_double_cash_expense(self):
        result = self.report()
        self.assertEqual(result["total_pengeluaran"],1000000)
        self.assertEqual(result["bahan_terpakai"],500000)
        self.assertEqual(result["nilai_persediaan_bahan"],500000)
        self.assertEqual(result["arus_kas_bersih"],-1000000)
        self.assertEqual(result["trend"][0]["pengeluaran"],1000000)
        rows = list_finance(jenis=None,limit=100,db=self.db)
        self.assertEqual(rows["total_keluar"],1000000)
        usage = next(t for t in result["transaksi"] if t["kategori"] == "Pemakaian bahan baku")
        self.assertFalse(usage["mempengaruhi_kas"])

    def test_return_does_not_reduce_net_sales_again(self):
        self.add_transaction(3,300000)
        self.add_transaction(4,30000)
        self.add_transaction(5,50000,"manual")
        self.db.commit()
        result = self.report()
        self.assertEqual(result["total_pemasukan"],300000)
        self.assertEqual(result["total_pengeluaran"],1050000)
        self.assertEqual(result["nilai_return"],30000)
        self.assertEqual(result["arus_kas_bersih"],-750000)

    def test_usage_only_day_has_no_cash_expense(self):
        purchase = self.db.query(TransaksiKeuangan).filter_by(id_kategori=1).one()
        purchase.tanggal_transaksi = date(2026,9,30)
        self.db.commit()
        result = self.report()
        self.assertEqual(result["total_pengeluaran"],0)
        self.assertEqual(result["bahan_terpakai"],500000)
        self.assertEqual(result["trend"][0]["pengeluaran"],0)


if __name__ == "__main__":
    unittest.main()
