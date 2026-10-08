# Karting Live — seguimiento y estrategia de carreras de resistencia (Apex Timing)

Dos contenedores comparten una base de datos SQLite:

| Servicio | Archivo | Qué hace |
|---|---|---|
| `collector` | `app/collector.py` | Lee el WSS de Apex Timing, guarda cada vuelta, cada entrada/salida de pit y el estado en vivo. Si arrancas con la carrera empezada, recupera las vueltas antiguas por `request.php`. |
| `web` | `app/web.py` + `app/index.html` | Calcula tabla y alertas desde la BD y sirve la web en el puerto del cfg. |

## 1. Requisitos
- Docker con el plugin Compose v2 (`docker compose version`).
- Linux (el compose usa `network_mode: host`; en Windows/Mac ver "Notas").
- Salida a internet hacia `live-data.apex-timing.com`.

## 2. Estructura
```
karting-live/
├── race.cfg              # <- LO ÚNICO QUE EDITAS CADA CARRERA
├── docker-compose.yml
├── Dockerfile
└── app/
    ├── collector.py  web.py  index.html  cfg.py
    └── apex_parser.py  apex_history.py
```

## 3. Configurar la carrera (`race.cfg`)
Formato INI. Valor vacío = no aplica; `false` = desactivado.

**`[race]`**
- `name`: título que sale arriba en la web.
- `race_url`: URL de la carrera en live.apex-timing.com.
- `wss_url`: WebSocket de la carrera (en DevTools → Network → WS), p. ej. `wss://live-data.apex-timing.com:8803/`.
- `history_port`: puerto de `request.php` para recuperar vueltas antiguas (p. ej. `8800`; si no devuelve datos, prueba el del WSS).
- `follow`: karts a seguir, separados por coma (se resaltan y reciben los consejos de kart). Recomendado: máximo 5.
- `duration_h`: duración de la carrera.

**`[web]`** → `port`: puerto de la web (por defecto **8090**).

**`[storage]`** → `reset_db`:
- `new_race` (por defecto): borra los datos solo si cambia `wss_url` o `race_url`.
- `always`: borra en **cada** arranque (úsalo solo para el primer arranque y luego cámbialo a `never`).
- `never`: no borra nunca.

**`[rules]`** (normativa; minutos salvo `*_s` en segundos): `min_stops`, `max_stops`, `min_pit_s`, `max_stint_min`, `min_stint_min`, `min_driver_min`, `max_driver_min`, `pit_close_remaining_min`. Hay tres campos informativos que todavía no generan alertas: `no_pit_first_min`, `mandatory_driver_change`, `stint_tolerance_s`.

**`[alerts]`** (umbrales del semáforo): porcentajes de stint (amarillo 12 %, rojo 6 %), diferencias de ritmo frente a stints anteriores o compañeros, vuelta lenta, ventana y mínimos de la cola de karts (`kart_queue_min/max`: karts que esperan en la fila de salida; si los conoces, pon el mismo valor en ambos), espera máxima y factor de "parado en pista".

## 4. Arrancar
```bash
cd karting-live
docker compose up -d --build
```
Abre `http://localhost:8090` (o `http://IP-DEL-SERVIDOR:8090` desde el móvil en la misma red).

## 5. Comprobar que funciona
```bash
docker compose ps                    # collector y web en "running"
docker compose logs -f collector     # debe aparecer "conectado wss://..."
docker compose logs -f web           # "web en http://0.0.0.0:8090"
curl -s localhost:8090/api/state | head -c 300
```
Si arrancas a mitad de carrera, verás en el log del colector líneas `backfill #<kart>: N vueltas`.

## 6. Cambiar de carrera o cambiar el cfg
1. Edita `race.cfg` (al cambiar `wss_url`/`race_url` con `reset_db = new_race`, la BD se limpia sola).
2. Reinicia: `docker compose restart`
   (si cambiaste el puerto o el código: `docker compose up -d --build`).

