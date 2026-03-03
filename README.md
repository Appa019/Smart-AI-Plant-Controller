# Hoya Pet - Smart AI Plant Controller

Sistema IoT inteligente de monitoramento e irrigacao automatica para plantas, com pet virtual que reage ao estado da planta.

## Sobre o Projeto

O Hoya Pet conecta um **ESP32-C3** com sensores de solo e ambiente a um servidor **FastAPI** na nuvem. Um dashboard web mostra dados em tempo real, e um **pet virtual** (gato ou cachorro) em pixel art reage ao estado da planta com frases e emocoes geradas por IA.

### Funcionalidades

- **Monitoramento em tempo real** — temperatura, umidade do ar e umidade do solo
- **Irrigacao inteligente** — bomba d'agua automatica com protecoes (cooldown, deteccao de reservatorio vazio)
- **Pet virtual com IA** — personagem pixel art que muda de expressao conforme a saude da planta
- **Identificacao de plantas** — tire uma foto e a IA identifica a especie
- **Multi-usuario** — sistema de login com JWT, cada usuario com ate 5 slots de plantas
- **Calibracao por usuario** — cada usuario pode calibrar seu sensor de solo
- **Dashboard responsivo** — interface web com graficos historicos e controle remoto da bomba

## Arquitetura

```
ESP32-C3 (sensores + bomba)
  |
  | HTTP POST /api/ingest (a cada 3s)
  v
FastAPI (servidor na nuvem)
  |-- SQLite (sensor_data.db)
  |-- /api/ingest    <-- recebe dados do ESP32
  |-- /api/commands  <-- ESP32 busca comandos remotos
  |-- /api/current   <-- dados atuais (JWT)
  |-- /api/history   <-- historico (JWT)
  |-- /api/login, /api/register, /api/verify <-- auth
  +-- frontend/dist/ <-- SPA servida pelo FastAPI
```

## Stack

| Camada | Tecnologia |
|--------|------------|
| Microcontrolador | ESP32-C3 SuperMini |
| Sensores | AHT10 (temp/umidade), Capacitivo v1.2 (solo) |
| Atuador | Mini bomba submersa 80-120L/h |
| Backend | Python 3.12, FastAPI, SQLite |
| Frontend | Vite, JavaScript vanilla |
| IA | OpenAI API (identificacao de plantas, geracao de imagens, frases) |
| Infra | Oracle Cloud (Always Free), Nginx, systemd |

## Setup

### Pre-requisitos

- Python 3.10+
- Node.js 18+
- Conta OpenAI com API key
- (Opcional) VPS para deploy em nuvem

### 1. Clonar e configurar

```bash
git clone https://github.com/Appa019/Smart-AI-Plant-Controller.git
cd Smart-AI-Plant-Controller

# Copiar e preencher variaveis de ambiente
cp .env.example .env
# Edite .env com suas credenciais
```

### 2. Backend

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev      # desenvolvimento (porta 5173)
npm run build    # build para producao (frontend/dist/)
```

### 4. ESP32

1. Abra `codigos_esp32/hoya_pet_firmware.ino` no Arduino IDE
2. Selecione a placa **ESP32C3 Dev Module**
3. Instale as bibliotecas: `Adafruit AHTX0`, `WiFi`, `HTTPClient`
4. Configure suas credenciais WiFi e IP do servidor no codigo
5. Faca upload para o ESP32-C3

### 5. Deploy (VPS)

Veja o guia completo em [`deploy/GUIA_DEPLOY.md`](deploy/GUIA_DEPLOY.md).

## Estrutura do Projeto

```
.
|-- server.py                  # Backend FastAPI (arquivo unico)
|-- requirements.txt           # Dependencias Python
|-- .env.example               # Template de variaveis de ambiente
|-- frontend/
|   |-- index.html             # SPA entry point
|   |-- src/
|   |   |-- main.js            # Logica do dashboard (JS vanilla)
|   |   +-- style.css          # Estilos
|   +-- vite.config.js
|-- codigos_esp32/
|   +-- hoya_pet_firmware.ino  # Firmware de producao do ESP32
|-- deploy/
|   |-- setup_vps.sh           # Script de deploy automatico
|   |-- nginx_hoya.conf        # Config Nginx
|   |-- esp32_cloud_firmware.ino
|   +-- GUIA_DEPLOY.md         # Guia passo a passo
+-- data/                      # Dados de usuarios (nao versionado)
```

## Licenca

Este projeto e de uso pessoal/educacional.
