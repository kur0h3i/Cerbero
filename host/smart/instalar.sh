#!/usr/bin/env bash
# Instala el volcado SMART de Cerbero en el host. Ejecutar con sudo, desde el repo:
#   sudo host/smart/instalar.sh
# Es idempotente: se puede repetir para actualizar el script o las unidades.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Ejecuta con sudo: sudo $0" >&2; exit 1; }
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Falta python3" >&2; exit 1; }

echo "==> smartmontools"
command -v smartctl >/dev/null || apt-get install -y smartmontools

echo "==> Carpetas y script"
install -d -m 755 /usr/local/lib/cerbero /etc/cerbero /var/lib/cerbero /var/lib/cerbero/smart
install -m 755 cerbero_smart.py /usr/local/lib/cerbero/cerbero_smart.py

if [[ ! -f /etc/cerbero/discos.conf ]]; then
  echo "==> No hay /etc/cerbero/discos.conf: lo genero a partir de los discos actuales"
  python3 /usr/local/lib/cerbero/cerbero_smart.py --detectar > /etc/cerbero/discos.conf
fi
echo "--- /etc/cerbero/discos.conf ---"
cat /etc/cerbero/discos.conf
echo "---"

echo "==> Timer"
install -m 644 cerbero-smart.service cerbero-smart.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cerbero-smart.timer

echo "==> Primera lectura"
systemctl start cerbero-smart.service || true
journalctl -u cerbero-smart.service -n 10 --no-pager -o cat
ls -l /var/lib/cerbero/smart
echo
echo "Listo. Revisa discos.conf (nombres, montajes, «opcional») y, si lo cambias:"
echo "  sudo systemctl start cerbero-smart.service"
