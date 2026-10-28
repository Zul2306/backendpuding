import unittest
from datetime import date, timedelta
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from app.database import Base
from app.models import Mitra, DataHarian
from app.main import daily_history, daily_history_detail


class DailyHistoryDetailTest(unittest.TestCase):
    def test_detail_matches_summary_and_keeps_inactive_and_closed_partners(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            day = date(2026, 10, 1)
            db.add_all([Mitra(id_mitra=1, nama_mitra='Ani', status='nonaktif'),
                        Mitra(id_mitra=2, nama_mitra='Budi', status='aktif'),
                        Mitra(id_mitra=3, nama_mitra='Cici', status='aktif')])
            db.flush()
            db.add_all([
                DataHarian(id_mitra=1, tanggal=day, jumlah_suplai=12,
                           jumlah_return=3, jumlah_terjual=9),
                DataHarian(id_mitra=2, tanggal=day, jumlah_suplai=0,
                           jumlah_return=0, jumlah_terjual=0, mitra_tutup=True),
                DataHarian(id_mitra=3, tanggal=day-timedelta(days=1),
                           jumlah_suplai=50, jumlah_return=5, jumlah_terjual=45)])
            db.commit()
            queries = []
            def record(conn, cursor, statement, parameters, context, many):
                queries.append(statement)
            event.listen(engine, 'before_cursor_execute', record)
            result = daily_history_detail(day, db)
            self.assertEqual(len(queries), 1)
            self.assertTrue(queries[0].lstrip().upper().startswith('SELECT'))
            self.assertEqual([r['nama_mitra'] for r in result['data']], ['Ani', 'Budi'])
            self.assertTrue(result['data'][1]['mitra_tutup'])
            self.assertEqual(result['jumlah_terjual'], 9)
            summary = daily_history(tahun=2026, bulan=10, db=db)['data'][0]
            for key in ['jumlah_suplai', 'jumlah_return']:
                self.assertEqual(result[key], summary[key])
            empty = daily_history_detail(day+timedelta(days=1), db)
            self.assertEqual(empty['data'], [])
            self.assertEqual(empty['jumlah_mitra'], 0)
            self.assertEqual(empty['jumlah_suplai'], 0)
        engine.dispose()
