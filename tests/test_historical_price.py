import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from fastapi import HTTPException
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from app.database import Base
from app.models import (Produk, Resep, Mitra, DataHarian, KategoriKeuangan, TransaksiKeuangan)
from app.schemas import (DataHarianCreate, DataHarianBatchCreate, ResepHargaUpdate)
from app.main import (save_daily, save_daily_batch, update_recipe_price, get_finance, _sync_daily_finance, daily_draft)

@compiles(BigInteger, 'sqlite')
def sqlite_bigint(type_, compiler, **kw):
    return 'INTEGER'

class HistoricalPriceTest(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db=Session(self.engine,autoflush=False)
        self.day=date.today()-timedelta(days=1)
        self.db.add(Produk(id_produk=1,nama_produk='Original',metode_perencanaan='prediksi',status='aktif'))
        self.db.add_all([Mitra(id_mitra=1,nama_mitra='Mitra 1',status='aktif'),Mitra(id_mitra=2,nama_mitra='Mitra 2',status='aktif')])
        self.db.add_all([
            KategoriKeuangan(id_kategori=1,nama_kategori='Penjualan puding',jenis='pemasukan',sumber='otomatis',status='aktif'),
            KategoriKeuangan(id_kategori=2,nama_kategori='Return puding',jenis='pengeluaran',sumber='otomatis',status='aktif')])
        self.db.flush()
        self.db.add(Resep(id_resep=1,id_produk=1,nama_resep='Puding Marie',metode_perencanaan='prediksi',harga_jual_per_potong=3000,hasil_per_loyang=33,status='aktif'))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def save(self,partner=1,day=None,supply=10,returned=2):
        return save_daily(DataHarianCreate(id_mitra=partner,tanggal=day or self.day,jumlah_suplai=supply,jumlah_return=returned),self.db)

    def totals(self):
        return {r.id_kategori:r.nominal for r in self.db.query(TransaksiKeuangan).all()}

    def test_draft_read_does_not_create_holidays(self):
        self.save(day=date.today()-timedelta(days=3))
        count = self.db.query(DataHarian).count()
        result = daily_draft(date.today(), self.db)
        self.assertEqual(result['tanggal_belum_diinput'], self.day)
        self.assertEqual(self.db.query(DataHarian).count(), count)
        self.assertFalse(self.db.new)
        self.assertTrue(all(not r['sudah_tersimpan'] for r in result['data']))
        self.assertTrue(all(not r['mitra_tutup'] for r in result['data']))

    def test_existing_yesterday_does_not_trigger_missing_report(self):
        self.save()
        self.assertIsNone(daily_draft(date.today(), self.db)['tanggal_belum_diinput'])

    def test_partial_batch_preserves_saved_partner_and_uses_new_price(self):
        self.save(partner=1)
        old = self.db.query(DataHarian).filter_by(id_mitra=1).one()
        update_recipe_price(1,ResepHargaUpdate(harga_jual_per_potong='4000'),self.db)
        save_daily_batch(DataHarianBatchCreate(tanggal=self.day,items=[
            dict(id_mitra=2,jumlah_suplai=10,jumlah_return=2,mitra_tutup=False)]),self.db)
        self.assertEqual(old.harga_jual_per_potong, Decimal('3000'))
        self.assertEqual(old.jumlah_suplai, 10)
        self.assertEqual(self.totals()[1], Decimal('56000'))
        self.assertEqual(self.db.query(DataHarian).count(), 2)

    def test_partial_batch_rejects_overwriting_saved_partner_atomically(self):
        self.save(partner=1)
        with self.assertRaises(HTTPException) as caught:
            save_daily_batch(DataHarianBatchCreate(tanggal=self.day,items=[
                dict(id_mitra=1,jumlah_suplai=99,jumlah_return=0),
                dict(id_mitra=2,jumlah_suplai=10,jumlah_return=0)]),self.db)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.db.query(DataHarian).count(), 1)
        self.assertEqual(self.db.query(DataHarian).one().jumlah_suplai, 10)

    def test_partial_legacy_batch_preserves_historical_amount(self):
        self.save(partner=1)
        old = self.db.query(DataHarian).one()
        old.harga_jual_per_potong = None
        self.db.commit()
        save_daily_batch(DataHarianBatchCreate(tanggal=self.day,items=[
            dict(id_mitra=2,jumlah_suplai=10,jumlah_return=2)]),self.db)
        self.assertIsNone(old.harga_jual_per_potong)
        self.assertEqual(self.totals()[1], Decimal('48000'))
        self.assertEqual(self.totals()[2], Decimal('12000'))

    def test_first_input_is_included_even_when_autoflush_disabled(self):
        result=self.save()
        self.assertEqual(result['data']['harga_jual_per_potong'],Decimal('3000'))
        self.assertEqual(self.totals(),{1:Decimal('24000'),2:Decimal('6000')})

    def test_price_change_cannot_reprice_saved_input_or_report(self):
        self.save()
        update_recipe_price(1,ResepHargaUpdate(harga_jual_per_potong='4000'),self.db)
        _sync_daily_finance(self.db,self.day)
        self.db.commit()
        self.assertEqual(self.totals(),{1:Decimal('24000'),2:Decimal('6000')})
        report=get_finance(tanggal=self.day,db=self.db)
        self.assertEqual(report['total_pemasukan'],24000)
        self.assertEqual(self.db.query(DataHarian).one().harga_jual_per_potong,Decimal('3000'))

    def test_next_input_uses_new_price_even_on_same_date(self):
        self.save()
        update_recipe_price(1,ResepHargaUpdate(harga_jual_per_potong='4000'),self.db)
        self.save(partner=2,supply=3,returned=1)
        rows=self.db.query(DataHarian).order_by(DataHarian.id_mitra).all()
        self.assertEqual([r.harga_jual_per_potong for r in rows],[Decimal('3000'),Decimal('4000')])
        self.assertEqual(self.totals(),{1:Decimal('32000'),2:Decimal('10000')})

    def test_next_date_uses_new_price(self):
        self.save()
        update_recipe_price(1,ResepHargaUpdate(harga_jual_per_potong='4000'),self.db)
        self.save(day=date.today())
        self.assertEqual(self.db.query(DataHarian).filter_by(tanggal=date.today()).one().harga_jual_per_potong,Decimal('4000'))
        old=get_finance(tanggal=self.day,db=self.db)
        new=get_finance(tanggal=date.today(),db=self.db)
        self.assertEqual(old['total_pemasukan'],24000)
        self.assertEqual(new['total_pemasukan'],32000)

    def test_batch_captures_one_price_and_correct_aggregate(self):
        request=DataHarianBatchCreate(tanggal=self.day,items=[
            {'id_mitra':1,'jumlah_suplai':10,'jumlah_return':2},
            {'id_mitra':2,'jumlah_suplai':5,'jumlah_return':1}])
        save_daily_batch(request,self.db)
        self.assertEqual([r.harga_jual_per_potong for r in self.db.query(DataHarian).all()],[Decimal('3000')]*2)
        self.assertEqual(self.totals(),{1:Decimal('36000'),2:Decimal('9000')})

    def legacy(self):
        self.db.add(DataHarian(id_mitra=1,tanggal=self.day,jumlah_suplai=10,jumlah_return=2,jumlah_terjual=8,mitra_tutup=False,harga_jual_per_potong=None))
        self.db.add(TransaksiKeuangan(id_transaksi=1,id_kategori=1,tanggal_transaksi=self.day,nominal=20000,sumber='otomatis',referensi_tipe='data_harian'))
        self.db.add(TransaksiKeuangan(id_transaksi=2,id_kategori=2,tanggal_transaksi=self.day,nominal=5000,sumber='otomatis',referensi_tipe='data_harian'))
        self.db.commit()

    def test_legacy_report_is_read_only_and_preserves_recorded_amount(self):
        self.legacy()
        with patch('app.main._sync_daily_finance',side_effect=AssertionError('GET must not sync')),patch('app.main._pudding_price',side_effect=AssertionError('GET must not use current price')):
            report=get_finance(tanggal=self.day,db=self.db)
        self.assertEqual(report['total_pemasukan'],20000)
        self.assertEqual(self.totals(),{1:Decimal('20000'),2:Decimal('5000')})
        self.assertIsNone(self.db.query(DataHarian).one().harga_jual_per_potong)
        self.assertFalse(self.db.dirty)

    def test_unknown_legacy_price_blocks_resync_and_rolls_back_new_input(self):
        self.legacy()
        with self.assertRaises(HTTPException) as error:
            self.save(partner=2)
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(self.db.query(DataHarian).count(),1)
        self.assertEqual(self.totals(),{1:Decimal('20000'),2:Decimal('5000')})

    def test_missing_master_price_captures_fallback(self):
        self.db.get(Resep,1).harga_jual_per_potong=0
        self.db.commit()
        self.save()
        self.assertEqual(self.db.query(DataHarian).one().harga_jual_per_potong,Decimal('3000'))

    def test_decimal_price_keeps_cents(self):
        update_recipe_price(1,ResepHargaUpdate(harga_jual_per_potong='3000.50'),self.db)
        self.save()
        self.assertEqual(self.totals(),{1:Decimal('24004'),2:Decimal('6001')})

if __name__=='__main__':
    unittest.main()
