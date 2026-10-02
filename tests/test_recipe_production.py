import unittest
from datetime import date, datetime, timedelta
from uuid import uuid4
from unittest.mock import patch
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from fastapi import HTTPException
from app.database import Base
from app.models import (Produk, Resep, ResepBahan, BahanBaku, MutasiStok,
                        StokBatch, Produksi, KategoriKeuangan, TransaksiKeuangan,
                        Mitra, ModelMitra, PrediksiHarian, EksekusiPrediksi)
from app.schemas import ProduksiCreate, ProduksiEksekusiCreate, ResepCreate, PrediksiProductionCreate
from app.main import (_manual_production_plan, create_production, preview_manual_production,
                      execute_batch_production, _batch_production_plan, pudding_stock,
                      create_recipe, _prediction_recipe, _recipe_json, _prediction_total_plan,
                      create_prediction_production, get_dashboard)

@compiles(BigInteger, 'sqlite')
def sqlite_bigint(type_, compiler, **kw):
    return 'INTEGER'

class RecipeProductionTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        self.db.add_all([
            Produk(id_produk=1,nama_produk='Original',metode_perencanaan='prediksi',status='aktif'),
            Produk(id_produk=2,nama_produk='Puding A',metode_perencanaan='manual',status='aktif'),
            BahanBaku(id_bahan=1,nama_bahan='Gula',satuan_resep='gram',harga_per_satuan_resep=10,status='aktif'),
            KategoriKeuangan(id_kategori=1,nama_kategori='Pemakaian bahan baku',jenis='pengeluaran',sumber='otomatis',status='aktif'),
        ])
        self.db.flush()
        self.db.add_all([
            Resep(id_resep=1,id_produk=1,nama_resep='Marie',metode_perencanaan='prediksi',hasil_per_loyang=33,status='aktif'),
            Resep(id_resep=3,id_produk=2,nama_resep='Lumut',metode_perencanaan='manual',hasil_per_loyang=20,status='aktif'),
            Resep(id_resep=5,id_produk=2,nama_resep='Snowcake',metode_perencanaan='manual',hasil_per_loyang=25,status='aktif'),
        ])
        self.db.flush()
        for id_, recipe, needed in [(1,1,33),(2,3,10),(3,5,20)]:
            self.db.add(ResepBahan(id_resep_bahan=id_,id_resep=recipe,id_bahan=1,kebutuhan_per_loyang=needed,status='aktif'))
        self.db.add(MutasiStok(id_mutasi=1,id_bahan=1,jenis_mutasi='pembelian',jumlah_masuk=1000,jumlah_keluar=0))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def batch(self,id_,recipe,qty,days=7,age=0):
        self.db.add(StokBatch(id_batch=id_,id_resep=recipe,jumlah_awal=qty,jumlah_sisa=qty,
            tanggal_kadaluarsa=datetime.now()+timedelta(days=days),
            created_at=datetime.now()-timedelta(days=age)))

    def predictions(self,qty):
        self.db.add(Mitra(id_mitra=1,nama_mitra='Mitra',status='aktif'))
        self.db.add(ModelMitra(id_model=1,id_mitra=1,jenis_dataset='uji',nama_file_model='model',path_model='model'))
        self.db.flush()
        self.db.add(PrediksiHarian(id_prediksi=1,id_mitra=1,id_model=1,
            tanggal_prediksi=date.today(),tanggal_target=date.today()+timedelta(days=1),
            hasil_prediksi=qty,hasil_prediksi_bulat=qty,jumlah_disetujui=qty,status_prediksi='disetujui',is_test=False))
        self.db.commit()

    def test_manual_preview_uses_exact_recipe_not_first_of_shared_product(self):
        lumut = _manual_production_plan(self.db,3,2)
        snow = _manual_production_plan(self.db,5,2)
        self.assertEqual(lumut['hasil_produksi'],40)
        self.assertEqual(snow['hasil_produksi'],50)
        self.assertEqual(snow['bahan'][0]['kebutuhan'],40)
        with self.assertRaises(HTTPException) as error:
            preview_manual_production(jumlah_loyang=1,id_resep=None,id_produk=2,db=self.db)
        self.assertEqual(error.exception.status_code,400)

    def test_manual_production_records_selected_recipe_and_batch(self):
        result = create_production(ProduksiCreate(id_resep=5,tanggal_produksi=date.today(),jumlah_loyang=2),self.db)
        data=result['data']
        self.assertEqual(data['nama_resep'],'Snowcake')
        batch=self.db.get(StokBatch,data['id_batch_baru'])
        self.assertEqual((batch.id_resep,batch.jumlah_sisa),(5,50))
        self.assertEqual(self.db.query(Produksi).one().id_resep,5)
        usage=self.db.query(MutasiStok).filter_by(jenis_mutasi='pemakaian_produksi').one()
        self.assertEqual(float(usage.jumlah_keluar),40)
        self.assertEqual(self.db.query(TransaksiKeuangan).count(),1)

    def test_shortage_rolls_back_production_and_batch(self):
        with self.assertRaises(HTTPException):
            create_production(ProduksiCreate(id_resep=5,tanggal_produksi=date.today(),jumlah_loyang=100),self.db)
        self.assertEqual(self.db.query(Produksi).count(),0)
        self.assertEqual(self.db.query(StokBatch).count(),0)
        self.assertEqual(self.db.query(MutasiStok).count(),1)

    def test_fifo_never_uses_other_recipe_unknown_or_expired(self):
        self.batch(1,1,4,age=2)
        self.batch(2,1,6,age=1)
        self.batch(3,3,80,age=3)
        self.batch(4,None,40,age=4)
        self.batch(5,1,50,days=-1,age=4)
        self.predictions(25)
        preview=_batch_production_plan(self.db,25)
        self.assertEqual(preview['total_stok_sistem'],10)
        result=execute_batch_production(ProduksiEksekusiCreate(id_rencana=1, tanggal_target=date.today()+timedelta(days=1), id_permintaan=uuid4(), prediksi_kebutuhan=25),self.db)['data']
        self.assertEqual(result['total_stok_sistem'],10)
        self.assertEqual(result['sisa_stok_baru'],18)
        self.assertEqual(self.db.get(StokBatch,result['id_batch_baru']).id_resep,1)
        self.assertEqual(self.db.get(StokBatch,3).jumlah_sisa,80)
        self.assertEqual(self.db.get(StokBatch,4).jumlah_sisa,40)
        self.assertEqual(self.db.get(StokBatch,5).jumlah_sisa,50)
        self.assertEqual(self.db.get(StokBatch,1).jumlah_sisa,0)
        self.assertEqual(self.db.get(StokBatch,2).jumlah_sisa,0)

    def test_fifo_stock_reduction_rolls_back_when_materials_insufficient(self):
        self.batch(1,1,10)
        self.predictions(2500)
        with self.assertRaises(HTTPException):
            execute_batch_production(ProduksiEksekusiCreate(id_rencana=1, tanggal_target=date.today()+timedelta(days=1), id_permintaan=uuid4(), prediksi_kebutuhan=2500),self.db)
        self.assertEqual(self.db.get(StokBatch,1).jumlah_sisa,10)
        self.assertEqual(self.db.query(StokBatch).count(),1)
        self.assertEqual(self.db.query(Produksi).count(),0)
        self.assertEqual(self.db.get(PrediksiHarian,1).status_prediksi,'disetujui')

    def test_failed_finance_record_rolls_back_manual_production(self):
        self.db.query(KategoriKeuangan).delete()
        self.db.commit()
        with self.assertRaises(HTTPException):
            create_production(ProduksiCreate(id_resep=5,tanggal_produksi=date.today(),jumlah_loyang=2),self.db)
        self.assertEqual(self.db.query(Produksi).count(),0)
        self.assertEqual(self.db.query(MutasiStok).count(),1)
        self.assertEqual(self.db.query(StokBatch).count(),0)

    def test_fifo_uses_oldest_batch_when_no_new_production_needed(self):
        self.batch(1,1,4,age=2)
        self.batch(2,1,20,age=1)
        self.predictions(3)
        result=execute_batch_production(ProduksiEksekusiCreate(id_rencana=1, tanggal_target=date.today()+timedelta(days=1), id_permintaan=uuid4(), prediksi_kebutuhan=3),self.db)['data']
        self.assertIsNone(result['id_batch_baru'])
        self.assertEqual(result['jumlah_loyang'],0)
        self.assertEqual(self.db.get(StokBatch,1).jumlah_sisa,1)
        self.assertEqual(self.db.get(StokBatch,2).jumlah_sisa,20)
        self.assertEqual(self.db.query(Produksi).count(),0)

    def test_recipe_method_is_independent_of_legacy_product(self):
        recipe=self.db.get(Resep,1)
        recipe.id_produk=2
        self.db.commit()
        self.assertEqual(_prediction_recipe(self.db).id_resep,1)
        self.assertEqual(_recipe_json(self.db,recipe)['metode_perencanaan'],'prediksi')

    def test_second_prediction_recipe_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            create_recipe(ResepCreate(nama_resep='Baru',metode_perencanaan='prediksi',hasil_per_loyang=20,satuan_hasil='potong'),self.db)
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(self.db.query(Resep).count(),3)

    def test_stock_groups_recipe_unknown_and_expired_separately(self):
        self.batch(1,1,10)
        self.batch(2,3,20)
        self.batch(3,3,5,days=-1)
        self.batch(4,None,7)
        self.db.commit()
        result={r['id_resep']:r for r in pudding_stock(self.db)['data']}
        self.assertEqual(result[1]['stok_tersedia'],10)
        self.assertEqual(result[3]['stok_tersedia'],20)
        self.assertEqual(result[3]['stok_kadaluarsa'],5)
        self.assertEqual(result[None]['nama_resep'],'Belum ditentukan')

    def execution_request(self, qty=25, **changes):
        values = dict(id_rencana=1, tanggal_target=date.today()+timedelta(days=1),
                      id_permintaan=uuid4(), prediksi_kebutuhan=qty)
        values.update(changes)
        return ProduksiEksekusiCreate(**values)

    def execution_counts(self):
        return tuple(self.db.query(model).count() for model in (
            Produksi, MutasiStok, StokBatch, TransaksiKeuangan, EksekusiPrediksi))

    def test_retry_returns_original_result_without_changing_stock_or_cost(self):
        self.batch(1,1,10)
        self.predictions(25)
        request = self.execution_request()
        first = execute_batch_production(request, self.db)
        counts = self.execution_counts()
        stock = [(b.id_batch,b.jumlah_sisa) for b in self.db.query(StokBatch).all()]
        second = execute_batch_production(request, self.db)
        self.assertEqual(second, first)
        self.assertEqual(self.execution_counts(), counts)
        self.assertEqual([(b.id_batch,b.jumlah_sisa) for b in self.db.query(StokBatch).all()], stock)
        self.assertEqual(self.db.query(Produksi).one().id_prediksi, 1)

    def test_new_request_id_cannot_produce_finished_plan_again(self):
        self.predictions(25)
        execute_batch_production(self.execution_request(), self.db)
        counts = self.execution_counts()
        with self.assertRaises(HTTPException) as caught:
            execute_batch_production(self.execution_request(), self.db)
        self.assertEqual(caught.exception.status_code,409)
        self.assertEqual(self.execution_counts(), counts)

    def test_same_request_id_cannot_change_payload(self):
        self.predictions(25)
        request = self.execution_request()
        execute_batch_production(request,self.db)
        with self.assertRaises(HTTPException) as caught:
            execute_batch_production(request.model_copy(update={'stok_manual_dipakai':0}),self.db)
        self.assertEqual(caught.exception.status_code,409)
        self.assertEqual(self.db.query(EksekusiPrediksi).count(),1)

    def test_wrong_plan_id_or_date_is_rejected_even_when_total_matches(self):
        self.predictions(25)
        for changes in [dict(id_rencana=999),dict(tanggal_target=date.today())]:
            with self.assertRaises(HTTPException) as caught:
                execute_batch_production(self.execution_request(**changes),self.db)
            self.assertEqual(caught.exception.status_code,409)
        self.assertEqual(self.execution_counts(),(0,1,0,0,0))

    def test_exact_target_used_instead_of_newest_plan_with_equal_total(self):
        self.predictions(25)
        self.db.add(PrediksiHarian(id_prediksi=2,id_mitra=1,id_model=1,
            tanggal_prediksi=date.today(),tanggal_target=date.today()+timedelta(days=2),
            hasil_prediksi=25,hasil_prediksi_bulat=25,jumlah_disetujui=25,
            status_prediksi='disetujui',is_test=False))
        self.db.commit()
        self.assertEqual(_prediction_total_plan(self.db)['id_rencana'],2)
        execute_batch_production(self.execution_request(),self.db)
        self.assertEqual(self.db.get(PrediksiHarian,1).status_prediksi,'diproduksi')
        self.assertEqual(self.db.get(PrediksiHarian,2).status_prediksi,'disetujui')

    def test_failed_execution_rolls_back_request_and_can_retry_same_id(self):
        self.batch(1,1,10)
        self.predictions(2500)
        request=self.execution_request(qty=2500)
        with self.assertRaises(HTTPException):
            execute_batch_production(request,self.db)
        self.assertEqual(self.db.query(EksekusiPrediksi).count(),0)
        self.assertEqual(self.db.get(StokBatch,1).jumlah_sisa,10)
        self.db.add(MutasiStok(id_bahan=1,jenis_mutasi='pembelian',jumlah_masuk=5000,jumlah_keluar=0))
        self.db.commit()
        result=execute_batch_production(request,self.db)
        self.assertEqual(result['data']['id_permintaan'],str(request.id_permintaan))
        self.assertEqual(self.db.query(EksekusiPrediksi).count(),1)

    def test_stock_only_execution_is_also_protected(self):
        self.batch(1,1,30)
        self.predictions(25)
        request=self.execution_request()
        result=execute_batch_production(request,self.db)
        self.assertEqual(result['data']['jumlah_loyang'],0)
        self.assertEqual(execute_batch_production(request,self.db),result)
        self.assertEqual(self.db.get(StokBatch,1).jumlah_sisa,5)
        with self.assertRaises(HTTPException):
            execute_batch_production(self.execution_request(),self.db)
        self.assertEqual(self.db.get(StokBatch,1).jumlah_sisa,5)

    def test_legacy_produced_plan_without_request_record_is_rejected(self):
        self.predictions(25)
        self.db.get(PrediksiHarian,1).status_prediksi='diproduksi'
        self.db.commit()
        with self.assertRaises(HTTPException):
            execute_batch_production(self.execution_request(),self.db)
        self.assertEqual(self.execution_counts(),(0,1,0,0,0))

    def test_commit_failure_rolls_back_all_effects(self):
        self.batch(1,1,10)
        self.predictions(25)
        with patch.object(self.db,'commit',side_effect=RuntimeError('commit failed')):
            with self.assertRaises(RuntimeError):
                execute_batch_production(self.execution_request(),self.db)
        self.assertEqual(self.execution_counts(),(0,1,1,0,0))
        self.assertEqual(self.db.get(StokBatch,1).jumlah_sisa,10)
        self.assertEqual(self.db.get(PrediksiHarian,1).status_prediksi,'disetujui')

    def test_alternative_endpoint_uses_same_request_protection(self):
        self.predictions(25)
        request=self.execution_request()
        alternate=PrediksiProductionCreate(**request.model_dump(),tanggal_produksi=date.today())
        first=create_prediction_production(alternate,self.db)
        self.assertEqual(execute_batch_production(request,self.db),first)
        with self.assertRaises(HTTPException):
            create_production(ProduksiCreate(id_resep=1,tanggal_produksi=date.today(),
                jumlah_loyang=1,sumber_produksi='prediksi'),self.db)
        self.assertEqual(self.db.query(Produksi).count(),1)

    def test_dashboard_status_uses_exact_target_and_read_only(self):
        self.predictions(25)
        counts=self.execution_counts()
        dashboard=get_dashboard(self.db)
        self.assertEqual(dashboard['rencana_prediksi']['status'],'Prediksi Dikonfirmasi')
        self.assertEqual(dashboard['rencana_prediksi']['tanggal_target'],date.today()+timedelta(days=1))
        self.assertEqual(dashboard['status_input']['tanggal'],date.today())
        self.assertEqual(dashboard['keuangan_hari_ini']['tanggal'],date.today())
        self.assertEqual(self.execution_counts(),counts)
        self.assertFalse(self.db.new)
        self.db.get(PrediksiHarian,1).status_prediksi='diproduksi'
        self.db.commit()
        self.assertEqual(get_dashboard(self.db)['rencana_prediksi']['status'],'Diproduksi')

if __name__ == '__main__':
    unittest.main()
