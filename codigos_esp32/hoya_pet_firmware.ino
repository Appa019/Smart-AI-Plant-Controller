/*
 * ============================================================
 *  HOYA PET — Firmware ESP32-C3 com Irrigacao Inteligente
 * ============================================================
 *
 *  Componentes:
 *    - ESP32-C3 SuperMini
 *    - AHT10 (I2C: SDA=GPIO8, SCL=GPIO9)
 *    - Sensor solo capacitivo v1.2 (GPIO4, ADC 12-bit)
 *    - Mini bomba submersa 80-120L/h (GPIO3, via rele)
 *
 *  Funcionalidades:
 *    - Leitura de sensores a cada 5s
 *    - Envio para Cloud (Vercel) via HTTP POST
 *    - Irrigacao inteligente com protecoes:
 *      > Espera minimo 1 hora apos regar para re-avaliar solo
 *      > Pulsos curtos (~2-3s) para evitar excesso
 *      > Se nao houver melhora em 5h, alerta reservatorio vazio
 *      > Nunca rega se solo esta ideal ou acima
 *    - Servidor web local para debug
 *
 *  Arduino IDE: Placa "ESP32C3 Dev Module"
 *  Bibliotecas: Adafruit AHTX0, WiFi, HTTPClient
 *
 *  Calibracao sensor solo (medido em 2026-02):
 *    Seco no ar : ADC ~2970
 *    Na agua    : ADC ~1320
 *    Range util : ~1650 ADC units
 * ============================================================
 */

#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_AHTX0.h>

// ==================== CONFIGURACAO WIFI ====================
// >>> ALTERE PARA SUA REDE <<<
const char* ssid     = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

// IP fixo na rede local (ajuste para sua rede)
IPAddress local_IP(192, 168, 101, 200);
IPAddress gateway(192, 168, 101, 1);
IPAddress subnet(255, 255, 255, 0);
IPAddress primaryDNS(8, 8, 8, 8);
IPAddress secondaryDNS(1, 1, 1, 1);

// ==================== CLOUD SERVER ====================
// >>> ALTERE PARA A URL DO SEU DEPLOY VERCEL <<<
const char* CLOUD_SERVER  = "https://YOUR_VERCEL_URL/api/ingest";
const char* CLOUD_API_KEY = "YOUR_INGEST_API_KEY";

// Credenciais HTTP Basic Auth para o servidor web local (endpoint /pump)
#define BASIC_USER "YOUR_ESP32_USER"
#define BASIC_PASS "YOUR_ESP32_PASS"
const unsigned long CLOUD_SEND_INTERVAL = 3000; // 3 segundos

// ==================== PINOS ====================
#define SOIL_SENSOR_PIN 4
#define PUMP_PIN        3
#define I2C_SDA         8
#define I2C_SCL         9

// ==================== CALIBRACAO SOLO (ADC DIRETO) ====================
// Valores medidos com o sensor capacitivo v1.2 no ESP32-C3 (ADC 12-bit):
//   Seco no ar : ADC ~2970  ->   0%
//   Na agua    : ADC ~1320  -> 100%
//   Range util : ~1650 ADC units
//
// Zonas (proporcional ao range real):
//   ADC >= 2970 -> Muito seco  (regar agora)
//   ADC >= 2720 -> Seco        (regar)
//   ADC >= 2475 -> Ficando seco (atencao)
//   ADC >= 1860 -> Ideal/Umido
//   ADC >= 1820 -> Muito umido  (nao regar)
//   ADC <  1820 -> Encharcado   (verificar drenagem)
const int ADC_SOAKED    = 1820;  // abaixo = encharcado
const int ADC_WET       = 1860;  // 1820-1860 = muito umido
const int ADC_IDEAL_MAX = 2475;  // 1860-2475 = ideal
const int ADC_DRY       = 2720;  // 2475-2720 = seco, regar!
const int ADC_VERY_DRY  = 2970;  // acima = muito seco

