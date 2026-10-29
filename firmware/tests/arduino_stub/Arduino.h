#ifndef TEST_ARDUINO_H
#define TEST_ARDUINO_H
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <string>
class __FlashStringHelper;
#define F(s) (reinterpret_cast<const __FlashStringHelper *>(s))
uint32_t millis();
uint32_t micros();
class Print {
public:
    virtual ~Print() {}
    virtual size_t write(uint8_t value) = 0;
    size_t print(const char *value) { size_t n = 0; while (*value) n += write((uint8_t)*value++); return n; }
    size_t print(const __FlashStringHelper *value) { return print(reinterpret_cast<const char *>(value)); }
    size_t print(char value) { return write((uint8_t)value); }
    size_t print(uint32_t value) { char number[20]; snprintf(number, sizeof(number), "%lu", (unsigned long)value); return print(number); }
    size_t print(float value, int decimals) { char number[40]; snprintf(number, sizeof(number), "%.*f", decimals, (double)value); return print(number); }
    size_t println(const char *value) { size_t n = print(value); return n + print("\r\n"); }
    size_t println(const __FlashStringHelper *value) { size_t n = print(value); return n + print("\r\n"); }
    size_t println(char value) { size_t n = print(value); return n + print("\r\n"); }
};
class Stream : public Print { public: virtual void flush() = 0; };
#endif
