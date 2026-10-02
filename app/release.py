"""Kontrak rilis dan pemeriksaan schema tanpa mengubah database."""
import json
from pathlib import Path
from sqlalchemy import inspect, Numeric
from app.database import Base

CONTRACT = json.loads(Path(__file__).with_name("release_contract.json").read_text(encoding="utf-8"))
API_VERSION = CONTRACT["api_version"]


def check_schema(connection):
    inspector = inspect(connection)
    problems = []
    existing = set(inspector.get_table_names())
    columns = {}
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            problems.append("table:" + table.name)
            continue
        columns[table.name] = {c["name"]: c for c in inspector.get_columns(table.name)}
        for name in table.columns.keys():
            if name not in columns[table.name]:
                problems.append("column:" + table.name + "." + name)
    for table in ["resep", "data_harian"]:
        column = columns.get(table, {}).get("harga_jual_per_potong")
        if column is not None:
            type_ = column["type"]
            if not isinstance(type_, Numeric) or type_.precision != 15 or type_.scale != 2:
                problems.append("type:" + table + ".harga_jual_per_potong")
    method = columns.get("resep", {}).get("metode_perencanaan")
    if method is not None and connection.dialect.name == "mysql":
        if set(getattr(method["type"], "enums", [])) != {"manual", "prediksi"}:
            problems.append("type:resep.metode_perencanaan")
    if "eksekusi_prediksi" in existing:
        if inspector.get_pk_constraint("eksekusi_prediksi")["constrained_columns"] != ["id_permintaan"]:
            problems.append("primary_key:eksekusi_prediksi.id_permintaan")
        uniques = inspector.get_unique_constraints("eksekusi_prediksi")
        indexes = inspector.get_indexes("eksekusi_prediksi")
        if not any(item["column_names"] == ["tanggal_target"] for item in uniques) and not any(
                item.get("unique") and item["column_names"] == ["tanggal_target"] for item in indexes):
            problems.append("unique:eksekusi_prediksi.tanggal_target")
    for table, column, parent, parent_column in [
            ("stok_batch", "id_resep", "resep", "id_resep"),
            ("eksekusi_prediksi", "id_rencana", "prediksi_harian", "id_prediksi")]:
        if table in existing and not any(
                fk["constrained_columns"] == [column] and fk["referred_table"] == parent
                and fk["referred_columns"] == [parent_column]
                for fk in inspector.get_foreign_keys(table)):
            problems.append("foreign_key:" + table + "." + column)
    return {"ready": not problems, "problems": problems}


def release_readiness(routes, connection):
    available = {(method, route.path) for route in routes
                 for method in getattr(route, "methods", set())}
    endpoints = [{**endpoint, "available": (endpoint["method"], endpoint["path"]) in available}
                 for endpoint in CONTRACT["required_endpoints"]]
    try:
        database = check_schema(connection)
    except Exception:
        database = {"ready": False, "problems": ["database_unavailable"]}
    ready = database["ready"] and all(endpoint["available"] for endpoint in endpoints)
    return {"api_version": API_VERSION, "contract_version": CONTRACT["contract_version"],
            "schema_revision": CONTRACT["schema_revision"] if database["ready"] else None,
            "ready": ready, "database": database, "endpoints": endpoints}