// Conversao para % (para display e envio ao cloud)
const int ADC_CAL_DRY = 2970;  // ADC = 0%  (seco no ar)
const int ADC_CAL_WET = 1320;  // ADC = 100% (na agua)
// Tabela de referencia:
//   ADC 2970 ->   0%  (seco no ar)
//   ADC 2720 ->  15%  (muito seco - regar)
//   ADC 2475 ->  30%  (ficando seco)
//   ADC 1860 ->  67%  (ideal/umido)
//   ADC 1820 ->  70%  (muito umido)
//   ADC 1320 -> 100%  (encharcado/agua)

// ==================== SUAVIZACAO (MEDIA MOVEL) ====================
// Janela de 30 leituras elimina spikes espurios
const int SMOOTH_WINDOW = 30;
int soilADCBuffer[SMOOTH_WINDOW];
int soilBufferIndex = 0;
bool soilBufferFull = false;

// ==================== IRRIGACAO INTELIGENTE ====================
// Bomba: 80-120L/h ~= 22-33ml/s.
// 2 segundos = ~44-66ml (pulso curto para bomba potente)
const unsigned long PUMP_PULSE_MS = 2000;            // 2 segundos
const unsigned long MIN_WAIT_AFTER_WATER = 3600000;  // 1 hora em ms
const unsigned long RESERVOIR_ALERT_MS = 18000000;   // 5 horas em ms
const unsigned long IRRIGATION_CHECK_INTERVAL = 60000; // verifica a cada 60s
const int MAX_CONSECUTIVE_PULSES = 3; // max pulsos sem ver melhora = alerta

// ==================== OBJETOS ====================
Adafruit_AHTX0 aht;
WebServer server(80);

// ==================== ESTADO ====================
// Cloud
unsigned long lastCloudSend = 0;
unsigned long cloudSuccessCount = 0;
unsigned long cloudFailCount = 0;

// Irrigacao
unsigned long lastWateringTime = 0;
unsigned long firstWateringAttempt = 0;
int soilBeforeWatering = -1;
int consecutivePulsesNoImprovement = 0;
bool reservoirAlertActive = false;
bool pumpActive = false;
unsigned long lastIrrigationCheck = 0;

// Leituras atuais (para envio e display)
float currentTemp = 0;
float currentHumidity = 0;
int currentSoilPercent = 0;
int currentSoilRaw = 0;
int currentSoilSmoothed = 0;
String currentSoilStatus = "";
String currentIrrigationStatus = "Aguardando primeira leitura";

// ==================== FUNCOES SENSOR SOLO ====================
int readSoilRaw() {
    return analogRead(SOIL_SENSOR_PIN);
}

void addSoilReading(int raw) {
    soilADCBuffer[soilBufferIndex] = raw;
    soilBufferIndex = (soilBufferIndex + 1) % SMOOTH_WINDOW;
    if (soilBufferIndex == 0) soilBufferFull = true;
}

int getSmoothedADC() {
    int count = soilBufferFull ? SMOOTH_WINDOW : soilBufferIndex;
    if (count == 0) return readSoilRaw();
    long sum = 0;
    for (int i = 0; i < count; i++) sum += soilADCBuffer[i];
    return (int)(sum / count);
}

int adcToPercent(int adc) {
    int pct = map(adc, ADC_CAL_DRY, ADC_CAL_WET, 0, 100);
    return constrain(pct, 0, 100);
}

String getSoilStatusFromADC(int adc) {
    if (adc >= ADC_VERY_DRY) return "Muito seco - regar agora";
    if (adc >= ADC_DRY)      return "Seco - regar";
    if (adc >= ADC_IDEAL_MAX) return "Ficando seco - atencao";
    if (adc >= ADC_WET)      return "Umido - ideal";
    if (adc >= ADC_SOAKED)   return "Muito umido - nao regar";
    return "Encharcado - verificar drenagem";
}

