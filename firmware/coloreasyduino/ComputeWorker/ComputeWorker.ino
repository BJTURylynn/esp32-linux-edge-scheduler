// Uno / ColorEasyDuino v2 compute worker. Every serial line is v2 JSON.
// Compile with the local firmware/compute-worker Arduino library.
#include <Arduino.h>
#include <EdgeWorkerArduino.h>

static EdgeWorkerRuntime worker("uno-01");
static uint32_t lastHeartbeat = 0;
static bool heartbeat = false;

static void tickHeartbeat() {
  const uint32_t now = millis();
  if (now - lastHeartbeat >= 1000UL) {
    lastHeartbeat = now;
    heartbeat = !heartbeat;
    digitalWrite(13, heartbeat ? HIGH : LOW);
  }
}

void setup() {
  pinMode(13, OUTPUT);
  digitalWrite(13, LOW);
  Serial.begin(115200);
  worker.hello(Serial);
}

void loop() {
  tickHeartbeat();
  worker.poll(Serial);
  unsigned budget = 64;
  while (Serial.available() && budget--) worker.feed(Serial, Serial.read(), tickHeartbeat);
}
