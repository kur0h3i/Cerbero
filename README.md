<p align="center">
  <img src="assets/cerbero-logo.svg" alt="Logo de Cerbero: un perro de tres cabezas con collar de púas bajo la lluvia" width="128">
</p>

<h1 align="center">Cerbero</h1>

Monitor de seguridad y estado de **server-kuro**. Vigila tres frentes a la vez —recursos del
host, contenedores Docker y accesos SSH— y avisa por Telegram cuando algo va mal. No sustituye
a Netdata en métricas detalladas: es la capa propia que solo avisa de lo que importa, sobre
todo en seguridad. Expone una API REST que consume [Dis](https://github.com/kur0h3i/Dis).

> Cerbero es el perro de tres cabezas que guarda el tercer círculo del Infierno; aquí guarda
> el servidor ([por qué el nombre](#el-nombre-y-el-logo)).

> **En construcción.** Orden de implementación: núcleo (config, almacén de alertas y
> Telegram) → cabeza Recursos → cabeza Contenedores → cabeza Accesos → API → Docker.

## Qué vigila

### Cabeza 1 — Recursos

CPU, RAM y discos del host con `psutil`, cada `POLL_INTERVAL_S` segundos (30).

| Alerta | Nivel | Cuándo salta | Cuándo se resuelve |
|--------|-------|--------------|--------------------|
| CPU sostenida | 🟡 | La media del intervalo supera `CPU_THRESHOLD` en `CPU_SUSTAINED` lecturas seguidas (un pico suelto no cuenta) | Baja 5 puntos por debajo del umbral |
| RAM baja | 🔴 | La RAM disponible cae por debajo de `RAM_FREE_MIN_GB` | Sube 0.25 GB por encima del umbral |
| Disco lleno | 🟡 / 🔴 ≥ 95 % | Un punto de montaje de `DISKS` supera `DISK_THRESHOLD` | Baja 2 puntos por debajo del umbral |
| Disco ausente | 🟡 | Un punto de montaje no existe o no tiene su disco montado | Vuelve a estar montado |

- **RAM disponible, no "libre".** Se usa la memoria *disponible* (`MemAvailable`, lo que
  `free -h` llama *available*), que incluye la caché que el kernel libera cuando hace falta. La
  "libre" a secas no la cuenta y daría falsos críticos.
- **Discos sin montar.** Si un disco no se monta, su punto de montaje es un directorio vacío
  del disco raíz: medirlo daría el uso del raíz y lo que se escribiera ahí lo iría llenando. Por
  eso Cerbero comprueba que cada punto de montaje (salvo `/`) lo sea de verdad.
- **Márgenes de resolución.** Para dar una alerta por resuelta se exige un margen bajo el
  umbral, y así no salta y se resuelve con cada lectura.

## Alertas

Cada cabeza dispara alertas con uno de tres niveles: `info` 🟢, `warning` 🟡 y `critical` 🔴.
Llegan a Telegram con este formato:

```
🔴 [CRÍTICO] Cabeza: Recursos
Mensaje: RAM libre 0.8 GB (umbral 1.5 GB)
Servidor: server-kuro · 2026-09-30 03:14:22
```

```
🟢 [RESUELTO] Cabeza: Recursos
Mensaje: RAM libre recuperada (1.9 GB)
Servidor: server-kuro · 2026-09-30 03:44:05
```

- **Una alerta por condición.** Cada alerta tiene una clave (`recursos:ram`,
  `contenedores:caido:nginx`...). Mientras la condición dura, la alerta sigue activa y no se
  duplica.
- **Cooldown.** Una alerta con la misma clave no se reenvía hasta pasados
  `ALERT_COOLDOWN_MIN` minutos, aunque entre medias se resuelva y vuelva a saltar. Si el
  problema persiste, pasado ese tiempo llega un recordatorio con la hora de inicio.
- **Si empeora, avisa ya.** Si una alerta sube de nivel (de 🟡 a 🔴), se envía sin esperar al
  cooldown.
- **Resolución.** Cuando la condición se normaliza llega un 🟢 [RESUELTO], solo si la alerta
  original se había notificado.
- **Sin inundar el chat.** Como mucho se envían 20 mensajes por minuto; lo que no se envía queda
  igualmente en la API.
- **En memoria.** Las alertas activas se conservan todas y, de las resueltas, las últimas 50.
  Si Cerbero se reinicia, el historial se pierde.

## Variables de entorno

Todas son opcionales salvo las de Telegram (sin ellas las alertas solo quedan en el registro y
en la API). Una variable vacía equivale a no definirla.

| Variable | Por defecto | Qué hace |
|----------|-------------|----------|
| `TELEGRAM_TOKEN` | — | Token del bot (de @BotFather) |
| `TELEGRAM_CHAT_ID` | — | Chat al que se envían las alertas |
| `SERVER_NAME` | `server-kuro` | Nombre del servidor en los mensajes |
| `TZ` | `Europe/Madrid` | Zona horaria de las fechas |
| `ALERT_COOLDOWN_MIN` | `30` | Minutos antes de reenviar una alerta con la misma clave |
| `NOTIFY_STARTUP` | `true` | Aviso 🟢 al arrancar (sirve para enterarse de un reinicio) |
| `POLL_INTERVAL_S` | `30` | Segundos entre lecturas de recursos y contenedores |
| `CPU_THRESHOLD` | `85` | % de CPU a partir del cual una lectura es alta |
| `CPU_SUSTAINED` | `3` | Lecturas altas seguidas para alertar |
| `RAM_FREE_MIN_GB` | `1.5` | GB de RAM disponible por debajo de los cuales se alerta |
| `DISK_THRESHOLD` | `85` | % de uso de disco a partir del cual se alerta |
| `DISKS` | `/,/srv/archivos,/srv/extra` | Puntos de montaje vigilados, separados por comas |
| `HOST_ROOT` | `/` | Dónde ve Cerbero la raíz del host (`/hostfs` en Docker, ya puesto en el compose) |
| `LOG_LEVEL` | `INFO` | Nivel del registro |

## Desarrollo

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```

## El nombre y el logo

> *Cerbero, fiera crudele e diversa,*
> *con tre gole caninamente latra*
> *sovra la gente che quivi è sommersa.*
>
> — Dante, *Infierno*, VI, 13-15

Cerbero guarda el tercer círculo del *Infierno* de Dante, el de los golosos, y ladra con sus
tres gargantas a quien se acerca. Aquí cada cabeza vigila un frente: recursos, contenedores y
accesos. El logo, en la misma paleta que Dis y Caronte, dibuja el Canto VI:

- **Las tres cabezas** en el violeta de Dis, con el borde al rojo vivo.
- **«Li occhi ha vermigli»** (v. 16): los ojos de brasa.
- **El collar de púas**, del mismo fuego que las murallas de Dis.
- **La lluvia eterna** del tercer círculo, «grandine grossa, acqua tinta e neve» (v. 10), en
  el lila de la Estigia.

`assets/cerbero-logo.svg` es el logo sin fondo; `assets/cerbero.svg`, la variante con fondo
oscuro redondeado para usar como icono.