// ==================== IRRIGACAO INTELIGENTE ====================
void checkIrrigation() {
    unsigned long now = millis();

    if (now - lastIrrigationCheck < IRRIGATION_CHECK_INTERVAL) return;
    lastIrrigationCheck = now;

    if (pumpActive) return;

    int adcNow = currentSoilSmoothed;

    if (!soilBufferFull) {
        currentIrrigationStatus = "Calibrando sensor (" + String(soilBufferIndex) + "/" + String(SMOOTH_WINDOW) + ")";
        return;
    }

    // --- CASO 1: Solo umido ou ideal -> nada a fazer ---
    if (adcNow < ADC_DRY) {
        currentIrrigationStatus = "Solo adequado - sem irrigacao";
        if (consecutivePulsesNoImprovement > 0) {
            consecutivePulsesNoImprovement = 0;
            reservoirAlertActive = false;
            firstWateringAttempt = 0;
        }
        return;
    }

    // --- CASO 2: Solo seco mas regou recentemente -> esperar ---
    if (lastWateringTime > 0 && (now - lastWateringTime < MIN_WAIT_AFTER_WATER)) {
        unsigned long remainMin = (MIN_WAIT_AFTER_WATER - (now - lastWateringTime)) / 60000;
        currentIrrigationStatus = "Aguardando absorcao (" + String(remainMin) + "min restantes)";
        return;
    }

    // --- CASO 3: Alerta de reservatorio -> nao rega mais ---
    if (reservoirAlertActive) {
        currentIrrigationStatus = "ALERTA: verificar reservatorio de agua";
        return;
    }

    // --- CASO 4: Verificar se tentativas anteriores funcionaram ---
    if (soilBeforeWatering >= 0 && lastWateringTime > 0) {
        if (adcNow >= soilBeforeWatering) {
            consecutivePulsesNoImprovement++;
            Serial.print("[IRRIGACAO] Solo nao melhorou. ADC=");
            Serial.print(adcNow);
            Serial.print(" (antes=");
            Serial.print(soilBeforeWatering);
            Serial.print("). Tentativas: ");
            Serial.println(consecutivePulsesNoImprovement);

            if (firstWateringAttempt > 0 && (now - firstWateringAttempt > RESERVOIR_ALERT_MS)) {
                reservoirAlertActive = true;
                currentIrrigationStatus = "ALERTA: verificar reservatorio de agua";
                Serial.println("[IRRIGACAO] ALERTA: 5h+ sem melhora. Reservatorio vazio?");
                return;
            }

            if (consecutivePulsesNoImprovement >= MAX_CONSECUTIVE_PULSES) {
                reservoirAlertActive = true;
                currentIrrigationStatus = "ALERTA: verificar reservatorio de agua";
                Serial.println("[IRRIGACAO] ALERTA: Max tentativas sem melhora.");
                return;
            }
        } else {
            consecutivePulsesNoImprovement = 0;
            Serial.print("[IRRIGACAO] Solo melhorou: ADC ");
            Serial.print(soilBeforeWatering);
            Serial.print(" -> ");
            Serial.println(adcNow);
        }
    }

    // --- CASO 5: Solo seco -> regar ---
    Serial.print("[IRRIGACAO] Solo seco (ADC=");
    Serial.print(adcNow);
    Serial.println("). Ativando bomba...");

    soilBeforeWatering = adcNow;
    if (firstWateringAttempt == 0) firstWateringAttempt = now;
    activatePump();
}

void activatePump() {
    pumpActive = true;
    currentIrrigationStatus = "Irrigando...";

    digitalWrite(PUMP_PIN, HIGH);
    Serial.print("[BOMBA] LIGADA por ");
    Serial.print(PUMP_PULSE_MS);
    Serial.println("ms");

    delay(PUMP_PULSE_MS);

    digitalWrite(PUMP_PIN, LOW);
    pumpActive = false;
    lastWateringTime = millis();

    currentIrrigationStatus = "Irrigou - aguardando absorcao (1h)";
    Serial.println("[BOMBA] DESLIGADA. Aguardando 1h para re-avaliar.");
}

