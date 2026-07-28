#!/usr/bin/env python3
"""Exporta el histórico local de MariaDB a CSV mensuales para GitHub Pages.

La consulta trabaja por sensor físico, usa el índice (id_sensor, fecha) y crea
una instantánea cronológica ancha por estación/mes. El mes vigente se reemplaza
de forma atómica; los meses anteriores solo se regeneran al ejecutar --all.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import mysql.connector


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "stations.json"
DEFAULT_OUTPUT = ROOT / "data"
DEFAULT_ENV_FILE = Path("/var/www/api_sensores/.env")
CSV_HEADER = [
    "fecha",
    "mp25_ugm3",
    "mp10_ugm3",
    "so2_ppb",
    "cov_ppb",
    "temperatura_c",
    "humedad_pct",
]
VARIABLE_COLUMNS = {
    "mp25": "mp25_ugm3",
    "mp10": "mp10_ugm3",
    "so2": "so2_ppb",
    "cov": "cov_ppb",
    "temp": "temperatura_c",
    "hum": "humedad_pct",
}
MAX_FILE_BYTES = 40 * 1024 * 1024
BATCH_SIZE = 5_000
PART_RE = re.compile(r"^(?P<month>\d{4}-\d{2})-part-(?P<part>\d{3})\.csv$")

# Solo se publican las variables utilizadas por index.html. Temperatura y
# humedad se toman del SHT40 (no del PMS5003), igual que en la web actual.
MODEL_VARIABLES: dict[str, dict[int, tuple[str, float]]] = {
    "PMS5003": {
        8: ("mp25", 1.0),
        9: ("mp10", 1.0),
    },
    "SHT40": {
        3: ("temp", 1.0),
        6: ("hum", 1.0),
    },
    "Gravity: Factory Calibrated Electrochemical multigas So2": {
        53: ("so2", 1000.0),  # MariaDB guarda ppm; el CSV publica ppb.
    },
    "Fermion: ENS160 Air Quality Sensor": {
        54: ("cov", 1.0),
    },
}


@dataclass(frozen=True)
class Source:
    sensor_id: int
    variables: dict[int, tuple[str, float]]


def load_env_file(path: Path) -> None:
    """Carga KEY=VALUE sin imprimir secretos ni reemplazar el entorno."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


