/* ESP32-C3 synthetic task source. TCP payloads go over Wi-Fi ONLY.
 * Arduino ESP32 3.3.12, FQBN:
 * esp32:esp32:esp32c3:FlashMode=dio,FlashFreq=40,FlashSize=8M
 * USB serial is a temporary configuration/control channel, not a data bridge.
 * CONFIG -> SSID line -> password line. SCAN scans without connecting.
 * TCP port 9001 accepts START <1..100> followed by LF; closes after that batch.
 * No application ACK, deduplication, persistent queue or uncertain-frame replay.
 */
#include <Arduino.h>
#include <WiFi.h>
#include <errno.h>
#include <lwip/sockets.h>

#if !CONFIG_IDF_TARGET_ESP32C3
#error "Select ESP32C3 Dev Module"
#endif

static constexpr uint16_t TCP_PORT = 9001;
static constexpr uint32_t CONFIG_TIMEOUT_MS = 15000;
static constexpr uint32_t FRAME_TIMEOUT_MS = 5000;
static constexpr size_t FRAME_CAPACITY = 2048;
static WiFiServer server(TCP_PORT, 1);
static bool serverStarted = false;
static String selectedSsid = "Shino";
static uint32_t nextSequence = 0; // Survives connections, not resets; may wrap.
static char frame[FRAME_CAPACITY];
static char control[32];
static size_t controlLength = 0;
static bool controlOverflow = false;
static uint32_t controlStarted = 0;

/* Unlike Stream.readStringUntil(), an absolute deadline cannot be extended by
 * a sender dripping one character before each per-character timeout. Buffers
 * are capped and passwords are never trimmed or printed. */
static bool serialLine(String &value, size_t maximum, uint32_t timeoutMs) {
  value = "";
  value.reserve(maximum);
  const uint32_t started = millis();
  bool overflow = false;
  while (millis() - started < timeoutMs) {
    while (Serial.available()) {
      const int input = Serial.read();
      if (input < 0) break;
      if (input == '\n') {
        // Only strip CR as part of CRLF framing, never spaces or punctuation.
        if (value.endsWith("\r")) value.remove(value.length() - 1);
        return !overflow;
      }
      if (value.length() < maximum + 1) value += static_cast<char>(input);
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
  WiFi.disconnect(true, false); // No flash erasure or saved credentials.
}

static void scanTarget() {
  if (serverStarted) {
    Serial.println("SCAN_BUSY use CONFIG to change network");
    return;
  }
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  const int count = WiFi.scanNetworks(false, false, false, 120);
  bool found = false;
  for (int index = 0; index < count; ++index) {
    if (WiFi.SSID(index) == selectedSsid) {
      found = true;
      Serial.printf("TARGET_AP rssi_dbm=%d channel=%d auth=%d\n",
                    WiFi.RSSI(index), WiFi.channel(index),
                    static_cast<int>(WiFi.encryptionType(index)));
    }
  }
  Serial.printf("SCAN count=%d target_seen=%d\n", count, found);
  WiFi.scanDelete();
  WiFi.mode(WIFI_OFF);
}

static void configureWifi() {
  stopWifi();
  String ssid, password;
  Serial.println("CONFIG_SSID");
  if (!serialLine(ssid, 32, CONFIG_TIMEOUT_MS) || ssid.isEmpty() || ssid.length() > 32) {
    Serial.println("CONFIG_FAILED field=ssid reason=timeout_or_length");
    return;
  }
  Serial.println("CONFIG_PASSWORD");
  if (!serialLine(password, 64, CONFIG_TIMEOUT_MS) || password.length() < 8 || password.length() > 63) {
    password = "";
    Serial.println("CONFIG_FAILED field=password reason=timeout_or_length");
    return;
  }
  selectedSsid = ssid;
  WiFi.persistent(false);
  WiFi.setAutoReconnect(false); // One bounded attempt, explicit CONFIG on failure.
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  const uint32_t started = millis();
  WiFi.begin(ssid.c_str(), password.c_str());
  for (size_t i = 0; i < password.length(); ++i) password.setCharAt(i, '\0');
  password = "";
  while (WiFi.status() != WL_CONNECTED && millis() - started < 30000) delay(20);
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("WIFI_FAILED status=%d waiting=CONFIG\n", static_cast<int>(WiFi.status()));
    stopWifi();
    return;
  }
  server.begin();
  server.setNoDelay(true);
  serverStarted = static_cast<bool>(server);
  if (!serverStarted) {
    Serial.println("SERVER_FAILED waiting=CONFIG");
    stopWifi();
    return;
  }
  Serial.printf("WIFI_READY ip=%s port=%u rssi_dbm=%d channel=%d connect_ms=%lu\n",
                WiFi.localIP().toString().c_str(), TCP_PORT, WiFi.RSSI(),
                WiFi.channel(), static_cast<unsigned long>(millis() - started));
}

static bool readStart(WiFiClient &client, unsigned &count) {
  char command[24];
  size_t length = 0;
  const uint32_t started = millis();
  while (millis() - started < 5000 && WiFi.status() == WL_CONNECTED) {
    while (client.available()) {
      const int input = client.read();
      if (input < 0) break;
      if (input == '\n') {
        if (length && command[length - 1] == '\r') --length;
        command[length] = '\0';
        if (length <= 6 || strncmp(command, "START ", 6) != 0) return false;
        unsigned value = 0;
        for (size_t i = 6; i < length; ++i) {
          if (command[i] < '0' || command[i] > '9') return false;
          value = value * 10U + static_cast<unsigned>(command[i] - '0');
          if (value > 100) return false;
        }
        if (value == 0) return false;
        count = value;
        return true;
      }
      if (length >= sizeof(command) - 1) return false;
      command[length++] = static_cast<char>(input);
    }
    if (!client.connected()) return false;
    delay(1);
  }
  return false;
}

static size_t makeFrame(uint32_t seq) {
  const bool urgent = seq % 5 == 4;
  const size_t samples = urgent ? 16 : 256;
  int length = snprintf(frame, sizeof(frame),
      "{\"device_id\":\"esp32c3-01\",\"seq\":%lu,\"priority\":\"%s\","
      "\"source\":\"synthetic\",\"samples\":[",
      static_cast<unsigned long>(seq), urgent ? "urgent" : "normal");
  if (length < 0 || static_cast<size_t>(length) >= sizeof(frame)) return 0;
  size_t used = static_cast<size_t>(length);
  for (size_t i = 0; i < samples; ++i) {
    const int sample = static_cast<int>((i + seq % 201) % 201) - 100;
    length = snprintf(frame + used, sizeof(frame) - used, "%s%d", i ? "," : "", sample);
    if (length < 0 || static_cast<size_t>(length) >= sizeof(frame) - used) return 0;
    used += static_cast<size_t>(length);
  }
  if (used + 3 >= sizeof(frame)) return 0;
  frame[used++] = ']'; frame[used++] = '}'; frame[used++] = '\n';
  frame[used] = '\0';
  return used;
}

/* Nonblocking send plus one absolute per-frame deadline handles partial writes
 * without NetworkClient.write()'s retry counter extending under slow progress. */
static bool sendFrame(WiFiClient &client, size_t length, size_t &sent) {
  sent = 0;
  const uint32_t started = millis();
  const int fd = client.fd();
  if (fd < 0) return false;
  while (sent < length && millis() - started < FRAME_TIMEOUT_MS) {
    if (WiFi.status() != WL_CONNECTED) return false;
    const int result = send(fd, frame + sent, length - sent, MSG_DONTWAIT);
    if (result > 0) sent += static_cast<size_t>(result);
    else if (result == 0) return false;
    else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) return false;
    if (sent < length) delay(1);
  }
  return sent == length;
}

