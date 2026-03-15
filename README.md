# Hoya Pet - Smart AI Plant Controller
**Link do Site: https://projetoirrigacao.vercel.app/**

Sistema IoT inteligente de monitoramento e irrigacao automatica para plantas, com pet virtual que reage ao estado da planta.

## Sobre o Projeto

O Hoya Pet conecta um **ESP32-C3** com sensores de solo e ambiente a um backend **FastAPI** na nuvem. Um dashboard web mostra dados em tempo real, e um **pet virtual** (gato ou cachorro) em pixel art reage ao estado da planta com frases e emocoes geradas por IA.

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
FastAPI (Vercel Serverless)
  |-- Supabase PostgreSQL (sensor_readings, users, plant_slots)
  |-- Supabase Storage (plant photos, pet images)
  |-- /api/ingest    <-- recebe dados do ESP32
  |-- /api/commands  <-- ESP32 busca comandos remotos
  |-- /api/current   <-- dados atuais (JWT)
  |-- /api/history   <-- historico (JWT)
  |-- /api/login, /api/register, /api/verify <-- auth
  +-- Vercel CDN     <-- frontend estatico
```

## Stack

| Camada | Tecnologia |
|--------|------------|
| Microcontrolador | ESP32-C3 SuperMini |
| Sensores | AHT10 (temp/umidade), Capacitivo v1.2 (solo) |
| Atuador | Mini bomba submersa 80-120L/h |
| Backend | Python 3.12, FastAPI, Supabase (PostgreSQL + Storage) |
| Frontend | Vite, JavaScript vanilla |
| IA | OpenAI API (identificacao de plantas, geracao de imagens, frases) |
| Infra | Vercel (frontend + serverless API), Supabase (DB + Storage) |

## Setup

### Pre-requisitos

- Python 3.10+
- Node.js 18+
- Conta OpenAI com API key
- Conta Supabase (free tier)
- Conta Vercel (free tier)

### 1. Clonar e configurar

```bash
git clone https://github.com/Appa019/Smart-AI-Plant-Controller.git
cd Smart-AI-Plant-Controller

# Copiar e preencher variaveis de ambiente
cp .env.example .env
# Edite .env com suas credenciais
```

### 2. Supabase

1. Crie um projeto no [Supabase](https://supabase.com)
2. Execute o schema SQL no SQL Editor (veja `supabase/schema.sql`)
3. Crie os Storage buckets: `plant-photos`, `pet-images`, `pet-references`
4. Copie a URL e Service Role Key para o `.env`

### 3. Backend (desenvolvimento local)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn api.index:app --host 0.0.0.0 --port 8000 --reload
```

### 4. Frontend

```bash
cd frontend
npm install
npm run dev      # desenvolvimento (porta 5173)
npm run build    # build para producao (frontend/dist/)
```

### 5. ESP32

1. Abra `codigos_esp32/hoya_pet_firmware.ino` no Arduino IDE
2. Selecione a placa **ESP32C3 Dev Module**
3. Instale as bibliotecas: `Adafruit AHTX0`, `WiFi`, `HTTPClient`
4. Configure suas credenciais WiFi e URL do servidor no codigo
5. Faca upload para o ESP32-C3

### 6. Deploy (Vercel)

```bash
npm i -g vercel
vercel --prod
```

Configure as variaveis de ambiente no dashboard Vercel (veja `.env.example`).

## Estrutura do Projeto

```
.
|-- api/
|   +-- index.py                  # Backend FastAPI (Vercel serverless)
|-- requirements.txt              # Dependencias Python
|-- vercel.json                   # Config Vercel (build, rewrites, crons)
|-- .env.example                  # Template de variaveis de ambiente
|-- frontend/
|   |-- index.html                # SPA entry point
|   |-- src/
|   |   |-- main.js               # Logica do dashboard (JS vanilla)
|   |   +-- style.css             # Estilos
|   +-- vite.config.js
|-- codigos_esp32/
|   +-- hoya_pet_firmware.ino     # Firmware de producao do ESP32
+-- supabase/
    +-- schema.sql                # Schema PostgreSQL para Supabase
```

## Licenca

Este projeto e de uso pessoal/educacional.
