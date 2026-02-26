#!/bin/bash
# === NexusAgent — Setup & Deploy ===
# Uso: curl/clone + bash setup.sh
set -e

echo "🚀 NexusAgent — Instalación automática"
echo "========================================"

# 1. Python venv + deps
echo "📦 Instalando dependencias..."
apt-get update -qq && apt-get install -y -qq python3.12 python3.12-venv python3-pip > /dev/null 2>&1
python3.12 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt

# 2. Playwright (browser search)
echo "🌐 Instalando Playwright..."
playwright install chromium > /dev/null 2>&1
playwright install-deps > /dev/null 2>&1

# 3. .env — solo si no existe
if [ ! -f .env ]; then
    echo "⚙️  Creando .env desde template..."
    cp .env.example .env
    echo ""
    echo "⚠️  IMPORTANTE: Editá .env con tus datos:"
    echo "   nano .env"
    echo ""
    echo "   Mínimo configurar:"
    echo "   - TELEGRAM_BOT_TOKEN"
    echo "   - OLLAMA_MODEL (el que tengas instalado)"
    echo ""
else
    echo "✅ .env ya existe, no se toca"
fi

# 4. Servicio systemd (24/7)
echo "🔧 Configurando servicio systemd..."
WORKDIR=$(pwd)
cat > /etc/systemd/system/nexus-agent.service << EOF
[Unit]
Description=NexusAgent AI Framework
After=network.target ollama.service
Wants=ollama.service

[Service]
Type=simple
User=root
WorkingDirectory=${WORKDIR}
Environment=PATH=${WORKDIR}/venv/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=${WORKDIR}/.env
ExecStart=${WORKDIR}/venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable nexus-agent > /dev/null 2>&1

echo ""
echo "✅ Instalación completa!"
echo ""
echo "Comandos:"
echo "  nano .env                          # configurar tokens"
echo "  systemctl start nexus-agent        # arrancar"
echo "  systemctl status nexus-agent       # ver estado"
echo "  journalctl -u nexus-agent -f       # ver logs"
echo "  systemctl restart nexus-agent      # reiniciar"
echo ""
