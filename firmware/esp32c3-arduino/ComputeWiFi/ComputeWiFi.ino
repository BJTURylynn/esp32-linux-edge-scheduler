/* ESP32-C3 v2 compute worker, synthetic filter/RMS actually runs on this MCU.
 * Arduino ESP32 3.3.12: FlashMode=dio,FlashFreq=40,FlashSize=8M.
 * USB: CONFIG -> SSID -> password (never echoed/saved); SCAN/STATUS diagnostics.
 * TCP 9001: v2 HELLO/WELCOME/TASK/ACCEPT/RESULT and PING/PONG only.
 */
#include <Arduino.h>
#include <WiFi.h>
#include <EdgeWorkerArduino.h>
#include <errno.h>
#include <lwip/sockets.h>

#if !CONFIG_IDF_TARGET_ESP32C3
#error "Select ESP32C3 Dev Module"
#endif

static constexpr uint16_t TCP_PORT = 9001;
static constexpr uint32_t CONFIG_TIMEOUT_MS = 15000;
static WiFiServer server(TCP_PORT, 1);
static bool serverStarted = false;
static String selectedSsid = "Shino";
static char control[32];
static size_t controlLength = 0;
static bool controlOverflow = false;
static uint32_t controlStarted = 0;
static EdgeWorkerRuntime worker("esp32c3-01");

/* NetworkClient.flush() is not a portable output flush (some cores clear RX).
 * Accumulate a bounded JSON line and use an explicit nonblocking send deadline.
 * No kernel sleeps are used to simulate compute. */
class WorkerTCPStream : public Stream {
public:
  explicit WorkerTCPStream(WiFiClient &client) : client_(client), used_(0), healthy_(true) {}
  int available() override { return client_.available(); }
  int read() override { return client_.read(); }
  int peek() override { return client_.peek(); }
  size_t write(uint8_t value) override {
    if (!healthy_ || used_ >= sizeof(output_)) { healthy_ = false; return 0; }
    output_[used_++] = value;
    if (value == '\n') flush();
    return healthy_ ? 1 : 0;
  }
  size_t write(const uint8_t *data, size_t size) override {
    size_t n = 0;
    while (n < size && write(data[n])) ++n;
    return n;
  }
  void flush() override {
    size_t sent = 0;
    const uint32_t started = millis();
    const int fd = client_.fd();
    while (healthy_ && sent < used_) {
      if (fd < 0 || WiFi.status() != WL_CONNECTED || millis() - started >= 3000UL) {
        healthy_ = false; break;
      }
      const int result = send(fd, output_ + sent, used_ - sent, MSG_DONTWAIT);
      if (result > 0) sent += (size_t)result;
      else if (!result || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) healthy_ = false;
      if (sent < used_) delay(1); // Network backpressure only; outside compute.
    }
    used_ = 0;
  }
  bool healthy() const { return healthy_; }
private:
  WiFiClient &client_;
  uint8_t output_[EW_MAX_LINE + 2];
  size_t used_;
  bool healthy_;
};

static bool serialLine(String &value, size_t maximum, uint32_t timeoutMs) {
  value = ""; value.reserve(maximum);
  const uint32_t started = millis();
  bool overflow = false;
  while (millis() - started < timeoutMs) {
    while (Serial.available()) {
      const int input = Serial.read();
      if (input < 0) break;
      if (input == '\n') {
        if (value.endsWith("\r")) value.remove(value.length() - 1);
        return !overflow;
      }
      if (value.length() < maximum + 1) value += (char)input;
      else overflow = true;
      if (millis() - started >= timeoutMs) break;
    }
    delay(1);
  }
  return false;
}

static void stopWifi() {
  if (serverStarted) server.end();
  serverStarted = false;
  WiFi.disconnect(true, false);
}

static void scanTarget() {
  if (serverStarted) { Serial.println("SCAN_BUSY use CONFIG to change network"); return; }
  WiFi.persistent(false); WiFi.mode(WIFI_STA);
  const int count = WiFi.scanNetworks(false, false, false, 120);
  bool found = false;
  for (int i = 0; i < count; ++i) {
    if (WiFi.SSID(i) == selectedSsid) {
      found = true;
      Serial.printf("TARGET_AP rssi_dbm=%d channel=%d auth=%d\n", WiFi.RSSI(i),
                    WiFi.channel(i), (int)WiFi.encryptionType(i));
    }
  }
  Serial.printf("SCAN count=%d target_seen=%d\n", count, found);
  WiFi.scanDelete(); WiFi.mode(WIFI_OFF);
}

