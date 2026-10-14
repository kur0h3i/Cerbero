#!/usr/bin/env bash
# Instala las tareas de usuario de Cerbero. Con tu usuario, SIN sudo, desde el repo:
#   host/tareas/instalar.sh [--cola-json RUTA]
#
# --cola-json RUTA  el JSON de estado que reescribe cola-tareas en cada ejecución;
#                   si se indica, se avisa también cuando el servicio termina bien
#                   pero no lo actualiza.
# Necesita una sola vez la carpeta de informes (esa parte sí es con sudo):
#   sudo install -d -m 755 -o "$USER" -g "$USER" /var/lib/cerbero/tareas
# Es idempotente: se puede repetir para actualizar los scripts o las unidades.
set -euo pipefail

COLA_JSON=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --cola-json) COLA_JSON=${2:?falta la ruta}; shift 2 ;;
    *) echo "Opción desconocida: $1" >&2; exit 1 ;;
  esac
done

[[ $EUID -ne 0 ]] || { echo "Ejecútalo con tu usuario, sin sudo." >&2; exit 1; }
cd "$(dirname "$0")"
DIR=/var/lib/cerbero/tareas
LIB="$HOME/.local/lib/cerbero"
UNITS="$HOME/.config/systemd/user"

if [[ ! -w $DIR ]]; then
  echo "Falta $DIR con permiso de escritura para $USER. Créalo una vez con:" >&2
  echo "  sudo install -d -m 755 -o $USER -g $USER $DIR" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Aviso: $USER no puede hablar con Docker (¿está en el grupo docker?)." >&2
fi
if [[ $(loginctl show-user "$USER" -P Linger 2>/dev/null) != yes ]]; then
  echo "Aviso: sin linger los timers de usuario solo corren con la sesión abierta:" >&2
  echo "  sudo loginctl enable-linger $USER" >&2
fi

echo "==> Scripts en $LIB"
install -d "$LIB" "$UNITS"
install -m 755 informe_tarea.py cerbero_limpieza.py "$LIB/"

echo "==> Limpieza semanal de imágenes"
install -m 644 cerbero-limpieza.service cerbero-limpieza.timer "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now cerbero-limpieza.timer
systemctl --user list-timers cerbero-limpieza.timer --no-pager

if systemctl --user cat cola-tareas.service >/dev/null 2>&1; then
  echo "==> Informe de cola-tareas (drop-in, la unidad no se toca)"
  DROPIN="$UNITS/cola-tareas.service.d"
  ARGS="systemd --tarea cola-tareas --cada 1800"
  if [[ -n $COLA_JSON ]]; then
    [[ -e $COLA_JSON ]] || echo "Aviso: $COLA_JSON no existe todavía." >&2
    ARGS+=" --fichero $COLA_JSON"
  fi
  install -d "$DROPIN"
  cat > "$DROPIN/cerbero.conf" <<CONF
# Cerbero: informe de cada ejecución (también si falla). Para quitarlo, borra este fichero.
[Service]
ExecStopPost=/usr/bin/python3 %h/.local/lib/cerbero/informe_tarea.py $ARGS
CONF
  systemctl --user daemon-reload
  cat "$DROPIN/cerbero.conf"
else
  echo "==> No hay cola-tareas.service: me salto su informe"
fi

echo
echo "Listo. Para probar la limpieza ya (borra las imágenes sin usar de más de 7 días):"
echo "  systemctl --user start cerbero-limpieza.service && cat $DIR/limpieza-docker.json"
echo "El informe de cola-tareas aparecerá tras su próxima ejecución (como mucho 30 min):"
echo "  cat $DIR/cola-tareas.json"
