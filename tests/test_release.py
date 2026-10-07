import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.database import Base
from app.main import app, get_release_readiness
from app.release import CONTRACT, release_readiness

script_path = Path(__file__).resolve().parents[1] / "scripts" / "check_release.py"
spec = importlib.util.spec_from_file_location("release_checker", script_path)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def readiness(self):
        return get_release_readiness(self.db)

    def test_ready_when_schema_routes_and_request_contract_match(self):
        result = self.readiness()
        self.assertTrue(result['ready'])
        self.assertEqual(checker.validate_release(result, app.openapi()), [])
        self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)
        self.assertEqual(self.db.execute(text('SELECT COUNT(*) FROM eksekusi_prediksi')).scalar(), 0)

    def test_missing_price_endpoint_is_not_ready(self):
        routes = [r for r in app.routes if getattr(r, 'path', None) != '/resep/{id_resep}/harga']
        result = release_readiness(routes, self.db.connection())
        self.assertFalse(result['ready'])
        self.assertTrue(result['database']['ready'])
        self.assertIn('Endpoint belum tersedia: PUT /resep/{id_resep}/harga',
                      checker.validate_release(result, app.openapi()))

    def test_missing_schema_table_is_not_ready_and_not_created(self):
        self.db.execute(text('DROP TABLE eksekusi_prediksi'))
        self.db.commit()
        result = self.readiness()
        self.assertFalse(result['ready'])
        self.assertIsNone(result['schema_revision'])
        self.assertIn('table:eksekusi_prediksi', result['database']['problems'])
        self.assertFalse(self.db.execute(text("SELECT name FROM sqlite_master WHERE name='eksekusi_prediksi'")).first())

    def test_missing_historical_price_column_is_detected(self):
        self.db.execute(text('ALTER TABLE data_harian DROP COLUMN harga_jual_per_potong'))
        self.db.commit()
        self.assertIn('column:data_harian.harga_jual_per_potong', self.readiness()['database']['problems'])

    def test_missing_unique_target_protection_is_detected(self):
        self.db.execute(text('DROP TABLE eksekusi_prediksi'))
        self.db.execute(text('CREATE TABLE eksekusi_prediksi (id_permintaan VARCHAR(36) PRIMARY KEY, id_rencana INTEGER REFERENCES prediksi_harian(id_prediksi), tanggal_target DATE, sidik_permintaan VARCHAR(64), hasil JSON, created_at DATETIME)'))
        self.db.commit()
        self.assertIn('unique:eksekusi_prediksi.tanggal_target', self.readiness()['database']['problems'])

    def test_database_error_does_not_expose_connection_details(self):
        with patch.object(self.db, 'connection', side_effect=RuntimeError('secret-password')):
            result=self.readiness()
        self.assertFalse(result['ready'])
        self.assertNotIn('secret-password', str(result))

    def test_release_cli_rejects_version_and_request_field_mismatches(self):
        result=self.readiness()
        result['api_version']='2.0.0'
        errors=checker.validate_release(result, app.openapi())
        self.assertIn('Versi API tidak sesuai artefak rilis.', errors)
        result=self.readiness()
        spec=copy.deepcopy(app.openapi())
        spec['components']['schemas']['ProduksiEksekusiCreate']['required'].remove('id_permintaan')
        self.assertIn('Kontrak body belum sesuai: POST /api/produksi/eksekusi', checker.validate_release(result,spec))

    def test_release_cli_rejects_reverse_proxy_missing_price_route(self):
        spec=copy.deepcopy(app.openapi())
        del spec['paths']['/resep/{id_resep}/harga']['put']
        self.assertIn('Endpoint belum tersedia: PUT /resep/{id_resep}/harga', checker.validate_release(self.readiness(),spec))

    def test_frontend_uses_same_release_contract(self):
        path=Path(__file__).resolve().parents[2]/'frontend'/'lib'/'services'/'release_contract.dart'
        if not path.exists():
            self.skipTest('Frontend tidak ada pada checkout backend saja')
        source=path.read_text(encoding='utf-8')
        self.assertIn("minimumApiVersion = '"+CONTRACT['api_version']+"'", source)
        self.assertIn('requiredApiContract = '+str(CONTRACT['contract_version']),source)
        self.assertIn("requiredSchemaRevision = '"+CONTRACT['schema_revision']+"'",source)
        for endpoint in CONTRACT['required_endpoints']:
            self.assertIn("'"+endpoint['method']+' '+endpoint['path']+"'",source)


if __name__ == '__main__':
    unittest.main()