// ==================== ENVIO CLOUD ====================
void sendToCloud() {
    if (WiFi.status() != WL_CONNECTED) return;

    HTTPClient http;
    http.begin(CLOUD_SERVER);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-API-Key", CLOUD_API_KEY);
    http.setTimeout(5000);

    String json = "{";
    json += "\"temperature\":" + String(currentTemp, 2);
    json += ",\"humidity\":" + String(currentHumidity, 2);
    json += ",\"soil_moisture\":" + String(currentSoilPercent);
    json += ",\"soil_raw\":" + String(currentSoilRaw);
    json += ",\"soil_status\":\"" + currentSoilStatus + "\"";
    json += "}";

    int httpCode = http.POST(json);

    if (httpCode == 200) {
        cloudSuccessCount++;
        if (cloudSuccessCount % 60 == 1) {
            Serial.print("[CLOUD] OK #");
            Serial.print(cloudSuccessCount);
            Serial.print(" | Falhas: ");
            Serial.println(cloudFailCount);
        }
    } else {
        cloudFailCount++;
        Serial.print("[CLOUD] ERRO HTTP ");
        Serial.println(httpCode);
    }

    http.end();
}

// ==================== LEITURA DE SENSORES ====================
void readSensors() {
    sensors_event_t hum, temp;
    aht.getEvent(&hum, &temp);

    currentTemp = temp.temperature;
    currentHumidity = hum.relative_humidity;
    currentSoilRaw = readSoilRaw();
    addSoilReading(currentSoilRaw);
    currentSoilSmoothed = getSmoothedADC();
    currentSoilPercent = adcToPercent(currentSoilSmoothed);
    currentSoilStatus = getSoilStatusFromADC(currentSoilSmoothed);
}

// ==================== PAGINA WEB LOCAL (DEBUG) ====================
void handleRoot() {
    readSensors();

    String html = "<!DOCTYPE html><html><head>";
    html += "<meta charset='UTF-8'>";
    html += "<meta name='viewport' content='width=device-width,initial-scale=1'>";
    html += "<meta http-equiv='refresh' content='10'>";
    html += "<title>ESP32 Hoya</title>";
    html += "<style>";
    html += "body{font-family:-apple-system,sans-serif;background:#f5f5f7;color:#1d1d1f;padding:20px;max-width:500px;margin:0 auto}";
    html += "h1{font-size:1.3em;text-align:center;margin-bottom:4px}";
    html += ".sub{text-align:center;color:#86868b;font-size:0.8em;margin-bottom:20px}";
    html += ".card{background:#fff;border-radius:12px;padding:16px;margin-bottom:10px;box-shadow:0 1px 4px rgba(0,0,0,0.04)}";
    html += ".row{display:flex;justify-content:space-between;align-items:center;padding:6px 0}";
    html += ".label{color:#86868b;font-size:0.85em}.val{font-weight:700;font-size:1.1em}";
    html += ".alert{background:#fff0f0;color:#c5221f;text-align:center;padding:12px;border-radius:10px;font-weight:600;margin-bottom:10px}";
    html += ".ok{background:#f0faf2;color:#1b7a3a}";
    html += ".info{text-align:center;color:#aeaeb2;font-size:0.7em;margin-top:16px}";
    html += "</style></head><body>";

    html += "<h1>Hoya Pet ESP32</h1>";
    html += "<div class='sub'>Monitoramento local</div>";

    html += "<div class='card'>";
    html += "<div class='row'><span class='label'>Temperatura</span><span class='val'>" + String(currentTemp, 1) + " C</span></div>";
    html += "<div class='row'><span class='label'>Umidade Ar</span><span class='val'>" + String(currentHumidity, 1) + "%</span></div>";
    html += "<div class='row'><span class='label'>Solo ADC (bruto)</span><span class='val'>" + String(currentSoilRaw) + "</span></div>";
    html += "<div class='row'><span class='label'>Solo ADC (media)</span><span class='val'>" + String(currentSoilSmoothed) + "</span></div>";
    html += "<div class='row'><span class='label'>Umidade Solo</span><span class='val'>" + String(currentSoilPercent) + "%</span></div>";
    html += "<div class='row'><span class='label'>Status</span><span class='val'>" + currentSoilStatus + "</span></div>";
    html += "</div>";

    html += "<div class='card" + String(reservoirAlertActive ? " alert" : " ok") + "'>";
    html += currentIrrigationStatus;
    html += "</div>";

    html += "<div class='card'>";
    html += "<div class='row'><span class='label'>Cloud OK</span><span class='val'>" + String(cloudSuccessCount) + "</span></div>";
    html += "<div class='row'><span class='label'>Cloud Falhas</span><span class='val'>" + String(cloudFailCount) + "</span></div>";
    html += "</div>";

    html += "<div class='info'>Uptime: " + String(millis() / 60000) + "min | IP: " + WiFi.localIP().toString() + "</div>";
    html += "</body></html>";

    server.sendHeader("Cache-Control", "no-store");
    server.send(200, "text/html", html);
}

