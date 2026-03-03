#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_AHTX0.h>

// ====== WIFI ======
const char* ssid     = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

// ====== IP FIXO (rede local) ======
IPAddress local_IP(192, 168, 101, 200);
IPAddress gateway(192, 168, 101, 1);
IPAddress subnet(255, 255, 255, 0);
IPAddress primaryDNS(8, 8, 8, 8);
IPAddress secondaryDNS(1, 1, 1, 1);

// ====== LOGIN (servidor local) ======
const char* http_user = "YOUR_ESP32_USER";
const char* http_pass = "YOUR_ESP32_PASS";

// ====== CLOUD SERVER ======
// >>> ALTERE PARA O IP DA SUA VPS <<<
const char* CLOUD_SERVER = "http://YOUR_VPS_IP:8000/api/ingest";
const char* CLOUD_API_KEY = "YOUR_INGEST_API_KEY";
const unsigned long CLOUD_SEND_INTERVAL = 5000; // ms (5 segundos)

// ====== SENSOR DE SOLO CALIBRADO ======
#define SOIL_SENSOR_PIN 4
const int SOIL_DRY = 2850;
const int SOIL_WET = 1350;

// ====== THRESHOLDS PARA HOYA ======
const int THRESHOLD_IDEAL_MIN = 20;
const int THRESHOLD_IDEAL_MAX = 45;
const int THRESHOLD_DRY = 25;

// ====== SENSORES ======
Adafruit_AHTX0 aht;
WebServer server(80);

// ====== Controle de envio ======
unsigned long lastCloudSend = 0;
unsigned long cloudSuccessCount = 0;
unsigned long cloudFailCount = 0;

// ====== FUNCOES DO SENSOR DE SOLO ======
int readSoilMoisture() {
  int rawValue = analogRead(SOIL_SENSOR_PIN);
  int moisture = map(rawValue, SOIL_DRY, SOIL_WET, 0, 100);
  moisture = constrain(moisture, 0, 100);
  return moisture;
}

int getRawSoilValue() {
  return analogRead(SOIL_SENSOR_PIN);
}

String getSoilStatus(int moisture) {
  if (moisture < THRESHOLD_DRY) return "SECO - Precisa regar!";
  if (moisture < THRESHOLD_IDEAL_MIN) return "Levemente seco";
  if (moisture <= THRESHOLD_IDEAL_MAX) return "IDEAL para Hoya";
  if (moisture < 70) return "Umido - Nao regar";
  return "MUITO UMIDO - Risco!";
}

String getSoilColor(int moisture) {
  if (moisture < THRESHOLD_DRY) return "#ef4444";
  if (moisture < THRESHOLD_IDEAL_MIN) return "#f59e0b";
  if (moisture <= THRESHOLD_IDEAL_MAX) return "#10b981";
  if (moisture < 70) return "#3b82f6";
  return "#dc2626";
}

String getSoilEmoji(int moisture) {
  if (moisture < THRESHOLD_DRY) return "&#128308;";
  if (moisture < THRESHOLD_IDEAL_MIN) return "&#128993;";
  if (moisture <= THRESHOLD_IDEAL_MAX) return "&#128994;";
  if (moisture < 70) return "&#128309;";
  return "&#9888;";
}

// ====== AUTENTICACAO ======
bool checkAuth() {
  if (!server.authenticate(http_user, http_pass)) {
    server.requestAuthentication();
    return false;
  }
  return true;
}

// ====== ENVIO PARA CLOUD ======
void sendToCloud(float temp, float humidity, int soilMoisture, int soilRaw, String soilStatus) {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.begin(CLOUD_SERVER);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", CLOUD_API_KEY);
  http.setTimeout(5000);

  String json = "{";
  json += "\"temperature\":" + String(temp, 2);
  json += ",\"humidity\":" + String(humidity, 2);
  json += ",\"soil_moisture\":" + String(soilMoisture);
  json += ",\"soil_raw\":" + String(soilRaw);
  json += ",\"soil_status\":\"" + soilStatus + "\"";
  json += "}";

  int httpCode = http.POST(json);

  if (httpCode == 200) {
    cloudSuccessCount++;
    if (cloudSuccessCount % 60 == 1) { // Log a cada ~5 min
      Serial.print("CLOUD OK #");
      Serial.print(cloudSuccessCount);
      Serial.print(" | Falhas: ");
      Serial.println(cloudFailCount);
    }
  } else {
    cloudFailCount++;
    Serial.print("CLOUD ERRO: HTTP ");
    Serial.print(httpCode);
    Serial.print(" | ");
    Serial.println(http.errorToString(httpCode));
  }

  http.end();
}

