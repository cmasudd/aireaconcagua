# Aire Aconcagua

Sitio estático de la Red de Monitoreo Comunitario del Valle de Aconcagua.

## Datos históricos

La web no consulta el histórico desde la API de sensores. Los datos se extraen
directamente desde el MariaDB local y se publican como CSV junto al sitio:

```text
MariaDB local
  -> scripts/export_monthly_csv.py
  -> data/HIRIPRO-V*/AAAA-MM-part-001.csv
  -> GitHub Pages
  -> index.html
```

Cada archivo contiene una estación y un mes. Si alcanza 40 MiB, el exportador
continúa automáticamente en `part-002`, por lo que ningún CSV sobrepasa el
umbral configurado. La ejecución horaria vuelve a generar únicamente el mes
vigente mediante consultas acotadas por sensor y fecha. El backfill completo
solo se ejecuta de forma manual con `--all`.

Columnas publicadas:

```text
fecha,mp25_ugm3,mp10_ugm3,so2_ppb,cov_ppb,temperatura_c,humedad_pct
```

SO₂ se publica en ppb. Los valores `-1` usados por los equipos para indicar una
lectura ausente se publican como celdas vacías para no graficar información
inválida.

El navegador carga `data/latest.csv` como respaldo inicial para el mapa y las
tarjetas. Después consulta una sola lectura (`limite=1`) por estación cada 10
minutos para mantener la vista en vivo. Solo descarga los CSV mensuales cuando
se elige una estación o un periodo. El botón de descarga en modo Escolar
entrega únicamente la variable seleccionada.

## Exportación local

El script usa por defecto las credenciales ya disponibles en
`/var/www/api_sensores/.env`; no copia ni imprime secretos.

Exportar el mes actual de todas las estaciones:

```bash
/var/www/api_sensores/venv/bin/python scripts/export_monthly_csv.py
```

Crear el histórico inicial completo:

```bash
/var/www/api_sensores/venv/bin/python scripts/export_monthly_csv.py --all
```

Exportar una estación o mes concreto:

```bash
/var/www/api_sensores/venv/bin/python scripts/export_monthly_csv.py \
  --station HIRIPRO-V2 \
  --month 2026-07
```

La operación es idempotente: repetirla produce la misma instantánea mensual y
no duplica filas.

## Actualización horaria

Después del primer `push`, se puede registrar este cron en el mismo servidor:

```cron
7 * * * * flock -n /tmp/aireaconcagua-update.lock /home/cmas/Documentos/aireaconcagua/scripts/update_data.sh >> /home/cmas/Documentos/aireaconcagua/data-update.log 2>&1
```

El desfase al minuto 7 evita ejecutar trabajo exactamente al cambio de hora.
`flock` impide solapamientos. El script solo crea un commit y hace `push` cuando
hay mediciones nuevas.

## Pruebas

```bash
/var/www/api_sensores/venv/bin/python -m unittest discover -s tests -v
sed -n '406,1448p' index.html | node --check
```