void handleData() {
    readSensors();

    String json = "{";
    json += "\"temperature\":" + String(currentTemp, 2);
    json += ",\"humidity\":" + String(currentHumidity, 2);
    json += ",\"soil_moisture\":" + String(currentSoilPercent);
    json += ",\"soil_raw\":" + String(currentSoilRaw);
    json += ",\"soil_status\":\"" + currentSoilStatus + "\"";
    json += ",\"irrigation_status\":\"" + currentIrrigationStatus + "\"";
    json += ",\"reservoir_alert\":" + String(reservoirAlertActive ? "true" : "false");
    json += ",\"pump_active\":" + String(pumpActive ? "true" : "false");
    json += ",\"cloud_ok\":" + String(cloudSuccessCount);
    json += ",\"cloud_fail\":" + String(cloudFailCount);
    json += ",\"uptime_ms\":" + String(millis());
    json += "}";

    server.sendHeader("Cache-Control", "no-store");
    server.sendHeader("Access-Control-Allow-Origin", "*");
    server.send(200, "application/json", json);
}

// ==================== COMANDO REMOTO DE BOMBA ====================
void handlePump() {
    if (!server.authenticate(BASIC_USER, BASIC_PASS)) {
        return server.requestAuthentication();
    }

    if (pumpActive) {
        server.send(409, "application/json", "{\"error\":\"Bomba ja ativa\"}");
        return;
    }

    int seconds = 3;
    if (server.hasArg("seconds")) {
        seconds = server.arg("seconds").toInt();
        if (seconds < 1) seconds = 1;
        if (seconds > 30) seconds = 30;
    }
    unsigned long durationMs = (unsigned long)seconds * 1000UL;

    Serial.print("[BOMBA] Comando remoto: ");
    Serial.print(seconds);
    Serial.println("s");

    String json = "{\"ok\":true,\"seconds\":" + String(seconds) + "}";
    server.sendHeader("Cache-Control", "no-store");
    server.send(200, "application/json", json);

    pumpActive = true;
    currentIrrigationStatus = "Irrigando (manual)...";
    digitalWrite(PUMP_PIN, HIGH);
    delay(durationMs);
    digitalWrite(PUMP_PIN, LOW);
    pumpActive = false;
    lastWateringTime = millis();
    currentIrrigationStatus = "Irrigou (manual) - aguardando absorcao";
    Serial.println("[BOMBA] Comando remoto finalizado.");
}

// ==================== BUSCA COMANDOS DO SERVIDOR ====================
void checkCommands() {
    if (WiFi.status() != WL_CONNECTED) return;

    String base = String(CLOUD_SERVER);
    int pos = base.lastIndexOf("/api/ingest");
    if (pos < 0) return;
    String cmdUrl  = base.substring(0, pos) + "/api/commands";
    String doneUrl = base.substring(0, pos) + "/api/commands/done";

    HTTPClient http;
    http.begin(cmdUrl);
    http.addHeader("X-API-Key", CLOUD_API_KEY);
    http.setTimeout(4000);
    int code = http.GET();

    if (code == 200) {
        String body = http.getString();
        int idx = body.indexOf("\"pump_seconds\":");
        if (idx >= 0) {
            int vs = idx + 15;
            while (vs < (int)body.length() && body[vs] == ' ') vs++;
            int ve = vs;
            while (ve < (int)body.length() && isDigit(body[ve])) ve++;
            int secs = body.substring(vs, ve).toInt();

            if (secs > 0 && !pumpActive) {
                Serial.print("[CMD] Rega remota: ");
                Serial.print(secs);
                Serial.println("s");

                pumpActive = true;
                currentIrrigationStatus = "Irrigando (remoto)...";
                digitalWrite(PUMP_PIN, HIGH);
                delay((unsigned long)secs * 1000UL);
                digitalWrite(PUMP_PIN, LOW);
                pumpActive = false;
                lastWateringTime = millis();
                currentIrrigationStatus = "Irrigou (remoto) - aguardando absorcao";
                Serial.println("[CMD] Rega remota concluida.");

                http.end();
                HTTPClient done;
                done.begin(doneUrl);
                done.addHeader("X-API-Key", CLOUD_API_KEY);
                done.addHeader("Content-Length", "0");
                done.POST("");
                done.end();
                return;
            }
        }
    }
    http.end();
}