// ====== PAGINA WEB ======
void handleRoot() {
  if (!checkAuth()) return;

  sensors_event_t humidity, temp;
  aht.getEvent(&humidity, &temp);

  int soilMoisture = readSoilMoisture();
  int soilRaw = getRawSoilValue();
  String soilStatus = getSoilStatus(soilMoisture);
  String soilColor = getSoilColor(soilMoisture);
  String soilEmoji = getSoilEmoji(soilMoisture);

  String html = "<!DOCTYPE html><html><head>";
  html += "<meta charset='UTF-8'>";
  html += "<meta name='viewport' content='width=device-width, initial-scale=1'>";
  html += "<meta http-equiv='refresh' content='5'>";
  html += "<title>ESP32 Monitoramento Hoya</title>";
  html += "<style>";
  html += "* { margin: 0; padding: 0; box-sizing: border-box; }";
  html += "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;";
  html += "background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);";
  html += "min-height: 100vh; padding: 20px; color: #fff; }";
  html += ".container { max-width: 1000px; margin: 0 auto; }";
  html += "h1 { text-align: center; margin-bottom: 10px; font-size: 1.8em; text-shadow: 2px 2px 4px rgba(0,0,0,0.2); }";
  html += ".subtitle { text-align: center; opacity: 0.8; margin-bottom: 30px; font-size: 0.9em; }";
  html += ".grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }";
  html += ".card { background: rgba(255,255,255,0.15); backdrop-filter: blur(10px);";
  html += "border-radius: 20px; padding: 30px; box-shadow: 0 8px 32px rgba(0,0,0,0.1);";
  html += "border: 1px solid rgba(255,255,255,0.18); transition: transform 0.3s; }";
  html += ".card:hover { transform: translateY(-5px); }";
  html += ".sensor-icon { font-size: 3em; text-align: center; margin-bottom: 15px; }";
  html += ".sensor-title { text-align: center; font-size: 1.1em; opacity: 0.9; margin-bottom: 20px; }";
  html += ".reading { text-align: center; margin: 15px 0; }";
  html += ".value { font-size: 2.5em; font-weight: 700; line-height: 1; }";
  html += ".label { font-size: 0.9em; opacity: 0.8; margin-top: 5px; }";
  html += ".status { text-align: center; margin-top: 15px; padding: 12px;";
  html += "background: rgba(255,255,255,0.2); border-radius: 10px; font-weight: 600; font-size: 1.05em; }";
  html += ".raw { text-align: center; font-size: 0.75em; opacity: 0.6; margin-top: 10px; }";
  html += ".cloud-status { text-align: center; margin-top: 20px; padding: 10px;";
  html += "background: rgba(255,255,255,0.1); border-radius: 10px; font-size: 0.85em; }";
  html += ".footer { text-align: center; margin-top: 30px; opacity: 0.7; font-size: 0.9em; }";
  html += "@media (max-width: 600px) { .value { font-size: 2em; } }";
  html += "</style></head><body>";

  html += "<div class='container'>";
  html += "<h1>&#127807; Monitoramento ESP32-C3</h1>";
  html += "<div class='subtitle'>Sistema de monitoramento para Hoya (Flor-de-cera)</div>";

  html += "<div class='grid'>";

  // Card Temperatura
  html += "<div class='card'>";
  html += "<div class='sensor-icon'>&#127777;&#65039;</div>";
  html += "<div class='sensor-title'>Temperatura</div>";
  html += "<div class='reading'>";
  html += "<div class='value'>" + String(temp.temperature, 1) + "&deg;C</div>";
  html += "<div class='label'>Ambiente</div>";
  html += "</div></div>";

  // Card Umidade do Ar
  html += "<div class='card'>";
  html += "<div class='sensor-icon'>&#128168;</div>";
  html += "<div class='sensor-title'>Umidade do Ar</div>";
  html += "<div class='reading'>";
  html += "<div class='value'>" + String(humidity.relative_humidity, 1) + "%</div>";
  html += "<div class='label'>AHT10</div>";
  html += "</div></div>";

  // Card Solo
  html += "<div class='card'>";
  html += "<div class='sensor-icon'>" + soilEmoji + "</div>";
  html += "<div class='sensor-title'>Umidade do Solo</div>";
  html += "<div class='reading'>";
  html += "<div class='value' style='color:" + soilColor + "'>" + String(soilMoisture) + "%</div>";
  html += "<div class='label'>Sensor Capacitivo v1.2</div>";
  html += "</div>";
  html += "<div class='status' style='color:" + soilColor + "'>" + soilStatus + "</div>";
  html += "<div class='raw'>ADC: " + String(soilRaw) + " | Cal: " + String(SOIL_DRY) + "-" + String(SOIL_WET) + "</div>";
  html += "</div>";

  html += "</div>"; // fim grid

  // Cloud status
  html += "<div class='cloud-status'>";
  html += "&#9729;&#65039; Cloud: " + String(cloudSuccessCount) + " enviados | " + String(cloudFailCount) + " falhas";
  html += " | Server: " + String(CLOUD_SERVER);
  html += "</div>";

  html += "<div class='footer'>Atualiza a cada 5 segundos<br>";
  html += "Uptime: " + String(millis()/1000) + "s | IP: " + WiFi.localIP().toString() + "</div>";

  html += "</div></body></html>";

  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "text/html", html);
}

