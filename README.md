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

### Cabeza 2 — Contenedores

Estado de los contenedores con el SDK oficial de Docker, cada `POLL_INTERVAL_S` segundos. Solo
lee: nunca arranca, para ni reinicia nada.

| Alerta | Nivel | Cuándo salta | Cuándo se resuelve |
|--------|-------|--------------|--------------------|
| Caída | 🔴 | Un contenedor que Cerbero vio en marcha pasa a `exited`/`dead` con un código de error, o lo mata el kernel por falta de memoria (OOM) | Vuelve a estar en marcha |
| Parada | 🟡 | Igual, pero por un `docker stop`/`compose stop`/`down` o con salida limpia (código 0 o 143) | Vuelve a estar en marcha |
| Bucle de reinicios | 🔴 | Más de `RESTART_LOOP_THRESHOLD` reinicios en `RESTART_LOOP_WINDOW_MIN` minutos | Una ventana entera sin reinicios |
| Unhealthy | 🟡 | Su healthcheck lo marca `unhealthy` (con la salida de la última comprobación) | Vuelve a `healthy` |
| Docker no responde | 🔴 | El socket de Docker falla dos lecturas seguidas | Docker responde de nuevo |

- **Los parados de antes no cuentan.** Los contenedores que ya estaban parados cuando arrancó
  Cerbero no generan alerta; solo los que caen mientras Cerbero está corriendo.
- **Parada manual vs. caída.** El código de salida no basta para distinguirlas: un proceso
  que ignora SIGTERM sale con 137 también en un `docker stop`. Por eso Cerbero consulta los
  eventos `stop` de Docker entre lectura y lectura: si hubo uno, es una parada a mano (🟡); un
  `docker kill` o un fallo del proceso es una caída (🔴).
- **Recordatorios.** Mientras un contenedor siga caído, la alerta crítica se recuerda tras el
  cooldown; una parada a mano se avisa una sola vez.
- Si se borra un contenedor, sus alertas se cierran. `CONTAINERS_IGNORE` excluye contenedores
  por nombre (p. ej. tareas puntuales que terminan solas).

### Cabeza 3 — Accesos

Sigue `/var/log/auth.log` como `tail -f` (lee solo lo nuevo, nunca el fichero entero) en un
hilo aparte, y analiza las líneas de sshd con expresiones regulares. No depende de fail2ban.

| Alerta | Nivel | Cuándo salta | Cuándo se resuelve |
|--------|-------|--------------|--------------------|
| Fuerza bruta | 🔴 IP externa / 🟡 IP de confianza | Más de `BRUTE_FORCE_THRESHOLD` fallos de login desde una IP en `BRUTE_FORCE_WINDOW_MIN` minutos | Una ventana entera sin fallos desde esa IP |
| Login externo | 🔴 | Un login SSH aceptado desde fuera de los rangos de confianza | Se cierra solo pasado el cooldown (es un evento, no una condición) |
| Intentos externos | 🟡 | Intentos fallidos con un usuario válido (que existe) desde fuera de los rangos de confianza | Una ventana entera sin intentos |
| Log ilegible | 🟡 | `auth.log` no existe o no se puede leer: la cabeza está ciega | Vuelve a poder leerse |

Rangos de confianza (`TRUSTED_NETWORKS`, definidos en `app/config.py`): LAN
`192.168.1.0/24`, WireGuard `10.0.0.0/24` y Tailscale `100.64.0.0/10`. El loopback siempre
es de confianza.

- **Qué es un fallo.** Cada línea `Failed <método>` y, si una conexión no tuvo ninguna, su
  cierre antes de autenticarse (`Connection closed by authenticating user ...`), que es lo
  único que deja un intento fallido con clave pública. No se cuenta el sondeo `Failed none` que
  hacen los clientes al empezar.
- **Botnets.** Los intentos externos con usuario válido van en una sola alerta agregada (con
  cuántos intentos y desde cuántas IPs), no en una por IP. La memoria está acotada: como mucho
  se siguen 5000 IPs a la vez.
- **Un usuario no puede falsear la IP.** El nombre de usuario lo elige quien se conecta y puede
  contener, p. ej., `from 192.168.1.5 port 1`. El parser se queda con la última IP de la línea,
  que es la que escribe sshd. Las líneas de otros programas que imitan a sshd se ignoran.
- **Rotación.** Se detecta tanto la rotación con fichero nuevo (lo normal en logrotate) como
  `copytruncate`. Al arrancar se empieza por el final: el histórico no genera alertas.
- **Formato de Debian 13.** OpenSSH 10 registra como `sshd-session` en vez de `sshd`, y
  rsyslog usa fechas RFC 3339; se aceptan ambos formatos. Para comprobar qué escribe tu
  servidor: `grep -E 'sshd(-session)?\[' /var/log/auth.log | tail`.
- **Hace falta rsyslog.** Debian 13 no lo instala por defecto (todo va a journald) y entonces
  `/var/log/auth.log` no existe. Cerbero lo avisa con la alerta *Log ilegible*; se arregla con
  `sudo apt install rsyslog`.

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
| `RESTART_LOOP_THRESHOLD` | `5` | Reinicios (más de) que cuentan como bucle |
| `RESTART_LOOP_WINDOW_MIN` | `10` | Ventana en minutos para contar reinicios |
| `CONTAINERS_IGNORE` | — | Contenedores que no generan alertas, separados por comas |
| `AUTH_LOG` | `/var/log/auth.log` | Log de autenticación del host |
| `BRUTE_FORCE_THRESHOLD` | `10` | Fallos (más de) desde una IP que cuentan como fuerza bruta |
| `BRUTE_FORCE_WINDOW_MIN` | `5` | Ventana en minutos para contar fallos |
| `TRUSTED_NETWORKS` | `192.168.1.0/24,10.0.0.0/24,100.64.0.0/10` | Rangos de confianza, separados por comas (sustituyen a los de `app/config.py`) |
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