// ==================== WIFI ====================
void connectWiFi() {
    WiFi.mode(WIFI_STA);

    if (!WiFi.config(local_IP, gateway, subnet, primaryDNS, secondaryDNS)) {
        Serial.println("[WIFI] Falha IP fixo");
    }

    WiFi.begin(ssid, password);
    Serial.print("[WIFI] Conectando");

    int tries = 0;
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
        tries++;
        if (tries > 60) {
            Serial.println("\n[WIFI] Timeout. Reiniciando...");
            WiFi.disconnect(true);
            delay(500);
            WiFi.begin(ssid, password);
            tries = 0;
        }
    }

    Serial.println();
    Serial.print("[WIFI] IP: ");
    Serial.println(WiFi.localIP());
}

// ==================== SETUP ====================
void setup() {
    Serial.begin(115200);
    delay(1000);

    Serial.println("\n====================================");
    Serial.println("  HOYA PET - ESP32-C3 + IRRIGACAO  ");
    Serial.println("====================================");

    pinMode(PUMP_PIN, OUTPUT);
    digitalWrite(PUMP_PIN, LOW);
    Serial.println("[BOMBA] GPIO3 configurado (desligada)");

    pinMode(SOIL_SENSOR_PIN, INPUT);
    analogReadResolution(12);
    Serial.print("[SOLO] GPIO4 | Faixas ADC: SECO>");
    Serial.print(ADC_DRY);
    Serial.print(" UMIDO<");
    Serial.println(ADC_IDEAL_MAX);

    Wire.begin(I2C_SDA, I2C_SCL);
    if (!aht.begin(&Wire)) {
        Serial.println("[AHT10] FALHOU! Verifique conexoes.");
    } else {
        Serial.println("[AHT10] OK");
    }

    connectWiFi();

    server.on("/", handleRoot);
    server.on("/data", handleData);
    server.on("/pump", handlePump);
    server.begin();
    Serial.println("[HTTP] Servidor local iniciado");

    Serial.print("[CLOUD] Destino: ");
    Serial.println(CLOUD_SERVER);
    Serial.println("====================================\n");
}

// ==================== LOOP ====================
void loop() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WIFI] Desconectado. Reconectando...");
        connectWiFi();
    }

    server.handleClient();

    unsigned long now = millis();
    if (now - lastCloudSend >= CLOUD_SEND_INTERVAL) {
        lastCloudSend = now;
        readSensors();
        sendToCloud();
        checkCommands();
    }

    checkIrrigation();

    static unsigned long lastLog = 0;
    if (now - lastLog > 60000) {
        lastLog = now;
        Serial.println("\n--- STATUS ---");
        Serial.print("Temp: ");      Serial.print(currentTemp, 1);      Serial.println(" C");
        Serial.print("Umid: ");      Serial.print(currentHumidity, 1);  Serial.println("%");
        Serial.print("Solo: ");      Serial.print(currentSoilPercent);  Serial.println("%");
        Serial.print("Irrigacao: "); Serial.println(currentIrrigationStatus);
        Serial.print("Cloud: ");     Serial.print(cloudSuccessCount);
        Serial.print(" OK / ");      Serial.print(cloudFailCount);      Serial.println(" falhas");
        Serial.println("--------------");
    }
}
