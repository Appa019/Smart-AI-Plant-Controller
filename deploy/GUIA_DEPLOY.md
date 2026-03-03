# Hoya Dashboard — Guia de Deploy Oracle Cloud

## Resumo dos Arquivos

| Arquivo | Descricao |
|---------|-----------|
| `deploy/setup_vps.sh` | Script que instala tudo na VPS automaticamente |
| `deploy/nginx_hoya.conf` | Configuracao do Nginx (reverse proxy) |
| `deploy/esp32_cloud_firmware.ino` | Firmware atualizado do ESP32 com envio para cloud |
| `server.py` | Endpoint POST `/api/ingest` (ja adicionado) |

---

## Passo a Passo

### 1. VM Oracle Cloud Criada ✅
- Shape: VM.Standard.E2.1.Micro (Always Free)
- Ubuntu 22.04

### 2. Adicionar IP Publico (apos VM estar Running)
1. Na pagina da instancia, clique em **"Networking"** (aba)
2. Clique no **VNIC** listado
3. Em **IPv4 Addresses**, clique nos **3 pontinhos** ao lado do IP privado
4. Selecione **"Edit"**
5. Em "Public IPv4 address", selecione **"Ephemeral public IP"** ou reserve um
6. Salve — anote o IP publico!

### 3. Abrir Portas no Security List da Oracle Cloud
> ⚠️ IMPORTANTE: O firewall da Oracle Cloud bloqueia tudo por padrao!

1. Va em **Networking > Virtual Cloud Networks**
2. Clique na VCN criada (`vcn-20260210-2323`)
3. Clique na **subnet**
4. Clique na **Security List** (Default)
5. Clique **"Add Ingress Rules"** e adicione:

| Source CIDR | Protocol | Dest Port | Descricao |
|-------------|----------|-----------|-----------|
| `0.0.0.0/0` | TCP | 80 | HTTP (Dashboard) |
| `0.0.0.0/0` | TCP | 443 | HTTPS (futuro) |
| `0.0.0.0/0` | TCP | 8000 | FastAPI direto (backup) |

### 4. Conectar via SSH
```bash
# Dar permissao ao arquivo de chave
chmod 600 ~/Downloads/ssh-key-*.key

# Conectar (substitua SEU_IP pelo IP publico da VM)
ssh -i ~/Downloads/ssh-key-*.key ubuntu@SEU_IP
```

### 5. Enviar Projeto para a VPS
```bash
# Do seu computador local, envie o projeto:
scp -i ~/Downloads/ssh-key-*.key -r \
  ~/projeto_irrigacao \
  ubuntu@SEU_IP:~/projeto_irrigacao
```

### 6. Rodar o Script de Deploy na VPS
```bash
# Na VPS (via SSH):
cd ~/projeto_irrigacao
bash deploy/setup_vps.sh
```

O script instala tudo automaticamente:
- Python 3, pip, venv
- Node.js 20
- Nginx
- Build do frontend
- Servico systemd `hoya-server`
- Regras de firewall

### 7. Atualizar Firmware do ESP32
1. Abra `deploy/esp32_cloud_firmware.ino` no Arduino IDE
2. Altere a linha `CLOUD_SERVER` para o IP publico da VPS:
   ```cpp
   const char* CLOUD_SERVER = "http://SEU_IP:8000/api/ingest";
   ```
3. Faca upload para o ESP32

### 8. Verificar
- Dashboard: `http://SEU_IP` (via nginx)
- API: `http://SEU_IP:8000/api/current`
- Testar ingestao manual:
  ```bash
  curl -X POST http://SEU_IP:8000/api/ingest \
    -H "Content-Type: application/json" \
    -H "X-API-Key: YOUR_INGEST_API_KEY" \
    -d '{"temperature":25.5,"humidity":60.0,"soil_moisture":35,"soil_raw":2100,"soil_status":"IDEAL"}'
  ```

---

## Arquitetura Final

```
ESP32 (sua casa)
  │
  │ HTTP POST /api/ingest (a cada 5s)
  │ (via internet)
  ▼
Oracle Cloud VPS (24/7)
  ├── Nginx (porta 80) ──► FastAPI (porta 8000)
  │                          ├── /api/ingest  ← recebe dados ESP32
  │                          ├── /api/current ← dados atuais
  │                          ├── /api/history ← historico
  │                          └── /api/stats   ← estatisticas
  │
  ├── SQLite (sensor_data.db)
  │
  └── Frontend (Vite build) ← servido pelo FastAPI StaticFiles

Voce (qualquer lugar)
  │
  └── Navegador ──► http://IP_VPS ──► Dashboard completo
```