Borrar todos los datos a mano:
```bash
docker compose down -v
```

## 6b. Reinicio automático (apagados del equipo)
Ambos servicios usan `restart: unless-stopped`:
- **Si apagas o reinicias el ordenador**, al volver a arrancar Docker levanta solos los contenedores que estaban en marcha.
- **Si los paras tú** (`docker compose stop` o `docker stop`), se quedan parados y **no** se reinician solos, ni siquiera tras reiniciar el ordenador, hasta que hagas `docker compose start` o `up -d`.
- `docker compose down` también los deja parados (y elimina los contenedores, no los datos; los datos solo se borran con `down -v`).
- Requisito: que Docker arranque con el sistema (`sudo systemctl enable docker`).
- Nota: si un proceso se cae por un error, Docker lo vuelve a levantar. Docker no distingue entre "cayó" y "lo paró el sistema". Para no reiniciar tras un fallo, cambia a `restart: "no"`, pero entonces tampoco volverán tras un apagado.

## 7. Cómo leer la web
- **Tabla:** posición, kart, equipo, última vuelta, distancia al primero y al de delante, pits, tiempo desde la última parada, tiempo en box si está dentro.
- **Posición:** al pasar el ratón (o mantener pulsado en el móvil) muestra la posición real estimada descontando paradas pendientes. No incluye sanciones.
- **Equipo:** mantener sobre el nombre muestra el piloto actual.
- **Círculo:** verde = bien, amarillo = aviso, rojo = urgente. Al pulsar se abre el popup con los motivos (tiempo máximo en pista, ritmo del stint, vuelta lenta, parado en pista, pit corto, paradas o minutos de piloto pendientes y la cola de karts).

## 8. Recuperar los datos (vuelta a vuelta)
La BD está en el volumen `data` (`/data/race.sqlite` dentro del contenedor). Para copiarla:
```bash
docker volume ls | grep data        # comprueba el nombre, normalmente karting-live_data
docker run --rm -v karting-live_data:/data -v "$PWD":/out alpine cp /data/race.sqlite /out/
```
Tablas: `lap` (vuelta a vuelta con piloto y sectores), `pit` (entradas y salidas), `live` (estado actual), `meta`.

## 9. Probar sin carrera en directo (sin Docker)
```bash
cd app
pip install websockets
export DATA_DIR=/tmp/karts RACE_CFG=../race.cfg
python collector.py --replay ruta/a/frames.jsonl   # carga una captura del WSS
python web.py                                        # web en el puerto del cfg
```
Con una captura antigua pueden salir avisos de "parado en pista", porque las horas ya no coinciden con las actuales.

## 10. Problemas frecuentes
| Síntoma | Qué hacer |
|---|---|
| "address already in use" | El puerto está ocupado: cambia `[web] port` y `docker compose up -d --build`. |
| Tabla vacía | Mira `docker compose logs collector`. Revisa `wss_url` y que haya carrera activa. |
| No salen vueltas antiguas | `history_port` incorrecto o la carrera ya borró su histórico. Prueba otro puerto. |
| Se pierden datos al reiniciar | Tienes `reset_db = always`. Pásalo a `new_race` o `never`. |
| Alertas de kart no aparecen | Necesitan al menos 3 entradas a box vistas en directo desde que arrancó el colector. |

## 11. Notas y limitaciones
- **Windows/Mac:** `network_mode: host` no funciona igual. En `docker-compose.yml` quita esa línea del servicio `web` y añade `ports: ["8090:8090"]`.
- Las alertas de kart son estimaciones: con los datos de la carrera de 30 h no se encontró una señal fiable de que el kart se herede de forma predecible. Úsalas como pista, no como predicción.
- La comparación de ritmo con stints anteriores no corrige por la hora del día.
- Los datos de Apex Timing son públicos para espectadores, pero revisa sus condiciones de uso antes de redistribuirlos o publicar un servicio derivado.
