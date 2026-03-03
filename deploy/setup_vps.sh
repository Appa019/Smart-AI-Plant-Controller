#!/bin/bash
# ============================================================
# Hoya Pet App — Script de Deploy para VPS (Oracle Cloud)
# ============================================================
# Uso: Copie este projeto para a VPS e rode:
#   bash deploy/setup_vps.sh
# ============================================================

set -e

echo ""
echo "============================================"
echo "  Hoya Pet App — Setup VPS"
echo "============================================"
echo ""

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "[INFO] Diretorio do projeto: $PROJECT_DIR"

# ==================== 1. ATUALIZACAO DO SISTEMA ====================
echo ""
echo "[1/8] Atualizando sistema..."
sudo apt update && sudo apt upgrade -y

# ==================== 2. INSTALAR DEPENDENCIAS ====================
echo ""
echo "[2/8] Instalando dependencias..."
sudo apt install -y python3 python3-pip python3-venv nginx curl

# Instalar Node.js 20 via NodeSource
if ! command -v node &> /dev/null; then
    echo "Instalando Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt install -y nodejs
fi

echo "  Python: $(python3 --version)"
echo "  Node: $(node --version)"
echo "  npm: $(npm --version)"

# ==================== 3. PYTHON VENV ====================
echo ""
echo "[3/8] Configurando Python virtualenv..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# ==================== 4. DATA DIRECTORY ====================
echo ""
echo "[4/8] Criando diretorio de dados..."
mkdir -p data

# ==================== 5. ENV FILE ====================
echo ""
echo "[5/8] Verificando .env..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "  ATENCAO: .env criado a partir de .env.example — edite com suas credenciais!"
    else
        echo "  ATENCAO: .env nao encontrado! Crie o arquivo manualmente."
    fi
else
    echo "  .env ja existe"
fi

# ==================== 6. BUILD FRONTEND ====================
echo ""
echo "[6/8] Building frontend..."
cd frontend
npm install
npm run build
cd ..

echo "  Frontend build: $(ls -la frontend/dist/index.html 2>/dev/null && echo 'OK' || echo 'FALHOU')"

# ==================== 7. NGINX ====================
echo ""
echo "[7/8] Configurando nginx..."
sudo cp deploy/nginx_hoya.conf /etc/nginx/sites-available/hoya
sudo ln -sf /etc/nginx/sites-available/hoya /etc/nginx/sites-enabled/hoya
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx

# ==================== 8. SYSTEMD SERVICE ====================
echo ""
echo "[8/8] Criando servico systemd..."

PYTHON_PATH="$PROJECT_DIR/.venv/bin/python3"
UVICORN_PATH="$PROJECT_DIR/.venv/bin/uvicorn"

sudo tee /etc/systemd/system/hoya-server.service > /dev/null << EOF
[Unit]
Description=Hoya Pet App API Server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$UVICORN_PATH server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$PROJECT_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable hoya-server
sudo systemctl restart hoya-server

# Firewall
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || true
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT 2>/dev/null || true

if command -v netfilter-persistent &> /dev/null; then
    sudo netfilter-persistent save
fi

echo ""
echo "============================================"
echo "  Deploy concluido com sucesso!"
echo "============================================"

PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "???")
echo "  Acesse: http://$PUBLIC_IP"
echo "  ESP32:  http://$PUBLIC_IP/api/ingest"
echo ""
echo "  Comandos uteis:"
echo "    sudo systemctl status hoya-server"
echo "    sudo systemctl restart hoya-server"
echo "    sudo journalctl -u hoya-server -f"
echo "============================================"