static void serveClient(WiFiClient &client) {
  unsigned requested = 0, completed = 0;
  client.setNoDelay(true);
  if (!readStart(client, requested)) {
    Serial.println("STREAM_REJECTED reason=invalid_START_or_timeout");
    client.stop();
    return;
  }
  Serial.printf("STREAM_START count=%u\n", requested);
  for (unsigned i = 0; i < requested; ++i) {
    // Consume sequence before send; never replay a possibly partial frame.
    const uint32_t seq = nextSequence++;
    const size_t length = makeFrame(seq);
    size_t sent = 0;
    if (!length || !sendFrame(client, length, sent)) {
      Serial.printf("STREAM_FAILED seq=%lu sent=%u expected=%u uncertain=%d\n",
                    static_cast<unsigned long>(seq), static_cast<unsigned>(sent),
                    static_cast<unsigned>(length), sent > 0);
      client.stop();
      return;
    }
    ++completed;
    Serial.printf("FRAME_SENT seq=%lu bytes=%u\n", static_cast<unsigned long>(seq),
                  static_cast<unsigned>(length));
    const uint32_t gapStarted = millis();
    while (millis() - gapStarted < 100 && WiFi.status() == WL_CONNECTED) delay(1);
  }
  Serial.printf("STREAM_DONE count=%u\n", completed);
  client.stop(); // EOF ends a batch. This is not an application-layer ACK.
}

void setup() {
  Serial.begin(115200);
  WiFi.persistent(false);
  WiFi.setAutoReconnect(false);
  delay(1200);
  Serial.println("READY commands=CONFIG,SCAN tcp=9001 no_network_ACK=1");
}

void loop() {
  // Bounded serial command assembly; discard an oversized line until LF.
  unsigned budget = 64;
  while (Serial.available() && budget--) {
    const int input = Serial.read();
    if (input < 0) break;
    if (!controlLength && !controlOverflow) controlStarted = millis();
    if (input == '\n') {
      if (controlLength && control[controlLength - 1] == '\r') --controlLength;
      control[controlLength] = '\0';
      if (controlOverflow) Serial.println("CONTROL_REJECTED reason=too_long");
      else if (strcmp(control, "CONFIG") == 0) configureWifi();
      else if (strcmp(control, "SCAN") == 0) scanTarget();
      else Serial.println("CONTROL_REJECTED reason=unknown_command");
      controlLength = 0; controlOverflow = false;
    } else if (controlLength < sizeof(control) - 1 && !controlOverflow) {
      control[controlLength++] = static_cast<char>(input);
    } else controlOverflow = true;
  }
  if ((controlLength || controlOverflow) && millis() - controlStarted > CONFIG_TIMEOUT_MS) {
    controlLength = 0; controlOverflow = false;
    Serial.println("CONTROL_REJECTED reason=timeout");
  }
  if (serverStarted && WiFi.status() != WL_CONNECTED) {
    Serial.println("WIFI_LOST waiting=CONFIG no_replay=1");
    stopWifi();
  }
  if (serverStarted) {
    WiFiClient client = server.accept();
    if (client) serveClient(client);
  }
  delay(1);
}
