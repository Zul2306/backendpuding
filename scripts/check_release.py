"""Pemeriksaan rilis terhadap server target; hanya melakukan GET."""
import argparse
import json
import sys
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

CONTRACT = json.loads((Path(__file__).resolve().parents[1] / "app" / "release_contract.json").read_text(encoding="utf-8"))


def get_json(base_url, path):
    with urlopen(base_url.rstrip("/") + path, timeout=20) as response:
        return json.load(response)


def validate_release(readiness, spec):
    errors = []
    if readiness.get("api_version") != CONTRACT["api_version"]:
        errors.append("Versi API tidak sesuai artefak rilis.")
    if spec.get("info", {}).get("version") != readiness.get("api_version"):
        errors.append("Versi OpenAPI berbeda dari versi pemeriksaan server.")
    if readiness.get("contract_version") != CONTRACT["contract_version"]:
        errors.append("Kontrak API tidak sesuai.")
    if readiness.get("ready") is not True or readiness.get("database", {}).get("ready") is not True:
        errors.append("Server/schema database belum siap.")
    if readiness.get("schema_revision") != CONTRACT["schema_revision"]:
        errors.append("Revisi schema belum sesuai.")
    advertised = {(item.get("method"), item.get("path")): item.get("available")
                  for item in readiness.get("endpoints", [])}
    for endpoint in CONTRACT["required_endpoints"]:
        method, path = endpoint["method"], endpoint["path"]
        if advertised.get((method, path)) is not True or method.lower() not in spec.get("paths", {}).get(path, {}):
            errors.append("Endpoint belum tersedia: " + method + " " + path)
    for operation, fields in CONTRACT["required_request_fields"].items():
        method, path = operation.split(" ", 1)
        schema = spec.get("paths", {}).get(path, {}).get(method.lower(), {}).get(
            "requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
        if "$ref" in schema:
            schema = spec.get("components", {}).get("schemas", {}).get(schema["$ref"].rsplit("/", 1)[-1], {})
        if not set(fields).issubset(set(schema.get("required", []))):
            errors.append("Kontrak body belum sesuai: " + operation)
    for operation, fields in CONTRACT.get("required_query_parameters", {}).items():
        method, path = operation.split(" ", 1)
        parameters = spec.get("paths", {}).get(path, {}).get(method.lower(), {}).get("parameters", [])
        available = {param.get("name") for param in parameters if param.get("in") == "query"}
        if not set(fields).issubset(available):
            errors.append("Kontrak pagination belum sesuai: " + operation)
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        readiness = get_json(args.base_url, "/release/readiness")
        spec = get_json(args.base_url, "/openapi.json")
        errors = validate_release(readiness, spec)
        report = {"passed": not errors, "api_version": readiness.get("api_version"),
                  "schema_revision": readiness.get("schema_revision"),
                  "endpoints_checked": len(CONTRACT["required_endpoints"]), "errors": errors}
    except (HTTPError, URLError, TimeoutError, ValueError, TypeError, AttributeError, OSError):
        report = {"passed": False, "errors": ["Server tidak dapat diperiksa atau endpoint pemeriksaan belum tersedia."]}
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
