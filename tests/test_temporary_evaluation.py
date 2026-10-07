from datetime import date, timedelta
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.main import app
from app.models import Mitra, ModelMitra, DataHarian
from app.temporary_evaluation import EvaluationRequest, run_evaluation


class YesterdayModel:
    def __init__(self):
        self.inputs = []

    def predict(self, frame):
        self.inputs.append(frame.copy())
        return frame['terjual'].to_numpy() + 1.25


class TemporaryEvaluationTest(unittest.TestCase):
    async def request(self, method, path, payload=None):
        messages = []
        body = json.dumps(payload).encode() if payload is not None else b''
        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}
        async def send(message):
            messages.append(message)
        await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                   'method': method, 'path': path, 'raw_path': path.encode(),
                   'root_path': '', 'scheme': 'http', 'query_string': b'',
                   'headers': [(b'content-type', b'application/json')],
                   'client': ('127.0.0.1', 1234), 'server': ('test', 80)}, receive, send)
        return next(m['status'] for m in messages if m['type'] == 'http.response.start')

    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / 'model.joblib'
        path.write_bytes(b'model-fixture')
        self.db.add(Mitra(id_mitra=1, nama_mitra='Mitra A', status='aktif'))
        self.db.add(ModelMitra(id_model=1, id_mitra=1, jenis_dataset='test',
                              nama_file_model='model.joblib', path_model=str(path), status_model='aktif'))
        for i in range(8):
            self.db.add(DataHarian(id_data=i+1, id_mitra=1,
                tanggal=date(2026,9,20)+timedelta(days=i),
                jumlah_suplai=100+i, jumlah_return=20, jumlah_terjual=80+i))
        self.db.commit()
        self.model = YesterdayModel()
        self.bundle = {'model': self.model, 'fitur_model': ['terjual'],
                       'target_column': 'target_suplai_besok',
                       'tanggal_train_akhir': '2026-09-19',
                       'tanggal_test_akhir': '2026-09-19'}
        self.env = patch.dict(os.environ, {'ENABLE_TEMP_EVALUATION': 'true'})
        self.loader = patch('app.temporary_evaluation.joblib.load', return_value=self.bundle)
        self.env.start()
        self.load_mock = self.loader.start()
        self.req = EvaluationRequest(tanggal_mulai=date(2026,9,21), tanggal_selesai=date(2026,9,27),
                    tanggal_akhir_data_latih=date(2026,9,20), target_suplai_adalah_terjual=True)

    def tearDown(self):
        self.loader.stop()
        self.env.stop()
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def test_seven_day_metrics_raw_predictions_and_no_writes(self):
        def reject_write(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER')):
                raise AssertionError('Evaluation attempted to write')
        event.listen(self.engine, 'before_cursor_execute', reject_write)
        with patch.object(self.db, 'commit', side_effect=AssertionError('commit called')):
            result = run_evaluation(self.req, self.db)
        self.assertEqual(result['metrik']['jumlah'], 7)
        self.assertEqual(result['metrik']['mae'], .25)
        self.assertEqual(result['metrik']['rmse'], .25)
        self.assertEqual(result['metrik']['bias'], .25)
        self.assertEqual(result['metrik']['mae_baseline'], 1)
        self.assertEqual(result['data'][0]['aktual'], 81)
        self.assertEqual(result['data'][0]['prediksi'], 81.25)
        self.assertEqual([x['terjual'].iloc[0] for x in self.model.inputs], list(range(80,87)))
        self.assertEqual(self.load_mock.call_count, 1)
        self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)
        self.assertFalse(result['ada_peringatan_model'])

    def test_future_changes_do_not_change_earlier_predictions(self):
        # Include engineered features to check their causal construction too.
        self.bundle['fitur_model'] = ['terjual', 'terjual_avg_7', 'suplai_lag_1', 'zero_streak']
        first = run_evaluation(self.req, self.db)
        old_inputs = [x.copy() for x in self.model.inputs]
        self.db.get(DataHarian, 6).jumlah_suplai = 500
        self.db.commit()
        self.model.inputs.clear()
        second = run_evaluation(self.req, self.db)
        self.assertEqual(first['data'][:4], second['data'][:4])
        for old, new in zip(old_inputs[:5], self.model.inputs[:5]):
            self.assertTrue(old.equals(new))

    def test_missing_report_not_zero_and_next_day_is_skipped(self):
        self.db.delete(self.db.get(DataHarian, 3))  # Sep 22
        self.db.commit()
        result = run_evaluation(self.req, self.db)
        self.assertEqual(result['dilewati'], 2)
        self.assertEqual(result['metrik']['jumlah'], 5)
        self.assertNotIn('aktual', result['data'][1])
        self.assertIn('sehari sebelum', result['data'][2]['alasan'])

    def test_training_or_tuning_overlap_blocks_partner(self):
        for key in ['tanggal_train_akhir', 'tanggal_test_akhir']:
            with self.subTest(key=key):
                previous = self.bundle[key]
                self.bundle[key] = '2026-09-20'  # next-day label reaches Sep 21
                result = run_evaluation(self.req, self.db)
                self.assertEqual(result['metrik']['jumlah'], 0)
                self.assertIsNone(result['metrik']['mae'])
                self.assertIn('berpotensi', result['data'][0]['alasan'])
                self.bundle[key] = previous

    def test_unconfirmed_target_and_unknown_metadata_are_visible(self):
        self.req.target_suplai_adalah_terjual = False
        self.bundle.pop('tanggal_train_akhir')
        result = run_evaluation(self.req, self.db)
        self.assertTrue(result['ada_peringatan_model'])
        self.assertEqual(len(result['mitra'][0]['peringatan']), 2)

    def test_request_bounds_and_default_disabled(self):
        for updates in [{'tanggal_selesai': date(2026,9,20)},
                        {'tanggal_mulai': date(2026,8,1)},
                        {'tanggal_selesai': date.today()},
                        {'tanggal_akhir_data_latih': date(2026,9,21)}]:
            with self.assertRaises(HTTPException) as caught:
                run_evaluation(self.req.model_copy(update=updates), self.db)
            self.assertEqual(caught.exception.status_code, 422)
        with patch.dict(os.environ, {'ENABLE_TEMP_EVALUATION': 'false'}):
            self.assertEqual(asyncio.run(self.request('GET', '/evaluasi-sementara/status')), 404)
            self.assertEqual(asyncio.run(self.request('POST', '/evaluasi-sementara/jalankan',
                             self.req.model_dump(mode='json'))), 404)

    def test_invalid_actual_and_model_failures_report_skips(self):
        self.db.get(DataHarian, 8).jumlah_return = 999
        self.db.commit()
        result = run_evaluation(self.req, self.db)
        self.assertEqual(result['dilewati'], 1)
        with patch.object(self.model, 'predict', return_value=[float('nan')]):
            result = run_evaluation(self.req, self.db)
        self.assertEqual(result['metrik']['jumlah'], 0)

    def test_zero_sales_is_valid_and_missing_model_is_visible(self):
        for row in self.db.query(DataHarian).all():
            row.jumlah_return = row.jumlah_suplai
        self.db.commit()
        result = run_evaluation(self.req, self.db)
        self.assertEqual(result['metrik']['jumlah'], 7)
        self.assertEqual(result['metrik']['mae'], 1.25)
        self.db.get(ModelMitra, 1).status_model = 'nonaktif'
        self.db.commit()
        result = run_evaluation(self.req, self.db)
        self.assertEqual(result['dilewati'], 7)
