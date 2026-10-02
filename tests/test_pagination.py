import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi import HTTPException
from app.database import Base
from app.models import BahanBaku, MutasiStok, KategoriKeuangan, TransaksiKeuangan
from app.main import stock_mutation_history, list_finance


class PaginationTest(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db=Session(self.engine)
        self.day=date(2026,10,1)
        self.db.add(BahanBaku(id_bahan=1,nama_bahan='Gula',satuan_resep='gram',harga_per_satuan_resep=100,status='aktif'))
        for id_, name, kind in [(1,'Biaya manual','pengeluaran'),(2,'Pemakaian bahan baku','pengeluaran'),(3,'Return puding','pengeluaran'),(4,'Penjualan puding','pemasukan')]:
            self.db.add(KategoriKeuangan(id_kategori=id_,nama_kategori=name,jenis=kind,sumber='otomatis',status='aktif'))
        self.db.flush()
        for id_ in range(1,46):
            self.db.add(MutasiStok(id_mutasi=id_,id_bahan=1,tanggal_mutasi=datetime(2026,10,1,12),jenis_mutasi='pembelian',jumlah_masuk=1,jumlah_keluar=0,harga_satuan=100))
            self.db.add(TransaksiKeuangan(id_transaksi=id_,id_kategori=1,tanggal_transaksi=self.day,nominal=Decimal('100'),sumber='manual'))
        self.db.add_all([
            TransaksiKeuangan(id_transaksi=46,id_kategori=2,tanggal_transaksi=self.day,nominal=500,sumber='otomatis'),
            TransaksiKeuangan(id_transaksi=47,id_kategori=3,tanggal_transaksi=self.day,nominal=300,sumber='otomatis'),
            TransaksiKeuangan(id_transaksi=48,id_kategori=4,tanggal_transaksi=self.day-timedelta(days=1),nominal=1000,sumber='otomatis')])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_material_pages_are_disjoint_and_summary_is_whole_filter(self):
        pages=[stock_mutation_history(page=p,limit=20,db=self.db) for p in [1,2,3]]
        self.assertEqual([len(p['data']) for p in pages],[20,20,5])
        ids=[r['id_mutasi'] for p in pages for r in p['data']]
        self.assertEqual(ids,list(range(45,0,-1)))
        for page in pages:
            self.assertEqual(page['pagination']['total'],45)
            self.assertEqual(page['summary']['nilai_masuk'],4500)
        self.assertFalse(pages[-1]['pagination']['has_next'])

    def test_material_filter_empty_and_end_of_day_inclusive(self):
        empty=stock_mutation_history(tanggal_mulai=self.day+timedelta(days=1),page=1,limit=20,db=self.db)
        self.assertEqual(empty['data'],[])
        self.assertEqual(empty['summary']['nilai_masuk'],0)
        last=self.db.get(MutasiStok,45)
        last.tanggal_mutasi=datetime(2026,10,1,23,59,59,999999)
        self.db.commit()
        result=stock_mutation_history(tanggal_mulai=self.day,tanggal_selesai=self.day,page=1,limit=20,db=self.db)
        self.assertEqual(result['pagination']['total'],45)
        self.assertEqual(result['data'][0]['id_mutasi'],45)

    def test_finance_pages_keep_global_cash_totals(self):
        pages=[list_finance(limit=20,page=p,db=self.db) for p in [1,2,3]]
        ids=[r['id_transaksi'] for p in pages for r in p['data']]
        self.assertEqual(ids,list(range(47,0,-1))+[48])
        self.assertEqual([len(p['data']) for p in pages],[20,20,8])
        for page in pages:
            self.assertEqual(page['total_keluar'],4500)
            self.assertEqual(page['total_masuk'],1000)
            self.assertEqual(page['pagination']['total'],48)

    def test_date_and_material_exclusion_applied_before_pagination(self):
        result=list_finance(limit=20,page=1,tanggal=self.day,exclude_material_usage=True,db=self.db)
        self.assertEqual(result['pagination']['total'],46)
        self.assertEqual(result['data'][0]['id_transaksi'],47)
        self.assertTrue(all(r['kategori']!='Pemakaian bahan baku' for r in result['data']))
        self.assertEqual(result['total_masuk'],0)
        self.assertEqual(result['total_keluar'],4500)
        income=list_finance(jenis='pemasukan',limit=20,page=1,db=self.db)
        self.assertEqual(income['pagination']['total'],1)

    def test_invalid_page_limit_and_date_range_rejected(self):
        for handler in [stock_mutation_history,list_finance]:
            for args in [dict(page=0,limit=20),dict(page=1,limit=0),dict(page=1,limit=101)]:
                with self.assertRaises(HTTPException) as caught:
                    handler(db=self.db,**args)
                self.assertEqual(caught.exception.status_code,422)
        with self.assertRaises(HTTPException):
            stock_mutation_history(tanggal_mulai=self.day+timedelta(days=1),tanggal_selesai=self.day,db=self.db)

    def test_page_beyond_end_is_empty_and_does_not_modify_data(self):
        result=stock_mutation_history(page=10,limit=20,db=self.db)
        self.assertEqual(result['data'],[])
        self.assertEqual(result['pagination']['total'],45)
        self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)