// ====== ENDPOINT JSON (local) ======
void handleData() {
  if (!checkAuth()) return;

  sensors_event_t humidity, temp;
  aht.getEvent(&humidity, &temp);

  int soilMoisture = readSoilMoisture();
  int soilRaw = getRawSoilValue();

  String json = "{";
  json += "\"temperature\":" + String(temp.temperature, 2);
  json += ",\"humidity\":" + String(humidity.relative_humidity, 2);
  json += ",\"soil_moisture\":" + String(soilMoisture);
  json += ",\"soil_raw\":" + String(soilRaw);
  json += ",\"soil_status\":\"" + getSoilStatus(soilMoisture) + "\"";
  json += ",\"thresholds\":{";
  json += "\"dry\":" + String(THRESHOLD_DRY);
  json += ",\"ideal_min\":" + String(THRESHOLD_IDEAL_MIN);
  json += ",\"ideal_max\":" + String(THRESHOLD_IDEAL_MAX);
  json += "}";
  json += ",\"calibration\":{\"dry\":" + String(SOIL_DRY) + ",\"wet\":" + String(SOIL_WET) + "}";
  json += ",\"cloud_ok\":" + String(cloudSuccessCount);
  json += ",\"cloud_fail\":" + String(cloudFailCount);
  json += ",\"uptime_ms\":" + String(millis());
  json += "}";

  server.sendHeader("Cache-Control", "no-store");
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", json);
}

// ====== WIFI ======
void connectWiFiStaticIP() {
  WiFi.mode(WIFI_STA);

  if (!WiFi.config(local_IP, gateway, subnet, primaryDNS, secondaryDNS)) {
    Serial.println("Falha ao configurar IP fixo");
  }

  WiFi.begin(ssid, password);
  Serial.print("Conectando WiFi (IP fixo ");
  Serial.print(local_IP);
  Serial.print(")");

  int tries = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    tries++;
    if (tries > 60) {
      Serial.println("\nNao conectou. Reiniciando WiFi...");
      WiFi.disconnect(true);
      delay(500);
      WiFi.begin(ssid, password);
      tries = 0;
    }
  }

  Serial.println();
  Serial.print("IP: "); Serial.println(WiFi.localIP());
  Serial.print("Gateway: "); Serial.println(WiFi.gatewayIP());
  Serial.print("MAC: "); Serial.println(WiFi.macAddress());
}

// ====== SETUP ======
void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("\n====================================");
  Serial.println("  MONITORAMENTO ESP32-C3 PARA HOYA  ");
  Serial.println("     + ENVIO PARA CLOUD SERVER       ");
  Serial.println("====================================");

  // Configura sensor de solo
  pinMode(SOIL_SENSOR_PIN, INPUT);
  Serial.println("Sensor de solo: GPIO4");
  Serial.print("  Calibracao: SECO="); Serial.print(SOIL_DRY);
  Serial.print(" | MOLHADO="); Serial.println(SOIL_WET);

  // Configura I2C para AHT10
  Wire.begin(8, 9); // SDA=GPIO8, SCL=GPIO9

  if (!aht.begin()) {
    Serial.println("AHT10 NAO encontrado!");
    Serial.println("  Verifique: VCC=3.3V, GND, SDA=8, SCL=9");
    while (1) delay(100);
  }
  Serial.println("AHT10 OK");

  // Conecta WiFi
  connectWiFiStaticIP();

  // Inicia servidor web local
  server.on("/", handleRoot);
  server.on("/data", handleData);
  server.begin();

  Serial.println("Servidor HTTP local iniciado");
  Serial.print("Cloud server: "); Serial.println(CLOUD_SERVER);
  Serial.println("====================================\n");
}

// ====== LOOP ======
void loop() {
  // Reconecta WiFi se necessario
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi caiu. Reconectando...");
    connectWiFiStaticIP();
  }

  server.handleClient();

  // Envio periodico para cloud
  if (millis() - lastCloudSend >= CLOUD_SEND_INTERVAL) {
    lastCloudSend = millis();

    sensors_event_t humidity, temp;
    aht.getEvent(&humidity, &temp);
    int soilMoisture = readSoilMoisture();
    int soilRaw = getRawSoilValue();
    String soilStatus = getSoilStatus(soilMoisture);

    sendToCloud(temp.temperature, humidity.relative_humidity, soilMoisture, soilRaw, soilStatus);
  }

  // Debug no Serial a cada 30 segundos
  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 30000) {
    lastPrint = millis();

    sensors_event_t humidity, temp;
    aht.getEvent(&humidity, &temp);
    int soilMoisture = readSoilMoisture();

    Serial.println("\n========== LEITURAS ==========");
    Serial.print("Temp: "); Serial.print(temp.temperature, 1); Serial.println(" C");
    Serial.print("Umid: "); Serial.print(humidity.relative_humidity, 1); Serial.println("%");
    Serial.print("Solo: "); Serial.print(soilMoisture); Serial.println("%");
    Serial.print("Cloud: "); Serial.print(cloudSuccessCount);
    Serial.print(" OK / "); Serial.print(cloudFailCount); Serial.println(" falhas");
    Serial.println("==============================");
  }
}
