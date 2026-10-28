// ColorEasyDuino (ATmega328P / Arduino Uno): synthetic data over USB serial.
// This is generated test data, not measurements from a physical sensor.
#include <Arduino.h>

#define EDGE_DEVICE_ID "uno-01"
const uint32_t SERIAL_BAUD = 115200UL;
const uint16_t NORMAL_SAMPLES = 256;
const uint16_t URGENT_SAMPLES = 16;
const uint8_t URGENT_EVERY = 5;
const uint32_t FRAME_GAP_MS = 250UL;
const uint8_t HEARTBEAT_PIN = 13;
const uint32_t HEARTBEAT_INTERVAL_MS = 1000UL;

static_assert(NORMAL_SAMPLES > 0 && NORMAL_SAMPLES <= 512,
              "Keep normal frames small for the USB serial demo");
static_assert(URGENT_SAMPLES > 0 && URGENT_SAMPLES <= NORMAL_SAMPLES,
              "Urgent frames must be nonempty and no larger than normal frames");
static_assert(URGENT_EVERY > 0, "URGENT_EVERY must be positive");

uint32_t sequence = 0;
uint32_t lastFrameCompletedMs = 0;
uint32_t lastHeartbeatMs = 0;
bool heartbeatOn = false;

void updateHeartbeat() {
  const uint32_t now = millis();
  if (now - lastHeartbeatMs >= HEARTBEAT_INTERVAL_MS) {
    lastHeartbeatMs = now;
    heartbeatOn = !heartbeatOn;
    digitalWrite(HEARTBEAT_PIN, heartbeatOn ? HIGH : LOW);
  }
}

int16_t syntheticSample(uint16_t index, uint32_t seq) {
  // Convert to signed before subtraction: AVR unsigned underflow is forbidden.
  return static_cast<int16_t>((index + (seq % 201UL)) % 201UL) - 100;
}

void sendFrame(uint32_t seq) {
  // seq=4,9,... are urgent: every fifth frame, with sequence starting at zero.
  const bool urgent = seq % URGENT_EVERY == URGENT_EVERY - 1;
  const uint16_t count = urgent ? URGENT_SAMPLES : NORMAL_SAMPLES;
  // Stream fields and samples directly. Never allocate a frame or samples array.
  // F() keeps literals in program flash instead of the Uno's 2 KiB SRAM.
  Serial.print(F("{\"device_id\":\""));
  Serial.print(F(EDGE_DEVICE_ID));
  Serial.print(F("\",\"seq\":"));
  Serial.print(seq);
  Serial.print(F(",\"priority\":\""));
  if (urgent) {
    Serial.print(F("urgent"));
  } else {
    Serial.print(F("normal"));
  }
  Serial.print(F("\",\"source\":\"synthetic\",\"samples\":["));
  for (uint16_t index = 0; index < count; ++index) {
    if (index != 0) {
      Serial.print(',');
    }
    Serial.print(syntheticSample(index, seq));
    updateHeartbeat();
  }
  Serial.println(F("]}")); // CRLF is accepted by the existing NDJSON protocol.
  // HardwareSerial printing and flush can block while bytes leave the UART.
  // Heartbeat timing can slip; this device does not make hard real-time claims.
  Serial.flush();
}

void setup() {
  pinMode(HEARTBEAT_PIN, OUTPUT);
  digitalWrite(HEARTBEAT_PIN, LOW);
  Serial.begin(SERIAL_BAUD);
  // Do not print startup/debug text: every serial line must be a protocol frame.
}

void loop() {
  updateHeartbeat();
  const uint32_t now = millis();
  if (now - lastFrameCompletedMs >= FRAME_GAP_MS) {
    sendFrame(sequence);
    ++sequence; // Defined uint32 wrap; reset/wrap can repeat device_id + seq.
    lastFrameCompletedMs = millis(); // Minimum gap after the complete frame.
  }
}