def db_config(env_file: Path) -> dict[str, Any]:
    load_env_file(env_file)
    required = ["DB_USER", "DB_PASSWORD", "DB_HOST", "DB_NAME"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError(
            "Faltan variables de MariaDB: " + ", ".join(sorted(missing))
        )
    return {
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "host": os.environ["DB_HOST"],
        "database": os.environ["DB_NAME"],
        "port": int(os.environ.get("DB_PORT", "3306")),
        "connection_timeout": 15,
    }


def load_stations(path: Path) -> list[dict[str, Any]]:
    stations = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(stations, list) or not stations:
        raise ValueError("config/stations.json debe contener una lista no vacía")
    return stations


def month_bounds(month: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(month, "%Y-%m")
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def iter_months(start: date, end: date) -> Iterable[str]:
    current = start.replace(day=1)
    last = end.replace(day=1)
    while current <= last:
        yield current.strftime("%Y-%m")
        days = monthrange(current.year, current.month)[1]
        current += timedelta(days=days)


def discover_sources(connection, device_id: int) -> list[Source]:
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT s.id_sensor, st.modelo
            FROM sensores_en_dispositivo AS sd
            JOIN sensores AS s ON s.id_sensor = sd.id_sensor
            JOIN sensores_tipo AS st
              ON st.id_sensor_tipo = s.id_sensor_tipo
            WHERE sd.id_dispositivo = %s
            ORDER BY s.id_sensor
            """,
            (device_id,),
        )
        sources = [
            Source(int(row["id_sensor"]), MODEL_VARIABLES[row["modelo"]])
            for row in cursor.fetchall()
            if row["modelo"] in MODEL_VARIABLES
        ]
    finally:
        cursor.close()
    if not sources:
        raise RuntimeError(f"Dispositivo {device_id} sin sensores publicables")
    return sources


def existing_parts(output_dir: Path, code: str, month: str) -> list[Path]:
    station_dir = output_dir / code
    return sorted(station_dir.glob(f"{month}-part-*.csv"))


def first_measurement(connection, sources: list[Source]) -> date | None:
    first: datetime | None = None
    for source in sources:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT fecha
                FROM datos
                WHERE id_sensor = %s
                ORDER BY fecha ASC
                LIMIT 1
                """,
                (source.sensor_id,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row and (first is None or row[0] < first):
            first = row[0]
    return first.date() if first else None


def export_month(
    connection,
    station: dict[str, Any],
    sources: list[Source],
    month: str,
    output_dir: Path,
) -> int:
    start, end = month_bounds(month)
    snapshots: dict[datetime, dict[str, str]] = {}
    measurements = 0

    for source in sources:
        variable_ids = sorted(source.variables)
        placeholders = ", ".join(["%s"] * len(variable_ids))
        last_date, last_id = start, 0
        while True:
            cursor = connection.cursor(dictionary=True)
            try:
                cursor.execute(
                    f"""
                    SELECT id_dato, fecha, id_variable, valor
                    FROM datos FORCE INDEX (idx_datos_sensor_fecha)
                    WHERE id_sensor = %s
                      AND id_variable IN ({placeholders})
                      AND fecha >= %s
                      AND fecha < %s
                      AND (fecha > %s OR (fecha = %s AND id_dato > %s))
                    ORDER BY fecha ASC, id_dato ASC
                    LIMIT %s
                    """,
                    (
                        source.sensor_id,
                        *variable_ids,
                        start,
                        end,
                        last_date,
                        last_date,
                        last_id,
                        BATCH_SIZE,
                    ),
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()

            for row in rows:
                variable, multiplier = source.variables[int(row["id_variable"])]
                raw_value = float(row["valor"])
                value = (
                    ""
                    if raw_value == -1
                    else format(raw_value * multiplier, ".10g")
                )
                snapshots.setdefault(row["fecha"], {})[
                    VARIABLE_COLUMNS[variable]
                ] = value
                measurements += 1
            if len(rows) < BATCH_SIZE:
                break
            last = rows[-1]
            last_date, last_id = last["fecha"], int(last["id_dato"])

    station_dir = output_dir / station["code"]
    station_dir.mkdir(parents=True, exist_ok=True)
    completed: list[Path] = []
    part_number = 1
    final_path = station_dir / f"{month}-part-{part_number:03d}.csv"
    temporary = final_path.with_suffix(final_path.suffix + ".tmp")
    handle = temporary.open("w", newline="", encoding="utf-8")
    writer = csv.DictWriter(handle, fieldnames=CSV_HEADER, lineterminator="\n")
    writer.writeheader()

    try:
        for timestamp in sorted(snapshots):
            row = {"fecha": timestamp.isoformat(sep=" "), **snapshots[timestamp]}
            writer.writerow(row)
            if handle.tell() >= MAX_FILE_BYTES:
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                os.replace(temporary, final_path)
                completed.append(final_path)
                part_number += 1
                final_path = (
                    station_dir / f"{month}-part-{part_number:03d}.csv"
                )
                temporary = final_path.with_suffix(final_path.suffix + ".tmp")
                handle = temporary.open("w", newline="", encoding="utf-8")
                writer = csv.DictWriter(
                    handle, fieldnames=CSV_HEADER, lineterminator="\n"
                )
                writer.writeheader()
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, final_path)
        completed.append(final_path)
    except Exception:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise

    completed_set = set(completed)
    for obsolete in existing_parts(output_dir, station["code"], month):
        if obsolete not in completed_set:
            obsolete.unlink()
    return measurements


def latest_rows(connection, stations, station_sources) -> list[list[Any]]:
    output: list[list[Any]] = []
    for station in stations:
        for source in station_sources[station["code"]]:
            # Una sola lectura reciente por sensor físico. Separar una consulta
            # por variable hacía que MariaDB eligiera fk_idVariable y ordenara
            # millones de filas. FORCE INDEX conserva el acceso O(lecturas
            # recientes) por (id_sensor, fecha).
            variable_ids = sorted(source.variables)
            placeholders = ", ".join(["%s"] * len(variable_ids))
            cursor = connection.cursor(dictionary=True)
            try:
                cursor.execute(
                    f"""
                    SELECT fecha, id_variable, valor
                    FROM datos FORCE INDEX (idx_datos_sensor_fecha)
                    WHERE id_sensor = %s
                      AND id_variable IN ({placeholders})
                    ORDER BY fecha DESC
                    LIMIT %s
                    """,
                    (source.sensor_id, *variable_ids, max(20, len(variable_ids) * 5)),
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()

            seen: set[int] = set()
            for row in rows:
                variable_id = int(row["id_variable"])
                if variable_id in seen or variable_id not in source.variables:
                    continue
                if float(row["valor"]) == -1:
                    continue
                seen.add(variable_id)
                variable, multiplier = source.variables[variable_id]
                output.append(
                    [
                        station["code"],
                        row["fecha"].isoformat(sep=" "),
                        variable,
                        format(float(row["valor"]) * multiplier, ".10g"),
                    ]
                )
                if len(seen) == len(source.variables):
                    break
    return output


def write_latest(output_dir: Path, rows: list[list[Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / "latest.csv.tmp"
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["codigo", "fecha", "variable", "valor"])
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output_dir / "latest.csv")


def write_manifest(
    output_dir: Path,
    stations: list[dict[str, Any]],
    updated_at: datetime,
) -> None:
    manifest_stations = []
    for station in stations:
        station_dir = output_dir / station["code"]
        months: dict[str, list[str]] = {}
        if station_dir.exists():
            for path in sorted(station_dir.glob("*.csv")):
                match = PART_RE.match(path.name)
                if match:
                    months.setdefault(match.group("month"), []).append(
                        str(path.relative_to(ROOT))
                    )
        manifest_stations.append({**station, "months": months})

    payload = {
        "schema_version": 1,
        "updated_at": updated_at.isoformat(timespec="seconds"),
        "timezone": "America/Santiago",
        "max_csv_bytes": MAX_FILE_BYTES,
        "stations": manifest_stations,
    }
    temporary = output_dir / "manifest.json.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output_dir / "manifest.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exporta CSV mensuales desde el MariaDB local."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--all",
        action="store_true",
        help="descubre y exporta todos los meses disponibles",
    )
    parser.add_argument(
        "--month",
        action="append",
        help="mes YYYY-MM; puede repetirse. Por defecto exporta el mes actual",
    )
    parser.add_argument("--station", action="append", help="código HIRIPRO opcional")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stations = load_stations(args.config)
    if args.station:
        selected = set(args.station)
        stations = [station for station in stations if station["code"] in selected]
        unknown = selected - {station["code"] for station in stations}
        if unknown:
            raise SystemExit("Estaciones desconocidas: " + ", ".join(sorted(unknown)))

    connection = mysql.connector.connect(**db_config(args.env_file))
    connection.autocommit = True
    total = 0
    try:
        station_sources = {
            station["code"]: discover_sources(connection, station["device_id"])
            for station in stations
        }
        today = date.today()
        for station in stations:
            if args.all:
                first = first_measurement(
                    connection, station_sources[station["code"]]
                )
                months = list(iter_months(first, today)) if first else []
            else:
                months = args.month or [today.strftime("%Y-%m")]
            for month in months:
                count = export_month(
                    connection,
                    station,
                    station_sources[station["code"]],
                    month,
                    args.output_dir,
                )
                total += count
                print(f"{station['code']} {month}: {count} filas nuevas", flush=True)

        write_latest(
            args.output_dir,
            latest_rows(connection, stations, station_sources),
        )
        write_manifest(args.output_dir, stations, datetime.now().astimezone())
    finally:
        connection.close()

    print(f"Exportación terminada: {total} filas nuevas", flush=True)


if __name__ == "__main__":
    main()
