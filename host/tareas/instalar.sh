#!/usr/bin/env bash
# Instala las tareas de usuario de Cerbero. Con tu usuario, SIN sudo, desde el repo:
#   host/tareas/instalar.sh
# Necesita una sola vez la carpeta de informes (esa parte sí es con sudo):
#   sudo install -d -m 755 -o "$USER" -g "$USER" /var/lib/cerbero/tareas
# Es idempotente: se puede repetir para actualizar los scripts o las unidades.
set -euo pipefail

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

echo
echo "Listo. Para probar la limpieza ya (borra las imágenes sin usar de más de 7 días):"
echo "  systemctl --user start cerbero-limpieza.service && cat $DIR/limpieza-docker.json"