static void configureWifi() {
  stopWifi();
  String ssid, password;
  Serial.println("CONFIG_SSID");
  if (!serialLine(ssid, 32, CONFIG_TIMEOUT_MS) || ssid.isEmpty() || ssid.length() > 32) {
    Serial.println("CONFIG_FAILED field=ssid reason=timeout_or_length"); return;
  }
  Serial.println("CONFIG_PASSWORD");
  if (!serialLine(password, 64, CONFIG_TIMEOUT_MS) || password.length() < 8 || password.length() > 63) {
    password = "";
    Serial.println("CONFIG_FAILED field=password reason=timeout_or_length"); return;
  }
  selectedSsid = ssid;
  WiFi.persistent(false); WiFi.setAutoReconnect(false);
  WiFi.mode(WIFI_STA); WiFi.setSleep(false);
  const uint32_t started = millis();
  WiFi.begin(ssid.c_str(), password.c_str());
  for (size_t i = 0; i < password.length(); ++i) password.setCharAt(i, '\0');
  password = "";
  while (WiFi.status() != WL_CONNECTED && millis() - started < 30000UL) delay(20);
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("WIFI_FAILED status=%d waiting=CONFIG\n", (int)WiFi.status()); stopWifi(); return;
  }
  server.begin(); server.setNoDelay(true);
  serverStarted = (bool)server;
  if (!serverStarted) { Serial.println("SERVER_FAILED waiting=CONFIG"); stopWifi(); return; }
  Serial.printf("WIFI_READY ip=%s port=%u rssi_dbm=%d channel=%d connect_ms=%lu\n",
                WiFi.localIP().toString().c_str(), TCP_PORT, WiFi.RSSI(), WiFi.channel(),
                (unsigned long)(millis() - started));
}

/* Read-only observation is available even while the TCP worker is attached.
 * Connection to Wi-Fi and connection to the central scheduler are separate:
 * this line reports the former without echoing SSID or credentials. */
static void reportWifiStatus() {
  const bool connected = WiFi.status() == WL_CONNECTED;
  const String ip = connected ? WiFi.localIP().toString() : String("0.0.0.0");
  Serial.printf("WIFI_STATUS state=%s ip=%s port=%u rssi_dbm=%d protocol=2\n",
                connected ? "connected" : "disconnected", ip.c_str(), TCP_PORT,
                connected ? (int)WiFi.RSSI() : 0);
}

/* Bounded USB command service shared by the outer loop and attached worker.
 * Changing networks or scanning while attached could interrupt an in-flight
 * task. The gateway must disconnect first; STATUS never changes the network. */
static void pollControl(bool workerConnected) {
  unsigned budget = 64;
  while (Serial.available() && budget--) {
    const int input = Serial.read();
    if (input < 0) break;
    if (!controlLength && !controlOverflow) controlStarted = millis();
    if (input == '\n') {
      if (controlLength && control[controlLength - 1] == '\r') --controlLength;
      control[controlLength] = '\0';
      if (controlOverflow) Serial.println("CONTROL_REJECTED reason=too_long");
      else if (!strcmp(control, "STATUS")) reportWifiStatus();
      else if (!strcmp(control, "CONFIG") || !strcmp(control, "SCAN")) {
        if (workerConnected) Serial.println("CONTROL_REJECTED reason=worker_connected");
        else if (!strcmp(control, "CONFIG")) configureWifi();
        else scanTarget();
      } else Serial.println("CONTROL_REJECTED reason=unknown_command");
      controlLength = 0; controlOverflow = false;
    } else if (controlLength < sizeof(control) - 1 && !controlOverflow) control[controlLength++] = (char)input;
    else controlOverflow = true;
  }
  if ((controlLength || controlOverflow) && millis() - controlStarted > CONFIG_TIMEOUT_MS) {
    controlLength = 0; controlOverflow = false; Serial.println("CONTROL_REJECTED reason=timeout");
  }
}

static void serveClient(WiFiClient &client) {
  client.setNoDelay(true);
  WorkerTCPStream channel(client);
  worker.reset(); worker.hello(channel); channel.flush();
  Serial.println("WORKER_CONNECTED protocol=2");
  uint32_t lastReceived = millis();
  while (client.connected() && channel.healthy() && WiFi.status() == WL_CONNECTED) {
    pollControl(true);
    worker.poll(channel); channel.flush();
    unsigned budget = 128;
    while (client.available() && budget--) {
      const int input = client.read();
      if (input >= 0) { worker.feed(channel, input); lastReceived = millis(); }
    }
    if (millis() - lastReceived > 15000UL) break;
    delay(1);
  }
  client.stop(); worker.reset();
  Serial.println("WORKER_DISCONNECTED no_replay=1");
}

void setup() {
  Serial.begin(115200);
  WiFi.persistent(false); WiFi.setAutoReconnect(false);
  delay(1200);
  Serial.println("READY commands=CONFIG,SCAN,STATUS tcp=9001 protocol=2 compute=on_device");
}

void loop() {
  pollControl(false);
  if (serverStarted && WiFi.status() != WL_CONNECTED) {
    Serial.println("WIFI_LOST waiting=CONFIG no_replay=1"); stopWifi();
  }
  if (serverStarted) { WiFiClient client = server.accept(); if (client) serveClient(client); }
  delay(1);
}
